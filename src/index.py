import boto3
from botocore.client import Config
import os
import json
import yaml
import difflib
import time
import datetime
from urllib.parse import urlparse

ROLE_ARN = os.environ['ROLE_ARN']
AWS_REGION = os.environ['AWS_DEFAULT_REGION']
WEB_URL = os.environ['WEB_URL']
BUCKET = os.environ['BUCKET']
BUCKET_URL = os.environ['BUCKET_URL']

cfn = boto3.client('cloudformation')
s3client = boto3.client('s3', config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))
pipeline=boto3.client('codepipeline')
sns=boto3.client('sns')



def lambda_handler(event, context):
    job=event['CodePipeline.job']
    job_id=event['CodePipeline.job']['id']
    print(json.dumps({'JobId': job_id}))
    try:
        params=yaml.safe_load(
            job['data']['actionConfiguration']['configuration']['UserParameters']
        )
    except:
        pipeline.put_job_failure_result(
            jobId=job_id,
            failureDetails={
                'type': 'JobFailed',
                'message': 'UserParameters is not valid JSON',
            }
        )
    change_sets = get_changesets(params['Stacks'])
    if len(change_sets) == 0:
        return pipeline.put_job_success_result(jobId=job_id)

    try:
        job_details = pipeline.get_job_details(jobId=job_id)['jobDetails']
        job['pipelineName'] = job_details['data']['pipelineContext']['pipelineName']

        all_changes = calculate_diff(change_sets, job)
        signed_url = put_changes(all_changes, job)
        send_notification(all_changes, params['TopicArn'], signed_url)
    except Exception as e:
        pipeline.put_job_failure_result(
            jobId=job_id,
            failureDetails={
                'type': 'JobFailed',
                'message': 'internal error',
            }
        )
        raise(e)


def aws_session(job_id):
    client = boto3.client('sts')
    response = client.assume_role(
        RoleArn=ROLE_ARN, RoleSessionName=f'pipeline-changes-{job_id}', Policy=json.dumps({
            'Statement': [{
                'Effect': 'Allow',
                'Resource': '*',
                'Action': [
                    'codepipeline:PutJobFailureResult',
                    'codepipeline:PutJobSuccessResult',
                    'codepipeline:GetJobDetails',
                ],
            }]}))
    return response['Credentials']


def get_changesets(stacks):
    change_sets = []
    for stack in stacks:
        cfnchange = list(filter(
            lambda x: x['ExecutionStatus'] == 'AVAILABLE',
            cfn.list_change_sets(StackName=stack)['Summaries'],
        ))

        if len(cfnchange):
            # FIXME: doesn't take into account existing changesets
            change_sets.append(cfnchange[0]['ChangeSetId'])
        else:
            print("no change set for the stack : " + stack)

    return change_sets


def put_changes(all_changes, job):
    job_id = job['id']
    s3client.put_object(Bucket=BUCKET,
                        Key=f'approvals/{job_id}.json',
                        Body=json.dumps(all_changes, default=default),
                        ContentType='application/json',
                        )
    url = s3client.generate_presigned_url(
        ClientMethod='get_object',
        Params={
            'Bucket': BUCKET,
            'Key': f'approvals/{job_id}.json'
        },
        ExpiresIn=1800)
    return f'{WEB_URL}#/{url.split("//")[1]}'


def send_notification(changes, topic_arn, signed_url):
    stacks = ', '.join(list(map(lambda x: x['StackName'], changes['Stacks'])))
    sns.publish(
        TopicArn=topic_arn,
        Message=f'Please approve or reject changes for {stacks}\n\n{signed_url}',
        Subject=f'Approval required: CodePipeline {changes["Pipeline"]["PipelineName"]} ({AWS_REGION})'
    )


def calculate_template_diff(cur_template, new_template):
    t_diff = difflib.unified_diff(
        cur_template.splitlines(True),
        new_template.splitlines(True),
    )
    return ''.join(t_diff)


def collect_parameters(template, change_set, stack):
    tpl_params = dict((x['ParameterKey'], x.get('DefaultValue'))
               for x in template.get('Parameters') or [])
    old = dict((x['ParameterKey'], x['ParameterValue'])
               for x in stack.get('Parameters') or [])
    new = dict((x['ParameterKey'], x['ParameterValue'])
               for x in change_set.get('Parameters') or [])
    params = []
    for name in new:
        param = {
            'Name': name,
            'Default': tpl_params.get(name),
            'CurrentValue': old.get(name),
            'NewValue': new[name],
        }
        params.append(param)

    return params

def get_drift_status(stackname):
    drift_id = cfn.detect_stack_drift(
        StackName = stackname
    )['StackDriftDetectionId']
    status = None
    while status is None or status == 'DETECTION_IN_PROGRESS':
        response = cfn.describe_stack_drift_detection_status(
            StackDriftDetectionId=drift_id
        )
        status = response['DetectionStatus']
        if status == 'DETECTION_COMPLETE':
            stack_drift_status = response['StackDriftStatus']
            return stack_drift_status
        elif status == 'DETECTION_IN_PROGRESS':
            time.sleep(2)
        else:
            return status

def get_drift_details(stackname):
    response = cfn.describe_stack_resource_drifts(
        StackName=stackname
    )
    resources = response['StackResourceDrifts']
    for resource in resources:
        actual_props = yaml.dump(yaml.safe_load(resource['ActualProperties']), default_flow_style=False)
        expected_props = yaml.dump(yaml.safe_load(resource['ExpectedProperties']), default_flow_style=False)
        resource['ActualProperties'] = actual_props
        resource['ExpectedProperties'] = expected_props
        if resource['StackResourceDriftStatus'] != 'IN_SYNC':
            diff = calculate_template_diff(expected_props, actual_props)
            resource['DriftDiff'] = diff
        else:
            resource['DriftDiff'] = ''

    return resources

def default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()

def calculate_diff(change_set_ids, job):
    stacks = []
    for change_set_id in change_set_ids:
        change_set = cfn.describe_change_set(
            ChangeSetName=change_set_id)
        stack_name = change_set['StackName']

        stack = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]

        new_template_info = cfn.get_template(
            ChangeSetName=change_set_id,
            TemplateStage='Processed',
        )
        new_template = get_canonical_template(new_template_info['TemplateBody'])

        status = stack['StackStatus']
        if status == 'REVIEW_IN_PROGRESS':
            # when a stack is created for the first time
            cur_template = ''
            cur_template_summary = {}
            drift_status = None
            drift_details = []
        else:
            cur_template_info = cfn.get_template(
                StackName=stack_name,
                TemplateStage='Processed',
            )
            cur_template_summary = cfn.get_template_summary(
                StackName=stack_name,
            )
            cur_template = get_canonical_template(cur_template_info['TemplateBody'])
            drift_status = get_drift_status(stack_name)
            drift_details = get_drift_details(stack_name)

        stacks.append({
            'StackName': stack_name,
            'Parameters': collect_parameters(cur_template_summary, change_set, stack),
            'TemplateDiff': calculate_template_diff(cur_template, new_template),
            'Changes': change_set['Changes'],
            'OldTemplate': cur_template,
            'DriftStatus': drift_status,
            'DriftDetails': drift_details
        })

    credentials = aws_session(job['id'])
    return {
        'Version': '1', # schema version
        'Pipeline': {
            'Region': AWS_REGION,
            'JobId': job['id'],
            'PipelineName': job['pipelineName'],
            'AccountId': job['accountId'],
        },
        'Credentials': {
            'AccessKeyId': credentials['AccessKeyId'],
            'SecretAccessKey': credentials['SecretAccessKey'],
            'SessionToken': credentials['SessionToken'],
        },
        'Stacks': stacks,
    }

# if the template deployed by the user is a yaml template and does not use
# any transforms then it will be returned as string instead of dict. In
# that case we don't try to parse and make it canonical as we cannot resolve
# YAML tags. We also retain comments in the template that way.

def get_canonical_template(body):
    if isinstance(body, str):
        return body
    else:
        return yaml.dump(
            yaml.safe_load(
                json.dumps(
                    body,
                    sort_keys=True,
                    default=str,
                )
            ),
            default_flow_style=False,
        )

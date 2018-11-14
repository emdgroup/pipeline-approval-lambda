import boto3
import os
import json
import yaml
import difflib
import json
from urllib.parse import urlparse

cfn = boto3.client('cloudformation')
s3client = boto3.client('s3')
pipeline = boto3.client('codepipeline')
sns = boto3.client('sns')

TOPIC = os.getenv('TOPIC')
ROLE_ARN = os.environ['ROLE_ARN']
AWS_REGION = os.environ['AWS_DEFAULT_REGION']
WEB_URL = os.environ['WEB_URL']
BUCKET = os.environ['BUCKET']
BUCKET_URL = os.environ['BUCKET_URL']


def lambda_handler(event, context):
    job = event['CodePipeline.job']
    job_id = event['CodePipeline.job']['id']
    print(json.dumps({'JobId': job_id}))
    try:
        params = json.loads(job['data']['actionConfiguration']
                            ['configuration']['UserParameters'])
    except:
        pipeline.put_job_failure_result(
            jobId=job_id,
            failureDetails={
                'type': 'JobFailed',
                'message': 'UserParameters is not valid JSON',
            }
        )
    change_sets = get_changeset_id(params['Stacks'])
    if len(change_sets) == 0:
        pipeline.put_job_success_result(jobId=job_id)
    else:
        try:
            describe_change_set(change_sets, job)
        except Exception as e:
            print(e)
            raise e
            pipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    'type': 'JobFailed',
                    'message': e,
                }
            )


def aws_session(job_id):
    client = boto3.client('sts')
    response = client.assume_role(
        RoleArn=ROLE_ARN, RoleSessionName=f'pipeline-changes-{job_id}', Policy=json.dumps({
            'Statement': [{
                'Effect': 'Allow',
                'Resource': '*',
                'Action': ['codepipeline:PutJobFailureResult', 'codepipeline:PutJobSuccessResult'],
            }]}))
    return response['Credentials']


def get_changeset_id(stacks):
    change_sets = []
    for stack in stacks:
        cfnchange = list(filter(
            lambda x: x['ExecutionStatus'] == 'AVAILABLE',
            cfn.list_change_sets(StackName=stack)['Summaries'],
        ))

        if len(cfnchange):
            change_sets.append(cfnchange[0]['ChangeSetId'])
        else:
            print("no change set for the stack : " + stack)

    return change_sets


def describe_change_set(change_sets, job):
    account_id = job['accountId']
    job_id = job['id']
    all_changes = calculate_diff(change_sets, job_id, account_id)
    s3client.put_object(Bucket=BUCKET,
                        Key=f'approvals/{job_id}.json',
                        Body=json.dumps(all_changes),
                        ContentType='application/json',
                        )
    url = s3client.generate_presigned_url(
        ClientMethod='get_object',
        Params={
            'Bucket': BUCKET,
            'Key': f'approvals/{job_id}.json'
        },
        ExpiresIn=1800)
    parsed = urlparse(url)
    signed_url = f'{WEB_URL}#/{BUCKET_URL}{parsed.path}?{parsed.query}'
    send_notification(f'Review the ChangeSets at: {signed_url}')


def send_notification(message):
    if TOPIC:
        sns.publish(
            TopicArn=TOPIC,
            Message=message,
            Subject='ChangeSets for the CloudFormation Stacks'
        )
        print("notification send to the email")


def calculate_template_diff(cur_template, new_template):
    t_diff = difflib.unified_diff(
        cur_template.splitlines(True),
        new_template.splitlines(True),
    )
    return ''.join(t_diff)


def collect_parameters(template, change_set, stack):
    tpl_params = template.get('Parameters') or []
    old = dict((x['ParameterKey'], x['ParameterValue'])
               for x in stack.get('Parameters') or [])
    new = dict((x['ParameterKey'], x['ParameterValue'])
               for x in change_set.get('Parameters') or [])
    params = []
    for name in tpl_params:
        param = {
            'Name': name,
            'Default': tpl_params[name].get('Default'),
            'CurrentValue': old.get(name),
            'NewValue': new.get(name),
        }
        params.append(param)

    return params


def calculate_diff(change_set_ids, job_id, account_id):
    print("calculating diff")
    try:
        name = pipeline.get_job_details(
            jobId=job_id)['jobDetails']['data']['pipelineContext']['pipelineName']
    except:
        name = 'unkown'
    print(name)
    # catch InvalidJobStateException if job has already been processed
    print(change_set_ids)
    stacks = []
    for change_set_id in change_set_ids:
        change_set = cfn.describe_change_set(
            ChangeSetName=change_set_id)
        stack_name = change_set['StackName']

        stack = cfn.describe_stacks(StackName=stack_name)['Stacks'][0]

        new_template_info = cfn.get_template(ChangeSetName=change_set_id)
        new_template = yaml.dump(yaml.load(json.dumps(
            new_template_info['TemplateBody'], sort_keys=True, default=str)), default_flow_style=False)

        status = stack['StackStatus']
        if status == 'REVIEW_IN_PROGRESS':
            param_diff = {}
            tem_diff = None
            cur_template = ''
        else:
            cur_template_info = cfn.get_template(StackName=stack_name)
            cur_template = yaml.dump(yaml.load(json.dumps(
                cur_template_info['TemplateBody'], sort_keys=True, default=str)), default_flow_style=False)

        stacks.append({
            'StackName': stack_name,
            'Parameters': collect_parameters(new_template_info['TemplateBody'], change_set, stack),
            'TemplateDiff': calculate_template_diff(cur_template, new_template),
            'Changes': change_set['Changes'],
            'OldTemplate': cur_template
        })

    credentials = aws_session(job_id)
    return {
        'Pipeline': {
            'Region': AWS_REGION, 'JobId': job_id, 'PipelineName': name, 'AccountId': account_id},
        'Credentials': {'AccessKeyId': credentials['AccessKeyId'],
                        'SecretAccessKey': credentials['SecretAccessKey'],
                        'SessionToken': credentials['SessionToken']},
        'Stacks': stacks,
    }

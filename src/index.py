import boto3
import os
import json
import yaml
import difflib
import json
from botocore.client import Config

cfn = boto3.client('cloudformation')
s3client = boto3.client('s3')
pipeline = boto3.client('codepipeline')
sns = boto3.client('sns')

TOPIC = os.getenv('TOPIC')
ROLE_ARN = os.environ['ROLE_ARN']
AWS_REGION = os.environ['AWS_DEFAULT_REGION']
WEB_URL = os.environ['WEB_URL']
BUCKET = os.environ['BUCKET']


class Parameters:
    def __init__(self, stack_name):
        self.stack = stack_name
        self.params = {}
        self.defaults = {}

    def read_parameters(self, parameter_values):
        param_values = []
        param_keys = []
        for value in parameter_values:
            param_values.append(value.values()[0])
            param_keys.append(value.values()[1])

        for key in param_keys:
            if key in self.params:
                continue
            else:
                self.params[key] = {'OldValue': None,
                                    'NewValue': None,
                                    'DefaultValue': None,
                                    }

        key_value = dict(zip(param_keys, param_values))

        return key_value

    def old_values(self, old_values):
        for key, value in old_values.items():
            self.params[key]['OldValue'] = value

    def new_values(self, new_values):
        for key, value in new_values.items():
            self.params[key]['NewValue'] = value

    def default_values(self, default_values):
        for key in default_values:
            if 'DefaultValue' in key:
                self.defaults[key['ParameterKey']] = key['DefaultValue']
            else:
                self.defaults[key['ParameterKey']] = None

        for key, value in self.defaults.items():
            self.params[key]['DefaultValue'] = value

        return self.params


def lambda_handler(event, context):
    print(event)
    job = event['CodePipeline.job']
    job_id = event['CodePipeline.job']['id']
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
    signature = url.split('?')[-1]
    signed_url = f'{WEB_URL}#/{BUCKET}/approvals/{job_id}.json?{signature}'
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


def calculate_parameter_diff(stack_name, describe_change_set, describe_stack):
    c_p = describe_stack['Stacks'][0]['Parameters']
    p = Parameters(stack_name)
    cur_dict = p.read_parameters(c_p)
    p.old_values(cur_dict)
    n_p = describe_change_set['Parameters']
    new_dict = p.read_parameters(n_p)
    p.new_values(new_dict)
    summary = cfn.get_template_summary(StackName=stack_name)['Parameters']
    all_parameters = p.default_values(summary)
    return all_parameters


def calculate_diff(change_set_ids, job_id, account_id):
    print("calculating diff")
    try:
        name = pipeline.get_job_details(
            jobId=job_id)['jobDetails']['data']['pipelineContext']['pipelineName']
    except:
        name = 'unkown'
    print(name)
    # catch InvalidJobStateException if job has already been processed
    credentials = aws_session(job_id)

    response = {
        'Pipeline': {
            'Region': AWS_REGION, 'JobId': job_id, 'PipelineName': name, 'AccountId': account_id},
        'Credentials': {'AccessKeyId': credentials['AccessKeyId'],
                        'SecretAccessKey': credentials['SecretAccessKey'],
                        'SessionToken': credentials['SessionToken']},
        'Changes': []
    }
    print(change_set_ids)
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
        else:
            cur_template_info = cfn.get_template(StackName=stack_name)
            cur_template = yaml.dump(yaml.load(json.dumps(
                cur_template_info['TemplateBody'], sort_keys=True, default=str)), default_flow_style=False)


        response['Changes'].append({
            'StackName': stack_name,
            'ParameterDiff': calculate_parameter_diff(stack_name, change_set, stack),
            'TemplateDiff': calculate_template_diff(cur_template, new_template),
            'ChangeSets': change_set['Changes'],
            'OldTemplate': cur_template
        })
    return response

#!/bin/env python3

import boto3
import subprocess
import os
import json


BRANCH = os.environ.get('TRAVIS_BRANCH')
COMMIT = os.environ.get('TRAVIS_COMMIT')
TAG = os.environ.get('TRAVIS_TAG')


def create_buckets():
    ec2 = boto3.client('ec2')
    s3 = boto3.client('s3')
    regions = map(lambda x: x['RegionName'], ec2.describe_regions()['Regions'])
    buckets = []

    for region in regions:
        bucket_name = f'pipeline-approval-{region}'
        buckets.append(bucket_name)
        bucket_location = None if region == 'us-east-1' else 'EU' if region == 'eu-west-1' else region
        regional_s3 = boto3.client('s3', region_name=region)
        try:
            bucket = s3.head_bucket(Bucket=bucket_name)
        except Exception as ex:
            print(f'bucket in region {region} does not exist, creating one...')
            if(bucket_location == None):
                regional_s3.create_bucket(Bucket=bucket_name,ACL='private')
            else:
              regional_s3.create_bucket(
                  Bucket=bucket_name,
                  ACL='private',
                  CreateBucketConfiguration={
                      'LocationConstraint': bucket_location,
                  }
              )

            regional_s3.put_bucket_policy(
              Bucket=bucket_name,
              ConfirmRemoveSelfBucketAccess=True,
              Policy=json.dumps({
                'Version': '2012-10-17',
                'Statement': [{
                  'Effect': 'Allow',
                  'Action': 's3:GetObject',
                  'Resource': f'arn:aws:s3:::{bucket_name}/*',
                  'Principal': '*',
                }]
              })
            )

            regional_s3.put_bucket_lifecycle_configuration(
                Bucket=bucket_name,
                LifecycleConfiguration={
                    'Rules': [{
                        'Expiration': { 'Days': 14 },
                        'Prefix': 'commits/',
                        'Status': 'Enabled',
                    }]
                }

            )

    return buckets

s3 = boto3.client('s3')

print('loading buckets')
buckets = create_buckets()

def update_cfn(key):
        with open('cfn/lambda.template.yml', 'r') as file:
            content = file.read()
            content = content.replace('{{S3Key}}', key)
            return content


if TAG:
    for bucket in buckets:
        s3.copy_object(
            CopySource={
                'Bucket': bucket,
                'Key': f'commits/{COMMIT}.zip',
            },
            Bucket=bucket,
            Key=f'release/{TAG}/lambda.zip',
        )
        content = update_cfn(f'release/{TAG}/lambda.zip')
        s3.put_object(
            Bucket=bucket,
            Key=f'release/{TAG}/lambda.template.yml',
            Body=content,
        )

elif BRANCH and COMMIT:
    subprocess.run(['zip', '-qr', 'lambda.zip', '.'], cwd='src/')

    with open('src/lambda.zip', 'rb') as file:
        data = file.read()
        for bucket in buckets:
            s3.put_object(
                Bucket=bucket,
                Key=f'{BRANCH}/lambda.zip',
                Body=data,
            )
            s3.put_object(
                Bucket=bucket,
                Key=f'commits/{COMMIT}.zip',
                Body=data,
            )
            content = update_cfn(f'commits/{COMMIT}.zip')
            s3.put_object(
                Bucket=bucket,
                Key=f'{BRANCH}/lambda.template.yml',
                Body=content,
            )

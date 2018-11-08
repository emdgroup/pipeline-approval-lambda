#!/bin/env python3

import boto3
import subprocess
import os


BRANCH = os.environ['TRAVIS_BRANCH']


def create_buckets():
    ec2 = boto3.client('ec2')
    s3 = boto3.client('s3')
    regions = map(lambda x: x['RegionName'], ec2.describe_regions()['Regions'])
    buckets = []

    for region in regions:
        bucket_name = f'pipeline-changes-{region}'
        buckets.append(bucket_name)
        bucket_location = None if region == 'us-east-1' else 'EU' if region == 'eu-west-1' else region
        regional_s3 = boto3.client('s3', region_name=region)
        try:
            bucket = s3.head_bucket(Bucket=bucket_name)
        except Exception as ex:
            print(f'bucket in region {region} does not exist, creating one...')
            if(bucket_location == None):
                regional_s3.create_bucket(
                    Bucket=bucket_name,
                    ACL='public-read',
                )
            else:
              regional_s3.create_bucket(
                  Bucket=bucket_name,
                  ACL='public-read',
                  CreateBucketConfiguration={
                      'LocationConstraint': bucket_location,
                  }
              )

    return buckets


subprocess.run(['zip', '-qr', 'lambda.zip', '.'], cwd='src/')

print('loading buckets')
buckets = create_buckets()


s3 = boto3.client('s3')

with open('src/lambda.zip', 'rb') as file:
    data = file.read()
    for bucket in buckets:
        print(s3.put_object(
            Bucket=bucket,
            Key=f'{BRANCH}/lambda.zip',
            Body=data,
        ))

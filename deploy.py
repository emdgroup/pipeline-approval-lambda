#!/bin/env python3

import boto3
import subprocess
import os

s3 = boto3.client('s3')

BRANCH = os.environ('TRAVIS_BRANCH')


def create_buckets():
    ec2 = boto3.client('ec2')
    regions = map(lambda x: x['RegionName'], ec2.describe_regions()['Regions'])
    buckets = []

    for region in regions:
        bucket_name = f'pipeline-changes-{region}'
        buckets.append(bucket_name)
        bucket_location = None if region == 'us-east-1' else 'EU' if region == 'eu-west-1' else region
        try:
            s3.head_bucket(Bucket=bucket_name)
        except:
            print(f'bucket in region {region} does not exist, creating one...')
            regional_s3 = s3 = boto3.client('s3', region_name=region)
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

buckets = create_buckets()

with open('src/lambda.zip') as file:
    data = file.read()
    for bucket in buckets:
        s3.put_object(
            Bucket=bucket,
            Key='master/lambda.zip',
            Body=data,
        )

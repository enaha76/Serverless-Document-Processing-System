import json
import os
import traceback
import boto3
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_fixed
import time
import urllib3

# AWS Clients
s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
ssm = boto3.client('ssm')

# Feature Flags
ENABLE_OPENROUTER = os.environ.get('ENABLE_OPENROUTER', 'true').lower() == 'true'
ENABLE_DYNAMODB = os.environ.get('ENABLE_DYNAMODB', 'true').lower() == 'true'
ENABLE_SNS = os.environ.get('ENABLE_SNS', 'true').lower() == 'true'

def get_api_key():
    try:
        response = ssm.get_parameter(Name=os.environ['OPENROUTER_API_KEY_PARAM'], WithDecryption=True)
        return response['Parameter']['Value']
    except Exception as e:
        print(f"❌ Failed to retrieve OpenRouter API key: {str(e)}")
        raise

def get_openrouter_client():
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=get_api_key()
    )

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def call_openrouter(text):
    client = get_openrouter_client()
    return client.chat.completions.create(
        model="mistralai/devstral-small-2505:free",
        messages=[{
            "role": "user",
            "content": f"Résume ce texte de manière concise en français :\n\n{text[:1000]}"
        }]
    )

# Custom CloudFormation response function using urllib3
def send_cfn_response(event, context, status, response_data=None):
    if response_data is None:
        response_data = {}
    
    response_body = {
        'Status': status,
        'Reason': f'See CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': event.get('PhysicalResourceId', context.log_stream_name),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': response_data
    }
    
    try:
        response_body_json = json.dumps(response_body)
        
        http = urllib3.PoolManager()
        response = http.request(
            'PUT',
            event['ResponseURL'],
            body=response_body_json.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Content-Length': str(len(response_body_json))
            }
        )
        print(f"✅ CloudFormation response sent. Status: {response.status}")
        return True
    except Exception as e:
        print(f"❌ Failed to send CloudFormation response: {str(e)}")
        traceback.print_exc()
        return False

def apply_bucket_policy(event, context):
    try:
        print(f"🔧 Custom resource event: {event['RequestType']}")
        
        if event['RequestType'] in ['Create', 'Update']:
            bucket_name = event['ResourceProperties']['BucketName']
            policy_document_dict = event['ResourceProperties']['PolicyDocument']
            policy_document_str = json.dumps(policy_document_dict)

            s3.put_bucket_policy(
                Bucket=bucket_name,
                Policy=policy_document_str
            )
            print(f"✅ Applied bucket policy to {bucket_name}")
            
            send_cfn_response(event, context, 'SUCCESS', {
                'Message': f'Bucket policy applied successfully to {bucket_name}'
            })
            
        elif event['RequestType'] == 'Delete':
            bucket_name = event['ResourceProperties']['BucketName']
            try:
                s3.delete_bucket_policy(Bucket=bucket_name)
                print(f"✅ Removed bucket policy from {bucket_name}")
            except Exception as e:
                print(f"⚠️ Could not remove bucket policy: {str(e)}")
            
            send_cfn_response(event, context, 'SUCCESS', {
                'Message': f'Bucket policy removal completed for {bucket_name}'
            })
            
    except Exception as e:
        print(f"❌ Error in bucket policy operation: {str(e)}")
        traceback.print_exc()
        send_cfn_response(event, context, 'FAILED', {
            'Error': str(e)
        })

def lambda_handler(event, context):
    start_time = time.time()
    try:
        print(f"📥 Received event: {json.dumps(event, default=str)}")
        
        # Check if this is a custom resource event for bucket policy
        if event.get('RequestType') and event.get('ResourceProperties') and 'BucketName' in event['ResourceProperties']:
            return apply_bucket_policy(event, context)

        # Validate event structure for S3 events
        if not event.get('Records') or not event['Records'][0].get('s3'):
            raise ValueError("Invalid S3 event structure")

        # 1. Retrieve file from S3
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = event['Records'][0]['s3']['object']['key']
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            text = obj['Body'].read().decode('utf-8')
            print(f"📄 Loaded {key} ({len(text)} chars)")
        except Exception as e:
            print(f"❌ S3 error: {str(e)}")
            raise

        # 2. Generate summary with OpenRouter
        summary = "Résumé non généré."
        if ENABLE_OPENROUTER:
            try:
                resp = call_openrouter(text)
                if resp.choices and len(resp.choices) > 0 and resp.choices[0].message.content:
                    summary = resp.choices[0].message.content
                    print(f"📝 Summary received: {summary[:200]}")
                else:
                    print(f"⚠️ Empty or unexpected response from OpenRouter: {resp}")
            except Exception as e:
                print(f"❌ OpenRouter error: {str(e)}")
                traceback.print_exc()

        # 3. Store in DynamoDB
        if ENABLE_DYNAMODB:
            try:
                table = dynamodb.Table(os.environ['TABLE_NAME'])
                item = {'NomFichier': key, 'Bucket': bucket, 'Texte': text, 'Résumé': summary}
                item_size = len(json.dumps(item).encode('utf-8'))
                if item_size > 400000:
                    raise ValueError("Item size exceeds DynamoDB 400 KB limit")
                table.put_item(Item=item)
                print("✅ Saved to DynamoDB")
            except Exception as e:
                print(f"❌ DynamoDB error: {str(e)}")
                traceback.print_exc()

        # 4. Send SNS notification
        if ENABLE_SNS:
            try:
                notification_subject = f"Nouveau fichier dans S3 : {key}"
                notification_message = (
                    f"Un fichier nommé '{key}' a été déposé dans le bucket '{bucket}'.\n\n"
                    f"Résumé du contenu :\n\n{summary[:1000]}"
                )
                sns.publish(
                    TopicArn=os.environ['TOPIC_ARN'],
                    Subject=notification_subject,
                    Message=notification_message
                )
                print("📧 SNS notification sent")
            except Exception as e:
                print(f"❌ SNS error: {str(e)}")
                traceback.print_exc()

        print(f"⏱️ Processing took {time.time() - start_time:.2f} seconds")
        return {
            'statusCode': 200,
            'body': json.dumps('Success')
        }

    except Exception as e:
        print(f"💥 General error: {str(e)}")
        traceback.print_exc()
        return {
            'statusCode': 500,
            'body': json.dumps('Lambda error')
        }

# DbMoto XML to GlueSync YAML Converter

A serverless AWS Lambda function that converts DbMoto XML metadata exports into GlueSync YAML configuration files.

## 🚀 Quick Deploy

```bash
# Navigate to the converter directory
cd dbmoto-converter

# Deploy to AWS (requires AWS CLI configured)
./deploy.sh
```

## 📁 Project Structure

```
dbmoto-converter/
├── lambda_function.py          # AWS Lambda handler
├── cloudformation-template.yaml # Infrastructure as Code
├── deploy.sh                   # Deployment script
├── api_example.py             # Python client example
├── requirements.txt           # Lambda dependencies
└── README.md                  # This file
```

## 🔧 Prerequisites

### AWS Setup
1. **AWS CLI configured** with appropriate permissions
2. **IAM permissions** for:
   - CloudFormation stack operations
   - Lambda function creation/update
   - S3 bucket creation
   - IAM role creation

### Required AWS Permissions
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cloudformation:*",
                "lambda:*",
                "s3:*",
                "iam:*"
            ],
            "Resource": "*"
        }
    ]
}
```

## 📖 Usage

### API Endpoint
Once deployed, you'll get an API endpoint like:
```
https://abc123.execute-api.us-east-1.amazonaws.com/prod/convert
```

### Convert XML File
```bash
curl -X POST 'https://your-endpoint.execute-api.region.amazonaws.com/prod/convert' \
  -H 'Content-Type: multipart/form-data' \
  -F 'xml_file=@your-metadata.xml' \
  -F 'include_targets=true'
```

### Python Example
```python
import requests
import zipfile
import io

response = requests.post(
    'https://your-endpoint.execute-api.region.amazonaws.com/prod/convert',
    files={'xml_file': open('metadata.xml', 'rb')},
    data={'include_targets': 'true'}
)

result = response.json()
print(f"Generated {result['stats']['yaml_files_generated']} YAML files")

# Download and extract the zip file
zip_response = requests.get(result['stats']['zip_file_url'])
zip_file = zipfile.ZipFile(io.BytesIO(zip_response.content))

# Extract all files
zip_file.extractall('conversion_outputs')
print("Files extracted to: conversion_outputs/")
```

## ⚙️ Configuration Options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `xml_file` | File | Required | DbMoto XML metadata file |
| `template_file` | File | Optional | Custom YAML template |
| `include_targets` | Boolean | `true` | Process target connections |
| `force_schemas` | String | Optional | Override schema mappings |

## 🏗️ Architecture

```
API Gateway → Lambda Function → S3 Bucket
                    ↓
               /tmp directory for processing
```

## 📊 Response Format

```json
{
  "status": "success",
  "message": "Successfully processed 3 YAML files",
  "request_id": "12345678-1234-1234-1234-123456789012",
  "results": {
    "zip_file": {
      "name": "conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
      "url": "https://s3-url/conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
      "size": 15360
    }
  },
  "stats": {
    "yaml_files_generated": 3,
    "tables_processed": 15,
    "zip_file_url": "https://s3-url/conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
    "zip_file_size": 15360
  }
}
```

### Download and Extract Results

The API returns a single zip file containing all generated YAML files and the conversion report:

```bash
# Download the zip file
curl -o conversion_outputs.zip "$ZIP_FILE_URL"

# Extract the contents
unzip conversion_outputs.zip

# Contents will include:
# - *.yaml (GlueSync configuration files)
# - conversion_report.txt (processing summary)
```

## 🔧 Manual Deployment

If you prefer manual deployment:

```bash
# 1. Package the function
pip install -r requirements.txt -t lambda-package/
cp ../parse_dbmoto_metadata_xml.py lambda-package/
cp ../table-list-template-basic.yaml lambda-package/
cd lambda-package && zip -r ../lambda-package.zip .

# 2. Deploy CloudFormation
aws cloudformation create-stack \
  --stack-name dbmoto-xml-converter \
  --template-body file://cloudformation-template.yaml \
  --capabilities CAPABILITY_IAM
```

## 🧹 Cleanup

Remove the deployment:
```bash
aws cloudformation delete-stack --stack-name dbmoto-xml-converter
```

## 🔒 Security

- Results stored in private S3 bucket
- Presigned URLs valid for 1 hour
- API Gateway provides basic rate limiting
- No authentication by default (add as needed)

## 💰 Cost Estimation

- **Lambda**: ~$0.0000002 per request + $0.00001667 per GB-second
- **API Gateway**: ~$3.50 per million requests
- **S3**: ~$0.023 per GB stored + $0.0004 per 1,000 requests
- **Example**: 100 conversions/month ≈ $0.50

## 🚀 CI/CD

The Lambda function is automatically deployed via GitLab CI when:
- Files in `dbmoto-converter/` are modified
- `parse_dbmoto_metadata_xml.py` changes
- Template files are updated

### Required GitLab CI/CD Variables
```
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
```

### Manual Deployment
You can also deploy manually by running the pipeline job from the GitLab web interface.

## 🐛 Troubleshooting

### Common Issues

1. **"Access Denied" error**
   - Check AWS credentials and permissions
   - Ensure CloudFormation capabilities are enabled

2. **"XML parsing failed"**
   - Verify XML file is a valid DbMoto export
   - Check file size (max 10MB)

3. **"Template not found"**
   - Ensure `table-list-template-basic.yaml` exists in parent directory

### Logs
Check CloudWatch logs for Lambda function errors:
```bash
aws logs tail /aws/lambda/dbmoto-xml-converter --follow
```

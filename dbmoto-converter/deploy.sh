#!/bin/bash

# DbMoto XML to GlueSync YAML Converter - AWS Deployment Script
# This script packages and deploys the Lambda function to AWS

set -e

echo "🚀 Starting DbMoto XML Converter deployment..."

# Configuration
STACK_NAME="dbmoto-xml-converter"
REGION=${AWS_DEFAULT_REGION:-us-east-1}
RESULTS_BUCKET="gluesync-conversion-results-${RANDOM}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "📦 Creating Lambda deployment package..."

# Create temporary directory for packaging
TEMP_DIR=$(mktemp -d)
echo "Working in: $TEMP_DIR"

# Copy source files from parent directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"

cp "$PARENT_DIR/parse_dbmoto_metadata_xml.py" "$TEMP_DIR/"
cp "$PARENT_DIR/lambda_function.py" "$TEMP_DIR/"  # This should be in the same dir, but let's be explicit

# Copy template file (if it exists)
if [ -f "$PARENT_DIR/table-list-template-basic.yaml" ]; then
    cp "$PARENT_DIR/table-list-template-basic.yaml" "$TEMP_DIR/"
fi

# Install dependencies to temp directory
echo "📚 Installing Python dependencies..."
pip install -r requirements.txt -t "$TEMP_DIR/" --quiet

# Create ZIP package
echo "📁 Creating deployment package..."
cd "$TEMP_DIR"
zip -r ../lambda-package.zip . --quiet
cd -

echo "☁️  Deploying to AWS..."

# Check if stack exists
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "🔄 Updating existing stack..."
    OPERATION="update-stack"
else
    echo "🆕 Creating new stack..."
    OPERATION="create-stack"
fi

# Deploy CloudFormation stack
aws cloudformation "$OPERATION" \
    --stack-name "$STACK_NAME" \
    --template-body file://cloudformation-template.yaml \
    --parameters ParameterKey=ResultsBucketName,ParameterValue="$RESULTS_BUCKET" \
    --capabilities CAPABILITY_IAM \
    --region "$REGION"

echo "⏳ Waiting for stack creation/update to complete..."
aws cloudformation wait stack-"${OPERATION//-stack/}"-complete --stack-name "$STACK_NAME" --region "$REGION"

# Get outputs
echo "📋 Getting deployment information..."
API_ENDPOINT=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query 'Stacks[0].Outputs[?OutputKey==`ApiEndpoint`].OutputValue' --output text)
RESULTS_BUCKET_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" --query 'Stacks[0].Outputs[?OutputKey==`ResultsBucketName`].OutputValue' --output text)

echo ""
echo "✅ Deployment completed successfully!"
echo ""
echo "🌐 API Endpoint: $API_ENDPOINT"
echo "🪣  Results Bucket: $RESULTS_BUCKET_NAME"
echo ""
echo "📖 Usage Examples:"
echo ""
echo "1. Using curl:"
echo "curl -X POST '$API_ENDPOINT/convert' \\"
echo "  -H 'Content-Type: multipart/form-data' \\"
echo "  -F 'xml_file=@your-metadata.xml' \\"
echo "  -F 'include_targets=true'"
echo ""
echo "2. Using Python requests:"
echo "import requests"
echo "response = requests.post('$API_ENDPOINT/convert',"
echo "    files={'xml_file': open('your-metadata.xml', 'rb')},"
echo "    data={'include_targets': 'true'})"
echo "print(response.json())"
echo ""
echo "🧹 Cleaning up temporary files..."
rm -rf "$TEMP_DIR"
rm lambda-package.zip

echo ""
echo "🎉 Ready to convert DbMoto XML files to GlueSync YAML!"
echo "📄 Check the generated conversion_report.txt for detailed processing information."

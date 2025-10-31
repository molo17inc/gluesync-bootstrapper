import json
import boto3
import os
import tempfile
import base64
import gzip
from urllib.parse import unquote
from parse_dbmoto_metadata_xml import parse_xml, export_as_yaml, write_conversion_report

s3_client = boto3.client('s3')

def lambda_handler(event, context):
    """
    AWS Lambda handler for DbMoto XML to Gluesync YAML conversion API

    Expected input: API Gateway event with multipart/form-data containing:
    - xml_file: XML metadata file
    - template_file: Optional YAML template file
    - include_targets: Optional boolean (default True)
    - force_schemas: Optional string for schema mappings
    """

    try:
        # Parse the multipart form data from API Gateway
        # Handle both lowercase and capitalized header names
        headers = event.get('headers', {})
        content_type = headers.get('content-type') or headers.get('Content-Type', '')
        
        if not content_type.startswith('multipart/form-data'):
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Content-Type must be multipart/form-data'})
            }

        # Extract files and parameters from the multipart data
        boundary = content_type.split('boundary=')[1]
        
        # Handle both base64 encoded and plain body
        if event.get('isBase64Encoded'):
            body = base64.b64decode(event['body'])
        else:
            # Convert string to bytes if needed
            body_data = event['body']
            body = body_data.encode('utf-8') if isinstance(body_data, str) else body_data

        files, params = parse_multipart_data(body, boundary)
        print(f"DEBUG: files keys: {list(files.keys())}")
        print(f"DEBUG: params keys: {list(params.keys())}")

        # Extract required files
        if 'xml_file' not in files:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'xml_file is required'})
            }

        xml_content = files['xml_file']
        print(f"DEBUG: xml_content type: {type(xml_content)}, length: {len(xml_content) if xml_content else 'None'}")
        
        if xml_content is None:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'XML file content is empty or malformed'})
            }
        
        template_content = files.get('template_file')
        
        # Decompress if gzipped (auto-detect or explicit parameter)
        if xml_content[:2] == b'\x1f\x8b':  # gzip magic number
            xml_content = gzip.decompress(xml_content)

        # Extract parameters
        include_targets = params.get('include_targets', 'true').lower() == 'true'
        force_schemas = params.get('force_schemas')

        # Create temporary directory for processing
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save XML file
            xml_path = os.path.join(temp_dir, 'input.xml')
            with open(xml_path, 'wb') as f:
                f.write(xml_content)

            # Save template file if provided
            template_path = None
            if template_content:
                template_path = os.path.join(temp_dir, 'template.yaml')
                with open(template_path, 'wb') as f:
                    f.write(template_content)

            # Create output directory
            output_dir = os.path.join(temp_dir, 'output')
            os.makedirs(output_dir, exist_ok=True)

            # Set environment variables for the script
            os.environ['XML_PATH'] = xml_path
            os.environ['OUTPUT_DIR'] = output_dir
            if template_path:
                os.environ['TEMPLATE_PATH'] = template_path
            os.environ['INCLUDE_TARGETS'] = str(include_targets)
            if force_schemas:
                os.environ['FORCE_SCHEMAS'] = force_schemas

            # Process the conversion
            connections, groups, chains, replications, source_to_target_schemas = parse_xml()
            exported_count = export_as_yaml(connections, groups, chains, replications, source_to_target_schemas)

            # Generate conversion report
            report_path = write_conversion_report()

            # Create a zip file containing all outputs
            zip_path = create_output_zip(output_dir, temp_dir)

            # Upload results to S3
            bucket_name = os.environ.get('RESULTS_BUCKET', 'gluesync-conversion-results')
            request_id = context.aws_request_id

            result_urls = upload_zip_to_s3(zip_path, bucket_name, request_id)

            return {
                'statusCode': 200,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'status': 'success',
                    'message': f'Successfully processed {exported_count} YAML files',
                    'request_id': request_id,
                    'results': result_urls,
                    'stats': {
                        'yaml_files_generated': exported_count,
                        'tables_processed': sum(len(schema.get('tables', {})) for conn in connections.values() for schema in conn.get('schemas', {}).values()),
                        'zip_file_url': result_urls['zip_file']['url'],
                        'zip_file_size': result_urls['zip_file']['size']
                    }
                })
            }

    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback
        traceback.print_exc()

        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'status': 'error',
                'message': str(e),
                'request_id': context.aws_request_id
            })
        }

def parse_multipart_data(body, boundary):
    """Parse multipart/form-data content"""
    files = {}
    params = {}

    # Simple multipart parser (for basic use cases)
    boundary_bytes = b'--' + boundary.encode()
    parts = body.split(boundary_bytes)

    for part in parts:
        part = part.strip()
        if not part or part == b'--':
            continue
            
        if b'Content-Disposition' in part:
            # Split part into headers and content
            header_end = part.find(b'\r\n\r\n')
            if header_end == -1:
                # Try with just \n\n for compatibility
                header_end = part.find(b'\n\n')
                if header_end == -1:
                    continue
                    
            headers = part[:header_end]
            content = part[header_end + 4:]  # Skip \r\n\r\n
            
            # Parse Content-Disposition header
            disposition = headers.decode('utf-8', errors='ignore')
            if 'filename=' in disposition:
                # File upload
                field_name = None
                filename = None
                
                # Extract name and filename
                for line in disposition.split('\n'):
                    line = line.strip()
                    if line.startswith('Content-Disposition'):
                        # Parse the disposition line
                        parts_disp = line.split(';')
                        for part_disp in parts_disp:
                            part_disp = part_disp.strip()
                            if part_disp.startswith('name="'):
                                field_name = part_disp[6:-1]  # Remove name="
                            elif part_disp.startswith('filename="'):
                                filename = part_disp[10:-1]  # Remove filename="
                
                if field_name:
                    files[field_name] = content
            else:
                # Regular parameter
                field_name = None
                
                for line in disposition.split('\n'):
                    line = line.strip()
                    if line.startswith('Content-Disposition'):
                        parts_disp = line.split(';')
                        for part_disp in parts_disp:
                            part_disp = part_disp.strip()
                            if part_disp.startswith('name="'):
                                field_name = part_disp[6:-1]  # Remove name="
                                break
                
                if field_name:
                    params[field_name] = content.decode('utf-8', errors='ignore').strip()

    return files, params

def create_output_zip(output_dir, temp_dir):
    """Create a zip file containing all output files"""
    import zipfile

    zip_path = os.path.join(temp_dir, 'conversion_outputs.zip')

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Add all YAML files
        for filename in os.listdir(output_dir):
            if filename.endswith('.yaml') or filename.endswith('.txt'):
                file_path = os.path.join(output_dir, filename)
                zip_file.write(file_path, filename)

    return zip_path

def upload_zip_to_s3(zip_path, bucket_name, request_id):
    """Upload the zip file to S3 and return presigned URL"""
    zip_filename = f"conversion_outputs_{request_id}.zip"
    s3_key = f"{request_id}/{zip_filename}"

    # Upload zip file to S3
    s3_client.upload_file(zip_path, bucket_name, s3_key)

    # Generate presigned URL (valid for 1 hour)
    url = s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket_name, 'Key': s3_key},
        ExpiresIn=3600
    )

    return {
        'zip_file': {
            'name': zip_filename,
            'url': url,
            'size': os.path.getsize(zip_path)
        }
    }

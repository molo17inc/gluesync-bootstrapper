#!/usr/bin/env python3
"""
Example script for using the DbMoto XML to GlueSync YAML Converter API
"""

import requests
import json
import sys

def convert_xml_to_yaml(api_endpoint, xml_file_path, template_file_path=None, include_targets=True, force_schemas=None):
    """
    Convert DbMoto XML file to GlueSync YAML using the AWS API

    Args:
        api_endpoint (str): The API Gateway endpoint URL
        xml_file_path (str): Path to the XML metadata file
        template_file_path (str): Optional path to YAML template file
        include_targets (bool): Whether to include target schemas
        force_schemas (str): Optional schema mapping overrides

    Returns:
        dict: API response containing result zip file URL and statistics
    """

    # Prepare files for upload
    files = {
        'xml_file': open(xml_file_path, 'rb')
    }

    if template_file_path:
        files['template_file'] = open(template_file_path, 'rb')

    # Prepare form data
    data = {
        'include_targets': str(include_targets).lower()
    }

    if force_schemas:
        data['force_schemas'] = force_schemas

    try:
        print(f" Uploading {xml_file_path} to conversion API...")
        print(f" Endpoint: {api_endpoint}")

        response = requests.post(f"{api_endpoint}/convert", files=files, data=data)

        if response.status_code == 200:
            result = response.json()
            print(" Conversion completed successfully!")
            print(f" Generated {result['stats']['yaml_files_generated']} YAML files")
            print(f" Zip file size: {result['stats']['zip_file_size']} bytes")

            # Display results
            if 'results' in result and 'zip_file' in result['results']:
                zip_info = result['results']['zip_file']
                print(f" Download zip file: {zip_info['name']}")
                print(f" URL: {zip_info['url']}")
                print(f" Size: {zip_info['size']} bytes")

                # Optionally download and extract
                download = input("Download and extract zip file now? (y/n): ").lower().strip()
                if download == 'y':
                    download_and_extract_zip(zip_info['url'], zip_info['name'])

            return result
        else:
            print(f" API Error ({response.status_code}): {response.text}")
            return None

    except Exception as e:
        print(f" Request failed: {str(e)}")
        return None

    finally:
        # Close files
        for f in files.values():
            f.close()

def download_and_extract_zip(zip_url, zip_filename):
    """Download and extract the zip file containing conversion results"""
    try:
        print(f" Downloading zip file...")
        response = requests.get(zip_url)

        if response.status_code == 200:
            import zipfile
            import io

            # Create zip file object from response content
            zip_file = zipfile.ZipFile(io.BytesIO(response.content))

            # Extract to current directory
            extract_dir = f"conversion_outputs_{zip_filename.replace('.zip', '')}"
            zip_file.extractall(extract_dir)

            print(f" Zip file extracted to: {extract_dir}/")
            print(" Contents:")
            for file_info in zip_file.filelist:
                print(f"  • {file_info.filename}")

            return extract_dir
        else:
            print(f" Failed to download zip file: {response.status_code}")
            return None

    except Exception as e:
        print(f" Failed to download/extract zip file: {str(e)}")
        return None

def main():
    if len(sys.argv) < 3:
        print("Usage: python api_example.py <api_endpoint> <xml_file> [template_file] [options]")
        print("")
        print("Arguments:")
        print("  api_endpoint    API Gateway endpoint URL")
        print("  xml_file        Path to DbMoto XML metadata file")
        print("  template_file   Optional YAML template file")
        print("")
        print("Options:")
        print("  --no-targets    Don't include target schemas")
        print("  --force-schemas SOURCE:TARGET,SOURCE2:TARGET2")
        print("")
        print("Example:")
        print("  python api_example.py https://abc123.execute-api.us-east-1.amazonaws.com/prod metadata.xml template.yaml")
        sys.exit(1)

    api_endpoint = sys.argv[1]
    xml_file = sys.argv[2]
    template_file = sys.argv[3] if len(sys.argv) > 3 else None

    # Parse additional options
    include_targets = True
    force_schemas = None

    for arg in sys.argv[4:]:
        if arg == '--no-targets':
            include_targets = False
        elif arg.startswith('--force-schemas='):
            force_schemas = arg.split('=', 1)[1]

    # Perform conversion
    result = convert_xml_to_yaml(
        api_endpoint=api_endpoint,
        xml_file_path=xml_file,
        template_file_path=template_file,
        include_targets=include_targets,
        force_schemas=force_schemas
    )

    if result and 'request_id' in result:
        print(f"\n📋 Request ID: {result['request_id']}")
        print("💡 Result URLs are valid for 1 hour from generation time")

if __name__ == "__main__":
    main()

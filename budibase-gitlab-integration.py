import requests
import json
import sys
from collections import defaultdict

def fetch_budibase_data(url, headers, data):
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    return response.json()

def trigger_gitlab_pipeline(url, token, ref, variables):
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    data = {
        'token': token,
        'ref': ref,
        **{f'variables[{k}]': v for k, v in variables.items()}
    }
    response = requests.post(url, headers=headers, data=data)
    response.raise_for_status()
    return response.json()

def get_latest_bootstrapper_version(rows):
    """Get the latest bootstrapper version from all available rows."""
    versions = [row.get('gluesyncBootstrapperVersion') for row in rows if row.get('gluesyncBootstrapperVersion')]
    return max(versions, default=None)

def get_unique_directories(rows):
    """Get unique directory entries with their latest configuration."""
    directory_map = {}
    
    for row in rows:
        directory = row.get('directory')
        if not directory:
            continue
            
        # If directory already exists, update only if current row is more recent
        if directory not in directory_map or row.get('_id', '') > directory_map[directory].get('_id', ''):
            directory_map[directory] = row
            
    return directory_map.values()

def main(version):
    # Budibase API details
    budibase_url = 'https://backoffice.molo17.com/api/public/v1/tables/datasource_plus_3dbc0976d090472f82658f287ec8ad6d__GluesyncIntegrationTests/rows/search'
    budibase_headers = {
        'x-budibase-app-id': 'app_dev_d411145ec77d45188bdeb932e443c5d2',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'x-budibase-api-key': 'a15cecc8a9c1b15bbaea24a84835f00a-e8395572facbfbe126b674e9f8d48b378271ba00dc3429b194a912255f1f5d1fd2cf59524ac2ceae'
    }
    budibase_data = {
        "query": {"allOr": "true"},
        "paginate": "false",
        "limit": 1000
    }

    # GitLab API details
    gitlab_url = 'https://gitlab.com/api/v4/projects/54760187/trigger/pipeline'
    gitlab_token = 'glptt-1fd62a7b277fcf1a324a40e54adcd2815161fa7e'
    gitlab_ref = 'main'

    try:
        # Fetch data from Budibase
        budibase_response = fetch_budibase_data(budibase_url, budibase_headers, budibase_data)
        rows = budibase_response.get('data', [])

        # First, try to find exact matches
        exact_matches = [
            row for row in rows 
            if row.get('gluesyncSourceVersion') == version 
            and row.get('gluesyncTargetVersion') == version 
            and row.get('gluesyncCoreHubVersion') == version
        ]

        if exact_matches:
            print(f"Found {len(exact_matches)} exact matches for version {version}")
            process_rows = exact_matches
        else:
            print(f"No exact matches found for version {version}. Processing unique directories with provided version...")
            process_rows = get_unique_directories(rows)

        # Get the latest bootstrapper version from all rows
        latest_bootstrapper = get_latest_bootstrapper_version(rows)
        if not latest_bootstrapper:
            raise ValueError("No bootstrapper version found in any row")

        # Process rows
        for row in process_rows:
            # Prepare variables for GitLab API call
            variables = {
                'DIRECTORY': row.get('directory', ''),
                'GSSOURCEVERSION': version if not exact_matches else row.get('gluesyncSourceVersion', ''),
                'GSTARGETVERSION': version if not exact_matches else row.get('gluesyncTargetVersion', ''),
                'GSCOREVERSION': version if not exact_matches else row.get('gluesyncCoreHubVersion', ''),
                'GSBOOTSTRAPPERVERSION': latest_bootstrapper,
                'CI_PIPELINE_DESCRIPTION': row.get('nickname', ''),
                'INTEGRATION_TEST_ID': row.get('_id', '')
            }

            # Skip if no directory is specified
            if not variables['DIRECTORY']:
                print(f"Skipping row {row.get('_id', 'unknown')}: No directory specified")
                continue

            # Trigger GitLab pipeline
            try:
                gitlab_response = trigger_gitlab_pipeline(gitlab_url, gitlab_token, gitlab_ref, variables)
                print(f"Pipeline triggered for directory {variables['DIRECTORY']}:")
                print(f"  Test ID: {variables['INTEGRATION_TEST_ID']}")
                print(f"  Source Version: {variables['GSSOURCEVERSION']}")
                print(f"  Target Version: {variables['GSTARGETVERSION']}")
                print(f"  Core Version: {variables['GSCOREVERSION']}")
                print(f"  Bootstrapper Version: {variables['GSBOOTSTRAPPERVERSION']}")
                print(f"  Pipeline Response: {gitlab_response}")
                print("-" * 80)
            except requests.exceptions.RequestException as e:
                print(f"Error triggering pipeline for directory {variables['DIRECTORY']}: {str(e)}")

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from Budibase: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script_name.py <version_to_search>")
        sys.exit(1)
    
    version_to_search = sys.argv[1]
    main(version_to_search)
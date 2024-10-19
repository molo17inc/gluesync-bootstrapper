import requests
import json
import sys

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

    # Fetch data from Budibase
    budibase_response = fetch_budibase_data(budibase_url, budibase_headers, budibase_data)

    # Process each row
    for row in budibase_response.get('data', []):
        if row.get('gluesyncSourceVersion') == version and row.get('gluesyncTargetVersion') == version and row.get('gluesyncCoreHubVersion') == version:
            # Prepare variables for GitLab API call
            variables = {
                'DIRECTORY': row.get('directory', ''),
                'GSSOURCEVERSION': row.get('gluesyncSourceVersion', ''),
                'GSTARGETVERSION': row.get('gluesyncTargetVersion', ''),
                'GSCOREVERSION': row.get('gluesyncCoreHubVersion', ''),
                'GSBOOTSTRAPPERVERSION': row.get('gluesyncBoostrapperVersion', ''),
                'CI_PIPELINE_DESCRIPTION': row.get('nickname', ''),
                'INTEGRATION_TEST_ID': row.get('_id', '')
            }

            # Trigger GitLab pipeline
            try:
                gitlab_response = trigger_gitlab_pipeline(gitlab_url, gitlab_token, gitlab_ref, variables)
                print(f"Pipeline triggered for row {row['_id']}: {gitlab_response}")
            except requests.exceptions.RequestException as e:
                print(f"Error triggering pipeline for row {row['_id']}: {str(e)}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script_name.py <version_to_search>")
        sys.exit(1)
    
    version_to_search = sys.argv[1]
    main(version_to_search)

import json
import os
from pathlib import Path

import requests

def _required_env(name):
    """Return the value of a required environment variable, exiting when it is missing."""
    value = os.getenv(name)
    if not value:
        print(f"Error: {name} environment variable is required")
        exit(1)
    return value


def _rename_module_type(items):
    """Replace each item's 'type' with its 'moduleType' (a 'type' without 'moduleType' is dropped)."""
    for item in items:
        if 'type' in item:
            del item['type']
        if 'moduleType' in item:
            item['type'] = item['moduleType']
            del item['moduleType']


def _process_agents(data):
    """Rename 'moduleType' to 'type' in the agent rows, whatever the response layout."""
    if 'data' in data:
        print("Found 'data' key")
        # Check if 'data' is a list or has 'rows'
        if isinstance(data['data'], list):
            # Direct list of items
            _rename_module_type(data['data'])
            print("Processed list items in data")
        elif isinstance(data['data'], dict) and 'rows' in data['data']:
            # Nested structure with rows
            _rename_module_type(data['data']['rows'])
            print("Processed rows in data")
    elif isinstance(data, list):
        # Try to process the data directly if it's a list
        _rename_module_type(data)
        print("Processed list items directly")


def fetch_and_save_data():
    # Get environment variables (required ones are validated)
    api_key = _required_env('BUDIBASE_API_KEY')
    app_id = _required_env('BUDIBASE_APP_ID')
    base_url = os.getenv('BUDIBASE_BASE_URL', 'https://backoffice.molo17.com/api/public/v1')

    # API endpoint
    url = f"{base_url}/tables/datasource_plus_3dbc0976d090472f82658f287ec8ad6d__AvailableAgents/rows/search"

    # Headers
    headers = {
        'x-budibase-app-id': app_id,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'x-budibase-api-key': api_key
    }

    # Request data
    data = json.dumps({
        "query": {
            "allOr": "true"
        },
        "paginate": "true",
        "limit": 1000
    })

    # Send POST request
    response = requests.post(url, headers=headers, data=data)

    # Check if the request was successful
    if response.status_code == 200:
        # Parse response
        data = response.json()

        # Debug: Print the structure of the data
        print("Data structure keys:", list(data.keys()))

        # Process the data based on the actual structure
        _process_agents(data)

        # Save to agents.json in the project root
        output_path = Path(__file__).resolve().parent.parent / "agents.json"
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=4)
        print(f"Data saved to {output_path} with 'moduleType' renamed to 'type'")
    else:
        print("Failed to fetch data: Status code", response.status_code)
        exit(1)

# Run the function
if __name__ == '__main__':
    fetch_and_save_data()

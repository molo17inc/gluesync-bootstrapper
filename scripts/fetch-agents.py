import json
import os
from pathlib import Path

import requests

def fetch_and_save_data():
    # Get environment variables
    api_key = os.getenv('BUDIBASE_API_KEY')
    app_id = os.getenv('BUDIBASE_APP_ID')
    base_url = os.getenv('BUDIBASE_BASE_URL', 'https://backoffice.molo17.com/api/public/v1')
    
    # Validate required environment variables
    if not api_key:
        print("Error: BUDIBASE_API_KEY environment variable is required")
        exit(1)
    if not app_id:
        print("Error: BUDIBASE_APP_ID environment variable is required")
        exit(1)
    
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
        if 'data' in data:
            print("Found 'data' key")
            # Check if 'data' is a list or has 'rows'
            if isinstance(data['data'], list):
                # Direct list of items
                for item in data['data']:
                    if 'type' in item:
                        del item['type']
                    if 'moduleType' in item:
                        item['type'] = item['moduleType']
                        del item['moduleType']
                print("Processed list items in data")
            elif isinstance(data['data'], dict) and 'rows' in data['data']:
                # Nested structure with rows
                for row in data['data']['rows']:
                    if 'type' in row:
                        del row['type']
                    if 'moduleType' in row:
                        row['type'] = row['moduleType']
                        del row['moduleType']
                print("Processed rows in data")
        else:
            # Try to process the data directly if it's a list
            if isinstance(data, list):
                for item in data:
                    if 'type' in item:
                        del item['type']
                    if 'moduleType' in item:
                        item['type'] = item['moduleType']
                        del item['moduleType']
                print("Processed list items directly")
        
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

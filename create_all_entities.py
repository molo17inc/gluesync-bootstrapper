# Copyright (c) 2024 MOLO17
# Author: Daniele Angeli
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import json
import requests
import argparse
import time
import uuid
from urllib.parse import urlencode, quote
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'http://localhost:1717')
DEFAULT_USER = os.getenv('DEFAULT_USER', 'admin')
DEFAULT_PASSWORD = os.getenv('DEFAULT_PASSWORD', 'admin')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]

def fetch_core_hub(path, method='GET', token=None, body=None, params=None):
    url = f"{CORE_HUB_URL}{path}"
    headers = {
        'Authorization': f'Bearer {token}' if token else None,
        'Content-Type': 'application/json'
    }

    print(f"Sending request to: {url}")
    print(f"Method: {method}")
    print(f"Headers: {headers}")
    print(f"Body: {body}")
    print(f"Params: {params}")

    response = requests.request(method, url, headers=headers, json=body, params=params)

    print(f"Response status code: {response.status_code}")
    print(f"Response content: {response.text}")

    if response.status_code < 200 or response.status_code >= 300:
        print(f"Request to {url} failed with status code {response.status_code}: {response.text}")
        return None

    try:
        return response.json()
    except json.JSONDecodeError:
        return response.text

def authenticate():
    auth_response = fetch_core_hub(
        '/authentication/login',
        method='POST',
        body={'username': DEFAULT_USER, 'password': DEFAULT_PASSWORD}
    )
    token = auth_response.get('apiToken')
    if not token:
        raise Exception('Failed to authenticate')
    return token

def get_pipeline_config(token, pipeline_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)

def get_pipeline_agents(token, pipeline_id):
    config = get_pipeline_config(token, pipeline_id)
    return config.get('agents', {})

def get_agent_tables(token, pipeline_id, agent_id, schema_name):
    params = {'schema': schema_name}
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/tables", token=token, params=params)

def get_table_columns(token, pipeline_id, agent_id, schema_name, table_name):
    params = {'tableschema': schema_name, 'tablename': table_name}
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/columns", token=token, params=params)

def create_entities(token, pipeline_id, schema, tables, source_agent_id, target_agent_id, target_type):
    entities = []
    
    for table in tables:
        table_name = table["name"]

        # Skip tables that start with "sys"
        if table_name.startswith("sys"):
            print(f"Skipping table: {table_name} (starts with 'sys')")
            continue

        columns = get_table_columns(token, pipeline_id, source_agent_id, schema, table_name)
        
        entity = {
            "entityName": f"{schema}.{table_name}",
            "agentEntities": [
                {
                    "type": "SingleTable",
                    "entityType": {
                        "type": "Source",
                        "maxItemsCountPerIteration": 1000,
                        "maxMigrationItemsCountPerIteration": 1000,
                        "pollingIntervalMilliseconds": 100
                    },
                    "agentId": source_agent_id,
                    "table": {
                        "name": table_name,
                        "schema": schema
                    },
                    "columns": [
                        {
                            "name": col["name"],
                            "alias": col["name"],
                            "type": col["type"]
                        } for col in columns["columns"]
                    ],
                    "keys": [
                        {
                            "name": col["name"],
                            "alias": col["name"],
                            "type": col["type"]
                        } for col in columns["columns"] if col["isPrimaryKey"]
                    ]
                },
                {
                    "type": "NoSqlEntity" if target_type.lower() == "nosql" else "SingleTable",
                    "entityType": {
                        "type": "Target"
                    },
                    "agentId": target_agent_id,
                    "entityObject": {
                        "scope": schema,
                        "collection": table_name
                    },
                    "table": {
                        "schema": schema,
                        "name": table_name
                    },
                    "columns": [
                        {
                            "name": col["name"],
                            "type": col["type"]
                        } for col in columns["columns"]
                    ],
                    "keys": [
                        {
                            "name": col["name"],
                            "type": col["type"]
                        } for col in columns["columns"] if col["isPrimaryKey"]
                    ],
                    "sourceAgent": source_agent_id,
                    "sourceTable": {
                        "schema": schema,
                        "name": table_name
                    }
                }
            ]
        }
        entities.append(entity)
    
    entity_data = {"entities": entities}
    return fetch_core_hub(f"/pipelines/{pipeline_id}/config/entities", method="PUT", token=token, body=entity_data)

def main(pipeline_id, schema_name, target_type):
    token = authenticate()
    
    # Get pipeline agents
    agents = get_pipeline_agents(token, pipeline_id)
    
    # Find source and target agents
    source_agent = next(({"id": agent_id, **agent_info} for agent_id, agent_info in agents.items() if agent_info['agentType'] == 'SOURCE'), None)
    target_agent = next(({"id": agent_id, **agent_info} for agent_id, agent_info in agents.items() if agent_info['agentType'] == 'TARGET'), None)
    
    if not source_agent or not target_agent:
        raise Exception("Could not find both source and target agents in the pipeline configuration")
    
    # Get tables for the given schema
    tables = get_agent_tables(token, pipeline_id, source_agent['id'], schema_name)
    
    # Create all entities in a single call
    response = create_entities(token, pipeline_id, schema_name, tables["tables"], source_agent['id'], target_agent['id'], target_type)
    
    if response:
        print(f"Entities created successfully for schema: {schema_name}")
    else:
        print(f"Failed to create entities for schema: {schema_name}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlueSync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--schema', required=True, help="Schema name")
    parser.add_argument('--target-type', required=True, choices=['SQL', 'NoSQL'], help="Target type (SQL or NoSQL)")
    args = parser.parse_args()

    main(args.pipeline, args.schema, args.target_type)
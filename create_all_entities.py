import os
import json
import requests
import argparse
import time
from urllib.parse import urlencode, quote

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'http://localhost:1717')
DEFAULT_USER = os.getenv('DEFAULT_USER', 'admin')
DEFAULT_PASSWORD = os.getenv('DEFAULT_PASSWORD', 'admin')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))

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

def create_source_entity(token, pipeline_id, agent_id, schema, table, columns):
    entity_data = {
        "entities": [
            {
                "type": "SingleTable",
                "entityName": f"{schema}.{table}",
                "entityType": {
                    "type": "Source",
                    "maxItemsCountPerIteration": 1000,
                    "maxMigrationItemsCountPerIteration": 1000,
                    "pollingIntervalMilliseconds": 100
                },
                "table": {
                    "name": table,
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
            }
        ],
        "customEntitiesProperties": {
            f"{schema}.{table}": {}
        },
        "customTableProperties": {
            f"{schema}.{table}": {}
        }
    }
    
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/config/entities", method="PUT", token=token, body=entity_data)

def create_target_entity(token, pipeline_id, agent_id, schema, table, columns, source_agent_id, target_type):
    entity_type = "NoSqlEntity" if target_type.lower() == "nosql" else "SqlEntity"
    
    entity_data = {
        "entities": [
            {
                "type": entity_type,
                "entityName": f"{schema}.{table}",
                "entityType": {
                    "type": "Target"
                },
                "entityObject": {
                    "scope": schema,
                    "collection": table
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
                    "name": table
                }
            }
        ],
        "customEntitiesProperties": {
            f"{schema}.{table}": {}
        },
        "customTableProperties": {
            f"{schema}.{table}": {}
        }
    }
    
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/config/entities", method="PUT", token=token, body=entity_data)

def create_entity_on_both_sides(token, pipeline_id, source_agent_id, target_agent_id, schema, table, columns, target_type):
    source_response = create_source_entity(token, pipeline_id, source_agent_id, schema, table, columns)
    print(f"Source entity creation response: {source_response}")
    
    if source_response is not None:
        target_response = create_target_entity(token, pipeline_id, target_agent_id, schema, table, columns, source_agent_id, target_type)
        print(f"Target entity creation response: {target_response}")
        return source_response, target_response
    else:
        return None, None

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
    
    for table in tables["tables"]:
        table_name = table["name"]
        print(f"Processing table: {table_name}")
        
        try:
            # Get columns for the table
            columns = get_table_columns(token, pipeline_id, source_agent['id'], schema_name, table_name)
            
            # Create entity for the table on both sides
            source_response, target_response = create_entity_on_both_sides(token, pipeline_id, source_agent['id'], target_agent['id'], schema_name, table_name, columns, target_type)
            
            if source_response and target_response:
                print(f"Entity created for table: {table_name} on both source and target")
            else:
                print(f"Failed to create entity for table: {table_name}")
        except Exception as e:
            print(f"Error processing table {table_name}: {str(e)}")
        
        print(f"Waiting for {ENTITY_START_TIMEOUT} seconds before processing the next entity...")
        time.sleep(ENTITY_START_TIMEOUT)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlueSync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--schema', required=True, help="Schema name")
    parser.add_argument('--target-type', required=True, choices=['SQL', 'NoSQL'], help="Target type (SQL or NoSQL)")
    args = parser.parse_args()

    main(args.pipeline, args.schema, args.target_type)

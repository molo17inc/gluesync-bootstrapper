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
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
DEFAULT_USER = os.getenv('DEFAULT_USER', 'admin')
DEFAULT_PASSWORD = os.getenv('DEFAULT_PASSWORD', 'admin')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))

class CustomHttpAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.ssl_context = create_urllib3_context(
            cert_reqs=ssl.CERT_NONE,
            ssl_version=ssl.PROTOCOL_TLS
        )
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

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

    session = requests.Session()
    adapter = CustomHttpAdapter()
    session.mount('https://', adapter)

    response = session.request(method, url, headers=headers, json=body, params=params, verify=False)

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

def get_node_info(token, pipeline_id, agent_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info", token=token)

def map_data_type(source_type, source_node_info, target_node_info):
    source_matrix = source_node_info['dataTypesMatrix']
    target_matrix = target_node_info['dataTypesMatrix']
    
    # Normalize the source type (remove any size specifiers, e.g., varchar(255) -> varchar)
    normalized_source_type = source_type.split('(')[0].lower()

    print(f"Mapping source type: {source_type} (normalized: {normalized_source_type})")

    # Special handling for some types
    if normalized_source_type == 'mediumblob':
        normalized_source_type = 'blob'
    elif normalized_source_type == 'year':
        normalized_source_type = 'int'  # Usually 'year' is stored as an integer

    # Find the matching GlueSync data type for the source type
    source_item = next((item for item in source_matrix if normalized_source_type in [t.lower() for t in item['supportedTypes']]), None)
    
    if not source_item:
        print(f"Warning: No mapping found for source type {source_type}. Using as is.")
        return source_type

    source_gluesync_type = source_item['gluesyncDataType']
    print(f"Matched GlueSync data type: {source_gluesync_type}")

    # Find the corresponding target type
    target_item = next((item for item in target_matrix if item['gluesyncDataType'] == source_gluesync_type), None)
    
    if target_item:
        # Check if there's a direct match in supported types
        normalized_target_types = [t.lower() for t in target_item['supportedTypes']]
        if normalized_source_type in normalized_target_types:
            print(f"Direct match found: {source_type}")
            return source_type  # Use the original source type if it's supported in the target
        
        # Special handling for specific types
        if normalized_source_type == 'geometry':
            print(f"Mapping geometry type: {source_type} -> geometry")
            return 'geometry'
        elif normalized_source_type in ['enum', 'set']:
            print(f"Mapping {normalized_source_type} to varchar")
            return 'varchar'
        elif normalized_source_type == 'json':
            mapped_type = 'json' if 'json' in normalized_target_types else 'varchar'
            print(f"Mapping json to {mapped_type}")
            return mapped_type
        elif normalized_source_type == 'bit':
            mapped_type = 'boolean' if 'boolean' in normalized_target_types else 'smallint'
            print(f"Mapping bit to {mapped_type}")
            return mapped_type
        elif normalized_source_type in ['tinyint', 'smallint', 'mediumint']:
            print(f"Mapping {normalized_source_type} to int")
            return 'int'
        
        # If no direct match, use the default type for this GlueSync data type in the target
        print(f"Mapping {source_type} to {target_item['defaultType']} (no direct match in target)")
        return target_item['defaultType']
    
    print(f"Warning: No target mapping found for GlueSync type {source_gluesync_type}. Using source type {source_type} as is.")
    return source_type  # If no mapping found, return the original type

def load_yaml_config(file_path):
    try:
        with open(file_path, 'r') as file:
            return yaml.safe_load(file)
    except FileNotFoundError:
        print(f"YAML file not found at {file_path}. Proceeding without it.")
        return {}
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}. Proceeding without it.")
        return {}

def create_entities(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id, source_type, target_type, yaml_config):
    entities = []
    
    source_node_info = get_node_info(token, pipeline_id, source_agent_id)
    target_node_info = get_node_info(token, pipeline_id, target_agent_id)
    
    print("Source Node Info:")
    print(json.dumps(source_node_info, indent=2))
    print("Target Node Info:")
    print(json.dumps(target_node_info, indent=2))
    
    schema_config = yaml_config.get('schemas', {}).get(source_schema, {})
    yaml_target_schema = schema_config.get('target', target_schema)
    whitelist = schema_config.get('tables', {}).get('whitelist', [])
    blacklist = schema_config.get('tables', {}).get('blacklist', [])
    custom_tables = schema_config.get('tables', {}).get('custom', {})

    print(f"Whitelist: {whitelist}")
    print(f"Blacklist: {blacklist}")
    print(f"Custom tables: {custom_tables}")
    
    for table in tables:
        table_name = table.get("name")
        if not table_name:
            print(f"Warning: Table without name encountered. Skipping.")
            continue

        # Check if table should be skipped
        if (table_name.startswith("sys") or 
            (blacklist and table_name in blacklist) or 
            (whitelist and table_name not in whitelist)):
            print(f"Skipping table: {table_name}")
            continue

        columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)

        custom_config = custom_tables.get(table_name, {})
        print(f"Custom config for {table_name}: {custom_config}")

        if custom_config and 'keys' in custom_config:
            keys = []
            for key_name in custom_config['keys']:
                key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)
                if key_column:
                    keys.append({
                        "name": key_column["name"],
                        "alias": key_column["name"],
                        "type": key_column["type"]
                    })
                else:
                    print(f"Warning: Key {key_name} not found in columns for table {table_name}. Adding with unknown type.")
                    keys.append({
                        "name": key_name,
                        "alias": key_name,
                        "type": "unknown"
                    })
            print(f"Using custom keys for {table_name}: {keys}")
        else:
            keys = [
                {
                    "name": col["name"],
                    "alias": col["name"],
                    "type": col["type"]
                } for col in columns["columns"] if col.get("isPrimaryKey")
            ]
            print(f"Using primary keys for {table_name}: {keys}")

        if not keys:
            print(f"Warning: No keys specified for {table_name}. Table will have no keys.")

        entity = {
            "entityName": f"{source_schema}.{table_name}",
            "agentEntities": [
                {
                    "type": "NoSqlEntity" if source_type.lower() == "nosql" else "SingleTable",
                    "entityType": {
                        "type": "Source",
                        "maxItemsCountPerIteration": 1000,
                        "maxMigrationItemsCountPerIteration": 1000,
                        "pollingIntervalMilliseconds": 100
                    },
                    "agentId": source_agent_id,
                    "entityObject": {
                        "scope": source_schema,
                        "collection": table_name
                    },
                    "table": {
                        "name": table_name,
                        "schema": source_schema
                    },
                    "columns": [
                        {
                            "name": col["name"],
                            "alias": col["name"],
                            "type": col["type"]
                        } for col in columns["columns"]
                    ],
                    "keys": keys
                },
                {
                    "type": "NoSqlEntity" if target_type.lower() == "nosql" else "SingleTable",
                    "entityType": {
                        "type": "Target"
                    },
                    "agentId": target_agent_id,
                    "entityObject": {
                        "scope": yaml_target_schema,
                        "collection": table_name
                    },
                    "table": {
                        "schema": yaml_target_schema,
                        "name": table_name
                    },
                    "columns": [
                        {
                            "name": col["name"],
                            "type": map_data_type(col["type"], source_node_info, target_node_info)
                        } for col in columns["columns"]
                    ],
                    "keys": [
                        {
                            "name": key["name"],
                            "type": map_data_type(key["type"], source_node_info, target_node_info)
                        } for key in keys
                    ],
                    "sourceAgent": source_agent_id,
                    "sourceTable": {
                        "schema": source_schema,
                        "name": table_name
                    }
                }
            ]
        }
        entities.append(entity)
    
    entity_data = {"entities": entities}
    return fetch_core_hub(f"/pipelines/{pipeline_id}/config/entities", method="PUT", token=token, body=entity_data)

def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file):
    token = authenticate()
    
    # Load YAML configuration
    yaml_config = load_yaml_config(yaml_file) if yaml_file else {}
    
    # Get pipeline agents
    agents = get_pipeline_agents(token, pipeline_id)
    
    # Find source and target agents
    source_agent = next(({"id": agent_id, **agent_info} for agent_id, agent_info in agents.items() if agent_info['agentType'] == 'SOURCE'), None)
    target_agent = next(({"id": agent_id, **agent_info} for agent_id, agent_info in agents.items() if agent_info['agentType'] == 'TARGET'), None)
    
    if not source_agent or not target_agent:
        raise Exception("Could not find both source and target agents in the pipeline configuration")
    
    # Get tables for the given source schema
    tables = get_agent_tables(token, pipeline_id, source_agent['id'], source_schema)
    
    # Create all entities in a single call
    response = create_entities(token, pipeline_id, source_schema, target_schema, tables["tables"], source_agent['id'], target_agent['id'], source_type, target_type, yaml_config)
    
    if response:
        print(f"Entities created successfully for source schema: {source_schema} and target schema: {target_schema}")
    else:
        print(f"Failed to create entities for source schema: {source_schema} and target schema: {target_schema}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlueSync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
    parser.add_argument('--target-schema', required=True, help="Target schema name")    
    parser.add_argument('--source-type', required=True, choices=['SQL', 'NoSQL'], help="Source type (SQL or NoSQL)")
    parser.add_argument('--target-type', required=True, choices=['SQL', 'NoSQL'], help="Target type (SQL or NoSQL)")    
    parser.add_argument('--yaml-file', help="Path to the YAML configuration file")    
    args = parser.parse_args()    
    main(args.pipeline, args.source_schema, args.target_schema, args.source_type, args.target_type, args.yaml_file)
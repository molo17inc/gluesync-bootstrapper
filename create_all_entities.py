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
import time
import uuid
import urllib.parse
from urllib.parse import urlencode, quote
import urllib3
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import yaml
import argparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))

class ProtocolAwareAdapter(HTTPAdapter):
    """HTTP adapter that handles both HTTP and HTTPS protocols."""
    def __init__(self, *args, **kwargs):
        self.ssl_context = create_urllib3_context(
            cert_reqs=ssl.CERT_NONE,
            ssl_version=ssl.PROTOCOL_TLS
        )
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.is_secure_protocol:
            kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        if self.is_secure_protocol:
            kwargs['ssl_context'] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

    @property
    def is_secure_protocol(self):
        return hasattr(self, '_is_secure') and self._is_secure

    def set_protocol(self, is_secure):
        self._is_secure = is_secure

class CoreHubClient:
    """Client for handling CoreHub API requests with protocol awareness."""
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.adapter = ProtocolAwareAdapter()
        
        # Parse URL to determine protocol
        parsed_url = urllib.parse.urlparse(base_url)
        is_secure = parsed_url.scheme == 'https'
        
        # Configure adapter based on protocol
        self.adapter.set_protocol(is_secure)
        
        # Mount adapter for both HTTP and HTTPS
        self.session.mount('http://', self.adapter)
        self.session.mount('https://', self.adapter)

    def request(self, path, method='GET', token=None, body=None, params=None):
        url = f"{self.base_url}{path}"
        headers = {
            'Authorization': f'Bearer {token}' if token else None,
            'Content-Type': 'application/json'
        }
        headers = {k: v for k, v in headers.items() if v is not None}

        print(f"Loading: {url} with: {body}")
        
        response = self.session.request(
            method, 
            url, 
            headers=headers, 
            json=body, 
            params=params,
            verify=False if self.adapter.is_secure_protocol else None
        )
        
        if response.status_code < 200 or response.status_code >= 300:
            print(f"Request to {url} failed with status code {response.status_code}: {response.text}")
            raise Exception(f"Request to {url} failed with status code {response.status_code}: {response.text}")
        
        if response.status_code == 202 and not response.content:
            return {}

        try:
            return response.json()
        except json.JSONDecodeError:
            print(f"Non-JSON response from {url}: {response.text}")
            return response.text

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

def fetch_core_hub(path, method='GET', token=None, body=None, params=None):
    return core_hub_client.request(path, method, token, body, params)

def get_pipeline_config(token, pipeline_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)

def get_pipeline_agents(token, pipeline_id):
    config = get_pipeline_config(token, pipeline_id)
    return config.get('agents', {})

def get_agent_tables(token, pipeline_id, agent_id, schema_name):
    """Get list of tables from an agent for a specific schema."""
    params = {'schema': schema_name}
    response = fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/tables",
        token=token,
        params=params
    )
    
    if isinstance(response, dict) and 'tables' in response:
        return response['tables']
    elif isinstance(response, list):
        return response
    else:
        print(f"Unexpected response format from get_agent_tables: {response}")
        return []

def get_table_columns(token, pipeline_id, agent_id, schema_name, table_name):
    params = {'tableschema': schema_name, 'tablename': table_name}
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/columns", token=token, params=params)

def get_node_info(token, pipeline_id, agent_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info", token=token)

def map_data_type(source_type, source_node_info, target_node_info):
    source_matrix = source_node_info['dataTypesMatrix']
    target_matrix = target_node_info['dataTypesMatrix']
    
    normalized_source_type = source_type.split('(')[0].lower()
    print(f"Mapping source type: {source_type} (normalized: {normalized_source_type})")

    if normalized_source_type == 'mediumblob':
        normalized_source_type = 'blob'
    elif normalized_source_type == 'year':
        normalized_source_type = 'int'

    # Find matching source type in matrix (case-insensitive)
    source_item = next(
        (item for item in source_matrix 
         if any(t.lower() == normalized_source_type for t in item['supportedTypes'])),
        None
    )
    
    if not source_item:
        print(f"Warning: No mapping found for source type {source_type}. Using as is.")
        return source_type

    source_gluesync_type = source_item['gluesyncDataType']
    print(f"Matched GlueSync data type: {source_gluesync_type}")

    # Find matching target type
    target_item = next(
        (item for item in target_matrix 
         if item['gluesyncDataType'] == source_gluesync_type),
        None
    )
    
    if target_item:
        # Case-insensitive search but return server's exact value if found
        supported_types_map = {t.lower(): t for t in target_item['supportedTypes']}
        
        if normalized_source_type in supported_types_map:
            server_type = supported_types_map[normalized_source_type]
            print(f"Direct match found: {server_type}")
            return server_type
        
        # Special case mappings using server's exact values
        if normalized_source_type == 'geometry':
            for t in target_item['supportedTypes']:
                if t.lower() == 'geometry':
                    print(f"Mapping geometry type: {source_type} -> {t}")
                    return t
        elif normalized_source_type in ['enum', 'set']:
            # Find first STRING type in target's supported types
            for t in target_item['supportedTypes']:
                if 'string' in t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type == 'json':
            # Try to find JSON type first, fall back to STRING
            for t in target_item['supportedTypes']:
                if 'json' in t.lower():
                    print(f"Mapping json to {t}")
                    return t
            for t in target_item['supportedTypes']:
                if 'string' in t.lower():
                    print(f"Mapping json to {t} (fallback)")
                    return t
        elif normalized_source_type == 'bit':
            # Try to find BOOLEAN type first, fall back to INT
            for t in target_item['supportedTypes']:
                if 'boolean' in t.lower():
                    print(f"Mapping bit to {t}")
                    return t
            for t in target_item['supportedTypes']:
                if 'int' in t.lower():
                    print(f"Mapping bit to {t} (fallback)")
                    return t
        elif normalized_source_type in ['tinyint', 'smallint', 'mediumint']:
            # Find appropriate INT type
            for t in target_item['supportedTypes']:
                if 'int' in t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        
        print(f"Mapping {source_type} to {target_item['defaultType']} (using target's default type)")
        return target_item['defaultType']
    
    print(f"Warning: No target mapping found for GlueSync type {source_gluesync_type}. Using source type {source_type} as is.")
    return source_type

def load_yaml_config(file_path):
    try:
        with open(file_path, 'r') as file:
            config = yaml.safe_load(file)
            print(f"Loaded YAML config: {json.dumps(config, indent=2)}")
            return config
    except FileNotFoundError:
        print(f"YAML file not found at {file_path}. Proceeding without it.")
        return {}
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}. Proceeding without it.")
        return {}

def process_filter_clauses(filter_config, columns_info):
    """
    Process filter clauses from YAML configuration into the required format
    """
    if not filter_config or 'clauses' not in filter_config:
        return None

    processed_clauses = []
    for clause in filter_config['clauses']:
        # Skip invalid clauses
        if 'column' not in clause or 'operation' not in clause:
            print(f"Warning: Skipping invalid filter clause: {clause}")
            continue

        column_name = clause['column']
        operation_type = clause['operation']

        # Create the basic filter clause
        filter_clause = {
            "column": {
                "name": column_name,
                "type": clause.get('type', 'string')
            },
            "operation": {
                "type": operation_type
            }
        }

        # Handle value based on operation type
        if operation_type not in ['IsNull', 'IsNotNull']:
            if 'value' not in clause:
                print(f"Warning: Missing value for operation {operation_type} on column {column_name}")
                continue
                
            if operation_type == 'Regex':
                filter_clause["operation"]["filterValue"] = clause['value']
            elif clause['type'] == 'int':
                filter_clause["operation"]["filterValue"] = int(clause['value'])
            elif clause['type'] == 'float':
                filter_clause["operation"]["filterValue"] = float(clause['value'])
            else:
                filter_clause["operation"]["filterValue"] = str(clause['value'])

        print(f"Generated filter clause: {json.dumps(filter_clause, indent=2)}")
        processed_clauses.append(filter_clause)

    if processed_clauses:
        return {"clauses": processed_clauses}
    return None

def process_filter_clauses(filter_config, columns_info):
    """
    Process filter clauses from YAML configuration into the required format
    All filter values are converted to strings as required by the backend
    """
    if not filter_config or 'clauses' not in filter_config:
        return None

    processed_clauses = []
    for clause in filter_config['clauses']:
        # Skip invalid clauses
        if 'column' not in clause or 'operation' not in clause:
            print(f"Warning: Skipping invalid filter clause: {clause}")
            continue

        column_name = clause['column']
        operation_type = clause['operation']

        # Create the basic filter clause
        filter_clause = {
            "column": {
                "name": column_name,
                "type": clause.get('type', 'string')
            },
            "operation": {
                "type": operation_type
            }
        }

        # Handle value based on operation type
        if operation_type not in ['IsNull', 'IsNotNull']:
            if 'value' not in clause:
                print(f"Warning: Missing value for operation {operation_type} on column {column_name}")
                continue
                
            # Convert all values to strings
            if isinstance(clause['value'], (list, tuple)):
                # Handle arrays (for IN operations)
                filter_clause["operation"]["filterValue"] = [str(v) for v in clause['value']]
            else:
                # Handle single values
                filter_clause["operation"]["filterValue"] = str(clause['value'])

        print(f"Generated filter clause: {json.dumps(filter_clause, indent=2)}")
        processed_clauses.append(filter_clause)

    if processed_clauses:
        return {"clauses": processed_clauses}
    return None

def create_entities(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id, source_type, target_type, yaml_config, skip_errors=False, chunk_size=50):
    """Create entities for the pipeline."""
    if not yaml_config:
        yaml_config = {}
    
    entities = []
    
    # Get node info for data type mapping
    source_node_info = get_node_info(token, pipeline_id, source_agent_id)
    target_node_info = get_node_info(token, pipeline_id, target_agent_id)
    
    print("Source Node Info:")
    print(json.dumps(source_node_info, indent=2))
    print("Target Node Info:")
    print(json.dumps(target_node_info, indent=2))
    
    print(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")
    
    schema_config = yaml_config.get(source_schema, {})
    print(f"Schema config for {source_schema}: {json.dumps(schema_config, indent=2)}")
    
    yaml_target_schema = schema_config.get('target', target_schema)
    whitelist = schema_config.get('tables', {}).get('whitelist', [])
    blacklist = schema_config.get('tables', {}).get('blacklist', [])
    custom_tables = schema_config.get('tables', {}).get('custom', {})

    # Get schema-level custom properties
    schema_custom_properties = schema_config.get('customProperties', {})
    global_source_custom_properties = schema_custom_properties.get('source', {})
    global_target_custom_properties = schema_custom_properties.get('target', {})

    print(f"Target schema: {yaml_target_schema}")
    print(f"Whitelist: {whitelist}")
    print(f"Blacklist: {blacklist}")
    print(f"Custom tables: {custom_tables}")
    print(f"Global source custom properties: {global_source_custom_properties}")
    print(f"Global target custom properties: {global_target_custom_properties}")
    
    for table in tables:
        if isinstance(table, str):
            table_name = table
        else:
            table_name = table.get("name")
            
        if not table_name:
            print(f"Warning: Table without name encountered. Skipping.")
            continue

        if (table_name.startswith("sys") or 
            (blacklist and table_name in blacklist) or 
            (whitelist and table_name not in whitelist)):
            print(f"Skipping table: {table_name}")
            continue

        columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)

        custom_config = custom_tables.get(table_name, {})
        print(f"Custom config for {table_name}: {custom_config}")
        
        # Get table-specific custom properties and merge with global properties
        table_custom_properties = custom_config.get('customProperties', {})
        source_custom_properties = {**global_source_custom_properties, **table_custom_properties.get('source', {})}
        target_custom_properties = {**global_target_custom_properties, **table_custom_properties.get('target', {})}
        
        print(f"Source custom properties for {table_name}: {source_custom_properties}")
        print(f"Target custom properties for {table_name}: {target_custom_properties}")
        
        # Get custom target table name if specified
        target_table_name = custom_config.get('name', table_name)
        print(f"Using target table name: {target_table_name} for source table: {table_name}")

        # Get and process filter configuration
        filter_config = custom_config.get('filter')
        processed_filters = process_filter_clauses(filter_config, columns) if filter_config else None
        print(f"Processed filters for {table_name}: {processed_filters}")

        # Get document key configuration if it exists
        document_key = None
        if custom_config and 'documentKey' in custom_config:
            doc_key_config = custom_config['documentKey']
            document_key = {
                "prefix": doc_key_config.get('prefix', ''),
                "suffix": doc_key_config.get('suffix', ''),
                "separator": doc_key_config.get('separator', '-'),
                "keys": doc_key_config.get('keys', [])
            }
            print(f"Document key configuration for {table_name}: {document_key}")

        # Process keys and other configurations as before...
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

        # Create source and target table property keys
        source_table_key = f"{source_schema}.{table_name}"
        target_table_key = f"{yaml_target_schema}.{target_table_name}"

        source_entity = {
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
                    "alias": next(
                        (target_name 
                        for column_map in custom_config.get('columns', [])
                        for source_name, target_name in column_map.items()
                        if source_name == col["name"]
                        ),
                        col["name"]
                    ),
                    "type": col["type"]
                } for col in columns["columns"]
            ],
            "keys": keys,
            "customProperties": source_custom_properties,
            "tablesProperties": {source_table_key: {}}
        }

        target_entity = {
            "type": "NoSqlEntity" if target_type.lower() == "nosql" else "SingleTable",
            "entityType": {
                "type": "Target",
                **({"filter": processed_filters} if processed_filters else {})
            },
            "agentId": target_agent_id,
            "entityObject": {
                "scope": yaml_target_schema,
                "collection": target_table_name
            },
            "table": {
                "schema": yaml_target_schema,
                "name": target_table_name
            },
            "columns": [
                {
                    "name": next(
                        (target_name 
                        for column_map in custom_config.get('columns', [])
                        for source_name, target_name in column_map.items()
                        if source_name == col["name"]
                        ),
                        col["name"]
                    ),
                    "alias": next(
                        (target_name 
                        for column_map in custom_config.get('columns', [])
                        for source_name, target_name in column_map.items()
                        if source_name == col["name"]
                        ),
                        col["name"]
                    ),
                    "type": map_data_type(col["type"], source_node_info, target_node_info)
                } for col in columns["columns"]
            ],
            "keys": [
                {
                    "name": key["name"],
                    "type": map_data_type(key["type"], source_node_info, target_node_info)
                } for key in keys
            ],
            "customProperties": target_custom_properties,
            "tablesProperties": {target_table_key: {}},
            "sourceAgent": source_agent_id,
            "sourceTable": {
                "schema": source_schema,
                "name": table_name
            }
        }

        # Add document key mapping if configured
        if document_key:
            target_entity["keyMapping"] = document_key

        entity = {
            "entityName": f"{source_schema}.{table_name}",
            "agentEntities": [source_entity, target_entity]
        }
        entities.append(entity)
    
    # Process entities in chunks
    total_entities = len(entities)
    successful_entities = 0
    failed_entities = 0
    
    print(f"Processing {total_entities} entities in chunks of {chunk_size}...")
    
    for i in range(0, total_entities, chunk_size):
        chunk = entities[i:i + chunk_size]
        chunk_data = {"entities": chunk}
        chunk_start = i + 1
        chunk_end = min(i + chunk_size, total_entities)
        
        print(f"\nProcessing chunk {chunk_start}-{chunk_end} of {total_entities} entities...")
        
        try:
            response = fetch_core_hub(
                f"/pipelines/{pipeline_id}/config/entities", 
                method="PUT", 
                token=token, 
                body=chunk_data
            )
            print(f"Successfully created entities {chunk_start}-{chunk_end}")
            successful_entities += len(chunk)
        except Exception as e:
            error_msg = str(e)
            print(f"Error creating entities {chunk_start}-{chunk_end}: {error_msg}")
            failed_entities += len(chunk)
            if not skip_errors:
                raise
            print("Skipping chunk due to skip_errors=True")
    
    print(f"\nProcessing complete:")
    print(f"- Total entities: {total_entities}")
    print(f"- Successfully created: {successful_entities}")
    print(f"- Failed: {failed_entities}")
    
    return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}

def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=False, chunk_size=50):
    """
    Main function to create entities for a pipeline
    """
    try:
        yaml_config = load_yaml_config(yaml_file) if yaml_file else None
        
        # Get pipeline configuration
        pipeline_config = get_pipeline_config(token, pipeline_id)
        
        # Get agents information
        agents = get_pipeline_agents(token, pipeline_id)
        
        if not agents or len(agents) != 2:
            raise Exception(f"Expected 2 agents, found {len(agents) if agents else 0}")
        
        # Identify source and target agents based on type
        source_agent = next((agent for agent in agents if agent['agentType'] == 'SOURCE'), None)
        target_agent = next((agent for agent in agents if agent['agentType'] == 'TARGET'), None)
        
        if not source_agent or not target_agent:
            raise Exception(f"Could not find required agents. Source ({source_type}): {source_agent}, Target ({target_type}): {target_agent}")
        
        # Get tables from source agent
        tables = get_agent_tables(token, pipeline_id, source_agent['agentId'], source_schema)
        
        if not tables:
            raise Exception(f"No tables found in schema {source_schema}")
            
        # Create entities
        create_entities(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent['agentId'], target_agent['agentId'],
            source_type, target_type, yaml_config, skip_errors, chunk_size
        )
        
    except Exception as e:
        print(f"Error: {str(e)}")
        if not skip_errors:
            raise
        print("Skipping error due to skip_errors=True")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gluesync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
    parser.add_argument('--chunk-size', type=int, default=50, help="Number of entities to process in each chunk (default: 50)")
    parser.add_argument('--skip-errors', action='store_true', help="Continue execution even if errors occur")
    parser.add_argument('--target-schema', required=True, help="Target schema name")
    parser.add_argument('--source-type', required=True, help="Source agent type")
    parser.add_argument('--target-type', required=True, help="Target agent type")
    parser.add_argument('--yaml-file', help="YAML configuration file path")
    parser.add_argument('--token', required=True, help="Authentication token")
    
    args = parser.parse_args()
    
    main(
        args.pipeline,
        args.source_schema,
        args.target_schema,
        args.source_type,
        args.target_type,
        args.yaml_file,
        args.token,
        args.skip_errors,
        args.chunk_size
    )
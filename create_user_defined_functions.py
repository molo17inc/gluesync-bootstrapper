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
import base64
import os
import json
import sys
from enum import Enum

import requests
import urllib3
import argparse

from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete
from utils.core_hub_client import CoreHubClient
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, create_entity_schedules, create_pipeline_schedules, map_data_type, load_yaml_config
from pathlib import Path, PosixPath
from pydantic import BaseModel, ConfigDict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
CHRONOS_URL = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'
UDF_PATH = os.getenv('UDF_PATH',
                     '/Users/zorzf/Molo17/git/gluesync-demo-kits/gs-2/integration_test/mysql8-vertica-integration-test/udf')

UDF_CLASS_FILENAME = "UserDefinedFunctionTemplate"

# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)


class UdfFunctionType(str, Enum):
    java = 'java'
    python = 'python'
    ruby = 'ruby'
    javascript = 'javascript'

    def extension(self):
        match self:
            case UdfFunctionType.java:
                return ".java"
            case UdfFunctionType.ruby:
                return ".rb"
            case UdfFunctionType.python:
                return ".py"
            case UdfFunctionType.javascript:
                return ".js"
            case _:
                raise NotImplementedError("not implemented")


class UdfFunctionCompileRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    code: str
    type: UdfFunctionType
    entityName: str


def get_udf_function_for_table(table_name: str, udf: list[dict]) -> dict:
    return next((item for item in udf if item.get("name") == table_name), {})


def find_udf_definition_in_path(table_name: str, udf_type: UdfFunctionType) -> PosixPath:
    filename = f"{UDF_CLASS_FILENAME}For{table_name}{udf_type.extension()}"
    path_location = Path(UDF_PATH)
    file_path = next((p for p in path_location.rglob(filename)), None)
    return file_path


def read_file(filepath):
    try:
        with open(filepath, "r", encoding="UTF-8") as f:
            content = f.read()
    except Exception as e:
        logger.exception(e)
        raise
    else:
        return content


def compile_udf_function(pipeline_id: str, token: str, udf_compile_request: UdfFunctionCompileRequest) -> str:
    try:
        print(f"Compile mapping function request: {udf_compile_request.model_dump()}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/mapping-functions/compile-mapping-function",
            method="POST",
            token=token,
            body=udf_compile_request.model_dump()
        )
        return response
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        print(f"compile mapping function error: {error_msg}")
        raise


def check_and_compile_udf_function(table_name: str, udf_definition: dict, pipeline_id: str, token: str):
    udf_type = UdfFunctionType(udf_definition.get("type"))
    file_path = find_udf_definition_in_path(table_name, udf_type)
    if file_path:
        file_data = read_file(file_path)
        b64_file_data = base64.b64encode(file_data.encode())
        udf_compile_request = UdfFunctionCompileRequest(code=b64_file_data, type=udf_type, entityName=table_name)
        compile_udf_function(pipeline_id=pipeline_id, token=token, udf_compile_request=udf_compile_request)
    else:
        logger.warning(f"missing function file for: {udf_definition}, file_path: {file_path}")


def handle_udf_function_definition(table_name, pipeline_id, udf, token):
    udf_definition = get_udf_function_for_table(table_name, udf)
    if udf_definition:
        print(f"found table with udf functions: {udf_definition}")
        check_and_compile_udf_function(table_name, udf_definition, pipeline_id, token)


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


def create_user_defined_functions(token, pipeline_id, source_schema, target_schema, tables, source_agent_id,
                                  target_agent_id,
                                  source_type, target_type, yaml_config, skip_errors=False, chunk_size=50):
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
    udf = schema_config.get('tables', {}).get('udf', [])
    # Handle empty custom tables attribute - convert None to empty dict
    tables_config = schema_config.get('tables', {})
    custom_tables = tables_config.get('custom', {})
    if custom_tables is None:
        custom_tables = {}
        print("Warning: 'custom' attribute is present but empty in YAML. Converting to empty dict.")

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

        handle_udf_function_definition(table_name, pipeline_id, udf, token)

    #     continue
    #
    #     columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)
    #
    #     custom_config = custom_tables.get(table_name, {})
    #     if custom_config is None:
    #         custom_config = {}
    #         print(
    #             f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
    #     print(f"Custom config for {table_name}: {custom_config}")
    #
    #     # Get table-specific custom properties and merge with global properties
    #     table_custom_properties = custom_config.get('customProperties', {})
    #     source_custom_properties = {**global_source_custom_properties, **table_custom_properties.get('source', {})}
    #     target_custom_properties = {**global_target_custom_properties, **table_custom_properties.get('target', {})}
    #
    #     print(f"Source custom properties for {table_name}: {source_custom_properties}")
    #     print(f"Target custom properties for {table_name}: {target_custom_properties}")
    #
    #     # Get custom target table name if specified
    #     target_table_name = custom_config.get('name', table_name)
    #     print(f"Using target table name: {target_table_name} for source table: {table_name}")
    #
    #     # Get and process filter configuration
    #     filter_config = custom_config.get('filter')
    #     processed_filters = process_filter_clauses(filter_config, columns) if filter_config else None
    #     print(f"Processed filters for {table_name}: {processed_filters}")
    #
    #     # Get document key configuration if it exists
    #     document_key = None
    #     if custom_config and 'documentKey' in custom_config:
    #         doc_key_config = custom_config['documentKey']
    #         document_key = {
    #             "prefix": doc_key_config.get('prefix', ''),
    #             "suffix": doc_key_config.get('suffix', ''),
    #             "separator": doc_key_config.get('separator', '-'),
    #             "keys": doc_key_config.get('keys', [])
    #         }
    #         print(f"Document key configuration for {table_name}: {document_key}")
    #
    #     # Process keys and other configurations as before...
    #     if custom_config and 'keys' in custom_config:
    #         keys = []
    #         for key_def in custom_config['keys']:
    #             # Handle both string (key name) and dict (key with name/alias) formats
    #             if isinstance(key_def, dict):
    #                 # Handle the case where the key is specified as a dict with 'name' and optional 'alias'
    #                 key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
    #                 key_config = key_def.get(key_name, {}) if isinstance(key_def.get(key_name), dict) else {}
    #
    #                 # Get the key name (either from the dict key or from the 'name' field)
    #                 key_name = key_name or key_config.get('name')
    #                 # Get the alias (defaults to the key name if not specified)
    #                 key_alias = key_config.get('name', key_name)
    #
    #                 # Get the key type from the config or find it in the columns
    #                 key_type = key_config.get('type')
    #             else:
    #                 # Simple string format - use the string as both name and alias
    #                 key_name = key_def
    #                 key_alias = key_def
    #                 key_type = None
    #
    #             # Try to find the key in the columns to get its type if not specified
    #             key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)
    #
    #             if key_column:
    #                 keys.append({
    #                     "name": key_name,
    #                     "alias": key_alias,
    #                     "type": key_type or key_column["type"]
    #                 })
    #             else:
    #                 print(
    #                     f"Warning: Key {key_name} not found in columns for table {table_name}. Adding with unknown type.")
    #                 keys.append({
    #                     "name": key_name,
    #                     "alias": key_alias,
    #                     "type": key_type or "unknown"
    #                 })
    #         print(f"Using custom keys for {table_name}: {keys}")
    #     else:
    #         keys = [
    #             {
    #                 "name": col["name"],
    #                 "alias": col["name"],
    #                 "type": col["type"]
    #             } for col in columns["columns"] if col.get("isPrimaryKey")
    #         ]
    #         print(f"Using primary keys for {table_name}: {keys}")
    #
    #     if not keys:
    #         print(f"Warning: No keys specified for {table_name}. Table will have no keys.")
    #
    #     # Create source and target table property keys
    #     source_table_key = f"{source_schema}.{table_name}"
    #     target_table_key = f"{yaml_target_schema}.{target_table_name}"
    #
    #     source_entity = {
    #         "type": "NoSqlEntity" if source_type.lower() == "nosql" else "SingleTable",
    #         "entityType": {**source_custom_properties, "type": "Source"},
    #         "agentId": source_agent_id,
    #         "entityObject": {
    #             "scope": source_schema,
    #             "collection": table_name
    #         },
    #         "table": {
    #             "name": table_name,
    #             "schema": source_schema
    #         },
    #         "columns": [
    #             {
    #                 "name": col["name"],
    #                 "alias": target_name,
    #                 "type": col["type"]
    #             }
    #             for col in columns["columns"]
    #             for column_map in custom_config.get('columns', [])
    #             for source_name, target_name in column_map.items()
    #             if source_name == col["name"]
    #         ] if custom_config.get('columns') else [
    #             {
    #                 "name": col["name"],
    #                 "alias": col["name"],
    #                 "type": col["type"]
    #             } for col in columns["columns"]
    #         ],
    #         "keys": keys,
    #         "customProperties": source_custom_properties,
    #         "tablesProperties": {source_table_key: {}}
    #     }
    #
    #     target_entity_type = {**target_custom_properties, "type": "Target"}
    #     if processed_filters:
    #         target_entity_type["filter"] = processed_filters
    #
    #     target_entity = {
    #         "type": "NoSqlEntity" if target_type.lower() == "nosql" else "SingleTable",
    #         "entityType": target_entity_type,
    #         "agentId": target_agent_id,
    #         "entityObject": {
    #             "scope": yaml_target_schema,
    #             "collection": target_table_name
    #         },
    #         "table": {
    #             "schema": yaml_target_schema,
    #             "name": target_table_name
    #         },
    #         "columns": [
    #             {
    #                 "name": target_name,
    #                 "alias": target_name,
    #                 "type": map_data_type(col["type"], source_node_info, target_node_info)
    #             }
    #             for col in columns["columns"]
    #             for column_map in custom_config.get('columns', [])
    #             for source_name, target_name in column_map.items()
    #             if source_name == col["name"]
    #         ] if custom_config.get('columns') else [
    #             {
    #                 "name": col["name"],
    #                 "alias": col["name"],
    #                 "type": map_data_type(col["type"], source_node_info, target_node_info)
    #             } for col in columns["columns"]
    #         ],
    #         "keys": [
    #             {
    #                 "name": key.get("alias", key["name"]),
    #                 "alias": key.get("alias", key["name"]),
    #                 "type": map_data_type(key["type"], source_node_info, target_node_info) if key.get("type") and key[
    #                     "type"] != "unknown" else key["type"]
    #             } for key in keys
    #         ],
    #         "customProperties": target_custom_properties,
    #         "tablesProperties": {target_table_key: {}},
    #         "sourceAgent": source_agent_id,
    #         "sourceTable": {
    #             "schema": source_schema,
    #             "name": table_name
    #         }
    #     }
    #
    #     # Add document key mapping if configured
    #     if document_key:
    #         target_entity["keyMapping"] = document_key
    #
    #     entity = {
    #         "entityName": f"{source_schema}.{table_name}",
    #         "agentEntities": [source_entity, target_entity]
    #     }
    #     entities.append(entity)
    # # Process entities in chunks
    # total_entities = len(entities)
    # successful_entities = 0
    # failed_entities = 0
    #
    # print(f"Processing {total_entities} entities in chunks of {chunk_size}...")
    #
    # for i in range(0, total_entities, chunk_size):
    #     chunk = entities[i:i + chunk_size]
    #     chunk_data = {"entities": chunk}
    #     chunk_start = i + 1
    #     chunk_end = min(i + chunk_size, total_entities)
    #
    #     print(f"\nProcessing chunk {chunk_start}-{chunk_end} of {total_entities} entities...")
    #
    #     try:
    #         response = fetch_core_hub(
    #             f"/pipelines/{pipeline_id}/config/entities",
    #             method="PUT",
    #             token=token,
    #             body=chunk_data
    #         )
    #         print(f"Successfully created entities {chunk_start}-{chunk_end}")
    #         successful_entities += len(chunk)
    #     except Exception as e:
    #         error_msg = str(e)
    #         print(f"Error creating entities {chunk_start}-{chunk_end}: {error_msg}")
    #         failed_entities += len(chunk)
    #         if not skip_errors:
    #             raise
    #         print("Skipping chunk due to skip_errors=True")
    #
    # print(f"\nProcessing complete:")
    # print(f"- Total entities: {total_entities}")
    # print(f"- Successfully created: {successful_entities}")
    # print(f"- Failed: {failed_entities}")
    #
    # # Create entity schedules if successful
    # if successful_entities > 0 and ENABLE_SCHEDULING:
    #     logger.info("Creating schedules for entities...")
    #
    #     # Get updated entity IDs from the pipeline config
    #     try:
    #         response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
    #         logger.info(f"Retrieved the following entities: {response}")
    #
    #         if not isinstance(response, list) or not response:
    #             logger.warning(f"Unexpected response when fetching entities: {response}")
    #             return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}
    #
    #         # Process entities in the format used in main.py
    #         entities_map = {}
    #         for item in response:
    #             if 'entity' in item and isinstance(item['entity'], dict):
    #                 entity = item['entity']
    #                 if 'entityId' in entity and 'entityName' in entity:
    #                     entity_id = entity['entityId']
    #                     entity_name = entity['entityName']
    #                     # Extract table name from entity name (typically schema.table)
    #                     parts = entity_name.split('.')
    #                     table_name = parts[-1] if len(parts) > 1 else entity_name
    #                     entities_map[table_name] = entity_id
    #                     logger.info(f"Found entity: {entity_name} (ID: {entity_id})")
    #
    #         # Create schedules for each entity found in the YAML config
    #         if yaml_config:
    #             # Track which tables we've already processed to avoid duplicates
    #             processed_tables = set()
    #
    #             # Determine if schemas are at root level or under 'schemas' key
    #             schemas_dict = yaml_config.get('schemas', {})
    #
    #             # If 'schemas' key doesn't exist or is empty, assume schemas are at root level
    #             if not schemas_dict:
    #                 # Treat each top-level key as a schema name
    #                 # Filter out keys that are not dictionaries (they wouldn't be schema configs)
    #                 schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
    #                 logger.info(f"Using root-level schema definitions: {list(schemas_dict.keys())}")
    #
    #             for schema_name, schema_config in schemas_dict.items():
    #                 if 'tables' in schema_config and 'custom' in schema_config['tables']:
    #                     custom_tables = schema_config['tables']['custom']
    #                     for table_key, table_data in custom_tables.items():
    #                         # Skip if we've already processed this table
    #                         if table_key in processed_tables:
    #                             continue
    #
    #                         processed_tables.add(table_key)
    #
    #                         # Use the table_key directly instead of looking for 'name' field
    #                         table_name = table_key
    #
    #                         if table_name in entities_map and 'schedules' in table_data:
    #                             entity_id = entities_map[table_name]
    #                             logger.info(f"Creating schedules for table {table_name} (Entity ID: {entity_id})")
    #                             create_entity_schedules(token, pipeline_id, entity_id, table_name,
    #                                                     table_data['schedules'])
    #                         else:
    #                             logger.warning(
    #                                 f"Unable to create schedules for {table_name}. Entity not found or no schedules defined.")
    #
    #         # Create pipeline-level schedules if defined
    #         if yaml_config:
    #             # Reuse the same schemas_dict from entity schedules
    #             if not 'schemas_dict' in locals():
    #                 # Determine if schemas are at root level or under 'schemas' key
    #                 schemas_dict = yaml_config.get('schemas', {})
    #
    #                 # If 'schemas' key doesn't exist or is empty, assume schemas are at root level
    #                 if not schemas_dict:
    #                     # Treat each top-level key as a schema name
    #                     schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
    #                     logger.info(
    #                         f"Using root-level schema definitions for pipeline schedules: {list(schemas_dict.keys())}")
    #
    #             for schema_name, schema_config in schemas_dict.items():
    #                 if 'schedules' in schema_config:
    #                     logger.info(f"Creating pipeline-level schedules for schema {schema_name}")
    #                     create_pipeline_schedules(token, pipeline_id, schema_config['schedules'])
    #
    #     except Exception as e:
    #         logger.error(f"Error creating schedules: {str(e)}")
    #         # Don't fail the whole process just because scheduling failed
    #
    # return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}


def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=False,
         chunk_size=50):
    """
    Main function to create entities for a pipeline
    """
    logger.info(f"Starting entity creation for pipeline {pipeline_id}")
    try:
        yaml_config = load_yaml_config(yaml_file) if yaml_file else None

        # Get pipeline configuration
        pipeline_config = get_pipeline_config(token, pipeline_id)

        # Get agents information
        agents = get_pipeline_agents(token, pipeline_id)

        if not agents or len(agents) != 2:
            error_msg = f"Expected 2 agents, found {len(agents) if agents else 0}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Identify source and target agents based on type
        source_agent = next((agent for agent in agents if agent['agentType'] == 'SOURCE'), None)
        target_agent = next((agent for agent in agents if agent['agentType'] == 'TARGET'), None)

        if not source_agent or not target_agent:
            error_msg = f"Could not find required agents. Source ({source_type}): {source_agent}, Target ({target_type}): {target_agent}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Get tables from source agent
        tables = get_agent_tables(token, pipeline_id, source_agent['agentId'], source_schema)

        if not tables:
            error_msg = f"No tables found in schema {source_schema}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Create entities
        create_user_defined_functions(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent['agentId'], target_agent['agentId'],
            source_type, target_type, yaml_config, skip_errors, chunk_size
        )

    except Exception as e:
        log_failure(logger, f"Error: {str(e)}")
        lockfile_failure()
        if not skip_errors:
            raise
        logger.warning("Skipping error due to skip_errors=True")
        return

    # Log successful completion
    log_success(logger, f"Entity creation completed successfully for pipeline {pipeline_id}")
    lockfile_complete()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gluesync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
    parser.add_argument('--chunk-size', type=int, default=50,
                        help="Number of entities to process in each chunk (default: 50)")
    parser.add_argument('--skip-errors', action='store_true', help="Continue execution even if errors occur")
    parser.add_argument('--target-schema', required=True, help="Target schema name")
    parser.add_argument('--source-type', required=True, help="Source agent type")
    parser.add_argument('--target-type', required=True, help="Target agent type")
    parser.add_argument('--yaml-file', help="YAML configuration file path")
    parser.add_argument('--token', required=True, help="Authentication token")
    parser.add_argument('--enable-scheduling', action='store_true',
                        help="Enable creation of schedules from YAML config")

    args = parser.parse_args()

    # Get command line arguments for scheduling
    if args.enable_scheduling:
        # Override the environment variable setting
        ENABLE_SCHEDULING = True

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

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
import urllib3
import argparse
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete
from utils.core_hub_client import CoreHubClient
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, map_data_type, load_yaml_config, create_group, \
    process_filter_clauses
from pydantic import BaseModel
from typing import List

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
CHRONOS_URL = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'

# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)


class ColumnDto(BaseModel):
    name: str
    type: str
    isPrimaryKey: bool = False
    isNullable: bool = False
    dataLength: int = 0


class GenerateTableStatementRequest(BaseModel):
    columns: List[ColumnDto]


class CreateTableRequest(BaseModel):
    statement: str


def table_exists(pipeline_id: str, schema_name: str, table_name: str, token: str) -> bool:
    try:
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{table_name}",
            method="GET",
            token=token
        )
        return True
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        print(f"checking table existence: {error_msg}")
        return False


def generate_create_table_statement(pipeline_id: str, schema_name: str, table_name: str, token: str,
                                    table_data: GenerateTableStatementRequest) -> str:
    try:
        print(f"table data: {table_data.model_dump()}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{table_name}/statements/create-table",
            method="PUT",
            token=token,
            body=table_data.model_dump()
        )
        return response
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        print(f"generate create table statement: {error_msg}")
        raise


def create_target_table(pipeline_id: str, create_table_request: CreateTableRequest, token: str):
    try:
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/statements/create-table",
            method="PUT",
            token=token,
            body=create_table_request.model_dump()
        )
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        print(f"generate create table statement: {error_msg}")
        raise


def create_tables(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id,
                  source_type, target_type, yaml_config, skip_errors=False):
    # First, collect all unique group names from the YAML configuration
    group_names = set()
    if yaml_config:
        schemas_dict = yaml_config.get('schemas', {})
        if not schemas_dict:
            schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}

        for schema_name, schema_config in schemas_dict.items():
            if 'tables' in schema_config and 'custom' in schema_config['tables']:
                for table_key, table_data in schema_config['tables']['custom'].items():
                    if 'groupId' in table_data and table_data['groupId'] != '_default':
                        group_names.add(table_data['groupId'])

    # Create groups before creating entities
    groupId_map = {}
    for group_name in group_names:
        groupId = create_group(token, pipeline_id, group_name)
        if groupId != '_default':
            groupId_map[group_name] = groupId
            logger.info(f"Created/retrieved group '{group_name}' with ID: {groupId}")

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

        columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)

        custom_config = custom_tables.get(table_name, {})
        if custom_config is None:
            custom_config = {}
            print(
                f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
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
            for key_def in custom_config['keys']:
                # Handle both string (key name) and dict (key with name/alias) formats
                if isinstance(key_def, dict):
                    # Handle the case where the key is specified as a dict with 'name' and optional 'alias'
                    key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
                    key_config = key_def.get(key_name, {}) if isinstance(key_def.get(key_name), dict) else {}

                    # Get the key name (either from the dict key or from the 'name' field)
                    key_name = key_name or key_config.get('name')
                    # Get the alias (defaults to the key name if not specified)
                    key_alias = key_config.get('name', key_name)

                    # Get the key type from the config or find it in the columns
                    key_type = key_config.get('type')
                else:
                    # Simple string format - use the string as both name and alias
                    key_name = key_def
                    key_alias = key_def
                    key_type = None

                # Try to find the key in the columns to get its type if not specified
                key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)

                if key_column:
                    keys.append({
                        "name": key_name,
                        "alias": key_alias,
                        "type": key_type or key_column["type"]
                    })
                else:
                    print(
                        f"Warning: Key {key_name} not found in columns for table {table_name}. Adding with unknown type.")
                    keys.append({
                        "name": key_name,
                        "alias": key_alias,
                        "type": key_type or "unknown"
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

        # check if the table exists on the target
        handle_table_creation(pipeline_id, target_table_name, yaml_target_schema, keys, token, columns, custom_config,
                              source_node_info, target_node_info)


def handle_table_creation(pipeline_id: str, target_table_name: str, yaml_target_schema: str, keys: list, token: str,
                          columns: list, custom_config, source_node_info, target_node_info):
    if not table_exists(pipeline_id=pipeline_id, schema_name=yaml_target_schema, table_name=target_table_name,
                        token=token):
        print(f"table: {target_table_name} does not exists")
        table_data = GenerateTableStatementRequest(columns=[
            ColumnDto(name=target_name,
                      type=map_data_type(col["type"], source_node_info, target_node_info),
                      isPrimaryKey=target_name in keys)
            for col in columns["columns"]
            for column_map in custom_config.get('columns', [])
            for source_name, target_name in column_map.items()
            if source_name == col["name"]
        ] if custom_config.get('columns') else [
            ColumnDto(name=col["name"], type=map_data_type(col["type"], source_node_info, target_node_info),
                      isPrimaryKey=col["isPrimaryKey"])
            for col in columns["columns"]
        ])
        statement = generate_create_table_statement(pipeline_id=pipeline_id, schema_name=yaml_target_schema,
                                                    table_name=target_table_name, token=token,
                                                    table_data=table_data)
        print(f"create table statement: {statement}")
        create_target_table(pipeline_id=pipeline_id, create_table_request=CreateTableRequest(statement=statement),
                            token=token)
    else:
        print(f"table: {target_table_name} already exists")


def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=False):
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

        if target_type.lower() != "sql":
            raise Exception(f"invalid target_type {target_type}")

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

        # Create tables
        create_tables(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent['agentId'], target_agent['agentId'],
            source_type, target_type, yaml_config, skip_errors
        )

    except Exception as e:
        log_failure(logger, f"Error: {str(e)}")
        lockfile_failure()
        if not skip_errors:
            raise
        logger.warning("Skipping error due to skip_errors=True")
        return

    # Log successful completion
    log_success(logger, f"Table creation completed successfully for pipeline {pipeline_id}")
    lockfile_complete()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gluesync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
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
    )

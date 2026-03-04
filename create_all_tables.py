# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

import os
import json
import urllib.parse

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
ENTITY_START_TIMEOUT = float(os.getenv('ENTITY_START_TIMEOUT', '1'))
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'
CREATE_TABLE_IF_NOT_EXISTS = os.getenv('CREATE_TABLE_IF_NOT_EXISTS', 'true').lower() == 'true'

# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

class ColumnDto(BaseModel):
    name: str
    type: str
    id: int
    ordinalPosition: int
    isPrimaryKey: bool = False
    isNullable: bool = False
    dataLength: int = 0
    numericPrecision: int = 0
    numericScale: int = 0

class GenerateTableStatementRequest(BaseModel):
    columns: List[ColumnDto]

class GenerateCreateTargetTableStatementRequest(BaseModel):
    columns: List[ColumnDto]

class CreateTableRequest(BaseModel):
    statement: str

def format_column_type(col_type: str, data_length: int, numeric_precision: int = 0, numeric_scale: int = 0) -> str:
    """
    Format column type for ColumnDto - return the base type without length formatting.
    The dataLength field should contain the length information separately.
    """
    # For all types, return as-is (the API will handle length/precision formatting)
    return col_type
    
def get_gluesync_data_type(source_type: str, source_node_info) -> str:
    """
    Get the Gluesync data type for a source column type.
    This extracts the gluesyncDataType from node info, similar to map_data_type.
    """
    source_matrix = source_node_info['dataTypesMatrix']
    
    normalized_source_type = source_type.split('(')[0].lower()
    
    # Find matching source type in matrix
    source_item = next(
        (item for item in source_matrix
         if any(t.lower() == normalized_source_type for t in item['supportedTypes'])),
        None
    )
    
    if source_item:
        return source_item['gluesyncDataType']
    
    # Default fallback
    logger.info(f"Warning: No gluesyncDataType mapping found for source type {source_type}. Using 'STRING' as fallback.")
    return 'STRING'

def table_exists(pipeline_id: str, schema_name: str, table_name: str, token: str) -> bool:
    """
    Check if a table exists in the given schema.
    """
    try:
        # URL-encode the table name to handle special characters
        encoded_table_name = urllib.parse.quote(table_name, safe='')
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{encoded_table_name}",
            method="GET",
            token=token
        )
        logger.info(f"Table {table_name} exists in schema {schema_name}")
        return True
    except (requests.exceptions.RequestException, RuntimeError) as e:
        error_msg = str(e)
        logger.info(f"Initial table existence check failed for {table_name}: {error_msg}")
        
        # Try opposite casing
        if table_name != table_name.lower():
            # Original has uppercase, try lowercase
            alternate_table_name = table_name.lower()
            logger.info(f"Trying lowercase version: {alternate_table_name}")
        elif table_name != table_name.upper():
            # Original is lowercase, try uppercase
            alternate_table_name = table_name.upper()
            logger.info(f"Trying uppercase version: {alternate_table_name}")
        else:
            # No casing change possible, return False
            return False
            
        try:
            # URL-encode the alternate table name as well
            encoded_alternate_table_name = urllib.parse.quote(alternate_table_name, safe='')
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{encoded_alternate_table_name}",
                method="GET",
                token=token
            )
            logger.info(f"Table {alternate_table_name} exists in schema {schema_name} (using alternate casing)")
            return True
        except (requests.exceptions.RequestException, RuntimeError) as e2:
            error_msg2 = str(e2)
            logger.info(f"Alternate casing check also failed for {alternate_table_name}: {error_msg2}")
        
        return False

def generate_create_table_statement(pipeline_id: str, schema_name: str, table_name: str, token: str,
                                    table_data: GenerateCreateTargetTableStatementRequest) -> str:
    try:
        # URL-encode the table name to handle special characters
        encoded_table_name = urllib.parse.quote(table_name, safe='')
        logger.info(f"table data: {table_data.model_dump()}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{encoded_table_name}/statements/create-table",
            method="PUT",
            token=token,
            body=table_data.model_dump()
        )
        return response
    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        logger.info(f"generate create table statement: {error_msg}")
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
        logger.info(f"generate create table statement: {error_msg}")
        raise

def create_tables(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id,
                  source_type, target_type, yaml_config, skip_errors=True,
                  source_agent_tag=None, target_agent_tag=None):
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

    logger.info("Source Node Info:")
    logger.info(json.dumps(source_node_info, indent=2))
    logger.info("Target Node Info:")
    logger.info(json.dumps(target_node_info, indent=2))

    logger.info(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")

    schemas_root = yaml_config.get('schemas', yaml_config)
    schema_config = schemas_root.get(source_schema, {})
    logger.info(f"Schema config for {source_schema}: {json.dumps(schema_config, indent=2)}")

    yaml_target_schema = schema_config.get('target', target_schema)
    whitelist = schema_config.get('tables', {}).get('whitelist', [])
    blacklist = schema_config.get('tables', {}).get('blacklist', [])
    # Handle empty custom tables attribute - convert None to empty dict
    tables_config = schema_config.get('tables', {})
    custom_tables = tables_config.get('custom', {})
    if custom_tables is None:
        custom_tables = {}
        logger.info("Warning: 'custom' attribute is present but empty in YAML. Converting to empty dict.")

    # Get schema-level custom properties
    schema_custom_properties = schema_config.get('customProperties', {})
    global_source_custom_properties = schema_custom_properties.get('source', {})
    global_target_custom_properties = schema_custom_properties.get('target', {})

    logger.info(f"Target schema: {yaml_target_schema}")
    logger.info(f"Whitelist: {whitelist}")
    logger.info(f"Blacklist: {blacklist}")
    logger.info(f"Custom tables: {custom_tables}")
    logger.info(f"Global source custom properties: {global_source_custom_properties}")
    logger.info(f"Global target custom properties: {global_target_custom_properties}")

    for table in tables:
        if isinstance(table, str):
            table_name = table
        else:
            table_name = table.get("name")

        if not table_name:
            logger.info(f"Warning: Table without name encountered. Skipping.")
            continue

        if (table_name.startswith("sys") or
                (blacklist and table_name in blacklist) or
                (whitelist and table_name not in whitelist)):
            logger.info(f"Skipping table: {table_name}")
            continue

        columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)

        custom_config = custom_tables.get(table_name, {})
        if custom_config is None:
            custom_config = {}
            logger.info(
                f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
        logger.info(f"Custom config for {table_name}: {custom_config}")

        # Get table-specific custom properties and merge with global properties
        table_custom_properties = custom_config.get('customProperties', {})
        source_custom_properties = {**global_source_custom_properties, **table_custom_properties.get('source', {})}
        target_custom_properties = {**global_target_custom_properties, **table_custom_properties.get('target', {})}

        logger.info(f"Source custom properties for {table_name}: {source_custom_properties}")
        logger.info(f"Target custom properties for {table_name}: {target_custom_properties}")

        # Get custom target table name if specified
        target_table_name = custom_config.get('name', table_name)
        logger.info(f"Using target table name: {target_table_name} for source table: {table_name}")

        # Get and process filter configuration
        filter_config = custom_config.get('filter')
        processed_filters = process_filter_clauses(filter_config, columns) if filter_config else None
        logger.info(f"Processed filters for {table_name}: {processed_filters}")

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
            logger.info(f"Document key configuration for {table_name}: {document_key}")

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
                    logger.info(
                        f"Warning: Key {key_name} not found in columns for table {table_name}. Adding with unknown type.")
                    keys.append({
                        "name": key_name,
                        "alias": key_alias,
                        "type": key_type or "unknown"
                    })
            logger.info(f"Using custom keys for {table_name}: {keys}")
        else:
            keys = [
                {
                    "name": col["name"],
                    "alias": col["name"],
                    "type": col["type"]
                } for col in columns["columns"] if col.get("isPrimaryKey")
            ]
            logger.info(f"Using primary keys for {table_name}: {keys}")

            if not keys and custom_config:
                # Source has no discovered PKs — fall back to YAML keys (or documentKey.keys)
                yaml_fallback_keys = custom_config.get('keys') or []
                if not yaml_fallback_keys and 'documentKey' in custom_config:
                    yaml_fallback_keys = custom_config['documentKey'].get('keys', [])

                if yaml_fallback_keys:
                    logger.info(f"Source table {table_name} has no PKs — using YAML-defined keys as fallback: {yaml_fallback_keys}")
                    for key_name in yaml_fallback_keys:
                        key_column = next((col for col in columns["columns"] if col["name"].lower() == key_name.lower()), None)
                        if key_column:
                            keys.append({
                                "name": key_column["name"],
                                "alias": key_column["name"],
                                "type": key_column["type"]
                            })
                        else:
                            logger.warning(f"Fallback key '{key_name}' not found in columns for table '{table_name}'")

        if not keys:
            logger.info(f"Warning: No keys specified for {table_name}. Table will have no keys.")

        # check if the table exists on the target
        handle_table_creation(pipeline_id, target_table_name, yaml_target_schema, keys, token, columns, custom_config,
                              source_node_info, target_node_info,
                              source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)


def handle_table_creation(pipeline_id: str, target_table_name: str, yaml_target_schema: str, keys: list, token: str,
                          columns: list, custom_config, source_node_info, target_node_info,
                          source_agent_tag=None, target_agent_tag=None):
    custom_config = custom_config or {}

    if not table_exists(pipeline_id=pipeline_id, schema_name=yaml_target_schema, table_name=target_table_name,
                        token=token):
        if CREATE_TABLE_IF_NOT_EXISTS:
            logger.info(f"table: {target_table_name} does not exists, creating it")
            # Create ColumnDto objects
            column_dtos = []
            column_entries = custom_config.get('columns') or []
            target_only_columns = custom_config.get('targetOnlyColumns', [])

            key_names = set()
            for key in keys:
                if isinstance(key, dict):
                    if key.get("name"):
                        key_names.add(key["name"])
                    if key.get("alias"):
                        key_names.add(key["alias"])
                else:
                    key_names.add(key)

            key_names_lower = {
                key_name.lower() for key_name in key_names if isinstance(key_name, str)
            }

            def _is_primary_key(column_name: str) -> bool:
                if column_name in key_names:
                    return True
                return isinstance(column_name, str) and column_name.lower() in key_names_lower

            def _to_int(value, default=0):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default

            def _to_bool(value, default=True):
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "y"}
                if value is None:
                    return default
                return bool(value)

            # Check if targetOnlyColumns are defined - if so, use them instead of source columns
            if target_only_columns:
                logger.info(f"Using targetOnlyColumns definition for table {target_table_name}")
                for idx, target_col in enumerate(target_only_columns, 1):
                    # Support both string format and object format
                    if isinstance(target_col, str):
                        # Simple string format: just the column name
                        col_name = target_col
                        col_type = 'varchar'  # Default type
                        col_data_length = 1024  # Default data length
                        col_numeric_precision = 0  # Default numeric precision
                        col_numeric_scale = 0  # Default numeric scale
                        col_is_nullable = False  # Default nullable
                    else:
                        # Object format with user-defined properties
                        col_name = target_col.get('name')
                        col_type = target_col.get('type', 'varchar')  # Default to varchar if not specified
                        col_data_length = target_col.get('dataLength', 1024)  # Default data length
                        col_numeric_precision = target_col.get('numericPrecision', 0)  # Default numeric precision
                        col_numeric_scale = target_col.get('numericScale', 0)  # Default numeric scale
                        col_is_nullable = target_col.get('isNullable', False)  # Default nullable
                    
                    # Map the column type to target node type
                    mapped_type = map_data_type(col_type, source_node_info, target_node_info,
                                               source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                    
                    column_dtos.append(ColumnDto(
                        name=col_name,
                        type=format_column_type(mapped_type, col_data_length, col_numeric_precision, col_numeric_scale),
                        id=idx,  # Use sequential IDs for target-only columns
                        ordinalPosition=idx,
                        isPrimaryKey=_is_primary_key(col_name),
                        isNullable=col_is_nullable,
                        dataLength=col_data_length,
                        numericPrecision=col_numeric_precision,
                        numericScale=col_numeric_scale
                    ))
            else:
                metadata_columns = []
                simple_mappings = []
                for entry in column_entries:
                    if isinstance(entry, dict) and 'name' in entry:
                        metadata_columns.append(entry)
                    elif entry is None:
                        continue
                    else:
                        simple_mappings.append(entry)

                if simple_mappings:
                    logger.info(
                        f"Skipping table creation for {target_table_name}: column mappings use source→target format, which is not supported for CREATE_TABLE_IF_NOT_EXISTS.")
                    return

                if metadata_columns:
                    logger.info(f"Using YAML column metadata for table {target_table_name}")
                    for idx, col_meta in enumerate(metadata_columns, 1):
                        col_name = col_meta.get('name')
                        if not col_name:
                            logger.info(
                                f"Warning: Column metadata entry missing 'name' for table {target_table_name}. Skipping entry: {col_meta}")
                            continue

                        col_type = col_meta.get('type', 'varchar')
                        col_data_length = _to_int(col_meta.get('dataLength', col_meta.get('data_length')))
                        col_numeric_precision = _to_int(col_meta.get('numericPrecision', col_meta.get('numeric_precision')))
                        col_numeric_scale = _to_int(col_meta.get('numericScale', col_meta.get('numeric_scale')))
                        col_is_nullable = _to_bool(col_meta.get('isNullable', col_meta.get('is_nullable')))
                        
                        # Use separate id and ordinalPosition from YAML metadata
                        column_id = _to_int(col_meta.get('id'), idx)
                        ordinal_position = _to_int(col_meta.get('ordinalPosition'), idx)

                        # Map source type to target type (e.g. CHARACTER -> varchar)
                        mapped_type = map_data_type(col_type, source_node_info, target_node_info,
                                                   source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                        column_dtos.append(ColumnDto(
                            name=col_name,
                            type=format_column_type(mapped_type, col_data_length, col_numeric_precision, col_numeric_scale),
                            id=column_id,
                            ordinalPosition=ordinal_position,
                            isPrimaryKey=_is_primary_key(col_name),
                            isNullable=col_is_nullable,
                            dataLength=col_data_length,
                            numericPrecision=col_numeric_precision,
                            numericScale=col_numeric_scale
                        ))

                if not column_dtos:
                    # When no metadata is provided, fall back to source column properties directly
                    for col in columns["columns"]:
                        # Map source type to target type (e.g. CHARACTER -> varchar)
                        mapped_type = map_data_type(col["type"], source_node_info, target_node_info,
                                                   source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                        column_dtos.append(ColumnDto(
                            name=col["name"],
                            type=format_column_type(mapped_type, col.get("dataLength", 0),
                                                   col.get("numericPrecision", 0), col.get("numericScale", 0)),
                            id=col.get("ordinalPosition", col.get("id", 1)),
                            ordinalPosition=col.get("ordinalPosition", col.get("id", 1)),
                            isPrimaryKey=_is_primary_key(col["name"]),
                            isNullable=col.get("isNullable", False),
                            dataLength=col.get("dataLength", 0),
                            numericPrecision=col.get("numericPrecision", 0),
                            numericScale=col.get("numericScale", 0)
                        ))
            
            table_data = GenerateCreateTargetTableStatementRequest(columns=column_dtos)
            statement = generate_create_table_statement(pipeline_id=pipeline_id, schema_name=yaml_target_schema,
                                                        table_name=target_table_name, token=token,
                                                        table_data=table_data)
            logger.info(f"create table statement: {statement}")
            create_target_table(pipeline_id=pipeline_id, create_table_request=CreateTableRequest(statement=statement),
                                token=token)
        else:
            logger.info(f"table: {target_table_name} does not exists, skipping creation as CREATE_TABLE_IF_NOT_EXISTS is false")
    else:
        logger.info(f"table: {target_table_name} already exists")


def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=True):
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
            source_type, target_type, yaml_config, skip_errors,
            source_agent_tag=source_agent.get('agentTag'),
            target_agent_tag=target_agent.get('agentTag')
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
    
    args = parser.parse_args()

    main(
        args.pipeline,
        args.source_schema,
        args.target_schema,
        args.source_type,
        args.target_type,
        args.yaml_file,
        args.token,
        args.skip_errors
    )

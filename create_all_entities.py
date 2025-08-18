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
import urllib3
import argparse
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, create_entity_schedules, map_data_type, create_pipeline_schedules, load_yaml_config, \
    process_filter_clauses, create_group, assign_entities_to_group
from create_all_tables import handle_table_creation
from create_user_defined_functions import handle_udf_function_definition
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete, exit_on_fail
from utils.core_hub_client import CoreHubClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENTITY_START_TIMEOUT = int(os.getenv('ENTITY_START_TIMEOUT', '1'))
CREATE_TABLE_IF_NOT_EXISTS = os.getenv('CREATE_TABLE_IF_NOT_EXISTS', 'true').lower() == 'true'

# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

def get_allowed_operations(target_custom_properties):
    """
    Get allowedOperations array from target custom properties.
    
    Args:
        target_custom_properties (dict): Target custom properties from YAML config
        
    Returns:
        list: Array of allowed operations, defaults to ["INSERT", "DELETE", "UPDATE", "TRUNCATE"] if not specified
    """
    # Check if allowedOperations is explicitly defined
    if 'allowedOperations' in target_custom_properties:
        allowed_ops = target_custom_properties['allowedOperations']
        if isinstance(allowed_ops, list) and len(allowed_ops) > 0:
            logger.info(f"Using configured allowedOperations: {allowed_ops}")
            return allowed_ops
    
    # Check for legacy skipDeletion property for backward compatibility
    if target_custom_properties.get('skipDeletion', False):
        logger.info("Found legacy skipDeletion=true, converting to allowedOperations without DELETE")
        return ["INSERT", "UPDATE", "TRUNCATE"]
    
    # Default values if nothing is declared
    default_ops = ["INSERT", "DELETE", "UPDATE", "TRUNCATE"]
    logger.info(f"Using default allowedOperations: {default_ops}")
    return default_ops

def create_entities(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id, source_type, target_type, yaml_config, skip_errors=False, chunk_size=50):
    # First, collect all unique group names and chain IDs from the YAML configuration
    group_names = set()
    chain_ids = set()
    chained_tables = {}  # Dictionary to store tables by chainId

    if yaml_config:
        schemas_dict = yaml_config.get('schemas', {})
        if not schemas_dict:
            schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}

        for schema_name, schema_config in schemas_dict.items():
            # Look for tables in both direct and nested 'tables.custom' structures
            tables_config = {}
            if 'tables' in schema_config and 'custom' in schema_config['tables']:
                tables_config = schema_config['tables']['custom']
            elif 'custom' in schema_config:
                tables_config = schema_config['custom']
            
            # Process each table in the config
            for table_key, table_data in tables_config.items():
                if 'groupId' in table_data and table_data['groupId'] != '_default':
                    group_name = str(table_data['groupId']).strip()
                    if group_name:  # Only add non-empty group names
                        group_names.add(group_name)
                        logger.debug(f"Found group '{group_name}' for table {schema_name}.{table_key}")
                    else:
                        logger.warning(f"Empty group ID found for table {schema_name}.{table_key}")

                    # Collect chainId information
                    if 'chainId' in table_data:
                        chain_id = table_data['chainId']
                        chain_ids.add(chain_id)

                        # Get orderIndex, default to 0 if not specified
                        order_index = table_data.get('orderIndex', 0)

                        if chain_id not in chained_tables:
                            chained_tables[chain_id] = []
                        chained_tables[chain_id].append((table_key, table_data, order_index))

    # Create groups before creating entities
    groupId_map = {}
    logger.debug(f"Groups to create: {group_names}")
    
    for group_name in group_names:
        logger.debug(f"Processing group: '{group_name}' (type: {type(group_name)})")
        group_id = create_group(token, pipeline_id, group_name)
        logger.debug(f"Group '{group_name}' creation result: {group_id}")
        
        if group_id and group_id != '_default':
            # Map the group name to its ID for later use
            groupId_map[group_name] = group_id
            logger.info(f"Created/retrieved group '{group_name}' with ID: {group_id}")
        else:
            logger.warning(f"Failed to create/retrieve group '{group_name}'. Using default group.")
            groupId_map[group_name] = '_default'
    
    # Log all group mappings for debugging
    logger.info(f"Group name to ID mappings: {json.dumps(groupId_map, indent=2)}")

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

        handle_table_creation(pipeline_id, target_table_name, yaml_target_schema, keys, token, columns, custom_config,
                              source_node_info, target_node_info)

        # Create source and target table property keys
        source_table_key = f"{source_schema}.{table_name}"
        target_table_key = f"{yaml_target_schema}.{target_table_name}"

        source_entity = {
            "type": "NoSqlEntity" if source_type.lower() == "nosql" else "SingleTable",
            "entityType": {**source_custom_properties, "type": "Source"},
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
                    "alias": target_name,
                    "type": col["type"]
                }
                for col in columns["columns"]
                for column_map in custom_config.get('columns', [])
                for source_name, target_name in column_map.items()
                if source_name == col["name"]
            ] if custom_config.get('columns') else [
                {
                    "name": col["name"],
                    "alias": col["name"],
                    "type": col["type"]
                } for col in columns["columns"]
            ],
            "keys": keys,
            "customProperties": source_custom_properties,
            "tablesProperties": {source_table_key: {}}
        }

        # Get allowed operations for the target entity
        allowed_operations = get_allowed_operations(target_custom_properties)
        
        # Create target entity type with allowedOperations
        target_entity_type = {
            "type": "Target",
            "allowedOperations": allowed_operations,
            "snapshotWritingConcurrency": target_custom_properties.get('snapshotWritingConcurrency', 1)
        }
        
        # Add other target custom properties (excluding the ones we handle separately)
        excluded_props = {'allowedOperations', 'skipDeletion', 'snapshotWritingConcurrency'}
        for key, value in target_custom_properties.items():
            if key not in excluded_props:
                target_entity_type[key] = value
        if processed_filters:
            target_entity_type["filter"] = processed_filters

        if target_custom_properties.get("udf"):
            # Get the first UDF definition (assuming one UDF per table for now)
            udf_list = target_custom_properties.get("udf")
            if udf_list and len(udf_list) > 0:
                udf_def = udf_list[0]  # Take the first UDF
                target_entity_type["mappingFunctionInfo"] = {
                    "name": udf_def.get("name"),
                    "type": udf_def.get("type")
                }

        target_entity = {
            "type": "NoSqlEntity" if target_type.lower() == "nosql" else "SingleTable",
            "entityType": target_entity_type,
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
                    "name": target_name,
                    "alias": target_name,
                    "type": map_data_type(col["type"], source_node_info, target_node_info)
                }
                for col in columns["columns"]
                for column_map in custom_config.get('columns', [])
                for source_name, target_name in column_map.items()
                if source_name == col["name"]
            ] if custom_config.get('columns') else [
                {
                    "name": col["name"],
                    "alias": col["name"],
                    "type": map_data_type(col["type"], source_node_info, target_node_info)
                } for col in columns["columns"]
            ],
            "keys": [
                {
                    "name": key.get("alias", key["name"]),
                    "alias": key.get("alias", key["name"]),
                    "type": map_data_type(key["type"], source_node_info, target_node_info) if key.get("type") and key[
                        "type"] != "unknown" else key["type"]
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

        # Get the requested group from config, default to '_default'
        requested_group = str(custom_config.get('groupId', '_default')).strip()
        group_id = "_default"  # Default group ID
        
        # Log the requested group for debugging
        logger.debug(f"Requested group: '{requested_group}' (type: {type(requested_group)})")
        
        # If a specific group was requested and it's not '_default'
        if requested_group and requested_group != '_default':
            # Try to create or get the group
            group_id = create_group(token, pipeline_id, requested_group)
            
            if group_id != "_default":
                logger.info(f"Using group '{requested_group}' with ID: {group_id}")
            else:
                logger.warning(f"Could not use group '{requested_group}'. Using default group.")
        else:
            logger.debug("Using default group as no specific group was requested")
        
        # Create the entity
        entity = {
            "entityName": f"{source_schema}.{table_name}",
            "agentEntities": [source_entity, target_entity]
        }
        
        # Only add groupId to the entity if we have a valid group ID
        if group_id and group_id != '_default':
            entity["groupId"] = group_id
        
        entities.append(entity)
        
        # Process UDFs if defined for this table
        table_custom_properties = custom_config.get('customProperties', {})
        target_custom_properties = table_custom_properties.get('target', {})
        table_udfs = target_custom_properties.get('udf', [])
        
        if table_udfs:
            logger.info(f"Found UDFs for table {table_name}: {table_udfs}")
            try:
                handle_udf_function_definition(table_name, pipeline_id, table_udfs, token)
                logger.info(f"Successfully processed UDFs for table {table_name}")
            except Exception as e:
                logger.error(f"Failed to process UDFs for table {table_name}: {str(e)}")
                if not skip_errors:
                    raise
        else:
            logger.debug(f"No UDFs defined for table {table_name}")

    # Process MultiTable entities first
    multi_table_entities = []
    # For each chainId, create a MultiTable entity
    for chain_id, tables_list in chained_tables.items():
        if not tables_list:
            continue

        # Sort tables by orderIndex
        sorted_tables = sorted(tables_list, key=lambda x: x[2])

        # Log table ordering for debugging
        logger.info(f"Creating MultiTable entity for chainId: {chain_id} with {len(sorted_tables)} tables in order:")
        for idx, (table_key, _, order_index) in enumerate(sorted_tables):
            logger.info(f"  {idx+1}. Table {table_key} with orderIndex: {order_index}")


        # We'll use the first table's name as the entity name prefix
        first_table_key, first_table_data, first_table_order_index = sorted_tables[0]
        entity_name = f"{source_schema}.{first_table_key}"

        # Initialize tables, columns, and keys for the MultiTable entity
        multi_tables = []
        multi_columns = []
        multi_keys = []
        tables_properties = {}

        # Process each table in the chain
        for table_key, table_data, order_index in sorted_tables:
            # Get columns for this table
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_key)

            # Add table to the list
            table_obj = {
                "name": table_key,
                "schema": source_schema
            }
            multi_tables.append(table_obj)

            # Add table to tables_properties
            tables_properties[f"{source_schema}.{table_key}"] = {}

            # Process columns
            table_columns = []
            for col in columns["columns"]:
                table_columns.append({
                    "name": col["name"],
                    "alias": col["name"],
                    "table": {"name": table_key, "schema": source_schema},
                    "type": col["type"]
                })

            # Add columns for this table
            multi_columns.append({"name": table_key, "schema": source_schema})
            multi_columns.append(table_columns)

            # Process keys
            custom_config = table_data
            if custom_config and 'keys' in custom_config:
                keys = []
                for key_def in custom_config['keys']:
                    # Handle both string (key name) and dict (key with name/alias) formats
                    if isinstance(key_def, dict):
                        key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
                        key_config = key_def.get(key_name, {}) if isinstance(key_def.get(key_name), dict) else {}

                        key_name = key_name or key_config.get('name')
                        key_alias = key_config.get('name', key_name)
                        key_type = key_config.get('type')
                    else:
                        key_name = key_def
                        key_alias = key_def
                        key_type = None

                    # Try to find the key in the columns to get its type if not specified
                    key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)

                    if key_column:
                        keys.append({
                            "name": key_name,
                            "alias": key_alias,
                            "table": {"name": table_key, "schema": source_schema},
                            "type": key_type or key_column["type"]
                        })
                    else:
                        print(f"Warning: Key {key_name} not found in columns for table {table_key}. Adding with unknown type.")
                        keys.append({
                            "name": key_name,
                            "alias": key_alias,
                            "table": {"name": table_key, "schema": source_schema},
                            "type": key_type or "unknown"
                        })
            else:
                keys = [
                    {
                        "name": col["name"],
                        "alias": col["name"],
                        "table": {"name": table_key, "schema": source_schema},
                        "type": col["type"]
                    } for col in columns["columns"] if col.get("isPrimaryKey")
                ]

            # Add keys for this table
            multi_keys.append({"name": table_key, "schema": source_schema})
            multi_keys.append(keys)

        # Create source entity for MultiTable
        source_custom_properties = table_data.get('customProperties', {}).get('source', {})
        source_entity = {
            "type": "MultiTable",
            "entityId": "",
            "entityName": entity_name,
            "agentEntityId": "",
            "entityType": {
                "type": "Source",
                "maxFetchItemsCountPerIteration": source_custom_properties.get('maxItemsCountPerIteration', 1000),
                "maxTransactionMessageKbSize": source_custom_properties.get('maxTransactionMessageKbSize', 1024),
                "pollingIntervalMilliseconds": source_custom_properties.get('pollingIntervalMilliseconds', 100),
                "unchangedDataFilterType": "ENTIRE_ROW"
            },
            "agentId": source_agent_id,
            "orderIndex": first_table_order_index,  # Use the first table's orderIndex for the source entity
            "customProperties": {},
            "tablesProperties": tables_properties,
            "tables": multi_tables,
            "columns": multi_columns,
            "keys": multi_keys
        }

        # Create target entity for MultiTable
        target_custom_properties = table_data.get('customProperties', {}).get('target', {})
        target_tables = []
        target_columns = []
        target_keys = []
        target_tables_properties = {}

        # Process each table for the target
        for table_key, table_data, order_index in sorted_tables:
            # Add table to the list
            target_table_obj = {
                "name": table_key,
                "schema": target_schema
            }
            target_tables.append(target_table_obj)

            # Add table to tables_properties
            target_tables_properties[f"{target_schema}.{table_key}"] = {}

            # Get columns for this table
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_key)

            # Process columns for target
            target_table_columns = []
            for col in columns["columns"]:
                target_table_columns.append({
                    "name": col["name"],
                    "type": map_data_type(col["type"], source_node_info, target_node_info)
                })

            # Add columns for this table
            target_columns.append({"name": table_key, "schema": target_schema})
            target_columns.append(target_table_columns)

            # Process keys for target
            custom_config = table_data
            if custom_config and 'keys' in custom_config:
                keys = []
                for key_def in custom_config['keys']:
                    if isinstance(key_def, dict):
                        key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
                    else:
                        key_name = key_def

                    # Try to find the key in the columns to get its type
                    key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)

                    if key_column:
                        keys.append({
                            "name": key_name,
                            "type": map_data_type(key_column["type"], source_node_info, target_node_info)
                        })
                    else:
                        keys.append({
                            "name": key_name,
                            "type": "unknown"
                        })
            else:
                keys = [
                    {
                        "name": col["name"],
                        "type": map_data_type(col["type"], source_node_info, target_node_info)
                    } for col in columns["columns"] if col.get("isPrimaryKey")
                ]

            # Add keys for this table
            target_keys.append({"name": table_key, "schema": target_schema})
            target_keys.append(keys)

        # Get allowed operations for the target entity
        allowed_operations = get_allowed_operations(target_custom_properties)
        
        # Create the target entity for MultiTable
        target_entity = {
            "type": "MultiTable",
            "entityId": "",
            "entityName": entity_name,
            "agentEntityId": "",
            "entityType": {
                "type": "Target",
                "allowedOperations": allowed_operations,
                "snapshotWritingConcurrency": target_custom_properties.get('snapshotWritingConcurrency', 1)
            },
            "agentId": target_agent_id,
            "orderIndex": first_table_order_index,  # Use the first table's orderIndex for the target entity
            "customProperties": {"ttlValue": target_custom_properties.get('ttlValue', 0)},
            "tablesProperties": target_tables_properties,
            "tables": target_tables,
            "columns": target_columns,
            "keys": target_keys
        }

        # Get group info for multi-table entity
        group_id = groupId_map.get(table_data.get('groupId', '_default'), table_data.get('groupId', '_default'))
        group_name = next((name for name, gid in groupId_map.items() if gid == group_id), group_id)
        logger.info(f"Assigning multi-table entity '{entity_name}' to group: {group_name} (ID: {group_id})")
        
        # Create the MultiTable entity
        multi_table_entity = {
            "entities": [{
                "entityId": "",
                "entityName": entity_name,
                "agentEntities": [source_entity, target_entity],
                "groupId": group_id if group_id != '_default' else None,  # Use None instead of '_default' for the API
                "orderIndex": first_table_order_index  # Use the first table's orderIndex for the entire entity
            }]
        }

        # Add to multi_table_entities for separate processing
        multi_table_entities.append(multi_table_entity)

    # Process standard entities in chunks
    # Filter out entities that are part of a MultiTable (chainId)
    filtered_entities = []
    for entity in entities:
        entity_name_parts = entity["entityName"].split('.')
        if len(entity_name_parts) > 1:
            table_name = entity_name_parts[-1]
            is_chained = False

            # Check if this table is in any chain
            for tables_list in chained_tables.values():
                for table_key, _, _ in tables_list:
                    if table_key == table_name:
                        is_chained = True
                        break
                if is_chained:
                    break

            if not is_chained:
                filtered_entities.append(entity)
        else:
            filtered_entities.append(entity)

    # Reset entities to the filtered list
    entities = filtered_entities

    # Now process MultiTable entities first
    successful_multi_tables = 0
    failed_multi_tables = 0
    total_multi_tables = len(multi_table_entities)

    if total_multi_tables > 0:
        logger.info(f"Processing {total_multi_tables} MultiTable entities...")

        for i, multi_entity in enumerate(multi_table_entities):
            try:
                logger.info(f"Creating MultiTable entity {i+1}/{total_multi_tables}: {multi_entity['entities'][0]['entityName']}")

                # Log the final tables ordering in payload before sending
                for agent_entity in multi_entity['entities'][0].get('agentEntities', []):
                    if 'tables' in agent_entity:
                        logger.info(f"Final tables order in payload for {agent_entity.get('entityName')}:")
                        for idx, table in enumerate(agent_entity.get('tables', [])):
                            logger.info(f"  {idx+1}. {table.get('schema')}.{table.get('name')}")

                response = fetch_core_hub(
                    f"/pipelines/{pipeline_id}/config/entities",
                    method="PUT",
                    token=token,
                    body=multi_entity
                )
                logger.info(f"Successfully created MultiTable entity {i+1}/{total_multi_tables}")
                successful_multi_tables += 1
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error creating MultiTable entity {i+1}/{total_multi_tables}: {error_msg}")
                failed_multi_tables += 1
                if not skip_errors:
                    raise
                logger.warning("Skipping MultiTable entity due to skip_errors=True")

    # Process regular entities in chunks
    total_entities = len(entities)
    successful_entities = 0
    failed_entities = 0

    if total_entities > 0:
        logger.info(f"Processing {total_entities} regular entities in chunks of {chunk_size}...")

        for i in range(0, total_entities, chunk_size):
            chunk = entities[i:i + chunk_size]
            chunk_data = {"entities": chunk}
            chunk_start = i + 1
            chunk_end = min(i + chunk_size, total_entities)

            logger.info(f"\nProcessing chunk {chunk_start}-{chunk_end} of {total_entities} entities...")

            # Log the final tables ordering in payload before sending
            for entity_payload in chunk_data.get('entities', []):
                for agent_entity in entity_payload.get('agentEntities', []):
                    if 'tables' in agent_entity:
                        logger.info(f"Final tables order in payload for {agent_entity.get('entityName')}:")
                        for idx, table in enumerate(agent_entity.get('tables', [])):
                            logger.info(f"  {idx+1}. {table.get('schema')}.{table.get('name')}")

            try:
                response = fetch_core_hub(
                    f"/pipelines/{pipeline_id}/config/entities",
                    method="PUT",
                    token=token,
                    body=chunk_data
                )
                logger.info(f"Successfully created entities {chunk_start}-{chunk_end}")
                successful_entities += len(chunk)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error creating entities {chunk_start}-{chunk_end}: {error_msg}")
                failed_entities += len(chunk)
                if not skip_errors:
                    raise
                logger.warning("Skipping chunk due to skip_errors=True")

    print(f"\nProcessing complete:")
    print(f"- Total regular entities: {total_entities}")
    print(f"- Successfully created regular entities: {successful_entities}")
    print(f"- Failed regular entities: {failed_entities}")
    print(f"- Total MultiTable entities: {total_multi_tables}")
    print(f"- Successfully created MultiTable entities: {successful_multi_tables}")
    print(f"- Failed MultiTable entities: {failed_multi_tables}")

    # Assign entities to their groups if successful
    if successful_entities > 0:
        # Group entities by their target group
        entities_by_group = {}
        
        # Get all created entities
        try:
            response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
            if isinstance(response, list):
                for entity in response:
                    if 'entity' in entity and 'entityId' in entity['entity']:
                        entity_id = entity['entity']['entityId']
                        group_id = entity['entity'].get('groupId', '_default')
                        
                        # Skip if no group is specified (will use _default)
                        if not group_id or group_id == '_default':
                            logger.debug(f"Entity {entity_id} has no group specified, will use _default")
                            continue
                        
                        # If group_id is a name, try to get the mapped ID
                        if group_id in groupId_map:
                            mapped_group_id = groupId_map[group_id]
                            logger.debug(f"Mapped group name '{group_id}' to ID: {mapped_group_id}")
                            group_id = mapped_group_id
                        
                        if group_id not in entities_by_group:
                            entities_by_group[group_id] = []
                        entities_by_group[group_id].append(entity_id)
            
            # Make assignment requests for each group
            for group_id, entity_ids in entities_by_group.items():
                if entity_ids:  # Only proceed if we have entities to assign
                    # Skip if group_id is _default or None
                    if not group_id or group_id == '_default':
                        logger.debug(f"Skipping assignment for default group")
                        continue
                        
                    logger.info(f"Assigning {len(entity_ids)} entities to group ID: {group_id}")
                    success = assign_entities_to_group(token, pipeline_id, group_id, entity_ids)
                    if not success and not skip_errors:
                        raise Exception(f"Failed to assign entities to group {group_id}")
                    elif success:
                        logger.info(f"Successfully assigned {len(entity_ids)} entities to group {group_id}")
                    
        except Exception as e:
            logger.error(f"Error during group assignment: {str(e)}")
            if not skip_errors:
                raise

    # Create entity schedules if successful
    if successful_entities > 0:
        # Check if any schedules are defined in the YAML config
        has_schedules = False
        if yaml_config:
            schemas_dict = yaml_config.get('schemas', {})
            if not schemas_dict:
                # Check if top-level keys are schemas
                schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
            
            # Check for pipeline-level schedules
            for schema_config in schemas_dict.values():
                if 'schedules' in schema_config:
                    has_schedules = True
                    break
                
                # Check for entity-level schedules
                if 'tables' in schema_config and 'custom' in schema_config['tables']:
                    for table_data in schema_config['tables']['custom'].values():
                        if 'schedules' in table_data:
                            has_schedules = True
                            break
                    if has_schedules:
                        break
        
        if not has_schedules:
            logger.debug("No schedules defined in YAML config, skipping schedule creation")
            return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}
            
        logger.info("Creating schedules for entities...")

        # Get updated entity IDs from the pipeline config
        try:
            response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
            logger.info(f"Retrieved the following entities: {response}")

            if not isinstance(response, list) or not response:
                logger.warning(f"Unexpected response when fetching entities: {response}")
                return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}

            # Process entities in the format used in main.py
            entities_map = {}
            for item in response:
                if 'entity' in item and isinstance(item['entity'], dict):
                    entity = item['entity']
                    if 'entityId' in entity and 'entityName' in entity:
                        entity_id = entity['entityId']
                        entity_name = entity['entityName']
                        # Extract table name from entity name (typically schema.table)
                        parts = entity_name.split('.')
                        table_name = parts[-1] if len(parts) > 1 else entity_name
                        entities_map[table_name] = entity_id
                        logger.info(f"Found entity: {entity_name} (ID: {entity_id})")

            # Create schedules for each entity found in the YAML config
            if yaml_config:
                # Track which tables we've already processed to avoid duplicates
                processed_tables = set()

                # Determine if schemas are at root level or under 'schemas' key
                schemas_dict = yaml_config.get('schemas', {})

                # If 'schemas' key doesn't exist or is empty, assume schemas are at root level
                if not schemas_dict:
                    # Treat each top-level key as a schema name
                    # Filter out keys that are not dictionaries (they wouldn't be schema configs)
                    schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
                    logger.info(f"Using root-level schema definitions: {list(schemas_dict.keys())}")

                for schema_name, schema_config in schemas_dict.items():
                    if 'tables' in schema_config and 'custom' in schema_config['tables']:
                        custom_tables = schema_config['tables']['custom']
                        for table_key, table_data in custom_tables.items():
                            # Skip if we've already processed this table
                            if table_key in processed_tables:
                                continue

                            processed_tables.add(table_key)

                            # Use the table_key directly instead of looking for 'name' field
                            table_name = table_key

                            if table_name in entities_map and 'schedules' in table_data:
                                entity_id = entities_map[table_name]
                                logger.info(f"Creating schedules for table {table_name} (Entity ID: {entity_id})")
                                create_entity_schedules(token, pipeline_id, entity_id, table_name,
                                                        table_data['schedules'])
                            else:
                                logger.warning(
                                    f"Unable to create schedules for {table_name}. Entity not found or no schedules defined.")

            # Create pipeline-level schedules if defined
            if yaml_config:
                # Reuse the same schemas_dict from entity schedules
                if not 'schemas_dict' in locals():
                    # Determine if schemas are at root level or under 'schemas' key
                    schemas_dict = yaml_config.get('schemas', {})

                    # If 'schemas' key doesn't exist or is empty, assume schemas are at root level
                    if not schemas_dict:
                        # Treat each top-level key as a schema name
                        schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
                        logger.info(
                            f"Using root-level schema definitions for pipeline schedules: {list(schemas_dict.keys())}")

                for schema_name, schema_config in schemas_dict.items():
                    if 'schedules' in schema_config:
                        logger.info(f"Creating pipeline-level schedules for schema {schema_name}")
                        create_pipeline_schedules(token, pipeline_id, schema_config['schedules'])

        except Exception as e:
            logger.error(f"Error creating schedules: {str(e)}")
            # Don't fail the whole process just because scheduling failed

    return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}


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
        create_entities(
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

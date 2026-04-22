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
import urllib3
import argparse
import copy
import itertools
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, create_entity_schedules, map_data_type, create_pipeline_schedules, create_group_schedules, load_yaml_config, \
    process_filter_clauses, create_group, assign_entities_to_group, get_table_id
from create_all_tables import handle_table_creation
from create_user_defined_functions import handle_udf_function_definition
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete, exit_on_fail
from utils.core_hub_client import CoreHubClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Metadata-only fields used in the export format {source: ..., target: ..., type: ..., ...}
# These are never column names in the explicit export format.
_EXPORT_METADATA_FIELDS = {"source", "target", "type", "dataLength", "numericPrecision", "numericScale", "isNullable", "id", "ordinalPosition"}


def _is_column_mappings(columns_list):
    """
    Return True if the columns list contains column mappings (not column definitions).

    Detects both:
    - Export format:  [{source: "col", target: "COL", type: ..., ...}]
    - User format:    [{col_name: "target_name"}, ...]  e.g. [{id: "ID"}]

    Column *definitions* (not mappings) have a 'name' key.
    """
    if not columns_list:
        return False
    first = columns_list[0]
    if not isinstance(first, dict):
        return False
    # Export format: has explicit 'source' key
    if "source" in first:
        return True
    # User format: no 'name' key (column definitions always have 'name')
    return "name" not in first


def _is_column_whitelist(columns_list):
    """
    Return True if the columns list contains column definitions (whitelist format).

    This format is used by parse_dbmoto_metadata_xml.py and represents
    an explicit whitelist of columns to include (not mappings).

    Detects:
    - Whitelist format: [{name: "col", type: "VARCHAR", sourceName: "COL"}, ...]

    Returns True when entries have 'name' and 'type' keys (column definitions).
    """
    if not columns_list:
        return False
    first = columns_list[0]
    if not isinstance(first, dict):
        return False
    # Whitelist format: has both 'name' and 'type' keys
    return "name" in first and "type" in first


def _extract_mapping_pairs(column_entry):
    """
    Extract (source_name, target_name) pairs from a column entry dict.

    Supports two formats:

    1. Explicit export format (from export_template_from_corehub.py):
       {source: "col", target: "COL", type: ..., id: ..., ordinalPosition: ...}
       -> returns [("col", "COL")]

    2. Legacy user format (hand-written YAML):
       {col_name: "target_name"}  e.g. {id: "ID", firstName: "first_name"}
       -> returns [("col_name", "target_name")]

    Returns:
        List of (source_name, target_name) tuples
    """
    if not isinstance(column_entry, dict):
        return []
    # Detect explicit export format by presence of 'source' key
    if "source" in column_entry:
        src = column_entry.get("source")
        tgt = column_entry.get("target", src)
        if src is not None:
            return [(src, tgt)]
        return []
    # Legacy user format: every key that is not a known metadata field is a mapping
    # In this format metadata fields (type, id, etc.) are never present, so this
    # simply returns all key-value pairs.
    return list(column_entry.items())


# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENTITY_START_TIMEOUT = float(os.getenv('ENTITY_START_TIMEOUT', '1'))
CREATE_TABLE_IF_NOT_EXISTS = os.getenv('CREATE_TABLE_IF_NOT_EXISTS', 'true').lower() == 'true'
DEFAULT_MAX_LOGICAL_PARTITIONS = int(os.getenv('MAX_LOGICAL_PARTITIONS', '10'))


def set_create_table_if_not_exists(enabled: bool) -> None:
    """Allow callers to toggle automatic table creation at runtime."""

    global CREATE_TABLE_IF_NOT_EXISTS
    CREATE_TABLE_IF_NOT_EXISTS = bool(enabled)


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

def create_entities(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id, source_type, target_type, yaml_config, skip_errors=True, chunk_size=50, source_agent_tag=None, target_agent_tag=None):
    # Debug logging to see what source_type and target_type values are received
    logger.debug(f"create_entities called with: source_type='{source_type}', target_type='{target_type}'")
    logger.debug(f"source_type type: {type(source_type)}, target_type type: {type(target_type)}")
    
    # Log CREATE_TABLE_IF_NOT_EXISTS status for debugging
    if CREATE_TABLE_IF_NOT_EXISTS:
        logger.info("CREATE_TABLE_IF_NOT_EXISTS is enabled - will create missing tables during entity discovery")
    else:
        logger.info("CREATE_TABLE_IF_NOT_EXISTS is disabled - assuming all tables exist")
        
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
                # Process groupId
                if 'groupId' in table_data and table_data['groupId'] != '_default':
                    group_name = str(table_data['groupId']).strip()
                    if group_name:  # Only add non-empty group names
                        group_names.add(group_name)
                        logger.debug(f"Found group '{group_name}' for table {schema_name}.{table_key}")
                    else:
                        logger.warning(f"Empty group ID found for table {schema_name}.{table_key}")

                # Collect chainId information (independent of groupId)
                if 'chainId' in table_data:
                    chain_id = table_data['chainId']
                    chain_ids.add(chain_id)

                    if chain_id not in chained_tables:
                        chained_tables[chain_id] = []
                    chained_tables[chain_id].append((table_key, table_data))

            # Note: Do NOT add group names from group-level schedules to group_names here,
            # because keys might be group IDs and we must not try to create groups using IDs as names.
            # We'll resolve schedule keys to IDs later, during schedule creation.

    # Create groups before creating entities
    groups_config = {}
    if yaml_config:
        raw_groups = yaml_config.get('groups')
        if isinstance(raw_groups, dict):
            groups_config = raw_groups

    groupId_map = {}
    logger.debug(f"Groups to create: {group_names}")
    
    for group_name in group_names:
        logger.debug(f"Processing group: '{group_name}' (type: {type(group_name)})")

        group_meta = groups_config.get(group_name) if isinstance(groups_config, dict) else None
        description = None
        color = None
        if isinstance(group_meta, dict):
            description = group_meta.get('description')
            color = group_meta.get('color')

        group_id = create_group(token, pipeline_id, group_name, description=description, color=color)
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

    logger.info("Source Node Info:")
    logger.info(json.dumps(source_node_info, indent=2))
    logger.info("Target Node Info:")
    logger.info(json.dumps(target_node_info, indent=2))

    logger.debug(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")

    schemas_root = yaml_config.get('schemas', yaml_config)
    schema_config = schemas_root.get(source_schema, {})
    logger.debug(f"Schema config for {source_schema}: {json.dumps(schema_config, indent=2)}")

    yaml_target_schema = schema_config.get('target', target_schema)
    whitelist = schema_config.get('tables', {}).get('whitelist', [])
    blacklist = schema_config.get('tables', {}).get('blacklist', [])
    # Handle empty custom tables attribute - convert None to empty dict
    tables_config = schema_config.get('tables', {})
    custom_tables = tables_config.get('custom', {})
    if custom_tables is None:
        custom_tables = {}
        logger.warning("Warning: 'custom' attribute is present but empty in YAML. Converting to empty dict.")

    # Get schema-level custom properties
    schema_custom_properties = schema_config.get('customProperties', {})
    global_source_custom_properties = schema_custom_properties.get('source', {})
    global_target_custom_properties = schema_custom_properties.get('target', {})

    logger.debug(f"Target schema: {yaml_target_schema}")
    logger.debug(f"Whitelist: {whitelist}")
    logger.debug(f"Blacklist: {blacklist}")
    logger.debug(f"Custom tables: {custom_tables}")
    logger.debug(f"Global source custom properties: {global_source_custom_properties}")
    logger.debug(f"Global target custom properties: {global_target_custom_properties}")

    def build_table_lookup(table_list, default_schema_name):
        lookup = {}
        for tbl in table_list or []:
            if isinstance(tbl, dict):
                name = tbl.get("name")
                schema_name = tbl.get("schema") or default_schema_name
                table_info = tbl
            elif isinstance(tbl, str):
                name = tbl
                schema_name = default_schema_name
                table_info = {"name": name, "schema": schema_name}
            else:
                continue

            if not name or not schema_name:
                continue

            lookup[(schema_name.lower(), name.lower())] = table_info
        return lookup

    def find_discovered_table(lookup, schema_name, table_name):
        if not lookup or not schema_name or not table_name:
            return None
        return lookup.get((schema_name.lower(), table_name.lower()))

    def resolve_table_id(schema_name, table_name, lookup, table_role):
        """Try to reuse discovery IDs before falling back to generated IDs."""
        discovered_table = find_discovered_table(lookup, schema_name, table_name)
        if discovered_table:
            discovered_id = discovered_table.get('id')
            if discovered_id is not None:
                logger.debug(
                    f"Using discovered {table_role} table ID for {schema_name}.{table_name}: {discovered_id}"
                )
                return discovered_id

        generated_id = get_table_id(schema_name, table_name)
        logger.debug(
            f"Using generated {table_role} table ID for {schema_name}.{table_name}: {generated_id}"
        )
        return generated_id

    def parse_partition_config(partition_value):
        """Return (column_name, max_partitions) from YAML configuration."""

        column_name = None
        max_partitions = DEFAULT_MAX_LOGICAL_PARTITIONS

        if isinstance(partition_value, dict):
            column_name = partition_value.get('column') or partition_value.get('name')
            raw_max = partition_value.get('maxPartitionsNumber') or partition_value.get('maxPartitions')
            if raw_max is not None:
                try:
                    max_partitions = max(1, int(raw_max))
                except (TypeError, ValueError):
                    logger.warning(f"Invalid maxPartitionsNumber '{raw_max}'. Falling back to {DEFAULT_MAX_LOGICAL_PARTITIONS}.")
        elif isinstance(partition_value, str):
            column_name = partition_value
        elif partition_value is not None:
            logger.warning(f"Unsupported partitions configuration type: {type(partition_value)}. Expected string or dict.")

        return column_name, max_partitions

    def _format_partition_object(partition_obj):
        if not isinstance(partition_obj, dict):
            return None

        partition_id = partition_obj.get('id')
        try:
            partition_id = int(partition_id)
        except (TypeError, ValueError):
            pass

        if 'startValue' not in partition_obj or 'endValue' not in partition_obj:
            return None

        return {
            "id": partition_id,
            "startValue": str(partition_obj.get('startValue')),
            "endValue": str(partition_obj.get('endValue'))
        }

    def extract_computed_partitions(response):
        """Normalize compute-logical-partitions API responses into Gluesync partition lists."""

        partitions = []
        if not isinstance(response, dict):
            return partitions

        raw_partitions = response.get('partitions')
        if isinstance(raw_partitions, list):
            for entry in raw_partitions:
                partition_obj = None
                if isinstance(entry, dict):
                    if {'id', 'startValue', 'endValue'}.issubset(entry.keys()):
                        partition_obj = entry
                    elif 'partition' in entry and isinstance(entry['partition'], dict):
                        partition_obj = entry['partition']

                formatted = _format_partition_object(partition_obj)
                if formatted:
                    partitions.append(formatted)
        elif isinstance(raw_partitions, dict):
            for key in raw_partitions.keys():
                partition_obj = None
                if isinstance(key, dict):
                    partition_obj = key
                elif isinstance(key, str):
                    try:
                        partition_obj = json.loads(key)
                    except json.JSONDecodeError:
                        logger.debug(f"Unable to decode partition key: {key}")

                formatted = _format_partition_object(partition_obj)
                if formatted:
                    partitions.append(formatted)

        partitions.sort(key=lambda part: part.get('id') if isinstance(part.get('id'), int) else 0)
        return partitions

    def compute_logical_partitions(token, pipeline_id, entity_id, column_payload, max_partitions_number):
        """Invoke CoreHub to compute logical partitions for a given column."""

        endpoint = f"/pipelines/{pipeline_id}/config/entities/{entity_id}/compute-logical-partitions"
        safe_column_payload = json.loads(json.dumps(column_payload))
        body = {
            "maxPartitionsNumber": max(1, max_partitions_number),
            "column": safe_column_payload
        }

        logger.info(
            "Computing logical partitions for entity %s (column=%s, max=%s)",
            entity_id,
            safe_column_payload.get('name'),
            body["maxPartitionsNumber"]
        )

        response = fetch_core_hub(endpoint, method='POST', token=token, body=body)
        partitions = extract_computed_partitions(response)

        if not partitions:
            logger.warning(
                "Logical partition computation returned no partitions for entity %s (column=%s)",
                entity_id,
                safe_column_payload.get('name')
            )

        return partitions

    def apply_logical_partitions(token, pipeline_id, partition_requests, skip_errors):
        if not partition_requests:
            return

        try:
            pipeline_entities = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
        except Exception as exc:
            logger.error(f"Failed to fetch entities for logical partitions: {exc}")
            if not skip_errors:
                raise
            return

        entity_map = {}
        if isinstance(pipeline_entities, list):
            for item in pipeline_entities:
                entity_data = item.get('entity') if isinstance(item, dict) else None
                if not entity_data:
                    continue
                entity_name = entity_data.get('entityName')
                if entity_name:
                    entity_map[entity_name] = entity_data
        else:
            logger.warning("Unexpected response while fetching entities for logical partitions: %s", pipeline_entities)

        for request in partition_requests:
            entity_name = request["entity_name"]
            column_info = request["column"]
            max_partitions = request["max_partitions"]
            source_agent_id = request["source_agent_id"]

            entity_from_core = entity_map.get(entity_name)
            if not entity_from_core:
                logger.warning(f"Cannot compute logical partitions: entity '{entity_name}' not found in pipeline")
                if not skip_errors:
                    raise RuntimeError(f"Entity '{entity_name}' not found for logical partition computation")
                continue

            entity_id = entity_from_core.get('entityId')
            if not entity_id:
                logger.warning(f"Entity '{entity_name}' from pipeline listing has no entityId")
                if not skip_errors:
                    raise RuntimeError(f"Entity '{entity_name}' missing ID in pipeline response")
                continue

            try:
                partitions = compute_logical_partitions(token, pipeline_id, entity_id, column_info, max_partitions)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(f"Failed to compute logical partitions for {entity_name}: {exc}")
                if not skip_errors:
                    raise
                continue

            if not partitions:
                logger.warning(f"No partitions returned for {entity_name}. Skipping partitionSettings update.")
                if not skip_errors:
                    raise RuntimeError(f"Logical partitions computation returned empty result for {entity_name}")
                continue

            partition_settings = {
                "column": column_info,
                "partitions": partitions
            }

            entity_payload = json.loads(json.dumps(entity_from_core))
            source_entity = next(
                (agent for agent in entity_payload.get('agentEntities', [])
                 if agent.get('agentId') == source_agent_id or agent.get('entityType', {}).get('type') == 'Source'),
                None
            )

            if not source_entity:
                logger.warning(f"Source agent entity not found in payload for {entity_name}")
                if not skip_errors:
                    raise RuntimeError(f"Source agent entity missing for {entity_name}")
                continue

            source_entity_type = source_entity.setdefault('entityType', {})
            source_entity_type['partitionSettings'] = partition_settings

            update_body = {"entities": [entity_payload]}

            try:
                fetch_core_hub(
                    f"/pipelines/{pipeline_id}/config/entities",
                    method='PUT',
                    token=token,
                    body=update_body
                )
                logger.info(f"Applied logical partitions for entity {entity_name} (ID: {entity_id})")
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(f"Failed to update entity {entity_name} with logical partitions: {exc}")
                if not skip_errors:
                    raise

    def build_partition_settings(column_name, table_columns, schema_name, table_name, table_id):
        """Create PartitionSettings payload for the specified column."""

        if not column_name:
            return None

        if not table_columns:
            logger.warning(f"Partition column '{column_name}' requested for {schema_name}.{table_name} but no columns were discovered")
            return None

        normalized_column = str(column_name).strip()
        if not normalized_column:
            return None

        matched_column = next((col for col in table_columns if col.get('name') == normalized_column), None)
        if not matched_column:
            logger.warning(f"Partition column '{normalized_column}' not found in discovery for {schema_name}.{table_name}. Skipping partitionSettings.")
            return None

        # Use the id field from CoreHub API as the column ID
        column_id = matched_column.get('id')
        if column_id is None:
            error_msg = f"CRITICAL ERROR: Column '{normalized_column}' in table {schema_name}.{table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
            logger.error(error_msg)
            raise ValueError(error_msg)

        partition_column = {
            "id": column_id,
            "name": normalized_column,
            "tableId": int(table_id),
            "position": matched_column.get('position', 0),
            "dataType": matched_column.get('dataType'),
        }
        
        # Include optional fields from matched_column
        for field in ['charMaxLength', 'charOctetLength', 'charSet', 'collation', 'numPrec', 'numPrecRadix', 'numScale', 'datetimePrec', 'isIdentity', 'isNullable', 'udtCategory', 'udtName']:
            if field in matched_column:
                partition_column[field] = matched_column[field]
                
        partition_column["isPK"] = matched_column.get('isPK', False)
        if 'default' in matched_column:
            partition_column["default"] = matched_column['default']
            
        # Backward compatibility
        partition_column["table"] = {
            "id": str(table_id),
            "schema": schema_name,
            "name": table_name
        }
        partition_column["type"] = matched_column.get('dataType')

        partition_settings = {
            "column": partition_column,
            "partitions": []
        }

        logger.info(f"Configured partitionSettings for {schema_name}.{table_name} on column '{normalized_column}'")
        return partition_settings

    source_tables_lookup = build_table_lookup(tables, source_schema)

    target_tables_lookup = {}
    try:
        target_discovered_tables = get_agent_tables(token, pipeline_id, target_agent_id, yaml_target_schema)
        target_tables_lookup = build_table_lookup(target_discovered_tables, yaml_target_schema)
        logger.info(f"Discovered {len(target_tables_lookup)} tables in target schema {yaml_target_schema}")
    except Exception as e:
        logger.warning(f"Could not retrieve target tables for schema {yaml_target_schema}: {str(e)}")

    pending_partition_requests = []
    
    # Track which YAML keys have been processed to avoid duplicates
    processed_yaml_keys = set()

    for table in tables:
        if isinstance(table, str):
            table_name = table
            table_schema = None  # Unknown schema for string-only tables
        else:
            table_name = table.get("name")
            table_schema = table.get("schema")  # Get schema from table object if available

        if not table_name:
            logger.warning(f"Warning: Table without name encountered. Skipping.")
            continue

        # Validate that the table belongs to the requested source schema
        # The API may return tables from all schemas, so we need to filter
        if table_schema and table_schema.lower() != source_schema.lower():
            logger.debug(f"Skipping table {table_name}: belongs to schema '{table_schema}' but processing schema '{source_schema}'")
            continue

        # Skip system tables
        if table_name.startswith("sys"):
            logger.info(f"Skipping system table: {table_name}")
            continue

        # Skip blacklisted tables
        if blacklist and table_name in blacklist:
            logger.info(f"Skipping blacklisted table: {table_name}")
            continue

        # STRICT WHITELIST ENFORCEMENT:
        # If whitelist is defined and not empty, ONLY process tables in the whitelist
        if whitelist:
            if table_name not in whitelist:
                logger.info(f"Skipping table {table_name}: not in whitelist for schema {source_schema}")
                continue
            else:
                logger.debug(f"Table {table_name} is in whitelist for schema {source_schema} - processing")
        else:
            logger.debug(f"No whitelist defined for schema {source_schema}, allowing table {table_name}")

        columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)
        
        # Debug logging for column discovery
        if columns and columns.get('columns'):
            logger.info(f"Discovered {len(columns['columns'])} columns for table {table_name}: {[col.get('name') for col in columns['columns']]}")
        else:
            logger.warning(f"No columns discovered for table {table_name}. columns={columns}")
        
        # Validate that columns were discovered
        if not columns or not columns.get('columns') or len(columns.get('columns', [])) == 0:
            error_msg = f"Cannot create entity for table {table_name}: No columns discovered from source. Please verify the table exists and is accessible."
            logger.error(error_msg)
            if not skip_errors:
                raise ValueError(error_msg)
            continue

        # Find custom config - handle both direct key match and sourceTableName match
        custom_config = custom_tables.get(table_name, {})
        yaml_table_key = table_name  # Track which YAML key we're using
        
        # If no direct match, check if any custom config has sourceTableName matching this table
        if not custom_config:
            for key, config in custom_tables.items():
                if isinstance(config, dict) and config.get('sourceTableName') == table_name:
                    custom_config = config
                    yaml_table_key = key
                    logger.info(f"Found custom config for table {table_name} under YAML key '{key}' (duplicate table)")
                    break
        
        if custom_config is None:
            custom_config = {}
            logger.warning(f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
        logger.debug(f"Custom config for {table_name}: {custom_config}")
        
        # Mark this YAML key as processed
        processed_yaml_keys.add(yaml_table_key)

        # Get snapshot write method configuration (UPSERT or INSERT, default is UPSERT)
        snapshot_write_method = custom_config.get('snapshotWriteMethod', 'UPSERT').upper()
        if snapshot_write_method not in ['UPSERT', 'INSERT']:
            logger.warning(f"Warning: Invalid snapshotWriteMethod '{snapshot_write_method}' for {table_name}. Using default 'UPSERT'")
        logger.info(f"Snapshot write method for {table_name}: {snapshot_write_method}")
        
        # Get table-specific custom properties and merge with global properties
        table_custom_properties = custom_config.get('customProperties', {})
        source_custom_properties = {**global_source_custom_properties, **table_custom_properties.get('source', {})}
        target_custom_properties = {**global_target_custom_properties, **table_custom_properties.get('target', {})}

        partition_config = source_custom_properties.pop('partitions', None)
        partition_column_name, partition_max_partitions = parse_partition_config(partition_config)
        
        # Store UDF configuration separately (will be added to entityType, not customProperties)
        udf_config = target_custom_properties.pop('udf', None)
        
        # Check if this table is configured for unlocked schema
        is_unlocked_schema = custom_config.get('unlockedSchema', False)
        
        # Validate: UDF is mandatory for unlocked schema
        if is_unlocked_schema and not udf_config:
            logger.error(f"Table {table_name} is configured with unlockedSchema=true but no UDF is defined. UDF is mandatory for unlocked schema.")
            if not skip_errors:
                raise ValueError(f"UDF is mandatory for table {table_name} with unlocked schema")
            continue
        
        # Note: snapshotWriteMethod is kept separately and NOT added to custom properties
        # It will be read directly from YAML config when needed during sync operations

        logger.debug(f"Source custom properties for {table_name}: {source_custom_properties}")
        logger.debug(f"Target custom properties for {table_name}: {target_custom_properties}")

        # Get custom target table name if specified
        target_table_name = custom_config.get('name', table_name)
        logger.info(f"Using target table name: {target_table_name} for source table: {table_name}")

        # Get and process filter configurations
        filter_config = custom_config.get('filter')
        snapshot_delete_filter_config = custom_config.get('snapshotDeleteFilter')
        
        # Process regular filter
        processed_filters = process_filter_clauses(filter_config, columns) if filter_config else None
        logger.debug(f"Processed filters for {table_name}: {processed_filters}")
        
        # Process snapshot delete filter
        processed_snapshot_delete_filters = process_filter_clauses(snapshot_delete_filter_config, columns) if snapshot_delete_filter_config else None
        if processed_snapshot_delete_filters:
            logger.debug(f"Processed snapshot delete filters for {table_name}: {processed_snapshot_delete_filters}")

        # Get document key configuration if it exists
        document_key = None
        if custom_config and 'documentKey' in custom_config:
            doc_key_config = custom_config['documentKey']

            # Convert column names to column IDs
            key_ids = []
            if 'keys' in doc_key_config:
                for key_name in doc_key_config['keys']:
                    # Find the column ID from the columns list
                    column_id = None
                    for col in columns["columns"]:
                        if col.get('name') == key_name:
                            # Use the id field from CoreHub API as the column ID
                            column_id = col.get('id')
                            if column_id is None:
                                error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                                logger.error(error_msg)
                                raise ValueError(error_msg)
                            break

                    if column_id is not None:
                        key_ids.append(column_id)
                    else:
                        logger.warning(f"Warning: Column '{key_name}' not found in table columns for document key")

            document_key = {
                "prefix": doc_key_config.get('prefix', ''),
                "suffix": doc_key_config.get('suffix', ''),
                "separator": doc_key_config.get('separator', '-'),
                "keys": key_ids  # Use column IDs instead of names
            }
            logger.debug(f"Document key configuration for {table_name}: {document_key}")

        # Build columns definition with IDs
        columns_def = []
        
        has_column_mappings = _is_column_mappings(custom_config.get('columns', []))
        is_column_whitelist = _is_column_whitelist(custom_config.get('columns', []))
        
        if has_column_mappings:
            # Custom column mappings (source→target)
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the column ID
                col_id = col.get('id')
                if col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                for column_map in custom_config.get('columns', []):
                    # Extract source:target pairs, ignoring metadata fields
                    mapping_pairs = _extract_mapping_pairs(column_map)
                    for source_name, target_name in mapping_pairs:
                        if source_name == col["name"]:
                            columns_def.append({
                                "id": col_id,  # Use actual ordinal position from database
                                "name": col["name"],
                                "alias": target_name,
                                "type": col.get("dataType"),
                                "isPrimaryKey": col.get("isPK", False)
                            })
                            break
        elif is_column_whitelist:
            # Column whitelist format: only use specified columns, not all discovered
            # Format: [{name: "col", type: "VARCHAR", sourceName: "SOURCE_COL"}, ...]
            yaml_columns = custom_config.get('columns', [])
            for yaml_col in yaml_columns:
                # sourceName is the source column name; name is the target column name
                source_col_name = yaml_col.get('sourceName') or yaml_col.get('name')
                target_col_name = yaml_col.get('name')
                
                # Find the matching column from discovered columns
                for col in columns["columns"]:
                    if col["name"] == source_col_name:
                        col_id = col.get('id')
                        if col_id is None:
                            error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response."
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                        
                        columns_def.append({
                            "id": col_id,
                            "name": source_col_name,
                            "alias": target_col_name,
                            "type": col["type"],
                        })
                        break
                else:
                    logger.warning(f"Column '{source_col_name}' from YAML whitelist not found in discovered columns for table {table_name}")
            
            logger.info(f"Using column whitelist for {table_name}: {len(columns_def)} of {len(yaml_columns)} specified columns matched")

        # If there are no column mappings or whitelist, fall back to
        # using discovery columns as-is (one-to-one source→target mapping).
        # IMPORTANT: Only fall back when NO filter was specified. If a whitelist
        # or mapping was explicitly specified but columns_def is empty, do NOT
        # fall back to all columns - that would silently include unwanted columns.
        if not has_column_mappings and not is_column_whitelist:
            columns_def = []
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the column ID
                col_id = col.get('id')
                if col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                columns_def.append({
                    "id": col_id,  # Use actual ordinal position from database
                    "name": col["name"],
                    "alias": col["name"],
                    "type": col.get("dataType"),
                    "isPrimaryKey": col.get("isPK", False)
                })

        logger.debug(f"Columns definition for {table_name}: {columns_def}")

        # Process keys and other configurations as before...
        if custom_config and 'keys' in custom_config:
            logger.info(f"Processing keys for table {table_name}. Requested keys: {custom_config['keys']}")
            logger.info(f"Has columns section: {bool(custom_config.get('columns'))}")
            keys = []
            
            has_column_mappings = _is_column_mappings(custom_config.get('columns', []))
            
            if has_column_mappings:
                # Use column mappings for keys
                logger.info(f"Key extraction via column mappings for {table_name}. Keys to find: {custom_config['keys']}")
                for col in columns["columns"]:
                    # Use the id field from CoreHub API as the column ID
                    col_id = col.get('id')
                    if col_id is None:
                        error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                        
                    for column_map in custom_config['columns']:
                        # Extract source:target pairs, ignoring metadata fields
                        mapping_pairs = _extract_mapping_pairs(column_map)
                        for source_name, target_name in mapping_pairs:
                            name_match = col["name"] == source_name
                            in_keys = col["name"] in custom_config["keys"]
                            if name_match and in_keys:
                                keys.append({
                                    "id": col_id,  # Use actual ordinal position from database
                                    "name": col["name"],
                                    "alias": target_name,
                                    "type": col.get("dataType")
                                })
                                logger.info(f"Key matched: col={col['name']!r}, source_name={source_name!r}, target_name={target_name!r}")
                            elif col["name"] in custom_config["keys"]:
                                logger.warning(f"Key col={col['name']!r} is in keys list but source_name={source_name!r} did not match (name_match={name_match}, in_keys={in_keys})")
            else:
                # No column mappings, use keys directly from source columns
                logger.debug(f"Processing keys for {table_name}: {custom_config['keys']}")
                logger.debug(f"Available columns: {[col['name'] for col in columns['columns']]}")
                for key_name in custom_config['keys']:
                    # Find the column and its index (case-insensitive to handle cross-db scenarios like pgsql→oracle)
                    found = False
                    for col in columns["columns"]:
                        if col["name"].lower() == key_name.lower():
                            # Use the id field from CoreHub API as the column ID
                            col_id = col.get('id')
                            if col_id is None:
                                error_msg = f"CRITICAL ERROR: Column '{key_name}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                                logger.error(error_msg)
                                raise ValueError(error_msg)

                            keys.append({
                                "id": col_id,  # Use actual ordinal position from database
                                "name": col["name"],
                                "alias": col["name"],
                                "type": col.get("dataType")
                            })
                            found = True
                            logger.debug(f"Found key '{key_name}' in columns with id={col_id}")
                            break
                    if not found:
                        logger.warning(f"Warning: Key '{key_name}' not found in columns for table '{table_name}'")
            
            logger.info(f"Final keys array for {table_name}: {keys} (count: {len(keys)})")
        else:
            keys = []
            for col in columns["columns"]:
                if col.get("isPK"):
                    # Use the id field from CoreHub API as the column ID
                    col_id = col.get('id')
                    if col_id is None:
                        error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                        
                    keys.append({
                        "id": col_id,  # Use actual ordinal position from database
                        "name": col["name"],
                        "alias": col["name"],
                        "type": col.get("dataType")
                    })
            logger.debug(f"Using primary keys for {table_name}: {keys}")

        if not keys:
            logger.warning(f"Warning: No keys specified for {table_name}. Table will have no keys.")

        if CREATE_TABLE_IF_NOT_EXISTS:
            # Only create tables for RDBMS targets, skip for NoSQL
            # target_type is passed from the caller and indicates SQL or NoSQL
            is_nosql_target = target_type and target_type.upper() == 'NOSQL'
            
            if is_nosql_target:
                logger.info(f"Skipping table creation for {target_table_name} - target is NoSQL")
            else:
                logger.info(f"CREATE_TABLE_IF_NOT_EXISTS is enabled - creating table {target_table_name}")
                handle_table_creation(
                    pipeline_id,
                    target_table_name,
                    yaml_target_schema,
                    keys,
                    token,
                    columns,
                    custom_config,
                    source_node_info,
                    target_node_info,
                    source_agent_tag=source_agent_tag,
                    target_agent_tag=target_agent_tag,
                )

        # Generate table IDs for use in entities (prefer discovered IDs)
        source_table_id = None
        if isinstance(table, dict):
            source_table_id = table.get('id')
        if source_table_id is None:
            discovered_source_table = find_discovered_table(source_tables_lookup, source_schema, table_name)
            if discovered_source_table:
                source_table_id = discovered_source_table.get('id')

        if source_table_id is None:
            source_table_id = get_table_id(source_schema, table_name)
            logger.debug(f"Using generated source table ID for {source_schema}.{table_name}: {source_table_id}")
        else:
            logger.debug(f"Using discovered source table ID for {source_schema}.{table_name}: {source_table_id}")

        target_table_id = None
        discovered_target_table = find_discovered_table(target_tables_lookup, yaml_target_schema, target_table_name)
        if discovered_target_table:
            target_table_id = discovered_target_table.get('id')

        if target_table_id is None:
            target_table_id = get_table_id(yaml_target_schema, target_table_name)
            logger.debug(f"Using generated target table ID for {yaml_target_schema}.{target_table_name}: {target_table_id}")
        else:
            logger.debug(f"Using discovered target table ID for {yaml_target_schema}.{target_table_name}: {target_table_id}")

        # Create source and target table property keys
        source_table_key = f"{source_schema}.{table_name}"
        target_table_key = f"{yaml_target_schema}.{target_table_name}"

        partition_settings = None
        if partition_column_name:
            partition_settings = build_partition_settings(
                partition_column_name,
                columns.get("columns"),
                source_schema,
                table_name,
                source_table_id
            )

        source_entity_type = {**source_custom_properties, "type": "Source"}

        # Determine source entity type based on source_type
        # Check if source_type indicates NoSQL (case-insensitive)
        is_source_nosql = source_type and "nosql" in source_type.lower()
        source_entity_type_name = "NoSqlEntity" if is_source_nosql else "SingleTable"
        logger.debug(f"Source entity type determination: source_type='{source_type}', is_source_nosql={is_source_nosql}, selected type='{source_entity_type_name}'")

        # Build source table properties with optional whereClause
        source_table_properties = {}
        if custom_config and 'whereClause' in custom_config:
            where_clause = custom_config['whereClause']
            if where_clause:
                source_table_properties["whereClause"] = str(where_clause)
                logger.info(f"Added whereClause for {source_table_key}: {where_clause}")
        
        source_entity = {
            "type": source_entity_type_name,
            "entityType": source_entity_type,
            "agentId": source_agent_id,
            "entityObject": {
                "id": str(source_table_id),
                "scope": source_schema,
                "collection": table_name
            },
            "table": {
                "id": str(source_table_id),
                "name": table_name,
                "schema": source_schema
            },
            "columns": columns_def,
            "customProperties": source_custom_properties,
            "tablesProperties": {source_table_key: source_table_properties}
        }

        # Get allowed operations for the target entity
        allowed_operations = get_allowed_operations(target_custom_properties)

        # Keys explicitly handled - exclude them from the passthrough spread
        _target_handled_keys = {'allowedOperations', 'skipDeletion', 'snapshotWritingConcurrency',
                                'useBulkOperationsDuringCDC', 'useBulkOperationsWhileSnapshot', 'udf'}
        _target_extra = {k: v for k, v in target_custom_properties.items() if k not in _target_handled_keys}

        # Create target entity type with allowedOperations and bulk operations settings
        # Any unknown customProperties.target keys are spread in dynamically
        target_entity_type = {
            **_target_extra,
            "type": "Target",
            "allowedOperations": allowed_operations,
            "snapshotWritingConcurrency": target_custom_properties.get('snapshotWritingConcurrency', 1),
            "useBulkOperationsDuringCDC": target_custom_properties.get('useBulkOperationsDuringCDC', False),
            "useBulkOperationsWhileSnapshot": target_custom_properties.get('useBulkOperationsWhileSnapshot', False)
        }

        # Add filters if they exist
        if processed_filters:
            target_entity_type["filter"] = processed_filters
        if processed_snapshot_delete_filters:
            target_entity_type["snapshotDeleteFilter"] = processed_snapshot_delete_filters
        
        # Add UDFs to entityType if they exist
        # UDFs must be in entityType, not in customProperties
        if udf_config:
            target_entity_type["udf"] = udf_config
            # Also add mappingFunctionInfo for the first UDF
            target_entity_type["mappingFunctionInfo"] = udf_config[0]
        
        # Discover target columns first (needed for columnsMappingMatrix)
        target_discovered_columns = None
        target_discovered_columns_by_name = {}
        try:
            target_discovered_columns = get_table_columns(token, pipeline_id, target_agent_id, yaml_target_schema, target_table_name)
            if target_discovered_columns and isinstance(target_discovered_columns.get('columns'), list):
                target_discovered_columns_by_name = {
                    c.get('name').lower(): c
                    for c in target_discovered_columns['columns']
                    if c.get('name')
                }
                logger.debug(
                    f"Found {len(target_discovered_columns_by_name)} existing columns in target table {yaml_target_schema}.{target_table_name}"
                )
        except Exception as e:
            logger.debug(f"Could not get target columns for {yaml_target_schema}.{target_table_name}: {str(e)}")

        # Generate columnsMappingMatrix
        columns_mapping_matrix = []
        
        if is_unlocked_schema:
            # For unlocked schema, add single entry with column IDs as 0
            columns_mapping_matrix.append({
                "sourceTableObjectId": source_table_id,
                "targetTableObjectId": target_table_id,
                "sourceColumnId": 0,
                "targetColumnId": 0
            })
            
            # Add tablesWithUnlockedSchema array with target table ID
            target_entity_type["tablesWithUnlockedSchema"] = [target_table_id]
            target_entity_type["tablesWithUnlockedDataTypes"] = []
            
            logger.info(f"Table {table_name} configured with unlocked schema. Target table ID: {target_table_id}")
        else:
            # For locked schema, create mapping for each column
            # IMPORTANT: When a whitelist or mapping is specified, only include
            # columns from columns_def, NOT all discovered source columns.
            # Otherwise, GlueSync would flag all source columns for syncing.
            has_filter = has_column_mappings or is_column_whitelist
            
            # Build a lookup of discovered source columns by name (case-insensitive)
            source_columns_by_name = {
                c.get('name', '').lower(): c
                for c in columns.get('columns', [])
                if c.get('name')
            }
            
            max_target_col_id = 0
            
            if has_filter:
                # Only iterate over the filtered columns_def
                for col_def in columns_def:
                    source_col_name = col_def.get('name')
                    target_col_name = col_def.get('alias', source_col_name)
                    source_col_id = col_def.get('id')
                    
                    if source_col_id is None or not source_col_name:
                        logger.warning(f"Skipping mapping matrix entry with missing id/name: {col_def}")
                        continue
                    
                    # Find the target column ID from discovered target columns
                    target_col_id = source_col_id  # Default to source ID
                    target_col = target_discovered_columns_by_name.get(target_col_name.lower())
                    if target_col and target_col.get('id') is not None:
                        target_col_id = target_col.get('id')
                        logger.debug(f"Mapped column: {source_col_name} (ID={source_col_id}) -> {target_col_name} (ID={target_col_id})")
                    else:
                        logger.debug(f"No target column found for {target_col_name}, using source ID {source_col_id}")
                    
                    columns_mapping_matrix.append({
                        "sourceTableObjectId": source_table_id,
                        "targetTableObjectId": target_table_id,
                        "sourceColumnId": source_col_id,
                        "targetColumnId": target_col_id
                    })
                    max_target_col_id = max(max_target_col_id, target_col_id)
                
                logger.info(f"columnsMappingMatrix for {table_name}: using filtered columns ({len(columns_mapping_matrix)} entries from {len(columns.get('columns', []))} discovered)")
            else:
                # No filter defined - map all discovered source columns
                if 'columns' in columns and isinstance(columns['columns'], list):
                    for col in columns['columns']:
                        # Use the id field from CoreHub API as the column ID
                        source_col_id = col.get('id')
                        if source_col_id is None:
                            error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                        
                        source_col_name = col.get('name')
                        target_col_name = source_col_name
                        
                        # Find the target column ID from discovered target columns
                        target_col_id = source_col_id  # Default to source ID
                        target_col = target_discovered_columns_by_name.get(target_col_name.lower())
                        if target_col and target_col.get('id') is not None:
                            target_col_id = target_col.get('id')
                            logger.debug(f"Mapped column: {source_col_name} (ID={source_col_id}) -> {target_col_name} (ID={target_col_id})")
                        else:
                            logger.debug(f"No target column found for {target_col_name}, using source ID {source_col_id}")
                        
                        columns_mapping_matrix.append({
                            "sourceTableObjectId": source_table_id,
                            "targetTableObjectId": target_table_id,
                            "sourceColumnId": source_col_id,
                            "targetColumnId": target_col_id
                        })
                        max_target_col_id = max(max_target_col_id, target_col_id)
            
            # Add empty arrays for locked schema
            target_entity_type["tablesWithUnlockedSchema"] = []
            target_entity_type["tablesWithUnlockedDataTypes"] = []
            
            logger.info(f"Table {table_name} using locked schema with {len(columns_mapping_matrix)} column mappings")
        
        # Add mappings for target-only columns if they exist (sourceColumnId = 0)
        target_only_columns = custom_config.get('targetOnlyColumns', [])
        if target_only_columns and not is_unlocked_schema:
            pass

        # Add columnsMappingMatrix to entityType
        target_entity_type["columnsMappingMatrix"] = columns_mapping_matrix

        # Build target columns definition with IDs
        target_columns_def = []
        max_target_col_id = 0
        
        has_column_mappings_target = _is_column_mappings(custom_config.get('columns', []))
        is_column_whitelist_target = _is_column_whitelist(custom_config.get('columns', []))
        
        if has_column_mappings_target:
            # Custom column mappings for target
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the source column ID
                source_col_id = col.get('id')
                if source_col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                for column_map in custom_config.get('columns', []):
                    # Extract source:target pairs, ignoring metadata fields
                    mapping_pairs = _extract_mapping_pairs(column_map)
                    for source_name, target_name in mapping_pairs:
                        if source_name == col["name"]:
                            discovered_target_col = target_discovered_columns_by_name.get(target_name.lower())
                            resolved_target_type = None
                            target_col_id = source_col_id  # Default to source ID
                            
                            if discovered_target_col:
                                if discovered_target_col.get('dataType'):
                                    resolved_target_type = discovered_target_col.get('dataType')
                                if discovered_target_col.get('id') is not None:
                                    target_col_id = discovered_target_col.get('id')
                            else:
                                resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                                     source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                            max_target_col_id = max(max_target_col_id, target_col_id)
                            # Get isPrimaryKey from discovered target column or fall back to source column
                            target_is_primary_key = discovered_target_col.get("isPK") if discovered_target_col else col.get("isPK", False)
                            target_columns_def.append({
                                "id": target_col_id,  # Use target column ID
                                "name": target_name,
                                "alias": target_name,
                                "type": resolved_target_type,
                                "isPrimaryKey": target_is_primary_key
                            })
                            break
        elif is_column_whitelist_target:
            # Column whitelist format: only use specified columns
            yaml_columns = custom_config.get('columns', [])
            for yaml_col in yaml_columns:
                source_col_name = yaml_col.get('sourceName') or yaml_col.get('name')
                target_col_name = yaml_col.get('name')
                
                # Find the matching column from discovered source columns
                for col in columns["columns"]:
                    if col["name"] == source_col_name:
                        source_col_id = col.get('id')
                        if source_col_id is None:
                            error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response."
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                        
                        # Look up target column info
                        discovered_target_col = target_discovered_columns_by_name.get(target_col_name.lower())
                        resolved_target_type = yaml_col.get('type')  # Use type from YAML if specified
                        target_col_id = source_col_id  # Default to source ID
                        
                        if discovered_target_col:
                            if discovered_target_col.get('type') and not resolved_target_type:
                                resolved_target_type = discovered_target_col.get('type')
                            if discovered_target_col.get('id') is not None:
                                target_col_id = discovered_target_col.get('id')
                        
                        if not resolved_target_type:
                            resolved_target_type = map_data_type(col["type"], source_node_info, target_node_info,
                                                                 source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                        
                        max_target_col_id = max(max_target_col_id, target_col_id)
                        target_columns_def.append({
                            "id": target_col_id,
                            "name": target_col_name,
                            "alias": target_col_name,
                            "type": resolved_target_type
                        })
                        break
                else:
                    logger.warning(f"Column '{source_col_name}' from YAML whitelist not found in discovered columns for table {table_name}")
            
            logger.info(f"Using column whitelist for target {table_name}: {len(target_columns_def)} of {len(yaml_columns)} specified columns matched")
        
        if not has_column_mappings_target and not is_column_whitelist_target:
            # No column mappings - use columns as-is with mapped types
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the source column ID
                source_col_id = col.get('id')
                if source_col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                discovered_target_col = target_discovered_columns_by_name.get(col["name"].lower())
                resolved_target_type = None
                target_col_id = source_col_id  # Default to source ID
                
                if discovered_target_col:
                    if discovered_target_col.get('dataType'):
                        resolved_target_type = discovered_target_col.get('dataType')
                    if discovered_target_col.get('id') is not None:
                        target_col_id = discovered_target_col.get('id')
                else:
                    resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                         source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                max_target_col_id = max(max_target_col_id, target_col_id)
                
                # Use the target-discovered column name (preserves original case from target DB)
                # Fall back to source column name if target column not found
                target_col_name = discovered_target_col.get('name') if discovered_target_col else col["name"]
                # Get isPrimaryKey from discovered target column or fall back to source column
                target_is_primary_key = discovered_target_col.get("isPK") if discovered_target_col else col.get("isPK", False)
                target_columns_def.append({
                    "id": target_col_id,  # Use target column ID
                    "name": target_col_name,
                    "alias": target_col_name,
                    "type": resolved_target_type,
                    "isPrimaryKey": target_is_primary_key
                })
        
        # Add target-only columns if specified (only supported with unlocked schema)
        target_only_columns = custom_config.get('targetOnlyColumns', [])
        if target_only_columns:
            if not is_unlocked_schema:
                logger.warning(f"Target-only columns are specified for table {table_name} but unlockedSchema is not true. Skipping target-only columns.")
                logger.warning(f"To use target-only columns, set 'unlockedSchema: true' for table {table_name}")
            elif is_unlocked_schema:
                logger.info(f"Adding {len(target_only_columns)} target-only columns for table {table_name}")

                # Try to get target columns if the target table exists
                target_columns = None
                try:
                    target_columns = get_table_columns(token, pipeline_id, target_agent_id, yaml_target_schema, target_table_name)
                    logger.debug(f"Found {len(target_columns.get('columns', []))} existing columns in target table {yaml_target_schema}.{target_table_name}")
                except Exception as e:
                    logger.debug(f"Could not get target columns for {yaml_target_schema}.{target_table_name}: {str(e)}")

                for target_col in target_only_columns:
                    # Increment from the maximum target column ID
                    max_target_col_id += 1

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
                        col_type = target_col.get('dataType', target_col.get('type', 'varchar'))  # Default to varchar if not specified
                        col_data_length = target_col.get('dataLength', 1024)  # Default data length
                        col_numeric_precision = target_col.get('numPrec', 0)  # Default numeric precision
                        col_numeric_scale = target_col.get('numScale', 0)  # Default numeric scale
                        col_is_nullable = target_col.get('isNullable', False)  # Default nullable

                    # Check if this column already exists in the target table
                    discovered_column = None
                    if target_columns and 'columns' in target_columns:
                        discovered_column = next(
                            (col for col in target_columns['columns'] if col.get('name') == col_name),
                            None
                        )

                    if discovered_column:
                        # Use discovered column properties (priority over user-defined)
                        logger.info(f"Using discovered properties for target-only column '{col_name}' from target table")
                        col_type = discovered_column.get('dataType', discovered_column.get('type', col_type))
                        col_data_length = discovered_column.get('charMaxLength', col_data_length)
                        col_numeric_precision = discovered_column.get('numPrec', col_numeric_precision)
                        col_numeric_scale = discovered_column.get('numScale', col_numeric_scale)
                        col_is_nullable = discovered_column.get('isNullable', col_is_nullable)
                    else:
                        # Column doesn't exist in target table, use user-defined properties
                        logger.debug(f"Using user-defined properties for target-only column '{col_name}' (not found in target table)")

                        # Validate that required properties are provided for user-defined target-only columns
                        if isinstance(target_col, dict):
                            required_props = ['name', 'type', 'dataLength', 'numericPrecision', 'numericScale', 'isNullable']
                            missing_props = [prop for prop in required_props if prop not in target_col or target_col.get(prop) is None]
                            if missing_props:
                                error_msg = f"Target-only column '{col_name}' is missing required properties: {missing_props}. All properties (name, type, dataLength, numericPrecision, numericScale, isNullable) must be specified when the column doesn't exist in the target table."
                                logger.error(error_msg)
                                if not skip_errors:
                                    raise ValueError(error_msg)

                    # Map the column type to target node type
                    mapped_type = map_data_type(col_type, source_node_info, target_node_info,
                                               source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                    target_columns_def.append({
                        "id": max_target_col_id,  # Continue from last target column ID
                        "name": col_name,
                        "alias": col_name,
                        "type": mapped_type,
                        "dataLength": col_data_length,
                        "numericPrecision": col_numeric_precision,
                        "numericScale": col_numeric_scale,
                        "isNullable": col_is_nullable,
                        "isPrimaryKey": False  # Target-only columns are not primary keys
                    })
                    logger.debug(f"Added target-only column: {col_name} (type: {mapped_type}, id: {max_target_col_id})")

        print(f"Columns definition for {table_name}: {columns_def}")

        # Process target keys with proper IDs
        target_keys = []
        if custom_config and 'keys' in custom_config:
            has_column_mappings = _is_column_mappings(custom_config.get('columns', []))
            
            if has_column_mappings:
                # Use column mappings for keys - map source key names to target key names
                for col in columns["columns"]:
                    # Use the id field from CoreHub API as the column ID
                    source_col_id = col.get('id')
                    if source_col_id is None:
                        error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                        logger.error(error_msg)
                        raise ValueError(error_msg)

                    for column_map in custom_config.get('columns', []):
                        # Extract source:target pairs, ignoring metadata fields
                        mapping_pairs = _extract_mapping_pairs(column_map)
                        for source_name, target_name in mapping_pairs:
                            if source_name == col["name"] and source_name in custom_config.get('keys', []):
                                discovered_target_col = target_discovered_columns_by_name.get(target_name.lower())
                                resolved_target_type = None
                                target_col_id = source_col_id  # Default to source ID
                                
                                if discovered_target_col:
                                    if discovered_target_col.get('dataType'):
                                        resolved_target_type = discovered_target_col.get('dataType')
                                    if discovered_target_col.get('id') is not None:
                                        target_col_id = discovered_target_col.get('id')
                                else:
                                    resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                                         source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                                target_keys.append({
                                    "id": target_col_id,  # Use target column ID
                                    "name": target_name,
                                    "alias": target_name,
                                    "type": resolved_target_type
                                })
                                logger.debug(f"Target key: {source_name} -> {target_name} (source ID={source_col_id}, target ID={target_col_id})")
                                break
            else:
                # No column mappings, use keys directly from source columns
                for key_name in custom_config['keys']:
                    # Find the column and its ID (case-insensitive)
                    for col in columns["columns"]:
                        if col["name"].lower() == key_name.lower():
                            # Use the id field from CoreHub API as the column ID
                            source_col_id = col.get('id')
                            if source_col_id is None:
                                error_msg = f"CRITICAL ERROR: Column '{key_name}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                                logger.error(error_msg)
                                raise ValueError(error_msg)

                            discovered_target_col = target_discovered_columns_by_name.get(key_name.lower())
                            resolved_target_type = None
                            target_col_id = source_col_id  # Default to source ID
                            
                            if discovered_target_col:
                                if discovered_target_col.get('dataType'):
                                    resolved_target_type = discovered_target_col.get('dataType')
                                if discovered_target_col.get('id') is not None:
                                    target_col_id = discovered_target_col.get('id')
                            else:
                                resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                                     source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                            # Use the target-discovered column name (preserves original case from target DB)
                            # Fall back to source column name if target column not found
                            target_key_name = discovered_target_col.get('name') if discovered_target_col else col["name"]
                            target_keys.append({
                                "id": target_col_id,  # Use target column ID
                                "name": target_key_name,
                                "alias": target_key_name,
                                "type": resolved_target_type
                            })
                            logger.debug(f"Target key: {key_name} (source ID={source_col_id}, target ID={target_col_id})")
                            break
                    else:
                        logger.warning(f"Key '{key_name}' not found in columns for table '{table_name}'")
        else:
            target_keys = []
            for col in columns["columns"]:
                if col.get("isPK"):
                    # Use the id field from CoreHub API as the column ID
                    col_id = col.get('id')
                    if col_id is None:
                        error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                        
                    target_keys.append({
                        "id": col_id,  # Use actual ordinal position from database
                        "name": col["name"],
                        "alias": col["name"],
                        "type": target_discovered_columns_by_name.get(col["name"].lower(), {}).get('type') or map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                                                                                                    source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                    })
            logger.debug(f"Using primary target keys for {table_name}: {target_keys}")

        # Determine target entity type based on target_type
        # Check if target_type indicates NoSQL (case-insensitive)
        is_target_nosql = target_type and "nosql" in target_type.lower()
        target_entity_type_name = "NoSqlEntity" if is_target_nosql else "SingleTable"
        logger.debug(f"Target entity type determination: target_type='{target_type}', is_target_nosql={is_target_nosql}, selected type='{target_entity_type_name}'")

        target_entity = {
            "type": target_entity_type_name,
            "entityType": target_entity_type,
            "agentId": target_agent_id,
            "entityObject": {
                "id": str(target_table_id),
                "scope": yaml_target_schema,
                "collection": target_table_name
            },
            "table": {
                "id": str(target_table_id),
                "schema": yaml_target_schema,
                "name": target_table_name
            },
            "columns": target_columns_def,  # Use target columns definition
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
        # Use custom entityName from YAML if provided, otherwise fall back to schema.table format
        custom_entity_name = custom_config.get('entityName', f"{source_schema}.{table_name}")
        entity = {
            "entityName": custom_entity_name,
            "agentEntities": [source_entity, target_entity]
        }
        
        # Only add groupId to the entity if we have a valid group ID
        if group_id and group_id != '_default':
            entity["groupId"] = group_id
        
        entities.append(entity)

        if partition_settings:
            pending_partition_requests.append({
                "entity_name": entity["entityName"],
                "column": partition_settings["column"],
                "max_partitions": partition_max_partitions,
                "source_agent_id": source_agent_id
            })
        
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

    # Second loop: process duplicate table configurations with @@ that weren't processed in first loop
    logger.info(f"First loop completed. Processed {len(processed_yaml_keys)} YAML keys.")
    logger.info(f"Checking for unprocessed duplicate tables with @@ separator...")
    
    unprocessed_count = 0
    for yaml_table_key, custom_config in custom_tables.items():
        # Skip if already processed in first loop
        if yaml_table_key in processed_yaml_keys:
            continue
        
        # Skip if not a dict
        if not isinstance(custom_config, dict):
            logger.warning(f"Skipping {yaml_table_key}: not a dict")
            continue
        
        # Get the actual source table name (should have sourceTableName for @@ tables)
        table_name = custom_config.get('sourceTableName', yaml_table_key)
        
        # Skip system tables and blacklisted tables
        if table_name.startswith("sys") or (blacklist and table_name in blacklist):
            logger.info(f"Skipping duplicate table {yaml_table_key}: system or blacklisted")
            continue
        
        # Check whitelist using the actual table name
        if whitelist and table_name not in whitelist:
            logger.info(f"Skipping duplicate table {yaml_table_key}: not in whitelist")
            continue
        
        # Check if this is part of a chain - if so, skip (will be processed in chain loop)
        if custom_config.get('chainId'):
            logger.info(f"Skipping duplicate table {yaml_table_key}: part of chain {custom_config.get('chainId')}")
            processed_yaml_keys.add(yaml_table_key)
            continue
        
        unprocessed_count += 1
        logger.info(f"Processing duplicate table #{unprocessed_count}: {yaml_table_key} (source table: {table_name})")
        
        # Get columns using the actual source table name
        try:
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, table_name)
        except Exception as e:
            logger.error(f"Failed to get columns for duplicate table {yaml_table_key}: {str(e)}")
            if not skip_errors:
                raise
            continue
        
        # Process this duplicate table with the same logic as the first loop
        # (The rest of the entity creation logic is identical to the first loop)
        
        # Get snapshot write method configuration
        snapshot_write_method = custom_config.get('snapshotWriteMethod', 'UPSERT').upper()
        if snapshot_write_method not in ['UPSERT', 'INSERT']:
            logger.warning(f"Invalid snapshotWriteMethod '{snapshot_write_method}' for {yaml_table_key}. Using 'UPSERT'")
        logger.info(f"Snapshot write method for {yaml_table_key}: {snapshot_write_method}")
        
        # Get table-specific custom properties
        table_custom_properties = custom_config.get('customProperties', {})
        source_custom_properties = {**global_source_custom_properties, **table_custom_properties.get('source', {})}
        target_custom_properties = {**global_target_custom_properties, **table_custom_properties.get('target', {})}

        partition_config = source_custom_properties.pop('partitions', None)
        partition_column_name, partition_max_partitions = parse_partition_config(partition_config)
        
        udf_config = target_custom_properties.pop('udf', None)
        is_unlocked_schema = custom_config.get('unlockedSchema', False)
        
        if is_unlocked_schema and not udf_config:
            logger.error(f"Table {yaml_table_key} has unlockedSchema=true but no UDF. Skipping.")
            if not skip_errors:
                raise ValueError(f"UDF is mandatory for table {yaml_table_key} with unlocked schema")
            continue
        
        target_table_name = custom_config.get('name', table_name)
        logger.info(f"Using target table name: {target_table_name} for duplicate table {yaml_table_key}")

        # Get filter configurations
        filter_config = custom_config.get('filter')
        snapshot_delete_filter_config = custom_config.get('snapshotDeleteFilter')
        
        processed_filters = process_filter_clauses(filter_config, columns) if filter_config else None
        processed_snapshot_delete_filters = process_filter_clauses(snapshot_delete_filter_config, columns) if snapshot_delete_filter_config else None

        # Process document key if exists
        document_key = None
        if custom_config and 'documentKey' in custom_config:
            doc_key_config = custom_config['documentKey']
            key_ids = []
            if 'keys' in doc_key_config:
                for key_name in doc_key_config['keys']:
                    column_id = None
                    for col in columns["columns"]:
                        if col.get('name') == key_name:
                            column_id = col.get('id')
                            if column_id is None:
                                error_msg = f"Column '{key_name}' in {yaml_table_key} missing 'id' field"
                                logger.error(error_msg)
                                raise ValueError(error_msg)
                            break
                    if column_id is not None:
                        key_ids.append(column_id)
            
            document_key = {
                "prefix": doc_key_config.get('prefix', ''),
                "suffix": doc_key_config.get('suffix', ''),
                "separator": doc_key_config.get('separator', '-'),
                "keys": key_ids
            }

        # Build columns definition
        columns_def = []
        has_column_mappings = _is_column_mappings(custom_config.get('columns', []))
        
        if has_column_mappings:
            for col in columns["columns"]:
                col_id = col.get('id')
                if col_id is None:
                    raise ValueError(f"Column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                for column_map in custom_config.get('columns', []):
                    # Extract source:target pairs, ignoring metadata fields
                    mapping_pairs = _extract_mapping_pairs(column_map)
                    for source_name, target_name in mapping_pairs:
                        if source_name == col["name"]:
                            columns_def.append({
                                "id": col_id,
                                "name": col["name"],
                                "alias": target_name,
                                "type": col.get("dataType"),
                                "isPrimaryKey": col.get("isPK", False)
                            })
        
        if not has_column_mappings or not columns_def:
            for col in columns["columns"]:
                col_id = col.get('id')
                if col_id is None:
                    raise ValueError(f"Column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                columns_def.append({
                    "id": col_id,
                    "name": col["name"],
                    "alias": col["name"],
                    "type": col.get("dataType"),
                    "isPrimaryKey": col.get("isPK", False)
                })

        # Process keys
        if custom_config and 'keys' in custom_config:
            keys = []
            if has_column_mappings:
                for col in columns["columns"]:
                    col_id = col.get('id')
                    if col_id is None:
                        raise ValueError(f"Column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                    for column_map in custom_config['columns']:
                        # Extract source:target pairs, ignoring metadata fields
                        mapping_pairs = _extract_mapping_pairs(column_map)
                        for source_name, target_name in mapping_pairs:
                            if col["name"] == source_name and col["name"] in custom_config["keys"]:
                                keys.append({
                                    "id": col_id,
                                    "name": col["name"],
                                    "alias": target_name,
                                    "type": col.get("dataType")
                                })
            else:
                for key_name in custom_config['keys']:
                    for col in columns["columns"]:
                        if col["name"].lower() == key_name.lower():
                            col_id = col.get('id')
                            if col_id is None:
                                raise ValueError(f"Key '{key_name}' in {yaml_table_key} missing 'id' field")
                            keys.append({
                                "id": col_id,
                                "name": col["name"],
                                "alias": col["name"],
                                "type": col.get("dataType")
                            })
                            break
        else:
            keys = []
            for col in columns["columns"]:
                if col.get("isPK"):
                    col_id = col.get('id')
                    if col_id is None:
                        raise ValueError(f"Primary key column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                    keys.append({
                        "id": col_id,
                        "name": col["name"],
                        "alias": col["name"],
                        "type": col.get("dataType")
                    })

        if not keys:
            logger.warning(f"No keys specified for duplicate table {yaml_table_key}")

        # Generate table IDs
        source_table_id = None
        if isinstance(table, dict):
            source_table_id = table.get('id')
        if source_table_id is None:
            discovered_source_table = find_discovered_table(source_tables_lookup, source_schema, table_name)
            if discovered_source_table:
                source_table_id = discovered_source_table.get('id')
        if source_table_id is None:
            source_table_id = get_table_id(source_schema, table_name)

        target_table_id = None
        discovered_target_table = find_discovered_table(target_tables_lookup, yaml_target_schema, target_table_name)
        if discovered_target_table:
            target_table_id = discovered_target_table.get('id')
        if target_table_id is None:
            target_table_id = get_table_id(yaml_target_schema, target_table_name)

        source_table_key = f"{source_schema}.{table_name}"
        target_table_key = f"{yaml_target_schema}.{target_table_name}"

        partition_settings = None
        if partition_column_name:
            partition_settings = build_partition_settings(
                partition_column_name,
                columns.get("columns"),
                source_schema,
                table_name,
                source_table_id
            )

        source_entity_type = {**source_custom_properties, "type": "Source"}
        is_source_nosql = source_type and "nosql" in source_type.lower()
        source_entity_type_name = "NoSqlEntity" if is_source_nosql else "SingleTable"

        source_table_properties = {}
        if custom_config and 'whereClause' in custom_config:
            where_clause = custom_config['whereClause']
            if where_clause:
                source_table_properties["whereClause"] = str(where_clause)
        
        source_entity = {
            "type": source_entity_type_name,
            "entityType": source_entity_type,
            "agentId": source_agent_id,
            "entityObject": {
                "id": str(source_table_id),
                "scope": source_schema,
                "collection": table_name
            },
            "table": {
                "id": str(source_table_id),
                "name": table_name,
                "schema": source_schema
            },
            "columns": columns_def,
            "customProperties": source_custom_properties,
            "tablesProperties": {source_table_key: source_table_properties}
        }

        allowed_operations = get_allowed_operations(target_custom_properties)

        # Keys explicitly handled - exclude them from the passthrough spread
        _target_handled_keys = {'allowedOperations', 'skipDeletion', 'snapshotWritingConcurrency',
                                'useBulkOperationsDuringCDC', 'useBulkOperationsWhileSnapshot', 'udf'}
        _target_extra = {k: v for k, v in target_custom_properties.items() if k not in _target_handled_keys}

        # Any unknown customProperties.target keys are spread in dynamically
        target_entity_type = {
            **_target_extra,
            "type": "Target",
            "allowedOperations": allowed_operations,
            "snapshotWritingConcurrency": target_custom_properties.get('snapshotWritingConcurrency', 1),
            "useBulkOperationsDuringCDC": target_custom_properties.get('useBulkOperationsDuringCDC', False),
            "useBulkOperationsWhileSnapshot": target_custom_properties.get('useBulkOperationsWhileSnapshot', False)
        }

        if processed_filters:
            target_entity_type["filter"] = processed_filters
        if processed_snapshot_delete_filters:
            target_entity_type["snapshotDeleteFilter"] = processed_snapshot_delete_filters
        
        if udf_config:
            target_entity_type["udf"] = udf_config
            target_entity_type["mappingFunctionInfo"] = udf_config[0]
        
        # Generate columnsMappingMatrix
        columns_mapping_matrix = []
        
        if is_unlocked_schema:
            columns_mapping_matrix.append({
                "sourceTableObjectId": source_table_id,
                "targetTableObjectId": target_table_id,
                "sourceColumnId": 0,
                "targetColumnId": 0
            })
            target_entity_type["tablesWithUnlockedSchema"] = [target_table_id]
            target_entity_type["tablesWithUnlockedDataTypes"] = []
        else:
            if 'columns' in columns and isinstance(columns['columns'], list):
                for col in columns['columns']:
                    source_col_id = col.get('id')
                    if source_col_id is None:
                        raise ValueError(f"Column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                    target_col_id = source_col_id
                    columns_mapping_matrix.append({
                        "sourceTableObjectId": source_table_id,
                        "targetTableObjectId": target_table_id,
                        "sourceColumnId": source_col_id,
                        "targetColumnId": target_col_id
                    })
            target_entity_type["tablesWithUnlockedSchema"] = []
            target_entity_type["tablesWithUnlockedDataTypes"] = []

        target_entity_type["columnsMappingMatrix"] = columns_mapping_matrix

        # Build target columns (simplified - using source columns with type mapping)
        target_columns_def = []
        for col in columns["columns"]:
            col_id = col.get('id')
            if col_id is None:
                raise ValueError(f"Column '{col.get('name')}' in {yaml_table_key} missing 'id' field")
            
            resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                 source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
            target_columns_def.append({
                "id": col_id,
                "name": col["name"],
                "alias": col["name"],
                "type": resolved_target_type,
                "isPrimaryKey": col.get("isPK", False)
            })

        # Build target keys
        target_keys = []
        if custom_config and 'keys' in custom_config:
            for key_name in custom_config['keys']:
                for col in columns["columns"]:
                    if col["name"].lower() == key_name.lower():
                        col_id = col.get('id')
                        if col_id is None:
                            raise ValueError(f"Key '{key_name}' in {yaml_table_key} missing 'id' field")
                        
                        resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                             source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                        target_keys.append({
                            "id": col_id,
                            "name": col["name"],
                            "alias": col["name"],
                            "type": resolved_target_type
                        })
                        break
        else:
            for col in columns["columns"]:
                if col.get("isPK"):
                    col_id = col.get('id')
                    if col_id is None:
                        raise ValueError(f"Primary key '{col.get('name')}' in {yaml_table_key} missing 'id' field")
                    
                    resolved_target_type = map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                         source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                    target_keys.append({
                        "id": col_id,
                        "name": col["name"],
                        "alias": col["name"],
                        "type": resolved_target_type
                    })

        is_target_nosql = target_type and "nosql" in target_type.lower()
        target_entity_type_name = "NoSqlEntity" if is_target_nosql else "SingleTable"

        target_table_properties = {}
        if document_key:
            target_table_properties["documentKey"] = document_key

        target_entity = {
            "type": target_entity_type_name,
            "entityType": target_entity_type,
            "agentId": target_agent_id,
            "entityObject": {
                "id": str(target_table_id),
                "scope": yaml_target_schema,
                "collection": target_table_name
            },
            "table": {
                "id": str(target_table_id),
                "name": target_table_name,
                "schema": yaml_target_schema
            },
            "columns": target_columns_def,
            "customProperties": target_custom_properties,
            "tablesProperties": {target_table_key: target_table_properties}
        }

        # Get entity name from custom config
        entity_name = custom_config.get('entityName', f"{target_table_name} - {source_schema}.{table_name}")
        
        # Get group ID
        group_name = custom_config.get('group')
        group_id = None
        if group_name and groups_by_name:
            group_id = groups_by_name.get(group_name, {}).get('id')
            if not group_id:
                logger.warning(f"Group '{group_name}' not found for duplicate table {yaml_table_key}")

        entity = {
            "entityName": entity_name,
            "agentEntities": [source_entity, target_entity],
            "columnsMappingMatrix": columns_mapping_matrix
        }
        
        if group_id and group_id != '_default':
            entity["groupId"] = group_id
        
        entities.append(entity)
        logger.info(f"Created entity for duplicate table {yaml_table_key}: {entity_name}")

        if partition_settings:
            pending_partition_requests.append({
                "entity_name": entity["entityName"],
                "column": partition_settings["column"],
                "max_partitions": partition_max_partitions,
                "source_agent_id": source_agent_id
            })
        
        # Process UDFs
        table_udfs = target_custom_properties.get('udf', [])
        if table_udfs:
            logger.info(f"Found UDFs for duplicate table {yaml_table_key}: {table_udfs}")
            try:
                handle_udf_function_definition(table_name, pipeline_id, table_udfs, token)
                logger.info(f"Successfully processed UDFs for duplicate table {yaml_table_key}")
            except Exception as e:
                logger.error(f"Failed to process UDFs for duplicate table {yaml_table_key}: {str(e)}")
                if not skip_errors:
                    raise
        
        # Mark as processed
        processed_yaml_keys.add(yaml_table_key)
    
    logger.info(f"Second loop completed. Processed {unprocessed_count} duplicate tables with @@ separator.")
    logger.info(f"Total YAML keys processed: {len(processed_yaml_keys)}")

    # Track discovered table IDs for chain processing
    chain_source_ids = {}
    chain_target_ids = {}

    # Process MultiTable entities first
    multi_table_entities = []
    # For each chainId, create a MultiTable entity
    for chain_id, tables_list in chained_tables.items():
        if not tables_list:
            continue

        # FILTER: Only include tables that are in the whitelist for the current schema
        # This prevents tables from other schemas (collected globally) from being included
        if whitelist:
            filtered_tables_list = [(table_key, table_data) for table_key, table_data in tables_list if table_key in whitelist]
            if not filtered_tables_list:
                logger.info(f"Skipping chain '{chain_id}' - no tables in whitelist for schema '{source_schema}'")
                continue
            if len(filtered_tables_list) != len(tables_list):
                skipped_tables = [tk for tk, _ in tables_list if tk not in whitelist]
                logger.info(f"For chain '{chain_id}', filtered out tables not in whitelist for schema '{source_schema}': {skipped_tables}")
                logger.info(f"Remaining tables in chain: {[tk for tk, _ in filtered_tables_list]}")
            tables_list = filtered_tables_list

        # Tables are processed in the order they appear in the YAML file
        # Log table ordering for debugging
        logger.info(f"Creating MultiTable entity for chainId: {chain_id} with {len(tables_list)} tables in YAML order:")
        for idx, (table_key, _) in enumerate(tables_list):
            logger.info(f"  {idx+1}. Table {table_key}")


        # We'll reuse the entity name captured during export if available
        first_table_key, first_table_data = tables_list[0]
        entity_name = first_table_data.get("entityName") or f"{source_schema}.{first_table_key}"

        # Initialize tables, columns, and keys for the MultiTable entity
        multi_tables = []
        multi_columns = []
        multi_keys = []
        tables_properties = {}

        # Process each table in the chain (in YAML order)
        for table_key, table_data in tables_list:
            # Use sourceTableName if present (for duplicate tables with unique YAML keys)
            # Otherwise use table_key as the actual table name
            actual_table_name = table_data.get('sourceTableName', table_key) if table_data else table_key
            
            source_table_id = resolve_table_id(source_schema, actual_table_name, source_tables_lookup, "source")
            # Get columns for this table using the actual table name
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, actual_table_name)

            # Add table to the list (use actual table name, not YAML key)
            table_obj = {
                "id": str(source_table_id),
                "name": actual_table_name,
                "schema": source_schema
            }
            multi_tables.append(table_obj)
            chain_source_ids[table_key] = source_table_id

            # Add table to tables_properties with optional whereClause
            # Use actual table name for the key
            source_table_key = f"{source_schema}.{actual_table_name}"
            table_properties = {}
            if table_data and 'whereClause' in table_data:
                where_clause = table_data['whereClause']
                if where_clause:
                    table_properties["whereClause"] = str(where_clause)
                    logger.info(f"Added whereClause for {source_table_key}: {where_clause}")
            tables_properties[source_table_key] = table_properties

            # Process columns
            table_columns = []
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the column ID
                col_id = col.get('id')
                if col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                table_columns.append({
                    "id": col_id,
                    "name": col["name"],
                    "alias": col["name"],
                    "table": {
                        "id": str(source_table_id),
                        "name": table_key,
                        "schema": source_schema
                    },
                    "type": col.get("dataType"),
                    "isPrimaryKey": col.get("isPK", False)
                })

            # Add table header metadata followed by the columns for this table
            multi_columns.append({
                "id": str(source_table_id),
                "name": table_key,
                "schema": source_schema
            })
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
                        key_type = key_config.get('dataType')
                    else:
                        key_name = key_def
                        key_alias = key_def
                        key_type = None

                    # Try to find the key in the columns to get its type and ID if not specified (case-insensitive)
                    for col in columns["columns"]:
                        if col["name"].lower() == key_name.lower():
                            # Use the id field from CoreHub API as the column ID
                            col_id = col.get('id')
                            if col_id is None:
                                error_msg = f"CRITICAL ERROR: Column '{key_name}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                                logger.error(error_msg)
                                raise ValueError(error_msg)

                            keys.append({
                                "id": col_id,  # Use actual ordinal position from database
                                "name": col["name"],
                                "alias": key_alias if key_alias.lower() != key_name.lower() else col["name"],
                                "table": {
                                    "id": str(source_table_id),
                                    "name": table_key,
                                    "schema": source_schema
                                },
                                "type": key_type or col.get("dataType")
                            })
                            break
                    else:
                        logger.warning(f"Warning: Key {key_name} not found in columns for table {table_key}")
            else:
                keys = []
                for col in columns["columns"]:
                    if col.get("isPK"):
                        # Use the id field from CoreHub API as the column ID
                        col_id = col.get('id')
                        if col_id is None:
                            error_msg = f"CRITICAL ERROR: Primary key column '{col.get('name')}' in table {table_key} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                            logger.error(error_msg)
                            raise ValueError(error_msg)
                            
                        keys.append({
                            "id": col_id,  # Use actual ordinal position from database
                            "name": col["name"],
                            "alias": col["name"],
                            "table": {
                                "id": str(source_table_id),
                                "name": table_key,
                                "schema": source_schema
                            },
                            "type": col.get("dataType")
                        })

            # Add keys for this table
            multi_keys.append({
                "id": str(source_table_id),
                "name": table_key,
                "schema": source_schema
            })
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
            "customProperties": {},
            "tablesProperties": tables_properties,
            "tables": multi_tables,
            "columns": multi_columns
        }

        # Initialize target tables and properties
        target_tables = []
        target_tables_properties = {}
        target_columns = []
        target_keys = []

        # Process each table for the target (in YAML order)
        for table_key, table_data in tables_list:
            # Use sourceTableName if present (for duplicate tables with unique YAML keys)
            actual_table_name = table_data.get('sourceTableName', table_key) if table_data else table_key
            
            target_table_id = resolve_table_id(yaml_target_schema, table_key, target_tables_lookup, "target")
            # Add table to the list (use table_key for target, as it may have custom target name)
            target_table_obj = {
                "id": str(target_table_id),
                "name": table_key,
                "schema": target_schema
            }
            target_tables.append(target_table_obj)
            chain_target_ids[table_key] = target_table_id

            # Add table to tables_properties
            target_tables_properties[f"{target_schema}.{table_key}"] = {}

            # Get columns for this table using actual source table name
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, actual_table_name)

            # Process columns for target
            target_table_columns = []
            max_target_col_id = 0
            
            # Check if this table has column mappings or whitelist
            table_yaml_columns = table_data.get('columns', []) if table_data else []
            table_has_mappings = _is_column_mappings(table_yaml_columns)
            table_has_whitelist = _is_column_whitelist(table_yaml_columns)
            
            # Build lookup from source column name -> (target_name, yaml_type)
            source_to_target_mapping = {}
            if table_has_mappings:
                for column_map in table_yaml_columns:
                    mapping_pairs = _extract_mapping_pairs(column_map)
                    for source_name, target_name in mapping_pairs:
                        source_to_target_mapping[source_name.lower()] = {
                            'target_name': target_name,
                            'yaml_type': column_map.get('type') if isinstance(column_map, dict) else None
                        }
            elif table_has_whitelist:
                for yaml_col in table_yaml_columns:
                    source_name = yaml_col.get('sourceName') or yaml_col.get('name')
                    target_name = yaml_col.get('name')
                    if source_name:
                        source_to_target_mapping[source_name.lower()] = {
                            'target_name': target_name,
                            'yaml_type': yaml_col.get('type')
                        }
            
            has_filter = table_has_mappings or table_has_whitelist
            
            # First add source columns mapped to target with their actual ordinal positions
            for col in columns["columns"]:
                # Use the id field from CoreHub API as the column ID
                col_id = col.get('id')
                if col_id is None:
                    error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                    logger.error(error_msg)
                    raise ValueError(error_msg)
                
                # When filter is active, skip columns not in the whitelist/mapping
                mapping_info = source_to_target_mapping.get(col["name"].lower())
                if has_filter and not mapping_info:
                    logger.debug(f"Skipping column {col['name']} for MultiTable target: not in whitelist/mapping for table {table_key}")
                    continue
                
                # Use target name from mapping if available, otherwise source name
                target_name = mapping_info['target_name'] if mapping_info else col["name"]
                    
                max_target_col_id = max(max_target_col_id, col_id)
                target_table_columns.append({
                    "id": col_id,  # Use actual ordinal position from database
                    "name": target_name,
                    "type": map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                         source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag),
                    "isPrimaryKey": col.get("isPK", False)
                })

            # Add target-only columns for this table if specified
            table_custom_config = table_data.get('customProperties', {}).get('target', {}) if table_data else {}
            table_target_only_columns = table_custom_config.get('targetOnlyColumns', [])

            if table_target_only_columns:
                logger.info(f"Adding {len(table_target_only_columns)} target-only columns for table {table_key} in MultiTable entity")

                # Try to get target columns if the target table exists for this specific table
                table_target_columns = None
                try:
                    table_target_columns = get_table_columns(token, pipeline_id, target_agent_id, target_schema, table_key)
                    logger.debug(f"Found {len(table_target_columns.get('columns', []))} existing columns in target table {target_schema}.{table_key}")
                except Exception as e:
                    logger.debug(f"Could not get target columns for {target_schema}.{table_key}: {str(e)}")

                for target_col in table_target_only_columns:
                    # Increment from the maximum target column ID
                    max_target_col_id += 1

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
                        col_type = target_col.get('dataType', target_col.get('type', 'varchar'))  # Default to varchar if not specified
                        col_data_length = target_col.get('dataLength', 1024)  # Default data length
                        col_numeric_precision = target_col.get('numPrec', 0)  # Default numeric precision
                        col_numeric_scale = target_col.get('numScale', 0)  # Default numeric scale
                        col_is_nullable = target_col.get('isNullable', False)  # Default nullable

                    # Check if this column already exists in the target table
                    discovered_column = None
                    if table_target_columns and 'columns' in table_target_columns:
                        discovered_column = next(
                            (col for col in table_target_columns['columns'] if col.get('name') == col_name),
                            None
                        )

                    if discovered_column:
                        # Use discovered column properties (priority over user-defined)
                        logger.info(f"Using discovered properties for target-only column '{col_name}' from target table")
                        col_type = discovered_column.get('dataType', discovered_column.get('type', col_type))
                        col_data_length = discovered_column.get('charMaxLength', col_data_length)
                        col_numeric_precision = discovered_column.get('numPrec', col_numeric_precision)
                        col_numeric_scale = discovered_column.get('numScale', col_numeric_scale)
                        col_is_nullable = discovered_column.get('isNullable', col_is_nullable)
                    else:
                        # Column doesn't exist in target table, use user-defined properties
                        logger.debug(f"Using user-defined properties for target-only column '{col_name}' (not found in target table)")

                        # Validate that required properties are provided for user-defined target-only columns
                        if isinstance(target_col, dict):
                            required_props = ['name', 'type', 'dataLength', 'numericPrecision', 'numericScale', 'isNullable']
                            missing_props = [prop for prop in required_props if prop not in target_col or target_col.get(prop) is None]
                            if missing_props:
                                error_msg = f"Target-only column '{col_name}' in table '{table_key}' is missing required properties: {missing_props}. All properties (name, type, dataLength, numericPrecision, numericScale, isNullable) must be specified when the column doesn't exist in the target table."
                                logger.error(error_msg)
                                if not skip_errors:
                                    raise ValueError(error_msg)

                    # Map the column type to target node type
                    mapped_type = map_data_type(col_type, source_node_info, target_node_info,
                                               source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)

                    target_table_columns.append({
                        "id": max_target_col_id,  # Continue from last target column ID
                        "name": col_name,
                        "type": mapped_type,
                        "dataLength": col_data_length,
                        "numericPrecision": col_numeric_precision,
                        "numericScale": col_numeric_scale,
                        "isNullable": col_is_nullable,
                        "isPrimaryKey": False  # Target-only columns are not primary keys
                    })
                    logger.debug(f"Added target-only column to MultiTable: {col_name} (type: {mapped_type}, id: {max_target_col_id})")

            # Add columns for this table
            target_columns.append({
                "id": str(target_table_id),
                "name": table_key,
                "schema": target_schema
            })
            target_columns.append(target_table_columns)

            # Process keys for target with IDs
            custom_config = table_data
            if custom_config and 'keys' in custom_config:
                keys = []
                for key_def in custom_config['keys']:
                    if isinstance(key_def, dict):
                        key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
                    else:
                        key_name = key_def

                    # Try to find the key in the columns to get its type and ID (case-insensitive)
                    for col in columns["columns"]:
                        if col["name"].lower() == key_name.lower():
                            # Use the id field from CoreHub API as the column ID
                            col_id = col.get('id')
                            if col_id is None:
                                error_msg = f"CRITICAL ERROR: Column '{key_name}' in table {table_name} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                                logger.error(error_msg)
                                raise ValueError(error_msg)
                            
                            # Use target name from mapping if available, otherwise source name
                            key_mapping_info = source_to_target_mapping.get(col["name"].lower())
                            target_key_name = key_mapping_info['target_name'] if key_mapping_info else col["name"]
                                
                            keys.append({
                                "id": col_id,  # Use actual ordinal position from database
                                "name": target_key_name,
                                "type": map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                     source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                            })
                            break
                    else:
                        logger.warning(f"Warning: Key {key_name} not found in columns for table {table_key}")
            else:
                keys = []
                for col in columns["columns"]:
                    if col.get("isPK"):
                        # Use ordinalPosition from API if available
                        col_id = col.get('position', col.get('id'))
                        if col_id is None:
                            # Fallback to finding position if not provided
                            col_id = next((i for i, c in enumerate(columns["columns"], 1) if c == col), 1)
                            
                        keys.append({
                            "id": col_id,  # Use actual ordinal position from database
                            "name": col["name"],
                            "type": map_data_type(col.get("dataType"), source_node_info, target_node_info,
                                                 source_agent_tag=source_agent_tag, target_agent_tag=target_agent_tag)
                        })

            # Add keys for this table
            target_keys.append({
                "id": str(target_table_id),
                "name": table_key,
                "schema": target_schema
            })
            target_keys.append(keys)

        # Get allowed operations for the target entity
        allowed_operations = get_allowed_operations(table_data.get('customProperties', {}).get('target', {}))
        
        # Create table mapping matrix for MultiTable entities
        columns_mapping_matrix = []
        for table_key, table_data in tables_list:
            # Use sourceTableName if present (for duplicate tables with unique YAML keys)
            actual_table_name = table_data.get('sourceTableName', table_key) if table_data else table_key
            
            source_table_id = chain_source_ids.get(table_key) or resolve_table_id(
                source_schema, actual_table_name, source_tables_lookup, "source"
            )
            target_table_id = chain_target_ids.get(table_key) or resolve_table_id(
                yaml_target_schema, table_key, target_tables_lookup, "target"
            )
            logger.info(f"Table mapping for {table_key}: source_id={source_table_id}, target_id={target_table_id}")

            # Get columns for this table to create mappings for each column (use actual table name)
            columns = get_table_columns(token, pipeline_id, source_agent_id, source_schema, actual_table_name)
            if columns and 'columns' in columns:
                for col in columns['columns']:
                    # Use the id field from CoreHub API as the column ID
                    col_id = col.get('id')
                    if col_id is None:
                        error_msg = f"CRITICAL ERROR: Column '{col.get('name')}' in table {table_key} is missing 'id' field in CoreHub API response. This indicates a serious issue with the discovery API."
                        logger.error(error_msg)
                        raise ValueError(error_msg)
                    columns_mapping_matrix.append({
                        "sourceTableObjectId": source_table_id,
                        "targetTableObjectId": target_table_id,
                        "sourceColumnId": col_id,
                        "targetColumnId": col_id
                    })
        
        target_custom_properties = {
            "ttlValue": table_data.get('customProperties', {}).get('target', {}).get('ttlValue', 0)
        }

        post_snapshot_cmd = table_data.get('customProperties', {}).get('target', {}).get('postSnapshotCommand', {})
        if isinstance(post_snapshot_cmd, dict) and post_snapshot_cmd:
            target_custom_properties["postSnapshotCommand"] = post_snapshot_cmd

        pre_snapshot_cmd = table_data.get('customProperties', {}).get('target', {}).get('preSnapshotCommand', {})
        if isinstance(pre_snapshot_cmd, dict) and pre_snapshot_cmd:
            target_custom_properties["preSnapshotCommand"] = pre_snapshot_cmd

        # Create the target entity for MultiTable
        target_entity = {
            "type": "MultiTable",
            "entityId": "",
            "entityName": entity_name,
            "agentEntityId": "",
            "entityType": {
                "type": "Target",
                "allowedOperations": allowed_operations,
                "snapshotWritingConcurrency": table_data.get('customProperties', {}).get('target', {}).get('snapshotWritingConcurrency', 1),
                "columnsMappingMatrix": columns_mapping_matrix
            },
            "agentId": target_agent_id,
            "customProperties": target_custom_properties,
            "tablesProperties": target_tables_properties,
            "tables": target_tables,
            "columns": target_columns
        }

        # Get group info for multi-table entity
        group_id = groupId_map.get(table_data.get('groupId', '_default'), table_data.get('groupId', '_default'))
        group_name = next((name for name, gid in groupId_map.items() if gid == group_id), group_id)
        logger.info(f"Assigning multi-table entity '{entity_name}' to group: {group_name} (ID: {group_id})")
        
        # Create the MultiTable entity
        entity_payload = {
            "entityId": "",
            "entityName": entity_name,
            "agentEntities": [source_entity, target_entity],
            "columnsMappingMatrix": columns_mapping_matrix  # Also add at entity level
        }
        
        # Only add groupId if it's not the default
        if group_id and group_id != '_default':
            entity_payload["groupId"] = group_id
            
        multi_table_entity = {
            "entities": [entity_payload]
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
                for table_key, _ in tables_list:
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
                entity_name = multi_entity['entities'][0]['entityName']
                logger.info(f"Creating MultiTable entity {i+1}/{total_multi_tables}: {entity_name}")

                # Log keys for each agent entity before sending
                for idx, agent_entity in enumerate(multi_entity['entities'][0].get('agentEntities', [])):
                    agent_type = "source" if idx == 0 else "target"
                    keys = agent_entity.get('keys', [])
                    logger.info(f"  {agent_type} keys: {keys}")
                    if not keys:
                        logger.warning(f"  WARNING: No keys for {agent_type} in MultiTable {entity_name}")
                    
                    # Log the final tables ordering in payload before sending
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

            # Log keys for each entity before sending
            for entity_payload in chunk_data.get('entities', []):
                entity_name = entity_payload.get('entityName', 'unknown')
                logger.info(f"Entity: {entity_name}")
                for idx, agent_entity in enumerate(entity_payload.get('agentEntities', [])):
                    agent_type = "source" if idx == 0 else "target"
                    keys = agent_entity.get('keys', [])
                    logger.info(f"  {agent_type} keys: {keys}")
                    if not keys:
                        logger.warning(f"  WARNING: No keys for {agent_type} in {entity_name}")
                    
                    # Log the final tables ordering in payload before sending
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

    apply_logical_partitions(token, pipeline_id, pending_partition_requests, skip_errors)

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

    # Create schedules if successful
    if successful_entities > 0:
        # Check if any schedules are defined in the YAML config for the current source_schema
        has_schedules = False
        current_schema_config = None
        if yaml_config:
            schemas_dict = yaml_config.get('schemas', {})
            if not schemas_dict:
                # Check if top-level keys are schemas
                schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
                
            current_schema_config = schemas_dict.get(source_schema)
            if not current_schema_config:
                logger.debug(f"Schema '{source_schema}' not found in schemas_dict keys: {list(schemas_dict.keys())}")
                # As a fallback, try direct lookup
                current_schema_config = yaml_config.get(source_schema, {})

            if isinstance(current_schema_config, dict):
                # Pipeline-level schedules
                if 'schedules' in current_schema_config:
                    has_schedules = True
                # Group-level schedules
                if 'group_schedules' in current_schema_config:
                    group_scheds = current_schema_config['group_schedules']
                    if isinstance(group_scheds, dict):
                        if any(isinstance(v, list) and v for v in group_scheds.values()):
                            has_schedules = True
                    elif isinstance(group_scheds, list) and group_scheds:
                        has_schedules = True
                # Entity-level schedules
                if 'tables' in current_schema_config and 'custom' in current_schema_config['tables']:
                    for table_data in current_schema_config['tables']['custom'].values():
                        if 'schedules' in table_data:
                            has_schedules = True
                            break

        if not has_schedules:
            logger.debug("No schedules defined in YAML config, skipping schedule creation")
            return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}
            
        logger.info("Creating schedules for current schema...")

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

            # Create schedules for each entity found in the YAML config (only for current source_schema)
            if yaml_config and isinstance(current_schema_config, dict):
                # Track which tables we've already processed to avoid duplicates
                processed_tables = set()

                if 'tables' in current_schema_config and 'custom' in current_schema_config['tables']:
                    custom_tables = current_schema_config['tables']['custom']
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

            # Create group-level schedules if defined (only for current source_schema)
            if yaml_config and isinstance(current_schema_config, dict) and 'group_schedules' in current_schema_config:
                group_scheds_cfg = current_schema_config.get('group_schedules')
                if group_scheds_cfg:
                    logger.info("Creating group-level schedules...")
                    try:
                        known_ids = {gid for gid in groupId_map.values() if gid and gid != '_default'}

                        def _expand_group_ids(raw_ids):
                            if raw_ids is None:
                                return []
                            if isinstance(raw_ids, str):
                                candidates = [raw_ids]
                            else:
                                candidates = list(raw_ids)
                            resolved = []
                            for candidate in candidates:
                                candidate_str = str(candidate).strip()
                                if not candidate_str:
                                    continue
                                if candidate_str == '*':
                                    if known_ids:
                                        resolved.extend(list(known_ids))
                                    else:
                                        logger.warning("Wildcard group_ids requested but no groups were created in this run")
                                    continue
                                if candidate_str in groupId_map and groupId_map[candidate_str] and groupId_map[candidate_str] != '_default':
                                    resolved.append(groupId_map[candidate_str])
                                elif candidate_str in known_ids:
                                    resolved.append(candidate_str)
                                else:
                                    logger.warning(f"Skipping unknown group reference '{candidate_str}' in schedule configuration")
                            # Deduplicate while preserving order
                            seen = set()
                            ordered = []
                            for gid in resolved:
                                if gid not in seen:
                                    seen.add(gid)
                                    ordered.append(gid)
                            return ordered

                        base_schedule_list = []
                        if isinstance(group_scheds_cfg, dict):
                            for grp_key, schedules_cfg in group_scheds_cfg.items():
                                if not isinstance(schedules_cfg, list):
                                    logger.warning(
                                        f"Skipping group schedule block for '{grp_key}': expected a list of schedule objects"
                                    )
                                    continue
                                for schedule_cfg in schedules_cfg:
                                    if not isinstance(schedule_cfg, dict):
                                        continue
                                    schedule_copy = copy.deepcopy(schedule_cfg)
                                    schedule_copy['group_ids'] = [grp_key]
                                    base_schedule_list.append(schedule_copy)
                        elif isinstance(group_scheds_cfg, list):
                            for schedule_cfg in group_scheds_cfg:
                                if isinstance(schedule_cfg, dict):
                                    base_schedule_list.append(copy.deepcopy(schedule_cfg))

                        prepared_schedules = []
                        for schedule_cfg in base_schedule_list:
                            raw_ids = schedule_cfg.get('group_ids')
                            if not raw_ids and 'groups' in schedule_cfg:
                                raw_ids = schedule_cfg['groups']
                            resolved_ids = _expand_group_ids(raw_ids)
                            if not resolved_ids:
                                logger.warning("No valid group IDs resolved for schedule; skipping entry")
                                continue
                            schedule_copy = copy.deepcopy(schedule_cfg)
                            schedule_copy['group_ids'] = resolved_ids
                            schedule_copy.pop('groups', None)
                            prepared_schedules.append(schedule_copy)

                        if prepared_schedules:
                            create_group_schedules(token, pipeline_id, prepared_schedules)
                        else:
                            logger.debug("No valid group schedules to create after resolving targets")
                    except Exception as e:
                        logger.error(f"Error creating group-level schedules: {str(e)}")

            # Create pipeline-level schedules if defined
            if yaml_config and isinstance(current_schema_config, dict):
                if 'schedules' in current_schema_config:
                    logger.info(f"Creating pipeline-level schedules for schema {source_schema}")
                    create_pipeline_schedules(token, pipeline_id, current_schema_config['schedules'])

        except Exception as e:
            logger.error(f"Error creating schedules: {str(e)}")
            # Don't fail the whole process just because scheduling failed

    return {"successful": successful_entities, "failed": failed_entities, "total": total_entities}


def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=True,
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

        # Extract agent IDs with fallback for different field names
        source_agent_id = source_agent.get('agentId') or source_agent.get('id')
        target_agent_id = target_agent.get('agentId') or target_agent.get('id')
        
        if not source_agent_id or not target_agent_id:
            error_msg = f"Agents missing IDs. Source ID: {source_agent_id}, Target ID: {target_agent_id}. Source agent: {source_agent}, Target agent: {target_agent}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)
        
        logger.info(f"Using source agent ID: {source_agent_id}, target agent ID: {target_agent_id}")

        # Get tables from source agent
        tables = get_agent_tables(token, pipeline_id, source_agent_id, source_schema)

        if not tables:
            error_msg = f"No tables found in schema {source_schema}"
            log_failure(logger, error_msg)
            lockfile_failure()
            raise Exception(error_msg)

        # Debug logging before calling create_entities
        logger.debug(f"main() about to call create_entities with: source_type='{source_type}', target_type='{target_type}'")

        # Create entities
        result = create_entities(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent_id, target_agent_id,
            source_type, target_type, yaml_config, skip_errors, chunk_size,
            source_agent_tag=source_agent.get('agentTag'),
            target_agent_tag=target_agent.get('agentTag')
        )
        
        if result.get('skipped_discovery', False):
            logger.info("Entity discovery was skipped as tables will be created on demand")

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

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
from dataclasses import dataclass
import urllib.parse

import requests
import urllib3
import argparse
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete
from utils.core_hub_client import CoreHubClient
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, map_data_type, load_yaml_config, create_group, \
    process_filter_clauses, canonicalize_target_type, is_length_type
from entity_mapping import ColumnNames, EntityMappingError, find_by_name, is_column_mappings, yaml_relations
from pydantic import BaseModel, Field, PrivateAttr
from typing import Optional
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


def set_create_table_if_not_exists(enabled: bool) -> None:
    """Toggle automatic table creation at runtime, for the table existence check and the creation itself."""

    global CREATE_TABLE_IF_NOT_EXISTS
    CREATE_TABLE_IF_NOT_EXISTS = bool(enabled)


class TargetTableCreationError(Exception):
    """The target table of a table could not be created, so an entity saved for it would write to no table."""


class Column(BaseModel):
    id: int
    tableId: int
    name: str
    position: int
    dataType: str
    charMaxLength: Optional[int] = None
    charOctetLength: Optional[int] = None
    charSet: Optional[str] = None
    collationName: Optional[str] = Field(default=None, alias="collation")
    numPrec: Optional[int] = None
    numPrecRadix: Optional[int] = None
    numScale: Optional[int] = None
    datetimePrec: Optional[int] = None
    isPK: bool = False
    isIdentity: bool = False
    # Not sent: whether the YAML/discovery declared the length MAX (-1), which _normalize_data_length turns into
    # 1024; a fan-in merge reads it so VARCHAR(10) + VARCHAR(MAX) stays MAX.
    _declared_max: bool = PrivateAttr(default=False)
    isNullable: bool = False
    columnDefault: Optional[str] = Field(default=None, alias="default")
    udtCategory: Optional[str] = None
    udtName: Optional[str] = None

class GenerateTableStatementRequest(BaseModel):
    columns: List[Column]

class GenerateCreateTargetTableStatementRequest(BaseModel):
    columns: List[Column]

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
    # When CREATE_TABLE_IF_NOT_EXISTS is enabled, a 404 "not found" is expected
    # during table discovery and should not be logged as an error.
    expected_statuses = {404} if CREATE_TABLE_IF_NOT_EXISTS else None

    try:
        # URL-encode the table name to handle special characters
        encoded_table_name = urllib.parse.quote(table_name, safe='')
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities/schemas/{schema_name}/tables/{encoded_table_name}",
            method="GET",
            token=token,
            expected_error_statuses=expected_statuses
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
                token=token,
                expected_error_statuses=expected_statuses
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

def _add_custom_group_names(group_names: set, custom_tables) -> None:
    """Add the (non-default) groups the custom tables of a schema belong to."""
    for _, table_data in custom_tables.items():
        if 'groupId' in table_data and table_data['groupId'] != '_default':
            group_names.add(table_data['groupId'])


def _collect_group_names(yaml_config) -> set:
    """Collect all unique group names from the YAML configuration."""
    group_names = set()
    if not yaml_config:
        return group_names
    schemas_dict = yaml_config.get('schemas', {})
    if not schemas_dict:
        schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}

    for _, schema_config in schemas_dict.items():
        if 'tables' in schema_config and 'custom' in schema_config['tables']:
            _add_custom_group_names(group_names, schema_config['tables']['custom'])
    return group_names


def _create_groups(token, pipeline_id, group_names) -> None:
    """Create groups before creating entities."""
    for group_name in group_names:
        group_id = create_group(token, pipeline_id, group_name)
        if group_id != '_default':
            logger.info(f"Created/retrieved group '{group_name}' with ID: {group_id}")


@dataclass
class _TableCreationContext:
    """What the creation of every table of one source schema shares."""

    token: str
    pipeline_id: str
    source_schema: str
    source_agent_id: str
    yaml_target_schema: str
    whitelist: list
    blacklist: list
    custom_tables: dict
    global_source_custom_properties: dict
    global_target_custom_properties: dict
    source_node_info: dict
    target_node_info: dict


def _table_name_of(table):
    """Name of a table of the source agent (a plain name, or an object with a name)."""
    if isinstance(table, str):
        return table
    return table.get("name")


def _is_skipped_table(table_name: str, blacklist, whitelist) -> bool:
    """Whether a table is a system table, blacklisted or missing from the whitelist."""
    return bool(table_name.startswith("sys") or
                (blacklist and table_name in blacklist) or
                (whitelist and table_name not in whitelist))


def _discovered_pk_keys(columns) -> list:
    """The primary keys the source agent discovered, as key definitions."""
    return [
        {
            "name": col["name"],
            "alias": col["name"],
            "type": col.get("dataType")
        } for col in columns["columns"] if col.get("isPK")
    ]


def _parse_key_def(key_def):
    """``(name, alias, type)`` of a YAML key: a plain name, or a dict with a name and an optional alias."""
    if not isinstance(key_def, dict):
        # Simple string format - use the string as both name and alias
        return key_def, key_def, None
    # Handle the case where the key is specified as a dict with 'name' and optional 'alias'
    key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
    key_config = key_def.get(key_name, {}) if isinstance(key_def.get(key_name), dict) else {}

    # Get the key name (either from the dict key or from the 'name' field)
    key_name = key_name or key_config.get('name')
    # Get the alias (defaults to the key name if not specified)
    key_alias = key_config.get('name', key_name)

    # Get the key type from the config or find it in the columns
    key_type = key_config.get('dataType')
    return key_name, key_alias, key_type


def _custom_keys(table_name: str, key_defs, columns) -> list:
    """The keys the YAML declares for a table, typed from the source columns when not specified."""
    keys = []
    for key_def in key_defs:
        # Handle both string (key name) and dict (key with name/alias) formats
        key_name, key_alias, key_type = _parse_key_def(key_def)

        # Try to find the key in the columns to get its type if not specified
        key_column = next((col for col in columns["columns"] if col["name"] == key_name), None)

        if key_column:
            keys.append({
                "name": key_name,
                "alias": key_alias,
                "type": key_type or key_column.get("dataType")
            })
        else:
            logger.info(
                f"Warning: Key {key_name} not found in columns for table {table_name}. Adding with unknown type.")
            keys.append({
                "name": key_name,
                "alias": key_alias,
                "type": key_type or "unknown"
            })
    return keys


def _yaml_fallback_keys(table_name: str, custom_config: dict, columns) -> list:
    """Source has no discovered PKs — fall back to YAML keys (or documentKey.keys)."""
    keys = []
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
                    "type": key_column.get("dataType")
                })
            else:
                logger.warning(f"Fallback key '{key_name}' not found in columns for table '{table_name}'")
    return keys


def _table_keys(table_name: str, custom_config: dict, columns) -> list:
    """The keys of a table: the YAML ones when declared, else the discovered primary keys (else the YAML fallback)."""
    if custom_config and 'keys' in custom_config and custom_config['keys'] is not None:
        keys = _custom_keys(table_name, custom_config['keys'], columns)
        logger.info(f"Using custom keys for {table_name}: {keys}")
        return keys

    keys = _discovered_pk_keys(columns)
    logger.info(f"Using primary keys for {table_name}: {keys}")
    if not keys and custom_config:
        keys = _yaml_fallback_keys(table_name, custom_config, columns)
    return keys


def _log_table_settings(ctx: _TableCreationContext, table_name: str, custom_config: dict, columns) -> None:
    """Log the custom properties, target name, filters and document key a table is configured with."""
    # Get table-specific custom properties and merge with global properties
    table_custom_properties = custom_config.get('customProperties', {})
    source_custom_properties = {**ctx.global_source_custom_properties, **table_custom_properties.get('source', {})}
    target_custom_properties = {**ctx.global_target_custom_properties, **table_custom_properties.get('target', {})}

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
    if custom_config and 'documentKey' in custom_config:
        doc_key_config = custom_config['documentKey']
        document_key = {
            "prefix": doc_key_config.get('prefix', ''),
            "suffix": doc_key_config.get('suffix', ''),
            "separator": doc_key_config.get('separator', '-'),
            "keys": doc_key_config.get('keys', [])
        }
        logger.info(f"Document key configuration for {table_name}: {document_key}")


def _create_table(ctx: _TableCreationContext, table_name: str) -> None:
    """Create the target table of one source table, as the YAML configures it."""
    columns = get_table_columns(ctx.token, ctx.pipeline_id, ctx.source_agent_id, ctx.source_schema, table_name)

    custom_config = ctx.custom_tables.get(table_name, {})
    if custom_config is None:
        custom_config = {}
        logger.info(
            f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
    logger.info(f"Custom config for {table_name}: {custom_config}")

    _log_table_settings(ctx, table_name, custom_config, columns)
    target_table_name = custom_config.get('name', table_name)

    # Process keys and other configurations as before...
    keys = _table_keys(table_name, custom_config, columns)
    if not keys:
        logger.info(f"Warning: No keys specified for {table_name}. Table will have no keys.")

    # Only send source-discovered keys to the create-table statement.
    # YAML-defined keys are honoured for entity creation (create_all_entities.py)
    # but must not be sent to generate_create_table_statement when the source
    # agent has not discovered any primary keys.
    source_discovered_keys = _discovered_pk_keys(columns)

    # check if the table exists on the target
    handle_table_creation(ctx.pipeline_id, target_table_name, ctx.yaml_target_schema, source_discovered_keys, ctx.token,
                          columns, custom_config, ctx.source_node_info, ctx.target_node_info)


def _schema_tables_context(yaml_config: dict, source_schema: str, target_schema: str) -> dict:
    """The table settings of ``source_schema`` in the YAML configuration (target schema, lists, custom tables)."""
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
    return {
        "yaml_target_schema": yaml_target_schema,
        "whitelist": whitelist,
        "blacklist": blacklist,
        "custom_tables": custom_tables,
        "global_source_custom_properties": global_source_custom_properties,
        "global_target_custom_properties": global_target_custom_properties,
    }


def create_tables(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id,
                  yaml_config):
    """Create the target tables of the source tables of a schema, as the YAML configuration describes them."""
    # First, collect all unique group names from the YAML configuration
    group_names = _collect_group_names(yaml_config)

    # Create groups before creating entities
    _create_groups(token, pipeline_id, group_names)

    if not yaml_config:
        yaml_config = {}

    # Get node info for data type mapping
    source_node_info = get_node_info(token, pipeline_id, source_agent_id)
    target_node_info = get_node_info(token, pipeline_id, target_agent_id)

    logger.info("Source Node Info:")
    logger.info(json.dumps(source_node_info, indent=2))
    logger.info("Target Node Info:")
    logger.info(json.dumps(target_node_info, indent=2))

    logger.info(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")

    ctx = _TableCreationContext(
        token=token,
        pipeline_id=pipeline_id,
        source_schema=source_schema,
        source_agent_id=source_agent_id,
        source_node_info=source_node_info,
        target_node_info=target_node_info,
        **_schema_tables_context(yaml_config, source_schema, target_schema),
    )

    failures = []
    for table in tables:
        table_name = _table_name_of(table)

        if not table_name:
            logger.info("Warning: Table without name encountered. Skipping.")
            continue

        if _is_skipped_table(table_name, ctx.blacklist, ctx.whitelist):
            logger.info(f"Skipping table: {table_name}")
            continue

        try:
            _create_table(ctx, table_name)
        except TargetTableCreationError as error:
            logger.error(f"Table {table_name} not created: {error}")
            failures.append(f"{table_name}: {error}")

    if failures:
        raise TargetTableCreationError(f"{len(failures)} table(s) not created: {'; '.join(failures)}")


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


def _is_max_length(data_length) -> bool:
    """Whether a declared length means MAX: -1 (Core Hub discovery, exports) or 'max'."""
    if isinstance(data_length, str) and data_length.strip().lower() == "max":
        return True
    return _to_int(data_length, 0) == -1


def _mark_max(raw_length, column):
    """``column``, remembering whether its declared length was MAX."""
    column._declared_max = _is_max_length(raw_length)  # pylint: disable=protected-access
    return column


def _normalize_data_length(col_type: str, data_length: int) -> int:
    length_value = _to_int(data_length, 0)
    if is_length_type(col_type):
        return length_value if length_value > 0 else 1024
    return length_value


class _KeyLookup:
    """The key names of a table (and their alias), matched as given or case-insensitively."""

    def __init__(self, keys):
        # Build key lookup from keys parameter (for backward compatibility)
        # and from column isPrimaryKey property (preferred for Gluesync 2.2.6.0+)
        self.key_names = set()
        for key in (keys or []):
            if isinstance(key, dict):
                if key.get("name"):
                    self.key_names.add(key["name"])
                if key.get("alias"):
                    self.key_names.add(key["alias"])
            else:
                self.key_names.add(key)
        self.key_names_lower = {
            key_name.lower() for key_name in self.key_names if isinstance(key_name, str)
        }

    def is_primary_key(self, column_name) -> bool:
        # Keys-based check for backward compatibility
        if column_name in self.key_names:
            return True
        return isinstance(column_name, str) and column_name.lower() in self.key_names_lower


def _target_only_column_spec(target_col):
    """``(name, type, dataLength, numPrec, numScale, isNullable)`` of a targetOnlyColumns entry."""
    # Support both string format and object format
    if isinstance(target_col, str):
        # Simple string format: just the column name, with the default type, length, precision, scale and nullable
        return target_col, 'varchar', 1024, 0, 0, False
    # Object format with user-defined properties
    return (
        target_col.get('name'),
        target_col.get('dataType') or target_col.get('type') or 'varchar',  # null-safe: treat explicit null as missing
        target_col.get('dataLength', 1024),  # Default data length
        target_col.get('numPrec', 0),  # Default numeric precision
        target_col.get('numScale', 0),  # Default numeric scale
        target_col.get('isNullable', False),  # Default nullable
    )


def _target_only_column_dto(idx: int, target_col, key_lookup: _KeyLookup, source_node_info, target_node_info) -> Column:
    """The target column of a targetOnlyColumns entry, at position ``idx``."""
    col_name, col_type, col_data_length, col_numeric_precision, col_numeric_scale, col_is_nullable = \
        _target_only_column_spec(target_col)

    # Map the column type to target node type
    mapped_type = map_data_type(
        col_type,
        source_node_info,
        target_node_info,
    )
    raw_length = col_data_length
    col_data_length = _normalize_data_length(mapped_type, col_data_length)

    return _mark_max(raw_length, Column(
        name=col_name,
        dataType=format_column_type(mapped_type, col_data_length, col_numeric_precision, col_numeric_scale),
        id=idx,  # Use sequential IDs for target-only columns
        tableId=0,
        position=idx,
        isPK=key_lookup.is_primary_key(col_name),
        isNullable=col_is_nullable,
        charMaxLength=col_data_length,
        numPrec=col_numeric_precision,
        numScale=col_numeric_scale
    ))


def _target_only_column_dtos(target_only_columns, key_lookup: _KeyLookup, source_node_info, target_node_info) -> list:
    """The target columns of a targetOnlyColumns definition."""
    return [
        _target_only_column_dto(idx, target_col, key_lookup, source_node_info, target_node_info)
        for idx, target_col in enumerate(target_only_columns, 1)
    ]


def _split_column_entries(column_entries):
    """``(metadata columns, source→target mappings)`` of the YAML column entries (null entries are dropped)."""
    metadata_columns = []
    simple_mappings = []
    for entry in column_entries:
        if isinstance(entry, dict) and 'name' in entry:
            metadata_columns.append(entry)
        elif entry is not None:
            simple_mappings.append(entry)
    return metadata_columns, simple_mappings


def _metadata_column_dto(idx: int, col_meta: dict, key_lookup: _KeyLookup, source_node_info,
                         target_node_info) -> Column:
    """The target column of a YAML column metadata entry."""
    col_name = col_meta.get('name')

    # Use `or` rather than .get(key, default) so that an explicit
    # null value (type: null in YAML) is treated the same as a
    # missing key and falls through to the 'varchar' fallback.
    col_type = col_meta.get('dataType') or col_meta.get('type') or 'varchar'
    col_data_length = _to_int(col_meta.get('dataLength', col_meta.get('data_length')))
    col_numeric_precision = _to_int(col_meta.get('numericPrecision', col_meta.get('numeric_precision')))
    col_numeric_scale = _to_int(col_meta.get('numericScale', col_meta.get('numeric_scale')))
    col_is_nullable = _to_bool(col_meta.get('isNullable', col_meta.get('is_nullable')))

    # Use separate id and ordinalPosition from YAML metadata
    column_id = _to_int(col_meta.get('id'), idx)
    ordinal_position = _to_int(col_meta.get('ordinalPosition'), idx)

    # User-declared targetDataType wins over both `type` and the
    # automatic source->target mapping. When set, use it verbatim
    # and skip map_data_type entirely so it lands in the CREATE TABLE
    # statement exactly as declared (e.g. TIMESTAMP, DATE, DATETIME).
    _override = col_meta.get('targetDataType')
    if _override:
        mapped_type = _override
    else:
        # Map source type to target type (e.g. CHARACTER -> varchar)
        mapped_type = map_data_type(
            col_type,
            source_node_info,
            target_node_info,
        )
    raw_length = col_data_length
    col_data_length = _normalize_data_length(mapped_type, col_data_length)
    resolved_target_type = mapped_type

    # YAML-defined keys are the bootstrapper source of truth for
    # target table creation.  When source metadata says a column is
    # not a PK (for example CUSTOMERS_NO_PKEY in demo-kits), still
    # mark it as PK if the table-list-template declares it in keys.
    col_is_pk = key_lookup.is_primary_key(col_name) or bool(col_meta.get('isPK', col_meta.get('is_primary_key')))
    return _mark_max(raw_length, Column(
        name=col_name,
        dataType=format_column_type(resolved_target_type, col_data_length, col_numeric_precision, col_numeric_scale),
        id=column_id,
        tableId=0,

        position=ordinal_position,
        isPK=col_is_pk,
        isNullable=col_is_nullable,
        charMaxLength=col_data_length,
        numPrec=col_numeric_precision,
        numScale=col_numeric_scale
    ))


def _metadata_column_dtos(metadata_columns, target_table_name: str, key_lookup: _KeyLookup, source_node_info,
                          target_node_info) -> list:
    """The target columns of the YAML column metadata entries."""
    logger.info(f"Using YAML column metadata for table {target_table_name}")
    column_dtos = []
    for idx, col_meta in enumerate(metadata_columns, 1):
        if not col_meta.get('name'):
            logger.info(
                f"Warning: Column metadata entry missing 'name' for table {target_table_name}. Skipping entry: {col_meta}")
            continue
        column_dtos.append(_metadata_column_dto(
            idx, col_meta, key_lookup, source_node_info, target_node_info
        ))
    return column_dtos


def _source_column_dtos(columns, key_lookup: _KeyLookup, source_node_info, target_node_info) -> list:
    """When no metadata is provided, fall back to source column properties directly."""
    column_dtos = []
    for col in columns["columns"]:
        # Map source type to target type (e.g. CHARACTER -> varchar)
        mapped_type = map_data_type(
            col.get("dataType"),
            source_node_info,
            target_node_info,
        )
        raw_length = col.get("charMaxLength", 0)
        col_data_length = _normalize_data_length(mapped_type, col.get("charMaxLength", 0))
        # YAML-defined keys override source metadata so templates can
        # create target PKs for source tables that do not expose one.
        col_is_pk = key_lookup.is_primary_key(col["name"]) or bool(col.get("isPK"))
        column_dtos.append(_mark_max(raw_length, Column(
            name=col["name"],
            dataType=format_column_type(mapped_type, col_data_length,
                                   col.get("numPrec", 0), col.get("numScale", 0)),
            id=col.get("position", col.get("id", 1)),
            tableId=0,
            position=col.get("position", col.get("id", 1)),
            isPK=col_is_pk,
            isNullable=col.get("isNullable", False),
            charMaxLength=col_data_length,
            numPrec=col.get("numPrec", 0),
            numScale=col.get("numScale", 0)
        )))
    return column_dtos


def _first_set(*values):
    """The first of ``values`` that is not None, None when all are."""
    return next((value for value in values if value is not None), None)


def _row_position(row: dict) -> int:
    """The position an exported row gives its target column: ``targetPosition``, else ``ordinalPosition``, 0 for none."""
    for key in ('targetPosition', 'ordinalPosition'):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
    return 0


def _exported_row(relation: dict) -> dict:
    """The exported mapping row of a relation; empty for a shorthand pair ``{SRC: tgt}``, whose keys are column names."""
    row = relation.get('row') or {}
    return row if 'source' in row or not relation['sources'] else {}


def _exported_target_type(row: dict, source_col, source_node_info, target_node_info) -> str:
    """The target type of an exported row.

    The ``targetDataType`` it declares, verbatim; else its ``type`` when the target advertises it (an older export wrote
    the source type there); else the type the discovered source column maps to; else the one its ``type`` maps to.
    """
    if row.get('targetDataType'):
        return row['targetDataType']
    advertised = canonicalize_target_type(row.get('type'), target_node_info)
    if advertised:
        return advertised
    for source_type in ((source_col or {}).get('dataType'), row.get('type')):
        if source_type:
            return map_data_type(source_type, source_node_info, target_node_info)
    return 'varchar'


def _exported_nullable(row: dict, source_col) -> bool:
    """Whether the target column of an exported row is nullable: ``targetIsNullable``, else the source column's
    nullability (a column no source feeds is nullable)."""
    if isinstance(row.get('targetIsNullable'), bool):
        return row['targetIsNullable']
    source_nullable = (source_col or {}).get('isNullable')
    return _to_bool(_first_set(row.get('isNullable'), source_nullable), default=source_col is None)


def _exported_column_dto(target_name: str, relation: dict, source_col, key_lookup: _KeyLookup, source_node_info,
                         target_node_info) -> Column:
    """The target column ``target_name`` a relation of exported mapping rows writes, fed by ``source_col`` (or by none)."""
    row = _exported_row(relation)
    source = source_col or {}
    data_type = _exported_target_type(row, source_col, source_node_info, target_node_info)
    raw_length = _first_set(row.get('targetDataLength'), row.get('dataLength'), source.get('charMaxLength'))
    data_length = _normalize_data_length(data_type, raw_length)
    numeric_precision = _to_int(_first_set(
        row.get('targetNumericPrecision'), row.get('numericPrecision'), source.get('numPrec')))
    numeric_scale = _to_int(_first_set(
        row.get('targetNumericScale'), row.get('numericScale'), source.get('numScale')))
    is_pk = _to_bool(row.get('isPK'), default=False) or key_lookup.is_primary_key(target_name) \
        or any(key_lookup.is_primary_key(source_name) for source_name in relation['sources'])
    return _mark_max(raw_length, Column(
        name=target_name,
        dataType=format_column_type(data_type, data_length, numeric_precision, numeric_scale),
        id=0,
        tableId=0,
        position=_row_position(row),
        isPK=is_pk,
        isNullable=_exported_nullable(row, source_col),
        charMaxLength=data_length,
        numPrec=numeric_precision,
        numScale=numeric_scale
    ))


class _TableColumns:
    """The columns of a table to create: each target name once, the first row naming it wins."""

    def __init__(self, relations):
        by_lower = {}
        for relation in relations:
            for name in relation['targets']:
                by_lower.setdefault(name.lower(), set()).add(name)
        # Names that differ by case only are different columns, as on the entity side
        self._names = ColumnNames(name for names in by_lower.values() if len(names) > 1 for name in names)
        self._seen = set()
        self.columns = []

    def __contains__(self, name) -> bool:
        return self._names.key(name) in self._seen

    def add(self, column: Column) -> None:
        if column.name not in self:
            self._seen.add(self._names.key(column.name))
            self.columns.append(column)


def _in_table_order(columns: list) -> list:
    """The columns by position, those that state none last in the order given, numbered 1..n as ``id`` and ``position``."""
    ordered = sorted(enumerate(columns), key=lambda pair: (pair[1].position < 1, pair[1].position or pair[0]))
    for number, (_, column) in enumerate(ordered, 1):
        column.id = column.position = number
    return [column for _, column in ordered]


def _fed_relation_columns(table: _TableColumns, relations, source_columns, key_lookup, source_node_info,
                          target_node_info) -> bool:
    """Add the columns the relations write from a source column the source has; whether it added any."""
    fed = False
    for relation in relations:
        if not relation['sources']:
            continue
        # The entity builds no column for a source discovery does not return
        source_col = next((col for col in (find_by_name(source_columns, name) for name in relation['sources']) if col),
                          None)
        if source_col is None:
            continue
        for target_name in relation['targets']:
            table.add(_exported_column_dto(target_name, relation, source_col, key_lookup, source_node_info,
                                           target_node_info))
            fed = True
    return fed


def _add_omitted_key_columns(table: _TableColumns, relations, custom_config, source_columns, key_lookup,
                             source_node_info, target_node_info) -> None:
    """Add, by its own name, each column of the YAML ``keys`` that no row names: the entity maps it by name."""
    source_key = ColumnNames(col['name'] for col in source_columns if col.get('name')).key
    named = {source_key(name) for relation in relations for name in relation['sources']}
    for key_def in custom_config.get('keys') or []:
        key_col = find_by_name(source_columns, _parse_key_def(key_def)[0])
        if key_col is not None and source_key(key_col['name']) not in named:
            relation = {'sources': [key_col['name']], 'targets': [key_col['name']]}
            table.add(_exported_column_dto(key_col['name'], relation, key_col, key_lookup, source_node_info,
                                           target_node_info))


def _add_function_columns(table: _TableColumns, relations, key_lookup, source_node_info, target_node_info) -> None:
    """Add the columns of the rows without ``source``: the ones a field function writes."""
    for relation in relations:
        if not relation['sources']:
            for target_name in relation['targets']:
                table.add(_exported_column_dto(target_name, relation, None, key_lookup, source_node_info,
                                               target_node_info))


def _add_unwritten_target_only_columns(table: _TableColumns, custom_config, key_lookup, source_node_info,
                                       target_node_info) -> None:
    """Add the ``targetOnlyColumns`` that no row writes."""
    for target_col in custom_config.get('targetOnlyColumns') or []:
        if _target_only_column_spec(target_col)[0] not in table:
            table.add(_target_only_column_dto(0, target_col, key_lookup, source_node_info, target_node_info))


def _exported_column_dtos(relations, custom_config: dict, columns, key_lookup: _KeyLookup, source_node_info,
                          target_node_info) -> list:
    """The target columns of exported mapping rows: the columns the entity is built with.

    Each row gives the column(s) it writes, from the discovered source column it names (``target: []`` writes none, a
    list ``source`` is one column, a row without ``source`` is a column a field function writes); a mapping that writes
    no column falls back to every discovered source column, as the entity does. Then come the columns of the YAML
    ``keys`` no row names and the ``targetOnlyColumns`` no row writes. Empty when no source column feeds any.
    """
    source_columns = (columns or {}).get('columns') or []
    table = _TableColumns(relations)
    if not _fed_relation_columns(table, relations, source_columns, key_lookup, source_node_info, target_node_info):
        for column in _source_column_dtos(columns or {'columns': []}, key_lookup, source_node_info, target_node_info):
            table.add(column)
        if not table.columns:
            return []
    _add_omitted_key_columns(table, relations, custom_config, source_columns, key_lookup, source_node_info,
                             target_node_info)
    _add_function_columns(table, relations, key_lookup, source_node_info, target_node_info)
    _add_unwritten_target_only_columns(table, custom_config, key_lookup, source_node_info, target_node_info)
    return _in_table_order(table.columns)


def _mapped_column_dtos(column_entries, target_table_name: str, columns, key_lookup: _KeyLookup, source_node_info,
                        target_node_info, custom_config=None) -> list:
    """The target columns from the YAML column rows (exported mapping rows, else column metadata), else from the
    source columns.

    Raises :class:`TargetTableCreationError` for rows no table can be made of.
    """
    if is_column_mappings(column_entries):
        try:
            relations = yaml_relations(column_entries, label=target_table_name)
        except EntityMappingError as error:
            raise TargetTableCreationError(
                f"the column rows of {target_table_name} describe no table: {error}") from error
        return _exported_column_dtos(relations or [], custom_config or {}, columns, key_lookup, source_node_info,
                                     target_node_info)

    metadata_columns, other_entries = _split_column_entries(column_entries)
    if other_entries:
        raise TargetTableCreationError(
            f"the column entries of {target_table_name} mix column metadata with rows that are not, which cannot "
            f"describe a table to create: {other_entries}")

    column_dtos = []
    if metadata_columns:
        column_dtos = _metadata_column_dtos(metadata_columns, target_table_name, key_lookup, source_node_info,
                                            target_node_info)

    if not column_dtos:
        column_dtos = _source_column_dtos(columns, key_lookup, source_node_info, target_node_info)
    return column_dtos


def _mark_target_keys(column_dtos: list, custom_config: dict, target_table_name: str) -> None:
    """Make the columns the YAML ``targetKeys`` name primary keys too: the entity keys are the target of its source
    keys plus ``targetKeys``, and on a table that has a primary key they must be exactly that key.

    Names are compared case-insensitively, as the key rules do; a name that is no column of the table is refused.
    """
    for key_name in custom_config.get('targetKeys') or []:
        keyed = [column for column in column_dtos if column.name.lower() == str(key_name).lower()]
        if not keyed:
            raise TargetTableCreationError(
                f"targetKeys of {target_table_name} names {key_name}, which is no column of the table to create "
                f"({', '.join(column.name for column in column_dtos)})")
        for column in keyed:
            column.isPK = True


def _fan_in_row_target(entry):
    """The target column name a fan-in feeder row writes, or None."""
    if not isinstance(entry, dict):
        return None
    target = entry.get('target')
    if not (isinstance(target, str) and target):
        target = entry.get('source')
    return target if isinstance(target, str) and target else None


def _fan_in_peer_columns(peers, target_table_name, key_lookup, source_node_info, target_node_info,
                         present=None) -> dict:
    """The column DTOs every other feeder writes, by case-folded target name. ``present`` (a dict) receives
    ``{column casefolded: {index of each peer writing it}}``; a peer without column mappings counts as unknown
    (it writes none)."""
    peer_columns = {}
    for index, cfg in enumerate(peers):
        for target, dto in _fan_in_peer_dtos(cfg, target_table_name, key_lookup, source_node_info,
                                             target_node_info):
            peer_columns.setdefault(target.casefold(), []).append(dto)
            if present is not None:
                present.setdefault(target.casefold(), set()).add(index)
    return peer_columns


def _fan_in_peer_dtos(cfg, target_table_name, key_lookup, source_node_info, target_node_info):
    """``(target name, column DTO)`` for each column one feeder writes; none without column mappings."""
    columns = cfg.get('columns') or []
    if not is_column_mappings(columns):
        return
    for entry in columns:
        target = _fan_in_row_target(entry)
        if target is None:
            continue
        relation = {'sources': [], 'targets': [target], 'row': entry}
        try:
            dto = _exported_column_dto(target, relation, None, key_lookup, source_node_info, target_node_info)
        except Exception as error:  # pylint: disable=broad-except
            logger.debug(f"fan-in: column {target} of a feeder of {target_table_name} skipped: {error}")
            continue
        yield target, dto


def _report_fan_in_conflict(conflict: str) -> None:
    logger.warning(conflict)
    try:
        import import_options  # pylint: disable=import-outside-toplevel
        import_options.current().report.warnings.append(conflict)
    except Exception:  # pylint: disable=broad-except
        pass


def _merge_fan_in_column(own, others, target_table_name: str, feeder_count: int) -> None:
    """Widen ``own`` to the type that safely holds every feeder's, or log the conflict."""
    import fan_in  # pylint: disable=import-outside-toplevel
    # MAX (-1) as declared, not the 1024 _normalize_data_length made of it: VARCHAR(10) + VARCHAR(MAX) is MAX.
    specs = [(c.dataType, -1 if getattr(c, '_declared_max', False) else c.charMaxLength, c.numPrec, c.numScale)
             for c in [own] + others]
    merged, reason = fan_in.merge_types(specs)
    if merged is None:
        _report_fan_in_conflict(
            f"fan-in {target_table_name}: column {own.name} has types no type safely holds ({reason}): "
            f"created as {fan_in.describe(fan_in._norm(specs[0]))}, the type of this feeder; set "
            f"targetDataType on the rows to choose one")
        return
    before = fan_in.describe(fan_in._norm(specs[0]))
    own.dataType = merged[0]
    if merged[1] == -1:
        # Kept as -1, the value Core Hub discovery itself reports for a MAX column.
        own.charMaxLength = -1
        own._declared_max = True  # pylint: disable=protected-access
    elif merged[1] is not None:
        own.charMaxLength = _normalize_data_length(own.dataType, merged[1])
    if merged[2] is not None:
        own.numPrec, own.numScale = merged[2], merged[3]
    after = fan_in.describe(fan_in._norm((own.dataType, own.charMaxLength, own.numPrec, own.numScale)))
    if after != before:
        logger.info(f"fan-in {target_table_name}: column {own.name} widened from {before} to {after} for all "
                    f"{feeder_count} feeders")


def _merge_fan_in_nullability(column_dtos, peer_columns, present, peer_count, target_table_name) -> None:
    """A column of this feeder is nullable when any other feeder lacks it or declares it nullable (its rows would
    otherwise fail on NOT NULL). Key columns stay NOT NULL; a key another feeder lacks is logged."""
    relaxed = []
    for column in column_dtos:
        folded = column.name.casefold()
        missing = len(present.get(folded, ())) < peer_count
        nullable_elsewhere = any(other.isNullable for other in peer_columns.get(folded, []))
        if not (missing or nullable_elsewhere) or column.isNullable:
            continue
        if column.isPK:
            if missing:
                _report_fan_in_conflict(f"fan-in {target_table_name}: key column {column.name} is not written by "
                                        f"every feeder; their rows will fail")
            continue
        column.isNullable = True
        relaxed.append(column.name)
    if relaxed:
        logger.info(f"fan-in {target_table_name}: column(s) made nullable as another feeder lacks them or has them "
                    f"nullable: {', '.join(relaxed)}")


def _apply_fan_in(column_dtos: list, custom_config: dict, fan_in_peers, target_table_name: str, key_lookup,
                  source_node_info, target_node_info) -> list:
    """The columns of a table several YAML tables feed: the union of every feeder's columns, each with the widest
    type that safely holds all feeders' (see :func:`fan_in.merge_types`). A column only other feeders write is added
    nullable; a column whose types conflict keeps this feeder's type, and is logged."""
    peers = [cfg for cfg in fan_in_peers or [] if isinstance(cfg, dict) and cfg is not custom_config and cfg != custom_config]
    if not peers:
        return column_dtos
    by_name = {column.name.casefold(): column for column in column_dtos}
    present = {}
    peer_columns = _fan_in_peer_columns(peers, target_table_name, key_lookup, source_node_info, target_node_info,
                                        present)
    _merge_fan_in_nullability(column_dtos, peer_columns, present, len(peers), target_table_name)
    added = []
    for folded, others in peer_columns.items():
        own = by_name.get(folded)
        if own is None:
            own = others[0].model_copy() if hasattr(others[0], 'model_copy') else others[0].copy()
            own.isPK, own.isNullable, own.position = False, True, 0
            column_dtos.append(own)
            by_name[folded] = own
            added.append(own.name)
        _merge_fan_in_column(own, others, target_table_name, len(peers) + 1)
    if added:
        logger.info(f"fan-in {target_table_name}: added nullable column(s) other feeders write: {', '.join(added)}")
    return _in_table_order(column_dtos) if added else column_dtos


def handle_table_creation(pipeline_id: str, target_table_name: str, yaml_target_schema: str, keys: list, token: str,
                          columns: list, custom_config, source_node_info, target_node_info, fan_in_peers=None) -> bool:
    """Create the target table when it does not exist and CREATE_TABLE_IF_NOT_EXISTS is on.

    Returns whether it created the table. Raises :class:`TargetTableCreationError` when the table has to be created
    and cannot be.
    """
    custom_config = custom_config or {}

    if table_exists(pipeline_id=pipeline_id, schema_name=yaml_target_schema, table_name=target_table_name,
                    token=token):
        logger.info(f"table: {target_table_name} already exists")
        return False
    if not CREATE_TABLE_IF_NOT_EXISTS:
        logger.info(f"table: {target_table_name} does not exists, skipping creation as CREATE_TABLE_IF_NOT_EXISTS is false")
        return False

    logger.info(f"table: {target_table_name} does not exists, creating it")
    # Create ColumnDto objects
    column_entries = custom_config.get('columns') or []
    target_only_columns = custom_config.get('targetOnlyColumns', [])

    key_lookup = _KeyLookup(keys)

    # Check if targetOnlyColumns are defined - if so, use them instead of source columns, unless the YAML maps
    # source columns onto the target: an export lists the columns a UDF writes next to its rows, and a table of
    # those columns alone would not be the one the rows write
    has_mapping_rows = any(isinstance(entry, dict) and 'source' in entry for entry in column_entries)
    if target_only_columns and not has_mapping_rows:
        logger.info(f"Using targetOnlyColumns definition for table {target_table_name}")
        column_dtos = _target_only_column_dtos(target_only_columns, key_lookup, source_node_info, target_node_info)
    else:
        column_dtos = _mapped_column_dtos(column_entries, target_table_name, columns, key_lookup, source_node_info,
                                          target_node_info, custom_config)

    if not column_dtos:
        raise TargetTableCreationError(
            f"no column to create table {yaml_target_schema}.{target_table_name} with: no source column feeds it")
    column_dtos = _apply_fan_in(column_dtos, custom_config, fan_in_peers, target_table_name, key_lookup,
                                source_node_info, target_node_info)
    _mark_target_keys(column_dtos, custom_config, target_table_name)

    table_data = GenerateCreateTargetTableStatementRequest(columns=column_dtos)
    try:
        statement = generate_create_table_statement(pipeline_id=pipeline_id, schema_name=yaml_target_schema,
                                                    table_name=target_table_name, token=token,
                                                    table_data=table_data)
        logger.info(f"create table statement: {statement}")
        create_target_table(pipeline_id=pipeline_id, create_table_request=CreateTableRequest(statement=statement),
                            token=token)
    except (requests.exceptions.RequestException, RuntimeError) as error:
        raise TargetTableCreationError(
            f"creating table {yaml_target_schema}.{target_table_name} failed: {error}") from error
    return True


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
            yaml_config
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

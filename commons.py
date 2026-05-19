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

import colorsys
import json
import os
import random
import re
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import yaml
from typing import Dict, List, Optional, Tuple, Union, Any
from utils.log import get_logger, log_success, log_failure
from utils.chronos_client import ChronosClient
from utils.core_hub_client import CoreHubClient

CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'
CHRONOS_URL = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)


def set_core_hub_client(client: CoreHubClient):
    """Replace the global CoreHub client instance."""
    global core_hub_client
    core_hub_client = client


def configure_core_hub(base_url: str, *, use_ssl: Optional[bool] = None, skip_verify: Optional[bool] = None) -> CoreHubClient:
    """
    Configure the CoreHub client with runtime options.

    Args:
        base_url: CoreHub base URL, with or without scheme.
        use_ssl: Force SSL enable/disable. Uses environment default when None.
        skip_verify: Force TLS verification skip flag. Uses environment default when None.

    Returns:
        CoreHubClient: The configured client instance.
    """

    logger.info("Reconfiguring CoreHub client: base_url=%s, use_ssl=%s, skip_verify=%s", base_url, use_ssl, skip_verify)
    os.environ['CORE_HUB_URL'] = base_url
    if use_ssl is not None:
        os.environ['SSL_ENABLED'] = 'true' if use_ssl else 'false'
    if skip_verify is not None:
        os.environ['SSL_SKIP_VERIFY'] = 'true' if skip_verify else 'false'

    client = CoreHubClient(base_url, use_ssl=use_ssl, skip_verify=skip_verify)
    set_core_hub_client(client)
    return client


def set_scheduling_enabled(enabled: bool):
    """Toggle scheduling feature at runtime."""
    global ENABLE_SCHEDULING
    ENABLE_SCHEDULING = enabled
    os.environ['ENABLE_SCHEDULING'] = 'true' if enabled else 'false'
    logger.info("Scheduling feature set to %s", ENABLE_SCHEDULING)

logger = get_logger()

_ORACLE_AGENT_TAGS: Optional[set[str]] = None


def get_oracle_agent_tags() -> set[str]:
    """Return a cached set of agent tags that map to Oracle from agents.json."""

    global _ORACLE_AGENT_TAGS
    if _ORACLE_AGENT_TAGS is not None:
        return _ORACLE_AGENT_TAGS

    oracle_tags: set[str] = set()
    try:
        json_path = Path(__file__).resolve().parent / "agents.json"
        if not json_path.exists():
            _ORACLE_AGENT_TAGS = oracle_tags
            return oracle_tags

        with json_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

        items = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            _ORACLE_AGENT_TAGS = oracle_tags
            return oracle_tags

        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("internalName") or "").strip().lower()
            database_name = str(entry.get("databaseName") or "").strip().lower()
            if not name:
                continue
            if "oracle" in name or "oracle" in database_name:
                oracle_tags.add(name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to load Oracle agent tags from agents.json: %s", exc)

    _ORACLE_AGENT_TAGS = oracle_tags
    return oracle_tags


def fetch_core_hub(path, method='GET', token=None, body=None, params=None, headers=None, expected_error_statuses=None):
    # Log request details
    logger.debug(f"\n{'=' * 80}")
    logger.debug(f"[API REQUEST] {method.upper()} {path}")

    if params:
        logger.debug("\nQuery Parameters:")
        for k, v in params.items():
            logger.debug(f"  {k}: {v}")

    if headers:
        logger.debug("\nCustom Headers:")
        for k, v in headers.items():
            logger.debug(f"  {k}: {v}")

    if body is not None:
        logger.debug("\nRequest Body:")
        try:
            if isinstance(body, (bytes, bytearray)):
                logger.debug(f"<binary payload: {len(body)} bytes>")
            else:
                logger.debug(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body))
        except Exception as e:
            logger.debug(f"<Unable to serialize request body: {e}>")

    logger.debug("-" * 40)

    try:
        # Make the request
        start_time = time.time()
        response = core_hub_client.request(path, method, token, body, params, headers=headers, expected_error_statuses=expected_error_statuses)
        duration = time.time() - start_time

        # Log response (dump full JSON when possible, otherwise capture long strings)
        logger.debug(f"Request completed in {duration:.3f}s")
        logger.debug(f"[RESPONSE] {method.upper()} {path}")

        try:
            if isinstance(response, (dict, list)):
                response_text = json.dumps(response, indent=2, ensure_ascii=False)
            else:
                response_text = str(response)
        except Exception as exc:  # pragma: no cover - defensive
            response_text = f"<unable to serialize response: {exc}>"

        max_preview_chars = 10000
        if len(response_text) > max_preview_chars:
            logger.debug(
                "Response (trimmed to %d of %d chars): %s…",
                max_preview_chars,
                len(response_text),
                response_text[:max_preview_chars],
            )
        else:
            logger.debug("Response: %s", response_text)

        logger.debug("=" * 80 + "\n")

        return response

    except Exception as e:
        # Check if this is an expected error status (e.g. 404 during table existence checks)
        is_expected = False
        if expected_error_statuses and hasattr(e, 'response') and e.response is not None:
            is_expected = e.response.status_code in expected_error_statuses

        if is_expected:
            logger.info(f"API request returned expected status: {str(e)}")
        else:
            logger.error(f"API request failed: {str(e)}")

        error_payload = None
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_payload = e.response.json()
                if is_expected:
                    logger.info(f"Response: {json.dumps(error_payload, indent=2)}")
                else:
                    logger.error(f"Error response: {json.dumps(error_payload, indent=2)}")
            except Exception:
                error_payload = e.response.text
                if is_expected:
                    logger.info(f"Response: {error_payload}")
                else:
                    logger.error(f"Error response: {error_payload}")
        logger.debug("=" * 80 + "\n")

        message = f"CoreHub request {method.upper()} {path} failed: {e}"
        if error_payload:
            if isinstance(error_payload, (dict, list)):
                message += f" | Response: {json.dumps(error_payload, ensure_ascii=False)}"
            else:
                message += f" | Response: {error_payload}"

        raise RuntimeError(message) from e


def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]


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

def compute_java_hashcode(s):
    """
    Compute Java-style hashCode for a string.
    The Java hashCode algorithm is: h = s[0]*31^(n-1) + s[1]*31^(n-2) + ... + s[n-1]
    where n is the length of the string.
    """
    h = 0
    for char in s:
        # Java's int wraps around at 2^31 - 1 and -(2^31)
        h = (31 * h + ord(char)) & 0xFFFFFFFF
        # Convert unsigned to signed
        if h > 0x7FFFFFFF:
            h = h - 0x100000000
    return h

def get_table_id(schema, table_name):
    """
    Generate the table ID using Java hashCode of "schema.tableName".
    """
    return compute_java_hashcode(f"{schema}.{table_name}")


def get_node_info(token, pipeline_id, agent_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info", token=token)


def create_entity_schedules(token, pipeline_id, entity_id, entity_name, schedules_config):
    """Create schedules for an entity based on the YAML configuration."""
    if not schedules_config or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating schedules for entity {entity_name} (ID: {entity_id})")

    chronos_client = ChronosClient(base_url=CHRONOS_URL, corehub_url=CORE_HUB_URL)
    
    # Wait for Chronos to be available before attempting to create schedules
    if not chronos_client.wait_for_chronos():
        logger.error(f"Chronos is not available. Skipping schedule creation for entity {entity_name}")
        return

    for schedule_config in schedules_config:
        try:
            # Extract schedule parameters
            task_type = schedule_config.get('task_type')
            name = schedule_config.get('name')
            description = schedule_config.get('description')
            with_snapshot = schedule_config.get('with_snapshot', False)
            enabled = schedule_config.get('enabled', True)
            snapshot_write_method = schedule_config.get('snapshot_write_method')

            # Create a configuration dict for the chronos client
            schedule_data = {}
            if 'cron_expression' in schedule_config:
                schedule_data['cron_expression'] = schedule_config['cron_expression']
            elif 'schedule' in schedule_config:
                schedule_data['schedule'] = schedule_config['schedule']
            else:
                logger.warning(
                    f"Schedule for entity {entity_name} is missing both 'cron_expression' and 'schedule'. Skipping.")
                continue

            # Create the schedule
            result = chronos_client.create_entity_schedule(
                pipeline_id=pipeline_id,
                entity_id=entity_id,
                task_type=task_type,
                schedule_config=schedule_data,
                name=name,
                description=description,
                with_snapshot=with_snapshot,
                enabled=enabled,
                snapshot_write_method=snapshot_write_method
            )

            log_success(logger, f"Created {task_type} schedule for entity {entity_name}: {name}")
            logger.debug(f"Schedule details: {json.dumps(result)}")

        except Exception as e:
            error_msg = f"Failed to create schedule for entity {entity_name}: {str(e)}"
            log_failure(logger, error_msg)
            logger.error(traceback.format_exc())
            # Continue creating other schedules even if one fails


def create_group_schedules(token, pipeline_id, group_schedules):
    """
    Create schedules for groups based on the YAML configuration.
    
    Args:
        token (str): Authentication token
        pipeline_id (str): ID of the pipeline
        group_schedules (dict): Dictionary mapping group IDs to their schedule configurations
                               or a list of schedule configurations for multiple groups
    """
    if not group_schedules or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating group-level schedules for pipeline {pipeline_id}")

    chronos_client = ChronosClient(base_url=CHRONOS_URL, corehub_url=CORE_HUB_URL)
    
    # Wait for Chronos to be available before attempting to create schedules
    if not chronos_client.wait_for_chronos():
        logger.error(f"Chronos is not available. Skipping group schedule creation for pipeline {pipeline_id}")
        return
    
    # Handle case where group_schedules is a list of schedule configs for multiple groups
    if isinstance(group_schedules, list):
        for schedule_config in group_schedules:
            _process_group_schedule(chronos_client, pipeline_id, None, schedule_config)
        return
    
    # Handle case where group_schedules is a dict mapping group IDs to schedule configs
    for group_id, schedules_config in group_schedules.items():
        if isinstance(schedules_config, dict) and 'task_type' in schedules_config:
            # Single schedule config for this group
            _process_group_schedule(chronos_client, pipeline_id, [group_id], schedules_config)
        elif isinstance(schedules_config, list):
            # Multiple schedule configs for this group
            for schedule_config in schedules_config:
                _process_group_schedule(chronos_client, pipeline_id, [group_id], schedule_config)

def _process_group_schedule(chronos_client, pipeline_id, group_ids, schedule_config):
    """
    Process a single group schedule configuration.
    
    Args:
        chronos_client (ChronosClient): The Chronos client instance
        pipeline_id (str): ID of the pipeline
        group_ids (list): List of group IDs this schedule applies to
        schedule_config (dict): Schedule configuration
    """
    try:
        # Extract schedule parameters with defaults
        task_type = schedule_config.get('task_type')
        name = schedule_config.get('name')
        description = schedule_config.get('description')
        with_snapshot = schedule_config.get('with_snapshot', False)
        enabled = schedule_config.get('enabled', True)
        snapshot_write_method = schedule_config.get('snapshot_write_method')
        
        # Support both 'group_ids' and 'groups' for specifying multiple groups
        if 'group_ids' in schedule_config:
            group_ids = schedule_config['group_ids']
            if isinstance(group_ids, str):
                group_ids = [group_ids]
        elif 'groups' in schedule_config:
            group_ids = schedule_config['groups']
            if isinstance(group_ids, str):
                group_ids = [group_ids]
        
        # If no group_ids provided at all, use the one from the function parameter
        if not group_ids and 'group_id' in schedule_config:
            group_ids = [schedule_config['group_id']]
        
        if not group_ids:
            logger.warning("No group IDs provided for schedule. Skipping.")
            return
            
        # Create a configuration dict for the chronos client
        schedule_data = {}
        if 'cron_expression' in schedule_config:
            schedule_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            # Support both direct schedule object and nested under 'schedule' key
            schedule_data['schedule'] = schedule_config['schedule']
        else:
            logger.warning("Schedule is missing both 'cron_expression' and 'schedule'. Skipping.")
            return

        # Create the schedule
        result = chronos_client.create_group_schedule(
            pipeline_id=pipeline_id,
            group_ids=group_ids,
            task_type=task_type,
            schedule_config=schedule_data,
            name=name,
            description=description,
            with_snapshot=with_snapshot,
            enabled=enabled,
            snapshot_write_method=snapshot_write_method
        )

        group_names = ", ".join(group_ids[:3])
        if len(group_ids) > 3:
            group_names += f" and {len(group_ids) - 3} more"
            
        log_success(logger, f"Created {task_type} schedule for groups {group_names}: {name}")
        logger.debug(f"Schedule details: {json.dumps(result, indent=2)}")

    except Exception as e:
        group_info = f"groups {group_ids}" if group_ids else "unknown group"
        error_msg = f"Failed to create schedule for {group_info}: {str(e)}"
        log_failure(logger, error_msg)
        logger.error(traceback.format_exc())
        # Continue creating other schedules even if one fails


def create_pipeline_schedules(token, pipeline_id, pipeline_schedules):
    """Create schedules for the entire pipeline based on the YAML configuration."""
    if not pipeline_schedules or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating pipeline-level schedules for pipeline {pipeline_id}")

    chronos_client = ChronosClient(base_url=CHRONOS_URL, corehub_url=CORE_HUB_URL)
    
    # Wait for Chronos to be available before attempting to create schedules
    if not chronos_client.wait_for_chronos():
        logger.error(f"Chronos is not available. Skipping pipeline schedule creation for pipeline {pipeline_id}")
        return

    for schedule_config in pipeline_schedules:
        try:
            # Extract schedule parameters
            task_type = schedule_config.get('task_type')
            name = schedule_config.get('name')
            description = schedule_config.get('description')
            with_snapshot = schedule_config.get('with_snapshot', False)
            enabled = schedule_config.get('enabled', True)
            snapshot_write_method = schedule_config.get('snapshot_write_method')

            # Create a configuration dict for the chronos client
            schedule_data = {}
            if 'cron_expression' in schedule_config:
                schedule_data['cron_expression'] = schedule_config['cron_expression']
            elif 'schedule' in schedule_config:
                schedule_data['schedule'] = schedule_config['schedule']
            else:
                logger.warning(f"Pipeline schedule is missing both 'cron_expression' and 'schedule'. Skipping.")
                continue

            # Create the schedule
            result = chronos_client.create_pipeline_schedule(
                pipeline_id=pipeline_id,
                task_type=task_type,
                schedule_config=schedule_data,
                name=name,
                description=description,
                with_snapshot=with_snapshot,
                enabled=enabled,
                snapshot_write_method=snapshot_write_method
            )

            log_success(logger, f"Created {task_type} schedule for pipeline {pipeline_id}: {name}")
            logger.debug(f"Schedule details: {json.dumps(result)}")

        except Exception as e:
            error_msg = f"Failed to create pipeline schedule: {str(e)}"
            log_failure(logger, error_msg)
            logger.error(traceback.format_exc())
            # Continue creating other schedules even if one fails


def extract_target_column_types(discovered_target_columns):
    """Extract the set of column dataTypes present in a discovered target table.

    Accepts either:
    - dict as returned by ``get_table_columns`` (``{"columns": [...]}``)
    - list of column dicts
    - dict keyed by column name with column dicts as values
    Returns a set of lower-cased dataType strings. Returns empty set if nothing
    usable is available.
    """
    if not discovered_target_columns:
        return set()

    cols = None
    if isinstance(discovered_target_columns, dict):
        if 'columns' in discovered_target_columns and isinstance(discovered_target_columns['columns'], list):
            cols = discovered_target_columns['columns']
        else:
            cols = list(discovered_target_columns.values())
    elif isinstance(discovered_target_columns, list):
        cols = discovered_target_columns

    if not cols:
        return set()

    result = set()
    for c in cols:
        if not isinstance(c, dict):
            continue
        dt = c.get('dataType') or c.get('type')
        if dt:
            result.add(dt)
    return result


def map_data_type(source_type, source_node_info, target_node_info,
                  source_agent_tag=None, target_agent_tag=None,
                  target_table_column_types=None):
    """Map a source native type to the corresponding target native type.

    Algorithm:
    1. If the target table already exists and ``target_table_column_types`` is
       provided, and the source column's ``dataType`` matches any dataType used
       by an existing target column (case-insensitive), keep the source type
       as-is. (Match is by data type value only, not by column name.)
    2. Otherwise, resolve the source native type -> ``gluesyncDataType`` using
       the source matrix, find the target matrix entry with that same
       ``gluesyncDataType`` and return its ``defaultType``.
    3. Fallbacks:
       - If no source entry is found: use target matrix's ``STRING`` defaultType.
       - If no target entry is found for the source's gluesyncDataType: return
         ``source_item.defaultType`` as a last resort.
    """
    source_node_info = source_node_info or {}
    target_node_info = target_node_info or {}

    source_matrix = source_node_info.get('dataTypesMatrix', [])
    target_matrix = target_node_info.get('dataTypesMatrix', [])

    # Step 1: If target table exists, reuse source type when it's already a
    # dataType used by an existing target column.
    if target_table_column_types and source_type in target_table_column_types:
        logger.debug(
            f"map_data_type: source type '{source_type}' already present "
            f"in target table, keeping as-is"
        )
        return source_type

    # Step 2: Resolve source -> gluesyncDataType
    source_item = next(
        (
            item for item in source_matrix
            if source_type in item.get('supportedTypes', [])
            or item.get('gluesyncDataType') == source_type
        ),
        None
    )

    if not source_item:
        logger.warning(
            f"map_data_type: no source mapping for '{source_type}'. "
            f"Falling back to target's STRING defaultType."
        )
        target_item_fallback = next(
            (item for item in target_matrix if item.get('gluesyncDataType') == 'STRING'),
            None
        )
        if target_item_fallback:
            return target_item_fallback.get('defaultType', source_type)
        return source_type

    source_gluesync_type = source_item.get('gluesyncDataType')

    # Step 3: Find target entry with the same gluesyncDataType and use its defaultType
    target_item = next(
        (
            item for item in target_matrix
            if item.get('gluesyncDataType') == source_gluesync_type
        ),
        None
    )

    if target_item and target_item.get('defaultType'):
        logger.debug(
            f"map_data_type: mapping '{source_type}' (gluesyncDataType={source_gluesync_type}) "
            f"-> target defaultType '{target_item['defaultType']}'"
        )
        return target_item['defaultType']

    # Step 4: Compatibility-group fallback. If the target matrix has no entry
    # for the source's gluesyncDataType, look up the target's
    # ``dataTypeCompatibilityMatrix`` to find the category containing the
    # source's gluesyncDataType (e.g. DOUBLE -> NUMBER), then pick the first
    # gluesyncDataType in that category that the target matrix implements.
    # This covers cases like source=double precision (DOUBLE) -> target Vertica
    # which exposes only FLOAT under the NUMBER group.
    compatibility_matrix = target_node_info.get('dataTypeCompatibilityMatrix', {}) or {}
    target_types_by_gsd = {
        item.get('gluesyncDataType'): item
        for item in target_matrix
        if item.get('gluesyncDataType')
    }
    for category, members in compatibility_matrix.items():
        if not isinstance(members, list) or source_gluesync_type not in members:
            continue
        for candidate_gsd in members:
            if candidate_gsd == source_gluesync_type:
                continue
            candidate_item = target_types_by_gsd.get(candidate_gsd)
            if candidate_item and candidate_item.get('defaultType'):
                logger.debug(
                    f"map_data_type: no exact target mapping for "
                    f"gluesyncDataType '{source_gluesync_type}'; using "
                    f"compatibility group '{category}' member "
                    f"'{candidate_gsd}' -> '{candidate_item['defaultType']}'"
                )
                return candidate_item['defaultType']
        break

    logger.warning(
        f"map_data_type: no target mapping for gluesyncDataType '{source_gluesync_type}'. "
        f"Using source's defaultType."
    )
    return source_item.get('defaultType', source_type)


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


def extract_schemas_from_yaml(yaml_file_path):
    """
    Extract source and target schema information from the YAML configuration file.
    Returns a tuple of (source_schema, target_schema) or (None, None) if not found.
    """
    try:
        # Check if file exists first
        import os
        if not os.path.exists(yaml_file_path):
            logger.error(f"YAML file does not exist: {yaml_file_path}")
            return None, None
        
        logger.info(f"Loading YAML configuration from: {yaml_file_path}")
        yaml_config = load_yaml_config(yaml_file_path)
        
        if not yaml_config:
            logger.warning(f"YAML file is empty or could not be loaded: {yaml_file_path}")
            return None, None
            
        # Handle two possible YAML formats:
        # Format 1: schemas wrapper (template format)
        # Format 2: source schema as top-level key (user format)
        
        if 'schemas' in yaml_config:
            # Format 1: schemas wrapper
            logger.info("Found 'schemas' section in YAML file")
            schemas = yaml_config['schemas']
            if not schemas:
                logger.warning(f"Empty schemas section in YAML file: {yaml_file_path}")
                return None, None
            
            logger.info(f"Found schemas: {list(schemas.keys())}")
            source_schema = list(schemas.keys())[0]
            schema_config = schemas[source_schema]
        else:
            # Format 2: source schema as top-level key
            logger.info("No 'schemas' section found, treating top-level keys as source schemas")
            logger.info(f"Available top-level keys: {list(yaml_config.keys())}")
            
            # Find the first key that has a 'target' property (indicating it's a schema config)
            schema_candidates = []
            for key, value in yaml_config.items():
                if isinstance(value, dict) and ('target' in value or 'tables' in value):
                    schema_candidates.append(key)
            
            if not schema_candidates:
                logger.warning(f"No valid schema configurations found in YAML file: {yaml_file_path}")
                return None, None
            
            source_schema = schema_candidates[0]
            schema_config = yaml_config[source_schema]
            logger.info(f"Using top-level key '{source_schema}' as source schema")
        
        # Get target schema from the configuration
        target_schema = schema_config.get('target', source_schema)
        
        logger.info(f"Extracted schemas from YAML - Source: {source_schema}, Target: {target_schema}")
        return source_schema, target_schema
        
    except Exception as e:
        logger.error(f"Error extracting schemas from YAML file {yaml_file_path}: {e}")
        logger.error(f"Exception details: {traceback.format_exc()}")
        return None, None


def extract_all_schemas_from_yaml(yaml_file_path):
    """
    Extract all source and target schema pairs from the YAML configuration file.
    Supports both formats:
    - Format 1: Top-level 'schemas' dict where each key is a source schema
    - Format 2: Multiple top-level keys each representing a schema configuration

    Returns a list of tuples: [(source_schema, target_schema), ...]
    Returns an empty list if none found or file missing.
    """
    try:
        if not os.path.exists(yaml_file_path):
            logger.error(f"YAML file does not exist: {yaml_file_path}")
            return []

        logger.info(f"Loading YAML configuration from: {yaml_file_path}")
        yaml_config = load_yaml_config(yaml_file_path)

        if not yaml_config:
            logger.warning(f"YAML file is empty or could not be loaded: {yaml_file_path}")
            return []

        schema_pairs = []

        # Format 1: schemas wrapper
        if isinstance(yaml_config.get('schemas'), dict) and yaml_config.get('schemas'):
            logger.info("Found 'schemas' section in YAML file")
            for source_schema, schema_config in yaml_config['schemas'].items():
                if not isinstance(schema_config, dict):
                    continue
                target_schema = schema_config.get('target', source_schema)
                schema_pairs.append((source_schema, target_schema))

        else:
            # Format 2: top-level keys treated as schema configs
            logger.info("No 'schemas' section found, treating top-level keys as source schemas")
            for key, value in yaml_config.items():
                # Skip non-dicts
                if not isinstance(value, dict):
                    continue
                # Skip reserved key if present
                if key == 'schemas':
                    continue
                # Consider as schema if contains either 'target' or 'tables'
                if 'target' in value or 'tables' in value:
                    source_schema = key
                    target_schema = value.get('target', source_schema)
                    schema_pairs.append((source_schema, target_schema))

        logger.info(f"Extracted {len(schema_pairs)} schema pair(s) from YAML: {schema_pairs}")
        return schema_pairs

    except Exception as e:
        logger.error(f"Error extracting schemas from YAML file {yaml_file_path}: {e}")
        logger.error(f"Exception details: {traceback.format_exc()}")
        return []


def extract_schema_types_from_yaml(yaml_file_path):
    """Extract normalized (source_type, target_type) hints from YAML.

    This looks for optional `sourceType` and `targetType` keys under each
    schema configuration (both single-schema and multi-schema formats).

    Returns a tuple `(source_type, target_type)` where each element is either
    a normalized string (e.g. "SQL" or "NoSQL") or None if it cannot be
    determined unambiguously.
    """

    try:
        if not os.path.exists(yaml_file_path):
            logger.error(f"YAML file does not exist: {yaml_file_path}")
            return None, None

        logger.info(f"Loading YAML configuration from: {yaml_file_path} for type extraction")
        yaml_config = load_yaml_config(yaml_file_path)

        if not yaml_config:
            logger.warning(f"YAML file is empty or could not be loaded: {yaml_file_path}")
            return None, None

        def _normalize(value):
            if not value:
                return None
            text = str(value).strip()
            upper = text.upper()
            if upper in {"RDBMS", "SQL"}:
                return "SQL"
            if upper == "NOSQL":
                return "NoSQL"
            return text

        source_types = set()
        target_types = set()

        # Format 1: schemas wrapper
        if isinstance(yaml_config.get('schemas'), dict) and yaml_config.get('schemas'):
            for schema_config in yaml_config['schemas'].values():
                if not isinstance(schema_config, dict):
                    continue
                st = _normalize(schema_config.get('sourceType'))
                tt = _normalize(schema_config.get('targetType'))
                if st:
                    source_types.add(st)
                if tt:
                    target_types.add(tt)
        else:
            # Format 2: top-level keys treated as schema configs
            for key, value in yaml_config.items():
                if not isinstance(value, dict):
                    continue
                if key in {'schemas', 'groups'}:
                    continue
                if 'target' not in value and 'tables' not in value:
                    continue
                logger.debug(f"Processing schema '{key}': sourceType={value.get('sourceType')}, targetType={value.get('targetType')}")
                st = _normalize(value.get('sourceType'))
                tt = _normalize(value.get('targetType'))
                logger.debug(f"After normalization for schema '{key}': sourceType={st}, targetType={tt}")
                if st:
                    source_types.add(st)
                if tt:
                    target_types.add(tt)

        if not source_types and not target_types:
            logger.info("No schema type hints (sourceType/targetType) found in YAML.")
            return None, None

        source_type = None
        target_type = None

        if source_types:
            if len(source_types) == 1:
                source_type = next(iter(source_types))
            else:
                logger.warning(f"Multiple sourceType values found in YAML: {sorted(source_types)}")

        if target_types:
            if len(target_types) == 1:
                target_type = next(iter(target_types))
            else:
                logger.warning(f"Multiple targetType values found in YAML: {sorted(target_types)}")

        logger.info(f"Extracted schema type hints from YAML: sourceType={source_type}, targetType={target_type}")
        return source_type, target_type

    except Exception as e:
        logger.error(f"Error extracting schema types from YAML file {yaml_file_path}: {e}")
        logger.error(f"Exception details: {traceback.format_exc()}")
        return None, None

def process_filter_clauses(filter_config, columns_info, table_id=None,
                           source_node_info=None, target_node_info=None):
    """
    Process filter clauses from YAML configuration into the required format.

    Builds the clause's column object as a full Column model (2.2.6.0):
    {id, tableId, name, position, dataType, charMaxLength, charOctetLength,
     charSet, collation, numPrec, numPrecRadix, numScale, datetimePrec,
     isPK, isIdentity, isNullable, ...}

    All filter values are converted to strings as required by the backend.
    Pass ``table_id`` (int) so the emitted column carries the required ``tableId``.
    Pass ``source_node_info`` and ``target_node_info`` so the column dataType is
    mapped to the target native type (e.g. varchar -> string for Couchbase).
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

        # Find the matching column from discovery
        matched_column = None
        if columns_info and 'columns' in columns_info:
            for col in columns_info['columns']:
                if col.get('name') == column_name:
                    matched_column = col
                    break

        fallback_type = clause.get('type', 'string')

        if matched_column is None:
            print(f"Warning: Column '{column_name}' not found in table columns, using minimal fallback")
            filter_column = {
                "id": 1,
                "name": column_name,
                "dataType": fallback_type
            }
            if table_id is not None:
                filter_column["tableId"] = int(table_id)
        else:
            # Map the source native dataType to the target native type when node info is available
            source_data_type = matched_column.get('dataType', fallback_type)
            if source_node_info and target_node_info:
                mapped_data_type = map_data_type(source_data_type, source_node_info, target_node_info)
            else:
                mapped_data_type = source_data_type

            # Full column object mirroring the entity's column payload (2.2.6.0 Column model)
            filter_column = {
                "id": matched_column.get('id'),
                "name": column_name,
                "position": matched_column.get('position', 0),
                "dataType": mapped_data_type,
                "isPK": matched_column.get('isPK', False),
                "isIdentity": matched_column.get('isIdentity', False),
                "isNullable": matched_column.get('isNullable', False),
            }
            if table_id is not None:
                filter_column["tableId"] = int(table_id)
            # Copy optional metadata fields when present
            for field in ['charMaxLength', 'charOctetLength', 'charSet', 'collation',
                          'numPrec', 'numPrecRadix', 'numScale', 'datetimePrec',
                          'udtCategory', 'udtName']:
                if field in matched_column and matched_column[field] is not None:
                    filter_column[field] = matched_column[field]

        filter_clause = {
            "column": filter_column,
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


def assign_entities_to_group(token, pipeline_id, group_id, entity_ids):
    """
    Assign entities to a group using the /assign endpoint
    Args:
        token: Authentication token
        pipeline_id: ID of the pipeline
        group_id: ID of the target group
        entity_ids: List of entity IDs to assign to the group
    """
    if not entity_ids:
        logger.warning("No entity IDs provided for group assignment")
        return False
    
    if not group_id or group_id == '_default':
        logger.debug("Skipping group assignment: no group ID or using default group")
        return False

    try:
        logger.debug(f"Assigning {len(entity_ids)} entities to group ID: {group_id}")
        logger.debug(f"Entities to assign: {entity_ids}")
        
        # Ensure we're using the correct endpoint format
        endpoint = f"/pipelines/{pipeline_id}/config/groups/{group_id}/assign"
        logger.debug(f"Using endpoint: {endpoint}")
        
        # Make sure entity_ids is a list of strings
        if not isinstance(entity_ids, list):
            entity_ids = [entity_ids]
            
        # Ensure all entity IDs are strings
        entity_ids = [str(eid) for eid in entity_ids]
        
        response = fetch_core_hub(
            endpoint,
            method='POST',
            token=token,
            body=entity_ids
        )
        
        logger.debug(f"Assignment response: {response}")
        logger.info(f"Successfully assigned {len(entity_ids)} entities to group {group_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to assign entities to group {group_id}")
        logger.error(f"Error details: {str(e)}")
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return False


def create_group(token, pipeline_id, group_name, description: Optional[str] = None, color: Optional[str] = None):
    """Create a new group in the pipeline if it doesn't exist.

    Returns the group ID or '_default' if group creation fails.
    """
    logger.debug(f"create_group called with name: '{group_name}' (type: {type(group_name)})")
    
    if not group_name or group_name == "_default":
        logger.debug("Using default group")
        return "_default"

    # Ensure group_name is a string
    group_name = str(group_name).strip()
    if not group_name:
        logger.warning("Empty group name provided, using default group")
        return "_default"
    
    # First, try to get existing groups
    try:
        # Get all groups for the pipeline
        groups = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/groups",
            method='GET',
            token=token
        )
        
        # Check if group already exists
        for group in groups:
            if group.get('name') == group_name:
                group_id = group.get('groupId')
                logger.info(f"Found existing group '{group_name}' with ID: {group_id}")
                return group_id
                
        # If we get here, group doesn't exist yet, so create it
        # Generate a random color in hex format (e.g., #RRGGBBAA)
        def random_color():
            return f"#{random.randint(0, 255):02x}{random.randint(0, 255):02x}{random.randint(0, 255):02x}ff"
            
        group_data = {
            "groupId": "",  # Will be generated by the server
            "name": group_name,
            "description": description if description is not None else "",
            "color": color if color is not None else random_color(),
        }
        
        logger.info(f"Creating new group: {group_name}")
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/groups",
            method='PUT',
            token=token,
            body=group_data
        )
        
        if response and 'groupId' in response:
            group_id = response['groupId']
            logger.info(f"Successfully created group '{group_name}' with ID: {group_id}")
            return group_id
            
        logger.warning(f"Failed to create group '{group_name}': Unexpected response format")
        return "_default"
        
    except Exception as e:
        logger.warning(f"Error in create_group for '{group_name}': {str(e)}")
        logger.debug(f"Error details: {traceback.format_exc()}")
        return "_default"

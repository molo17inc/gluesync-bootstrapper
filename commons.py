import os
import json
import time
import uuid
import traceback
import yaml
from utils.log import get_logger, log_success, log_failure
from utils.chronos_client import ChronosClient
from utils.core_hub_client import CoreHubClient

CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENABLE_SCHEDULING = os.getenv('ENABLE_SCHEDULING', 'true').lower() == 'true'
CHRONOS_URL = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

logger = get_logger()


def fetch_core_hub(path, method='GET', token=None, body=None, params=None):
    # Log request details
    logger.debug(f"\n{'=' * 80}")
    logger.debug(f"[API REQUEST] {method.upper()} {path}")

    if params:
        logger.debug("\nQuery Parameters:")
        for k, v in (params.items() if params else {}):
            logger.debug(f"  {k}: {v}")

    if body is not None:
        logger.debug("\nRequest Body:")
        try:
            logger.debug(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body))
        except Exception as e:
            logger.debug(f"<Unable to serialize request body: {e}>")

    logger.debug("-" * 40)

    try:
        # Make the request
        start_time = time.time()
        response = core_hub_client.request(path, method, token, body, params)
        duration = time.time() - start_time

        # Log response
        logger.debug(f"Request completed in {duration:.3f}s")
        logger.debug(f"[RESPONSE] {method.upper()} {path}")
        logger.debug(f"Response (first 1000 chars): {str(response)[:1000]}")
        logger.debug("=" * 80 + "\n")

        return response

    except Exception as e:
        logger.error(f"API request failed: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_body = e.response.json()
                logger.error(f"Error response: {json.dumps(error_body, indent=2)}")
            except:
                logger.error(f"Error response: {e.response.text}")
        logger.debug("=" * 80 + "\n")
        raise


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


def get_node_info(token, pipeline_id, agent_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info", token=token)


def create_entity_schedules(token, pipeline_id, entity_id, entity_name, schedules_config):
    """Create schedules for an entity based on the YAML configuration."""
    if not schedules_config or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating schedules for entity {entity_name} (ID: {entity_id})")

    chronos_client = ChronosClient(CHRONOS_URL)

    for schedule_config in schedules_config:
        try:
            # Extract schedule parameters
            task_type = schedule_config.get('task_type')
            name = schedule_config.get('name')
            description = schedule_config.get('description')
            with_snapshot = schedule_config.get('with_snapshot', False)
            enabled = schedule_config.get('enabled', True)

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
                enabled=enabled
            )

            log_success(logger, f"Created {task_type} schedule for entity {entity_name}: {name}")
            logger.debug(f"Schedule details: {json.dumps(result)}")

        except Exception as e:
            error_msg = f"Failed to create schedule for entity {entity_name}: {str(e)}"
            log_failure(logger, error_msg)
            logger.error(traceback.format_exc())
            # Continue creating other schedules even if one fails


def create_pipeline_schedules(token, pipeline_id, pipeline_schedules):
    """Create schedules for the entire pipeline based on the YAML configuration."""
    if not pipeline_schedules or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating pipeline-level schedules for pipeline {pipeline_id}")

    chronos_client = ChronosClient(CHRONOS_URL)

    for schedule_config in pipeline_schedules:
        try:
            # Extract schedule parameters
            task_type = schedule_config.get('task_type')
            name = schedule_config.get('name')
            description = schedule_config.get('description')
            with_snapshot = schedule_config.get('with_snapshot', False)
            enabled = schedule_config.get('enabled', True)

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
                enabled=enabled
            )

            log_success(logger, f"Created {task_type} schedule for pipeline {pipeline_id}: {name}")
            logger.debug(f"Schedule details: {json.dumps(result)}")

        except Exception as e:
            error_msg = f"Failed to create pipeline schedule: {str(e)}"
            log_failure(logger, error_msg)
            logger.error(traceback.format_exc())
            # Continue creating other schedules even if one fails


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
    print(f"Matched Gluesync data type: {source_gluesync_type}")

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

    print(
        f"Warning: No target mapping found for Gluesync type {source_gluesync_type}. Using source type {source_type} as is.")
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

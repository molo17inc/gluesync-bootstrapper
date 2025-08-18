import colorsys
import os
import json
import random
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
    """Create schedules for groups based on the YAML configuration."""
    if not group_schedules or not ENABLE_SCHEDULING:
        return

    logger.info(f"Creating group-level schedules for pipeline {pipeline_id}")

    chronos_client = ChronosClient(CHRONOS_URL)

    for group_id, schedules_config in group_schedules.items():
        logger.info(f"Processing schedules for group: {group_id}")
        
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
                        f"Schedule for group {group_id} is missing both 'cron_expression' and 'schedule'. Skipping.")
                    continue

                # Create the schedule
                result = chronos_client.create_group_schedule(
                    pipeline_id=pipeline_id,
                    group_id=group_id,
                    task_type=task_type,
                    schedule_config=schedule_data,
                    name=name,
                    description=description,
                    with_snapshot=with_snapshot,
                    enabled=enabled
                )

                log_success(logger, f"Created {task_type} schedule for group {group_id}: {name}")
                logger.debug(f"Schedule details: {json.dumps(result)}")

            except Exception as e:
                error_msg = f"Failed to create schedule for group {group_id}: {str(e)}"
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
                if 'string' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type == 'json':
            # Try to find JSON type first, fall back to STRING
            for t in target_item['supportedTypes']:
                if 'json' == t.lower():
                    print(f"Mapping json to {t}")
                    return t
                elif 'clob' == t.lower():
                    print(f"Mapping json to {t}")
                    return t
            for t in target_item['supportedTypes']:
                if 'string' == t.lower():
                    print(f"Mapping json to {t} (fallback)")
                    return t
        elif normalized_source_type == 'bit':
            # Try to find BOOLEAN type first, fall back to INT
            for t in target_item['supportedTypes']:
                if 'boolean' == t.lower():
                    print(f"Mapping bit to {t}")
                    return t
            for t in target_item['supportedTypes']:
                if 'int' == t.lower():
                    print(f"Mapping bit to {t} (fallback)")
                    return t
        elif normalized_source_type in ['tinyint', 'smallint', 'mediumint']:
            # Find appropriate INT type
            for t in target_item['supportedTypes']:
                if 'int' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type in ['blob', 'smallblob', 'mediumblob']:
            for t in target_item['supportedTypes']:
                if 'varbinary' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type in ['longtext']:
            for t in target_item['supportedTypes']:
                if 'varchar' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type in ['datetime']:
            for t in target_item['supportedTypes']:
                if 'timestamp with time zone' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type in ['bigint']:
            for t in target_item['supportedTypes']:
                if 'number' == t.lower():
                    print(f"Mapping {normalized_source_type} to {t}")
                    return t
        elif normalized_source_type in ['time']:
            for t in target_item['supportedTypes']:
                if 'timestamp with local time zone' == t.lower():
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


def create_group(token, pipeline_id, group_name):
    """
    Create a new group in the pipeline if it doesn't exist.
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
        group_data = {
            "groupId": "",  # Will be generated by the server
            "name": group_name,
            "description": "",
            "color": "#ff1792cd"  # Default color from the logs
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

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
import yaml
import requests
from add_agents_with_conductor import add_agents_with_conductor
from faker import Faker
import urllib.parse
import time
import subprocess
import uuid
import urllib3
import ssl
import secrets
import string
import traceback
import argparse
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from urllib.parse import urlparse
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, exit_on_fail, lockfile_complete
from utils.gluesync_sdk_client import initialize_gluesync_sdk, get_token, get_gluesync_client
from utils.core_hub_client import CoreHubClient
from commons import extract_schemas_from_yaml, extract_all_schemas_from_yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Core hub client accessor
def get_core_hub_client():
    global _core_hub_client
    if _core_hub_client is None:
        core_hub_url = os.getenv('CORE_HUB_URL', 'http://localhost:1717')
        _core_hub_client = CoreHubClient(core_hub_url)
    return _core_hub_client

# def get_conductor_client():
#     global _conductor_client
#     if _conductor_client is None:
#         conductor_url = os.getenv('CONDUCTOR_URL', 'http://gluesync-conductor:1717')
#         _conductor_client = ConductorClient(conductor_url)
#     return _conductor_client


def set_core_hub_client(client):
    global _core_hub_client
    _core_hub_client = client

# Private module variable
_core_hub_client = None

# Define this function early since it's used throughout the code
def fetch_core_hub(path, method='GET', token=None, body=None, params=None):
    client = get_core_hub_client()
    return client.request(path, method, token, body, params)

fake = Faker()

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables and constants
file_conf_path = os.getenv('FILE_CONF_PATH', './config.json')

# Retrieve CoreHub URL from SDK if not specified
core_hub_url = os.getenv('CORE_HUB_URL')

conductor_url = os.getenv('CONDUCTOR_URL', 'http://gluesync-conductor:5017')

handle_with_conductor = os.getenv('HANDLE_WITH_CONDUCTOR', 'false').lower() == 'true'

# use_sdk variable is now defined earlier in the code

if not core_hub_url:
    if use_sdk:
        logger.info("No CoreHub URL specified, attempting to discover via SDK.")
        sdk_client = get_gluesync_client()
        if sdk_client:
            core_hub_url = sdk_client.get_core_hub_url()
            if core_hub_url:
                logger.info(f"Discovered CoreHub URL via SDK: {core_hub_url}")
            else:
                logger.warning("Failed to discover CoreHub URL via SDK, using default.")
        else:
            logger.error("SDK client not initialized, cannot discover CoreHub URL.")
    else:
        logger.info("USE_SDK is false, skipping SDK CoreHub URL discovery.")

# Fallback to default CoreHub URL
if not core_hub_url:
    core_hub_url = 'http://gluesync-core-hub:1717'
    logger.info(f"Using default CoreHub URL: {core_hub_url}")

default_user = 'admin'
default_password = ''
user_defined_password = os.getenv('DEFAULT_PASSWORD', default_password)
source_type = os.getenv('SOURCE_TYPE', 'SQL')
target_type = os.getenv('TARGET_TYPE', 'NoSQL')
TABLE_LIST_YAML = os.getenv('TABLE_LIST_YAML', '/opt/config/tables-list.yaml')

# Schema extraction will be done after logger initialization
AUTH_TOKEN_PATH = os.path.join('/opt/config', 'auth_token.json')

ENTITY_START_TIMEOUT = 1

# CoreHubClient and ProtocolAwareAdapter have been moved to utils/core_hub_client.py

def generate_fancy_names(length):
    return [fake.catch_phrase() for _ in range(length)]

def safe_encode(s):
    return urllib.parse.quote(s, safe='')

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]

def generate_random_password() -> str:
    symbols = [chr(i) for i in range(33, 47)]
    password = ""
    for _ in range(9):
        password += secrets.choice(string.ascii_lowercase)
    password += secrets.choice(string.ascii_uppercase)
    password += secrets.choice(string.digits)
    password += secrets.choice(symbols)
    return password


def save_token(token):
    """Save the authentication token to a JSON file in the config directory."""
    try:
        # Create config directory if it doesn't exist
        os.makedirs(os.path.dirname(AUTH_TOKEN_PATH), exist_ok=True)

        # Create token JSON structure
        token_data = {
            "token": token
        }

        with open(AUTH_TOKEN_PATH, 'w') as f:
            json.dump(token_data, f, indent=2)
        print(f"Authentication token saved successfully to {AUTH_TOKEN_PATH}")
    except Exception as e:
        print(f"Warning: Failed to save authentication token: {e}")

# Retrieve use_sdk from environment early to avoid unnecessary imports/checks
use_sdk = os.getenv('USE_SDK', 'False').lower() in ['true', '1', 't', 'y', 'yes']

# Only check license file and initialize SDK if USE_SDK is true
if use_sdk:
    # Check if the license file exists before initializing the SDK
    license_file_path = os.getenv('GLUESYNC_LICENSE_FILE', '/opt/gluesync/data/gs-license.dat')

    if not os.path.exists(license_file_path):
        logger.error(f"License file not found at {license_file_path}. Cannot proceed with SDK initialization.")
        exit(1)

    try:
        from gluesync_sdk import GluesyncSDK
    except ModuleNotFoundError:
        logger.error("gluesync_sdk module not found. Make sure it's installed as a submodule.")
        # Handle the absence of the SDK appropriately, e.g., set a flag or use a mock
        GluesyncSDK = None

    # Initialize the Gluesync SDK client
    initialize_gluesync_sdk()
else:
    logger.info("Skipping SDK initialization as USE_SDK is set to false")

# Global initialization complete
if not handle_with_conductor:
    # Determine authentication method
    if not use_sdk:
        logger.info("Using manual authentication with provided password or token.")
        # Existing authentication logic
        try:
            with open(file_conf_path, 'r') as file:
                conf_test = json.load(file)
            logger.info(f"Loaded configuration from {file_conf_path}")
        except Exception as e:
            log_failure(logger, f"Failed to load configuration: {str(e)}")
            lockfile_failure()

        # Check if a valid token is present
        try:
            with open(AUTH_TOKEN_PATH, 'r') as f:
                token_data = json.load(f)
                token = token_data.get('token')
                if token:
                    # Verify login by attempting to authenticate
                    try:
                        check_token = fetch_core_hub(
                            '/pipelines',
                            method='GET',
                            token=token
                        )
                        if isinstance(check_token, list):
                            log_success(logger, "Successfully authenticated with saved token")
                    except Exception as e:
                        if "401" in str(e):
                            logger.warning("Saved token is invalid, attempting to authenticate with default credentials")
                            token = None
                        else:
                            raise e
        except FileNotFoundError:
            logger.info("No saved token found, attempting to authenticate with default credentials")
            token = None

        if not token:
            # Initial authentication
            auth_response = fetch_core_hub(
                '/authentication/login',
                method='POST',
                body={'username': default_user, 'password': user_defined_password}
            )
            token = auth_response.get('apiToken')
            if not token:
                log_failure(logger, "Failed to authenticate")
                lockfile_failure()
                raise Exception('Failed to authenticate')

            change_required = auth_response.get('changeRequired', False)
            if change_required:
                logger.info("Password change required")
                # Generate a new random password and change it
                new_password = generate_random_password()
                try:
                    # Change password and get new token
                    token = change_password(token, user_defined_password, new_password)
                    log_success(logger, f"Successfully changed password to: {new_password}")
                except Exception as e:
                    log_failure(logger, f"Password change failed, attempting to continue with default password: {str(e)}")
                    # Try to get a fresh token with the user-defined password
                    auth_response = fetch_core_hub(
                        '/authentication/login',
                        method='POST',
                        body={'username': default_user, 'password': user_defined_password}
                    )
                    token = auth_response.get('apiToken')
                    if not token:
                        log_failure(logger, "Failed to re-authenticate with user-defined password")
                        lockfile_failure()
                        raise Exception('Failed to re-authenticate with user-defined password')
                    new_password = user_defined_password
            else:
                new_password = user_defined_password
                # Save the initial token if no password change was required
                save_token(token)

        # Use the token for CoreHubClient
        set_core_hub_client(CoreHubClient(core_hub_url))
    else:
        # If we're using SDK, still need to set the core_hub_client for API calls
        logger.info("Using Gluesync SDK for authentication.")
        # Make sure we have a client
        if get_core_hub_client() is None:
            set_core_hub_client(CoreHubClient(core_hub_url))
        try:
            # Get the SDK client for inspection
            sdk_client = get_gluesync_client()
            if sdk_client:
                logger.debug(f"SDK client type: {type(sdk_client).__name__}")
                # Check if client has token-related attributes
                token_attrs = [attr for attr in dir(sdk_client) if 'token' in attr.lower()]
                if token_attrs:
                    logger.debug(f"Token-related attributes in SDK client: {token_attrs}")
            
            # Try to get the token
            token = get_token()
            logger.debug(f"Token retrieved: {token is not None}")
            
            if not token:
                logger.error("Failed to obtain token from Gluesync SDK - token is None.")
                # Check for any alternative token access methods
                if sdk_client and hasattr(sdk_client, 'token'):
                    logger.debug("Trying to access token via property...")
                    token = sdk_client.token
                    logger.debug(f"Token via property: {token is not None}")
                
                if not token:
                    logger.error("All token retrieval methods failed.")
                    exit(1)
            else:
                logger.info("Successfully obtained token from Gluesync SDK.")
                # Save the token
                save_token(token)
        except Exception as e:
            logger.error(f"Exception during token retrieval: {str(e)}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.error(f"Stack trace: {traceback.format_exc()}")
            exit(1)

        # Use the token for CoreHubClient
        # Set the global core_hub_client using the existing get_core_hub_client function
        core_hub_client = get_core_hub_client()

    def get_entities(token, pipeline_id):
        response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
        logger.debug(f"Retrieved the following entities: {response}")

        if not isinstance(response, list) or not response:
            logger.warning(f"Unexpected response when fetching entities: {response}")
            return []

        entities = []
        for item in response:
            if 'entity' in item and isinstance(item['entity'], dict):
                entity = item['entity']
                if 'entityId' in entity and 'entityName' in entity:
                    entities.append({
                        'entityId': entity['entityId'],
                        'entityName': entity['entityName']
                    })

        return entities
    
    def get_snapshot_write_method_from_yaml(entity_name, yaml_path):
        """Extract snapshotWriteMethod for a specific entity from YAML configuration."""
        try:
            # Parse entity name to get schema and table
            parts = entity_name.split('.')
            if len(parts) != 2:
                return 'UPSERT'  # Default
            
            schema_name, table_name = parts
            
            # Load YAML configuration
            with open(yaml_path, 'r') as file:
                yaml_content = yaml.safe_load(file)
            
            # Navigate to the table configuration
            if schema_name in yaml_content:
                schema_config = yaml_content[schema_name]
                if 'tables' in schema_config and 'custom' in schema_config['tables']:
                    custom_tables = schema_config['tables']['custom']
                    if table_name in custom_tables:
                        table_config = custom_tables[table_name]
                        return table_config.get('snapshotWriteMethod', 'UPSERT')
            
            return 'UPSERT'  # Default if not found
            
        except Exception as e:
            logger.debug(f"Could not get snapshotWriteMethod from YAML for {entity_name}: {e}")
            return 'UPSERT'  # Default on error

    def configure_entities(agents_to_conf, pipeline_id, token):
        entities_payload = {"entities": []}

        for agent in agents_to_conf:
            for entity in agent['entities']:
                # Find existing entity or create a new one
                existing_entity = next((e for e in entities_payload["entities"] if e["entityName"] == entity["entityName"]), None)
                if existing_entity is None:
                    existing_entity = {
                        "entityId": str(uuid.uuid4()),  # Generate a new ID for the entity
                        "entityName": entity["entityName"],
                        "agentEntities": []
                    }
                    entities_payload["entities"].append(existing_entity)

                # Add the agentEntity to the entity
                existing_entity["agentEntities"].append({
                    "type": entity["type"],
                    "entityType": entity["entityType"],
                    "agentId": agent['agentId'],
                    "customProperties": entity.get("customProperties", {}),
                    "tablesProperties": entity.get("tablesProperties", {}),
                    "table": entity.get("table", {}),
                    "columns": entity.get("columns", []),
                    "keys": entity.get("keys", [])
                })

        fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/entities",
            method='PUT',
            token=token,
            body=entities_payload
        )

    def start_entity_syncs(token, pipeline_id):
        entities = get_entities(token, pipeline_id)
        print(f"Retrieved the following entities: {entities}")
        
        # Get YAML path from environment or use default
        yaml_path = os.environ.get('TABLE_LIST_YAML', '/opt/config/TABLE_LIST.yaml')

        for entity in entities:
            entityId = entity['entityId']
            entityName = entity['entityName']
            
            # TODO: Implement snapshotWriteMethod when API supports it
            # Currently the API doesn't accept snapshotWriteMethod as a query parameter
            # Get the snapshotWriteMethod from YAML configuration
            # snapshot_write_method = get_snapshot_write_method_from_yaml(entityName, yaml_path)
            # if snapshot_write_method != 'UPSERT':
            #     logger.info(f"Using snapshotWriteMethod '{snapshot_write_method}' for entity {entityName}")

            try:
                encoded_entity_id = safe_encode(entityId)
                
                # Build query parameters - currently only with entity ID
                # TODO: Add snapshotWriteMethod when API supports it
                query_params = f"entity={encoded_entity_id}"
                # Future: query_params = f"entity={encoded_entity_id}&snapshotWriteMethod={snapshot_write_method}"

                response = fetch_core_hub(
                    f"/pipelines/{pipeline_id}/commands/sync/start?withSnapshot=true&{query_params}",
                    method='POST',
                    token=token
                )
                print(f"Started sync for entity: {entityName} (ID: {entityId})")
                print(f"Response: {response}")

                time.sleep(ENTITY_START_TIMEOUT)
            except Exception as e:
                print(f"Error starting sync for entity {entityName} (ID: {entityId}): {str(e)}")
                # Log the error using the enhanced logging framework
                log_failure(logger, f"Failed to start sync for entity {entityName}")

    def change_password(token, old_password, new_password):
        """Change the user password and return the new token."""
        response = fetch_core_hub(
            '/authentication/reset-password',
            method='POST',
            token=token,
            body={
                'oldPassword': old_password,
                'newPassword': new_password
            }
        )

        if response != "Password changed":
            raise Exception(f"Unexpected response from password reset: {response}")

        print("Password reset successful")

        # Re-authenticate with the new password to get a fresh token
        auth_response = fetch_core_hub(
            '/authentication/login',
            method='POST',
            body={'username': default_user, 'password': new_password}
        )
        new_token = auth_response.get('apiToken')
        if not new_token:
            raise Exception('Failed to re-authenticate after password change')

        change_required = auth_response.get('changeRequired', False)
        if change_required:
            raise Exception('Password change still required after reset')

        # Save the new token
        save_token(new_token)

        return new_token

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Gluesync Bootstrapper')
    parser.add_argument('--pipeline-name', type=str, help='Optional name for the pipeline')
    args = parser.parse_args()
    
    # Display ASCII art at startup
    ascii_art = """
                                                                                                        
██████   ██████   ██████  ████████ ███████ ████████ ██████   █████  ██████  ██████  ███████ ██████  
██   ██ ██    ██ ██    ██    ██    ██         ██    ██   ██ ██   ██ ██   ██ ██   ██ ██      ██   ██ 
██████  ██    ██ ██    ██    ██    ███████    ██    ██████  ███████ ██████  ██████  █████   ██████  
██   ██ ██    ██ ██    ██    ██         ██    ██    ██   ██ ██   ██ ██      ██      ██      ██   ██ 
██████   ██████   ██████     ██    ███████    ██    ██   ██ ██   ██ ██      ██      ███████ ██   ██ 
                                                                                                    
                                               
"""
    logger.info("\n" + ascii_art)
    if handle_with_conductor:
        logger.info("Starting Gluesync Bootstrapper module in CONDUCTOR-ONLY mode...")
        logger.info("Adding agents to Conductor and starting containers …")
        reply = add_agents_with_conductor(file_conf_path, conductor_url)

        if "error" in reply:
            logger.error(f"Failed: {reply['error']}")
            lockfile_failure()
            raise RuntimeError(reply["error"])

        logger.info(f"POST /services accepted {len(reply['services'])} definition(s)")
        if reply["containers_started"]:
            logger.info("Containers started successfully; they will shortly appear in /containers")
        else:
            logger.warning(f"Container start failed: {reply.get('start_error')}")

        logger.debug("Full reply:\n" + json.dumps(reply, indent=2))

        print("Waiting 10 seconds before returning to let agents warm up...")
        time.sleep(10)
        print("Starting bootstrapper now...")

        # If this is conductor-only mode, exit here
        log_success(logger, "Conductor-only operations completed successfully")
        lockfile_complete()
        return

    else:
        logger.info("Starting Gluesync Bootstrapper module in STANDARD mode...")

        # Extract schema information from YAML file
        logger.info(f"Attempting to extract schemas from YAML file: {TABLE_LIST_YAML}")
        schema_pairs = extract_all_schemas_from_yaml(TABLE_LIST_YAML)
        if not schema_pairs:
            logger.warning("No schemas found in YAML file. Entity creation will be skipped.")
        else:
            logger.info(f"Schema extraction successful. {len(schema_pairs)} schema pair(s) found: {schema_pairs}")

        # Initialize variables that might be used in different code paths
        change_required = False
        new_password = user_defined_password
        
        try:
            with open(file_conf_path, 'r') as file:
                conf_test = json.load(file)
            logger.info(f"Loaded configuration from {file_conf_path}")
        except Exception as e:
            log_failure(logger, f"Failed to load configuration: {str(e)}")
            lockfile_failure()
            return
        
        # First check if we have a valid SDK token
        sdk_token = None
        if not use_sdk:
            try:
                # Try to get token from SDK first if available
                if 'GluesyncSDK' in globals():
                    logger.info("Attempting to get token from Gluesync SDK")
                    sdk_client = get_gluesync_client()
                    if sdk_client:
                        sdk_token = get_token()
                        if sdk_token:
                            logger.info("Successfully retrieved token from Gluesync SDK")
                            # Verify the SDK token works
                            try:
                                check_token = fetch_core_hub(
                                    '/pipelines',
                                    method='GET',
                                    token=sdk_token
                                )
                                if isinstance(check_token, list):
                                    log_success(logger, "Successfully authenticated with SDK token")
                                    token = sdk_token  # Use the SDK token for all subsequent requests
                                else:
                                    logger.warning("SDK token verification returned unexpected response")
                                sdk_token = None
                            except Exception as e:
                                logger.warning(f"SDK token verification failed: {str(e)}")
                                sdk_token = None
            except Exception as e:
                logger.warning(f"Error retrieving SDK token: {str(e)}")
                sdk_token = None
        
        # If SDK token is valid, use it
        if sdk_token:
            token = sdk_token
        else:
            # Otherwise, check if a valid saved token is present
            try:
                with open(AUTH_TOKEN_PATH, 'r') as f:
                    token_data = json.load(f)
                    token = token_data.get('token')
                    if token:
                        # Verify login by attempting to authenticate
                        try:
                            check_token = fetch_core_hub(
                                '/pipelines',
                                method='GET',
                                token=token
                            )
                            if isinstance(check_token, list):
                                log_success(logger, "Successfully authenticated with saved token")
                        except Exception as e:
                            if "401" in str(e):
                                logger.warning("Saved token is invalid, attempting to authenticate with default credentials")
                                token = None
                            else:
                                raise e
            except FileNotFoundError:
                logger.info("No saved token found, attempting to authenticate with default credentials")
                token = None

            if not token:
                # Initial authentication with default credentials as last resort
                logger.info("Attempting authentication with default credentials")
                try:
                    auth_response = fetch_core_hub(
                        '/authentication/login',
                        method='POST',
                        body={'username': default_user, 'password': user_defined_password}
                    )
                    token = auth_response.get('apiToken')

                    change_required = auth_response.get('changeRequired', False)

                    if not change_required == False and not token:
                        log_failure(logger, "Failed to authenticate")
                        lockfile_failure()
                        raise Exception('Failed to authenticate')
                except Exception as e:
                    if sdk_token:
                        # If we have an SDK token but direct auth failed, use the SDK token
                        logger.info("Using SDK token after direct authentication failure")
                        token = sdk_token
                    else:
                        # If all authentication methods failed, raise the exception
                        raise e

            if change_required:
                logger.info("Password change required")
                # Generate a new random password and change it
                new_password = generate_random_password()
                try:
                    # Change password and get new token
                    token = change_password(token, user_defined_password, new_password)
                    log_success(logger, f"Successfully changed password to: {new_password}")
                except Exception as e:
                    log_failure(logger, f"Password change failed, attempting to continue with default password: {str(e)}")
                    # Try to get a fresh token with the user-defined password
                    auth_response = fetch_core_hub(
                        '/authentication/login',
                        method='POST',
                        body={'username': default_user, 'password': user_defined_password}
                    )
                    token = auth_response.get('apiToken')
                    if not token:
                        log_failure(logger, "Failed to re-authenticate with user-defined password")
                        lockfile_failure()
                        raise Exception('Failed to re-authenticate with user-defined password')
                    new_password = user_defined_password
            else:
                new_password = user_defined_password
                # Save the initial token if no password change was required
                save_token(token)

        # List unassigned agents
        unassigned_agents = fetch_core_hub('/unassigned-agents', token=token)
        if not isinstance(unassigned_agents, list):
            log_failure(logger, "Failed to retrieve unassigned agents")
            lockfile_failure()
            raise Exception('Failed to retrieve unassigned agents')

        logger.info(f"Found {len(unassigned_agents)} unassigned agents")
        logger.debug(f"Unassigned agents: {json.dumps(unassigned_agents, indent=2)}")
        logger.debug(f"Config agents: {json.dumps(conf_test['agents'], indent=2)}")

        # Use provided pipeline name or generate a fancy one
        if args.pipeline_name:
            pipeline_name = args.pipeline_name
            pipeline_description = f"Pipeline {pipeline_name}"
        else:
            fancy_names = generate_fancy_names(2)
            pipeline_name = fancy_names[0]
            pipeline_description = fancy_names[1]

        # Create pipeline
        pipeline_response = fetch_core_hub(
            '/pipelines',
            method='POST',
            token=token,
            body={'name': pipeline_name, 'description': pipeline_description, 'configurationCompleted': False}
        )
        pipeline_id = pipeline_response.get('pipelineId')
        if not pipeline_id:
            log_failure(logger, "Failed to create pipeline")
            lockfile_failure()
            raise Exception('Failed to create pipeline')

        # Filter agents to configure
        agents_to_conf = [
            {
                'agentId': agent['agentId'],
                'agentType': agent['agentType'],
                'agentTag': agent['agentTag'],
                'hostCredentials': conf_agent['hostCredentials'],
                'customHostCredentials': conf_agent['customHostCredentials'],
                'specificConfiguration': conf_agent['specificConfiguration'],
                'entities': conf_agent['entities']
            }
            for conf_agent in conf_test['agents']
            for agent in unassigned_agents
            if agent['agentTag'] == conf_agent['agentTag'] and agent['agentType'] == conf_agent['agentType']
        ]

        logger.info(f"Filtered {len(agents_to_conf)} agents for configuration")
        logger.debug(f"Filtered agents: {json.dumps(agents_to_conf, indent=2)}")

        # Assign agents to pipeline
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                logger.warning(f"Agent missing 'agentId' field: {agent}")
                continue

            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}",
                method='PUT',
                token=token
            )

        # Apply agent host credentials
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                logger.warning(f"Agent missing 'agentId' field: {agent}")
                continue

            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/credentials",
                method='PUT',
                token=token,
                body={
                    'hostCredentials': agent['hostCredentials'],
                    'customHostCredentials': agent['customHostCredentials']
                }
            )

        # Apply agent specific configuration
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                logger.warning(f"Agent missing 'agentId' field: {agent}")
                continue

            if agent['specificConfiguration']:
                fetch_core_hub(
                    f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/specific",
                    method='PUT',
                    token=token,
                    body={"configuration": agent['specificConfiguration']}
                )

        configure_entities(agents_to_conf, pipeline_id, token)

        # Create entities sequentially for each schema pair
        if schema_pairs:
            entity_creation_script = 'create_all_entities.py'
            for idx, (source_schema, target_schema) in enumerate(schema_pairs, start=1):
                logger.info(f"[Schema {idx}/{len(schema_pairs)}] Starting entity creation for source='{source_schema}', target='{target_schema}'")
                try:
                    cmd = [
                        'python3',
                        entity_creation_script,
                        '--pipeline', pipeline_id,
                        '--source-schema', source_schema,
                        '--source-type', source_type,
                        '--target-type', target_type,
                        '--token', token,
                        '--target-schema', target_schema or source_schema
                    ]

                    if os.path.exists(TABLE_LIST_YAML):
                        cmd.extend(['--yaml-file', TABLE_LIST_YAML])
                        logger.info(f"Using TABLE_LIST.yaml: {TABLE_LIST_YAML}")
                    else:
                        logger.warning(f"TABLE_LIST.yaml not found at {TABLE_LIST_YAML}. Proceeding without it.")

                    subprocess.run(cmd, check=True)
                    log_success(logger, f"[Schema {idx}/{len(schema_pairs)}] Entity creation completed for source {source_schema} -> target {target_schema or source_schema}")
                except subprocess.CalledProcessError as e:
                    log_failure(logger, f"[Schema {idx}/{len(schema_pairs)}] Error running entity creation script: {e}")
                    # Continue with next schema without failing entire process
                    continue

        try:
            # Set pipeline as ready (exiting from Draft status)
            fetch_core_hub(
                    f"/pipelines/{pipeline_id}",
                    method='PUT',
                    token=token,
                    body={'configurationCompleted': True, 'name': fancy_names[0]}
            )

            time.sleep(ENTITY_START_TIMEOUT)

            start_entity_syncs(token, pipeline_id)

            # Log successful completion
            log_success(logger, f"Pipeline {pipeline_id} successfully configured and started")
            lockfile_complete()
        except Exception as error:
            log_failure(logger, f"Error: {error}")
            lockfile_failure()

if __name__ == "__main__":
    main()


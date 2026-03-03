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
import yaml
import requests
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
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from urllib.parse import urlparse
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, exit_on_fail, lockfile_complete
from utils.gluesync_sdk_client import initialize_gluesync_sdk, get_token, get_gluesync_client
from utils.core_hub_client import CoreHubClient
from commons import extract_schemas_from_yaml, extract_all_schemas_from_yaml, extract_schema_types_from_yaml, configure_core_hub

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
def wait_for_corehub_ready(max_retries=30, initial_delay=2):
    """Wait for CoreHub to be ready with exponential backoff.
    
    Args:
        max_retries: Maximum number of connection attempts (default: 30)
        initial_delay: Initial delay in seconds between retries (default: 2)
    
    Returns:
        True if CoreHub is ready, raises exception if max retries exceeded
    """
    client = get_core_hub_client()
    delay = initial_delay
    max_delay = 30  # Cap maximum delay at 30 seconds
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Checking CoreHub connectivity (attempt {attempt}/{max_retries})...")
            # Simple health check - try to fetch pipelines endpoint
            response = client.request('/pipelines', 'GET', token=None, body=None, params=None)
            logger.info("CoreHub is ready and responding")
            return True
        except Exception as e:
            error_msg = str(e)
            if "Connection refused" in error_msg or "Failed to establish" in error_msg:
                if attempt < max_retries:
                    logger.warning(
                        f"CoreHub not ready yet (attempt {attempt}/{max_retries}). "
                        f"Retrying in {delay} seconds... Error: {error_msg}"
                    )
                    time.sleep(delay)
                    # Exponential backoff with cap
                    delay = min(delay * 1.5, max_delay)
                else:
                    logger.error(
                        f"CoreHub failed to respond after {max_retries} attempts. "
                        f"Please check that CoreHub service is running and accessible."
                    )
                    raise RuntimeError(
                        f"CoreHub at {client.base_url} is not accessible after {max_retries} attempts. "
                        f"Last error: {error_msg}"
                    ) from e
            else:
                # Different error - might be auth related, let it through
                logger.info(f"CoreHub responded (got error: {error_msg}), proceeding with authentication")
                return True
    
    raise RuntimeError(f"CoreHub connection check failed after {max_retries} attempts")


def fetch_core_hub(path, method='GET', token=None, body=None, params=None):
    client = get_core_hub_client()
    return client.request(path, method, token, body, params)

fake = Faker()

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

_AGENT_TYPE_BY_NAME_BOOT: dict[str, str] | None = None


def _load_agent_type_catalog_for_bootstrapper() -> dict[str, str]:
    """Load agents.json catalog and normalize types to SQL/NoSQL for main.py.

    This mirrors the logic used by the export and Automator code paths so that
    SOURCE_TYPE / TARGET_TYPE can be omitted and inferred from configured agents.
    """

    global _AGENT_TYPE_BY_NAME_BOOT
    if _AGENT_TYPE_BY_NAME_BOOT is not None:
        return _AGENT_TYPE_BY_NAME_BOOT

    mapping: dict[str, str] = {}

    try:
        base_dir = Path(__file__).resolve().parent
        json_path = base_dir / "agents.json"
        if not json_path.exists():
            _AGENT_TYPE_BY_NAME_BOOT = mapping
            return mapping

        with json_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

        items = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            _AGENT_TYPE_BY_NAME_BOOT = mapping
            return mapping

        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = entry.get("internalName")
            kind = entry.get("type")
            if not name or not kind:
                continue
            label = str(kind).strip().upper()
            if not label:
                continue
            if label == "RDBMS":
                normalized = "SQL"
            else:
                normalized = "NoSQL"
            mapping[str(name).lower()] = normalized
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to load agents.json catalog for type inference in bootstrapper: %s", exc)
        mapping = {}

    _AGENT_TYPE_BY_NAME_BOOT = mapping
    return mapping


def infer_pipeline_schema_types_from_config(conf: dict) -> tuple[str, str]:
    """Infer (sourceType, targetType) as SQL/NoSQL from config agents.

    Uses agents.json as the primary source of truth and falls back to SQL when
    an agent tag is unknown. Returns (source_type, target_type).
    """

    catalog = _load_agent_type_catalog_for_bootstrapper()
    if not isinstance(conf, dict):
        return "SQL", "SQL"

    agents_cfg = conf.get("agents")
    if not isinstance(agents_cfg, list):
        return "SQL", "SQL"

    src_type: str | None = None
    tgt_type: str | None = None

    for agent in agents_cfg:
        if not isinstance(agent, dict):
            continue
        tag = str(agent.get("agentTag") or "").lower()
        agent_type = agent.get("agentType")
        if not tag or agent_type not in {"SOURCE", "TARGET"}:
            continue

        if agent_type == "SOURCE" and src_type is None:
            src_type = catalog.get(tag, "SQL")
        elif agent_type == "TARGET" and tgt_type is None:
            tgt_type = catalog.get(tag, "SQL")

    return src_type or "SQL", tgt_type or "SQL"


def load_config_from_file(path):
    """Load Bootstrapper configuration from JSON or YAML.

    The format is detected from the file extension when possible, otherwise
    the function will attempt JSON first and then fall back to YAML.
    """

    _, ext = os.path.splitext(path)
    ext = ext.lower()

    with open(path, 'r') as file:
        if ext in ('.yaml', '.yml'):
            logger.info(f"Loading YAML configuration from {path}")
            return yaml.safe_load(file)

        if ext == '.json':
            logger.info(f"Loading JSON configuration from {path}")
            return json.load(file)

        # Unknown extension: auto-detect
        logger.info(f"Loading configuration from {path} (auto-detect JSON/YAML)")
        text = file.read()
        try:
            return json.loads(text)
        except Exception:
            return yaml.safe_load(text)


# Environment variables and constants
file_conf_path = os.getenv('FILE_CONF_PATH', './config.json')
CONFIG_BASE_DIR = Path(file_conf_path).expanduser().resolve().parent

# Retrieve CoreHub URL from SDK if not specified
core_hub_url = os.getenv('CORE_HUB_URL')

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
_raw_source_type = os.getenv('SOURCE_TYPE')
_raw_target_type = os.getenv('TARGET_TYPE')
source_type = _raw_source_type.strip() if _raw_source_type else None
target_type = _raw_target_type.strip() if _raw_target_type else None
TABLE_LIST_YAML = os.getenv('TABLE_LIST_YAML', '/opt/config/tables-list.yaml')
ssl_enabled = os.getenv('SSL_ENABLED', 'False').lower() == 'true'
ssl_skip_verify = os.getenv('SSL_SKIP_VERIFY', 'False').lower() == 'true'

# Schema extraction will be done after logger initialization
AUTH_TOKEN_PATH = os.path.join('/opt/config', 'auth_token.json')

# Allow sub-second pauses when starting entities
ENTITY_START_TIMEOUT = float(os.getenv('ENTITY_START_TIMEOUT', '1'))

# CoreHubClient and ProtocolAwareAdapter have been moved to utils/core_hub_client.py

def generate_fancy_names(length):
    return [fake.catch_phrase() for _ in range(length)]

def safe_encode(s):
    return urllib.parse.quote(s, safe='')

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]


def _normalize_agent_type_for_add(agent_type: str) -> str:
    normalized = str(agent_type or '').strip().lower()
    if normalized in {'source', 'target'}:
        return normalized
    raise ValueError(f"Unsupported agentType {agent_type!r}. Expected SOURCE or TARGET")


def add_agent_to_pipeline_via_corehub(
    *,
    token: str,
    pipeline_id: str,
    agent_type: str,
    agent_user_tag: str,
    agent_internal_name: str,
) -> str:
    """Create/deploy an agent through Core Hub and attach it to a pipeline."""

    normalized_type = _normalize_agent_type_for_add(agent_type)
    response = fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/add",
        method='GET',
        token=token,
        params={
            'agentType': normalized_type,
            'agentUserTag': agent_user_tag,
            'agentInternalName': agent_internal_name,
        },
    )

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Unexpected response while adding {normalized_type} agent "
            f"{agent_user_tag!r}/{agent_internal_name!r}: {response}"
        )

    raw_agent_id = response.get('agentId') or response.get('id')
    if not raw_agent_id:
        raise RuntimeError(
            f"Core Hub did not return an agentId for {normalized_type} agent "
            f"{agent_user_tag!r}/{agent_internal_name!r}: {response}"
        )

    agent_id = str(raw_agent_id)
    logger.info(
        "Agent created via /agents/add: %s agent %s (id=%s)",
        normalized_type,
        agent_user_tag,
        agent_id,
    )
    
    # Explicitly assign the agent to the pipeline
    # The /agents/add endpoint creates the agent but may not fully bind it to the pipeline
    logger.info(
        "Assigning agent %s to pipeline %s with type %s",
        agent_id,
        pipeline_id,
        normalized_type.upper(),
    )
    fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{agent_id}",
        method='PUT',
        token=token,
        params={'agentType': normalized_type.upper()}  # agentType is a query parameter, not body
    )
    logger.info(
        "Successfully assigned %s agent %s (id=%s) to pipeline %s",
        normalized_type,
        agent_user_tag,
        agent_id,
        pipeline_id,
    )
    return agent_id


def upload_agent_certificate(
    *,
    token: str,
    pipeline_id: str,
    agent_id: str,
    certificate_type: str,
    certificate_path: str,
):
    """Upload a certificate file for the specified agent."""

    certificate_file = resolve_certificate_path(certificate_path)

    certificate_bytes = certificate_file.read_bytes()
    if not certificate_bytes:
        raise ValueError(f"Certificate file {certificate_path} is empty")

    certificate_ext = certificate_file.suffix.lstrip('.').lower() or 'crt'
    logger.info(
        "Uploading %s certificate for agent %s from %s (%d bytes)",
        certificate_type,
        agent_id,
        certificate_path,
        len(certificate_bytes),
    )

    fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{agent_id}/config/certificate/{certificate_type}",
        method='PUT',
        token=token,
        body=certificate_bytes,
        headers={'Certificate-Ext': certificate_ext},
    )


def resolve_certificate_path(certificate_path: str) -> Path:
    """Resolve certificate path, supporting relative paths to the config directory."""

    path_obj = Path(certificate_path).expanduser()
    candidates = []

    if path_obj.is_absolute():
        candidates.append(path_obj)
        candidates.append((CONFIG_BASE_DIR / path_obj.name).resolve())
        stripped = Path(path_obj.as_posix().lstrip('/'))
        if stripped and not stripped.is_absolute():
            candidates.append((CONFIG_BASE_DIR / stripped).resolve())
    else:
        candidates.append((CONFIG_BASE_DIR / path_obj).resolve())
        candidates.append(path_obj.resolve())

    # Always ensure CONFIG_BASE_DIR candidate is included even if duplicates
    candidates.append((CONFIG_BASE_DIR / path_obj.name).resolve())

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            logger.info("Resolved certificate path '%s' to '%s'", certificate_path, candidate)
            return candidate

    raise FileNotFoundError(
        f"Certificate file not found at {certificate_path}. Checked: "
        f"{', '.join(str(c) for c in seen)}"
    )

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
if True:
    # Determine authentication method
    if not use_sdk:
        logger.info("Using manual authentication with provided password or token.")
        # Existing authentication logic
        try:
            conf_test = load_config_from_file(file_conf_path)
            logger.info(f"Loaded configuration from {file_conf_path}")
        except Exception as e:
            log_failure(logger, f"Failed to load configuration: {str(e)}")
            lockfile_failure()

        # Initialize Core Hub client
        configure_core_hub(core_hub_url, use_ssl=ssl_enabled, skip_verify=ssl_skip_verify)
        logger.info(f"Core Hub URL: {core_hub_url}")

        # Wait for CoreHub to be ready before attempting authentication
        logger.info("Waiting for CoreHub to be ready...")
        try:
            wait_for_corehub_ready(max_retries=30, initial_delay=2)
        except RuntimeError as e:
            logger.error(f"Failed to connect to CoreHub: {e}")
            lockfile_failure()
            raise

        # Check if a valid token is present
        token = None
        if os.path.exists(AUTH_TOKEN_PATH):
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
                    log_success(logger, f"[NEW PASSWORD] Successfully changed password to: {new_password}")
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
                    logger.error("SDK authentication failed - token is None.")
                    # Retry SDK connection
                    max_retries = 30
                    retry_delay = 2
                    for retry in range(1, max_retries + 1):
                        logger.info(f"Retrying SDK authentication (attempt {retry}/{max_retries})...")
                        time.sleep(retry_delay)
                        try:
                            # Reinitialize SDK client
                            initialize_gluesync_sdk()
                            token = get_token()
                            if token:
                                logger.info(f"Successfully obtained token from SDK on retry {retry}")
                                save_token(token)
                                break
                        except Exception as retry_error:
                            logger.warning(f"Retry {retry} failed: {str(retry_error)}")
                            if retry == max_retries:
                                logger.error("All SDK authentication retries exhausted.")
                                raise Exception("Failed to authenticate via SDK after retries")
            else:
                logger.info("Successfully obtained token from Gluesync SDK.")
                # Save the token
                save_token(token)
        except Exception as e:
            logger.error(f"Exception during SDK token retrieval: {str(e)}")
            logger.error(f"Exception type: {type(e).__name__}")
            raise Exception(f"SDK authentication failed: {str(e)}")

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
    logger.info("Starting Gluesync Bootstrapper module in STANDARD mode...")

    # Start with explicit environment overrides when provided; these will
    # be refined/overridden by YAML hints and agents.json when absent.
    effective_source_type = source_type
    effective_target_type = target_type

    # Extract schema information from YAML file
    logger.info(f"Attempting to extract schemas from YAML file: {TABLE_LIST_YAML}")
    schema_pairs = extract_all_schemas_from_yaml(TABLE_LIST_YAML)
    if not schema_pairs:
        logger.warning("No schemas found in YAML file. Entity creation will be skipped.")
    else:
        logger.info(f"Schema extraction successful. {len(schema_pairs)} schema pair(s) found: {schema_pairs}")

        # Optionally derive source/target types from YAML if present
        yaml_source_type, yaml_target_type = extract_schema_types_from_yaml(TABLE_LIST_YAML)
        if yaml_source_type:
            effective_source_type = yaml_source_type
        if yaml_target_type:
            effective_target_type = yaml_target_type
        logger.info(
            "YAML type hints: sourceType=%s, targetType=%s",
            yaml_source_type,
            yaml_target_type,
        )

    # Initialize variables that might be used in different code paths
    change_required = False
    new_password = user_defined_password

    try:
        conf_test = load_config_from_file(file_conf_path)
        logger.info(f"Loaded configuration from {file_conf_path}")
    except Exception as e:
        log_failure(logger, f"Failed to load configuration: {str(e)}")
        lockfile_failure()
        return

    # If no explicit env/YAML type was provided, derive from config agents via agents.json
    if effective_source_type is None or effective_target_type is None:
        src_from_cfg, tgt_from_cfg = infer_pipeline_schema_types_from_config(conf_test)
        if effective_source_type is None:
            effective_source_type = src_from_cfg
        if effective_target_type is None:
            effective_target_type = tgt_from_cfg

    # Final fallbacks if everything else failed
    if effective_source_type is None:
        effective_source_type = "SQL"
    if effective_target_type is None:
        effective_target_type = "NoSQL"

    logger.info(
        "Using source_type=%s, target_type=%s (env/YAML/agents.json)",
        effective_source_type,
        effective_target_type,
    )

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
                log_success(logger, f"[NEW PASSWORD] Successfully changed password to: {new_password}")
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

    # Use provided pipeline name or generate/derive one
    if args.pipeline_name:
        pipeline_name = args.pipeline_name
        pipeline_description = f"Pipeline {pipeline_name}"
    else:
        config_pipeline_name = None
        if isinstance(conf_test, dict):
            config_pipeline_name = conf_test.get('pipelineName')

        if config_pipeline_name:
            pipeline_name = str(config_pipeline_name)
            pipeline_description = f"Pipeline {pipeline_name}"
            logger.info(f"Using pipeline name from configuration: {pipeline_name}")
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

    # Provision and attach SOURCE/TARGET agents directly via Core Hub.
    agents_to_conf = []
    for conf_agent in conf_test.get('agents', []):
        if not isinstance(conf_agent, dict):
            logger.warning("Skipping invalid agent configuration entry: %r", conf_agent)
            continue

        configured_type = str(conf_agent.get('agentType') or '').upper()
        agent_user_tag = conf_agent.get('agentUserTag') or conf_agent.get('agentTag')
        agent_internal_name = conf_agent.get('agentInternalName') or conf_agent.get('agentTag')

        if configured_type not in {'SOURCE', 'TARGET'}:
            raise RuntimeError(
                f"Invalid agentType {configured_type!r} in configuration. "
                "Expected SOURCE or TARGET"
            )
        if not agent_user_tag:
            raise RuntimeError(
                f"Missing agentUserTag/agentTag for {configured_type} agent in configuration"
            )
        if not agent_internal_name:
            raise RuntimeError(
                f"Missing agentInternalName (or legacy agentTag) for {configured_type} agent "
                f"{agent_user_tag!r}"
            )

        logger.info(
            "Adding %s agent via Core Hub add API (userTag=%s, internalName=%s)",
            configured_type,
            agent_user_tag,
            agent_internal_name,
        )
        agent_id = add_agent_to_pipeline_via_corehub(
            token=token,
            pipeline_id=pipeline_id,
            agent_type=configured_type,
            agent_user_tag=str(agent_user_tag),
            agent_internal_name=str(agent_internal_name),
        )

        agents_to_conf.append({
            'agentId': agent_id,
            'agentType': configured_type,
            'agentTag': str(agent_user_tag),
            'agentUserTag': str(agent_user_tag),
            'agentInternalName': str(agent_internal_name),
            'hostCredentials': conf_agent.get('hostCredentials', {}),
            'customHostCredentials': conf_agent.get('customHostCredentials', {}),
            'specificConfiguration': conf_agent.get('specificConfiguration', {}),
            'entities': conf_agent.get('entities', []),
        })

    logger.info(f"Prepared {len(agents_to_conf)} agents for configuration")
    logger.debug(f"Filtered agents: {json.dumps(agents_to_conf, indent=2)}")

    # Apply agent host credentials
    for agent in agents_to_conf:
        if 'agentId' not in agent:
            logger.warning(f"Agent missing 'agentId' field: {agent}")
            continue

        host_credentials = dict(agent['hostCredentials']) if agent['hostCredentials'] else {}
        custom_host_credentials = agent['customHostCredentials']
        certificate_path = host_credentials.pop('certificatePath', None)
        certificate_type = host_credentials.pop('certificateType', None)

        fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/credentials",
            method='PUT',
            token=token,
            body={
                'hostCredentials': host_credentials,
                'customHostCredentials': custom_host_credentials
            }
        )

        if certificate_path and certificate_type:
            try:
                upload_agent_certificate(
                    token=token,
                    pipeline_id=pipeline_id,
                    agent_id=agent['agentId'],
                    certificate_type=certificate_type,
                    certificate_path=certificate_path,
                )
            except Exception as cert_error:
                log_failure(
                    logger,
                    f"Failed to upload certificate for agent {agent['agentId']}: {cert_error}"
                )
                raise

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

    # Verify agents are properly registered in pipeline before creating entities
    logger.info("Verifying agents are registered in pipeline configuration...")
    pipeline_config = fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)
    registered_agents = pipeline_config.get('agents', [])
    logger.info(f"Pipeline config shows {len(registered_agents)} agents registered")
    for agent in registered_agents:
        agent_id = agent.get('agentId') or agent.get('id')
        agent_type = agent.get('agentType')
        logger.info(f"  - Agent: type={agent_type}, id={agent_id}")
    
    if len(registered_agents) < 2:
        raise RuntimeError(
            f"Pipeline has only {len(registered_agents)} agents registered. "
            f"Expected 2 (SOURCE and TARGET). Agents: {registered_agents}"
        )

    # Create entities sequentially for each schema pair
    if schema_pairs:
        entity_creation_script = 'create_all_entities.py'
        for idx, (source_schema, target_schema) in enumerate(schema_pairs, start=1):
            logger.info(
                f"[Schema {idx}/{len(schema_pairs)}] Starting entity creation for "
                f"source='{source_schema}', target='{target_schema}'"
            )
            try:
                cmd = [
                    'python3',
                    entity_creation_script,
                    '--pipeline', pipeline_id,
                    '--source-schema', source_schema,
                    '--source-type', effective_source_type,
                    '--target-type', effective_target_type,
                    '--token', token,
                    '--target-schema', target_schema or source_schema
                ]

                if os.path.exists(TABLE_LIST_YAML):
                    cmd.extend(['--yaml-file', TABLE_LIST_YAML])
                    logger.info(f"Using TABLE_LIST.yaml: {TABLE_LIST_YAML}")
                else:
                    logger.warning(f"TABLE_LIST.yaml not found at {TABLE_LIST_YAML}. Proceeding without it.")

                subprocess.run(cmd, check=True)
                log_success(
                    logger,
                    f"[Schema {idx}/{len(schema_pairs)}] Entity creation completed for "
                    f"source {source_schema} -> target {target_schema or source_schema}"
                )
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
            body={'configurationCompleted': True, 'name': pipeline_name}
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


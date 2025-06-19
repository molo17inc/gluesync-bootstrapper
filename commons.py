import os
import json
import requests
import time
import uuid
import urllib.parse
from urllib.parse import urlencode, quote
import urllib3
import ssl
import traceback
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import yaml
import argparse
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete, exit_on_fail
from utils.chronos_client import ChronosClient
from utils.core_hub_client import CoreHubClient

CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')

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
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/columns", token=token,
                          params=params)

def get_node_info(token, pipeline_id, agent_id):
    return fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/node-info", token=token)


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
import requests
import sys
from faker import Faker
import urllib.parse
import time
import subprocess
import uuid
import urllib3
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

fake = Faker()

# Environment variables and constants
file_conf_path = os.getenv('FILE_CONF_PATH', './config.json')
core_hub_url = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
default_user = 'admin'
default_password = 'admin'
user_defined_password = os.getenv('DEFAULT_PASSWORD', default_password)
create_entities_from_schema = os.getenv('CREATE_ENTITIES_FROM_SCHEMA')
target_schema = os.getenv('TARGET_SCHEMA')
source_type = os.getenv('SOURCE_TYPE', 'SQL')
target_type = os.getenv('TARGET_TYPE', 'NoSQL')
TABLE_LIST_YAML = os.getenv('TABLE_LIST_YAML', 'TABLE_LIST.yaml')

ENTITY_START_TIMEOUT = 1

class GlueSyncError(Exception):
    """Custom exception for GlueSync-related errors"""
    pass

class CustomHttpAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.ssl_context = create_urllib3_context(
            cert_reqs=ssl.CERT_NONE,
            ssl_version=ssl.PROTOCOL_TLS
        )
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

def generate_fancy_names(length):
    return [fake.catch_phrase() for _ in range(length)]

def safe_encode(s):
    return urllib.parse.quote(s, safe='')

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]

def fetch_core_hub(path, method='GET', token=None, body=None):
    url = f"{core_hub_url}{path}"
    headers = {
        'Authorization': f'Bearer {token}' if token else None,
        'Content-Type': 'application/json'
    }

    print(f"Loading: {url} with: {body}")
    
    session = requests.Session()
    adapter = CustomHttpAdapter()
    session.mount('https://', adapter)
    
    try:
        response = session.request(method, url, headers=headers, json=body, verify=False)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"API request failed: {str(e)}")
        if hasattr(e.response, 'text'):
            print(f"Response content: {e.response.text}")
        raise GlueSyncError(f"API request to {url} failed: {str(e)}")
    
    if response.status_code == 202 and not response.content:
        return {}

    try:
        return response.json()
    except json.JSONDecodeError as e:
        print(f"Non-JSON response from {url}: {response.text}")
        raise GlueSyncError(f"Invalid JSON response from {url}: {str(e)}")

def get_entities(token, pipeline_id):
    try:
        response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
        print(f"Retrieved the following entities: {response}")
        
        if not isinstance(response, list):
            raise GlueSyncError(f"Unexpected response format when fetching entities: {response}")
        
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
    except Exception as e:
        print(f"Error getting entities: {str(e)}")
        raise

def configure_entities(agents_to_conf, pipeline_id, token):
    try:
        entities_payload = {"entities": []}

        for agent in agents_to_conf:
            for entity in agent['entities']:
                existing_entity = next((e for e in entities_payload["entities"] if e["entityName"] == entity["entityName"]), None)
                if existing_entity is None:
                    existing_entity = {
                        "entityId": str(uuid.uuid4()),
                        "entityName": entity["entityName"],
                        "agentEntities": []
                    }
                    entities_payload["entities"].append(existing_entity)

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
    except Exception as e:
        print(f"Error configuring entities: {str(e)}")
        raise

def start_entity_syncs(token, pipeline_id):
    try:
        entities = get_entities(token, pipeline_id)
        print(f"Retrieved the following entities: {entities}")
        
        for entity in entities:
            entityId = entity['entityId']
            entityName = entity['entityName']
            
            try:
                encoded_entity_id = safe_encode(entityId)
                query_params = f"entity={encoded_entity_id}"
                
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
                raise
    except Exception as e:
        print(f"Error in start_entity_syncs: {str(e)}")
        raise

def main():
    try:
        # Load configuration
        try:
            with open(file_conf_path, 'r') as file:
                conf_test = json.load(file)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error loading configuration file: {str(e)}")
            sys.exit(1)

        # Authenticate
        auth_response = fetch_core_hub(
            '/authentication/login',
            method='POST',
            body={'username': default_user, 'password': default_password}
        )
        token = auth_response.get('apiToken')
        if not token:
            raise GlueSyncError('Failed to authenticate: No API token received')

        # List unassigned agents
        unassigned_agents = fetch_core_hub('/unassigned-agents', token=token)
        if not isinstance(unassigned_agents, list):
            raise GlueSyncError('Failed to retrieve unassigned agents: Invalid response format')

        print("Unassigned agents:", json.dumps(unassigned_agents, indent=2))
        print("Config agents:", json.dumps(conf_test['agents'], indent=2))

        fancy_names = generate_fancy_names(2)

        # Create pipeline
        pipeline_response = fetch_core_hub(
            '/pipelines',
            method='POST',
            token=token,
            body={'name': fancy_names[0], 'description': fancy_names[1], 'configurationCompleted': False}
        )
        pipeline_id = pipeline_response.get('pipelineId')
        if not pipeline_id:
            raise GlueSyncError('Failed to create pipeline: No pipeline ID received')

        # Filter and configure agents
        agents_to_conf = [
            {
                'agentId': agent['agentId'],
                'agentType': agent['agentType'],
                'agentTag': agent['agentTag'],
                'hostCredentials': conf_agent['hostCredentials'],
                'customHostCredentials': conf_agent.get('hostCredentialsCustomProperties', {}),
                'specificConfiguration': conf_agent['specificConfiguration'],
                'entities': conf_agent['entities']
            }
            for conf_agent in conf_test['agents']
            for agent in unassigned_agents
            if agent['agentTag'] == conf_agent['agentTag'] and agent['agentType'] == conf_agent['agentType']
        ]

        if not agents_to_conf:
            raise GlueSyncError("No matching agents found to configure")

        print("Filtered agents:", json.dumps(agents_to_conf, indent=2))

        # Configure each agent
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                raise GlueSyncError(f"Invalid agent configuration: Missing agentId field")
            
            # Assign agent to pipeline
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}",
                method='PUT',
                token=token
            )

            # Configure host credentials
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/credentials",
                method='PUT',
                token=token,
                body={
                    'hostCredentials': agent['hostCredentials'],
                    'customHostCredentials': agent['customHostCredentials']
                }
            )
            
            # Configure specific settings
            if agent['specificConfiguration']:
                fetch_core_hub(
                    f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/specific",
                    method='PUT',
                    token=token,
                    body={"configuration": agent['specificConfiguration']}
                )

        configure_entities(agents_to_conf, pipeline_id, token)

        if create_entities_from_schema:
            source_schema = create_entities_from_schema
            cmd = [
                'python',
                'create_all_entities.py',
                '--pipeline', pipeline_id,
                '--source-schema', source_schema,
                '--source-type', source_type,
                '--target-type', target_type
            ]
            
            if target_schema:
                cmd.extend(['--target-schema', target_schema])
            else:
                cmd.extend(['--target-schema', source_schema])

            if os.path.exists(TABLE_LIST_YAML):
                cmd.extend(['--yaml-file', TABLE_LIST_YAML])
                print(f"Using TABLE_LIST.yaml: {TABLE_LIST_YAML}")
            
            try:
                subprocess.run(cmd, check=True)
                print(f"Entity creation completed for pipeline {pipeline_id}")
            except subprocess.CalledProcessError as e:
                raise GlueSyncError(f"Entity creation script failed: {str(e)}")

        # Set pipeline as ready
        fetch_core_hub(
            f"/pipelines/{pipeline_id}",
            method='PUT',
            token=token,
            body={'configurationCompleted': True, 'name': fancy_names[0]}
        )

        time.sleep(ENTITY_START_TIMEOUT)
        
        start_entity_syncs(token, pipeline_id)

    except GlueSyncError as error:
        print(f"GlueSync Error: {str(error)}")
        sys.exit(1)
    except Exception as error:
        print(f"Unexpected error: {str(error)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
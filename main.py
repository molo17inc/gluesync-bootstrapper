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
from faker import Faker
import urllib.parse
import time
import subprocess
import uuid
import urllib3
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from urllib.parse import urlparse

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
AUTH_TOKEN_PATH = os.path.join('/opt/config', 'auth_token.json')

ENTITY_START_TIMEOUT = 1

class ProtocolAwareAdapter(HTTPAdapter):
    """HTTP adapter that handles both HTTP and HTTPS protocols."""
    def __init__(self, *args, **kwargs):
        self.ssl_context = create_urllib3_context(
            cert_reqs=ssl.CERT_NONE,
            ssl_version=ssl.PROTOCOL_TLS
        )
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self.is_secure_protocol:
            kwargs['ssl_context'] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        if self.is_secure_protocol:
            kwargs['ssl_context'] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)

    @property
    def is_secure_protocol(self):
        return hasattr(self, '_is_secure') and self._is_secure

    def set_protocol(self, is_secure):
        self._is_secure = is_secure

class CoreHubClient:
    """Client for handling CoreHub API requests with protocol awareness."""
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.adapter = ProtocolAwareAdapter()
        
        # Parse URL to determine protocol
        parsed_url = urlparse(base_url)
        is_secure = parsed_url.scheme == 'https'
        
        # Configure adapter based on protocol
        self.adapter.set_protocol(is_secure)
        
        # Mount adapter for both HTTP and HTTPS
        self.session.mount('http://', self.adapter)
        self.session.mount('https://', self.adapter)

    def request(self, path, method='GET', token=None, body=None):
        url = f"{self.base_url}{path}"
        headers = {
            'Authorization': f'Bearer {token}' if token else None,
            'Content-Type': 'application/json'
        }
        headers = {k: v for k, v in headers.items() if v is not None}

        print(f"Loading: {url} with: {body}")
        
        response = self.session.request(
            method, 
            url, 
            headers=headers, 
            json=body, 
            verify=False if self.adapter.is_secure_protocol else None
        )
        
        if response.status_code < 200 or response.status_code >= 300:
            print(f"Request to {url} failed with status code {response.status_code}: {response.text}")
            raise Exception(f"Request to {url} failed with status code {response.status_code}: {response.text}")
        
        if response.status_code == 202 and not response.content:
            return {}

        try:
            return response.json()
        except json.JSONDecodeError:
            print(f"Non-JSON response from {url}: {response.text}")
            return response.text

def generate_fancy_names(length):
    return [fake.catch_phrase() for _ in range(length)]

def safe_encode(s):
    return urllib.parse.quote(s, safe='')

def generate_short_guid():
    return str(uuid.uuid4()).split('-')[0]

# Initialize the CoreHub client
core_hub_client = CoreHubClient(core_hub_url)

def fetch_core_hub(path, method='GET', token=None, body=None):
    return core_hub_client.request(path, method, token, body)

def get_entities(token, pipeline_id):
    response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
    print(f"Retrieved the following entities: {response}")
    
    if not isinstance(response, list) or not response:
        print(f"Unexpected response when fetching entities: {response}")
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
    with open(file_conf_path, 'r') as file:
        conf_test = json.load(file)

    try:
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
                            print("Successfully authenticated with saved token")
                    except Exception as e:
                        if "401" in str(e):
                            print("Saved token is invalid, attempting to authenticate with default credentials")
                            token = None
                        else:
                            raise e
        except FileNotFoundError:
            print("No saved token found, attempting to authenticate with default credentials")
            token = None
        
        if not token:
            # Initial authentication
            auth_response = fetch_core_hub(
                '/authentication/login',
                method='POST',
                body={'username': default_user, 'password': default_password}
            )
            token = auth_response.get('apiToken')
            if not token:
                raise Exception('Failed to authenticate')
                
            change_required = auth_response.get('changeRequired', False)
            if change_required:
                print(f"Password change required")
                # Generate a new random password and change it
                new_password = f"{fake.word().upper()}_{generate_short_guid()}_!{fake.random_number(digits=3)}"
                try:
                    # Change password and get new token
                    token = change_password(token, default_password, new_password)
                    print(f"Successfully changed password to: {new_password}")
                except Exception as e:
                    print(f"Password change failed, attempting to continue with default password: {str(e)}")
                    # Try to get a fresh token with the default password
                    auth_response = fetch_core_hub(
                        '/authentication/login',
                        method='POST',
                        body={'username': default_user, 'password': default_password}
                    )
                    token = auth_response.get('apiToken')
                    if not token:
                        raise Exception('Failed to re-authenticate with default password')
                    new_password = default_password
            else:
                new_password = default_password
                # Save the initial token if no password change was required
                save_token(token)

        # List unassigned agents
        unassigned_agents = fetch_core_hub('/unassigned-agents', token=token)
        if not isinstance(unassigned_agents, list):
            raise Exception('Failed to retrieve unassigned agents')

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

        print("Filtered agents:", json.dumps(agents_to_conf, indent=2))

        # Assign agents to pipeline
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                print(f"Warning: Agent missing 'agentId' field: {agent}")
                continue
            
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}",
                method='PUT',
                token=token
            )

        # Apply agent host credentials
        for agent in agents_to_conf:
            if 'agentId' not in agent:
                print(f"Warning: Agent missing 'agentId' field: {agent}")
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
                print(f"Warning: Agent missing 'agentId' field: {agent}")
                continue
            
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

            # Invoke the entity creation script
            entity_creation_script = 'create_all_entities.py' 
            try:
                cmd = [
                    'python',
                    entity_creation_script,
                    '--pipeline', pipeline_id,
                    '--source-schema', source_schema,
                    '--source-type', source_type,
                    '--target-type', target_type,
                    '--token', token
                ]
                
                if target_schema:
                    cmd.extend(['--target-schema', target_schema])
                else:
                    cmd.extend(['--target-schema', source_schema])

                if os.path.exists(TABLE_LIST_YAML):
                    cmd.extend(['--yaml-file', TABLE_LIST_YAML])
                    print(f"Using TABLE_LIST.yaml: {TABLE_LIST_YAML}")
                else:
                    print(f"TABLE_LIST.yaml not found at {TABLE_LIST_YAML}. Proceeding without it.")

                subprocess.run(cmd, check=True)
                print(f"Entity creation completed for pipeline {pipeline_id}, source schema {source_schema}, target schema {target_schema or source_schema}, source type {source_type}, target type {target_type}")
            except subprocess.CalledProcessError as e:
                print(f"Error running entity creation script: {e}")

        # Set pipeline as ready (exiting from Draft status)
        fetch_core_hub(
                f"/pipelines/{pipeline_id}",
                method='PUT',
                token=token,
                body={'configurationCompleted': True, 'name': fancy_names[0]}
        )

        time.sleep(ENTITY_START_TIMEOUT)
                
        start_entity_syncs(token, pipeline_id)

    except Exception as error:
        print(f"Error: {error}")

if __name__ == "__main__":
    main()
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

fake = Faker()

# Environment variables and constants
file_conf_path = os.getenv('FILE_CONF_PATH', './config.json')
core_hub_url = os.getenv('CORE_HUB_URL', 'http://localhost:1717')
default_user = 'admin'
default_password = 'admin'
user_defined_password = os.getenv('DEFAULT_PASSWORD', default_password)
create_entities_from_schema = os.getenv('CREATE_ENTITIES_FROM_SCHEMA')
target_type = os.getenv('TARGET_TYPE', 'NoSQL')

ENTITY_START_TIMEOUT = 1  # Timeout in seconds between entity start calls

def generate_fancy_names(length):
    return [fake.catch_phrase() for _ in range(length)]

def safe_encode(s):
    return urllib.parse.quote(s, safe='')

def fetch_core_hub(path, method='GET', token=None, body=None):
    url = f"{core_hub_url}{path}"
    headers = {
        'Authorization': f'Bearer {token}' if token else None,
        'Content-Type': 'application/json'
    }

    print(f"Loading: {url} with: {body}")
    response = requests.request(method, url, headers=headers, json=body)
    
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

def get_entities(token, pipeline_id):
    response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)
    entities = []

    if isinstance(response, list):
        for item in response:
            if isinstance(item, dict) and 'entityName' in item:
                entity_name = item['entityName']
                agents = item.get('agents', {})
                
                for agent_id, config in agents.items():
                    if 'entity' in config and 'entityName' in config['entity']:
                        entities.append({
                            'entityName': entity_name,
                            'agentId': agent_id,
                            'entityConfig': config['entity']
                        })

    print(f"Total entities found: {len(entities)}")
    
    for entity in entities:
        print(f"  - Entity: {entity['entityName']}, Agent: {entity['agentId']}")

    return entities

def main():
    with open(file_conf_path, 'r') as file:
        conf_test = json.load(file)

    try:
        # Authenticate
        auth_response = fetch_core_hub(
            '/authentication/login',
            method='POST',
            body={'username': default_user, 'password': default_password}
        )
        token = auth_response.get('apiToken')
        if not token:
            raise Exception('Failed to authenticate')

        # List unassigned agents
        unassigned_agents = fetch_core_hub('/unassigned-agents', token=token)
        if not isinstance(unassigned_agents, list):
            raise Exception('Failed to retrieve unassigned agents')

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
                **agent,
                'hostCredentials': conf_agent['hostCredentials'],
                'customHostCredentials': agent['hostCredentialsCustomProperties'],
                'specificConfiguration': conf_agent['specificConfiguration'],
                'entities': conf_agent['entities']
            }
            for conf_agent in conf_test['agents']
            for agent in unassigned_agents
            if agent['agentTag'] == conf_agent['agentTag'] and agent['agentType'] == conf_agent['agentType']
        ]

        # Assign agents to pipeline
        for agent in agents_to_conf:
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['id']}",
                method='PUT',
                token=token
            )

        # Apply agent host credentials
        for agent in agents_to_conf:
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['id']}/config/credentials",
                method='PUT',
                token=token,
                body={
                    'hostCredentials': agent['hostCredentials'],
                    'customHostCredentials': agent['customHostCredentials']
                }
            )
        
        # Apply agent specific configuration
        for agent in agents_to_conf:
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent['id']}/config/specific",
                method='PUT',
                token=token,
                body=agent['specificConfiguration']
            )

        # Apply agent entities
        for agent in agents_to_conf:
            entity_data = {
                "entities": [
                    {
                        "agentId": agent['id'],
                        "entity": entity,
                        "customEntitiesProperties": {},
                        "customTableProperties": {}
                    } for entity in agent['entities']
                ]
            }
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/config/entities",
                method='PUT',
                token=token,
                body=entity_data
            )

        if create_entities_from_schema:
            # Use create_entities_from_schema as the schema name
            schema_name = create_entities_from_schema

            # Invoke the entity creation script
            entity_creation_script = 'create_all_entities.py' 
            try:
                subprocess.run([
                    'python',
                    entity_creation_script,
                    '--pipeline', pipeline_id,
                    '--schema', schema_name,
                    '--target-type', target_type
                ], check=True)
                print(f"Entity creation completed for pipeline {pipeline_id}, schema {schema_name}, target type {target_type}")
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
        
        # Get entities directly from the pipeline
        entity_configs = get_entities(token, pipeline_id)
        
        # Start pipeline entities with individual API calls for each entity
        for config in entity_configs:
            try:
                encoded_entity_name = safe_encode(config['entityName'])
                query_params = f"entity={encoded_entity_name}"
                
                fetch_core_hub(
                    f"/pipelines/{pipeline_id}/commands/sync/start?withSnapshot=true&{query_params}",
                    method='POST',
                    token=token
                )
                print(f"Started sync for entity: {config['entityName']} on agent: {config['agentId']}")
                
                time.sleep(ENTITY_START_TIMEOUT)
            except Exception as e:
                print(f"Error starting sync for entity {config['entityName']}: {str(e)}")

    except Exception as error:
        print(f"Error: {error}")

if __name__ == "__main__":
    main()
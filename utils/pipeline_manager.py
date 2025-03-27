#!/usr/bin/env python3
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
import uuid
import time
import logging
from urllib.parse import quote as url_encode
from faker import Faker

from utils.log import log_success, log_failure

class PipelineManager:
    """
    Handles all pipeline-related operations including creating, configuring, and
    managing pipelines and their entities.
    """
    
    def __init__(self, logger, core_hub_client):
        """
        Initialize the PipelineManager
        
        Args:
            logger: The logger instance to use
            core_hub_client: The CoreHubClient instance
        """
        self.logger = logger
        self.core_hub_client = core_hub_client
        self.faker = Faker()
    
    def get_or_create_pipeline(self, token, pipeline_name=None):
        """
        Get an existing pipeline or create a new one
        
        Args:
            token: The authentication token
            pipeline_name: Optional name for the pipeline, generates one if None
            
        Returns:
            dict: The pipeline data with ID
        """
        # Check if a pipeline with the given name exists
        if pipeline_name:
            pipelines = self.get_pipelines(token)
            for pipeline in pipelines:
                if pipeline.get('pipelineName') == pipeline_name:
                    self.logger.info(f"Found existing pipeline: {pipeline_name}")
                    return pipeline
        
        # Generate a name if none provided
        if not pipeline_name:
            pipeline_name = self.faker.catch_phrase()
        
        # Create a new pipeline
        self.logger.info(f"Creating new pipeline: {pipeline_name}")
        payload = {
            "pipelineName": pipeline_name
        }
        
        response = self.core_hub_client.request(
            '/pipelines',
            method='POST',
            token=token,
            body=payload
        )
        
        log_success(self.logger, f"Successfully created pipeline: {pipeline_name}")
        return response
    
    def get_pipelines(self, token):
        """
        Get all available pipelines
        
        Args:
            token: The authentication token
            
        Returns:
            list: List of pipeline objects
        """
        response = self.core_hub_client.request(
            '/pipelines',
            method='GET',
            token=token
        )
        
        if not isinstance(response, list):
            self.logger.warning(f"Unexpected response when fetching pipelines: {response}")
            return []
            
        return response
    
    def get_agents(self, token, pipeline_id):
        """
        Get all agents for a pipeline
        
        Args:
            token: The authentication token
            pipeline_id: The ID of the pipeline
            
        Returns:
            list: List of agent objects
        """
        response = self.core_hub_client.request(
            f"/pipelines/{pipeline_id}/agents",
            token=token
        )
        
        if not isinstance(response, list):
            self.logger.warning(f"Unexpected response when fetching agents: {response}")
            return []
            
        return response
    
    def get_entities(self, token, pipeline_id):
        """
        Get all entities for a pipeline
        
        Args:
            token: The authentication token
            pipeline_id: The ID of the pipeline
            
        Returns:
            list: List of entity objects with ID and name
        """
        response = self.core_hub_client.request(
            f"/pipelines/{pipeline_id}/entities", 
            token=token
        )
        
        self.logger.debug(f"Retrieved the following entities: {response}")

        if not isinstance(response, list) or not response:
            self.logger.warning(f"Unexpected response when fetching entities: {response}")
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
    
    def configure_entities(self, agents_to_conf, pipeline_id, token):
        """
        Configure pipeline entities with agent associations
        
        Args:
            agents_to_conf: List of agents with entity configurations
            pipeline_id: The ID of the pipeline
            token: The authentication token
            
        Returns:
            dict: The configuration response
        """
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

        return self.core_hub_client.request(
            f"/pipelines/{pipeline_id}/config/entities",
            method='PUT',
            token=token,
            body=entities_payload
        )
    
    def start_entity_syncs(self, token, pipeline_id, entity_start_timeout=1):
        """
        Start synchronization for all entities in a pipeline
        
        Args:
            token: The authentication token
            pipeline_id: The ID of the pipeline
            entity_start_timeout: Timeout in seconds between entity starts
            
        Returns:
            bool: True if all entities started successfully
        """
        entities = self.get_entities(token, pipeline_id)
        
        for entity in entities:
            entity_id = entity['entityId']
            entity_name = entity['entityName']
            
            self.logger.info(f"Starting entity {entity_name} sync...")
            
            try:
                self.core_hub_client.request(
                    f"/pipelines/{pipeline_id}/entities/{entity_id}/start",
                    method='POST',
                    token=token
                )
                log_success(self.logger, f"Successfully started entity: {entity_name}")
                
                # Small delay between entity starts to avoid overloading the system
                time.sleep(entity_start_timeout)
            except Exception as e:
                log_failure(self.logger, f"Failed to start entity {entity_name}: {str(e)}")
                return False
                
        return True
    
    def safe_encode(self, s):
        """
        URL-encode a string safely
        
        Args:
            s: The string to encode
            
        Returns:
            str: The URL-encoded string
        """
        return url_encode(s, safe='')
    
    def generate_fancy_names(self, length):
        """
        Generate a list of random fancy names for entities
        
        Args:
            length: Number of names to generate
            
        Returns:
            list: List of generated names
        """
        return [self.faker.catch_phrase() for _ in range(length)]
    
    def generate_short_guid(self):
        """
        Generate a shortened UUID
        
        Returns:
            str: A shortened UUID
        """
        return str(uuid.uuid4()).split('-')[0]

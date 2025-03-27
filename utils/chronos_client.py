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
import requests
import json
from urllib.parse import urljoin, urlparse, urlunparse
from utils.log import get_logger, log_success, log_failure

# Initialize logger
logger = get_logger()

class ChronosClient:
    """Client for interacting with the Chronos scheduling service."""
    
    def __init__(self, base_url=None):
        self.base_url = base_url or os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')
        if not self.base_url.endswith('/'):
            self.base_url += '/'
            
        # Check SSL configuration
        self.use_ssl = os.getenv('SSL_ENABLED', 'False').lower() == 'true'
        self.verify_ssl = not (os.getenv('SSL_SKIP_VERIFY', 'False').lower() == 'true')
        
        # Update URL scheme if SSL is enabled
        if self.use_ssl:
            parsed_url = urlparse(self.base_url)
            # If the URL is using http, update it to https
            if parsed_url.scheme == 'http':
                updated_url = urlunparse(('https',) + parsed_url[1:])
                logger.info(f"SSL enabled: Changed Chronos URL from {self.base_url} to {updated_url}")
                self.base_url = updated_url
        
        logger.info(f"Initializing Chronos client with base URL: {self.base_url}, SSL={self.use_ssl}, verify={self.verify_ssl}")
        
    def _request(self, endpoint, method='GET', data=None, params=None):
        """Make a request to the Chronos API."""
        url = urljoin(self.base_url, endpoint)
        headers = {'Content-Type': 'application/json'}
        
        try:
            logger.debug(f"Making {method} request to {url}")
            if data:
                logger.debug(f"Request data: {json.dumps(data)}")
                
            # Use SSL verification based on configuration
            verify = self.verify_ssl if self.use_ssl else False
            
            response = requests.request(
                method, 
                url, 
                json=data, 
                params=params,
                headers=headers,
                verify=verify
            )
            
            response.raise_for_status()
            
            if response.status_code == 204:  # No content
                return None
                
            return response.json()
        except requests.exceptions.RequestException as e:
            error_msg = f"Error making request to {url}: {str(e)}"
            log_failure(logger, error_msg)
            
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
                
            raise Exception(error_msg)
    
    def create_job(self, job_data):
        """Create a new scheduled job."""
        logger.info(f"Creating new job: {job_data.get('name')}")
        return self._request('api/jobs/', method='POST', data=job_data)
    
    def get_jobs(self, task_type=None, enabled=None, limit=100):
        """Get a list of scheduled jobs with optional filtering."""
        params = {'limit': limit}
        if task_type:
            params['task_type'] = task_type
        if enabled is not None:
            params['enabled'] = enabled
            
        logger.info(f"Getting jobs with params: {params}")
        return self._request('api/jobs/', params=params)
    
    def get_job(self, job_id):
        """Get a specific job by ID."""
        logger.info(f"Getting job with ID: {job_id}")
        return self._request(f'api/jobs/{job_id}')
    
    def update_job(self, job_id, job_data):
        """Update an existing job."""
        logger.info(f"Updating job with ID: {job_id}")
        return self._request(f'api/jobs/{job_id}', method='PUT', data=job_data)
    
    def delete_job(self, job_id):
        """Delete a job by ID."""
        logger.info(f"Deleting job with ID: {job_id}")
        return self._request(f'api/jobs/{job_id}', method='DELETE')
    
    def create_entity_schedule(self, pipeline_id, entity_id, task_type, schedule_config, 
                              name=None, description=None, with_snapshot=False, enabled=True):
        """
        Create a schedule for an entity with the specified configuration.
        
        Args:
            pipeline_id (str): ID of the pipeline
            entity_id (str): ID of the entity (will be added to entity_ids array)
            task_type (str): Type of task to schedule (entity_start, entity_stop, entity_snapshot)
            schedule_config (dict): Schedule configuration dict with either 'cron_expression' or 'schedule' 
            name (str, optional): Name for the job
            description (str, optional): Description for the job
            with_snapshot (bool, optional): Whether to include snapshot when starting entities
            enabled (bool, optional): Whether the job is enabled initially
            
        Returns:
            dict: The created job details
        """
        if not pipeline_id or not entity_id:
            raise ValueError("Both pipeline_id and entity_id are required")
            
        if task_type not in ('entity_start', 'entity_stop', 'entity_snapshot'):
            raise ValueError(f"Invalid task_type for entity: {task_type}")
            
        # Generate a name if not provided
        if not name:
            task_name_map = {
                'entity_start': 'Start',
                'entity_stop': 'Stop',
                'entity_snapshot': 'Snapshot'
            }
            name = f"{task_name_map.get(task_type, 'Schedule')} for {entity_id}"
            
        job_data = {
            'name': name,
            'description': description,
            'task_type': task_type,
            'pipeline_id': pipeline_id,
            'entity_ids': [entity_id],  # Use entity_ids array instead of entity_id
            'with_snapshot': with_snapshot,
            'enabled': enabled
        }
        
        # Add either cron_expression or schedule
        if 'cron_expression' in schedule_config:
            job_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            job_data['schedule'] = schedule_config['schedule']
        else:
            raise ValueError("Either cron_expression or schedule must be provided")
            
        return self.create_job(job_data)
    
    def create_pipeline_schedule(self, pipeline_id, task_type, schedule_config,
                                name=None, description=None, with_snapshot=False, enabled=True):
        """
        Create a schedule for a pipeline with the specified configuration.
        
        Args:
            pipeline_id (str): ID of the pipeline
            task_type (str): Type of task to schedule (pipeline_start, pipeline_stop, pipeline_snapshot)
            schedule_config (dict): Schedule configuration dict with either 'cron_expression' or 'schedule'
            name (str, optional): Name for the job 
            description (str, optional): Description for the job
            with_snapshot (bool, optional): Whether to include snapshot when starting pipeline
            enabled (bool, optional): Whether the job is enabled initially
            
        Returns:
            dict: The created job details
        """
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
            
        if task_type not in ('pipeline_start', 'pipeline_stop', 'pipeline_snapshot'):
            raise ValueError(f"Invalid task_type for pipeline: {task_type}")
            
        # Generate a name if not provided
        if not name:
            task_name_map = {
                'pipeline_start': 'Start',
                'pipeline_stop': 'Stop',
                'pipeline_snapshot': 'Snapshot'
            }
            name = f"{task_name_map.get(task_type, 'Schedule')} for pipeline {pipeline_id}"
            
        job_data = {
            'name': name,
            'description': description,
            'task_type': task_type,
            'pipeline_id': pipeline_id,
            'with_snapshot': with_snapshot,
            'enabled': enabled
        }
        
        # Add either cron_expression or schedule
        if 'cron_expression' in schedule_config:
            job_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            job_data['schedule'] = schedule_config['schedule']
        else:
            raise ValueError("Either cron_expression or schedule must be provided")
            
        return self.create_job(job_data)

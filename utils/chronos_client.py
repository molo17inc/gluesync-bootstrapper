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
import requests
import json
import time
from urllib.parse import urljoin, urlparse, urlunparse
from utils.log import get_logger, log_success, log_failure

# Initialize logger
logger = get_logger()

class ChronosClient:
    """Client for interacting with the Chronos scheduling service."""
    
    def __init__(self, base_url=None, corehub_url=None, token=None):
        if corehub_url and not base_url:
            # Build chronos URL from corehub URL: same host/protocol/port + /chronos path
            try:
                from urllib.parse import urljoin
                chronos_url = urljoin(corehub_url.rstrip('/'), '/chronos')
                logger.info(f"Using corehub-derived chronos URL: {chronos_url}")
                base_url = chronos_url
            except Exception as exc:
                logger.warning(f"Failed to derive chronos URL from corehub: {exc}")
                base_url = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:1717')
        elif not base_url:
            base_url = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:1717')
            
        self.base_url = base_url
        if not self.base_url.endswith('/'):
            self.base_url += '/'
            
        # Authentication token (JWT) for Chronos authenticated endpoints
        self.token = token
            
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
        
        logger.info(f"Initializing Chronos client with base URL: {self.base_url}, SSL={self.use_ssl}, verify={self.verify_ssl}, token={'present' if self.token else 'absent'}")
    
    def wait_for_chronos(self, max_retries=30, retry_delay=2):
        """
        Wait for Chronos to be available before attempting operations.
        
        Args:
            max_retries (int): Maximum number of connection attempts (default: 30)
            retry_delay (int): Seconds to wait between retries (default: 2)
            
        Returns:
            bool: True if Chronos is available, False otherwise
        """
        # Use the jobs endpoint to check if Chronos is available
        check_endpoint = 'api/jobs/'
        url = urljoin(self.base_url, check_endpoint)
        verify = self.verify_ssl if self.use_ssl else False
        
        logger.info(f"Waiting for Chronos to be available at {self.base_url} (max {max_retries} retries, {retry_delay}s delay)")
        
        for attempt in range(1, max_retries + 1):
            try:
                headers = {}
                if self.token:
                    headers['Authorization'] = f'Bearer {self.token}'
                    headers['Cookie'] = f'gs-auth={self.token}'
                response = requests.get(url, verify=verify, timeout=5, params={'limit': 1}, headers=headers)
                # Accept 200 (success) as a sign that Chronos is available
                if response.status_code == 200:
                    logger.info(f"Chronos is available (attempt {attempt}/{max_retries})")
                    return True
                else:
                    logger.warning(f"Chronos returned status {response.status_code} (attempt {attempt}/{max_retries})")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"Connection refused to Chronos (attempt {attempt}/{max_retries})")
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout connecting to Chronos (attempt {attempt}/{max_retries})")
            except Exception as e:
                logger.warning(f"Error checking Chronos availability (attempt {attempt}/{max_retries}): {str(e)}")
            
            if attempt < max_retries:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
        
        logger.error(f"Chronos is not available after {max_retries} attempts")
        return False
        
    def _request(self, endpoint, method='GET', data=None, params=None):
        """Make a request to the Chronos API."""
        url = urljoin(self.base_url, endpoint)
        headers = {'Content-Type': 'application/json'}
        
        # Add authentication headers if token is available
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
            headers['Cookie'] = f'gs-auth={self.token}'
        
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
                              name=None, description=None, with_snapshot=False, enabled=True, snapshot_write_method=None):
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
            snapshot_write_method (str, optional): Write method for snapshots (INSERT or UPSERT, default: UPSERT)
            
        Returns:
            dict: The created job details
        """
        if not pipeline_id or not entity_id:
            raise ValueError("Both pipeline_id and entity_id are required")
            
        if task_type not in ('entity_start', 'entity_stop', 'entity_snapshot', 'entity_redo'):
            raise ValueError(f"Invalid task_type for entity: {task_type}")
            
        # Generate a name if not provided
        if not name:
            task_name_map = {
                'entity_start': 'Start',
                'entity_stop': 'Stop',
                'entity_snapshot': 'Snapshot',
                'entity_redo': 'Redo'
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
        
        # Add snapshot_write_method if provided, otherwise default to UPSERT
        if snapshot_write_method:
            if snapshot_write_method not in ('INSERT', 'UPSERT'):
                raise ValueError(f"Invalid snapshot_write_method: {snapshot_write_method}. Must be 'INSERT' or 'UPSERT'")
            job_data['snapshot_write_method'] = snapshot_write_method
        else:
            job_data['snapshot_write_method'] = 'UPSERT'  # Default value
        
        # Add either cron_expression or schedule
        if 'cron_expression' in schedule_config:
            job_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            job_data['schedule'] = schedule_config['schedule']
        else:
            raise ValueError("Either cron_expression or schedule must be provided")
            
        return self.create_job(job_data)
    
    def create_pipeline_schedule(self, pipeline_id, task_type, schedule_config,
                                name=None, description=None, with_snapshot=False, enabled=True, snapshot_write_method=None):
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
            snapshot_write_method (str, optional): Write method for snapshots (INSERT or UPSERT, default: UPSERT)
            
        Returns:
            dict: The created job details
        """
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
            
        if task_type not in ('pipeline_start', 'pipeline_stop', 'pipeline_snapshot', 'pipeline_redo', 'pipeline_enter_maintenance', 'pipeline_exit_maintenance'):
            raise ValueError(f"Invalid task_type for pipeline: {task_type}")
            
        # Generate a name if not provided
        if not name:
            task_name_map = {
                'pipeline_start': 'Start',
                'pipeline_stop': 'Stop',
                'pipeline_snapshot': 'Snapshot',
                'pipeline_redo': 'Redo',
                'pipeline_enter_maintenance': 'Enter Maintenance',
                'pipeline_exit_maintenance': 'Exit Maintenance'
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
        
        # Add snapshot_write_method if provided, otherwise default to UPSERT
        if snapshot_write_method:
            if snapshot_write_method not in ('INSERT', 'UPSERT'):
                raise ValueError(f"Invalid snapshot_write_method: {snapshot_write_method}. Must be 'INSERT' or 'UPSERT'")
            job_data['snapshot_write_method'] = snapshot_write_method
        else:
            job_data['snapshot_write_method'] = 'UPSERT'  # Default value
        
        # Add either cron_expression or schedule
        if 'cron_expression' in schedule_config:
            job_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            job_data['schedule'] = schedule_config['schedule']
        else:
            raise ValueError("Either cron_expression or schedule must be provided")
            
        return self.create_job(job_data)
    def create_group_schedule(self, pipeline_id, group_ids, task_type, schedule_config,
                             name=None, description=None, with_snapshot=False, enabled=True, snapshot_write_method=None):
        """
        Create a schedule for one or more groups with the specified configuration.
        
        Args:
            pipeline_id (str): ID of the pipeline
            group_ids (str or list): ID or list of IDs of the groups
            task_type (str): Type of task to schedule (group_start, group_stop, group_snapshot)
            schedule_config (dict): Schedule configuration dict with either 'cron_expression' or 'schedule'
            name (str, optional): Name for the job 
            description (str, optional): Description for the job
            with_snapshot (bool, optional): Whether to include snapshot when starting group
            enabled (bool, optional): Whether the job is enabled initially
            snapshot_write_method (str, optional): Write method for snapshots (INSERT or UPSERT, default: UPSERT)
            
        Returns:
            dict: The created job details
            
        Example:
            # Using cron expression
            create_group_schedule(
                pipeline_id="b5ba417a",
                group_ids=["fe9d40c8", "group-2", "group-3"],
                task_type="group_snapshot",
                schedule_config={
                    "cron_expression": "30 2 * * 1-5"
                },
                name="Daily Group Backup",
                description="Daily group snapshot at 2:30 AM",
                with_snapshot=True,
                snapshot_write_method="UPSERT",
                enabled=True
            )
            
            # Using schedule with days and time
            create_group_schedule(
                pipeline_id="b5ba417a",
                group_ids=["fe9d40c8"],
                task_type="group_snapshot",
                schedule_config={
                    "schedule": {
                        "days_of_week": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                        "hour": 2,
                        "minute": 30
                    }
                },
                name="Weekday Group Backup",
                description="Weekday group snapshot at 2:30 AM"
            )
        """
        if not pipeline_id or not group_ids:
            raise ValueError("Both pipeline_id and group_ids are required")
            
        # Convert single group_id to list for consistent handling
        if isinstance(group_ids, str):
            group_ids = [group_ids]
            
        if not isinstance(group_ids, list) or not all(isinstance(gid, str) for gid in group_ids):
            raise ValueError("group_ids must be a string or a list of strings")
            
        if task_type not in ('group_start', 'group_stop', 'group_snapshot', 'group_redo'):
            raise ValueError(f"Invalid task_type for group: {task_type}")
            
        # Generate a name if not provided
        if not name:
            task_name_map = {
                'group_start': 'Start',
                'group_stop': 'Stop',
                'group_snapshot': 'Snapshot',
                'group_redo': 'Redo'
            }
            group_names = ", ".join(group_ids[:3])
            if len(group_ids) > 3:
                group_names += f" and {len(group_ids) - 3} more"
            name = f"{task_name_map.get(task_type, 'Schedule')} for groups: {group_names}"
            
        job_data = {
            'name': name,
            'description': description,
            'task_type': task_type,
            'pipeline_id': pipeline_id,
            'group_ids': group_ids,  # Now accepts a list of group IDs
            'with_snapshot': with_snapshot,
            'enabled': enabled,
            'snapshot_write_method': 'UPSERT'  # Default value
        }
        
        # Add snapshot_write_method if provided
        if snapshot_write_method:
            if snapshot_write_method not in ('INSERT', 'UPSERT'):
                raise ValueError(f"Invalid snapshot_write_method: {snapshot_write_method}. Must be 'INSERT' or 'UPSERT'")
            job_data['snapshot_write_method'] = snapshot_write_method
        
        # Handle both cron_expression and schedule formats
        if 'cron_expression' in schedule_config:
            job_data['cron_expression'] = schedule_config['cron_expression']
        elif 'schedule' in schedule_config:
            # For the new schedule format with days_of_week, hour, minute
            if isinstance(schedule_config['schedule'], dict):
                job_data['schedule'] = schedule_config['schedule']
            else:
                # Backward compatibility for simple schedule format
                job_data['schedule'] = schedule_config['schedule']
        else:
            raise ValueError("Either cron_expression or schedule must be provided in schedule_config")
            
        return self.create_job(job_data)

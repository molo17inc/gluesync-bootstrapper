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
import time
import sys
import urllib3
import traceback
import argparse
from pathlib import Path

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import utilities and managers
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, exit_on_fail, lockfile_complete
from utils.gluesync_sdk_client import initialize_gluesync_sdk, get_token, get_gluesync_client
from utils.core_hub_client import CoreHubClient
from utils.config_manager import ConfigManager
from utils.auth_manager import AuthManager
from utils.pipeline_manager import PipelineManager

# Handle imports for optional dependencies
try:
    from utils.chronos_client import ChronosClient
    CHRONOS_AVAILABLE = True
except ImportError:
    CHRONOS_AVAILABLE = False

def parse_arguments():
    """
    Parse command line arguments
    
    Returns:
        argparse.Namespace: Parsed arguments
    """
    parser = argparse.ArgumentParser(description='Gluesync Bootstrapper')
    parser.add_argument('--pipeline-name', type=str, help='Name for the pipeline to create or use')
    parser.add_argument('--create-new-pipeline', action='store_true', help='Force creation of a new pipeline')
    parser.add_argument('--enable-scheduling', action='store_true', help='Enable Chronos scheduling for pipelines and entities')
    parser.add_argument('--config-file', type=str, help='Path to configuration file')
    parser.add_argument('--table-list', type=str, help='Path to YAML file with table definitions')
    
    return parser.parse_args()

# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

def main():
    """Main execution function"""
    try:
        # Parse command line arguments
        args = parse_arguments()
        
        # Initialize configuration manager
        config_manager = ConfigManager(logger)
        
        # Retrieve use_sdk from environment
        use_sdk = os.getenv('USE_SDK', 'False').lower() in ['true', '1', 't', 'y', 'yes']
        
        # Only initialize SDK if USE_SDK is true
        if use_sdk:
            initialize_gluesync_sdk()
        else:
            logger.info("Skipping SDK initialization as USE_SDK is set to false")
        
        # Set up the core hub client
        sdk_client = get_gluesync_client() if use_sdk else None
        core_hub_url = config_manager.get_core_hub_url(sdk_client)
        
        # Configure SSL settings for core hub client
        core_hub_client = CoreHubClient(
            core_hub_url, 
            use_ssl=config_manager.ssl_enabled, 
            verify=not config_manager.ssl_skip_verify
        )
        logger.info(f"Initializing CoreHubClient with: URL={core_hub_url}, SSL={config_manager.ssl_enabled}, verify={not config_manager.ssl_skip_verify}")
        
        # Set up authentication manager
        auth_manager = AuthManager(logger, core_hub_client, use_sdk)
        
        # Authenticate and get token
        token = auth_manager.authenticate()
        if not token:
            logger.error("Authentication failed")
            lockfile_failure()
            return 1
            
        # Set up pipeline manager
        pipeline_manager = PipelineManager(logger, core_hub_client)
        
        # Load pipeline configuration
        config_path = args.config_file or config_manager.file_conf_path
        config = config_manager.load_json_config(config_path)
        
        # Get or create pipeline
        pipeline_name = args.pipeline_name or config.get('pipelineName')
        if args.create_new_pipeline or not pipeline_name:
            pipeline = pipeline_manager.get_or_create_pipeline(token)
        else:
            pipeline = pipeline_manager.get_or_create_pipeline(token, pipeline_name)
        
        pipeline_id = pipeline.get('pipelineId')
        pipeline_name = pipeline.get('pipelineName')
        
        if not pipeline_id:
            logger.error("Failed to get or create pipeline")
            lockfile_failure()
            return 1
            
        log_success(logger, f"Using pipeline: {pipeline_name} (ID: {pipeline_id})")
        
        # Get or create agents
        agents = pipeline_manager.get_agents(token, pipeline_id)
        
        # Create and configure entities if requested
        create_entities = config_manager.create_entities_from_schema or args.table_list
        if create_entities:
            # Load entity definitions from YAML
            yaml_path = args.table_list or config_manager.table_list_yaml
            try:
                entities_config = config_manager.load_yaml_config(yaml_path)
                
                if entities_config and 'agents' in entities_config:
                    # Configure entities using the loaded configuration
                    pipeline_manager.configure_entities(entities_config['agents'], pipeline_id, token)
                    log_success(logger, "Successfully configured entities")
                    
                    # Start entity synchronization
                    success = pipeline_manager.start_entity_syncs(
                        token, 
                        pipeline_id, 
                        config_manager.entity_start_timeout
                    )
                    
                    if success:
                        log_success(logger, "All entities started successfully")
                    else:
                        log_failure(logger, "Failed to start some entities")
                else:
                    logger.warning("No valid entity configuration found in YAML")
            except Exception as yaml_error:
                log_failure(logger, f"Failed to load or process entity configuration: {str(yaml_error)}")
        
        # Set up Chronos scheduling if enabled and available
        enable_scheduling = args.enable_scheduling or os.getenv('ENABLE_SCHEDULING', 'False').lower() in ['true', '1', 't', 'y', 'yes']
        
        if enable_scheduling and CHRONOS_AVAILABLE:
            try:
                chronos_url = os.getenv('CHRONOS_URL', 'http://gluesync-chronos:8000')
                chronos_client = ChronosClient(
                    chronos_url, 
                    token,
                    use_ssl=config_manager.ssl_enabled, 
                    verify=not config_manager.ssl_skip_verify
                )
                
                # Configure schedules from YAML if available
                if 'entities_config' in locals() and entities_config and 'schedules' in entities_config:
                    for schedule in entities_config['schedules']:
                        chronos_client.create_schedule(schedule, pipeline_id)
                        
                log_success(logger, "Successfully configured schedules")
            except Exception as chronos_error:
                log_failure(logger, f"Failed to configure scheduling: {str(chronos_error)}")
        
        # Mark the bootstrapping as complete
        log_success(logger, "Bootstrapping completed successfully")
        lockfile_complete()
        return 0
        
    except Exception as e:
        log_failure(logger, f"Bootstrapping failed: {str(e)}")
        logger.error(traceback.format_exc())
        lockfile_failure()
        return 1

    # Check if we need to create a pipeline or use an existing one
    pipeline_name = args.pipeline_name
    create_new_pipeline = args.create_new_pipeline
    
    # Get or create a pipeline
    try:
        pipeline_id = pipeline_manager.get_or_create_pipeline(token, pipeline_name)
        if not pipeline_id:
            log_failure(logger, "Failed to get or create pipeline")
            lockfile_failure()
            return 1
            
        logger.info(f"Working with pipeline ID: {pipeline_id}")
        
        # List unassigned agents
        unassigned_agents = core_hub_client.request('/unassigned-agents', token=token)
        if not isinstance(unassigned_agents, list):
            log_failure(logger, "Failed to retrieve unassigned agents")
            lockfile_failure()
            return 1
            
        logger.info(f"Found {len(unassigned_agents)} unassigned agents")
        logger.debug(f"Unassigned agents: {json.dumps(unassigned_agents, indent=2)}")
        logger.debug(f"Config agents: {json.dumps(config['agents'], indent=2)}")
    except Exception as e:
        log_failure(logger, f"Error getting pipeline or unassigned agents: {str(e)}")
        lockfile_failure()
        return 1

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
        for conf_agent in config['agents']
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

        core_hub_client.request(
            f"/pipelines/{pipeline_id}/agents/{agent['agentId']}",
            method='PUT',
            token=token
        )

    # Apply agent host credentials
    for agent in agents_to_conf:
        if 'agentId' not in agent:
            logger.warning(f"Agent missing 'agentId' field: {agent}")
            continue

        core_hub_client.request(
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
            core_hub_client.request(
                f"/pipelines/{pipeline_id}/agents/{agent['agentId']}/config/specific",
                method='PUT',
                token=token,
                body={"configuration": agent['specificConfiguration']}
            )

    # Configure entities using the PipelineManager
    pipeline_manager.configure_entities(agents_to_conf, pipeline_id, token)

    # Check if we need to create entities from a schema
    create_entities_from_schema = config.get('create_entities_from_schema')
    if create_entities_from_schema:
        source_schema = create_entities_from_schema
        source_type = config.get('source_type')
        target_type = config.get('target_type')
        target_schema = config.get('target_schema')
        
        # Get the table list yaml path
        table_list_yaml = args.table_list or os.getenv('TABLE_LIST_YAML', 'table-list-template.yaml')

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

            if os.path.exists(table_list_yaml):
                cmd.extend(['--yaml-file', table_list_yaml])
                logger.info(f"Using table list YAML: {table_list_yaml}")
            else:
                logger.warning(f"Table list YAML not found at {table_list_yaml}. Proceeding without it.")

            subprocess.run(cmd, check=True)
            log_success(logger, f"Entity creation completed for pipeline {pipeline_id}, source schema {source_schema}, target schema {target_schema or source_schema}, source type {source_type}, target type {target_type}")
        except subprocess.CalledProcessError as e:
            log_failure(logger, f"Error running entity creation script: {e}")
            lockfile_failure()
            return 1

    try:
        # Set pipeline as ready (exiting from Draft status)
        pipeline_name = config.get('pipeline_name', pipeline_manager.generate_fancy_names(1)[0])
        core_hub_client.request(
                f"/pipelines/{pipeline_id}",
                method='PUT',
                token=token,
                body={'configurationCompleted': True, 'name': pipeline_name}
        )

        # Get the entity start timeout from environment or use default
        entity_start_timeout = int(os.getenv('ENTITY_START_TIMEOUT', '1'))
        time.sleep(entity_start_timeout)

        # Start entity syncs using the PipelineManager
        success = pipeline_manager.start_entity_syncs(token, pipeline_id, entity_start_timeout)
        
        if success:
            # Log successful completion
            log_success(logger, f"Pipeline {pipeline_id} successfully configured and started")
            lockfile_complete()
            return 0
        else:
            log_failure(logger, "Failed to start entity syncs")
            lockfile_failure()
            return 1
    except Exception as error:
        log_failure(logger, f"Error: {error}")
        lockfile_failure()
        return 1

if __name__ == "__main__":
    sys.exit(main())

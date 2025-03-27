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
import json
import yaml
import logging
from utils.log import log_success, log_failure, lockfile_failure

class ConfigManager:
    """
    Manages configuration loading and validation for the bootstrapper.
    """
    
    def __init__(self, logger):
        """
        Initialize the ConfigManager
        
        Args:
            logger: The logger instance to use
        """
        self.logger = logger
        
        # Environment variables and defaults
        self.file_conf_path = os.getenv('FILE_CONF_PATH', './config.json')
        self.core_hub_url = os.getenv('CORE_HUB_URL', 'http://gluesync-core-hub:1717')
        self.use_sdk = os.getenv('USE_SDK', 'False').lower() in ['true', '1', 't', 'y', 'yes']
        self.create_entities_from_schema = os.getenv('CREATE_ENTITIES_FROM_SCHEMA')
        self.target_schema = os.getenv('TARGET_SCHEMA')
        self.source_type = os.getenv('SOURCE_TYPE', 'SQL')
        self.target_type = os.getenv('TARGET_TYPE', 'NoSQL')
        self.table_list_yaml = os.getenv('TABLE_LIST_YAML', 'TABLE_LIST.yaml')
        self.ssl_enabled = os.getenv('SSL_ENABLED', 'False').lower() == 'true'
        self.ssl_skip_verify = os.getenv('SSL_SKIP_VERIFY', 'False').lower() == 'true'
        self.entity_start_timeout = int(os.getenv('ENTITY_START_TIMEOUT', '1'))

    def load_json_config(self, config_path=None):
        """
        Load and validate JSON configuration file
        
        Args:
            config_path: Optional path to the config file, uses default if None
            
        Returns:
            dict: The loaded configuration
        """
        path = config_path or self.file_conf_path
        try:
            with open(path, 'r') as file:
                config = json.load(file)
            log_success(self.logger, f"Loaded configuration from {path}")
            return config
        except Exception as e:
            log_failure(self.logger, f"Failed to load configuration from {path}: {str(e)}")
            lockfile_failure()
            raise

    def load_yaml_config(self, yaml_path=None):
        """
        Load and validate YAML configuration file
        
        Args:
            yaml_path: Optional path to the YAML file, uses default if None
            
        Returns:
            dict: The loaded configuration
        """
        path = yaml_path or self.table_list_yaml
        try:
            with open(path, 'r') as file:
                config = yaml.safe_load(file)
            log_success(self.logger, f"Loaded YAML configuration from {path}")
            return config
        except Exception as e:
            log_failure(self.logger, f"Failed to load YAML configuration from {path}: {str(e)}")
            lockfile_failure()
            raise
            
    def get_core_hub_url(self, sdk_client=None):
        """
        Get the Core Hub URL, trying SDK discovery if applicable
        
        Args:
            sdk_client: Optional SDK client for URL discovery
            
        Returns:
            str: The Core Hub URL to use
        """
        # If URL is explicitly set, use it
        if self.core_hub_url:
            return self.core_hub_url
            
        # Try to discover via SDK if enabled and client is provided
        if self.use_sdk and sdk_client:
            self.logger.info("No CoreHub URL specified, attempting to discover via SDK.")
            try:
                sdk_url = sdk_client.get_core_hub_url()
                if sdk_url:
                    self.logger.info(f"Discovered CoreHub URL via SDK: {sdk_url}")
                    return sdk_url
                else:
                    self.logger.warning("Failed to discover CoreHub URL via SDK, using default.")
            except Exception as e:
                self.logger.error(f"Error during SDK CoreHub URL discovery: {str(e)}")
        else:
            self.logger.info("USE_SDK is false or SDK client not available, skipping SDK CoreHub URL discovery.")
            
        # Fall back to default
        self.logger.info(f"Using default CoreHub URL: {self.core_hub_url}")
        return self.core_hub_url

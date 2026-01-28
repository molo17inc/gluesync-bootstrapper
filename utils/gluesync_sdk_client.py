#!/usr/bin/env python3
"""
 * This file is part of Gluesync Bootstrapper.
 *
 * Gluesync Bootstrapper is dual-licensed under the following licenses:
 *
 * 1. GNU General Public License (GPL) Version 3
 *    You may use, modify, and distribute this software under the terms of the GPL v3.
 *    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
 *    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
 *
 * 2. MOLO17 Commercial License
 *    Alternatively, you may use this software under the MOLO17 Commercial License,
 *    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
 *    for licensing terms and conditions.
 *
 * You must choose one of these licenses to use this software. Using this software implies
 * acceptance of one of these licenses. See the accompanying LICENSE files or contact
 * MOLO17 for more information.
 *
 * Copyright (C) 2025 MOLO17. All rights reserved.
"""

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import urlparse

from gluesync_sdk import (
    GluesyncClient,
    GluesyncError,
    GluesyncConnectionError,
    GluesyncAuthenticationError,
    GluesyncLicenseError
)

logger = logging.getLogger(__name__)

DEFAULT_COREHUB_PORT = 1717

def resolve_gluesync_file(env_var_name, default_filename):
    """Resolve gluesync file path with fallback to shared directory"""
    # Check environment variable first
    env_path = os.getenv(env_var_name)
    if env_path and os.path.exists(env_path):
        return env_path, True
    
    # Check shared directory
    shared_path = f'/opt/gluesync/shared/{default_filename}'
    if os.path.exists(shared_path):
        return shared_path, True
    
    # Check data directory
    data_path = f'/opt/gluesync/data/{default_filename}'
    if os.path.exists(data_path):
        return data_path, True
    
    # Return default path even if not exists
    return env_path or shared_path, False

def normalize_corehub_host(host_value, ssl_enabled, default_port):
    """Normalize CoreHub host value to extract host, port, and build URL"""
    if not host_value:
        return None, None, None
    
    # Parse the URL
    if not host_value.startswith(('http://', 'https://')):
        host_value = f"{'https' if ssl_enabled else 'http'}://{host_value}"
    
    parsed = urlparse(host_value)
    host = parsed.hostname
    port = parsed.port or default_port
    
    scheme = 'https' if ssl_enabled else 'http'
    normalized_url = f"{scheme}://{host}:{port}"
    
    return host, port, normalized_url

class GluesyncSDKClient:
    """Singleton class for managing the Gluesync SDK client connection"""
    
    _instance = None
    _token = None
    _client = None
    _is_initialized = False
    
    @classmethod
    def get_instance(cls):
        """Get the singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @property
    def token(self) -> Optional[str]:
        """Get the current JWT token"""
        return self._token
    
    @property
    def client(self) -> Optional[GluesyncClient]:
        """Get the Gluesync client instance"""
        return self._client
    
    @property
    def is_initialized(self) -> bool:
        """Check if the client is initialized"""
        return self._is_initialized
        
    @property
    def corehub_url(self) -> str:
        """Get the CoreHub URL after discovery"""
        if not self._client or not self._is_initialized:
            return None
            
        # Get the host and port from the client
        host = self._client.host
        port = self._client.port
        
        # Use the helper method to build the URL
        return self._build_corehub_url(host, port)
        
    def _build_corehub_url(self, host, port):
        """Build a proper CoreHub URL with the given host, port and SSL setting
        
        Args:
            host (str): The host name or IP address
            port (int): The port number
            
        Returns:
            str: The formatted CoreHub URL
        """
        if not host:
            return None
            
        # Use default port if None
        if port is None:
            port = 1717
            
        scheme = "https" if os.getenv('SSL_ENABLED', 'False').lower() in ('true', '1', 't') else "http"
        return f"{scheme}://{host}:{port}"
    
    async def initialize(self):
        """Initialize the Gluesync client with indefinite retries and exponential backoff
        
        The method will retry indefinitely with exponential backoff starting at 1 second,
        doubling each time up to 30 seconds, then resetting back to 1 second.
        """
        if self._is_initialized and self._token:
            logger.info("Gluesync SDK client already initialized with valid token")
            return
        elif self._is_initialized and not self._token:
            logger.warning("SDK client marked as initialized but no token available - reinitializing")
            self._is_initialized = False
        
        # Determine SSL settings early for URL normalization
        ssl_enabled = os.getenv('SSL_ENABLED', 'False').lower() in ('true', '1', 't')
        ssl_skip_verify = os.getenv('SSL_SKIP_VERIFY', 'False').lower() in ('true', '1', 't')
        logger.info(f"SSL is {'enabled' if ssl_enabled else 'disabled'}")

        # Parse host and port from CORE_HUB_URL if provided
        host = None
        port = None
        core_hub_url = os.getenv('CORE_HUB_URL', '')
        if core_hub_url:
            logger.info(f"CORE_HUB_URL environment variable set to: {core_hub_url}")
            normalized_host, normalized_port, normalized_url = normalize_corehub_host(
                core_hub_url,
                ssl_enabled,
                DEFAULT_COREHUB_PORT,
            )

            if normalized_host:
                host = normalized_host
                port = normalized_port
                if normalized_url and normalized_url != core_hub_url:
                    os.environ['CORE_HUB_URL'] = normalized_url
                    logger.info(f"Normalized CORE_HUB_URL to: {normalized_url}")
            else:
                logger.warning("Unable to parse CORE_HUB_URL value, falling back to discovery")
        else:
            logger.info("No CoreHub URL provided, will use UDP discovery instead")

        # Get license file path with fallback resolution
        license_file_path, license_exists = resolve_gluesync_file(
            'GLUESYNC_LICENSE_FILE', 'gs-license.dat'
        )
        if not license_exists:
            logger.warning(
                "License file not found at %s (including legacy fallbacks), will attempt to proceed without it",
                license_file_path,
            )

        # Security configuration
        security_config = None
        config_path, config_exists = resolve_gluesync_file(
            'GLUESYNC_SECURITY_CONFIG', 'security-config.json'
        )
        if ssl_enabled:  # Only process security config if SSL is enabled
            if config_exists:
                logger.info(f"Using security config from: {config_path}")
                security_config = config_path
            else:
                logger.warning(
                    "Security config file not found at %s (including legacy fallbacks), will use default settings",
                    config_path,
                )
        elif config_exists:
            # SSL is disabled, so don't use security config even if it exists
            logger.info(
                "Security config found at %s but SSL is disabled - ignoring security config",
                config_path,
            )
            
        # Determine the protocol based on ssl_enabled
        protocol = 'https' if ssl_enabled else 'http'
        
        # Log final configuration before creating client
        logger.info(f"Creating GluesyncClient with:")
        logger.info(f"  - host: {host}")
        logger.info(f"  - port: {port if port is not None else DEFAULT_COREHUB_PORT}")
        logger.info(f"  - protocol: {protocol}")
        logger.info(f"  - use_ssl: {ssl_enabled}")
        logger.info(f"  - verify_ssl: {not ssl_skip_verify}")
        logger.info(f"  - security_config: {security_config}")
        logger.info(f"  - module_tag: {os.getenv('GLUESYNC_MODULE_TAG', 'automator')}")
        
        # Prepare the client arguments
        client_args = {
            'host': host,  # None will trigger UDP discovery
            'port': port if port is not None else 1717,  # Default port 1717 if None
            'license_file_path': license_file_path,
            'module_tag': os.getenv('GLUESYNC_MODULE_TAG', 'automator'),
            'use_ssl': ssl_enabled,
            'security_config': security_config,
            'verify_ssl': not ssl_skip_verify,
            # Add other default parameters as needed
            'ping_interval': 5.0,
            'timeout': 10.0,
            'discovery_start_port': 1717,
            'discovery_port_range': 10
        }
        
        # Log the arguments (without sensitive data)
        safe_args = client_args.copy()
        if 'security_config' in safe_args and safe_args['security_config']:
            safe_args['security_config'] = '[REDACTED]'
        logger.info(f"Initializing GluesyncClient with args: {safe_args}")
        
        # Create the client
        self._client = GluesyncClient(**client_args)
        
        # Set up event handlers
        self._client.on_connected = self._on_connected
        self._client.on_disconnected = self._on_disconnected
        self._client.on_error = self._on_error
        # Note: on_reconnecting, on_reconnected, and on_token_updated are not supported by the current SDK
        
        # Connect to CoreHub with indefinite retry logic for both CORE_HUB_URL and UDP discovery
        retry_count = 0
        backoff_delay = 1  # Start with 1 second delay
        max_backoff = 30  # Maximum backoff of 30 seconds
        cycle_count = 0   # Count full cycles of backoff

        while True:  # Retry indefinitely for both CORE_HUB_URL and UDP discovery
            try:
                if host and port:
                    if retry_count == 0:
                        logger.info(f"Connecting to CoreHub at {protocol}://{host}:{port}...")
                    else:
                        logger.info(f"Retry {retry_count} (cycle {cycle_count}) connecting to CoreHub at {protocol}://{host}:{port}...")
                    await self._client.connect()
                    break  # Connection successful
                else:
                    # UDP discovery mode
                    if retry_count > 0:
                        logger.info(f"Retry {retry_count} (cycle {cycle_count}) for UDP discovery...")
                    else:
                        logger.info("Starting UDP discovery to find CoreHub...")

                    await self._client.connect()

                    # After connect, check if we have a host (discovery worked)
                    if self._client.host:
                        logger.info(f"UDP discovery successful! Found CoreHub at {self._client.host}:{self._client.port}")
                        # Update the discovered host/port for future use
                        host = self._client.host
                        port = self._client.port
                        corehub_url = self._build_corehub_url(host, port)
                        logger.info(f"Updated CoreHub URL to {corehub_url}")
                        break  # Connection successful
                    else:
                        # If no host was discovered, raise an error to trigger retry
                        raise GluesyncConnectionError("UDP discovery did not find a CoreHub")

            except GluesyncConnectionError as e:
                retry_count += 1
                logger.warning(f"Connection attempt {retry_count} failed: {e}")

                # Calculate backoff with exponential increase
                logger.info(f"Waiting {backoff_delay} seconds before next retry...")
                await asyncio.sleep(backoff_delay)

                # Double the backoff for next time, up to the maximum
                backoff_delay = min(backoff_delay * 2, max_backoff)

                # If we've reached max backoff, reset on the next failure
                if backoff_delay >= max_backoff:
                    backoff_delay = 1  # Reset to 1 second
                    cycle_count += 1   # Increment cycle count
                    logger.info(f"Completed backoff cycle {cycle_count}, resetting delay to 1 second")

            except (GluesyncLicenseError, GluesyncAuthenticationError) as e:
                # Don't retry for these errors
                logger.error(f"{type(e).__name__}: {e}")
                raise
        
        # Verify we have a token after connection
        connection_timeout = 10  # seconds
        token_check_interval = 0.5  # seconds
        total_wait = 0
        
        while total_wait < connection_timeout and not self._token:
            await asyncio.sleep(token_check_interval)
            total_wait += token_check_interval
            logger.debug(f"Waiting for token... ({total_wait}s/{connection_timeout}s)")
        
        if self._token:
            self._is_initialized = True
            logger.info("Gluesync SDK client initialized successfully with token")
        else:
            self._is_initialized = False
            logger.error("SDK client connected but no token received within timeout")
            raise GluesyncAuthenticationError("No authentication token received after connection")
    
    async def shutdown(self):
        """Shutdown the Gluesync client"""
        if self._client and self._client.is_connected:
            logger.info("Disconnecting from CoreHub...")
            await self._client.disconnect()
            self._is_initialized = False
            self._token = None
    
    async def _on_connected(self, token):
        """
        Handle the connected event.
        
        Args:
            token: The JWT token received from the server
        """
        if token:
            self._token = token
            self._is_initialized = True  # Mark as initialized when connected
            logger.info(f"Connected to CoreHub successfully! Token received and stored.")
            logger.debug(f"Token length: {len(token) if token else 0} characters")
        else:
            logger.error("Connected to CoreHub but no token provided in callback")
            self._token = None
            self._is_initialized = False
    
    async def _on_disconnected(self, reason):
        """
        Handle the disconnected event.
        
        Args:
            reason: The reason for disconnection
        """
        logger.warning(f"Disconnected from CoreHub: {reason}")
        
        # Clear authentication state
        old_token = self._token
        self._token = None
        self._is_initialized = False
        
        if old_token:
            logger.info("Previous authentication token invalidated")
        
        # Note: The SDK handles reconnection internally, so we don't need to start a reconnection task
        logger.info("SDK will handle reconnection automatically")
    
    async def _on_error(self, error):
        """
        Handle the error event.
        
        Args:
            error: The exception that occurred
        """
        logger.error(f"Error in connection: {error}")
        
        # Reset token and initialization state
        self._token = None
        self._is_initialized = False
        
        # Note: The SDK handles reconnection internally, so we don't need custom reconnection logic
        logger.info("Connection error occurred - SDK will handle reconnection automatically")
    
    # Note: Callback methods for on_reconnecting, on_reconnected, and on_token_updated
    # have been removed as they are not supported by the current SDK version.
    # The SDK handles reconnection internally and updates tokens through the on_connected callback.
        
    # Note: Custom reconnection methods have been removed as the SDK handles
    # reconnection internally. The SDK will automatically attempt to reconnect
    # when the connection is lost and will call the on_connected callback
    # when reconnection is successful.
        
# Create a global instance for easy import
gluesync_sdk_client = GluesyncSDKClient.get_instance()


# Legacy compatibility functions for bootstrapper
def initialize_gluesync_sdk():
    """Legacy synchronous wrapper - starts async initialization in background"""
    import threading
    
    def run_async_init():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(gluesync_sdk_client.initialize())
        except Exception as e:
            logger.error(f"SDK initialization failed: {e}")
    
    thread = threading.Thread(target=run_async_init, daemon=True)
    thread.start()
    logger.info("SDK initialization started in background")


def get_token():
    """Legacy function to get token"""
    return gluesync_sdk_client.token


def get_corehub_url():
    """Legacy function to get CoreHub URL"""
    return gluesync_sdk_client.corehub_url

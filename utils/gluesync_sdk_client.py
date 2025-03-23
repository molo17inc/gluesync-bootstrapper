import os
import json
import sys
import logging

logger = logging.getLogger(__name__)

# Try to import the SDK, but provide a mock implementation if it's not available
try:
    from gluesync_sdk import GluesyncSDK
    SDK_AVAILABLE = True
except ImportError:
    logger.warning("gluesync_sdk module not found. Using mock implementation.")
    SDK_AVAILABLE = False
    
    # Mock implementation of GluesyncSDK
    class MockGluesyncSDK:
        def __init__(self, **kwargs):
            self.license_file = kwargs.get('license_file')
            self.module_tag = kwargs.get('module_tag')
            logger.warning(f"Initialized mock SDK with module_tag={self.module_tag}")
            
        def get_token(self):
            return "mock-token"
            
        def get_core_hub_url(self):
            return os.getenv('CORE_HUB_URL', 'http://gluesync-core-hub:1717')
    
    GluesyncSDK = MockGluesyncSDK

# Try to load security configuration if needed
security_config = {}
try:
    security_config_path = os.getenv('GLUESYNC_SECURITY_CONFIG', '/opt/gluesync/data/security-config.json')
    if os.path.exists(security_config_path):
        with open(security_config_path) as f:
            security_config = json.load(f)
            logger.info(f"Loaded security config from {security_config_path}")
    else:
        logger.warning(f"Security config not found at {security_config_path}")
except Exception as e:
    logger.error(f"Error loading security config: {str(e)}")

# Singleton client instance
_gluesync_client = None

def initialize_gluesync_sdk():
    global _gluesync_client
    if _gluesync_client is None:
        try:
            license_file = os.getenv('GLUESYNC_LICENSE_FILE', '/opt/gluesync/data/gs-license.dat')
            module_tag = os.getenv('GLUESYNC_MODULE_TAG', 'gluesync-bootstrapper')
            use_ssl = os.getenv('GLUESYNC_USE_SSL', 'False').lower() == 'true'
            
            # Get keystore info from security config if available
            keystore_path = None
            keystore_password = None
            if security_config and 'ssl' in security_config:
                keystore_path = security_config['ssl'].get('sslCertificatePath')
                keystore_password = security_config['ssl'].get('certificatePassword')
            
            logger.info(f"Initializing Gluesync SDK with module_tag={module_tag}, use_ssl={use_ssl}")
            
            # Determine host from CORE_HUB_URL if available
            core_hub_url = os.getenv('CORE_HUB_URL', 'http://gluesync-core-hub:1717')
            from urllib.parse import urlparse
            parsed_url = urlparse(core_hub_url)
            host = parsed_url.hostname
            port = parsed_url.port or 1717
            
            _gluesync_client = GluesyncSDK(
                host=host,
                port=port,
                license_file_path=license_file,
                module_tag=module_tag,
                ssl=use_ssl,
                security_config=security_config
            )
            
            logger.info("Gluesync SDK initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Gluesync SDK: {str(e)}")
            # If SDK is available but initialization failed, create a mock client
            if SDK_AVAILABLE:
                _gluesync_client = MockGluesyncSDK(module_tag=os.getenv('GLUESYNC_MODULE_TAG', 'gluesync-bootstrapper'))
                logger.warning("Using mock SDK client due to initialization failure")


def get_token():
    if _gluesync_client is None:
        raise RuntimeError("Gluesync SDK is not initialized")
    # Access the _token attribute instead of calling get_token()
    return _gluesync_client._token


def get_gluesync_client():
    if _gluesync_client is None:
        raise RuntimeError("Gluesync SDK is not initialized")
    return _gluesync_client

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
            logger.debug(f"Using CoreHub URL: {core_hub_url}")
            
            from urllib.parse import urlparse
            parsed_url = urlparse(core_hub_url)
            host = parsed_url.hostname
            port = parsed_url.port or 1717
            logger.debug(f"Parsed host: {host}, port: {port}")
            
            # Check if license file exists
            if not os.path.exists(license_file):
                logger.error(f"License file not found at {license_file}")
            else:
                logger.debug(f"License file found at {license_file}")
                
            # Log SDK class information
            logger.debug(f"SDK class: {GluesyncSDK.__name__}, module: {GluesyncSDK.__module__}")
            
            # Initialize the SDK with debug logging
            logger.debug("Creating SDK client instance...")
            logger.debug(f"Initialization parameters:")
            logger.debug(f"  - host: {host}")
            logger.debug(f"  - port: {port}")
            logger.debug(f"  - license_file_path: {license_file}")
            logger.debug(f"  - module_tag: {module_tag}")
            logger.debug(f"  - ssl: {use_ssl}")
            logger.debug(f"  - security_config: {json.dumps(security_config) if security_config else None}")
            
            try:
                _gluesync_client = GluesyncSDK(
                    host=host,
                    port=port,
                    license_file_path=license_file,
                    module_tag=module_tag,
                    ssl=use_ssl,
                    security_config=security_config
                )
                logger.debug("SDK client instance created successfully")
            except Exception as e:
                logger.error(f"Exception during SDK client creation: {str(e)}")
                logger.error(f"Exception type: {type(e).__name__}")
                import traceback
                logger.error(f"Stack trace: {traceback.format_exc()}")
                raise
            
            # Check if initialization was successful and inspect the client
            if _gluesync_client is None:
                logger.error("SDK client is None after initialization")
            else:
                logger.debug(f"SDK client type: {type(_gluesync_client).__name__}")
                logger.debug(f"SDK client attributes: {dir(_gluesync_client)}")
                
                # Check if _token attribute exists
                if hasattr(_gluesync_client, '_token'):
                    token_value = _gluesync_client._token
                    logger.debug(f"Token exists: {token_value is not None}")
                    if token_value is None:
                        logger.warning("Token is None after initialization")
                else:
                    logger.error("_token attribute not found on SDK client")
                    # Try to find any token-related attributes
                    token_attrs = [attr for attr in dir(_gluesync_client) if 'token' in attr.lower()]
                    if token_attrs:
                        logger.debug(f"Found token-related attributes: {token_attrs}")
            
            logger.info("Gluesync SDK initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Gluesync SDK: {str(e)}")
            # If SDK is available but initialization failed, create a mock client
            if SDK_AVAILABLE:
                _gluesync_client = MockGluesyncSDK(module_tag=os.getenv('GLUESYNC_MODULE_TAG', 'gluesync-bootstrapper'))
                logger.warning("Using mock SDK client due to initialization failure")


def get_token():
    if _gluesync_client is None:
        logger.error("Gluesync SDK is not initialized when trying to get token")
        raise RuntimeError("Gluesync SDK is not initialized")
    
    logger.debug(f"Client type: {type(_gluesync_client).__name__}")
    
    # Try multiple ways to get the token
    token = None
    
    # Method 1: Access the _token attribute
    try:
        token = getattr(_gluesync_client, '_token', None)
        logger.debug(f"Method 1 (_token attribute): {token is not None}")
    except Exception as e:
        logger.error(f"Error accessing _token attribute: {str(e)}")
    
    # Method 2: Try token property if it exists
    if token is None and hasattr(_gluesync_client, 'token'):
        try:
            token = _gluesync_client.token
            logger.debug(f"Method 2 (token property): {token is not None}")
        except Exception as e:
            logger.error(f"Error accessing token property: {str(e)}")
    
    # Method 3: Try get_token method if it exists
    if token is None and hasattr(_gluesync_client, 'get_token'):
        try:
            token = _gluesync_client.get_token()
            logger.debug(f"Method 3 (get_token method): {token is not None}")
        except Exception as e:
            logger.error(f"Error calling get_token method: {str(e)}")
    
    # Debug logging
    if token is None:
        logger.error(f"Token is None after all retrieval attempts")
        logger.error(f"Client attributes: {dir(_gluesync_client)}")
        
        # Try to find any token-related attributes
        token_attrs = [attr for attr in dir(_gluesync_client) if 'token' in attr.lower()]
        if token_attrs:
            logger.debug(f"Token-related attributes found: {token_attrs}")
            for attr in token_attrs:
                try:
                    value = getattr(_gluesync_client, attr)
                    logger.debug(f"Attribute '{attr}' value: {value}")
                except Exception as e:
                    logger.error(f"Error accessing attribute '{attr}': {str(e)}")
    else:
        logger.info(f"Successfully retrieved token from Gluesync SDK")
    
    return token


def get_gluesync_client():
    if _gluesync_client is None:
        raise RuntimeError("Gluesync SDK is not initialized")
    return _gluesync_client

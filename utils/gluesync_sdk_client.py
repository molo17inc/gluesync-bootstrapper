import os
import json
import sys
import logging

logger = logging.getLogger(__name__)

# Import the SDK - no mock implementation
from gluesync_sdk import GluesyncSDK

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
            use_ssl = os.getenv('SSL_ENABLED', 'False').lower() == 'true'
            
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
                # Pass the path to the security config file, not the dictionary
                security_config_path = os.getenv('GLUESYNC_SECURITY_CONFIG', '/opt/gluesync/data/security-config.json')
                _gluesync_client = GluesyncSDK(
                    host=host,
                    port=port,
                    license_file_path=license_file,
                    module_tag=module_tag,
                    ssl=use_ssl,
                    security_config=security_config_path
                )
                logger.debug("SDK client instance created successfully")
                
                # Check if the client needs to be connected
                if hasattr(_gluesync_client, 'connect') and hasattr(_gluesync_client, 'is_connected'):
                    # Check if is_connected is a method or a property
                    if callable(getattr(_gluesync_client, 'is_connected')):
                        # It's a method
                        if not _gluesync_client.is_connected():
                            logger.debug("Client is not connected. Attempting to connect...")
                            try:
                                # Check if connect is a coroutine function
                                import inspect
                                if inspect.iscoroutinefunction(_gluesync_client.connect):
                                    logger.debug("Connect is a coroutine, using asyncio to connect")
                                    import asyncio
                                    # Create an event loop if one doesn't exist
                                    try:
                                        loop = asyncio.get_event_loop()
                                    except RuntimeError:
                                        loop = asyncio.new_event_loop()
                                        asyncio.set_event_loop(loop)
                                    # Run the connect coroutine
                                    loop.run_until_complete(_gluesync_client.connect())
                                else:
                                    _gluesync_client.connect()
                                logger.debug("Client connection successful")
                            except Exception as e:
                                logger.error(f"Failed to connect client: {str(e)}")
                        else:
                            logger.debug("Client is already connected")
                    else:
                        # It's a property
                        if not _gluesync_client.is_connected:
                            logger.debug("Client is not connected (property). Attempting to connect...")
                            try:
                                # Check if connect is a coroutine function
                                import inspect
                                if inspect.iscoroutinefunction(_gluesync_client.connect):
                                    logger.debug("Connect is a coroutine, using asyncio to connect")
                                    import asyncio
                                    # Create an event loop if one doesn't exist
                                    try:
                                        loop = asyncio.get_event_loop()
                                    except RuntimeError:
                                        loop = asyncio.new_event_loop()
                                        asyncio.set_event_loop(loop)
                                    # Run the connect coroutine
                                    loop.run_until_complete(_gluesync_client.connect())
                                else:
                                    _gluesync_client.connect()
                                logger.debug("Client connection successful")
                            except Exception as e:
                                logger.error(f"Failed to connect client: {str(e)}")
                        else:
                            logger.debug("Client is already connected (property)")
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
            logger.error(f"Exception type: {type(e).__name__}")
            import traceback
            logger.error(f"Stack trace: {traceback.format_exc()}")
            # No mock fallback - just raise the exception
            raise RuntimeError(f"Failed to initialize Gluesync SDK: {str(e)}")


def get_token():
    if _gluesync_client is None:
        logger.error("Gluesync SDK is not initialized when trying to get token")
        raise RuntimeError("Gluesync SDK is not initialized")
    
    logger.debug(f"Client type: {type(_gluesync_client).__name__}")
    
    # Ensure client is connected if it has connection methods
    if hasattr(_gluesync_client, 'connect') and hasattr(_gluesync_client, 'is_connected'):
        # Check if is_connected is a method or a property
        if callable(getattr(_gluesync_client, 'is_connected')):
            # It's a method
            if not _gluesync_client.is_connected():
                logger.debug("Client is not connected when trying to get token. Attempting to connect...")
                try:
                    # Check if connect is a coroutine function
                    import inspect
                    if inspect.iscoroutinefunction(_gluesync_client.connect):
                        logger.debug("Connect is a coroutine, using asyncio to connect during token retrieval")
                        import asyncio
                        # Create an event loop if one doesn't exist
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        # Run the connect coroutine
                        loop.run_until_complete(_gluesync_client.connect())
                    else:
                        _gluesync_client.connect()
                    logger.debug("Client connection successful during token retrieval")
                except Exception as e:
                    logger.error(f"Failed to connect client during token retrieval: {str(e)}")
        else:
            # It's a property
            if not _gluesync_client.is_connected:
                logger.debug("Client is not connected (property) when trying to get token. Attempting to connect...")
                try:
                    # Check if connect is a coroutine function
                    import inspect
                    if inspect.iscoroutinefunction(_gluesync_client.connect):
                        logger.debug("Connect is a coroutine, using asyncio to connect during token retrieval")
                        import asyncio
                        # Create an event loop if one doesn't exist
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                        # Run the connect coroutine
                        loop.run_until_complete(_gluesync_client.connect())
                    else:
                        _gluesync_client.connect()
                    logger.debug("Client connection successful during token retrieval")
                except Exception as e:
                    logger.error(f"Failed to connect client during token retrieval: {str(e)}")
    
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
            # Check if token is a property with a getter that might be async
            import inspect
            token_property = getattr(type(_gluesync_client), 'token', None)
            if token_property and isinstance(token_property, property) and inspect.iscoroutinefunction(token_property.fget):
                logger.debug("token property getter is a coroutine, using asyncio to get token")
                import asyncio
                # Create an event loop if one doesn't exist
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                # Run the token property getter coroutine
                token = loop.run_until_complete(token_property.fget(_gluesync_client))
            else:
                token = _gluesync_client.token
            logger.debug(f"Method 2 (token property): {token is not None}")
        except Exception as e:
            logger.error(f"Error accessing token property: {str(e)}")
    
    # Method 3: Try get_token method if it exists
    if token is None and hasattr(_gluesync_client, 'get_token'):
        try:
            # Check if get_token is a coroutine function
            import inspect
            if inspect.iscoroutinefunction(_gluesync_client.get_token):
                logger.debug("get_token is a coroutine, using asyncio to get token")
                import asyncio
                # Create an event loop if one doesn't exist
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                # Run the get_token coroutine
                token = loop.run_until_complete(_gluesync_client.get_token())
            else:
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

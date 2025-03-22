import os
import json
from gluesync_sdk import GluesyncSDK

# Load security configuration
with open('/path/to/security-config.json') as f:
    security_config = json.load(f)

# Singleton client instance
_gluesync_client = None

def initialize_gluesync_sdk():
    global _gluesync_client
    if _gluesync_client is None:
        license_file = os.getenv('GLUESYNC_LICENSE_FILE', 'gs-license.dat')
        module_tag = os.getenv('GLUESYNC_MODULE_TAG', 'gluesync-bootstrapper')
        use_ssl = os.getenv('GLUESYNC_USE_SSL', 'False').lower() == 'true'
        keystore_path = security_config['ssl']['sslCertificatePath']
        keystore_password = security_config['ssl']['certificatePassword']
        
        _gluesync_client = GluesyncSDK(
            license_file=license_file,
            module_tag=module_tag,
            use_ssl=use_ssl,
            keystore_path=keystore_path,
            keystore_password=keystore_password
        )


def get_token():
    if _gluesync_client is None:
        raise RuntimeError("Gluesync SDK is not initialized")
    return _gluesync_client.get_token()


def get_gluesync_client():
    if _gluesync_client is None:
        raise RuntimeError("Gluesync SDK is not initialized")
    return _gluesync_client

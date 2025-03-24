import requests
from urllib.parse import urlparse
import urllib3
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

logger = logging.getLogger(__name__)

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ProtocolAwareAdapter(HTTPAdapter):
    """HTTP adapter that's aware of secure vs insecure connections."""
    def __init__(self, *args, **kwargs):
        # Initialize _is_secure before calling super().__init__
        # to ensure it exists before init_poolmanager is called
        self._is_secure = kwargs.pop('is_secure', False)
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        if self._is_secure:
            context = create_urllib3_context()
            kwargs['ssl_context'] = context
        else:
            kwargs['assert_hostname'] = False
            kwargs['cert_reqs'] = 'CERT_NONE'
        
        return super().init_poolmanager(*args, **kwargs)

class CoreHubClient:
    """Client for handling CoreHub API requests with protocol awareness."""
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        
        # Parse URL to determine protocol
        parsed_url = urlparse(base_url)
        is_secure = parsed_url.scheme == 'https'
        
        # Create adapter with is_secure parameter
        self.adapter = ProtocolAwareAdapter(is_secure=is_secure)

        # Mount adapter for both HTTP and HTTPS
        self.session.mount('http://', self.adapter)
        self.session.mount('https://', self.adapter)

    def request(self, path, method='GET', token=None, body=None, params=None):
        url = f"{self.base_url}{path}"
        headers = {}
        
        if token:
            headers['Authorization'] = f'Bearer {token}'
        
        try:
            if method.upper() == 'GET':
                response = self.session.get(url, headers=headers, verify=False, params=params)
            elif method.upper() == 'POST':
                headers['Content-Type'] = 'application/json'
                response = self.session.post(url, headers=headers, json=body, verify=False, params=params)
            elif method.upper() == 'PUT':
                headers['Content-Type'] = 'application/json'
                response = self.session.put(url, headers=headers, json=body, verify=False, params=params)
            elif method.upper() == 'DELETE':
                response = self.session.delete(url, headers=headers, verify=False, params=params)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            
            # Force raise for status
            response.raise_for_status()
            
            # Parse JSON response or return text if not JSON
            try:
                return response.json()
            except ValueError:
                return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"CoreHub API request failed: {str(e)}")
            # Include response details if available
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Status code: {e.response.status_code}")
                logger.error(f"Response: {e.response.text}")
            raise

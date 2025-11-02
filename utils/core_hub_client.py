import requests
import os
from urllib.parse import urlparse, urlunparse
import urllib3
import logging

logger = logging.getLogger(__name__)

# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CoreHubClient:
    """Client for handling CoreHub API requests."""

    def __init__(self, base_url, *, use_ssl=None, skip_verify=None):
        """
        Initialize the CoreHub client.

        Args:
            base_url (str): Base URL for CoreHub. If no scheme is provided, http is assumed.
            use_ssl (bool, optional): Force SSL usage. Defaults to environment variable ``SSL_ENABLED``.
            skip_verify (bool, optional): Skip certificate verification. Defaults to ``SSL_SKIP_VERIFY`` env.
        """

        # Normalize URL to ensure a scheme is always present
        parsed_url = urlparse(base_url if '://' in base_url else f"http://{base_url}")
        if not parsed_url.scheme:
            parsed_url = parsed_url._replace(scheme='http')

        # Resolve SSL configuration
        if use_ssl is None:
            use_ssl = os.getenv('SSL_ENABLED', 'False').lower() == 'true'
        if skip_verify is None:
            skip_verify = os.getenv('SSL_SKIP_VERIFY', 'False').lower() == 'true'

        # Update URL scheme when SSL is enabled and scheme is explicitly http
        if use_ssl and parsed_url.scheme == 'http':
            updated_url = urlunparse(('https',) + parsed_url[1:])
            logger.info(f"SSL enabled: Changed CoreHub URL from {base_url} to {updated_url}")
            base_url = updated_url
            parsed_url = urlparse(base_url)
        else:
            base_url = urlunparse(parsed_url)

        self.base_url = base_url
        self.use_ssl = use_ssl
        self.verify_ssl = not skip_verify

        logger.info(
            "Initializing CoreHubClient with: URL=%s, SSL=%s, verify=%s",
            self.base_url,
            self.use_ssl,
            self.verify_ssl,
        )

        self.session = requests.Session()

    def request(self, path, method='GET', token=None, body=None, params=None):
        url = f"{self.base_url}{path}"
        headers = {}
        
        if token:
            headers['Authorization'] = f'Bearer {token}'
        
        # Use configured SSL verification setting
        verify = self.verify_ssl if self.use_ssl else False
        
        try:
            if method.upper() == 'GET':
                response = self.session.get(url, headers=headers, verify=verify, params=params)
            elif method.upper() == 'POST':
                headers['Content-Type'] = 'application/json'
                response = self.session.post(url, headers=headers, json=body, verify=verify, params=params)
            elif method.upper() == 'PUT':
                headers['Content-Type'] = 'application/json'
                response = self.session.put(url, headers=headers, json=body, verify=verify, params=params)
            elif method.upper() == 'DELETE':
                response = self.session.delete(url, headers=headers, verify=verify, params=params)
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

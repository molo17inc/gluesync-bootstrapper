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

import requests
import os
import time
from urllib.parse import urlparse, urlunparse
import urllib3
import logging
from utils.redact import redact

logger = logging.getLogger(__name__)

try:  # the dry-run guard of the backup import options (absent when this module is used on its own)
    from import_options import guard_write as _guard_write
except ImportError:  # pragma: no cover
    def _guard_write(*_args):
        return None

# Timeouts of every CoreHub request, (connect, read) seconds: before, a request could wait forever on a CoreHub
# or database that stopped answering. Long enough for a slow discovery or a function collection build.
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 300.0
# GET requests only (idempotent) are retried, on a connection error, a timeout or a 502/503/504; POST, PUT and
# DELETE are never retried, since CoreHub may have applied a request whose answer was lost.
DEFAULT_GET_RETRIES = 2
# A credentials PUT makes CoreHub open a pool on the agent's database: against an unreachable host it answered
# after ~76 s (Core Hub 2.3.1). Its read timeout is never below CORE_HUB_CREDENTIALS_TIMEOUT.
DEFAULT_CREDENTIALS_TIMEOUT = 120.0
DEFAULT_RETRY_BACKOFF = 1.0
_RETRYABLE_STATUSES = frozenset({502, 503, 504})


def _float_env(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        logger.warning(f"Ignoring invalid {name}={os.getenv(name)!r}; using {default}")
        return float(default)


def request_timeout():
    """The ``(connect, read)`` timeout of a CoreHub request: ``CORE_HUB_CONNECT_TIMEOUT`` / ``CORE_HUB_TIMEOUT``."""
    return (_float_env('CORE_HUB_CONNECT_TIMEOUT', DEFAULT_CONNECT_TIMEOUT),
            _float_env('CORE_HUB_TIMEOUT', DEFAULT_READ_TIMEOUT))


def credentials_request_timeout():
    """The ``(connect, read)`` timeout of a credentials PUT: read is at least ``CORE_HUB_CREDENTIALS_TIMEOUT``."""
    connect, read = request_timeout()
    return connect, max(read, _float_env('CORE_HUB_CREDENTIALS_TIMEOUT', DEFAULT_CREDENTIALS_TIMEOUT))


def get_retries():
    """How many times a failed GET is sent again (``CORE_HUB_GET_RETRIES``) and the first backoff in seconds."""
    return (max(0, int(_float_env('CORE_HUB_GET_RETRIES', DEFAULT_GET_RETRIES))),
            max(0.0, _float_env('CORE_HUB_RETRY_BACKOFF', DEFAULT_RETRY_BACKOFF)))


def retry_get(send, describe, sleep=time.sleep):
    """``send()`` (a GET), sent again with exponential backoff on a transient failure; the last outcome.

    A response is retried on 502/503/504, an exception on a connection error or a timeout; anything else
    (a 4xx, a 500) is returned or raised at once.
    """
    retries, backoff = get_retries()
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            response = send()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            if last:
                raise
            logger.warning(f"{describe} failed ({error}); retry {attempt + 1}/{retries}")
        else:
            if getattr(response, 'status_code', None) not in _RETRYABLE_STATUSES or last:
                return response
            logger.warning(f"{describe} answered {response.status_code}; retry {attempt + 1}/{retries}")
        sleep(backoff * (2 ** attempt))
    raise AssertionError("unreachable")  # pragma: no cover


# Disable insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _log_request_error(e, expected_error_statuses):
    """Log a failed CoreHub request at the level its status calls for.

    Expected statuses (e.g. 404 during table existence checks) are logged as
    info, 401 Unauthorized as a warning (it is expected during initial
    authentication attempts), anything else as an error.
    """
    response = e.response if hasattr(e, 'response') else None
    has_response = response is not None
    if expected_error_statuses and has_response and response.status_code in expected_error_statuses:
        log = logger.info
        log(f"CoreHub API request returned expected status: {str(e)}")
    else:
        log = logger.warning if has_response and response.status_code == 401 else logger.error
        log(f"CoreHub API request failed: {str(e)}")
    # Include response details if available
    if has_response:
        log(f"Status code: {response.status_code}")
        log(f"Response: {response.text}")


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

    def request(self, path, method='GET', token=None, body=None, params=None, headers=None, expected_error_statuses=None):
        _guard_write(method, path)
        url = f"{self.base_url}{path}"
        headers = self._prepare_headers(token, headers)

        # Use configured SSL verification setting
        verify = self.verify_ssl if self.use_ssl else False

        try:
            response = self._send(method, url, headers, body, verify, params)

            # Force raise for status
            response.raise_for_status()

            # Parse JSON response or return text if not JSON
            try:
                return response.json()
            except ValueError:
                return response.text
        except requests.exceptions.RequestException as e:
            _log_request_error(e, expected_error_statuses)
            raise

    def _prepare_headers(self, token, headers):
        """Return a copy of ``headers`` with the auth and encoding headers set."""
        headers = headers.copy() if headers else {}

        if token:
            headers['Authorization'] = f'Bearer {token}'
            # Also sync the token as the gs-auth cookie so that CoreHub
            # instances whose extractAuthHeader reads the cookie (rather than
            # the Authorization header) still authenticate correctly.  This
            # matters for subprocesses that start a fresh requests.Session()
            # and therefore have no cookies carried over from a prior login.
            parsed = urlparse(self.base_url)
            self.session.cookies.set('gs-auth', token, domain=parsed.hostname or 'localhost')

        # Disable automatic gzip acceptance to avoid issues with servers that
        # send malformed/double Content-Encoding headers
        if 'Accept-Encoding' not in headers:
            headers['Accept-Encoding'] = 'identity'
        return headers

    def _send(self, method, url, headers, body, verify, params):
        """Send the request with the session method matching ``method``."""
        timeout = request_timeout()
        if method.upper() == 'GET':
            return retry_get(lambda: self.session.get(url, headers=headers, verify=verify, params=params,
                                                      timeout=timeout),
                             f"GET {url}")
        if method.upper() in ('POST', 'PUT'):
            return self._send_with_body(method.upper(), url, headers, body, verify, params)
        if method.upper() == 'DELETE':
            return self.session.delete(url, headers=headers, verify=verify, params=params, timeout=timeout)
        raise ValueError(f"Unsupported HTTP method: {method}")

    def _send_with_body(self, verb, url, headers, body, verify, params):
        """Send a POST or PUT: raw bytes as they are, anything else as JSON."""
        send = self.session.post if verb == 'POST' else self.session.put
        timeout = (credentials_request_timeout() if verb == 'PUT' and url.split('?')[0].endswith('/config/credentials')
                   else request_timeout())
        if isinstance(body, (bytes, bytearray)):
            # For binary data, don't set Content-Type - send raw bytes
            if verb == 'PUT':
                logger.debug(f"Sending binary PUT: {len(body)} bytes, headers: {redact(headers)}")
            response = send(url, headers=headers, data=body, verify=verify, params=params,
                            timeout=timeout)
            if verb == 'PUT':
                logger.debug(f"Binary PUT response status: {response.status_code}")
            return response
        # For JSON data, set Content-Type if not already set
        if 'Content-Type' not in headers:
            headers['Content-Type'] = 'application/json'
        return send(url, headers=headers, json=body, verify=verify, params=params, timeout=timeout)

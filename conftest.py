"""Shared pytest setup: the repository root on sys.path, and no real network call from any test.

Tests that talk to a fake Core Hub mock fetch_core_hub/requests themselves or serve on localhost; any other HTTP
request (an unmocked GET to a default Core Hub URL) fails at once instead of waiting for timeouts and retries.
"""
import os
import sys
from urllib.parse import urlparse

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, os.path.join(ROOT, "dbmoto-converter")):
    if path not in sys.path:
        sys.path.insert(0, path)

# No retry/backoff of failed GETs while testing (a test that checks retries sets these itself)
os.environ.setdefault("CORE_HUB_GET_RETRIES", "0")
os.environ.setdefault("CORE_HUB_CONNECT_TIMEOUT", "2")
os.environ.setdefault("CORE_HUB_TIMEOUT", "5")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _e2e_allowed_hosts():
    """Hostnames live e2e may call (GS_E2E_HUB_URL and optional GS_E2E_ALLOW_HOSTS). Never hardcode secrets."""
    allowed = set()
    for var in ("GS_E2E_HUB_URL", "GS_COREHUB_URL", "GS_E2E_AUTOMATOR_URL"):
        hub = (os.getenv(var) or "").strip()
        if hub:
            host = urlparse(hub).hostname
            if host:
                allowed.add(host.lower())
    for item in (os.getenv("GS_E2E_ALLOW_HOSTS") or "").split(","):
        item = item.strip().lower()
        if item:
            allowed.add(item)
    return allowed


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    try:
        import requests
        from requests.adapters import HTTPAdapter
    except ImportError:  # pragma: no cover
        yield
        return
    original_send = HTTPAdapter.send
    allowed = _LOCAL_HOSTS | _e2e_allowed_hosts()

    def guarded_send(self, request, *args, **kwargs):
        if (urlparse(request.url).hostname or "").lower() in allowed:
            return original_send(self, request, *args, **kwargs)
        raise requests.exceptions.ConnectionError(f"network disabled in tests: {request.method} {request.url}")

    monkeypatch.setattr(HTTPAdapter, "send", guarded_send)
    yield

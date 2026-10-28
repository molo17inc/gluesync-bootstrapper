"""e2e fixtures: skip live tests unless GS_E2E_HUB_URL is set; share HubSession.

On Mac, Core Hub is typically https://localhost (self-signed) and DBs are on localhost ports.
All connection details come from env vars — see RUN_E2E.md / README.md.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Live e2e needs longer Hub timeouts than the unit-test defaults in root conftest.
if (os.getenv("GS_E2E_HUB_URL") or "").strip():
    os.environ.setdefault("CORE_HUB_GET_RETRIES", "2")
    os.environ.setdefault("CORE_HUB_CONNECT_TIMEOUT", "10")
    os.environ.setdefault("CORE_HUB_TIMEOUT", "120")

from . import db_probe  # noqa: E402
from . import env as e2e_env  # noqa: E402
from .hub_api import HubSession  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: live Core Hub end-to-end tests (need GS_E2E_HUB_URL)")


def _live_reason() -> str:
    if not e2e_env.hub_configured():
        return "GS_E2E_HUB_URL + password (GS_E2E_HUB_PASSWORD / GS_COREHUB_PASSWORD / GS_E2E_HUB_PASSWORD_ENV) not set"
    return ""


skip_without_hub = pytest.mark.skipif(bool(_live_reason()), reason=_live_reason() or "live hub required")


@pytest.fixture(scope="session")
def hub_env():
    reason = _live_reason()
    if reason:
        pytest.skip(reason)
    return e2e_env.load_hub()


@pytest.fixture(scope="session")
def agents_env():
    return e2e_env.load_agents()


@pytest.fixture(scope="session")
def hub(hub_env):
    session = HubSession(hub_env)
    yield session
    # Id-only: delete pipelines this session's tests recorded (never by name).
    deleted = session.delete_tracked_pipelines()
    if deleted:
        print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")


@pytest.fixture(params=["mssql", "mysql", "postgres"])
def target_db(request):
    db = e2e_env.load_db(request.param)
    ok, reason = db_probe.available(db)
    if not ok:
        pytest.skip(f"{request.param}: {reason}")
    return db


@pytest.fixture
def overrides_json(tmp_path, agents_env):
    """Agent override file from env (localhost-friendly). Passwords only via passwordEnv names."""
    path = tmp_path / "overrides.json"
    # Always infer dialect from agents_env.target_tag. Do not honour GS_E2E_TARGET_DIALECT here —
    # a leftover TARGET_DIALECT=mssql in the shell would put MSSQL ports under mysql-cdc.
    path.write_text(
        json.dumps(e2e_env.overrides_dict_for(agents_env), indent=2),
        encoding="utf-8",
    )
    # Ensure GS_IMPORT_AGENT_PASSWORD_* (or configured names) are populated for this process
    for key, value in e2e_env.ensure_agent_passwords_in_environ(agents_env).items():
        os.environ.setdefault(key, value)
    return path

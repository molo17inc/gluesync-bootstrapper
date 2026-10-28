"""Live e2e: Automator HTTP endpoints /api/import/* (paths from automator_app/app.py).

Needs GS_E2E_AUTOMATOR_URL and hub credentials (login posts baseUrl+user+password to /api/login).
Session is process-global on the Automator (state.token); no cookie required after login.
"""
from __future__ import annotations

import os

import pytest
import requests

from .zip_builder import synth_archive

pytestmark = [pytest.mark.e2e]


def _automator_url():
    return (os.getenv("GS_E2E_AUTOMATOR_URL") or "").rstrip("/")


skip_without_automator = pytest.mark.skipif(
    not _automator_url(), reason="GS_E2E_AUTOMATOR_URL not set"
)


@pytest.fixture
def automator_session():
    base = _automator_url()
    if not base:
        pytest.skip("GS_E2E_AUTOMATOR_URL not set")
    hub_url = os.getenv("GS_E2E_HUB_URL") or os.getenv("GS_COREHUB_URL")
    password = os.getenv("GS_E2E_HUB_PASSWORD") or os.getenv("GS_COREHUB_PASSWORD")
    if not hub_url or not password:
        pytest.skip("hub credentials required to login Automator")
    username = os.getenv("GS_E2E_HUB_USERNAME") or os.getenv("GS_COREHUB_USERNAME") or "admin"
    skip_verify = (os.getenv("GS_E2E_SKIP_VERIFY") or "1").lower() in ("1", "true", "yes", "on")
    sess = requests.Session()
    sess.verify = False
    resp = sess.post(f"{base}/api/login", json={
        "baseUrl": hub_url,
        "username": username,
        "password": password,
        "useSsl": hub_url.lower().startswith("https://"),
        "skipVerify": skip_verify,
        "enableScheduling": False,
        "createTables": True,
    }, timeout=60)
    assert resp.status_code == 200, f"Automator login failed: {resp.status_code} {resp.text[:300]}"
    yield sess
    try:
        sess.post(f"{base}/api/logout", timeout=30)
    except Exception:
        pass


@skip_without_automator
def test_login_then_analyze_synth_zip(automator_session):
    """POST /api/login then /api/import/analyze — returns uploadId + plan (keys from _analyze_archive)."""
    base = _automator_url()
    files = {"file": ("synth1.zip", synth_archive("synth1"), "application/zip")}
    resp = automator_session.post(f"{base}/api/import/analyze", files=files, timeout=120)
    assert resp.status_code == 200, resp.text[:500]
    body = resp.json()
    assert "uploadId" in body
    assert "plan" in body
    assert "pipelines" in body


@skip_without_automator
def test_preview_after_analyze(automator_session):
    """POST /api/import/preview with ImportPreviewRequest (uploadId, plan, agent_passwords)."""
    base = _automator_url()
    files = {"file": ("synth1.zip", synth_archive("synth1"), "application/zip")}
    analysis = automator_session.post(f"{base}/api/import/analyze", files=files, timeout=120)
    assert analysis.status_code == 200, analysis.text[:500]
    body = analysis.json()
    preview = automator_session.post(
        f"{base}/api/import/preview",
        json={
            "uploadId": body["uploadId"],
            "plan": body.get("plan"),
            "agent_passwords": {
                "5e1f0001": {
                    "mssql-ct": os.getenv("GS_IMPORT_AGENT_PASSWORD_MSSQL_CT")
                        or os.getenv("GS_E2E_MSSQL_PASSWORD") or "unset",
                }
            },
        },
        timeout=180,
    )
    # Preview may fail validation if agents unreachable — still must not 404/5xx from routing
    assert preview.status_code in (200, 400, 409), preview.text[:500]


@skip_without_automator
def test_import_all_form_fields_documented(automator_session):
    """POST /api/import/all accepts file + agent_overrides + plan + resume_pipeline_id + uploadId.

    Uses analyze uploadId then a plan that imports nothing (all import:false) to avoid writing —
    if plan refuses empty selection, expect 400 PlanError (from _check_plan).
    """
    base = _automator_url()
    files = {"file": ("synth1.zip", synth_archive("synth1"), "application/zip")}
    analysis = automator_session.post(f"{base}/api/import/analyze", files=files, timeout=120).json()
    # Empty selection should be refused by PlanError ("selects no pipeline")
    import json
    plan = analysis["plan"]
    for pid, entry in (plan.get("pipelines") or {}).items():
        if isinstance(entry, dict):
            entry["import"] = False
    resp = automator_session.post(
        f"{base}/api/import/all",
        data={
            "uploadId": analysis["uploadId"],
            "plan": json.dumps(plan),
            "agent_overrides": None,
        },
        timeout=120,
    )
    assert resp.status_code in (400, 409), resp.text[:500]


@skip_without_automator
def test_import_routes_not_missing():
    base = _automator_url()
    # Unauthenticated: 401 is fine; 404 means route missing
    for path in ("/api/import/analyze", "/api/import/all", "/api/import/preview",
                 "/api/import/validate-all", "/api/import/full-backup",
                 "/api/import/users", "/api/import/oidc", "/api/import/ai-settings"):
        resp = requests.post(base + path, timeout=30, verify=False)
        assert resp.status_code != 404, path

"""Offline tests for e2e kit registry (no hub)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "integration-tests"))

from e2e import env as e2e_env  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_kit_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GS_E2E_") or k.startswith("GS_IMPORT_"):
            monkeypatch.delenv(k, raising=False)
    yield


def test_offline_only_kits_never_configured():
    for kit in e2e_env.OFFLINE_ONLY_KITS:
        assert not e2e_env.kit_configured(kit)


def test_available_kits_respects_role_and_env(monkeypatch):
    monkeypatch.setenv("GS_E2E_MSSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_MSSQL_PASSWORD", "x")
    monkeypatch.setenv("GS_E2E_KAFKA_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_VERTICA_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_VERTICA_PASSWORD", "v")
    src = e2e_env.available_kits(role="source")
    tgt = e2e_env.available_kits(role="target")
    assert "mssql" in src and "kafka" not in src and "vertica" not in src
    assert "kafka" in tgt and "vertica" in tgt and "mssql" in tgt


def test_overrides_pair_uses_host_docker_internal(monkeypatch):
    monkeypatch.setenv("GS_E2E_MSSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_MSSQL_PASSWORD", "x")
    monkeypatch.setenv("GS_E2E_MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_MYSQL_PASSWORD", "y")
    monkeypatch.setenv("GS_E2E_AGENT_CONNECT_HOST", "host.docker.internal")
    ov = e2e_env.overrides_dict_for_pair("mssql", "mysql")
    assert "mssql-ct" in ov and "mysql-cdc" in ov
    assert ov["mssql-ct"]["host"] == "host.docker.internal"
    assert ov["mysql-cdc"]["port"] == 3306


def test_same_tag_pair_keys_by_agent_id_with_distinct_password_env(monkeypatch):
    """MySQL→MySQL (same agentTag) must not collapse overrides or share one passwordEnv."""
    import os
    from e2e import env as e2e_env
    import agent_overrides

    monkeypatch.setenv("GS_E2E_MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("GS_E2E_MYSQL_PASSWORD", "secret-mysql")
    monkeypatch.setenv("GS_E2E_MYSQL_PORT", "3306")
    monkeypatch.setenv("GS_E2E_MYSQL_DATABASE", "demo")
    monkeypatch.setenv("GS_E2E_MYSQL_USERNAME", "gluesync")
    ov = e2e_env.overrides_dict_for_pair("mysql", "mysql", "mysql-cdc", "mysql-cdc")
    assert e2e_env.SYNTH_SOURCE_AGENT_ID in ov
    assert e2e_env.SYNTH_TARGET_AGENT_ID in ov
    assert "mysql-cdc" not in ov  # must not key by tag alone
    assert ov[e2e_env.SYNTH_SOURCE_AGENT_ID]["passwordEnv"] != ov[e2e_env.SYNTH_TARGET_AGENT_ID]["passwordEnv"]
    assert ov[e2e_env.SYNTH_SOURCE_AGENT_ID]["passwordEnv"].endswith("_SOURCE")
    assert ov[e2e_env.SYNTH_TARGET_AGENT_ID]["passwordEnv"].endswith("_TARGET")
    # apply with role-suffixed env vars set — check_password_vars must accept
    e2e_env.ensure_all_kit_passwords_in_environ()
    agents = [
        {"agentId": "a9e00001", "agentType": "SOURCE", "agentTag": "mysql-cdc",
         "hostCredentials": {"host": "old", "port": 1, "databaseName": "x", "username": "u"}},
        {"agentId": "a9e00002", "agentType": "TARGET", "agentTag": "mysql-cdc",
         "hostCredentials": {"host": "old", "port": 1, "databaseName": "x", "username": "u"}},
    ]
    lines = agent_overrides.apply(agents, ov, env=os.environ)
    assert agents[0]["hostCredentials"]["host"] != "old"
    assert agents[1]["hostCredentials"]["host"] != "old"
    assert agents[0]["hostCredentials"]["password"] == agents[1]["hostCredentials"]["password"] == "secret-mysql"

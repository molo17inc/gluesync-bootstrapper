"""Live e2e: import_backup CLI — dry-run, exit codes, plan, overrides."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from .conftest import skip_without_hub
from .runner import run_import_backup
from .zip_builder import live_synth_archive, synth_archive
from .runner import run_import_backup_tracked
from . import env as e2e_env

pytestmark = [pytest.mark.e2e, skip_without_hub]


@skip_without_hub
def test_dry_run_exits_0_or_1_never_writes(hub, hub_env, overrides_json, agents_env):
    before = {str(p.get("pipelineId") or p.get("id")) for p in hub.list_pipelines()}
    code = run_import_backup(live_synth_archive(agents_env), hub_env, overrides_json, dry_run=True)
    assert code in (0, 1)
    after = {str(p.get("pipelineId") or p.get("id")) for p in hub.list_pipelines()}
    assert after == before


@skip_without_hub
def test_write_plan_then_import_with_plan(hub_env, overrides_json, tmp_path, agents_env):
    from automator_app import import_backup
    zpath = tmp_path / "s.zip"
    zpath.write_bytes(live_synth_archive(agents_env))
    plan_path = tmp_path / "plan.json"
    assert import_backup.main([str(zpath), "--write-plan", str(plan_path)]) == 0
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert "5e1f0001" in plan["pipelines"]
    code = run_import_backup(
        zpath.read_bytes(), hub_env, overrides_json, "--plan", str(plan_path), dry_run=True,
    )
    assert code in (0, 1)


@skip_without_hub
def test_full_import_exit_code_semantics(hub_env, overrides_json, hub, agents_env):
    if not e2e_env.agents_ready():
        pytest.skip("GS_E2E_AGENTS_READY not set — full import hangs on credential PUT without agents")
    code = run_import_backup_tracked(live_synth_archive(agents_env), hub_env, overrides_json, hub)
    assert code in (0, 1, 2)
    # Id-only cleanup: delete only pipelines this test recorded as created
    deleted = hub.delete_tracked_pipelines()
    if deleted:
        print(f"e2e_cleanup_tracked_ids={','.join(deleted)}")


@skip_without_hub
def test_agent_overrides_refuse_inline_password(tmp_path):
    import agent_overrides
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mssql-ct": {"host": "127.0.0.1", "password": "x"}}), encoding="utf-8")
    payload = bad.read_text(encoding="utf-8")
    with pytest.raises(agent_overrides.AgentOverrideError):
        agent_overrides.load(payload)

"""Run automator_app.import_backup against a live hub with env-configured overrides."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from . import env as e2e_env


def run_import_backup(
    zip_bytes: bytes,
    hub_env: e2e_env.HubEnv,
    overrides_path: Path,
    *extra: str,
    create_tables: bool = True,
    dry_run: bool = False,
    extra_env: Optional[dict] = None,
) -> int:
    from automator_app import import_backup

    agents = e2e_env.load_agents()
    env = {
        hub_env.password_env_name: hub_env.password,
        "START_SYNC": "false",
        "CREATE_TABLE_IF_NOT_EXISTS": "true" if create_tables else "false",
    }
    env.update(e2e_env.ensure_agent_passwords_in_environ(agents))
    if extra_env:
        env.update(extra_env)
    for key, value in env.items():
        if value is not None:
            os.environ[key] = value

    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "backup.zip"
        zpath.write_bytes(zip_bytes)
        argv = [
            str(zpath),
            "--base-url", hub_env.url,
            "--username", hub_env.username,
            "--password-env", hub_env.password_env_name,
            "--agent-overrides", str(overrides_path),
        ]
        if hub_env.skip_verify:
            argv.append("--skip-verify")
        if dry_run:
            argv.append("--dry-run")
        if not create_tables:
            argv.append("--no-create-tables")
        argv.extend(extra)
        return import_backup.main(argv)


def run_import_backup_tracked(
    zip_bytes: bytes,
    hub_env: e2e_env.HubEnv,
    overrides_path: Path,
    hub: "HubSession",
    *extra: str,
    create_tables: bool = True,
    dry_run: bool = False,
    extra_env: Optional[dict] = None,
) -> int:
    """Like run_import_backup, but records any newly created pipeline ids on ``hub`` for id-only cleanup."""
    before = {str(p.get("pipelineId") or p.get("id") or "") for p in hub.list_pipelines()}
    code = 1
    try:
        code = run_import_backup(
            zip_bytes, hub_env, overrides_path, *extra,
            create_tables=create_tables, dry_run=dry_run, extra_env=extra_env,
        )
    finally:
        # Record ids even when import raises / pytest-timeout fires mid-request, so cleanup
        # can delete ONLY what this call created (never by name).
        if not dry_run:
            try:
                hub.snapshot_new(before)
            except Exception:
                pass
    return code

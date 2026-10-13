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

"""Automator export round trip against a live CoreHub.

Given a pipeline that is already configured, this:

1. exports it to a table-list YAML (the same export the CLI and Automator use);
2. restores that YAML as a second pipeline, with the same agent config file,
   through ``main.py`` with ``START_SYNC=false`` so nothing is synced twice;
3. exports the restored pipeline again;
4. checks that the restored pipeline is configured like the original
   (entities, groups, Chronos schedules) and that the two exports match.

Everything lands in ``--output-dir``: both exports, both CoreHub snapshots,
the restore log, ``report.json``, a per-check diff and a JUnit file.
Exit code 0 means every check passed, 1 means at least one did not.

The demo-kits integration tests run this inside each kit's bootstrapper
service once the kit's own tests are green.
"""

import argparse
import copy
import difflib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import yaml

from commons import fetch_core_hub
from export_template_from_corehub import (
    fetch_pipeline_entities,
    fetch_pipeline_jobs,
    render_pipeline_export,
)
from utils.log import create_log_file, get_logger

log_file = create_log_file()
logger = get_logger(log_file)

ROUND_TRIP_SUFFIX = " [automator round-trip]"
DEFAULT_TOKEN_FILE = "/opt/config/auth_token.json"
SCRIPT_DIR = Path(__file__).resolve().parent

# Runtime state, not configuration: these differ between two pipelines that
# are configured identically, or change while the original one is syncing.
VOLATILE_KEYS = frozenset({
    "status", "state", "entityStatus", "syncStatus", "running", "isRunning",
    "lastUpdate", "lastUpdated", "lastModified", "updatedAt", "createdAt",
    "created_at", "updated_at", "creationDate", "lastRun", "last_run",
    "last_run_at", "next_run", "next_run_at", "nextRun", "metrics",
    "progress", "snapshotProgress", "lastError", "errors", "warnings",
    "checkpoint", "offset", "lastCheckpoint",
})

# Chronos job fields that only identify the job row itself.
JOB_IDENTITY_KEYS = frozenset({"id", "job_id", "pipeline_id"})

MAX_DIFF_LINES = 400


# ---------------------------------------------------------------------------
# Normalisation (pure functions, unit tested offline)
# ---------------------------------------------------------------------------

def build_id_map(
    pipeline_id: str,
    agents: List[Dict[str, Any]],
    entities: List[Dict[str, Any]],
    groups: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Map every pipeline-specific id to a stable, name-based placeholder."""
    id_map: Dict[str, str] = {}
    if pipeline_id:
        id_map[str(pipeline_id)] = "<pipeline>"
    for agent in agents:
        agent_id = agent.get("agentId") or agent.get("id")
        if agent_id:
            id_map[str(agent_id)] = f"<agent:{str(agent.get('agentType') or '?').upper()}>"
    agent_types = {str(a.get("agentId") or a.get("id")): str(a.get("agentType") or "?").upper() for a in agents}
    for entity in entities:
        entity_id = entity.get("entityId")
        if entity_id:
            id_map[str(entity_id)] = f"<entity:{entity.get('entityName')}>"
        for agent_entity in entity.get("agentEntities") or []:
            agent_entity_id = agent_entity.get("agentEntityId") if isinstance(agent_entity, dict) else None
            if agent_entity_id:
                side = agent_types.get(str(agent_entity.get("agentId")), str(agent_entity.get("type") or "?").upper())
                id_map[str(agent_entity_id)] = f"<agentEntity:{entity.get('entityName')}:{side}>"
    for group in groups:
        group_id = group.get("groupId")
        if group_id:
            id_map[str(group_id)] = f"<group:{group.get('name')}>"
    return id_map


def normalize(value: Any, id_map: Dict[str, str], drop_keys: frozenset = VOLATILE_KEYS) -> Any:
    """Return a copy with ids replaced by placeholders and runtime keys removed."""
    if isinstance(value, dict):
        return {
            key: normalize(item, id_map, drop_keys)
            for key, item in value.items()
            if key not in drop_keys
        }
    if isinstance(value, list):
        return canonical_order([normalize(item, id_map, drop_keys) for item in value])
    if isinstance(value, str):
        if value in id_map:
            return id_map[value]
        for raw_id, placeholder in id_map.items():
            if len(raw_id) >= 8 and raw_id in value:
                value = value.replace(raw_id, placeholder)
        return value
    return value


# Lists whose items all carry one of these keys are sets of named things
# (columns, mappings): their order is not configuration, each item carries its
# own position. Lists of plain values (keys, whitelists) keep their order.
ORDER_KEYS = ("name", "source", "entityName")


def canonical_order(items: List[Any]) -> List[Any]:
    """Sort a list of named dicts by name; leave every other list alone."""
    if len(items) < 2 or not all(isinstance(i, dict) for i in items):
        return items
    for key in ORDER_KEYS:
        if all(key in i for i in items):
            return sorted(items, key=lambda i: json.dumps(i.get(key), sort_keys=True, default=str))
    return items


def key_by_name(items: List[Dict[str, Any]], name_key: str) -> Dict[str, Dict[str, Any]]:
    """Index items by name; repeated names get a ``#n`` suffix in order."""
    keyed: Dict[str, Dict[str, Any]] = {}
    for item in items:
        name = str(item.get(name_key))
        key = name
        n = 2
        while key in keyed:
            key = f"{name}#{n}"
            n += 1
        keyed[key] = item
    return keyed


def strip_export_header(text: str) -> str:
    """Drop the provenance comment block (URL, pipeline id, timestamp)."""
    lines = text.splitlines()
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines) + "\n"


def json_lines(value: Any) -> List[str]:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def unified_diff(left: Any, right: Any, left_name: str, right_name: str) -> List[str]:
    left_lines = left if isinstance(left, list) and all(isinstance(x, str) for x in left) else json_lines(left)
    right_lines = right if isinstance(right, list) and all(isinstance(x, str) for x in right) else json_lines(right)
    return list(difflib.unified_diff(left_lines, right_lines, left_name, right_name, lineterm="", n=3))


class Check:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.passed = True
        self.message = ""
        self.diff: List[str] = []

    def fail(self, message: str, diff: Optional[List[str]] = None) -> None:
        self.passed = False
        self.message = message
        if diff:
            self.diff = diff

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "passed": self.passed,
            "message": self.message,
            "diffLines": len(self.diff),
        }


def compare_keyed(
    check: Check,
    original: Dict[str, Any],
    restored: Dict[str, Any],
    what: str,
) -> Check:
    """Fail ``check`` when the two name-keyed collections differ."""
    missing = sorted(set(original) - set(restored))
    extra = sorted(set(restored) - set(original))
    changed = sorted(k for k in set(original) & set(restored) if original[k] != restored[k])
    if not (missing or extra or changed):
        check.message = f"{len(original)} {what} match"
        return check

    parts = []
    if missing:
        parts.append(f"missing after restore: {', '.join(missing)}")
    if extra:
        parts.append(f"only in restore: {', '.join(extra)}")
    if changed:
        parts.append(f"configured differently: {', '.join(changed)}")
    diff: List[str] = []
    for key in changed:
        diff.extend(unified_diff(original[key], restored[key], f"original/{key}", f"restored/{key}"))
    if missing or extra:
        diff.extend(unified_diff(sorted(original), sorted(restored), f"original/{what}", f"restored/{what}"))
    check.fail(f"{len(original)} {what} in the original, {len(restored)} after restore; " + "; ".join(parts), diff)
    return check


def align_source_custom_properties(original: Dict[str, Any], restored: Dict[str, Any]) -> None:
    """Accept the effective source settings that a restore writes out explicitly.

    The export carries the source entityType (what CoreHub runs with) and the
    import stores that same dict as the source agent entity's customProperties,
    which CoreHub only keeps (it reads customProperties on the target side
    alone). So a restored source customProperties may hold more keys than the
    original: each extra key is accepted only when it equals the restored
    entityType value of that key. Anything else still shows as a difference.
    """
    pairs = zip(original.get("agentEntities") or [], restored.get("agentEntities") or [])
    for left_ae, right_ae in pairs:
        if not isinstance(left_ae, dict) or not isinstance(right_ae, dict):
            continue
        if (right_ae.get("entityType") or {}).get("type") != "Source":
            continue
        left_cp = left_ae.get("customProperties") or {}
        right_cp = right_ae.get("customProperties") or {}
        effective = right_ae.get("entityType") or {}
        extra = {k: v for k, v in right_cp.items() if k not in left_cp}
        if all(effective.get(k) == v for k, v in extra.items()):
            right_ae["customProperties"] = {k: v for k, v in right_cp.items() if k in left_cp}


def check_entities(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    check = Check("restored-entities-match", "Every entity is restored with the same configuration")
    left = {k: normalize(v, original["idMap"]) for k, v in key_by_name(original["entities"], "entityName").items()}
    right = {k: normalize(v, restored["idMap"]) for k, v in key_by_name(restored["entities"], "entityName").items()}
    for name in set(left) & set(right):
        align_source_custom_properties(left[name], right[name])
    if not left:
        check.fail("the original pipeline has no entities, so the round trip proves nothing")
        return check
    return compare_keyed(check, left, right, "entities")


def check_groups(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    check = Check("restored-groups-match", "Entity groups are restored with the same members")
    left = {k: normalize(v, original["idMap"]) for k, v in key_by_name(original["groups"], "name").items()}
    right = {k: normalize(v, restored["idMap"]) for k, v in key_by_name(restored["groups"], "name").items()}
    return compare_keyed(check, left, right, "groups")


def check_schedules(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    check = Check("restored-schedules-match", "Chronos schedules are restored for the new pipeline")
    drop = VOLATILE_KEYS.union(JOB_IDENTITY_KEYS)
    left = {k: normalize(v, original["idMap"], drop) for k, v in key_by_name(original["jobs"], "name").items()}
    right = {k: normalize(v, restored["idMap"], drop) for k, v in key_by_name(restored["jobs"], "name").items()}
    return compare_keyed(check, left, right, "schedules")


def check_export_stable(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    check = Check("export-is-stable", "Exporting the restored pipeline gives back the same YAML")
    left = normalize(yaml.safe_load(strip_export_header(original["export"])) or {}, original["idMap"])
    right = normalize(yaml.safe_load(strip_export_header(restored["export"])) or {}, restored["idMap"])
    if left == right:
        check.message = "both exports are identical"
        return check
    left_text = yaml.safe_dump(left, sort_keys=True, allow_unicode=True).splitlines()
    right_text = yaml.safe_dump(right, sort_keys=True, allow_unicode=True).splitlines()
    check.fail(
        "the export of the restored pipeline differs from the original export",
        unified_diff(left_text, right_text, "original-export.yaml", "restored-export.yaml"),
    )
    return check


def run_checks(original: Dict[str, Any], restored: Dict[str, Any]) -> List[Check]:
    return [
        check_entities(original, restored),
        check_groups(original, restored),
        check_schedules(original, restored),
        check_export_stable(original, restored),
    ]


def junit_xml(suite_name: str, checks: List[Check], duration: float) -> str:
    failures = sum(1 for c in checks if not c.passed)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name={quoteattr(suite_name)} tests="{len(checks)}" failures="{failures}" '
        f'time="{duration:.1f}">',
    ]
    for c in checks:
        lines.append(f'  <testcase classname="automator-roundtrip" name={quoteattr(c.name)}>')
        if not c.passed:
            body = "\n".join(c.diff[:MAX_DIFF_LINES])
            if len(c.diff) > MAX_DIFF_LINES:
                body += f"\n... {len(c.diff) - MAX_DIFF_LINES} more lines in {c.name}.diff"
            lines.append(f"    <failure message={quoteattr(c.message)}>{escape(body)}</failure>")
        else:
            lines.append(f"    <system-out>{escape(c.message)}</system-out>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CoreHub side
# ---------------------------------------------------------------------------

def _as_list(response: Any, *keys: str) -> List[Dict[str, Any]]:
    if isinstance(response, list):
        return [x for x in response if isinstance(x, dict)]
    if isinstance(response, dict):
        for key in keys:
            if isinstance(response.get(key), list):
                return [x for x in response[key] if isinstance(x, dict)]
    return []


def authenticate(token_file: str) -> str:
    """Reuse the token the kit's bootstrapper saved, else log in as admin."""
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token = json.load(f).get("token")
        if token and isinstance(fetch_core_hub("/pipelines", token=token), list):
            return token
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Saved token in {token_file} not usable: {exc}")

    response = fetch_core_hub(
        "/authentication/login",
        method="POST",
        body={"username": "admin", "password": os.getenv("DEFAULT_PASSWORD", "")},
    )
    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise RuntimeError("cannot authenticate to CoreHub: no saved token and admin login failed")
    return token


def snapshot_pipeline(token: str, pipeline_id: str) -> Dict[str, Any]:
    """Read everything the round trip compares for one pipeline."""
    pipeline = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
    config = fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)
    # Only identity: the agent config itself comes from the kit's config file,
    # and its credentials have no place in a test artifact.
    agents = [
        {k: a.get(k) for k in ("agentId", "agentType", "agentTag", "agentUserTag") if k in a}
        for a in _as_list(config.get("agents") if isinstance(config, dict) else None)
    ]
    entities = fetch_pipeline_entities(token, pipeline_id)
    try:
        groups = _as_list(fetch_core_hub(f"/pipelines/{pipeline_id}/config/groups", token=token), "groups")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"No groups for pipeline {pipeline_id}: {exc}")
        groups = []
    jobs = fetch_pipeline_jobs(pipeline_id, token=token)
    return {
        "pipelineId": pipeline_id,
        "pipeline": pipeline,
        "agents": agents,
        "entities": entities,
        "groups": groups,
        "jobs": jobs,
        "idMap": build_id_map(pipeline_id, agents, entities, groups),
    }


def restore_export(
    export_path: Path,
    pipeline_name: str,
    out_dir: Path,
    timeout: int,
) -> Tuple[Optional[str], str]:
    """Run main.py on the export; return (restored pipeline id, error)."""
    result_file = out_dir / "restore-result.json"
    env = dict(os.environ)
    env.update({
        "TABLE_LIST_YAML": str(export_path),
        "START_SYNC": "false",
        "CREATE_TABLE_IF_NOT_EXISTS": "false",
        "BOOTSTRAPPER_RESULT_FILE": str(result_file),
    })
    cmd = [sys.executable, str(SCRIPT_DIR / "main.py"), "--pipeline-name", pipeline_name]
    logger.info(f"Restoring {export_path} as pipeline '{pipeline_name}'")
    with open(out_dir / "restore.log", "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, f"restore did not finish within {timeout}s"
    if not result_file.exists():
        return None, f"restore exited with code {proc.returncode} before creating a pipeline (see restore.log)"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("status") != "complete":
        return result.get("pipelineId"), f"restore of pipeline {result.get('pipelineId')} failed (see restore.log)"
    return result.get("pipelineId"), ""


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def run(pipeline_id: str, out_dir: Path, token_file: str, timeout: int, suite_name: str) -> int:
    started = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    checks: List[Check] = []

    def finish() -> int:
        duration = time.time() - started
        for c in checks:
            if c.diff:
                (out_dir / f"{c.name}.diff").write_text("\n".join(c.diff) + "\n", encoding="utf-8")
        write_json(out_dir / "report.json", {
            "pipelineId": pipeline_id,
            "passed": all(c.passed for c in checks),
            "durationSeconds": round(duration, 1),
            "checks": [c.to_dict() for c in checks],
        })
        (out_dir / "junit.xml").write_text(junit_xml(suite_name, checks, duration), encoding="utf-8")
        for c in checks:
            logger.info(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.message}")
        return 0 if all(c.passed for c in checks) else 1

    step = Check("export-and-restore", "The pipeline exports and the export restores as a new pipeline")
    checks.append(step)
    try:
        token = authenticate(token_file)
        original = snapshot_pipeline(token, pipeline_id)
        original["export"] = render_pipeline_export(token, pipeline_id)
    except Exception as exc:  # pylint: disable=broad-except
        step.fail(f"export of pipeline {pipeline_id} failed: {exc}")
        return finish()

    export_path = out_dir / "original-export.yaml"
    export_path.write_text(original["export"], encoding="utf-8")
    write_json(out_dir / "original-snapshot.json", {k: v for k, v in original.items() if k != "export"})

    name = (original["pipeline"] or {}).get("name") if isinstance(original["pipeline"], dict) else None
    restored_id, error = restore_export(export_path, f"{name or pipeline_id}{ROUND_TRIP_SUFFIX}", out_dir, timeout)
    if error:
        step.fail(error)
        return finish()

    try:
        restored = snapshot_pipeline(token, restored_id)
        restored["export"] = render_pipeline_export(token, restored_id)
    except Exception as exc:  # pylint: disable=broad-except
        step.fail(f"export of restored pipeline {restored_id} failed: {exc}")
        return finish()
    (out_dir / "restored-export.yaml").write_text(restored["export"], encoding="utf-8")
    write_json(out_dir / "restored-snapshot.json", {k: v for k, v in restored.items() if k != "export"})
    step.message = f"pipeline {pipeline_id} exported and restored as {restored_id}"

    checks.extend(run_checks(original, restored))
    return finish()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pipeline", required=True, help="ID of the pipeline to round-trip")
    parser.add_argument("--output-dir", required=True, help="Where the exports, diffs and reports go")
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE,
                        help=f"Saved CoreHub token (default {DEFAULT_TOKEN_FILE})")
    parser.add_argument("--restore-timeout", type=int, default=1800,
                        help="Seconds the restore may take (default 1800)")
    parser.add_argument("--suite-name", default=os.getenv("TEST_NAME", "automator-roundtrip"),
                        help="JUnit suite name (default $TEST_NAME)")
    args = parser.parse_args(argv)
    return run(args.pipeline, Path(args.output_dir), args.token_file, args.restore_timeout, args.suite_name)


if __name__ == "__main__":
    sys.exit(main())

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

1. exports it to a table-list YAML (the same export the CLI and Automator use), and its
   function collection (the Java sources its UDFs and Custom Field Functions call) to a folder;
2. restores that YAML as a second pipeline, with the same agent config file,
   through ``main.py`` with ``START_SYNC=false`` so nothing is synced twice, publishing the
   function collection first (``FUNCTION_COLLECTION_PATH``);
3. exports the restored pipeline again;
4. checks that the restored pipeline is configured like the original
   (entities, groups, Chronos schedules, function collection) and that the two exports match.

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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import yaml

from commons import admin_credentials_path, fetch_core_hub, load_admin_password, login_password_candidates
from function_collection import fetch_function_collection
from export_template_from_corehub import (
    fetch_pipeline_entities,
    read_pipeline_jobs,
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

# Chronos job fields that identify the job row or record its runs.
JOB_IDENTITY_KEYS = frozenset({
    "id", "job_id", "pipeline_id", "cron_job_identifier", "command",
    "last_successful_run", "last_error_message", "last_run_error_time",
})

# Fields that reference a table by id. A target table that does not exist when
# a pipeline is created gets an id the bootstrapper makes up (a 32-bit hash of
# its name); once the table exists, a restore discovers it with CoreHub's own
# id. The id says nothing about the configuration, the table's name does.
TABLE_ID_REFERENCE_KEYS = {
    "tableId": None,
    "sourceTableObjectId": "SOURCE",
    "targetTableObjectId": "TARGET",
}

MAX_DIFF_LINES = 400
RESTORE_ERROR_LINES = 3
_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Normalisation (pure functions, unit tested offline)
# ---------------------------------------------------------------------------

def _agent_sides(agents: List[Dict[str, Any]]) -> Dict[str, str]:
    """agent id -> SOURCE / TARGET, for the agents that have an id."""
    sides: Dict[str, str] = {}
    for agent in agents:
        agent_id = agent.get("agentId") or agent.get("id")
        if agent_id:
            sides[str(agent_id)] = str(agent.get("agentType") or "?").upper()
    return sides


def _entity_ids(entity: Dict[str, Any], agent_sides: Dict[str, str]) -> Dict[str, str]:
    """Placeholders for an entity id and the ids of its agent entities."""
    name = entity.get("entityName")
    ids: Dict[str, str] = {}
    if entity.get("entityId"):
        ids[str(entity["entityId"])] = f"<entity:{name}>"
    for agent_entity in entity.get("agentEntities") or []:
        if isinstance(agent_entity, dict) and agent_entity.get("agentEntityId"):
            side = agent_sides.get(str(agent_entity.get("agentId")), str(agent_entity.get("type") or "?").upper())
            ids[str(agent_entity["agentEntityId"])] = f"<agentEntity:{name}:{side}>"
    return ids


def build_id_map(
    pipeline_id: str,
    agents: List[Dict[str, Any]],
    entities: List[Dict[str, Any]],
    groups: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Map every pipeline-specific id to a stable, name-based placeholder."""
    agent_sides = _agent_sides(agents)
    id_map: Dict[str, str] = {k: f"<agent:{side}>" for k, side in agent_sides.items()}
    for entity in entities:
        id_map.update(_entity_ids(entity, agent_sides))
    id_map.update({str(g["groupId"]): f"<group:{g.get('name')}>" for g in groups if g.get("groupId")})
    if pipeline_id:
        id_map[str(pipeline_id)] = "<pipeline>"
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


def _agent_entity_side(agent_entity: Dict[str, Any], agent_sides: Dict[str, str]) -> str:
    fallback = str(agent_entity.get("type") or "?").upper()
    return agent_sides.get(str(agent_entity.get("agentId")), fallback if fallback in ("SOURCE", "TARGET") else "?")


def _table_ref_name(ref: Dict[str, Any]) -> Optional[str]:
    """``schema.name`` of a table reference (``scope``/``collection`` for a NoSQL entity)."""
    schema = ref.get("schema") if ref.get("schema") is not None else ref.get("scope")
    name = ref.get("name") if ref.get("name") is not None else ref.get("collection")
    if name is None:
        return None
    return f"{schema}.{name}" if schema not in (None, "") else str(name)


def _is_table_ref(node: Any) -> bool:
    """A dict that names a table and carries its id: ``{id, schema, name}`` or ``{id, scope, collection}``."""
    return (
        isinstance(node, dict)
        and isinstance(node.get("id"), (int, str))
        and not isinstance(node.get("id"), bool)
        and _table_ref_name(node) is not None
        and (node.get("schema") is not None or node.get("scope") is not None)
    )


def _walk_table_refs(node: Any):
    if _is_table_ref(node):
        yield node
    if isinstance(node, dict):
        for child in node.values():
            yield from _walk_table_refs(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_table_refs(child)


def _replace_table_ids(node: Any, side: str, tables: Dict[str, Dict[str, str]]) -> None:
    """Replace, in place, every table id of an agent entity by its table's placeholder."""
    if isinstance(node, list):
        for child in node:
            _replace_table_ids(child, side, tables)
        return
    if not isinstance(node, dict):
        return
    if _is_table_ref(node):
        node["id"] = f"<table:{side}:{_table_ref_name(node)}>"
    for key, child in node.items():
        if key in TABLE_ID_REFERENCE_KEYS and isinstance(child, (int, str)) and not isinstance(child, bool):
            preferred = TABLE_ID_REFERENCE_KEYS[key] or side
            for lookup_side in [preferred] + [s for s in tables if s != preferred]:
                placeholder = tables.get(lookup_side, {}).get(str(child))
                if placeholder:
                    node[key] = placeholder
                    break
        else:
            _replace_table_ids(child, side, tables)


def normalize_table_ids(entity: Dict[str, Any], agent_sides: Dict[str, str]) -> Dict[str, Any]:
    """Return a copy of an entity whose table ids are replaced by name-based placeholders.

    Every table reference of an agent entity (its ``table``, a NoSQL ``entityObject``,
    the tables and headers of a MultiTable) gives ``<table:SIDE:schema.name>``, and the
    fields that point at a table by id (a column's ``tableId``, the mapping matrix's
    ``sourceTableObjectId`` / ``targetTableObjectId``, a partition or filter column) get
    the placeholder of the table that id belongs to on the matching side. This is done per
    entity and per side because CoreHub gives the source and the target table the same id
    when they have the same name. An id that matches no table is left as it is.
    """
    result = copy.deepcopy(entity)
    agent_entities = [(ae, _agent_entity_side(ae, agent_sides)) for ae in result.get("agentEntities") or []
                      if isinstance(ae, dict)]
    tables: Dict[str, Dict[str, str]] = {"SOURCE": {}, "TARGET": {}, "?": {}}
    for agent_entity, side in agent_entities:
        for ref in _walk_table_refs(agent_entity):
            tables[side].setdefault(str(ref["id"]), f"<table:{side}:{_table_ref_name(ref)}>")
    for agent_entity, side in agent_entities:
        _replace_table_ids(agent_entity, side, tables)
    return result


def drop_computed_partitions(entity: Dict[str, Any]) -> Dict[str, Any]:
    """Remove ``entityType.partitionSettings.partitions`` from every agent entity, in place.

    CoreHub computes those ranges when the snapshot runs, and an import writes an empty list.
    The partition column and the rest of ``partitionSettings`` stay compared, and so does a
    ``partitionSettings`` that is missing on one side.
    """
    for agent_entity in entity.get("agentEntities") or []:
        entity_type = agent_entity.get("entityType") if isinstance(agent_entity, dict) else None
        settings = entity_type.get("partitionSettings") if isinstance(entity_type, dict) else None
        if isinstance(settings, dict):
            settings.pop("partitions", None)
    return entity


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


def _normalize_entities(side: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    agent_sides = _agent_sides(side.get("agents") or [])
    return {
        name: drop_computed_partitions(normalize(normalize_table_ids(entity, agent_sides), side["idMap"]))
        for name, entity in key_by_name(side["entities"], "entityName").items()
    }


def check_entities(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    check = Check("restored-entities-match", "Every entity is restored with the same configuration")
    left = _normalize_entities(original)
    right = _normalize_entities(restored)
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


def check_function_collection(original: Dict[str, Any], restored: Dict[str, Any]) -> Check:
    """Compare the function collection sources of the two pipelines, file by file."""
    check = Check("restored-function-collection-matches", "The function collection is restored with the same sources")
    left = {name: code.splitlines() for name, code in (original.get("functionCollection") or {}).items()}
    right = {name: code.splitlines() for name, code in (restored.get("functionCollection") or {}).items()}
    return compare_keyed(check, left, right, "function collection sources")


def declares_schedules(template: Any) -> bool:
    """Whether a table-list template asks for any Chronos schedule."""
    if isinstance(template, dict):
        return any(
            (key in ("schedules", "group_schedules") and bool(value)) or declares_schedules(value)
            for key, value in template.items()
        )
    if isinstance(template, list):
        return any(declares_schedules(item) for item in template)
    return False


def job_settings(job: Dict[str, Any]) -> Dict[str, Any]:
    """A Chronos job without what Chronos derives: a cron job's start_time is its next run."""
    if job.get("is_cron_expression"):
        return {k: v for k, v in job.items() if k != "start_time"}
    return job


def check_schedules(original: Dict[str, Any], restored: Dict[str, Any], expects_schedules: bool = False) -> Check:
    """Compare Chronos jobs; with ``expects_schedules`` an unreadable or empty Chronos fails."""
    check = Check("restored-schedules-match", "Chronos schedules are restored for the new pipeline")
    if original["jobs"] is None or restored["jobs"] is None:
        if expects_schedules:
            check.fail("Chronos could not be read, so the schedules the kit declares cannot be checked")
        else:
            check.message = "Chronos could not be read; the kit declares no schedules"
        return check
    if expects_schedules and not original["jobs"]:
        check.fail("the kit declares schedules but the original pipeline has none in Chronos")
        return check
    drop = VOLATILE_KEYS.union(JOB_IDENTITY_KEYS)
    left = {k: normalize(job_settings(v), original["idMap"], drop) for k, v in key_by_name(original["jobs"], "name").items()}
    right = {k: normalize(job_settings(v), restored["idMap"], drop) for k, v in key_by_name(restored["jobs"], "name").items()}
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


def run_checks(original: Dict[str, Any], restored: Dict[str, Any], expects_schedules: bool = False) -> List[Check]:
    checks = [
        check_entities(original, restored),
        check_groups(original, restored),
        check_schedules(original, restored, expects_schedules),
        check_export_stable(original, restored),
    ]
    if original.get("functionCollection") or restored.get("functionCollection"):
        checks.append(check_function_collection(original, restored))
    return checks


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


def _sdk_token() -> Optional[str]:
    """A token from the Gluesync SDK handshake when USE_SDK is on.

    The SDK session stays open for the rest of this process: Chronos accepts an
    SDK token only while its session is alive (CoreHub keeps accepting it).
    """
    if os.getenv("USE_SDK", "false").strip().lower() not in ("true", "1", "t", "y", "yes"):
        return None
    try:
        from utils.gluesync_sdk_client import get_token, initialize_gluesync_sdk

        initialize_gluesync_sdk()
        return get_token()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Gluesync SDK token not available: {exc}")
        return None


def _login_token(password: str) -> Optional[str]:
    try:
        response = fetch_core_hub(
            "/authentication/login",
            method="POST",
            body={"username": "admin", "password": password},
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Admin login failed: {exc}")
        return None
    if not isinstance(response, dict) or response.get("changeRequired"):
        return None
    return response.get("apiToken")


def _password_token(token_file: str) -> Optional[str]:
    """An admin login with the saved password, else ADMIN_PASSWORD, else DEFAULT_PASSWORD (no password twice)."""
    passwords = login_password_candidates(
        load_admin_password(admin_credentials_path(token_file)),
        os.getenv("ADMIN_PASSWORD", ""),
        os.getenv("DEFAULT_PASSWORD", ""),
    )
    for password in passwords:
        token = _login_token(password)
        if token:
            return token
    return None


def _saved_token(token_file: str) -> Optional[str]:
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token = json.load(f).get("token")
        if token and isinstance(fetch_core_hub("/pipelines", token=token), list):
            return token
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Saved token in {token_file} not usable: {exc}")
    return None


def authenticate(token_file: str) -> str:
    """A token Chronos accepts too: an SDK token, else an admin login, else the saved token.

    The admin login tries the password the bootstrapper saved when it changed the default one, then
    ADMIN_PASSWORD, then DEFAULT_PASSWORD. The saved token comes last: once its run has ended Chronos may refuse it
    (an SDK token whose session is closed) although CoreHub still accepts it, and the schedules could
    then not be read. It also expires after minutes, which the saved password does not.
    """
    token = _sdk_token() or _password_token(token_file) or _saved_token(token_file)
    if not token:
        raise RuntimeError(
            "cannot authenticate to CoreHub: tried the SDK token, an admin login with the saved password "
            f"({admin_credentials_path(token_file)}), ADMIN_PASSWORD and DEFAULT_PASSWORD, and the saved token "
            f"({token_file}); none worked"
        )
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
    jobs = read_pipeline_jobs(pipeline_id, token=token)  # None: Chronos could not be read
    # The sources of the function collection by file name; none when the pipeline has no collection.
    collection = fetch_function_collection(token, pipeline_id, fetch=fetch_core_hub)
    function_collection_sources = {
        s["fileName"]: s["code"] for s in (collection or {}).get("sources") or []
        if isinstance(s, dict) and s.get("fileName") and isinstance(s.get("code"), str)
    }
    return {
        "pipelineId": pipeline_id,
        "pipeline": pipeline,
        "agents": agents,
        "entities": entities,
        "groups": groups,
        "jobs": jobs,
        "functionCollection": function_collection_sources,
        "idMap": build_id_map(pipeline_id, agents, entities, groups),
    }


def restore_error_lines(log_path: Path, limit: int = RESTORE_ERROR_LINES) -> str:
    """The last ERROR / FATAL / Traceback lines of a restore log, joined for one message."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    found = []
    for line in lines:
        plain = _ANSI_ESCAPES.sub("", line).strip()
        if re.search(r"\b(ERROR|FATAL|CRITICAL)\b|Traceback", plain):
            found.append(plain[:300])
    return " | ".join(found[-limit:])


def restore_export(
    export_path: Path,
    pipeline_name: str,
    out_dir: Path,
    timeout: int,
    function_collection_dir: Optional[Path] = None,
) -> Tuple[Optional[str], str]:
    """Run main.py on the export; return (restored pipeline id, error).

    ``function_collection_dir`` holds the sources of the function collection to publish on the restored pipeline
    before its entities are created (the YAML alone cannot recreate it).

    The restore counts as done only when main.py exits 0 and reports ``complete``.
    """
    result_file = out_dir / "restore-result.json"
    result_file.unlink(missing_ok=True)  # a result left by an earlier run says nothing about this one
    env = dict(os.environ)
    env.update({
        "TABLE_LIST_YAML": str(export_path),
        "START_SYNC": "false",
        "CREATE_TABLE_IF_NOT_EXISTS": "false",
        "BOOTSTRAPPER_RESULT_FILE": str(result_file),
    })
    if function_collection_dir is not None:
        env["FUNCTION_COLLECTION_PATH"] = str(function_collection_dir)
    else:
        env.pop("FUNCTION_COLLECTION_PATH", None)  # one left by the caller's environment belongs to another pipeline
    cmd = [sys.executable, str(SCRIPT_DIR / "main.py"), "--pipeline-name", pipeline_name]
    logger.info(f"Restoring {export_path} as pipeline '{pipeline_name}'")
    log_path = out_dir / "restore.log"
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, f"restore did not finish within {timeout}s"
    detail = restore_error_lines(log_path)
    suffix = f": {detail}" if detail else ""
    if not result_file.exists():
        return None, f"restore exited with code {proc.returncode} before creating a pipeline (see restore.log){suffix}"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    pipeline_id = result.get("pipelineId")
    if result.get("status") != "complete":
        return pipeline_id, f"restore of pipeline {pipeline_id} failed (see restore.log){suffix}"
    if proc.returncode != 0:
        return pipeline_id, f"restore of pipeline {pipeline_id} exited with code {proc.returncode} (see restore.log){suffix}"
    return pipeline_id, ""


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def kit_declares_schedules() -> bool:
    """Whether the template the pipeline was built from (TABLE_LIST_YAML) declares schedules."""
    path = os.getenv("TABLE_LIST_YAML", "")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return declares_schedules(yaml.safe_load(f))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"Cannot read the kit template {path!r} to look for schedules: {exc}")
        return False


class RoundTripError(Exception):
    """The pipeline could not be exported, restored or exported again."""


def capture(token: str, pipeline_id: str, out_dir: Path, label: str) -> Dict[str, Any]:
    """Snapshot and export one pipeline, keeping both as ``<label>-*`` files."""
    side = snapshot_pipeline(token, pipeline_id)
    side["export"] = render_pipeline_export(token, pipeline_id)
    (out_dir / f"{label}-export.yaml").write_text(side["export"], encoding="utf-8")
    write_json(out_dir / f"{label}-snapshot.json", {k: v for k, v in side.items() if k != "export"})
    for file_name, code in (side.get("functionCollection") or {}).items():
        source = out_dir / f"{label}-function-collection" / file_name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(code, encoding="utf-8")
    return side


def export_and_restore(
    pipeline_id: str, out_dir: Path, token_file: str, timeout: int,
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    """Return the original side, the restored side and what happened."""
    try:
        token = authenticate(token_file)
        original = capture(token, pipeline_id, out_dir, "original")
    except Exception as exc:  # pylint: disable=broad-except
        raise RoundTripError(f"export of pipeline {pipeline_id} failed: {exc}") from exc

    pipeline = original["pipeline"] if isinstance(original["pipeline"], dict) else {}
    restore_name = f"{pipeline.get('name') or pipeline_id}{ROUND_TRIP_SUFFIX}"
    collection_dir = out_dir / "original-function-collection" if original.get("functionCollection") else None
    restored_id, error = restore_export(out_dir / "original-export.yaml", restore_name, out_dir, timeout,
                                        function_collection_dir=collection_dir)
    if error:
        raise RoundTripError(error)

    try:
        restored = capture(token, restored_id, out_dir, "restored")
    except Exception as exc:  # pylint: disable=broad-except
        raise RoundTripError(f"export of restored pipeline {restored_id} failed: {exc}") from exc
    return original, restored, f"pipeline {pipeline_id} exported and restored as {restored_id}"


def write_results(out_dir: Path, pipeline_id: str, suite_name: str, checks: List[Check], duration: float) -> int:
    """Write the diffs, report.json and junit.xml; return the exit code."""
    passed = all(c.passed for c in checks)
    for c in checks:
        if c.diff:
            (out_dir / f"{c.name}.diff").write_text("\n".join(c.diff) + "\n", encoding="utf-8")
        logger.info(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.message}")
    write_json(out_dir / "report.json", {
        "pipelineId": pipeline_id,
        "passed": passed,
        "durationSeconds": round(duration, 1),
        "checks": [c.to_dict() for c in checks],
    })
    (out_dir / "junit.xml").write_text(junit_xml(suite_name, checks, duration), encoding="utf-8")
    return 0 if passed else 1


def run(pipeline_id: str, out_dir: Path, token_file: str, timeout: int, suite_name: str) -> int:
    started = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)
    step = Check("export-and-restore", "The pipeline exports and the export restores as a new pipeline")
    checks = [step]
    try:
        original, restored, step.message = export_and_restore(pipeline_id, out_dir, token_file, timeout)
        checks.extend(run_checks(original, restored, kit_declares_schedules()))
    except RoundTripError as exc:
        step.fail(str(exc))
    return write_results(out_dir, pipeline_id, suite_name, checks, time.time() - started)


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

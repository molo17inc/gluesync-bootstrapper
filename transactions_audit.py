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

"""Back up and restore the transactions audit configuration of a pipeline.

Core Hub (Gluesync 2.3.4.0 and later) keeps one audit configuration per pipeline: the store (an existing target agent
that holds the ledger), the level, the failure policy, the scope, the schema and the table layout. The backup carries
that configuration in the pipeline YAML, as a ``transactionsAudit`` section. Nothing else is backed up: not the ledger
rows (they live in the store), not the gaps, sessions or activity of the audit.

Ids change on a restore, so the section names things instead of pointing at them: the store is "the target agent of
this pipeline" or "the target agent of the pipeline named X (id Y in the backup)", and a scope of selected entities
lists entity names. A restore resolves them on the target Core Hub once every pipeline of the backup exists, and sets
the configuration (``PUT /pipelines/{id}/transactions-audit``). It never starts a recording: a start creates tables in
the store and needs the statements confirmed, which only a person (or a Chronos task) does. A section that cannot be
restored (store pipeline missing, entity not found, Core Hub refuses) is reported, never raised: the audit is "needs
attention" and the rest of the restore goes on.

Where the section sits in a pipeline YAML (the same places as the pipeline schedules):

* one schema: inside that schema, as the key already did in the Automator template;
* several schemas (``schemas:`` layout): at the root.

The older shape of that key (``enabled``, ``targetAgentId``, ``deepTrace``) is still read: it becomes a store on the
named target agent of the pipeline, at Trace, and never starts anything.

EXTENSION POINT (retention purge): the retention purge of the audit ledger does not exist yet. When it does, its
settings are one more field of the exported section: add it in :func:`export_section` and apply it in
:func:`restore_section`, with its test. Nothing here handles it.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from commons import fetch_core_hub

logger = logging.getLogger(__name__)

SECTION_KEY = "transactionsAudit"
# Written instead of the section when the audit configuration could not be read: a restore reports it, so a failed
# export cannot pass for "this pipeline has no audit".
ERROR_KEY = "transactionsAuditError"

STATUS_RESTORED = "restored"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_FAILED = "failed"

Fetch = Callable[..., Any]


def audit_path(pipeline_id: Any) -> str:
    return f"/pipelines/{pipeline_id}/transactions-audit"


def _http_status(exc: BaseException) -> Optional[int]:
    response = getattr(exc.__cause__, "response", None)
    return getattr(response, "status_code", None)


def _message(exc: BaseException) -> str:
    response = getattr(exc.__cause__, "response", None)
    try:
        payload = response.json() if response is not None else None
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return str(exc)


def _is_status(answer: Any) -> bool:
    """A Core Hub with the audit answers a dict with a ``state``; an older one 404s or serves the UI page."""
    return isinstance(answer, dict) and "state" in answer


def read_status(token: str, pipeline_id: Any, fetch: Fetch = None) -> Optional[Dict[str, Any]]:
    """``GET /pipelines/{id}/transactions-audit``; ``None`` when this Core Hub has no such API (older than 2.3.4.0)."""
    fetch = fetch or fetch_core_hub
    try:
        answer = fetch(audit_path(pipeline_id), token=token)
    except RuntimeError as exc:
        if _http_status(exc) in (404, 405):
            return None
        raise
    return answer if _is_status(answer) else None


# --- export ------------------------------------------------------------------------------------------------------

def _pipeline_info(token: str, pipeline_id: Any, fetch: Fetch) -> Dict[str, Any]:
    info = fetch(f"/pipelines/{pipeline_id}", token=token)
    return info if isinstance(info, dict) else {}


def _target_agents(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [a for a in (info.get("targetAgentsInfo") or []) if isinstance(a, dict)]


def _agent_tag(info: Dict[str, Any], agent_id: Any) -> Optional[str]:
    for agent in _target_agents(info):
        if agent.get("agentId") == agent_id:
            return agent.get("agentTag")
    return None


def _portable_store(token: str, pipeline_id: Any, store: Dict[str, Any], fetch: Fetch) -> Dict[str, Any]:
    store_pipeline = store.get("pipelineId") or pipeline_id
    own = str(store_pipeline) == str(pipeline_id)
    info = _pipeline_info(token, store_pipeline, fetch)
    portable: Dict[str, Any] = {"own": True} if own else {"pipelineId": store_pipeline, "pipelineName": info.get("name")}
    tag = _agent_tag(info, store.get("agentId"))
    if tag:
        portable["agentTag"] = tag
    if store.get("agentId"):
        portable["agentId"] = store["agentId"]  # informational: ids differ on another Core Hub
    return portable


def _portable_scope(scope: Dict[str, Any], entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    mode = scope.get("mode") or "ALL"
    if mode != "SELECTED":
        return {"mode": mode}
    names = {e.get("entityId"): e.get("entityName") for e in entities}
    return {"mode": mode, "entities": [names.get(i) or i for i in scope.get("entityIds") or []]}


def export_section(token: str, pipeline_id: Any, fetch: Fetch = None) -> Optional[Dict[str, Any]]:
    """The portable ``transactionsAudit`` section of a pipeline; ``None`` when it has no audit configuration
    (or this Core Hub has no audit API). Raises when Core Hub fails to answer: the caller records the error."""
    fetch = fetch or fetch_core_hub
    status = read_status(token, pipeline_id, fetch)
    config = (status or {}).get("config")
    if not isinstance(config, dict):
        return None
    section: Dict[str, Any] = {
        "state": status.get("state"),
        "store": _portable_store(token, pipeline_id, config.get("store") or {}, fetch),
        "level": config.get("level"),
        "onStoreFailure": config.get("onStoreFailure"),
        "scope": _portable_scope(config.get("scope") or {}, status.get("entities") or []),
        "schema": config.get("schema"),
        "layout": config.get("layout"),
    }
    if config.get("tableName") is not None:
        section["tableName"] = config["tableName"]
    return {k: v for k, v in section.items() if v is not None}


def export_into(yaml_data: Dict[str, Any], token: str, pipeline_id: Any, fetch: Fetch = None) -> Dict[str, Any]:
    """Add the audit section (or the error that stopped it) to a pipeline YAML structure, in place; returns it."""
    try:
        section = export_section(token, pipeline_id, fetch)
        key, value = SECTION_KEY, section
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("The transactions audit of pipeline %s is NOT in the backup: %s", pipeline_id, exc)
        key, value = ERROR_KEY, str(exc)
    if value is not None:
        _place(yaml_data, key, value)
    return yaml_data


def _place(yaml_data: Dict[str, Any], key: str, value: Any) -> None:
    schemas = yaml_data.get("schemas") if isinstance(yaml_data.get("schemas"), dict) else None
    if schemas is not None:
        yaml_data[key] = value
        return
    sole = [v for k, v in yaml_data.items() if isinstance(v, dict) and k not in ("groups", "exportMetadata")]
    if len(sole) == 1:
        sole[0][key] = value
    else:
        logger.warning("The pipeline has no schema to carry %s; it is left out of the backup", key)


# --- reading a YAML -----------------------------------------------------------------------------------------------

def find_section(yaml_config: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(section, error)`` of a pipeline YAML: the root, or the first schema that has the key."""
    if not isinstance(yaml_config, dict):
        return None, None
    containers = [yaml_config]
    schemas = yaml_config.get("schemas")
    containers.extend(v for v in (schemas.values() if isinstance(schemas, dict) else yaml_config.values())
                      if isinstance(v, dict))
    section: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    for container in containers:
        if section is None and isinstance(container.get(SECTION_KEY), dict):
            section = container[SECTION_KEY]
        if error is None and container.get(ERROR_KEY):
            error = str(container[ERROR_KEY])
    return section, error


# --- restore -----------------------------------------------------------------------------------------------------

def _result(status: str, pipeline_id: Any, warnings: Optional[List[str]] = None, **extra: Any) -> Dict[str, Any]:
    return {"status": status, "pipelineId": pipeline_id, "warnings": warnings or [], **extra}


def _attention(pipeline_id: Any, message: str, **extra: Any) -> Dict[str, Any]:
    return _result(STATUS_NEEDS_ATTENTION, pipeline_id, [message], **extra)


def _store_pipeline_id(store: Dict[str, Any], pipeline_id: Any, id_map: Dict[str, str],
                       token: str, fetch: Fetch) -> Optional[str]:
    """The pipeline of the store on this Core Hub: the pipeline itself, its new id from this restore, or the
    pipeline of that name that already exists."""
    if store.get("own") or not store.get("pipelineId"):
        return str(pipeline_id)
    mapped = id_map.get(str(store["pipelineId"]))
    if mapped:
        return str(mapped)
    wanted = store.get("pipelineName")
    if wanted:
        for item in fetch("/pipelines", token=token) or []:
            if isinstance(item, dict) and item.get("name") == wanted:
                return str(item.get("id") or item.get("pipelineId"))
    return None


def _store_agent_id(info: Dict[str, Any], store: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(agent id, warning)``: a pipeline has one target agent; a different tag than the backup's is a warning."""
    agents = _target_agents(info)
    wanted = store.get("agentTag")
    matching = [a for a in agents if wanted and a.get("agentTag") == wanted]
    if matching:
        return matching[0].get("agentId"), None
    if len(agents) == 1:
        found = agents[0]
        note = None
        if wanted and found.get("agentTag") != wanted:
            note = f"the store agent is now {found.get('agentTag')!r}, the backup had {wanted!r}"
        return found.get("agentId"), note
    return None, None


def _scope_ids(scope: Dict[str, Any], status: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    mode = scope.get("mode") or "ALL"
    if mode != "SELECTED":
        return {"mode": mode, "entityIds": []}, []
    by_name = {e.get("entityName"): e.get("entityId") for e in (status or {}).get("entities") or []}
    ids, missing = [], []
    for name in scope.get("entities") or []:
        (ids if by_name.get(name) else missing).append(by_name.get(name) or name)
    return {"mode": mode, "entityIds": ids}, missing


def legacy_to_section(legacy: Dict[str, Any], target_agents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The section the older ``enabled`` / ``targetAgentId`` / ``deepTrace`` shape stands for, ``None`` when it asks
    for nothing (disabled) or names no agent that can be found (a lone target agent is the fallback)."""
    if legacy.get("enabled") is False:
        return None
    wanted = legacy.get("targetAgentId")
    chosen = next((a for a in target_agents if wanted and wanted in (a.get("agentId"), a.get("agentTag"))), None)
    if chosen is None and len(target_agents) == 1:
        chosen = target_agents[0]
    if chosen is None:
        return None
    return {"store": {"own": True, "agentTag": chosen.get("agentTag"), "agentId": chosen.get("agentId")},
            "level": "TRACE"}


def _config_body(section: Dict[str, Any], store_pipeline: str, agent_id: str, scope: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"store": {"pipelineId": store_pipeline, "agentId": agent_id}, "scope": scope}
    for key in ("level", "schema", "layout", "tableName", "onStoreFailure"):
        if section.get(key) is not None:
            body[key] = section[key]
    return body


def restore_section(token: str, pipeline_id: Any, section: Dict[str, Any],
                    id_map: Optional[Dict[str, str]] = None, fetch: Fetch = None) -> Dict[str, Any]:
    """Set the audit configuration of the restored pipeline ``pipeline_id`` from a backup section.

    ``id_map`` is {pipeline id in the backup: pipeline id on this Core Hub} of the restore in progress. Never raises
    and never starts a recording: ``status`` is restored, skipped, needs_attention or failed, ``warnings`` say what
    to do about it."""
    fetch = fetch or fetch_core_hub
    id_map = id_map or {}
    try:
        return _restore(token, pipeline_id, section, id_map, fetch)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("The transactions audit of pipeline %s was not restored: %s", pipeline_id, exc)
        text = _message(exc) if isinstance(exc, RuntimeError) else str(exc)
        return _result(STATUS_FAILED, pipeline_id, [f"transactions audit not restored: {text}"])


def _restore(token: str, pipeline_id: Any, section: Dict[str, Any], id_map: Dict[str, str], fetch: Fetch) -> Dict[str, Any]:
    status = read_status(token, pipeline_id, fetch)
    if status is None:
        return _result(STATUS_SKIPPED, pipeline_id,
                       ["this Core Hub has no transactions audit (it needs 2.3.4.0 or later): its configuration was not restored"])
    if "store" not in section:
        legacy = section
        section = legacy_to_section(legacy, _target_agents(_pipeline_info(token, pipeline_id, fetch)))
        if section is None:
            if legacy.get("enabled") is False:
                return _result(STATUS_SKIPPED, pipeline_id)
            return _attention(pipeline_id, "the audit of the backup names no target agent that this pipeline has: configure it again")
    store = section["store"] if isinstance(section["store"], dict) else {}
    store_pipeline = _store_pipeline_id(store, pipeline_id, id_map, token, fetch)
    if store_pipeline is None:
        return _attention(pipeline_id, f"the pipeline that holds the ledger ({store.get('pipelineName') or store.get('pipelineId')}) "
                                       "is not on this Core Hub: configure the audit again")
    agent_id, note = _store_agent_id(_pipeline_info(token, store_pipeline, fetch), store)
    if not agent_id:
        return _attention(pipeline_id, f"the store agent {store.get('agentTag')!r} was not found on the pipeline that holds the ledger: configure the audit again")
    scope, missing = _scope_ids(section.get("scope") or {}, status)
    if missing:
        return _attention(pipeline_id, "entities of the audit scope were not found after the restore: " + ", ".join(map(str, missing)))
    body = _config_body(section, store_pipeline, agent_id, scope)
    fetch(audit_path(pipeline_id), method="PUT", token=token, body=body)
    warnings = [f"transactions audit: {note}"] if note else []
    if section.get("state") in ("RECORDING", "PAUSED", "ON_HOLD"):
        warnings.append(f"the transactions audit was {section['state']} when backed up: it is configured but not started; "
                        "start it again (the ledger tables are appended to)")
    return _result(STATUS_RESTORED, pipeline_id, warnings, wasState=section.get("state"))


def restore_from_yaml(token: str, pipeline_id: Any, yaml_config: Any,
                      id_map: Optional[Dict[str, str]] = None, fetch: Fetch = None) -> Optional[Dict[str, Any]]:
    """Restore the audit a pipeline YAML carries; ``None`` when it carries none (and no error)."""
    section, error = find_section(yaml_config)
    if section is None and error is None:
        return None
    if section is None:
        return _attention(pipeline_id, f"the backup could not read the transactions audit of this pipeline: {error}")
    return restore_section(token, pipeline_id, section, id_map, fetch)

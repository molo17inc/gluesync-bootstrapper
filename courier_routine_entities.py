#!/usr/bin/env python3
# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#
# 2. MOLO17 Commercial License
#    Contact MOLO17 at <info@molo17.com> for licensing terms and conditions.
#
# Copyright (C) 2026 MOLO17. All rights reserved.
"""The entities of Courier EXECUTE routes, whose target is the stored procedure or function they call.

An EXECUTE route (Core Hub 2.3.2.2) declares what the request carries and what it answers; the ONE entity of the HTTP
Source's pipeline built on it picks the routine and maps it: each IN or INOUT parameter takes the value of the binding
mapped onto it (none: NULL), and one output of the routine is mapped onto the route's ``_response`` column, returned as
it is. The general table discovery does not list routines, so these entities are not built from the tables list: the
``call`` of the route in the HTTP Source's ``courier`` block names the routine on the pipeline's target agent::

    {"name": "place_order", "httpVerb": "POST", "path": "/place-order",
     "bindings": [{"name": "customer"}, {"name": "amount", "type": "NUMBER"}, {"name": "note", "required": false}],
     "routine": {"answer": {"body": "ALL", "format": "JSON"}},
     "call": {
       "schema": "shop",                      # required: the routine's schema (on MySQL and MariaDB, the database)
       "name": "place_order",                 # required
       "kind": "PROCEDURE",                   # PROCEDURE or FUNCTION; read from the catalog when left out
       "parameters": {"p_note": null},        # parameter -> binding (null: NULL); by default the binding of its name,
                                              # with or without a `p_` prefix, ignoring case
       "response": "_result",                 # the output mapped onto `_response`; by default the one the route's
                                              # return type names (see default_output)
       "entityName": "Place order"            # defaults to the route's name
     }}

The target is read from ``GET /api-agent/inbound/routines/target`` (its table of type ROUTINE, the parameters and the
pieces of the answer as columns, the definition in the custom property ``courierRoutine``), the source from the
discovery of the HTTP Source (the route's table: its bindings and ``_response``), and the entity is saved with
``PUT /pipelines/{id}/config/entities`` once the tables list's entities exist, before the pipeline starts. Core Hub
checks the call when it saves the entity: a binding onto an OUT parameter, an output that does not give the route's
return type, a field function or a mapping function are refused.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from courier_support import CourierConfigError

ROUTINES_PATH = "/api-agent/inbound/routines"
ROUTINE_TARGET_PATH = "/api-agent/inbound/routines/target"
ENTITIES_CONFIG_PATH = "/pipelines/{pipeline_id}/config/entities"
DISCOVERY_COLUMNS_PATH = "/pipelines/{pipeline_id}/agents/{agent_id}/discovery/columns"

RESPONSE_COLUMN = "_response"
RESULT_COLUMN = "_result"
ROWS_COLUMN = "_rows"
VALUE_COLUMN = "_value"
RETURN_COLUMN = "_return"
PIECE_COLUMNS = (RESULT_COLUMN, ROWS_COLUMN, VALUE_COLUMN, RETURN_COLUMN)
ROUTINE_KINDS = ("PROCEDURE", "FUNCTION")
DEFINITION_PROPERTY = "courierRoutine"
DEFAULT_GROUP = "_default"
VALUE_MODES = ("IN", "INOUT")
RETURN_MODES = ("OUT", "INOUT")

FetchFn = Callable[..., Any]

_logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _columns_of(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("columns")
    return [c for c in payload if isinstance(c, dict)] if isinstance(payload, list) else []


def binding_matches(binding: str, parameter: str) -> bool:
    """A binding feeds the parameter of its name, ignoring case and a ``p_`` prefix (as Core Hub's MCP draft does)."""
    stripped = parameter[2:] if parameter[:2].lower() == "p_" else parameter
    return binding.lower() in (parameter.lower(), stripped.lower())


def default_output(body: str, outputs: List[str]) -> str:
    """The output that gives the route's return type ``body``: the whole result, the rows, the first value, or, for one
    output, a function's return, else the routine's only OUT parameter."""
    by_body = {"ALL": RESULT_COLUMN, "ROWS": ROWS_COLUMN, "VALUE": VALUE_COLUMN}
    if body in by_body:
        return by_body[body]
    if RETURN_COLUMN in outputs:
        return RETURN_COLUMN
    named = [o for o in outputs if o not in PIECE_COLUMNS]
    if len(named) == 1:
        return named[0]
    raise CourierConfigError(f"the route answers one output: name it in 'call.response', one of {', '.join(outputs)}")


def _definition(target: Dict[str, Any]) -> Dict[str, Any]:
    raw = (target.get("customProperties") or {}).get(DEFINITION_PROPERTY)
    try:
        definition = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as error:
        raise CourierConfigError(f"Core Hub answered an unreadable routine definition: {error}") from error
    if not isinstance(definition, dict):
        raise CourierConfigError("Core Hub answered no routine definition for the target")
    return definition


def _mode(parameter: Dict[str, Any]) -> str:
    return str(parameter.get("mode") or "IN").upper()


def _explicit_errors(route: Dict[str, Any], routine: str, explicit: Dict[str, Any], parameters: List[Dict[str, Any]],
                     binding_names: List[str]) -> List[str]:
    """What is wrong with ``call.parameters``: a parameter the routine lacks, a binding the route lacks."""
    errors: List[str] = []
    for parameter, binding in explicit.items():
        if not any(p.get("name") == parameter for p in parameters):
            errors.append(f"routine '{routine}' has no parameter '{parameter}'")
        if binding is not None and binding not in binding_names:
            errors.append(f"route '{route.get('name')}' has no binding '{binding}'")
    return errors


def _inputs(parameters: List[Dict[str, Any]], explicit: Dict[str, Any], binding_names: List[str]) -> Dict[str, Optional[str]]:
    """The binding each IN or INOUT parameter takes: the one ``call.parameters`` names, else the one of its name."""
    inputs: Dict[str, Optional[str]] = {}
    for parameter in parameters:
        if _mode(parameter) not in VALUE_MODES:
            continue
        name = parameter.get("name")
        inputs[name] = explicit[name] if name in explicit else next((b for b in binding_names if binding_matches(b, name)), None)
    return inputs


def _outputs(target: Dict[str, Any], parameters: List[Dict[str, Any]]) -> List[str]:
    """The target's columns that are pieces of the answer: `_result`, `_rows`, `_value`, `_return`, each OUT or INOUT."""
    returned = {p.get("name") for p in parameters if _mode(p) in RETURN_MODES}
    return [c.get("name") for c in _columns_of(target) if c.get("name") in PIECE_COLUMNS or c.get("name") in returned]


def plan_call(route: Dict[str, Any], call_cfg: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    """``{"inputs": {parameter: binding or None}, "response": output, "warnings": [...]}``: which binding each IN or INOUT
    parameter takes and which output answers. Pure."""
    parameters = [p for p in _definition(target).get("parameters") or [] if isinstance(p, dict)]
    binding_names = [_text(b.get("name") if isinstance(b, dict) else b) for b in route.get("bindings") or []]
    routine = f"{call_cfg.get('schema')}.{call_cfg.get('name')}"
    explicit = call_cfg.get("parameters") if isinstance(call_cfg.get("parameters"), dict) else {}
    errors = _explicit_errors(route, routine, explicit, parameters, binding_names)
    inputs = _inputs(parameters, explicit, binding_names)
    outputs = _outputs(target, parameters)
    answer = (route.get("routine") or {}).get("answer") or {}
    response = _text(call_cfg.get("response")) or default_output(_text(answer.get("body")).upper() or "ALL", outputs)
    if response not in outputs:
        errors.append(f"'{response}' is no output of routine '{routine}': choose one of {', '.join(outputs)}")
    if errors:
        raise CourierConfigError(f"Invalid 'call' of route '{route.get('name')}':\n  - " + "\n  - ".join(errors))
    warnings = [f"parameter '{p}' of '{routine}' takes no binding: it is called with NULL" for p, b in inputs.items() if b is None]
    return {"inputs": inputs, "response": response, "warnings": warnings}


def build_entity(route: Dict[str, Any], call_cfg: Dict[str, Any], collection: str, source_agent_id: str,
                 source_columns: List[Dict[str, Any]], target_agent_id: str, target: Dict[str, Any],
                 entity_id: Optional[str] = None) -> Dict[str, Any]:
    """The entity payload (``PUT /pipelines/{id}/config/entities``) of an EXECUTE route: the route's table in the HTTP
    Source, the routine as the target, the call as its ``columnsMappingMatrix``. Pure."""
    plan = plan_call(route, call_cfg, target)
    if not source_columns:
        raise CourierConfigError(f"the HTTP Source does not list route '{route.get('name')}' of collection '{collection}' yet")
    source_table_id = source_columns[0].get("tableId")
    table = target.get("table") or {}
    target_columns = _columns_of(target)
    target_ids = {c.get("name"): c.get("id") for c in target_columns}
    matrix = []
    for column in source_columns:
        name = column.get("name")
        mapped = plan["response"] if name == RESPONSE_COLUMN else next((p for p, b in plan["inputs"].items() if b == name), None)
        matrix.append({
            "sourceTableObjectId": source_table_id,
            "targetTableObjectId": table.get("id"),
            "sourceColumnIds": [column.get("id")],
            "targetColumnIds": [target_ids[mapped]] if mapped in target_ids else [],
        })
    name = _text(call_cfg.get("entityName")) or _text(route.get("name"))
    entity_id = entity_id or str(uuid.uuid4())
    route_name = _text(route.get("name"))
    source = {
        "type": "NoSqlEntity",
        "entityId": entity_id,
        "entityName": name,
        "agentEntityId": "",
        "entityType": {"type": "Source"},
        "agentId": source_agent_id,
        "entityObject": {"id": source_table_id, "scope": collection, "collection": route_name},
        "columns": source_columns,
        "customProperties": {},
        "tablesProperties": {f"{collection}.{route_name}": {}},
    }
    target_entity = {
        "type": "SingleTable",
        "entityId": entity_id,
        "entityName": name,
        "agentEntityId": "",
        "entityType": {"type": "Target", "columnsMappingMatrix": matrix},
        "agentId": target_agent_id,
        "table": table,
        "columns": target_columns,
        "customProperties": {DEFINITION_PROPERTY: (target.get("customProperties") or {}).get(DEFINITION_PROPERTY)},
        "tablesProperties": {f"{table.get('schema')}.{table.get('name')}": {}},
    }
    return {"entityId": entity_id, "entityName": name, "agentEntities": [source, target_entity], "groupId": DEFAULT_GROUP,
            "warnings": plan["warnings"]}


def _kind(call: FetchFn, token: str, pipeline_id: str, target_agent_id: str, call_cfg: Dict[str, Any]) -> str:
    kind = _text(call_cfg.get("kind")).upper()
    if kind:
        if kind not in ROUTINE_KINDS:
            raise CourierConfigError(f"call.kind {kind!r} is not one of {list(ROUTINE_KINDS)}")
        return kind
    listed = call(ROUTINES_PATH, method="GET", token=token,
                  params={"pipelineId": pipeline_id, "agentId": target_agent_id, "schema": call_cfg.get("schema")})
    routines = listed.get("routines", listed) if isinstance(listed, dict) else listed
    found = [r for r in routines or [] if isinstance(r, dict) and r.get("name") == call_cfg.get("name")]
    if len(found) != 1:
        raise CourierConfigError(
            f"routine '{call_cfg.get('schema')}.{call_cfg.get('name')}' is {'not' if not found else 'more than once'} in the "
            "catalog of the target agent: name its 'kind' (PROCEDURE or FUNCTION)"
        )
    return _text(found[0].get("kind")).upper() or "PROCEDURE"


def _existing_entity_ids(call: FetchFn, token: str, pipeline_id: str) -> Dict[str, str]:
    stored = call(f"/pipelines/{pipeline_id}/entities", method="GET", token=token)
    ids: Dict[str, str] = {}
    for item in stored if isinstance(stored, list) else []:
        entity = item.get("entity", item) if isinstance(item, dict) else {}
        if entity.get("entityName") and entity.get("entityId"):
            ids[entity["entityName"]] = entity["entityId"]
    return ids


def create_routine_entities(token: str, pipeline_id: str, agents: List[Dict[str, Any]], fetch: FetchFn,
                            logger: Optional[logging.Logger] = None) -> List[str]:
    """Saves the entity of every EXECUTE route with a ``call`` in the HTTP Source entries of ``agents`` (each with its
    ``agentId``), onto the pipeline's target agent; returns the entity names. Nothing to do without such a route."""
    from courier_inbound_support import routine_calls  # the routes' shorthand is read as provisioning normalised it

    log = logger or _logger
    calls = [c for agent in agents for c in ({**c, "sourceAgentId": agent.get("agentId")} for c in routine_calls(agent))]
    if not calls:
        return []
    targets = [a for a in agents if str(a.get("agentType") or "").upper() == "TARGET"]
    if len(targets) != 1:
        raise CourierConfigError("an EXECUTE route's entity calls its routine on the pipeline's target agent: "
                                 f"the pipeline has {len(targets)} target agents")
    target_agent_id = targets[0]["agentId"]
    existing = _existing_entity_ids(fetch, token, pipeline_id)
    entities = []
    for item in calls:
        route, call_cfg = item["route"], item["call"]
        if not _text(call_cfg.get("schema")) or not _text(call_cfg.get("name")):
            raise CourierConfigError(f"the 'call' of route '{route.get('name')}' needs the routine's 'schema' and 'name'")
        kind = _kind(fetch, token, pipeline_id, target_agent_id, call_cfg)
        target = fetch(ROUTINE_TARGET_PATH, method="GET", token=token, params={
            "pipelineId": pipeline_id, "agentId": target_agent_id, "schema": call_cfg["schema"],
            "name": call_cfg["name"], "kind": kind,
        })
        columns = _columns_of(fetch(DISCOVERY_COLUMNS_PATH.format(pipeline_id=pipeline_id, agent_id=item["sourceAgentId"]),
                                    method="GET", token=token,
                                    params={"tableschema": item["collection"], "tablename": route.get("name")}))
        name = _text(call_cfg.get("entityName")) or _text(route.get("name"))
        entity = build_entity(route, call_cfg, item["collection"], item["sourceAgentId"], columns, target_agent_id,
                              target, entity_id=existing.get(name))
        for warning in entity.pop("warnings"):
            log.warning("Route '%s': %s", route.get("name"), warning)
        log.info("Entity '%s': route %s %s calls %s %s.%s", entity["entityName"], route.get("httpVerb"), route.get("path"),
                 kind.lower(), call_cfg["schema"], call_cfg["name"])
        entities.append(entity)
    fetch(ENTITIES_CONFIG_PATH.format(pipeline_id=pipeline_id), method="PUT", token=token, body={"entities": entities})
    return [e["entityName"] for e in entities]

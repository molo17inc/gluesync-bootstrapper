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
"""Support for the HTTP Source agent (``api-agent-shadow`` as SOURCE, the Courier inbound side).

The HTTP Source serves Courier *inbound* write endpoints on its own listener (``inboundPort``,
8090 by default): every CREATE / UPDATE / DELETE request is bound into one pipeline row, queued
on disk, answered ``202`` and delivered by the pipeline to its target. CoreHub exposes the
endpoints to the pipeline through discovery as schema = inbound collection, table = endpoint,
column = binding, so the usual entity creation of the bootstrapper applies once they exist.

An inbound *pipeline endpoint* names the pipeline and the HTTP Source agent that serve it, so
it can only be created once the agent has been added to the pipeline. This module creates the
collections and endpoints of the ``courier`` block of a SOURCE agent entry, and optionally the
Personal API Tokens clients call them with::

    {
      "agentType": "SOURCE",
      "agentTag": "api-agent-shadow",
      "hostCredentials": {"connectionName": "HTTP Source"},
      "customHostCredentials": {"inboundPort": 8090},
      "courier": {
        "collections": [
          {
            "name": "shop",
            "basePath": "shop",
            "allowlist": true,
            "endpoints": [
              {
                "name": "orders",
                "httpVerb": "POST",
                "path": "/orders/{id}",
                "operationBinding": "op",
                "bindings": [
                  {"name": "id", "location": "PATH", "type": "NUMBER"},
                  {"name": "op", "location": "QUERY", "required": false, "defaultValue": "Insert"},
                  {"name": "status", "location": "BODY"}
                ]
              }
            ]
          }
        ],
        "tokens": [
          {"name": "shop-client", "outputFile": "/secrets/http-source-token.json"}
        ]
      }
    }

Collections and endpoints are matched by name and updated in place when they already exist.
A token is created on every run (CoreHub shows its plaintext only once), after revoking the
active tokens with the same name, and its plaintext is written to ``outputFile`` only.

The payloads mirror ``InboundEndpointCollection`` / ``InboundEndpoint`` of
``gluesync-kotlin/api-agent`` and the checks of ``InboundEndpointValidator`` for pipeline
endpoints, so that a wrong configuration fails here with a readable message.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from courier_support import (
    BINDING_LOCATIONS,
    BINDING_TYPES,
    CourierConfigError,
    NAME_PATTERN,
    apply_courier_defaults,
    is_courier_agent,
    normalize_binding,
    provision_courier,
    stable_id,
)

INBOUND_COLLECTIONS_PATH = "/api-agent/inbound/collections"
INBOUND_ENDPOINTS_PATH = "/api-agent/inbound/endpoints"
API_TOKENS_PATH = "/api-tokens"
INBOUND_COLLECTION_KEY = "inboundCollection"
ENDPOINT_INBOUND_PERMISSION = "gluesync.endpoint_inbound"

# Pipeline endpoints: the operation each verb fits (InboundEndpointValidator.VERBS_BY_OPERATION).
# PATCH is refused on a pipeline endpoint: a row carries every binding and replaces the target row.
OPERATION_BY_VERB = {"POST": "CREATE", "PUT": "UPDATE", "DELETE": "DELETE"}
VERBS_WITH_BODY = ("POST", "PUT")
INBOUND_BODY_TYPES = ("NONE", "JSON", "FORM_URLENCODED")
BODY_TYPE_ALIASES = {"FORM": "FORM_URLENCODED"}
ROW_OPERATIONS = ("Insert", "Update", "Delete")
SEQUENCE_COLUMN = "_seq"
CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
BASE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$")
NUMBER_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

FetchFn = Callable[..., Any]

_logger = logging.getLogger(__name__)


def is_http_source_agent(agent: Optional[Dict[str, Any]]) -> bool:
    """True when the configured agent is the HTTP Source (``api-agent-shadow`` as SOURCE)."""
    return is_courier_agent(agent) and str(agent.get("agentType") or "").strip().upper() == "SOURCE"


# ---------------------------------------------------------------------------
# Normalisation (configuration shorthand -> CoreHub payload)
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _string_list(value: Any) -> List[str]:
    return [_text(v) for v in value or [] if _text(v)]


def normalize_inbound_collection(collection: Any, existing_id: Optional[str] = None) -> Dict[str, Any]:
    """``InboundEndpointCollection`` payload (without its endpoints); ``basePath`` defaults to the name."""
    if not isinstance(collection, dict):
        raise CourierConfigError(f"collection must be an object, got {type(collection).__name__}")
    name = _text(collection.get("name"))
    return {
        "id": existing_id or collection.get("id") or stable_id("inbound-collection", name),
        "name": name,
        "description": collection.get("description"),
        "basePath": _text(collection.get("basePath") or name).strip("/"),
        "tags": list(collection.get("tags") or []),
        "allowlistEnabled": bool(collection.get("allowlist", collection.get("allowlistEnabled", False))),
        "allowedTokenIds": _string_list(collection.get("allowedTokenIds")),
    }


def _body_type(endpoint: Dict[str, Any], verb: str) -> str:
    raw = _text(endpoint.get("bodyType")).upper()
    if not raw:
        return "JSON" if verb in VERBS_WITH_BODY else "NONE"
    return BODY_TYPE_ALIASES.get(raw, raw)


def normalize_inbound_endpoint(endpoint: Any, collection_id: str, pipeline_id: str, agent_id: str,
                               existing_id: Optional[str] = None) -> Dict[str, Any]:
    """``InboundEndpoint`` payload of a pipeline endpoint served by the HTTP Source ``agent_id``."""
    if not isinstance(endpoint, dict):
        raise CourierConfigError(f"endpoint must be an object, got {type(endpoint).__name__}")
    name = _text(endpoint.get("name"))
    verb = _text(endpoint.get("httpVerb", endpoint.get("method"))).upper() or "POST"
    operation = _text(endpoint.get("operation")).upper() or OPERATION_BY_VERB.get(verb, "")
    path = _text(endpoint.get("path", endpoint.get("pathTemplate"))) or "/"
    operation_binding = _text(endpoint.get("operationBinding")) or None
    return {
        "id": existing_id or endpoint.get("id") or stable_id("inbound-endpoint", name, collection_id),
        "name": name,
        "collectionId": collection_id,
        "description": endpoint.get("description"),
        "path": path,
        "httpVerb": verb,
        "operation": operation,
        "pipelineId": pipeline_id,
        "agentId": agent_id,
        "bindings": [normalize_binding(b) for b in endpoint.get("bindings") or []],
        "keyBindings": _string_list(endpoint.get("keyBindings")),
        "operationBinding": operation_binding,
        "bodyType": _body_type(endpoint, verb),
    }


# ---------------------------------------------------------------------------
# Validation (mirrors InboundEndpointValidator for pipeline endpoints)
# ---------------------------------------------------------------------------

# Validation message shared by the collection, endpoint and token entries.
NAME_REQUIRED = "name is required"


def validate_inbound_collection(collection: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not collection.get("name"):
        errors.append(NAME_REQUIRED)
    if not BASE_PATH_PATTERN.match(collection.get("basePath") or ""):
        errors.append(
            f"basePath {collection.get('basePath')!r} is not valid: use letters, digits, '.', '_', '~', '-' separated by '/'"
        )
    return [f"collection '{collection.get('name')}': {e}" for e in errors]


def _placeholders(path: str) -> List[str]:
    segments = [s for s in path.strip("/").split("/") if s]
    return [s[1:-1] for s in segments if len(s) > 2 and s.startswith("{") and s.endswith("}")]


def _path_errors(path: str) -> List[str]:
    if not path.startswith("/"):
        return [f"path {path!r} must start with '/'"]
    return [
        f"placeholder in {segment!r} must be a whole path segment, like /{{id}}"
        for segment in (s for s in path.strip("/").split("/") if s)
        if ("{" in segment or "}" in segment) and not (len(segment) > 2 and segment[0] == "{" and segment[-1] == "}")
    ]


def _default_value_error(binding: Dict[str, Any]) -> Optional[str]:
    default = binding.get("defaultValue")
    if default is None:
        return None
    if binding.get("type") == "NUMBER" and not NUMBER_PATTERN.match(default.strip()):
        return f"binding '{binding.get('name')}': defaultValue {default!r} is not a number"
    if binding.get("type") == "BOOLEAN" and default.strip().lower() not in ("true", "false"):
        return f"binding '{binding.get('name')}': defaultValue {default!r} is not true or false"
    return None


def _binding_errors(endpoint: Dict[str, Any], binding: Dict[str, Any]) -> List[str]:
    name = binding.get("name") or ""
    errors: List[str] = []
    if not NAME_PATTERN.match(name):
        errors.append(f"binding name {name!r} is not valid")
    elif name.lower() == SEQUENCE_COLUMN:
        errors.append(f"binding name '{name}' is reserved for the request sequence")
    if binding.get("location") not in BINDING_LOCATIONS:
        errors.append(f"binding {name!r}: location {binding.get('location')!r} is not one of {list(BINDING_LOCATIONS)}")
    if binding.get("type") not in BINDING_TYPES:
        errors.append(f"binding {name!r}: type {binding.get('type')!r} is not one of {list(BINDING_TYPES)}")
    location = binding.get("location")
    if location == "BODY" and endpoint.get("httpVerb") not in VERBS_WITH_BODY:
        errors.append(f"BODY binding '{name}' needs a verb with a body (POST or PUT)")
    elif location == "HEADER" and not HEADER_NAME_PATTERN.match(name):
        errors.append(f"HEADER binding '{name}' is not a valid header name")
    elif location == "HEADER" and name.lower() in CREDENTIAL_HEADERS:
        errors.append(f"HEADER binding '{name}' would write the caller's credentials into the target")
    elif location == "PATH" and not binding.get("required"):
        errors.append(f"PATH binding '{name}' must be required")
    default_error = _default_value_error(binding)
    if default_error:
        errors.append(default_error)
    return errors


def _duplicate_binding_errors(bindings: List[Dict[str, Any]]) -> List[str]:
    seen: Dict[str, str] = {}
    errors: List[str] = []
    for binding in bindings:
        name = binding.get("name") or ""
        if name.lower() in seen:
            errors.append(f"binding '{name}' is declared more than once (names are compared ignoring case)")
        seen[name.lower()] = name
    return errors


def _placeholder_errors(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> List[str]:
    placeholders = _placeholders(endpoint.get("path") or "")
    path_bindings = [b.get("name") for b in bindings if b.get("location") == "PATH"]
    errors = [f"placeholder '{{{p}}}' needs a PATH binding with the same name" for p in placeholders if p not in path_bindings]
    errors.extend(f"PATH binding '{b}' has no '{{{b}}}' placeholder in the path" for b in path_bindings if b not in placeholders)
    return errors


def _key_names(endpoint: Dict[str, Any]) -> List[str]:
    return endpoint.get("keyBindings") or [
        b.get("name") for b in endpoint.get("bindings") or [] if b.get("required") and b.get("location") == "PATH"
    ]


def _key_errors(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> List[str]:
    by_name = {b.get("name"): b for b in bindings}
    errors: List[str] = []
    for key in endpoint.get("keyBindings") or []:
        if key not in by_name:
            errors.append(f"key binding '{key}' is not declared")
        elif not by_name[key].get("required"):
            errors.append(f"key binding '{key}' must be required")
    operation_binding = endpoint.get("operationBinding")
    if operation_binding is not None:
        if operation_binding not in by_name:
            errors.append(f"operationBinding '{operation_binding}' is not declared")
        elif by_name[operation_binding].get("type") != "STRING":
            errors.append(f"operationBinding '{operation_binding}' must be a STRING binding")
    if not _key_names(endpoint) and (endpoint.get("operation") in ("UPDATE", "DELETE") or operation_binding):
        errors.append(
            "needs a key to find the row (a key binding or a required PATH binding): without one every request "
            f"gets a new '{SEQUENCE_COLUMN}' key and its updates and deletes change nothing"
        )
    return errors


def _verb_errors(endpoint: Dict[str, Any]) -> List[str]:
    verb = endpoint.get("httpVerb")
    operation = endpoint.get("operation")
    if verb == "PATCH":
        return ["PATCH is not available on a pipeline endpoint: a row carries every binding and replaces the target row, so use PUT"]
    if verb not in OPERATION_BY_VERB:
        return [f"httpVerb {verb!r} is not one of {list(OPERATION_BY_VERB)}"]
    if operation not in OPERATION_BY_VERB.values():
        return [f"operation {operation!r} is not one of {sorted(set(OPERATION_BY_VERB.values()))}"]
    if OPERATION_BY_VERB[verb] != operation:
        return [f"httpVerb {verb} does not fit operation {operation}"]
    return []


def validate_inbound_endpoint(endpoint: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not endpoint.get("name"):
        errors.append(NAME_REQUIRED)
    errors.extend(_verb_errors(endpoint))
    if endpoint.get("bodyType") not in INBOUND_BODY_TYPES:
        errors.append(f"bodyType {endpoint.get('bodyType')!r} is not one of {list(INBOUND_BODY_TYPES)}")
    bindings = endpoint.get("bindings") or []
    if not bindings:
        errors.append("a pipeline endpoint needs at least one binding")
    path_errors = _path_errors(endpoint.get("path") or "")
    errors.extend(path_errors)
    errors.extend(_duplicate_binding_errors(bindings))
    for binding in bindings:
        errors.extend(_binding_errors(endpoint, binding))
    if not path_errors:
        errors.extend(_placeholder_errors(endpoint, bindings))
    errors.extend(_key_errors(endpoint, bindings))
    if endpoint.get("bodyType") == "NONE" and any(b.get("location") == "BODY" for b in bindings):
        errors.append("BODY bindings need a body type")
    return [f"endpoint '{endpoint.get('name')}': {e}" for e in errors]


def normalize_token(token: Any, collection_names: List[str]) -> Dict[str, Any]:
    """A token to create: name, expiresInDays, the collections whose allowlist it joins, outputFile."""
    if not isinstance(token, dict):
        raise CourierConfigError(f"token must be an object, got {type(token).__name__}")
    expires = token.get("expiresInDays")
    return {
        "name": _text(token.get("name")),
        "expiresInDays": None if expires is None else int(expires),
        "collections": _string_list(token.get("collections")) or list(collection_names),
        "outputFile": _text(token.get("outputFile")),
    }


def validate_token(token: Dict[str, Any], collection_names: List[str]) -> List[str]:
    errors: List[str] = []
    if not token.get("name"):
        errors.append(NAME_REQUIRED)
    if not token.get("outputFile"):
        errors.append("outputFile is required: CoreHub shows a token's plaintext only once")
    if token.get("expiresInDays") is not None and token["expiresInDays"] <= 0:
        errors.append("expiresInDays must be positive")
    errors.extend(f"unknown collection '{c}'" for c in token.get("collections") or [] if c not in collection_names)
    return [f"token '{token.get('name')}': {e}" for e in errors]


# ---------------------------------------------------------------------------
# Plan (pure)
# ---------------------------------------------------------------------------

def _by_name(items: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {str(i["name"]): i for i in items if isinstance(i, dict) and i.get("name") is not None}


def _plan_inbound_endpoints(collection_cfg: Dict[str, Any], collection: Dict[str, Any], stored_endpoints: Dict[str, Dict[str, Any]],
                            pipeline_id: str, agent_id: str, errors: List[str]) -> List[Dict[str, Any]]:
    endpoints: List[Dict[str, Any]] = []
    names: set = set()
    for endpoint_cfg in collection_cfg.get("endpoints") or []:
        stored = stored_endpoints.get(_text((endpoint_cfg or {}).get("name")))
        endpoint = normalize_inbound_endpoint(
            endpoint_cfg, collection["id"], pipeline_id, agent_id, existing_id=stored.get("id") if stored else None
        )
        if endpoint["name"] in names:
            errors.append(f"endpoint '{endpoint['name']}' of collection '{collection['name']}' is declared twice")
        names.add(endpoint["name"])
        errors.extend(validate_inbound_endpoint(endpoint))
        endpoints.append({"definition": endpoint, "exists": stored is not None})
    if not endpoints:
        errors.append(f"collection '{collection['name']}' declares no endpoint")
    return endpoints


def _plan_collections(collections_cfg: List[Any], existing_collections: Any,
                      existing_endpoints_for: Optional[Callable[[str], Any]],
                      pipeline_id: str, agent_id: str, errors: List[str]) -> List[Dict[str, Any]]:
    stored_collections = _by_name(existing_collections)
    plan: List[Dict[str, Any]] = []
    names: set = set()
    base_paths: set = set()
    for collection_cfg in collections_cfg:
        stored = stored_collections.get(_text((collection_cfg or {}).get("name")))
        collection = normalize_inbound_collection(collection_cfg, existing_id=stored.get("id") if stored else None)
        if collection["name"] in names:
            errors.append(f"collection '{collection['name']}' is declared twice")
        if collection["basePath"] in base_paths:
            errors.append(f"basePath '{collection['basePath']}' is used by two collections")
        names.add(collection["name"])
        base_paths.add(collection["basePath"])
        errors.extend(validate_inbound_collection(collection))
        stored_endpoints = _by_name(existing_endpoints_for(collection["id"])) if (existing_endpoints_for and stored) else {}
        endpoints = _plan_inbound_endpoints(collection_cfg, collection, stored_endpoints, pipeline_id, agent_id, errors)
        plan.append({"collection": collection, "exists": stored is not None, "endpoints": endpoints})
    return plan


def plan_inbound_definitions(courier_cfg: Any, pipeline_id: str, agent_id: str, existing_collections: Any = None,
                             existing_endpoints_for: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Normalised and validated definitions: ``{"collections": [...], "tokens": [...], "force": bool}``.

    Pure: no CoreHub call. ``existing_collections`` is what ``GET /api-agent/inbound/collections``
    returned and ``existing_endpoints_for(collection_id)`` what ``GET /api-agent/inbound/endpoints``
    returns for it, so that stored definitions keep their id and are updated instead of duplicated.
    """
    if not isinstance(courier_cfg, dict):
        raise CourierConfigError("'courier' must be an object with a 'collections' list")
    collections_cfg = courier_cfg.get("collections")
    if not isinstance(collections_cfg, list) or not collections_cfg:
        raise CourierConfigError("'courier.collections' must be a non-empty list")
    if not pipeline_id or not agent_id:
        raise CourierConfigError("the HTTP Source endpoints need the pipeline and the agent id")

    errors: List[str] = []
    collections = _plan_collections(collections_cfg, existing_collections, existing_endpoints_for, pipeline_id, agent_id, errors)
    collection_names = [c["collection"]["name"] for c in collections]
    tokens: List[Dict[str, Any]] = []
    for token_cfg in courier_cfg.get("tokens") or []:
        token = normalize_token(token_cfg, collection_names)
        errors.extend(validate_token(token, collection_names))
        tokens.append(token)
    if errors:
        raise CourierConfigError("Invalid 'courier' configuration of the HTTP Source:\n  - " + "\n  - ".join(errors))
    return {"collections": collections, "tokens": tokens, "force": bool(courier_cfg.get("force", False))}


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def _default_fetch() -> FetchFn:
    from commons import fetch_core_hub  # imported lazily: commons initialises logging on import
    return fetch_core_hub


def write_token_file(path: str, content: Dict[str, Any]) -> None:
    """Writes the token JSON readable by its owner only; the directory is created when missing."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Restricted before the plaintext is written (no bitwise flags: test_pyinstaller_compatibility reads them as X | Y hints).
    target = Path(path)
    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(content, handle)
        handle.write("\n")


def _rotate_token(call: FetchFn, auth: str, token: Dict[str, Any], existing_tokens: Any, pipeline_id: str,
                  agent_id: str, log: logging.Logger) -> str:
    for stale in existing_tokens if isinstance(existing_tokens, list) else []:
        if isinstance(stale, dict) and stale.get("name") == token["name"] and stale.get("active"):
            log.info("Revoking the previous Personal API Token '%s' (id=%s)", token["name"], stale.get("id"))
            call(f"{API_TOKENS_PATH}/{stale['id']}/revoke", method="POST", token=auth)
    created = call(API_TOKENS_PATH, method="POST", token=auth, body={
        "name": token["name"],
        "expiresInDays": token["expiresInDays"],
        "scopes": [{"pipelineId": pipeline_id, "agentId": agent_id}],
        "permissionScopes": [ENDPOINT_INBOUND_PERMISSION],
    })
    if not isinstance(created, dict) or not created.get("id") or not created.get("token"):
        raise RuntimeError(f"CoreHub did not return the Personal API Token '{token['name']}'")
    write_token_file(token["outputFile"], {
        "token": created["token"],
        "tokenId": created["id"],
        "name": token["name"],
        "pipelineId": pipeline_id,
        "agentId": agent_id,
    })
    # The plaintext never reaches the log: only where it was written.
    log.info("Created Personal API Token '%s' (id=%s), written to %s", token["name"], created["id"], token["outputFile"])
    return created["id"]


def _status_of(error: Exception) -> Optional[int]:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


def _upsert_endpoint(call: FetchFn, auth: str, item: Dict[str, Any], force: bool, log: logging.Logger) -> None:
    endpoint = item["definition"]
    if not item["exists"]:
        log.info("Creating inbound endpoint '%s' (%s %s)", endpoint["name"], endpoint["httpVerb"], endpoint["path"])
        call(INBOUND_ENDPOINTS_PATH, method="POST", token=auth, body=endpoint)
        return
    log.info("Updating inbound endpoint '%s' (%s %s)", endpoint["name"], endpoint["httpVerb"], endpoint["path"])
    try:
        call(f"{INBOUND_ENDPOINTS_PATH}/{endpoint['id']}", method="PUT", token=auth, body=endpoint,
             params={"force": "true"} if force else None)
    except Exception as error:
        if _status_of(error) == 409:
            raise CourierConfigError(
                f"Inbound endpoint '{endpoint['name']}' is read by entities of another pipeline and CoreHub refused "
                "to change it. Set \"force\": true in the 'courier' block to change it anyway (those entities break)."
            ) from error
        raise


def _rotate_tokens(call: FetchFn, token: str, token_defs: List[Dict[str, Any]], pipeline_id: str, agent_id: str,
                   log: logging.Logger) -> Dict[str, List[str]]:
    """Rotates the configured tokens and returns their ids by the name of each collection they are for."""
    token_ids_by_collection: Dict[str, List[str]] = {}
    if not token_defs:
        return token_ids_by_collection
    existing_tokens = call(API_TOKENS_PATH, method="GET", token=token)
    for token_def in token_defs:
        token_id = _rotate_token(call, token, token_def, existing_tokens, pipeline_id, agent_id, log)
        for name in token_def["collections"]:
            token_ids_by_collection.setdefault(name, []).append(token_id)
    return token_ids_by_collection


def _upsert_collection(call: FetchFn, token: str, item: Dict[str, Any], token_ids_by_collection: Dict[str, List[str]],
                       log: logging.Logger) -> str:
    """Creates or updates one planned inbound collection, its allowlist joined by the tokens for it; returns its name."""
    collection = dict(item["collection"])
    collection["allowedTokenIds"] = collection["allowedTokenIds"] + token_ids_by_collection.get(collection["name"], [])
    if item["exists"]:
        log.info("Updating inbound collection '%s' (basePath=%s)", collection["name"], collection["basePath"])
        call(f"{INBOUND_COLLECTIONS_PATH}/{collection['id']}", method="PUT", token=token, body=collection)
    else:
        log.info("Creating inbound collection '%s' (basePath=%s)", collection["name"], collection["basePath"])
        call(INBOUND_COLLECTIONS_PATH, method="POST", token=token, body=collection)
    if collection["allowlistEnabled"] and not collection["allowedTokenIds"]:
        log.warning("Inbound collection '%s' has its allowlist on and no token on it: every request is refused", collection["name"])
    return collection["name"]


def provision_inbound(token: str, courier_cfg: Dict[str, Any], pipeline_id: str, agent_id: str,
                      fetch: Optional[FetchFn] = None, logger: Optional[logging.Logger] = None) -> List[str]:
    """Creates or updates the inbound collections, endpoints and tokens of an HTTP Source in CoreHub.

    Tokens come first so that their ids can join the allowlist of the collections they are for.
    Returns the collection names, in configuration order. Raises ``CourierConfigError`` on an invalid
    configuration, and whatever ``fetch`` raises on a CoreHub failure.
    """
    log = logger or _logger
    call = fetch or _default_fetch()

    existing_collections = call(INBOUND_COLLECTIONS_PATH, method="GET", token=token)

    def existing_endpoints_for(collection_id: str) -> Any:
        return call(INBOUND_ENDPOINTS_PATH, method="GET", token=token, params={"collectionId": collection_id})

    plan = plan_inbound_definitions(courier_cfg, pipeline_id, agent_id, existing_collections, existing_endpoints_for)

    token_ids_by_collection = _rotate_tokens(call, token, plan["tokens"], pipeline_id, agent_id, log)

    names: List[str] = []
    for item in plan["collections"]:
        names.append(_upsert_collection(call, token, item, token_ids_by_collection, log))
        for endpoint_item in item["endpoints"]:
            _upsert_endpoint(call, token, endpoint_item, plan["force"], log)
    return names


def apply_inbound_defaults(agent: Dict[str, Any], collection_names: List[str],
                           logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Fills ``customHostCredentials.inboundCollection`` when exactly one collection is configured.

    Without it the HTTP Source serves every inbound collection of its pipeline, which is also fine;
    with one collection the explicit value only makes discovery match what the user configured.
    """
    log = logger or _logger
    custom = agent.get("customHostCredentials")
    if not isinstance(custom, dict):
        custom = {}
        agent["customHostCredentials"] = custom
    names = [n for n in collection_names if n]
    if not _text(custom.get(INBOUND_COLLECTION_KEY)) and len(names) == 1:
        custom[INBOUND_COLLECTION_KEY] = names[0]
        log.info("HTTP Source inbound collection defaulted to '%s'", names[0])
    return agent


def provision_courier_agent(token: str, pipeline_id: str, agent: Dict[str, Any], fetch: Optional[FetchFn] = None,
                            logger: Optional[logging.Logger] = None) -> Optional[Dict[str, Any]]:
    """Creates the Courier definitions of an HTTP Source or HTTP Target agent entry and fills its defaults.

    The agent discovers them (collection = schema, endpoint = table, binding = column), so they must
    exist before it is configured:

    - HTTP Source (SOURCE): inbound collections and pipeline endpoints bound to ``pipeline_id`` and the
      agent, the Personal API Tokens clients call them with, then ``inboundCollection``;
    - HTTP Target (TARGET): outbound collections and endpoints, then ``outboundCollection``.

    Returns the agent's ``customHostCredentials`` to configure, or None when there is nothing to do
    (another agent, or no ``courier`` block). Raises ``CourierConfigError`` on an invalid block.
    """
    log = logger or _logger
    if not is_courier_agent(agent):
        return None
    source = is_http_source_agent(agent)
    if not agent.get("courier"):
        log.warning(
            "%s agent %s has no 'courier' block: its Courier collections and endpoints must already exist in CoreHub",
            "HTTP Source" if source else "HTTP Target", agent.get("agentId"),
        )
        return None
    if source:
        names = provision_inbound(token, agent["courier"], pipeline_id, agent["agentId"], fetch=fetch, logger=log)
        apply_inbound_defaults(agent, names, logger=log)
    else:
        names = provision_courier(token, agent["courier"], fetch=fetch, logger=log)
        apply_courier_defaults(agent, names, logger=log)
    return agent["customHostCredentials"]

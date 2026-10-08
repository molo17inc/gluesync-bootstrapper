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
"""Support for the HTTP Target agent (``api-agent-shadow``, the Courier outbound target).

The HTTP Target turns every row the pipeline emits into one HTTP call toward an external REST
API. What it calls is not a database: it is a *Courier collection* (base URL + authentication)
holding *endpoints* (verb, path template, headers, bindings). CoreHub exposes them to the
pipeline through discovery as schema = collection, table = endpoint, column = binding, so the
usual entity creation of the bootstrapper applies unchanged once the definitions exist.

This module creates those definitions from the bootstrapper configuration, so that a pipeline
toward an HTTP Target can be bootstrapped end to end without touching the Courier UI. The
``courier`` block of a TARGET agent entry in ``config.json`` looks like this::

    {
      "agentType": "TARGET",
      "agentTag": "api-agent-shadow",
      "hostCredentials": {"connectionName": "HTTP Target"},
      "customHostCredentials": {},
      "courier": {
        "collections": [
          {
            "name": "demo-api",
            "baseUrl": "http://api.example.com",
            "auth": {"type": "Bearer", "token": "secret"},
            "endpoints": [
              {
                "name": "customers",
                "httpVerb": "POST",
                "pathTemplate": "/customers/{id}",
                "bodyType": "JSON",
                "headers": [{"name": "X-Operation", "valueTemplate": "{__operation}"}],
                "bindings": [
                  {"name": "id", "location": "PATH", "type": "NUMBER"},
                  {"name": "name", "location": "BODY"},
                  {"name": "email", "location": "BODY", "required": false}
                ]
              }
            ]
          }
        ]
      }
    }

Collections and endpoints are matched by name and updated in place when they already exist, so
running the bootstrapper twice is idempotent. ``customHostCredentials.outboundCollection`` is
filled in with the collection name when the configuration declares exactly one collection and
does not set it.

The payloads mirror the Kotlin models of ``gluesync-kotlin/api-agent`` (``EndpointCollection``,
``EndpointDefinition``, ``BindingDefinition``, ``HeaderDefinition``, ``CollectionAuth``) and the
checks of ``EndpointCollectionValidator`` / ``EndpointDefinitionValidator``, so that a wrong
configuration fails here with a readable message instead of a 400 from CoreHub.
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

COURIER_AGENT_TAG = "api-agent-shadow"
COURIER_CONFIG_KEY = "courier"
OUTBOUND_COLLECTION_KEY = "outboundCollection"

COLLECTIONS_PATH = "/api-agent/collections"
ENDPOINTS_PATH = "/api-agent/endpoints"

HTTP_VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
BODY_VERBS = ("POST", "PUT", "PATCH")
BODY_TYPES = ("NONE", "JSON", "FORM_URLENCODED")
BINDING_LOCATIONS = ("PATH", "QUERY", "HEADER", "BODY")
BINDING_TYPES = ("STRING", "NUMBER", "BOOLEAN")
AUTH_TYPES = ("None", "Basic", "Bearer", "ApiKey")
API_KEY_LOCATIONS = ("HEADER", "QUERY")
DEFAULT_OPERATION_FIELD = "operation"
# The value the HTTP Target adds to every row: Insert, Update or Delete. Usable in templates.
OPERATION_BINDING = "__operation"

# Same grammar as the `{placeholder}` names the HTTP Target templates accept.
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")

# Stable ids for definitions created by the bootstrapper: the same name always gets the same
# id, so a re-run that cannot read the existing definitions still upserts instead of duplicating.
_ID_NAMESPACE = uuid.UUID("5d9c1b6e-3f0a-4c3e-9f2a-7c1d2e4b8a10")

FetchFn = Callable[..., Any]

_logger = logging.getLogger(__name__)


class CourierConfigError(ValueError):
    """A ``courier`` block that CoreHub would refuse, reported before anything is sent."""


def is_courier_agent(agent: Optional[Dict[str, Any]]) -> bool:
    """True when the configured agent is the HTTP Target."""
    if not isinstance(agent, dict):
        return False
    tag = agent.get("agentInternalName") or agent.get("agentTag") or ""
    return str(tag).strip().lower() == COURIER_AGENT_TAG


def stable_id(kind: str, name: str, parent: str = "") -> str:
    """Deterministic id for a collection (``kind='collection'``) or an endpoint of a collection."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{kind}:{parent}:{name}"))


# ---------------------------------------------------------------------------
# Normalisation (configuration shorthand -> CoreHub payload)
# ---------------------------------------------------------------------------

def _upper(value: Any, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text.upper() if text else default


def normalize_auth(auth: Any) -> Dict[str, Any]:
    """``CollectionAuth`` wire format: ``{"type": "None"|"Basic"|"Bearer"|"ApiKey", ...}``."""
    if auth is None:
        return {"type": "None"}
    if isinstance(auth, str):
        auth = {"type": auth}
    if not isinstance(auth, dict):
        raise CourierConfigError(f"auth must be an object, got {type(auth).__name__}")

    raw_type = str(auth.get("type") or "None").strip()
    auth_type = next((t for t in AUTH_TYPES if t.lower() == raw_type.lower()), None)
    if auth_type is None:
        raise CourierConfigError(f"auth.type {raw_type!r} is not one of {list(AUTH_TYPES)}")

    if auth_type == "None":
        return {"type": "None"}
    if auth_type == "Basic":
        return {"type": "Basic", "username": auth.get("username", ""), "password": auth.get("password")}
    if auth_type == "Bearer":
        return {"type": "Bearer", "token": auth.get("token")}
    location = _upper(auth.get("location"), "HEADER")
    if location not in API_KEY_LOCATIONS:
        raise CourierConfigError(f"auth.location {location!r} is not one of {list(API_KEY_LOCATIONS)}")
    return {"type": "ApiKey", "location": location, "name": auth.get("name", ""), "value": auth.get("value")}


def normalize_binding(binding: Any) -> Dict[str, Any]:
    """``BindingDefinition``: name, type (STRING), location (BODY), defaultValue, required (true), description."""
    if isinstance(binding, str):
        binding = {"name": binding}
    if not isinstance(binding, dict):
        raise CourierConfigError(f"binding must be an object or a name, got {type(binding).__name__}")
    default_value = binding.get("defaultValue", binding.get("default"))
    return {
        "name": str(binding.get("name") or "").strip(),
        "type": _upper(binding.get("type"), "STRING"),
        "location": _upper(binding.get("location"), "BODY"),
        "defaultValue": None if default_value is None else str(default_value),
        "required": bool(binding.get("required", True)),
        "description": binding.get("description"),
    }


def normalize_header(header: Any) -> Dict[str, Any]:
    """``HeaderDefinition``: name + valueTemplate (``{binding}`` placeholders allowed)."""
    if not isinstance(header, dict):
        raise CourierConfigError(f"header must be an object, got {type(header).__name__}")
    value = header.get("valueTemplate", header.get("value", ""))
    return {
        "name": str(header.get("name") or "").strip(),
        "valueTemplate": "" if value is None else str(value),
        "description": header.get("description"),
    }


def normalize_collection(collection: Dict[str, Any], existing_id: Optional[str] = None) -> Dict[str, Any]:
    """``EndpointCollection`` payload (without its endpoints)."""
    if not isinstance(collection, dict):
        raise CourierConfigError(f"collection must be an object, got {type(collection).__name__}")
    name = str(collection.get("name") or "").strip()
    return {
        "id": existing_id or collection.get("id") or stable_id("collection", name),
        "name": name,
        "description": collection.get("description"),
        "tags": list(collection.get("tags") or []),
        "baseUrl": str(collection.get("baseUrl") or "").strip(),
        "auth": normalize_auth(collection.get("auth")),
    }


def normalize_endpoint(endpoint: Dict[str, Any], collection_id: str, existing_id: Optional[str] = None) -> Dict[str, Any]:
    """``EndpointDefinition`` payload for an endpoint of ``collection_id``."""
    if not isinstance(endpoint, dict):
        raise CourierConfigError(f"endpoint must be an object, got {type(endpoint).__name__}")
    name = str(endpoint.get("name") or "").strip()
    verb = _upper(endpoint.get("httpVerb", endpoint.get("method")), "POST")
    body_type = _upper(endpoint.get("bodyType"), "JSON" if verb in BODY_VERBS else "NONE")
    path_template = endpoint.get("pathTemplate", endpoint.get("path"))
    path_template = "" if path_template is None else str(path_template).strip()
    return {
        "id": existing_id or endpoint.get("id") or stable_id("endpoint", name, collection_id),
        "name": name,
        "collectionId": collection_id,
        "description": endpoint.get("description"),
        "pathTemplate": path_template,
        "httpVerb": verb,
        "bodyType": body_type,
        "operationField": str(endpoint.get("operationField") or DEFAULT_OPERATION_FIELD),
        "headers": [normalize_header(h) for h in endpoint.get("headers") or []],
        "bindings": [normalize_binding(b) for b in endpoint.get("bindings") or []],
    }


# ---------------------------------------------------------------------------
# Validation (mirrors EndpointCollectionValidator / EndpointDefinitionValidator)
# ---------------------------------------------------------------------------

def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def validate_collection(collection: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not collection.get("name"):
        errors.append("name is required")
    if not _is_http_url(collection.get("baseUrl") or ""):
        errors.append("baseUrl must be an http:// or https:// URL")
    auth = collection.get("auth") or {"type": "None"}
    auth_type = auth.get("type")
    if auth_type == "Basic":
        if not str(auth.get("username") or "").strip():
            errors.append("auth.username is required")
        if not str(auth.get("password") or "").strip():
            errors.append("auth.password is required")
    elif auth_type == "Bearer":
        if not str(auth.get("token") or "").strip():
            errors.append("auth.token is required")
    elif auth_type == "ApiKey":
        if not str(auth.get("name") or "").strip():
            errors.append("auth.name is required")
        if not str(auth.get("value") or "").strip():
            errors.append("auth.value is required")
    return [f"collection '{collection.get('name')}': {e}" for e in errors]


def validate_endpoint(endpoint: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not endpoint.get("name"):
        errors.append("name is required")
    if not endpoint.get("collectionId"):
        errors.append("collectionId is required")
    if endpoint.get("httpVerb") not in HTTP_VERBS:
        errors.append(f"httpVerb {endpoint.get('httpVerb')!r} is not one of {list(HTTP_VERBS)}")
    if endpoint.get("bodyType") not in BODY_TYPES:
        errors.append(f"bodyType {endpoint.get('bodyType')!r} is not one of {list(BODY_TYPES)}")
    path_template = endpoint.get("pathTemplate") or ""
    if path_template and not path_template.startswith("/"):
        errors.append(f"pathTemplate {path_template!r} must start with '/'")

    names = [b.get("name") or "" for b in endpoint.get("bindings") or []]
    for binding in endpoint.get("bindings") or []:
        binding_name = binding.get("name") or ""
        if not NAME_PATTERN.match(binding_name):
            errors.append(f"binding name {binding_name!r} is not valid")
        if binding.get("location") not in BINDING_LOCATIONS:
            errors.append(f"binding {binding_name!r}: location {binding.get('location')!r} is not one of {list(BINDING_LOCATIONS)}")
        if binding.get("type") not in BINDING_TYPES:
            errors.append(f"binding {binding_name!r}: type {binding.get('type')!r} is not one of {list(BINDING_TYPES)}")
    seen = set()
    for binding_name in names:
        if binding_name in seen:
            errors.append(f"binding '{binding_name}' is declared twice")
        seen.add(binding_name)

    operation_field = endpoint.get("operationField") or ""
    if not NAME_PATTERN.match(operation_field):
        errors.append(f"operationField {operation_field!r} is not a valid field name")
    if operation_field in names:
        errors.append(f"operationField {operation_field!r} is also a binding name")

    # `__operation` is always resolvable: the HTTP Target adds it to every row (Insert/Update/Delete).
    for placeholder in re.findall(r"\$?\{([A-Za-z_][A-Za-z0-9_.\-]*)\}", path_template):
        if placeholder not in names and placeholder != OPERATION_BINDING:
            errors.append(f"pathTemplate placeholder '{{{placeholder}}}' has no binding")

    sends_body = endpoint.get("httpVerb") in BODY_VERBS and endpoint.get("bodyType") != "NONE"
    body_bindings = [b.get("name") for b in endpoint.get("bindings") or [] if b.get("location") == "BODY"]
    if body_bindings and not sends_body:
        errors.append(
            f"BODY bindings need a body: {endpoint.get('httpVerb')} with body type {endpoint.get('bodyType')} "
            f"sends none ({', '.join(body_bindings)})"
        )
    for header in endpoint.get("headers") or []:
        if not str(header.get("name") or "").strip():
            errors.append("header name is required")
    return [f"endpoint '{endpoint.get('name')}': {e}" for e in errors]


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def _default_fetch() -> FetchFn:
    from commons import fetch_core_hub  # imported lazily: commons initialises logging on import
    return fetch_core_hub


def _by_name(items: Any) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("name") is not None:
                result[str(item["name"])] = item
    return result


def plan_courier_definitions(courier_cfg: Dict[str, Any], existing_collections: Any = None,
                             existing_endpoints_for: Optional[Callable[[str], Any]] = None) -> List[Dict[str, Any]]:
    """Normalised and validated definitions, each ``{"collection": {...}, "endpoints": [...], "exists": bool}``.

    Pure: no CoreHub call. ``existing_collections`` is what ``GET /api-agent/collections`` returned and
    ``existing_endpoints_for(collection_id)`` what ``GET /api-agent/endpoints?collectionId=`` returns, so
    that already stored definitions keep their id and are updated instead of duplicated.
    """
    if not isinstance(courier_cfg, dict):
        raise CourierConfigError("'courier' must be an object with a 'collections' list")
    collections_cfg = courier_cfg.get("collections")
    if not isinstance(collections_cfg, list) or not collections_cfg:
        raise CourierConfigError("'courier.collections' must be a non-empty list")

    stored_collections = _by_name(existing_collections)
    plan: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_names: set = set()

    for collection_cfg in collections_cfg:
        stored = stored_collections.get(str((collection_cfg or {}).get("name", "")).strip())
        collection = normalize_collection(collection_cfg, existing_id=stored.get("id") if stored else None)
        if collection["name"] in seen_names:
            errors.append(f"collection '{collection['name']}' is declared twice")
        seen_names.add(collection["name"])
        errors.extend(validate_collection(collection))

        stored_endpoints = _by_name(existing_endpoints_for(collection["id"])) if (existing_endpoints_for and stored) else {}
        endpoints: List[Dict[str, Any]] = []
        endpoint_names: set = set()
        for endpoint_cfg in collection_cfg.get("endpoints") or []:
            stored_endpoint = stored_endpoints.get(str((endpoint_cfg or {}).get("name", "")).strip())
            endpoint = normalize_endpoint(
                endpoint_cfg,
                collection_id=collection["id"],
                existing_id=stored_endpoint.get("id") if stored_endpoint else None,
            )
            if endpoint["name"] in endpoint_names:
                errors.append(f"endpoint '{endpoint['name']}' of collection '{collection['name']}' is declared twice")
            endpoint_names.add(endpoint["name"])
            errors.extend(validate_endpoint(endpoint))
            endpoints.append({"definition": endpoint, "exists": stored_endpoint is not None})
        if not endpoints:
            errors.append(f"collection '{collection['name']}' declares no endpoint")

        plan.append({"collection": collection, "exists": stored is not None, "endpoints": endpoints})

    if errors:
        raise CourierConfigError("Invalid 'courier' configuration:\n  - " + "\n  - ".join(errors))
    return plan


def provision_courier(token: str, courier_cfg: Dict[str, Any], fetch: Optional[FetchFn] = None,
                      logger: Optional[logging.Logger] = None) -> List[str]:
    """Creates or updates the Courier collections and endpoints of ``courier_cfg`` in CoreHub.

    Returns the names of the collections, in configuration order. Raises ``CourierConfigError`` on an
    invalid configuration, and whatever ``fetch`` raises on a CoreHub failure.
    """
    log = logger or _logger
    call = fetch or _default_fetch()

    existing_collections = call(COLLECTIONS_PATH, method="GET", token=token)

    def existing_endpoints_for(collection_id: str) -> Any:
        return call(ENDPOINTS_PATH, method="GET", token=token, params={"collectionId": collection_id})

    plan = plan_courier_definitions(courier_cfg, existing_collections, existing_endpoints_for)

    names: List[str] = []
    for item in plan:
        collection = item["collection"]
        if item["exists"]:
            log.info("Updating Courier collection '%s' (id=%s)", collection["name"], collection["id"])
            call(f"{COLLECTIONS_PATH}/{collection['id']}", method="PUT", token=token, body=collection)
        else:
            log.info("Creating Courier collection '%s' (id=%s)", collection["name"], collection["id"])
            call(COLLECTIONS_PATH, method="POST", token=token, body=collection)
        names.append(collection["name"])

        for endpoint_item in item["endpoints"]:
            endpoint = endpoint_item["definition"]
            if endpoint_item["exists"]:
                log.info("Updating Courier endpoint '%s' (%s %s)", endpoint["name"], endpoint["httpVerb"], endpoint["pathTemplate"])
                call(f"{ENDPOINTS_PATH}/{endpoint['id']}", method="PUT", token=token, body=endpoint)
            else:
                log.info("Creating Courier endpoint '%s' (%s %s)", endpoint["name"], endpoint["httpVerb"], endpoint["pathTemplate"])
                call(ENDPOINTS_PATH, method="POST", token=token, body=endpoint)
    return names


def apply_courier_defaults(agent: Dict[str, Any], collection_names: Iterable[str],
                           logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Fills ``customHostCredentials.outboundCollection`` when the configuration leaves it out.

    The HTTP Target scopes its outbound discovery to that collection: with one configured collection
    it is the obvious value, with several the user has to choose. The agent entry is modified in
    place and returned.
    """
    log = logger or _logger
    names = [n for n in collection_names if n]
    custom = agent.get("customHostCredentials")
    if not isinstance(custom, dict):
        custom = {}
        agent["customHostCredentials"] = custom
    if not str(custom.get(OUTBOUND_COLLECTION_KEY) or "").strip():
        if len(names) == 1:
            custom[OUTBOUND_COLLECTION_KEY] = names[0]
            log.info("HTTP Target outbound collection defaulted to '%s'", names[0])
        elif len(names) > 1:
            log.warning(
                "HTTP Target: %d Courier collections configured (%s) and no 'customHostCredentials.%s': "
                "discovery will expose every collection", len(names), ", ".join(names), OUTBOUND_COLLECTION_KEY
            )
    return agent


def courier_block(agent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The ``courier`` block of a configured agent, deep-copied, or None."""
    block = agent.get(COURIER_CONFIG_KEY) if isinstance(agent, dict) else None
    return copy.deepcopy(block) if isinstance(block, dict) else None

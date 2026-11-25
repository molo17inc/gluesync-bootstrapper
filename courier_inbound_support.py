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
"""Support for the HTTP Source agent (``courier-agent`` as SOURCE, the Courier inbound side).

Since Core Hub 2.3.2.2 an inbound *route* is defined free of any pipeline: verb, path, bindings. Its collection is
attached to exactly one HTTP Source (that agent's ``inboundCollection`` setting), and the pipeline of that agent
decides what each route does. Core Hub serves every route on its own inbound server (``GLUESYNC_COURIER_PORT``, 8090
by default) under ``/api-inbound/{basePath}{path}``:

- a *write* (POST = CREATE, PUT or PATCH = UPDATE, DELETE) becomes one row of every entity built on it, queued by the
  HTTP Source and answered ``202``; a PATCH writes only the bindings its request carries;
- a *read* (GET: READ with a path placeholder, LIST without) answers from the target table of the one entity built on
  it; its table's columns are its filters (PATH and QUERY bindings) and its ``responseFields``;
- an *EXECUTE* calls the stored procedure or function that is the target of the one entity built on it; the route
  declares the call's options and its return type (``routine``), the entity picks the routine and maps the bindings
  onto its parameters (see ``courier_routine_entities``).

Every route is a table of the HTTP Source (schema = collection name, table = route name), so the usual entity creation
of the bootstrapper maps writes and reads onto target tables once the routes exist. This module creates the
collections and routes of the ``courier`` block of a SOURCE agent entry, and the Personal API Tokens clients call
them with::

    {
      "agentType": "SOURCE",
      "agentTag": "courier-agent",
      "hostCredentials": {"connectionName": "HTTP Source"},
      "customHostCredentials": {"inboundCollection": "shop", "maxBodyKb": 64},
      "courier": {
        "collections": [
          {
            "name": "shop",
            "basePath": "shop",
            "allowlist": true,
            "endpoints": [
              {"name": "orders", "httpVerb": "POST", "path": "/orders/{id}",
               "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}, {"name": "status"}]},
              {"name": "order", "httpVerb": "GET", "path": "/orders/{id}",
               "bindings": [{"name": "id", "location": "PATH", "type": "NUMBER"}],
               "responseFields": [{"name": "id", "type": "NUMBER"}, {"name": "status"}]},
              {"name": "place_order", "httpVerb": "POST", "path": "/place-order",
               "bindings": [{"name": "customer"}, {"name": "amount", "type": "NUMBER"}],
               "routine": {"timeoutSeconds": 30, "answer": {"body": "ALL", "format": "JSON"}},
               "call": {"schema": "shop", "name": "place_order", "kind": "PROCEDURE"}}
            ]
          }
        ],
        "tokens": [
          {"name": "shop-client", "outputFile": "/secrets/http-source-token.json"}
        ]
      }
    }

Collections and routes are matched by name and updated in place when they already exist. The HTTP Source serves the
collection its ``inboundCollection`` names, which defaults to the only collection of the block: a block with several
collections must name one (the others are created attached to nothing, and answer 404). A token is created on every
run (CoreHub shows its plaintext only once), after revoking the active tokens with the same name, and its plaintext is
written to ``outputFile`` only. A token carries the ``gluesync.https_agent`` permission scope, is scoped to this
pipeline and HTTP Source, and to the collections it is for (``collections``, every configured one by default).

CoreHub saves an inbound collection only while an active token with that scope can call it, so the tokens are created
first and join the allowlist of the collections they are for. ``blockedTokenIds`` lists tokens that may never call a
collection.

The payloads mirror ``InboundEndpointCollection`` / ``InboundEndpoint`` of ``gluesync-kotlin/commons`` and the checks
of ``InboundEndpointValidator`` (``courier`` module), so that a wrong configuration fails here with a readable message.
"""

from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
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
    error_text,
    save_definition,
    stable_id,
    status_of,
)

INBOUND_COLLECTIONS_PATH = "/api-agent/inbound/collections"
INBOUND_ENDPOINTS_PATH = "/api-agent/inbound/endpoints"
API_TOKENS_PATH = "/api-tokens"
# The field of a token that narrows it to inbound collections (Core Hub with gluesync-kotlin !2954).
COLLECTION_SCOPES_KEY = "collectionScopes"
INBOUND_COLLECTION_KEY = "inboundCollection"
HTTPS_AGENT_PERMISSION = "gluesync.https_agent"
# CoreHub's code for a collection no active token can call (gluesync-kotlin CourierRouteSupport.MISSING_TOKEN_CODE).
MISSING_TOKEN_CODE = "MISSING_TOKEN"
# What the HTTP Source's own listener read before Core Hub 2.3.2.2 (ShadowAgentSettings.LISTENER_KEYS): Core Hub's
# inbound server serves every route now, and the agent only warns about these.
LISTENER_KEYS = (
    "inboundPort", "inboundBindHost", "inboundTlsKeystorePath", "inboundTlsKeystorePassword", "inboundTlsKeyAlias",
    "inboundAllowPlainHttp", "serverThreads", "maxConnections", "acceptQueueSize", "acceptors",
)

# Operations, verbs, types and body types, as InboundModels.kt names them.
OP_LIST, OP_READ, OP_CREATE, OP_UPDATE, OP_DELETE, OP_EXECUTE = "LIST", "READ", "CREATE", "UPDATE", "DELETE", "EXECUTE"
VERB_GET, VERB_POST, VERB_PUT, VERB_PATCH, VERB_DELETE = "GET", "POST", "PUT", "PATCH", "DELETE"
TYPE_STRING, TYPE_NUMBER, TYPE_BOOLEAN = "STRING", "NUMBER", "BOOLEAN"
BODY_NONE, BODY_JSON, BODY_FORM = "NONE", "JSON", "FORM_URLENCODED"
BODY_NEEDS_TYPE = "BODY bindings need a body type"
NOT_A_NUMBER = "is not a number"

# The verbs each operation fits (InboundEndpointValidator.VERBS_BY_OPERATION); a read-only routine takes GET too.
VERBS_BY_OPERATION = {
    OP_LIST: (VERB_GET,),
    OP_READ: (VERB_GET,),
    OP_CREATE: (VERB_POST,),
    OP_UPDATE: (VERB_PUT, VERB_PATCH),
    OP_DELETE: (VERB_DELETE,),
    OP_EXECUTE: (VERB_POST,),
}
READ_ONLY_ROUTINE_VERBS = (VERB_GET, VERB_POST)
OPERATION_BY_VERB = {VERB_POST: OP_CREATE, VERB_PUT: OP_UPDATE, VERB_PATCH: OP_UPDATE, VERB_DELETE: OP_DELETE}
READ_OPERATIONS = (OP_LIST, OP_READ)
WRITE_OPERATIONS = (OP_CREATE, OP_UPDATE, OP_DELETE)
KEYED_ROW_OPERATIONS = (OP_UPDATE, OP_DELETE)
VERBS_WITH_BODY = (VERB_POST, VERB_PUT, VERB_PATCH)
INBOUND_BODY_TYPES = (BODY_NONE, BODY_JSON, BODY_FORM)
BODY_TYPE_ALIASES = {"FORM": BODY_FORM}
ROUTINE_BODIES = ("ALL", "ROWS", "VALUE", "OUTPUT")
FORMAT_JSON, FORMAT_XML = "JSON", "XML"
ROUTINE_FORMATS = (FORMAT_JSON, FORMAT_XML)
MAX_ROUTINE_TIMEOUT_SECONDS = 3600
DEFAULT_ROUTINE_TIMEOUT_SECONDS = 30
MAX_ROWS_LIMIT = 10000
DEFAULT_MAX_ROWS = 200
ROW_OPERATIONS = ("Insert", "Update", "Delete")
SEQUENCE_COLUMN = "_seq"
# The column of an EXECUTE route's table that the routine's answer is mapped onto (CourierRoutineTarget.RESPONSE_COLUMN).
RESPONSE_COLUMN = "_response"
CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# One segment: the runtime routes match `/{basePath}/...` against the first segment of the request path only.
BASE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._~-]+$")
NUMBER_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
# What a binding's value must fit (CourierColumns, InboundValues): a write's or a read's column is VARCHAR(255) or
# NUMERIC(18,2); a routine's argument a number of at most 38 digits on either side of the point.
STRING_LENGTH = 255
NUMBER_SCALE = 2
NUMBER_INTEGER_DIGITS = 16
ROUTINE_DIGITS = 38
ROUTINE_KINDS = ("PROCEDURE", "FUNCTION")

FetchFn = Callable[..., Any]

_logger = logging.getLogger(__name__)


def is_http_source_agent(agent: Optional[Dict[str, Any]]) -> bool:
    """True when the configured agent is the HTTP Source (``courier-agent`` as SOURCE)."""
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
        "blockedTokenIds": _string_list(collection.get("blockedTokenIds")),
    }


def _placeholders(path: str) -> List[str]:
    segments = [s for s in path.strip("/").split("/") if s]
    return [s[1:-1] for s in segments if len(s) > 2 and s.startswith("{") and s.endswith("}")]


def _operation(endpoint: Dict[str, Any], verb: str, path: str) -> str:
    """The stored operation: explicit, else EXECUTE for a route that calls a routine, else what the verb means."""
    explicit = _text(endpoint.get("operation")).upper()
    if explicit:
        return explicit
    if endpoint.get("routine") is not None or endpoint.get("call") is not None:
        return OP_EXECUTE
    if verb == VERB_GET:
        return OP_READ if _placeholders(path) else OP_LIST
    return OPERATION_BY_VERB.get(verb, "")


def _body_type(endpoint: Dict[str, Any], verb: str, operation: str) -> str:
    raw = _text(endpoint.get("bodyType")).upper()
    if not raw:
        return BODY_JSON if verb in VERBS_WITH_BODY and operation not in READ_OPERATIONS else BODY_NONE
    return BODY_TYPE_ALIASES.get(raw, raw)


def normalize_response_field(field: Any) -> Dict[str, Any]:
    """``InboundResponseField``: name, type (STRING), description. A bare string is a STRING field of that name."""
    if isinstance(field, str):
        field = {"name": field}
    if not isinstance(field, dict):
        raise CourierConfigError(f"response field must be an object or a name, got {type(field).__name__}")
    return {
        "name": _text(field.get("name")),
        "type": _text(field.get("type")).upper() or TYPE_STRING,
        "description": field.get("description"),
    }


def normalize_routine_options(routine: Any) -> Dict[str, Any]:
    """``InboundRoutineOptions``: readOnly, timeoutSeconds, ownTransaction and the return type ``answer``."""
    routine = routine if isinstance(routine, dict) else {}
    answer = routine.get("answer") if isinstance(routine.get("answer"), dict) else {}
    timeout = routine.get("timeoutSeconds")
    return {
        "readOnly": bool(routine.get("readOnly", False)),
        "timeoutSeconds": DEFAULT_ROUTINE_TIMEOUT_SECONDS if timeout is None else int(timeout),
        "ownTransaction": bool(routine.get("ownTransaction", False)),
        "answer": {
            "body": _text(answer.get("body")).upper() or "ALL",
            "format": _text(answer.get("format")).upper() or FORMAT_JSON,
        },
    }


def normalize_inbound_endpoint(endpoint: Any, collection_id: str, existing_id: Optional[str] = None) -> Dict[str, Any]:
    """``InboundEndpoint`` payload of a route of the collection ``collection_id``: it names no pipeline or agent."""
    if not isinstance(endpoint, dict):
        raise CourierConfigError(f"endpoint must be an object, got {type(endpoint).__name__}")
    name = _text(endpoint.get("name"))
    verb = _text(endpoint.get("httpVerb", endpoint.get("method"))).upper() or VERB_POST
    path = _text(endpoint.get("path", endpoint.get("pathTemplate"))) or "/"
    operation = _operation(endpoint, verb, path)
    operation_binding = _text(endpoint.get("operationBinding")) or None
    max_rows = endpoint.get("maxRows")
    payload = {
        "id": existing_id or endpoint.get("id") or stable_id("inbound-endpoint", name, collection_id),
        "name": name,
        "collectionId": collection_id,
        "description": endpoint.get("description"),
        "path": path,
        "httpVerb": verb,
        "operation": operation,
        "maxRows": DEFAULT_MAX_ROWS if max_rows is None else int(max_rows),
        "bindings": [normalize_binding(b) for b in endpoint.get("bindings") or []],
        "keyBindings": _string_list(endpoint.get("keyBindings")),
        "operationBinding": operation_binding,
        "bodyType": _body_type(endpoint, verb, operation),
        "responseFields": [normalize_response_field(f) for f in endpoint.get("responseFields") or []],
    }
    if operation == OP_EXECUTE or endpoint.get("routine") is not None:
        payload["routine"] = normalize_routine_options(endpoint.get("routine"))
    return payload


# ---------------------------------------------------------------------------
# Validation (mirrors InboundEndpointValidator)
# ---------------------------------------------------------------------------

# Validation message shared by the collection, endpoint and token entries.
NAME_REQUIRED = "name is required"


def validate_inbound_collection(collection: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not collection.get("name"):
        errors.append(NAME_REQUIRED)
    if not BASE_PATH_PATTERN.match(collection.get("basePath") or ""):
        errors.append(
            f"basePath {collection.get('basePath')!r} is not valid: one segment of letters, digits, '.', '_', '~', '-', without '/'"
        )
    both = sorted(set(collection.get("allowedTokenIds") or []) & set(collection.get("blockedTokenIds") or []))
    if both:
        errors.append(f"a token cannot be both allowed and blocked: {', '.join(both)}")
    return [f"collection '{collection.get('name')}': {e}" for e in errors]


def _path_errors(path: str) -> List[str]:
    if not path.startswith("/"):
        return [f"path {path!r} must start with '/'"]
    return [
        f"placeholder in {segment!r} must be a whole path segment, like /{{id}}"
        for segment in (s for s in path.strip("/").split("/") if s)
        if ("{" in segment or "}" in segment) and not (len(segment) > 2 and segment[0] == "{" and segment[-1] == "}")
    ]


def _number_error(text: str, routine: bool) -> Optional[str]:
    """Why ``text`` is no value of a NUMBER binding (InboundValues.number, routineNumber); None when it fits."""
    if not NUMBER_PATTERN.match(text):
        return NOT_A_NUMBER
    try:
        value = Decimal(text).normalize()
    except InvalidOperation:
        return NOT_A_NUMBER
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # NaN or Infinity: the pattern above lets neither through
        return NOT_A_NUMBER
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if routine and (scale > ROUTINE_DIGITS or integer_digits > ROUTINE_DIGITS):
        return f"takes at most {ROUTINE_DIGITS} integer digits and {ROUTINE_DIGITS} decimal places"
    if not routine and scale > NUMBER_SCALE:
        return f"takes at most {NUMBER_SCALE} decimal places"
    if not routine and integer_digits > NUMBER_INTEGER_DIGITS:
        return f"takes at most {NUMBER_INTEGER_DIGITS} integer digits"
    return None


def _default_value_error(binding: Dict[str, Any], routine: bool = False) -> Optional[str]:
    default = binding.get("defaultValue")
    if default is None:
        return None
    name = binding.get("name")
    if binding.get("type") == TYPE_NUMBER:
        problem = _number_error(default.strip(), routine)
        return f"binding '{name}': defaultValue {default!r} {problem}" if problem else None
    if binding.get("type") == TYPE_BOOLEAN and default.strip().lower() not in ("true", "false"):
        return f"binding '{name}': defaultValue {default!r} is not true or false"
    if binding.get("type") == TYPE_STRING and not routine and len(default) > STRING_LENGTH:
        return f"binding '{name}': defaultValue is longer than {STRING_LENGTH} characters"
    return None


def _binding_name_error(name: str) -> Optional[str]:
    if not NAME_PATTERN.match(name):
        return f"binding name {name!r} is not valid"
    if name.lower() == SEQUENCE_COLUMN:
        return f"binding name '{name}' is reserved for the request sequence"
    if name.lower() == RESPONSE_COLUMN:
        return f"binding name '{name}' is reserved for the answer of a routine"
    return None


def _binding_errors(endpoint: Dict[str, Any], binding: Dict[str, Any]) -> List[str]:
    name = binding.get("name") or ""
    errors: List[str] = []
    name_error = _binding_name_error(name)
    if name_error:
        errors.append(name_error)
    if binding.get("location") not in BINDING_LOCATIONS:
        errors.append(f"binding {name!r}: location {binding.get('location')!r} is not one of {list(BINDING_LOCATIONS)}")
    if binding.get("type") not in BINDING_TYPES:
        errors.append(f"binding {name!r}: type {binding.get('type')!r} is not one of {list(BINDING_TYPES)}")
    location = binding.get("location")
    if location == "BODY" and endpoint.get("httpVerb") not in VERBS_WITH_BODY:
        errors.append(f"BODY binding '{name}' needs a verb with a body (POST, PUT or PATCH)")
    elif location == "HEADER" and not HEADER_NAME_PATTERN.match(name):
        errors.append(f"HEADER binding '{name}' is not a valid header name")
    elif location == "HEADER" and name.lower() in CREDENTIAL_HEADERS:
        errors.append(f"HEADER binding '{name}' would write the caller's credentials into the target")
    elif location == "PATH" and not binding.get("required"):
        errors.append(f"PATH binding '{name}' must be required")
    default_error = _default_value_error(binding, routine=endpoint.get("operation") == OP_EXECUTE)
    if default_error:
        errors.append(default_error)
    return errors


def _duplicate_errors(names: List[str], label: str) -> List[str]:
    seen: Dict[str, str] = {}
    errors: List[str] = []
    for name in names:
        if name.lower() in seen:
            errors.append(f"{label} '{name}' is declared more than once (names are compared ignoring case)")
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


def _write_errors(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> List[str]:
    """A write has no table: its bindings are the row (InboundEndpointValidator.writeErrors)."""
    errors: List[str] = []
    if not bindings:
        errors.append(f"{endpoint.get('operation')} needs at least one binding: the bindings are the columns of its row")
    if endpoint.get("responseFields"):
        errors.append(f"{endpoint.get('operation')} answers no field: response fields are a read's")
    for binding in bindings:
        errors.extend(_binding_errors(endpoint, binding))
    by_name = {b.get("name"): b for b in bindings}
    errors.extend(_key_binding_errors(endpoint, by_name))
    errors.extend(_operation_binding_errors(endpoint, by_name))
    if not _key_names(endpoint) and (endpoint.get("operation") in KEYED_ROW_OPERATIONS or endpoint.get("operationBinding")):
        errors.append(
            "needs a key to find the row (a key binding or a required PATH binding): without one every request "
            f"gets a new '{SEQUENCE_COLUMN}' key and its updates and deletes change nothing"
        )
    if _body_without_type(endpoint, bindings):
        errors.append(BODY_NEEDS_TYPE)
    return errors


def _body_without_type(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> bool:
    return endpoint.get("bodyType") == BODY_NONE and any(b.get("location") == "BODY" for b in bindings)


def _key_binding_errors(endpoint: Dict[str, Any], by_name: Dict[Any, Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    for key in endpoint.get("keyBindings") or []:
        if key not in by_name:
            errors.append(f"key binding '{key}' is not declared")
        elif not by_name[key].get("required"):
            errors.append(f"key binding '{key}' must be required")
    return errors


def _operation_binding_errors(endpoint: Dict[str, Any], by_name: Dict[Any, Dict[str, Any]]) -> List[str]:
    operation_binding = endpoint.get("operationBinding")
    if operation_binding is None:
        return []
    errors: List[str] = []
    if operation_binding not in by_name:
        errors.append(f"operationBinding '{operation_binding}' is not declared")
    elif by_name[operation_binding].get("type") != TYPE_STRING:
        errors.append(f"operationBinding '{operation_binding}' must be a STRING binding")
    if endpoint.get("httpVerb") == VERB_PATCH:
        errors.append("a PATCH updates the columns its request carries: it takes no operation binding")
    return errors


def _response_field_errors(endpoint: Dict[str, Any]) -> List[str]:
    fields = endpoint.get("responseFields") or []
    errors: List[str] = []
    if not fields:
        errors.append(f"{endpoint.get('operation')} needs at least one response field: the fields its answer carries")
    errors.extend(_duplicate_errors([f.get("name") or "" for f in fields], "response field"))
    for field in fields:
        errors.extend(_one_response_field_errors(endpoint, field))
    return errors


def _one_response_field_errors(endpoint: Dict[str, Any], field: Dict[str, Any]) -> List[str]:
    name = field.get("name") or ""
    errors: List[str] = []
    if not NAME_PATTERN.match(name):
        errors.append(f"response field name {name!r} is not valid")
    elif name.lower() == SEQUENCE_COLUMN:
        errors.append(f"response field '{name}' is reserved for the request sequence")
    if field.get("type") not in BINDING_TYPES:
        errors.append(f"response field {name!r}: type {field.get('type')!r} is not one of {list(BINDING_TYPES)}")
    same = next((b for b in endpoint.get("bindings") or [] if (b.get("name") or "").lower() == name.lower()), None)
    if same is None:
        return errors
    if same.get("name") != name:
        errors.append(f"filter '{same.get('name')}' and response field '{name}' are one column: spell them the same way")
    elif same.get("type") != field.get("type"):
        errors.append(f"filter '{name}' is a {same.get('type')} and its response field a {field.get('type')}: one column has one type")
    return errors


def _read_errors(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> List[str]:
    """A read is a table of its filters (PATH and QUERY bindings) and response fields (InboundEndpointValidator.readErrors)."""
    operation = endpoint.get("operation")
    errors: List[str] = []
    for binding in bindings:
        name = binding.get("name")
        if binding.get("location") == "QUERY" and operation == OP_READ:
            errors.append(f"READ finds its row by the path: '{name}' is a QUERY binding, which only a LIST filters on")
        elif binding.get("location") in ("PATH", "QUERY"):
            errors.extend(_binding_errors(endpoint, binding))
        else:
            errors.append(f"{operation} reads PATH and QUERY bindings only: '{name}' is a {binding.get('location')} one")
    if endpoint.get("operationBinding") is not None:
        errors.append(f"{operation} writes no row: it takes no operation binding")
    errors.extend(_response_field_errors(endpoint))
    columns = {b.get("name") for b in bindings} | {f.get("name") for f in endpoint.get("responseFields") or []}
    errors.extend(f"key '{k}' is neither a filter nor a response field of {operation}" for k in endpoint.get("keyBindings") or []
                  if k not in columns)
    if not _key_names(endpoint):
        errors.append(f"{operation} needs a key: choose the filters or response fields that identify a row (keyBindings)")
    placeholders = _placeholders(endpoint.get("path") or "")
    if operation == OP_READ and not placeholders:
        errors.append("READ needs a path placeholder for the key, like /{id}")
    if operation == OP_LIST and placeholders:
        errors.append("LIST takes no path placeholder: filter it with QUERY bindings")
    return errors


def _routine_errors(endpoint: Dict[str, Any], bindings: List[Dict[str, Any]]) -> List[str]:
    """An EXECUTE declares its bindings and the options of the call (InboundEndpointValidator.routineErrors)."""
    errors: List[str] = []
    if endpoint.get("bodyType") == BODY_FORM:
        errors.append("a routine endpoint reads a JSON body: a form body is not available")
    if _body_without_type(endpoint, bindings):
        errors.append(BODY_NEEDS_TYPE)
    if endpoint.get("responseFields"):
        errors.append("EXECUTE answers what its routine returns: response fields are a read's")
    if endpoint.get("keyBindings") or endpoint.get("operationBinding") is not None:
        errors.append("a routine endpoint takes no key or operation binding")
    for binding in bindings:
        if binding.get("location") == "BODY" and "." in (binding.get("name") or ""):
            errors.append(f"BODY binding '{binding.get('name')}' reads a nested value, which a routine endpoint does not")
        else:
            errors.extend(_binding_errors(endpoint, binding))
    return errors + _routine_option_errors(endpoint.get("routine") or {})


def _routine_option_errors(options: Dict[str, Any]) -> List[str]:
    answer = options.get("answer") or {}
    errors: List[str] = []
    if options.get("readOnly") and options.get("ownTransaction"):
        errors.append("a read-only routine runs in a transaction rolled back at the end: it cannot also commit by itself")
    if not 1 <= int(options.get("timeoutSeconds") or 0) <= MAX_ROUTINE_TIMEOUT_SECONDS:
        errors.append(f"routine.timeoutSeconds must be between 1 and {MAX_ROUTINE_TIMEOUT_SECONDS}")
    if answer.get("body") not in ROUTINE_BODIES:
        errors.append(f"routine.answer.body {answer.get('body')!r} is not one of {list(ROUTINE_BODIES)}")
    if answer.get("format") not in ROUTINE_FORMATS:
        errors.append(f"routine.answer.format {answer.get('format')!r} is not one of {list(ROUTINE_FORMATS)}")
    return errors


def _verb_errors(endpoint: Dict[str, Any]) -> List[str]:
    verb = endpoint.get("httpVerb")
    operation = endpoint.get("operation")
    if operation not in VERBS_BY_OPERATION:
        return [f"operation {operation!r} is not one of {list(VERBS_BY_OPERATION)}"]
    routine = endpoint.get("routine") or {}
    allowed = READ_ONLY_ROUTINE_VERBS if operation == OP_EXECUTE and routine.get("readOnly") else VERBS_BY_OPERATION[operation]
    if verb not in allowed:
        return [f"httpVerb {verb} does not fit operation {operation} (allowed: {', '.join(allowed)})"]
    return []


def validate_inbound_endpoint(endpoint: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if not endpoint.get("name"):
        errors.append(NAME_REQUIRED)
    errors.extend(_verb_errors(endpoint))
    if endpoint.get("bodyType") not in INBOUND_BODY_TYPES:
        errors.append(f"bodyType {endpoint.get('bodyType')!r} is not one of {list(INBOUND_BODY_TYPES)}")
    if not 1 <= int(endpoint.get("maxRows") or 0) <= MAX_ROWS_LIMIT:
        errors.append(f"maxRows must be between 1 and {MAX_ROWS_LIMIT}")
    bindings = endpoint.get("bindings") or []
    path_errors = _path_errors(endpoint.get("path") or "")
    errors.extend(path_errors)
    errors.extend(_duplicate_errors([b.get("name") or "" for b in bindings], "binding"))
    operation = endpoint.get("operation")
    if operation == OP_EXECUTE:
        errors.extend(_routine_errors(endpoint, bindings))
    elif operation in READ_OPERATIONS:
        errors.extend(_read_errors(endpoint, bindings))
    elif operation in WRITE_OPERATIONS:
        errors.extend(_write_errors(endpoint, bindings))
    if operation != OP_EXECUTE and endpoint.get("routine") is not None:
        errors.append("only an EXECUTE endpoint calls a routine")
    if not path_errors:
        errors.extend(_placeholder_errors(endpoint, bindings))
    return [f"endpoint '{endpoint.get('name')}': {e}" for e in errors]


def _route_shape(endpoint: Dict[str, Any]) -> tuple:
    segments = [s for s in (endpoint.get("path") or "").strip("/").split("/") if s]
    return endpoint.get("httpVerb"), tuple("{}" if s.startswith("{") and s.endswith("}") else s for s in segments)


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


def _by_id(items: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {str(i["id"]): i for i in items if isinstance(i, dict) and i.get("id") is not None}


def _call_errors(endpoint: Dict[str, Any], call: Any) -> List[str]:
    """What is wrong with the ``call`` of a route (the routine its entity calls, see courier_routine_entities), checked
    before anything is written: only an EXECUTE route has one, naming the routine's schema and name."""
    if call is None:
        return []
    prefix = f"endpoint '{endpoint.get('name')}': call"
    if not isinstance(call, dict):
        return [f"{prefix} must be an object"]
    errors: List[str] = []
    if endpoint.get("operation") != OP_EXECUTE:
        errors.append(f"{prefix}: only an EXECUTE route calls a routine (operation {endpoint.get('operation')})")
    errors.extend(f"{prefix} needs the routine's '{key}'" for key in ("schema", "name") if not _text(call.get(key)))
    kind = _text(call.get("kind")).upper()
    if kind and kind not in ROUTINE_KINDS:
        errors.append(f"{prefix}.kind {call.get('kind')!r} is not one of {list(ROUTINE_KINDS)}")
    if call.get("parameters") is not None and not isinstance(call.get("parameters"), dict):
        errors.append(f"{prefix}.parameters must map each parameter to a binding name or null")
    return errors


def _handle(value: Any) -> str:
    """A collection's id, name or base path as an HTTP Source's inboundCollection resolves it: trimmed, no slashes."""
    return _text(value).strip("/")


def _handle_clash_errors(collection: Dict[str, Any], stored_collections: Any) -> List[str]:
    """InboundEndpointValidator.validateCollection: no handle of this collection is another stored collection's."""
    others = [c for c in stored_collections or [] if isinstance(c, dict) and c.get("id") != collection["id"]]
    mine = {_handle(collection["name"]), _handle(collection["basePath"]), _handle(collection["id"])} - {""}
    return [
        f"collection '{collection['name']}': its name, basePath or id is a handle of stored collection '{other.get('name')}'"
        for other in others
        if mine & ({_handle(other.get("name")), _handle(other.get("basePath")), _handle(other.get("id"))} - {""})
    ]


def _plan_inbound_endpoints(collection_cfg: Dict[str, Any], collection: Dict[str, Any], stored_endpoints: List[Dict[str, Any]],
                            errors: List[str]) -> List[Dict[str, Any]]:
    endpoints: List[Dict[str, Any]] = []
    names: set = set()
    shapes: Dict[tuple, str] = {}
    stored_by_name, stored_by_id = _by_name(stored_endpoints), _by_id(stored_endpoints)
    for endpoint_cfg in collection_cfg.get("endpoints") or []:
        cfg = endpoint_cfg if isinstance(endpoint_cfg, dict) else {}
        # By name, else by the id the block carries (an export keeps ids; the endpoint may have been renamed in the UI).
        stored = stored_by_name.get(_text(cfg.get("name"))) or stored_by_id.get(_text(cfg.get("id")))
        endpoint = normalize_inbound_endpoint(endpoint_cfg, collection["id"], existing_id=stored.get("id") if stored else None)
        if endpoint["name"] in names:
            errors.append(f"endpoint '{endpoint['name']}' of collection '{collection['name']}' is declared twice")
        names.add(endpoint["name"])
        shape = _route_shape(endpoint)
        if shape in shapes:
            errors.append(f"endpoint '{endpoint['name']}': {endpoint['httpVerb']} {endpoint['path']} conflicts with "
                          f"endpoint '{shapes[shape]}' of collection '{collection['name']}'")
        shapes[shape] = endpoint["name"]
        errors.extend(validate_inbound_endpoint(endpoint))
        errors.extend(_call_errors(endpoint, cfg.get("call")))
        endpoints.append({"definition": endpoint, "exists": stored is not None, "call": cfg.get("call")})
    if not endpoints:
        errors.append(f"collection '{collection['name']}' declares no endpoint")
    return endpoints


def _plan_collections(collections_cfg: List[Any], existing_collections: Any,
                      existing_endpoints_for: Optional[Callable[[str], Any]], errors: List[str]) -> List[Dict[str, Any]]:
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
        errors.extend(_handle_clash_errors(collection, existing_collections))
        plan.append(_plan_collection_item(collection_cfg, collection, stored, existing_endpoints_for, errors))
    return plan


def _plan_collection_item(collection_cfg: Dict[str, Any], collection: Dict[str, Any], stored: Optional[Dict[str, Any]],
                          existing_endpoints_for: Optional[Callable[[str], Any]], errors: List[str]) -> Dict[str, Any]:
    """One planned collection: its routes, whether Core Hub stores it, and the HTTP Source Core Hub reports holding it."""
    stored_endpoints = existing_endpoints_for(collection["id"]) if (existing_endpoints_for and stored) else []
    endpoints = _plan_inbound_endpoints(collection_cfg, collection, stored_endpoints or [], errors)
    owner = stored.get("owner") if stored else None
    return {"collection": collection, "exists": stored is not None, "endpoints": endpoints,
            "storedOwner": owner if isinstance(owner, dict) else None}


def _attached_collection(collections: List[Dict[str, Any]], inbound_collection: str, errors: List[str]) -> Optional[str]:
    """The name of the collection the HTTP Source serves: the one ``inboundCollection`` names (by name, base path or
    id, as the agent resolves it), else the only one of the block."""
    if inbound_collection:
        handle = inbound_collection.strip().strip("/")
        for item in collections:
            collection = item["collection"]
            if handle in (collection["name"], collection["basePath"], collection["id"]):
                return collection["name"]
        # Not one of the block's: an existing collection of Core Hub, which Core Hub checks when the agent is configured.
        return None
    if len(collections) == 1:
        return collections[0]["collection"]["name"]
    errors.append(
        "an HTTP Source serves one collection: with several in 'courier.collections', name the one it serves in "
        f"customHostCredentials.{INBOUND_COLLECTION_KEY} (the others are created attached to nothing)"
    )
    return None


def plan_inbound_definitions(courier_cfg: Any, existing_collections: Any = None,
                             existing_endpoints_for: Optional[Callable[[str], Any]] = None,
                             inbound_collection: str = "") -> Dict[str, Any]:
    """Normalised and validated definitions: ``{"collections": [...], "tokens": [...], "force": bool, "attached": name}``.

    Pure: no CoreHub call. ``existing_collections`` is what ``GET /api-agent/inbound/collections``
    returned and ``existing_endpoints_for(collection_id)`` what ``GET /api-agent/inbound/endpoints``
    returns for it, so that stored definitions keep their id and are updated instead of duplicated.
    ``inbound_collection`` is the agent's ``inboundCollection`` setting, when the configuration sets it.
    """
    if not isinstance(courier_cfg, dict):
        raise CourierConfigError("'courier' must be an object with a 'collections' list")
    collections_cfg = courier_cfg.get("collections")
    if not isinstance(collections_cfg, list) or not collections_cfg:
        raise CourierConfigError("'courier.collections' must be a non-empty list")

    errors: List[str] = []
    collections = _plan_collections(collections_cfg, existing_collections, existing_endpoints_for, errors)
    attached = _attached_collection(collections, _text(inbound_collection), errors)
    collection_names = [c["collection"]["name"] for c in collections]
    tokens: List[Dict[str, Any]] = []
    for token_cfg in courier_cfg.get("tokens") or []:
        token = normalize_token(token_cfg, collection_names)
        errors.extend(validate_token(token, collection_names))
        tokens.append(token)
    for item in collections:
        if item["collection"]["name"] != attached:
            calls = [e["definition"]["name"] for e in item["endpoints"] if e.get("call")]
            if calls:
                errors.append(f"collection '{item['collection']['name']}' is not the one the HTTP Source serves: its routes "
                              f"{', '.join(calls)} cannot have an entity of this pipeline ('call')")
    if errors:
        raise CourierConfigError("Invalid 'courier' configuration of the HTTP Source:\n  - " + "\n  - ".join(errors))
    return {"collections": collections, "tokens": tokens, "force": bool(courier_cfg.get("force", False)), "attached": attached}


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


def _create_token(call: FetchFn, auth: str, body: Dict[str, Any], log: logging.Logger) -> Any:
    """POSTs a token with collection scopes, and warns when the Core Hub did not keep them.

    A Core Hub older than token collection scopes (gluesync-kotlin !2954) ignores the unknown field (its JSON reads
    with ignoreUnknownKeys) and answers without one: the token is then not narrowed to its collections, and the
    allowlist of those collections is all that keeps it out of the others. A refusal is never retried without the
    scopes: on a Core Hub that has them, a 400 is a real error, and dropping the scopes would widen the token.
    """
    created = call(API_TOKENS_PATH, method=VERB_POST, token=auth, body=body)
    if isinstance(created, dict) and COLLECTION_SCOPES_KEY not in created:
        log.warning(
            "This Core Hub does not scope Personal API Tokens to collections: token '%s' can also call the inbound "
            "collections whose allowlist is off. Turn the allowlist on for those, or upgrade Core Hub.", body["name"]
        )
    return created


def _rotate_token(call: FetchFn, auth: str, token: Dict[str, Any], existing_tokens: Any, pipeline_id: str,
                  agent_id: str, collection_ids: List[str], log: logging.Logger) -> str:
    for stale in existing_tokens if isinstance(existing_tokens, list) else []:
        if isinstance(stale, dict) and stale.get("name") == token["name"] and stale.get("active"):
            log.info("Revoking the previous Personal API Token '%s' (id=%s)", token["name"], stale.get("id"))
            call(f"{API_TOKENS_PATH}/{stale['id']}/revoke", method=VERB_POST, token=auth)
    created = _create_token(call, auth, {
        "name": token["name"],
        "expiresInDays": token["expiresInDays"],
        "scopes": [{"pipelineId": pipeline_id, "agentId": agent_id}],
        "permissionScopes": [HTTPS_AGENT_PERMISSION],
        # The collections it is for, and no other one.
        COLLECTION_SCOPES_KEY: collection_ids,
    }, log)
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


def _upsert_endpoint(call: FetchFn, auth: str, item: Dict[str, Any], force: bool, log: logging.Logger) -> None:
    endpoint = item["definition"]
    log.info("%s inbound route '%s' (%s %s, %s)", "Updating" if item["exists"] else "Creating",
             endpoint["name"], endpoint["httpVerb"], endpoint["path"], endpoint["operation"])
    try:
        # A create whose id CoreHub already stores (409) is repeated as an update, with the same force.
        save_definition(call, auth, INBOUND_ENDPOINTS_PATH, endpoint["id"], endpoint, item["exists"],
                        params={"force": "true"} if force else None)
    except Exception as error:
        if status_of(error) == 409:
            raise CourierConfigError(
                f"Inbound route '{endpoint['name']}' has entities built on it and CoreHub refused to change it: "
                f"{error_text(error)}. Set \"force\": true in the 'courier' block to change it anyway (those entities break)."
            ) from error
        raise


def _rotate_tokens(call: FetchFn, token: str, token_defs: List[Dict[str, Any]], pipeline_id: str, agent_id: str,
                   collection_ids_by_name: Dict[str, str], log: logging.Logger) -> Dict[str, List[str]]:
    """Rotates the configured tokens, each scoped to the collections it is for, and returns their ids by the name
    of each of those collections."""
    token_ids_by_collection: Dict[str, List[str]] = {}
    if not token_defs:
        return token_ids_by_collection
    existing_tokens = call(API_TOKENS_PATH, method=VERB_GET, token=token)
    for token_def in token_defs:
        collection_ids = [collection_ids_by_name[name] for name in token_def["collections"]]
        token_id = _rotate_token(call, token, token_def, existing_tokens, pipeline_id, agent_id, collection_ids, log)
        for name in token_def["collections"]:
            token_ids_by_collection.setdefault(name, []).append(token_id)
    return token_ids_by_collection


def _upsert_collection(call: FetchFn, token: str, item: Dict[str, Any], token_ids_by_collection: Dict[str, List[str]],
                       log: logging.Logger) -> str:
    """Creates or updates one planned inbound collection, its allowlist joined by the tokens for it; returns its name."""
    collection = dict(item["collection"])
    collection["allowedTokenIds"] = collection["allowedTokenIds"] + token_ids_by_collection.get(collection["name"], [])
    log.info("%s inbound collection '%s' (basePath=%s)", "Updating" if item["exists"] else "Creating",
             collection["name"], collection["basePath"])
    try:
        save_definition(call, token, INBOUND_COLLECTIONS_PATH, collection["id"], collection, item["exists"])
    except Exception as error:
        if status_of(error) == 400 and MISSING_TOKEN_CODE in error_text(error):
            raise CourierConfigError(
                f"Inbound collection '{collection['name']}' was refused: CoreHub saves a collection only while an active "
                f"Personal API Token with the '{HTTPS_AGENT_PERMISSION}' permission scope, admitted by its allowlist and "
                "blocklist, can call it. Add one to 'courier.tokens' for this collection."
            ) from error
        raise
    if collection["allowlistEnabled"] and not collection["allowedTokenIds"]:
        log.warning("Inbound collection '%s' has its allowlist on and no token on it: every request is refused", collection["name"])
    return collection["name"]


def _check_attached_elsewhere(plan: Dict[str, Any], pipeline_id: str) -> None:
    """A collection is served by one HTTP Source: Core Hub refuses an HTTP Source whose collection another one holds.

    Core Hub reports a collection's ``owner`` only for a pipeline it knows, so an owner other than this pipeline is a
    live one, and its clients' tokens are those this run would revoke: refused before anything is written, whether a
    bootstrapper run (a run that failed earlier leaves its pipeline holding the collection) or a restore.
    """
    attached = next((i for i in plan["collections"] if i["collection"]["name"] == plan["attached"]), None)
    owner = attached.get("storedOwner") if attached else None
    if not owner or str(owner.get("pipelineId")) == str(pipeline_id):
        return
    raise CourierConfigError(
        f"Inbound collection '{plan['attached']}' is served by HTTP Source {owner.get('agentId')} of pipeline "
        f"{owner.get('pipelineId')}, and Core Hub attaches a collection to one HTTP Source only. Delete that pipeline "
        "(a run that failed earlier leaves it behind), or give the collection of the 'courier' block another name and "
        "basePath."
    )


def provision_inbound(token: str, courier_cfg: Dict[str, Any], pipeline_id: str, agent_id: str,
                      fetch: Optional[FetchFn] = None, logger: Optional[logging.Logger] = None,
                      inbound_collection: str = "") -> Dict[str, Any]:
    """Creates or updates the inbound collections, routes and tokens of an HTTP Source in CoreHub.

    Tokens come first so that their ids can join the allowlist of the collections they are for. Returns the plan
    (``collections``, ``attached``: the name of the collection the HTTP Source serves). Raises ``CourierConfigError`` on
    an invalid configuration, and whatever ``fetch`` raises on a CoreHub failure. A collection another live pipeline's
    HTTP Source holds is refused before anything is written (see ``_check_attached_elsewhere``), on a restore as on a run.
    """
    log = logger or _logger
    call = fetch or _default_fetch()

    existing_collections = call(INBOUND_COLLECTIONS_PATH, method=VERB_GET, token=token)

    def existing_endpoints_for(collection_id: str) -> Any:
        return call(INBOUND_ENDPOINTS_PATH, method=VERB_GET, token=token, params={"collectionId": collection_id})

    plan = plan_inbound_definitions(courier_cfg, existing_collections, existing_endpoints_for, inbound_collection)
    _check_attached_elsewhere(plan, pipeline_id)

    collection_ids_by_name = {item["collection"]["name"]: item["collection"]["id"] for item in plan["collections"]}
    token_ids_by_collection = _rotate_tokens(call, token, plan["tokens"], pipeline_id, agent_id, collection_ids_by_name, log)

    for item in plan["collections"]:
        _upsert_collection(call, token, item, token_ids_by_collection, log)
        for endpoint_item in item["endpoints"]:
            _upsert_endpoint(call, token, endpoint_item, plan["force"], log)
    return plan


def apply_inbound_defaults(agent: Dict[str, Any], attached: Optional[str],
                           logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """Sets ``customHostCredentials.inboundCollection`` to the collection the HTTP Source serves, when the configuration
    does not name one, and drops the listener settings of Core Hub 2.3.2.1 and older, which the agent no longer reads."""
    log = logger or _logger
    custom = agent.get("customHostCredentials")
    if not isinstance(custom, dict):
        custom = {}
        agent["customHostCredentials"] = custom
    if not _text(custom.get(INBOUND_COLLECTION_KEY)) and attached:
        custom[INBOUND_COLLECTION_KEY] = attached
        log.info("HTTP Source inbound collection defaulted to '%s'", attached)
    stale = [key for key in LISTENER_KEYS if key in custom]
    for key in stale:
        custom.pop(key)
    if stale:
        log.warning("HTTP Source %s: dropped %s, read by Core Hub 2.3.2.1 and older only: Core Hub serves every inbound "
                    "route on its own inbound server (GLUESYNC_COURIER_PORT)", agent.get("agentId"), ", ".join(stale))
    return agent


def routine_calls(agent: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The EXECUTE routes of an HTTP Source entry whose entity the bootstrapper builds (``call``), each
    ``{"collection": name, "route": endpoint config, "call": call config}``, in configuration order."""
    block = agent.get("courier") if is_http_source_agent(agent) else None
    if not isinstance(block, dict):
        return []
    return [
        {"collection": _text(collection.get("name")), "route": endpoint, "call": endpoint["call"]}
        for collection in block.get("collections") or [] if isinstance(collection, dict)
        for endpoint in collection.get("endpoints") or [] if isinstance(endpoint, dict) and endpoint.get("call")
    ]


def provision_courier_agent(token: str, pipeline_id: str, agent: Dict[str, Any], fetch: Optional[FetchFn] = None,
                            logger: Optional[logging.Logger] = None) -> Optional[Dict[str, Any]]:
    """Creates the Courier definitions of an HTTP Source or HTTP Target agent entry and fills its defaults.

    The agent discovers them (collection = schema, route or endpoint = table, binding = column), so they must
    exist before it is configured:

    - HTTP Source (SOURCE): inbound collections and routes, the Personal API Tokens clients call them with, then
      ``inboundCollection``, the collection it serves;
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
        custom = agent.get("customHostCredentials") if isinstance(agent.get("customHostCredentials"), dict) else {}
        plan = provision_inbound(token, agent["courier"], pipeline_id, agent["agentId"], fetch=fetch, logger=log,
                                 inbound_collection=_text(custom.get(INBOUND_COLLECTION_KEY)))
        apply_inbound_defaults(agent, plan["attached"], logger=log)
    else:
        names = provision_courier(token, agent["courier"], fetch=fetch, logger=log)
        apply_courier_defaults(agent, names, logger=log)
    return agent["customHostCredentials"]

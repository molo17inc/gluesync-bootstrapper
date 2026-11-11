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

"""Exports the Courier definitions of an HTTP Source or HTTP Target as the ``courier`` block of its agent.

The block has the shape ``config.json`` takes (see ``courier_support`` and ``courier_inbound_support``), so an
export can be pasted into a configuration, or restored by the bootstrapper and the Automator, which provision it
as they provision a hand-written one:

- HTTP Source (``courier-agent`` as SOURCE): the inbound collections that hold a pipeline endpoint of the agent,
  with those endpoints only, their allowlist and blocklist;
- HTTP Target (``courier-agent`` as TARGET): the outbound collection the agent sends to
  (``customHostCredentials.outboundCollection``), or every outbound collection when it names none, with their
  endpoints.

Ids are kept, so a restore updates the same definitions instead of creating new ones. What is not exported:

- Personal API Tokens (``courier.tokens``): a token is a credential. Restoring one would create a new token and
  revoke the one the clients use; the tokens already on an allowlist keep working on the Core Hub they were made
  on. On another Core Hub, add a ``tokens`` entry to the block (or create a token) before restoring a collection.
- The secret of an outbound collection's auth: Core Hub never returns it. A restore onto the Core Hub that stores
  the collection keeps the stored secret; onto another one the secret has to be entered in the block.
- Header values of outbound endpoints, unless ``include_secrets`` (a SUPER_ADMIN only): Core Hub masks them, and a
  restore sends the mask back, which keeps the stored value.

Command line, for every Courier agent of a pipeline (JSON on stdout, ``{agentType:agentTag:agentId: block}``)::

    python courier_export.py --pipeline <pipeline id> [--include-secrets]

Connection: CORE_HUB_URL, SSL_ENABLED, SSL_SKIP_VERIFY as for the Bootstrapper; the token comes from --token, else
COURIER_EXPORT_TOKEN, else a login as admin with DEFAULT_PASSWORD.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from courier_support import (
    COLLECTIONS_PATH,
    ENDPOINTS_PATH,
    OUTBOUND_COLLECTION_KEY,
    is_courier_agent,
    status_of,
)
from courier_inbound_support import (
    INBOUND_COLLECTIONS_PATH,
    INBOUND_ENDPOINTS_PATH,
    is_http_source_agent,
)

FetchFn = Callable[..., Any]

TOKEN_ENV = "COURIER_EXPORT_TOKEN"

_logger = logging.getLogger(__name__)

# Binding fields as config.json writes them (normalize_binding reads the same names).
_BINDING_FIELDS = ("name", "location", "type", "required", "defaultValue", "description")
# Auth fields that are not secrets (the secret one Core Hub never returns).
_AUTH_PUBLIC_FIELDS = {"Basic": ("username",), "Bearer": (), "ApiKey": ("location", "name"), "None": ()}


def _list(response: Any) -> List[Dict[str, Any]]:
    return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []


def _without_empty(item: Dict[str, Any]) -> Dict[str, Any]:
    """Drops the fields that only repeat a default (None, an empty list or text), so that the block reads like a config."""
    return {k: v for k, v in item.items() if v is not None and v != [] and v != ""}


def _binding(binding: Dict[str, Any]) -> Dict[str, Any]:
    exported = {k: binding.get(k) for k in _BINDING_FIELDS}
    if exported.get("required") is True:
        exported.pop("required")  # the default
    return _without_empty(exported)


def _collections(call: FetchFn, token: str, path: str) -> Optional[List[Dict[str, Any]]]:
    """The collections at ``path``; None on a Core Hub that has no such route (404): it predates that Courier side."""
    try:
        return _list(call(path, method="GET", token=token))
    except Exception as error:
        if status_of(error) != 404:
            raise
        _logger.warning("This Core Hub has no %s: its Courier definitions are not exported", path)
        return None


def custom_credentials_of(agent: Dict[str, Any]) -> Dict[str, Any]:
    """Custom host credentials of an agent, as Core Hub reports them or as a config names them."""
    custom = agent.get("customHostCredentials") or agent.get("hostCredentialsCustomProperties")
    return custom if isinstance(custom, dict) else {}


# ---------------------------------------------------------------------------
# HTTP Source
# ---------------------------------------------------------------------------

def _inbound_endpoint(endpoint: Dict[str, Any]) -> Dict[str, Any]:
    return _without_empty({
        "id": endpoint.get("id"),
        "name": endpoint.get("name"),
        "description": endpoint.get("description"),
        "httpVerb": endpoint.get("httpVerb"),
        "path": endpoint.get("path"),
        "operation": endpoint.get("operation"),
        "bodyType": endpoint.get("bodyType"),
        "operationBinding": endpoint.get("operationBinding"),
        "keyBindings": endpoint.get("keyBindings"),
        "bindings": [_binding(b) for b in _list(endpoint.get("bindings"))],
    })


def _inbound_collection(collection: Dict[str, Any], endpoints: List[Dict[str, Any]]) -> Dict[str, Any]:
    exported = _without_empty({
        "id": collection.get("id"),
        "name": collection.get("name"),
        "description": collection.get("description"),
        "basePath": collection.get("basePath"),
        "tags": collection.get("tags"),
        "allowedTokenIds": collection.get("allowedTokenIds"),
        "blockedTokenIds": collection.get("blockedTokenIds"),
    })
    exported["allowlist"] = bool(collection.get("allowlistEnabled"))
    exported["endpoints"] = endpoints
    return exported


def export_inbound_block(call: FetchFn, token: str, pipeline_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    """The inbound collections holding a pipeline endpoint of HTTP Source ``agent_id``, with those endpoints; None if none."""
    stored_collections = _collections(call, token, INBOUND_COLLECTIONS_PATH)
    if stored_collections is None:
        return None
    collections: List[Dict[str, Any]] = []
    for collection in stored_collections:
        stored = _list(call(INBOUND_ENDPOINTS_PATH, method="GET", token=token, params={"collectionId": collection.get("id")}))
        endpoints = [
            _inbound_endpoint(e) for e in stored
            if str(e.get("pipelineId")) == str(pipeline_id) and str(e.get("agentId")) == str(agent_id)
        ]
        if endpoints:
            collections.append(_inbound_collection(collection, endpoints))
    return {"collections": collections} if collections else None


# ---------------------------------------------------------------------------
# HTTP Target
# ---------------------------------------------------------------------------

def _auth(auth: Any) -> Dict[str, Any]:
    if not isinstance(auth, dict) or not auth.get("type"):
        return {"type": "None"}
    auth_type = str(auth["type"])
    return {"type": auth_type, **{k: auth.get(k) for k in _AUTH_PUBLIC_FIELDS.get(auth_type, ()) if auth.get(k) is not None}}


def _outbound_endpoint(endpoint: Dict[str, Any]) -> Dict[str, Any]:
    return _without_empty({
        "id": endpoint.get("id"),
        "name": endpoint.get("name"),
        "description": endpoint.get("description"),
        "httpVerb": endpoint.get("httpVerb"),
        "pathTemplate": endpoint.get("pathTemplate"),
        "bodyType": endpoint.get("bodyType"),
        "operationField": endpoint.get("operationField"),
        "headers": [
            _without_empty({"name": h.get("name"), "valueTemplate": h.get("valueTemplate"), "description": h.get("description")})
            for h in _list(endpoint.get("headers"))
        ],
        "bindings": [_binding(b) for b in _list(endpoint.get("bindings"))],
    })


def export_outbound_block(call: FetchFn, token: str, agent: Dict[str, Any], include_secrets: bool = False) -> Optional[Dict[str, Any]]:
    """The outbound collection the HTTP Target sends to (every one when it names none), with its endpoints; None if none."""
    wanted = str(custom_credentials_of(agent).get(OUTBOUND_COLLECTION_KEY) or "").strip()
    params_extra = {"include_secrets": "true"} if include_secrets else {}
    stored_collections = _collections(call, token, COLLECTIONS_PATH)
    if stored_collections is None:
        return None
    collections: List[Dict[str, Any]] = []
    for collection in stored_collections:
        if wanted and wanted not in (collection.get("name"), collection.get("id")):
            continue
        stored = _list(call(ENDPOINTS_PATH, method="GET", token=token,
                            params={"collectionId": collection.get("id"), **params_extra}))
        exported = _without_empty({
            "id": collection.get("id"),
            "name": collection.get("name"),
            "description": collection.get("description"),
            "tags": collection.get("tags"),
            "baseUrl": collection.get("baseUrl"),
        })
        exported["auth"] = _auth(collection.get("auth"))
        exported["endpoints"] = [_outbound_endpoint(e) for e in stored]
        collections.append(exported)
    return {"collections": collections} if collections else None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def export_courier_block(token: str, pipeline_id: str, agent: Dict[str, Any], fetch: Optional[FetchFn] = None,
                         include_secrets: bool = False) -> Optional[Dict[str, Any]]:
    """The ``courier`` block of ``agent`` (a Core Hub or config.json agent entry of ``pipeline_id``), or None.

    None for an agent that is not an HTTP agent, and for one with no Courier definition to export.
    """
    if not is_courier_agent(agent):
        return None
    if fetch is None:
        from commons import fetch_core_hub  # pylint: disable=import-outside-toplevel
        fetch = fetch_core_hub
    if is_http_source_agent(agent):
        agent_id = agent.get("agentId") or agent.get("id")
        return export_inbound_block(fetch, token, pipeline_id, str(agent_id)) if agent_id is not None else None
    return export_outbound_block(fetch, token, agent, include_secrets=include_secrets)


def agent_key(agent: Dict[str, Any]) -> str:
    """``agentType:agentTag``: what matches an agent of one pipeline with the same agent of a copy of it."""
    return f"{str(agent.get('agentType') or '').upper()}:{agent.get('agentTag')}"


def export_pipeline_courier(token: str, pipeline_id: str, agents: List[Dict[str, Any]], fetch: Optional[FetchFn] = None,
                            include_secrets: bool = False) -> Dict[str, Dict[str, Any]]:
    """The ``courier`` block of every HTTP agent of ``pipeline_id`` that has one, by :func:`agent_key`."""
    blocks: Dict[str, Dict[str, Any]] = {}
    for agent in agents:
        block = export_courier_block(token, pipeline_id, agent, fetch=fetch, include_secrets=include_secrets)
        if block is not None:
            blocks[agent_key(agent)] = block
    return blocks


def _admin_login(fetch: FetchFn) -> str:
    response = fetch("/authentication/login", method="POST",
                     body={"username": "admin", "password": os.getenv("DEFAULT_PASSWORD", "")})
    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise SystemExit(f"Cannot log in to Core Hub: set {TOKEN_ENV} or DEFAULT_PASSWORD")
    return token


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export the Courier definitions of the HTTP agents of a pipeline")
    parser.add_argument("--pipeline", required=True, help="ID of the pipeline")
    parser.add_argument("--include-secrets", action="store_true",
                        help="Export the header values of the outbound endpoints (a SUPER_ADMIN only)")
    parser.add_argument("--token", help=f"Core Hub token (default ${TOKEN_ENV}, else an admin login)")
    args = parser.parse_args(argv)
    from commons import fetch_core_hub, get_pipeline_agents  # pylint: disable=import-outside-toplevel
    token = args.token or os.getenv(TOKEN_ENV) or _admin_login(fetch_core_hub)
    agents = [a for a in (get_pipeline_agents(token, args.pipeline) or []) if isinstance(a, dict)]
    json.dump(export_pipeline_courier(token, args.pipeline, agents, include_secrets=args.include_secrets),
              sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

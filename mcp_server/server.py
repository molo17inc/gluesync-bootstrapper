"""
Gluesync MCP Server
-------------------
Exposes Gluesync CoreHub operations as Model Context Protocol (MCP) tools so
that AI agents can automate pipeline management, debugging and development
tasks without human interaction.

Transports
~~~~~~~~~~
* **stdio** – run ``python -m mcp_server.server`` for direct AI agent
  integration (e.g. Claude Desktop, OpenClaw tool-call path).
* **SSE**  – mount via the Automator's FastAPI app at ``/mcp`` so any
  HTTP-capable MCP client can connect while the Automator is running.

Authentication
~~~~~~~~~~~~~~
Every tool accepts a ``token`` argument (CoreHub JWT / API key).  When the
server is mounted inside the Automator, the token can also be pre-configured
via the ``COREHUB_TOKEN`` environment variable so agents do not need to pass
it on every call.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _token(arguments: Dict[str, Any]) -> str:
    """Return the CoreHub token from tool arguments or environment."""
    tok = arguments.get("token") or os.environ.get("COREHUB_TOKEN", "")
    if not tok:
        raise ValueError(
            "CoreHub token is required. Pass 'token' in the tool arguments "
            "or set the COREHUB_TOKEN environment variable."
        )
    return tok


def _corehub_call(path: str, token: str, method: str = "GET", body: Optional[Dict] = None) -> Any:
    """Thin wrapper around commons.fetch_core_hub."""
    from commons import fetch_core_hub  # imported lazily to avoid circular deps
    return fetch_core_hub(path, method=method, token=token, body=body)


def _fmt(data: Any) -> str:
    """Pretty-print any value as a JSON string for tool output."""
    try:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(data)


def _text(content: str) -> List[types.TextContent]:
    return [types.TextContent(type="text", text=content)]


# ---------------------------------------------------------------------------
# MCP Server definition
# ---------------------------------------------------------------------------

server = Server("gluesync-automator")


# ── TOOLS ──────────────────────────────────────────────────────────────────

TOOLS: List[types.Tool] = [
    types.Tool(
        name="list_pipelines",
        description=(
            "List all configured Gluesync pipelines with their IDs, names "
            "and configuration-completion status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "CoreHub authentication token"},
            },
        },
    ),
    types.Tool(
        name="get_pipeline",
        description="Get the full configuration and agent details for a single pipeline.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string", "description": "Pipeline UUID"},
            },
        },
    ),
    types.Tool(
        name="get_pipeline_entities",
        description=(
            "List all entities (table pairs) configured for a pipeline, "
            "including their sync status and last error information."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="get_pipeline_status",
        description=(
            "Get a high-level health summary for a pipeline: how many entities "
            "are syncing, idle, erroring or paused."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="discover_tables",
        description=(
            "Discover available source or target tables for a specific agent "
            "within a pipeline. Useful for verifying connectivity and schema visibility."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id", "schema_name"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string", "description": "Agent UUID"},
                "schema_name": {"type": "string", "description": "Database schema to discover"},
            },
        },
    ),
    types.Tool(
        name="discover_columns",
        description="Discover the columns for a specific table via the source or target agent.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id", "schema_name", "table_name"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "schema_name": {"type": "string"},
                "table_name": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="start_entity_sync",
        description=(
            "Start the sync (with optional snapshot) for one or more entities. "
            "Pass an empty entity_ids list to start all entities in the pipeline."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity UUIDs to start. Empty = all entities.",
                },
                "with_snapshot": {
                    "type": "boolean",
                    "default": False,
                    "description": "Whether to trigger a full snapshot before CDC.",
                },
            },
        },
    ),
    types.Tool(
        name="stop_entity_sync",
        description=(
            "Stop the sync for one or more entities. "
            "Pass an empty entity_ids list to stop all entities in the pipeline."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity UUIDs to stop. Empty = all entities.",
                },
            },
        },
    ),
    types.Tool(
        name="export_pipeline_yaml",
        description=(
            "Export the configuration of a pipeline to a YAML backup string. "
            "The output can be stored and later passed to import_pipeline_yaml."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "base_url"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "base_url": {
                    "type": "string",
                    "description": "CoreHub base URL, e.g. https://localhost:1717",
                },
            },
        },
    ),
    types.Tool(
        name="get_notifications",
        description=(
            "Retrieve recent notifications from CoreHub. Useful for debugging "
            "errors, warnings or informational events emitted by agents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum number of notifications to return",
                },
                "pipeline_id": {
                    "type": "string",
                    "description": "Filter by pipeline (optional)",
                },
                "levels": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["INFO", "WARNING", "ERROR"]},
                    "description": "Filter by severity level(s)",
                },
            },
        },
    ),
    types.Tool(
        name="get_corehub_version",
        description="Return the CoreHub version string.",
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string"},
            },
        },
    ),
]


@server.list_tools()
async def list_tools() -> List[types.Tool]:
    return TOOLS


# ── TOOL IMPLEMENTATIONS ────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[types.TextContent]:
    try:
        token = _token(arguments)
    except ValueError as exc:
        return _text(f"ERROR: {exc}")

    try:
        if name == "list_pipelines":
            result = _corehub_call("/pipelines", token)
            return _text(_fmt(result))

        elif name == "get_pipeline":
            pid = arguments["pipeline_id"]
            result = _corehub_call(f"/pipelines/{pid}", token)
            return _text(_fmt(result))

        elif name == "get_pipeline_entities":
            pid = arguments["pipeline_id"]
            result = _corehub_call(f"/pipelines/{pid}/entities", token)
            return _text(_fmt(result))

        elif name == "get_pipeline_status":
            pid = arguments["pipeline_id"]
            entities_resp = _corehub_call(f"/pipelines/{pid}/entities", token)
            entities = []
            if isinstance(entities_resp, list):
                for item in entities_resp:
                    e = item.get("entity", item) if isinstance(item, dict) and "entity" in item else item
                    entities.append(e)
            elif isinstance(entities_resp, dict) and "entities" in entities_resp:
                entities = entities_resp["entities"]

            summary: Dict[str, Any] = {
                "pipeline_id": pid,
                "total_entities": len(entities),
                "statuses": {},
            }
            for ent in entities:
                status = ent.get("status") or ent.get("syncStatus") or "unknown"
                summary["statuses"].setdefault(status, []).append(
                    ent.get("entityName") or ent.get("entityId", "?")
                )
            return _text(_fmt(summary))

        elif name == "discover_tables":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            schema = arguments["schema_name"]
            result = _corehub_call(
                f"/pipelines/{pid}/agents/{aid}/discovery/tables",
                token,
                method="GET",
            )
            # If there is a schema filter, apply it client-side
            if isinstance(result, dict) and "tables" in result:
                tables = [t for t in result["tables"] if t.get("schema", "") == schema or not schema]
                return _text(_fmt({"schema": schema, "tables": tables, "count": len(tables)}))
            return _text(_fmt(result))

        elif name == "discover_columns":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            schema = arguments["schema_name"]
            table = arguments["table_name"]
            from commons import get_table_columns
            result = get_table_columns(token, pid, aid, schema, table)
            return _text(_fmt(result))

        elif name == "start_entity_sync":
            pid = arguments["pipeline_id"]
            entity_ids: List[str] = arguments.get("entity_ids") or []
            with_snapshot: bool = arguments.get("with_snapshot", False)
            snapshot_param = "true" if with_snapshot else "false"

            if entity_ids:
                results = []
                for eid in entity_ids:
                    r = _corehub_call(
                        f"/pipelines/{pid}/commands/sync/start"
                        f"?withSnapshot={snapshot_param}&entity={eid}",
                        token,
                        method="POST",
                    )
                    results.append({"entity_id": eid, "result": r})
                return _text(_fmt(results))
            else:
                # Start all entities
                r = _corehub_call(
                    f"/pipelines/{pid}/commands/sync/start?withSnapshot={snapshot_param}",
                    token,
                    method="POST",
                )
                return _text(_fmt(r))

        elif name == "stop_entity_sync":
            pid = arguments["pipeline_id"]
            entity_ids: List[str] = arguments.get("entity_ids") or []

            if entity_ids:
                results = []
                for eid in entity_ids:
                    r = _corehub_call(
                        f"/pipelines/{pid}/commands/sync/stop?entity={eid}",
                        token,
                        method="POST",
                    )
                    results.append({"entity_id": eid, "result": r})
                return _text(_fmt(results))
            else:
                r = _corehub_call(
                    f"/pipelines/{pid}/commands/sync/stop",
                    token,
                    method="POST",
                )
                return _text(_fmt(r))

        elif name == "export_pipeline_yaml":
            pid = arguments["pipeline_id"]
            base_url = arguments["base_url"]
            from automator_app.corehub import (
                configure_core_hub,
                export_pipeline_yaml as _export,
            )
            configure_core_hub(base_url)
            yaml_text = _export(
                token=token,
                base_url=base_url,
                pipeline_id=pid,
                use_ssl=None,
                skip_verify=None,
            )
            return _text(yaml_text)

        elif name == "get_notifications":
            limit = arguments.get("limit", 50)
            pipeline_id = arguments.get("pipeline_id")
            levels = arguments.get("levels")

            params_parts = [f"limit={limit}", f"offset=0"]
            if pipeline_id:
                params_parts.append(f"pipelinesId={pipeline_id}")
            if levels:
                for lv in levels:
                    params_parts.append(f"levels={lv}")
            qs = "&".join(params_parts)
            result = _corehub_call(f"/notifications?{qs}", token)
            return _text(_fmt(result))

        elif name == "get_corehub_version":
            result = _corehub_call("/version", token)
            return _text(_fmt(result))

        else:
            return _text(f"ERROR: Unknown tool '{name}'")

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("MCP tool '%s' raised an error: %s", name, exc)
        return _text(f"ERROR: {exc}")


# ── RESOURCES ───────────────────────────────────────────────────────────────

@server.list_resources()
async def list_resources() -> List[types.Resource]:
    return [
        types.Resource(
            uri="gluesync://pipelines",
            name="All Pipelines",
            description="Live list of all configured Gluesync pipelines",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    token = os.environ.get("COREHUB_TOKEN", "")
    if not token:
        return json.dumps({"error": "Set COREHUB_TOKEN environment variable to enable resources"})
    try:
        result = _corehub_call("/pipelines", token)
        return _fmt(result)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── ENTRY POINT (stdio transport) ───────────────────────────────────────────

async def _run_stdio() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="gluesync-automator",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


def run_stdio() -> None:
    """Entry point for stdio transport (Claude Desktop, OpenClaw, etc.)."""
    import asyncio
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    run_stdio()

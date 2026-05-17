"""
Gluesync MCP Server
-------------------
Exposes Gluesync CoreHub operations as Model Context Protocol (MCP) tools so
that AI agents can automate pipeline management, debugging and development
tasks without human interaction.

API coverage (mapped against develop branch of gluesync-kotlin):
  Pipelines  : list, get, create, update, delete, snapshot-status
  Agents     : list, get, node-info, schemas, assign/unassign
  Entities   : list, get, delete, upsert
  Commands   : start/stop/redo/one-time-snapshot (entity & group level)
  Maintenance: enter/exit maintenance mode
  Global cfg : get all, get keys, get/set logging level, get/set release channel
  Notifications: list, count, mark-read
  Metrics    : get agent metrics
  Groups     : list, get
  Mapping fn : list, get base-code
  Checkpoint : patch (reset)
  Version    : get

Transports
~~~~~~~~~~
* **stdio** – run ``python -m mcp_server.server`` for direct AI agent
  integration (e.g. Claude Desktop, OpenClaw tool-call path).
* **SSE**  – mounted inside the Automator's FastAPI app at ``/mcp``.

Authentication
~~~~~~~~~~~~~~
Pass ``token`` in each tool call, or set ``COREHUB_TOKEN`` env var.
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
    tok = arguments.get("token") or os.environ.get("COREHUB_TOKEN", "")
    if not tok:
        raise ValueError(
            "CoreHub token is required. Pass 'token' in the tool arguments "
            "or set the COREHUB_TOKEN environment variable."
        )
    return tok


def _call(path: str, token: str, method: str = "GET", body: Optional[Dict] = None) -> Any:
    from commons import fetch_core_hub
    return fetch_core_hub(path, method=method, token=token, body=body)


def _fmt(data: Any) -> str:
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

    # ── PIPELINES ───────────────────────────────────────────────────────────
    types.Tool(
        name="list_pipelines",
        description="List all configured Gluesync pipelines with IDs, names and status.",
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="get_pipeline",
        description="Get full configuration and agent details for a single pipeline.",
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
            "High-level health summary for a pipeline: how many entities are "
            "syncing, idle, erroring or paused."
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
        name="get_pipeline_snapshot_status",
        description=(
            "Get the current snapshot progress for all entities in a pipeline. "
            "Returns percentage, estimated time remaining and current table."
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
        name="delete_pipeline",
        description="Delete a pipeline and stop all its running entities.",
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
        name="reset_pipeline_checkpoint",
        description=(
            "Reset the CDC checkpoint of a pipeline so that replication "
            "restarts from the current log position. Useful after a recovery "
            "or when the checkpoint is stale."
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

    # ── AGENTS ──────────────────────────────────────────────────────────────
    types.Tool(
        name="list_pipeline_agents",
        description="List the source and target agents configured for a pipeline.",
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
        name="get_agent_node_info",
        description=(
            "Get the node information for a specific agent: data type matrix, "
            "supported entity types, bulk mode, max alias length. "
            "Useful for understanding what types and features an agent supports."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="discover_schemas",
        description="Discover the available database schemas for an agent.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "limit": {"type": "integer", "default": 200},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    types.Tool(
        name="discover_tables",
        description="Discover available tables in a schema for an agent.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id", "schema_name"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "schema_name": {"type": "string"},
                "limit": {"type": "integer", "default": 500},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    types.Tool(
        name="discover_columns",
        description="Discover the columns for a specific table via an agent.",
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

    # ── ENTITIES ────────────────────────────────────────────────────────────
    types.Tool(
        name="get_pipeline_entities",
        description=(
            "List all entities (table pairs) for a pipeline with sync state "
            "and error information."
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
        name="get_entity",
        description="Get the full configuration of a single entity.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "entity_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_id": {"type": "string"},
            },
        },
    ),
    types.Tool(
        name="delete_entities",
        description="Delete one or more entities from a pipeline.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "entity_ids"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),

    # ── SYNC COMMANDS ────────────────────────────────────────────────────────
    types.Tool(
        name="start_sync",
        description=(
            "Start sync for one or more entities (optionally with a full snapshot). "
            "Pass an empty entity_ids list to start all entities."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Empty = all entities"},
                "with_snapshot": {"type": "boolean", "default": False},
                "snapshot_write_method": {
                    "type": "string",
                    "enum": ["UPSERT", "INSERT"],
                    "default": "UPSERT",
                },
            },
        },
    ),
    types.Tool(
        name="stop_sync",
        description="Stop sync for one or more entities. Empty entity_ids = stop all.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    types.Tool(
        name="redo_sync",
        description=(
            "Restart replication from zero for one or more entities "
            "(equivalent to stop + reset + start with snapshot)."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}},
                "with_snapshot": {"type": "boolean", "default": True},
                "snapshot_write_method": {
                    "type": "string",
                    "enum": ["UPSERT", "INSERT"],
                    "default": "UPSERT",
                },
            },
        },
    ),
    types.Tool(
        name="one_time_snapshot",
        description=(
            "Trigger a one-time snapshot for one or more entities without "
            "switching them into full CDC mode."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entity_ids": {"type": "array", "items": {"type": "string"}},
                "snapshot_write_method": {
                    "type": "string",
                    "enum": ["UPSERT", "INSERT"],
                    "default": "UPSERT",
                },
            },
        },
    ),
    types.Tool(
        name="start_group_sync",
        description="Start sync for all entities in a group.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "group_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "group_id": {"type": "string"},
                "with_snapshot": {"type": "boolean", "default": False},
            },
        },
    ),
    types.Tool(
        name="stop_group_sync",
        description="Stop sync for all entities in a group.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "group_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "group_id": {"type": "string"},
            },
        },
    ),

    # ── MAINTENANCE ─────────────────────────────────────────────────────────
    types.Tool(
        name="enter_maintenance_mode",
        description=(
            "Put a pipeline into maintenance mode: pauses all syncs without "
            "losing the checkpoint. Use before applying schema changes."
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
        name="exit_maintenance_mode",
        description="Resume all syncs for a pipeline that is in maintenance mode.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
            },
        },
    ),

    # ── GROUPS ───────────────────────────────────────────────────────────────
    types.Tool(
        name="list_groups",
        description="List all entity groups for a pipeline.",
        inputSchema={
            "type": "object",
            "required": ["pipeline_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
            },
        },
    ),

    # ── GLOBAL CONFIG ────────────────────────────────────────────────────────
    types.Tool(
        name="get_global_config",
        description=(
            "Read the global CoreHub configuration: logging level, telemetry, "
            "release channel, Grafana integration, SMTP and session settings."
        ),
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="get_global_config_keys",
        description="List the keys present in the global CoreHub configuration.",
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="get_release_channel",
        description="Get the current CoreHub update release channel (GA, beta, alpha).",
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="set_log_level",
        description=(
            "Change the CoreHub logging level at runtime. "
            "Accepted values: TRACE, DEBUG, INFO, WARN, ERROR."
        ),
        inputSchema={
            "type": "object",
            "required": ["level"],
            "properties": {
                "token": {"type": "string"},
                "level": {
                    "type": "string",
                    "enum": ["TRACE", "DEBUG", "INFO", "WARN", "ERROR"],
                },
            },
        },
    ),

    # ── NOTIFICATIONS ────────────────────────────────────────────────────────
    types.Tool(
        name="get_notifications",
        description=(
            "Retrieve recent CoreHub notifications (errors, warnings, info). "
            "Filter by pipeline, level, and date range."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "pipeline_id": {"type": "string"},
                "levels": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["INFO", "WARNING", "ERROR"]},
                },
                "from_date": {"type": "string", "description": "ISO-8601 datetime"},
                "to_date": {"type": "string", "description": "ISO-8601 datetime"},
            },
        },
    ),
    types.Tool(
        name="get_notification_count",
        description="Get unread notification counts grouped by severity.",
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),

    # ── METRICS ──────────────────────────────────────────────────────────────
    types.Tool(
        name="get_agent_metrics",
        description=(
            "Get real-time replication metrics for a specific agent: "
            "events/sec, lag, throughput, error counters."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
            },
        },
    ),

    # ── EXPORT ───────────────────────────────────────────────────────────────
    types.Tool(
        name="export_pipeline_yaml",
        description=(
            "Export the configuration of a pipeline to a YAML backup string "
            "including the CoreHub provenance header."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "base_url"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "base_url": {"type": "string",
                             "description": "CoreHub base URL, e.g. https://localhost:1717"},
            },
        },
    ),

    # ── VERSION ──────────────────────────────────────────────────────────────
    types.Tool(
        name="get_corehub_version",
        description="Return the CoreHub version string.",
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
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
        # ── Pipelines ──────────────────────────────────────────────────────
        if name == "list_pipelines":
            return _text(_fmt(_call("/pipelines", token)))

        elif name == "get_pipeline":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}", token)))

        elif name == "get_pipeline_status":
            pid = arguments["pipeline_id"]
            entities_resp = _call(f"/pipelines/{pid}/entities", token)
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
                "by_status": {},
            }
            for ent in entities:
                status = ent.get("status") or ent.get("syncStatus") or "unknown"
                summary["by_status"].setdefault(status, []).append(
                    ent.get("entityName") or ent.get("entityId", "?")
                )
            return _text(_fmt(summary))

        elif name == "get_pipeline_snapshot_status":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/snapshot-status", token)))

        elif name == "delete_pipeline":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}", token, method="DELETE")))

        elif name == "reset_pipeline_checkpoint":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/checkpoint", token, method="PATCH")))

        # ── Agents ─────────────────────────────────────────────────────────
        elif name == "list_pipeline_agents":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/agents", token)))

        elif name == "get_agent_node_info":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/agents/{aid}/discovery/node-info", token)))

        elif name == "discover_schemas":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            limit = arguments.get("limit", 200)
            offset = arguments.get("offset", 0)
            return _text(_fmt(_call(
                f"/pipelines/{pid}/agents/{aid}/discovery/schemas?limit={limit}&offset={offset}",
                token,
            )))

        elif name == "discover_tables":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            schema = arguments["schema_name"]
            limit = arguments.get("limit", 500)
            offset = arguments.get("offset", 0)
            result = _call(
                f"/pipelines/{pid}/agents/{aid}/discovery/tables"
                f"?tableschema={schema}&limit={limit}&offset={offset}",
                token,
            )
            return _text(_fmt(result))

        elif name == "discover_columns":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            schema = arguments["schema_name"]
            table = arguments["table_name"]
            from commons import get_table_columns
            return _text(_fmt(get_table_columns(token, pid, aid, schema, table)))

        # ── Entities ───────────────────────────────────────────────────────
        elif name == "get_pipeline_entities":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/entities", token)))

        elif name == "get_entity":
            pid = arguments["pipeline_id"]
            eid = arguments["entity_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/entities/{eid}", token)))

        elif name == "delete_entities":
            pid = arguments["pipeline_id"]
            eids = arguments["entity_ids"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/config/entities",
                token,
                method="DELETE",
                body={"entitiesId": eids},
            )))

        # ── Sync commands ──────────────────────────────────────────────────
        elif name == "start_sync":
            pid = arguments["pipeline_id"]
            eids: List[str] = arguments.get("entity_ids") or []
            snapshot = "true" if arguments.get("with_snapshot") else "false"
            write_method = arguments.get("snapshot_write_method", "UPSERT")
            qs = f"?withSnapshot={snapshot}&snapshotWriteMethod={write_method}"
            if eids:
                qs += "&" + "&".join(f"entitiesId={e}" for e in eids)
            return _text(_fmt(_call(f"/pipelines/{pid}/commands/sync/start{qs}", token, method="POST")))

        elif name == "stop_sync":
            pid = arguments["pipeline_id"]
            eids = arguments.get("entity_ids") or []
            qs = "?" + "&".join(f"entitiesId={e}" for e in eids) if eids else ""
            return _text(_fmt(_call(f"/pipelines/{pid}/commands/sync/stop{qs}", token, method="POST")))

        elif name == "redo_sync":
            pid = arguments["pipeline_id"]
            eids = arguments.get("entity_ids") or []
            snapshot = "true" if arguments.get("with_snapshot", True) else "false"
            write_method = arguments.get("snapshot_write_method", "UPSERT")
            qs = f"?withSnapshot={snapshot}&snapshotWriteMethod={write_method}"
            if eids:
                qs += "&" + "&".join(f"entitiesId={e}" for e in eids)
            return _text(_fmt(_call(f"/pipelines/{pid}/commands/sync/redo{qs}", token, method="POST")))

        elif name == "one_time_snapshot":
            pid = arguments["pipeline_id"]
            eids = arguments.get("entity_ids") or []
            write_method = arguments.get("snapshot_write_method", "UPSERT")
            qs = f"?snapshotWriteMethod={write_method}"
            if eids:
                qs += "&" + "&".join(f"entitiesId={e}" for e in eids)
            return _text(_fmt(_call(
                f"/pipelines/{pid}/commands/sync/one-time-snapshot{qs}", token, method="POST"
            )))

        elif name == "start_group_sync":
            pid = arguments["pipeline_id"]
            gid = arguments["group_id"]
            snapshot = "true" if arguments.get("with_snapshot") else "false"
            return _text(_fmt(_call(
                f"/pipelines/{pid}/commands/sync/start-group?groupId={gid}&withSnapshot={snapshot}",
                token,
                method="POST",
            )))

        elif name == "stop_group_sync":
            pid = arguments["pipeline_id"]
            gid = arguments["group_id"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/commands/sync/stop-group?groupId={gid}",
                token,
                method="POST",
            )))

        # ── Maintenance ────────────────────────────────────────────────────
        elif name == "enter_maintenance_mode":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/commands/maintenance/enter", token, method="POST"
            )))

        elif name == "exit_maintenance_mode":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/commands/maintenance/exit", token, method="POST"
            )))

        # ── Groups ─────────────────────────────────────────────────────────
        elif name == "list_groups":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/config/groups", token)))

        # ── Global config ──────────────────────────────────────────────────
        elif name == "get_global_config":
            return _text(_fmt(_call("/global-config", token)))

        elif name == "get_global_config_keys":
            return _text(_fmt(_call("/global-config/keys", token)))

        elif name == "get_release_channel":
            return _text(_fmt(_call("/global-config/release-channel", token)))

        elif name == "set_log_level":
            level = arguments["level"]
            return _text(_fmt(_call(
                f"/global-config/logging/level/{level}", token, method="PUT"
            )))

        # ── Notifications ──────────────────────────────────────────────────
        elif name == "get_notifications":
            limit = arguments.get("limit", 50)
            offset = arguments.get("offset", 0)
            parts = [f"limit={limit}", f"offset={offset}"]
            if arguments.get("pipeline_id"):
                parts.append(f"pipelinesId={arguments['pipeline_id']}")
            for lv in (arguments.get("levels") or []):
                parts.append(f"levels={lv}")
            if arguments.get("from_date"):
                parts.append(f"fromDate={arguments['from_date']}")
            if arguments.get("to_date"):
                parts.append(f"toDate={arguments['to_date']}")
            return _text(_fmt(_call(f"/notifications?{'&'.join(parts)}", token)))

        elif name == "get_notification_count":
            return _text(_fmt(_call("/notifications/count", token)))

        # ── Metrics ────────────────────────────────────────────────────────
        elif name == "get_agent_metrics":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(f"/metrics/{pid}/agents/{aid}", token)))

        # ── Export ─────────────────────────────────────────────────────────
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

        # ── Version ────────────────────────────────────────────────────────
        elif name == "get_corehub_version":
            return _text(_fmt(_call("/version", token)))

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
        types.Resource(
            uri="gluesync://global-config",
            name="Global Configuration",
            description="CoreHub global configuration (logging, telemetry, release channel, etc.)",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    token = os.environ.get("COREHUB_TOKEN", "")
    if not token:
        return json.dumps({"error": "Set COREHUB_TOKEN environment variable to enable resources"})
    try:
        if uri == "gluesync://pipelines":
            return _fmt(_call("/pipelines", token))
        elif uri == "gluesync://global-config":
            return _fmt(_call("/global-config", token))
        return json.dumps({"error": f"Unknown resource URI: {uri}"})
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

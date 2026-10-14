"""
Gluesync MCP Server
-------------------
Exposes Gluesync CoreHub operations as Model Context Protocol (MCP) tools so
that AI agents can automate pipeline management, debugging and development
tasks without human interaction.

API coverage (mapped against develop branch of gluesync-kotlin):
  Pipelines  : list, get, status, delete, snapshot-status, checkpoint reset
  Agents     : list, node-info, discovery (schemas/tables/columns), assign/unassign
  Entities   : list, get, delete, upsert
  Commands   : start/stop/redo/one-time-snapshot (entity & group level)
  Maintenance: enter/exit maintenance mode
  Global cfg : get all, get keys, set logging level, release channel
  Notifications: list, count, mark-read
  Metrics    : get agent/pipeline/global metrics (JSON), Prometheus text (raw)
  Groups     : list
  Mapping fn : list, get code
  License    : get instance/license info
  Export     : pipeline YAML backup
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
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Global token provider for SSE transport (set by Automator)
_token_provider: Optional[callable] = None

def set_token_provider(provider: callable) -> None:
    """Set a callable that returns the CoreHub token (for SSE transport)."""
    global _token_provider
    _token_provider = provider

def _read_token_from_file() -> Optional[str]:
    """Read token from .gluesync_mcp_token in the project root (written by Automator)."""
    try:
        # Resolve relative to this file's location: <project_root>/mcp_server/server.py
        # (Claude Desktop ignores the cwd config option and runs the process from /)
        token_file = Path(__file__).resolve().parent.parent / ".gluesync_mcp_token"
        logger.info("Looking for token file at: %s (exists=%s)", token_file, token_file.exists())
        if token_file.exists():
            with open(token_file, "r") as f:
                lines = f.readlines()
                logger.info("Token file has %d lines", len(lines))
                if lines:
                    token = lines[0].strip()
                    logger.info("Token read successfully (length=%d)", len(token))
                    if len(lines) > 1:
                        # Also set CORE_HUB_URL from file
                        base_url = lines[1].strip()
                        os.environ["CORE_HUB_URL"] = base_url
                        logger.info("Set CORE_HUB_URL=%s", base_url)
                    return token
    except Exception as exc:
        logger.warning("Failed to read token file: %s", exc)
    return None

def _token(arguments: Dict[str, Any]) -> str:
    # Priority: tool argument > token provider (SSE) > token file > environment variable
    tok = arguments.get("token")
    if not tok and _token_provider:
        tok = _token_provider()
    if not tok:
        tok = _read_token_from_file()
    if not tok:
        tok = os.environ.get("COREHUB_TOKEN", "")
    if not tok:
        raise ValueError(
            "CoreHub token is required. Pass 'token' in the tool arguments, "
            "set the COREHUB_TOKEN environment variable, or authenticate via the Automator."
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
        name="get_agent",
        description=(
            "Get the full configuration for a specific agent in a pipeline: "
            "host, port, credentials mask, specific configuration, certificates."
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
        name="get_agent_raw",
        description=(
            "Get the raw configuration for a specific agent including secrets. "
            "Requires SUPER_ADMIN role."
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

    types.Tool(
        name="assign_agent",
        description=(
            "Assign (attach) an existing agent to a pipeline as SOURCE or TARGET."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "agent_id", "agent_type"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "agent_type": {"type": "string", "enum": ["SOURCE", "TARGET"]},
            },
        },
    ),
    types.Tool(
        name="unassign_agent",
        description="Unassign (detach) an agent from a pipeline.",
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
        name="get_pipeline_entities_status",
        description=(
            "Get real-time runtime status for every entity in a pipeline: "
            "isSyncActive, isMigrationActive, isBusy, errorState. "
            "This is the same data the Gluesync MPP UI uses to display "
            "entity status (Active / Hold / Error)."
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
        name="upsert_entities",
        description=(
            "Create or update one or more entities in a pipeline. Each entity "
            "must be a full entity payload in the same JSON format returned by "
            "get_entity / get_pipeline_entities (entityName, agentEntities, ...). "
            "Use this to fix entity configurations programmatically. "
            "On Gluesync 2.3 and later, each entry of the target entityType's "
            "columnsMappingMatrix holds lists: {sourceTableObjectId, targetTableObjectId, "
            "sourceColumnIds, targetColumnIds}; one source and one target is 1:1, several "
            "sources N:1 (needs a custom field function or the entity's UDF), several targets "
            "1:N (each gets a copy of the source unless a function writes it), no target 1:null, "
            "no source a column a field function or the UDF produces; several sources to "
            "several targets is refused. Every checked column of the entity appears in exactly "
            "one entry. A new or changed entity is refused with HTTP 400 for a matrix that "
            "breaks these rules, a checked target column nothing writes, a fixed or technical "
            "field function on a column a source feeds, keys that break the key rules (the "
            "physical primary key of a table is its key; R12), or keys that cannot be read "
            "because an agent is not loaded; one refused entity refuses the whole request."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "entities"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "entities": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Full entity payloads to create or update",
                },
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
            "Each notification carries pipelineId, agentId, entityId and groupId "
            "so you can correlate errors with the exact pipeline and entity they "
            "belong to."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
                "text_to_search": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "entity_id": {"type": "string"},
                "group_id": {"type": "string"},
                "level": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["INFO", "WARNING", "ERROR"]},
                },
                "from_date": {"type": "string", "description": "ISO-8601 datetime"},
                "to_date": {"type": "string", "description": "ISO-8601 datetime"},
            },
        },
    ),
    types.Tool(
        name="get_notification",
        description="Get a single notification by its unique ID.",
        inputSchema={
            "type": "object",
            "required": ["notification_id"],
            "properties": {
                "token": {"type": "string"},
                "notification_id": {"type": "string"},
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
    types.Tool(
        name="mark_notifications_read",
        description=(
            "Mark one or more notifications as read by ID. "
            "Pass an empty list to mark ALL notifications as read."
        ),
        inputSchema={
            "type": "object",
            "required": ["notification_ids"],
            "properties": {
                "token": {"type": "string"},
                "notification_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Notification IDs to mark as read (empty = all)",
                },
            },
        },
    ),

    # ── MAPPING FUNCTIONS (UDF) ──────────────────────────────────────────────
    types.Tool(
        name="list_mapping_functions",
        description=(
            "List the mapping functions (UDFs) referenced by the entities of a "
            "pipeline, with name, type and the entities that use them."
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
        name="get_mapping_function_code",
        description=(
            "Get the source code of a mapping function (UDF) by name. "
            "Returns the code and language type (Java, Kotlin, Python, ...)."
        ),
        inputSchema={
            "type": "object",
            "required": ["pipeline_id", "udf_name"],
            "properties": {
                "token": {"type": "string"},
                "pipeline_id": {"type": "string"},
                "udf_name": {"type": "string"},
            },
        },
    ),

    # ── METRICS ──────────────────────────────────────────────────────────────
    types.Tool(
        name="get_pipeline_metrics",
        description=(
            "Get aggregated real-time metrics for an entire pipeline: "
            "total events/sec, lag, throughput across all agents."
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
    types.Tool(
        name="get_entity_metrics",
        description=(
            "Get real-time metrics for a specific entity: events processed, "
            "lag, throughput, last sync time."
        ),
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
        name="get_global_metrics",
        description=(
            "Get system-wide CoreHub metrics: total pipelines, agents, entities, "
            "and overall throughput."
        ),
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="get_prometheus_metrics",
        description=(
            "Get the CoreHub Prometheus metrics scrape endpoint in raw OpenMetrics "
            "text format. Returns system-level metrics: CPU, memory, JVM, GC, "
            "thread pools, replication lag, entity heartbeat, thresholds, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
        },
    ),
    types.Tool(
        name="get_agent_prometheus_metrics",
        description=(
            "Get Prometheus metrics for a specific agent in raw OpenMetrics text "
            "format: events/sec, lag, throughput, error counters, snapshot progress."
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

    # ── LICENSE ──────────────────────────────────────────────────────────────
    types.Tool(
        name="get_license_info",
        description=(
            "Get the CoreHub license / instance information: plan, expiry and "
            "licensed features."
        ),
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
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

ToolHandler = Callable[[Dict[str, Any], str], List[types.TextContent]]

_PID = "pipeline_id"
_AID = "agent_id"


def _raw_text(raw: Any) -> List[types.TextContent]:
    """Return a raw (Prometheus) text payload as is, anything else as JSON."""
    return _text(raw if isinstance(raw, str) else _fmt(raw))


def _path_tool(
    path_template: str, *arg_names: str, method: str = "GET", raw: bool = False
) -> ToolHandler:
    """Build a handler that calls ``path_template`` filled with the named arguments.

    The arguments are read in the given order, so a missing one raises the same
    ``KeyError`` as reading them one by one. With ``raw`` a text response is
    returned as is instead of being JSON-formatted.
    """
    def handler(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
        values = [arguments[arg_name] for arg_name in arg_names]
        result = _call(path_template.format(*values), token, method=method)
        if raw:
            return _raw_text(result)
        return _text(_fmt(result))
    return handler


def _entity_ids_query(arguments: Dict[str, Any]) -> str:
    """Return the ``&entitiesId=...`` query suffix for the optional ``entity_ids``."""
    eids: List[str] = arguments.get("entity_ids") or []
    if eids:
        return "&" + "&".join(f"entitiesId={e}" for e in eids)
    return ""


def _sync_command(
    arguments: Dict[str, Any], token: str, command: str, default_snapshot: bool
) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    entity_qs = _entity_ids_query(arguments)
    snapshot = "true" if arguments.get("with_snapshot", default_snapshot) else "false"
    write_method = arguments.get("snapshot_write_method", "UPSERT")
    qs = f"?withSnapshot={snapshot}&snapshotWriteMethod={write_method}" + entity_qs
    return _text(_fmt(_call(f"/pipelines/{pid}/commands/sync/{command}{qs}", token, method="POST")))


# ── Pipelines ──────────────────────────────────────────────────────────────

def _tool_get_pipeline_status(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    status_resp = _call(f"/pipelines/{pid}/entities-status", token)
    statuses = status_resp if isinstance(status_resp, list) else []

    summary: Dict[str, Any] = {
        "pipeline_id": pid,
        "total_entities": len(statuses),
        "by_status": {},
    }
    for st in statuses:
        if st.get("errorState"):
            status = "error"
        elif st.get("isSyncActive") or st.get("isMigrationActive"):
            status = "active"
        else:
            status = "hold"
        summary["by_status"].setdefault(status, []).append(
            st.get("entityId", "?")
        )
    return _text(_fmt(summary))


# ── Agents ─────────────────────────────────────────────────────────────────

def _tool_discover_schemas(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    aid = arguments["agent_id"]
    limit = arguments.get("limit", 200)
    offset = arguments.get("offset", 0)
    return _text(_fmt(_call(
        f"/pipelines/{pid}/agents/{aid}/discovery/schemas?limit={limit}&offset={offset}",
        token,
    )))


def _tool_discover_tables(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
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


def _tool_discover_columns(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    aid = arguments["agent_id"]
    schema = arguments["schema_name"]
    table = arguments["table_name"]
    from commons import get_table_columns
    return _text(_fmt(get_table_columns(token, pid, aid, schema, table)))


def _tool_assign_agent(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    aid = arguments["agent_id"]
    agent_type = arguments["agent_type"].upper()
    return _text(_fmt(_call(
        f"/pipelines/{pid}/agents/{aid}?agentType={agent_type}",
        token,
        method="PUT",
    )))


# ── Entities ───────────────────────────────────────────────────────────────

def _tool_upsert_entities(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    entities = arguments["entities"]
    return _text(_fmt(_call(
        f"/pipelines/{pid}/config/entities",
        token,
        method="PUT",
        body={"entities": entities},
    )))


def _tool_delete_entities(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    eids = arguments["entity_ids"]
    return _text(_fmt(_call(
        f"/pipelines/{pid}/config/entities",
        token,
        method="DELETE",
        body={"entitiesId": eids},
    )))


# ── Sync commands ──────────────────────────────────────────────────────────

def _tool_start_sync(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    return _sync_command(arguments, token, "start", default_snapshot=False)


def _tool_stop_sync(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    eids = arguments.get("entity_ids") or []
    qs = "?" + "&".join(f"entitiesId={e}" for e in eids) if eids else ""
    return _text(_fmt(_call(f"/pipelines/{pid}/commands/sync/stop{qs}", token, method="POST")))


def _tool_redo_sync(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    return _sync_command(arguments, token, "redo", default_snapshot=True)


def _tool_one_time_snapshot(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    entity_qs = _entity_ids_query(arguments)
    write_method = arguments.get("snapshot_write_method", "UPSERT")
    qs = f"?snapshotWriteMethod={write_method}" + entity_qs
    return _text(_fmt(_call(
        f"/pipelines/{pid}/commands/sync/one-time-snapshot{qs}", token, method="POST"
    )))


def _tool_start_group_sync(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    gid = arguments["group_id"]
    snapshot = "true" if arguments.get("with_snapshot") else "false"
    return _text(_fmt(_call(
        f"/pipelines/{pid}/commands/sync/start-group?groupId={gid}&withSnapshot={snapshot}",
        token,
        method="POST",
    )))


# ── Notifications ──────────────────────────────────────────────────────────

# Optional notification filters, in query-string order: (argument, query parameter).
_NOTIFICATION_FILTERS_BEFORE_LEVEL = (
    ("text_to_search", "textToSearch"),
    ("pipeline_id", "pipelineId"),
    ("agent_id", "agentId"),
    ("entity_id", "entityId"),
    ("group_id", "groupId"),
)
_NOTIFICATION_FILTERS_AFTER_LEVEL = (
    ("from_date", "fromDate"),
    ("to_date", "toDate"),
)


def _append_filters(parts: List[str], arguments: Dict[str, Any], filters: Sequence[Any]) -> None:
    for arg_name, param in filters:
        if arguments.get(arg_name):
            parts.append(f"{param}={arguments[arg_name]}")


def _tool_get_notifications(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    limit = arguments.get("limit", 50)
    offset = arguments.get("offset", 0)
    parts = [f"limit={limit}", f"offset={offset}"]
    _append_filters(parts, arguments, _NOTIFICATION_FILTERS_BEFORE_LEVEL)
    for lv in (arguments.get("level") or []):
        parts.append(f"level={lv}")
    _append_filters(parts, arguments, _NOTIFICATION_FILTERS_AFTER_LEVEL)
    return _text(_fmt(_call(f"/notifications?{'&'.join(parts)}", token)))


def _tool_mark_notifications_read(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    nids = arguments.get("notification_ids") or []
    if len(nids) == 1:
        # Single notification → POST /notifications/{id}/read
        return _text(_fmt(_call(
            f"/notifications/{nids[0]}/read", token, method="POST"
        )))
    if nids:
        # Multiple → POST /notifications/read with ids in body
        return _text(_fmt(_call(
            "/notifications/read",
            token,
            method="POST",
            body={"notificationsId": nids},
        )))
    # All → POST /notifications/read with empty body
    return _text(_fmt(_call("/notifications/read", token, method="POST")))


# ── Mapping functions (UDF) ────────────────────────────────────────────────

def _collect_udf_entries(
    udf_entries: List[Any], entity_name: str, udfs: Dict[str, Dict[str, Any]]
) -> None:
    for u in udf_entries:
        if isinstance(u, dict) and u.get("name"):
            entry = udfs.setdefault(
                str(u["name"]),
                {"name": u["name"], "type": u.get("type"), "usedByEntities": []},
            )
            if entity_name not in entry["usedByEntities"]:
                entry["usedByEntities"].append(entity_name)


def _scan_custom_props(obj: Any, entity_name: str, udfs: Dict[str, Dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        udf_entries = obj.get("udf")
        if isinstance(udf_entries, list):
            _collect_udf_entries(udf_entries, entity_name, udfs)
        for v in obj.values():
            _scan_custom_props(v, entity_name, udfs)
    elif isinstance(obj, list):
        for v in obj:
            _scan_custom_props(v, entity_name, udfs)


def _entity_items(entities_resp: Any) -> Any:
    """Return the entity list of a ``/entities`` response (a list or ``{"entities": [...]}``)."""
    if isinstance(entities_resp, list):
        return entities_resp
    if isinstance(entities_resp, dict):
        return entities_resp.get("entities", [])
    return []


def _tool_list_mapping_functions(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
    pid = arguments["pipeline_id"]
    entities_resp = _call(f"/pipelines/{pid}/entities", token)
    udfs: Dict[str, Dict[str, Any]] = {}
    for item in _entity_items(entities_resp):
        ent = item.get("entity", item) if isinstance(item, dict) else item
        if isinstance(ent, dict):
            ent_name = str(ent.get("entityName") or ent.get("id") or "unknown")
            _scan_custom_props(ent, ent_name, udfs)
    return _text(_fmt(list(udfs.values())))


# ── Export ─────────────────────────────────────────────────────────────────

def _tool_export_pipeline_yaml(arguments: Dict[str, Any], token: str) -> List[types.TextContent]:
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


# Tool name -> handler(arguments, token).
_TOOL_HANDLERS: Dict[str, ToolHandler] = {
    # Pipelines
    "list_pipelines": _path_tool("/pipelines"),
    "get_pipeline": _path_tool("/pipelines/{}", _PID),
    "get_pipeline_status": _tool_get_pipeline_status,
    "get_pipeline_snapshot_status": _path_tool("/pipelines/{}/snapshot-status", _PID),
    "delete_pipeline": _path_tool("/pipelines/{}", _PID, method="DELETE"),
    "reset_pipeline_checkpoint": _path_tool("/pipelines/{}/checkpoint", _PID, method="PATCH"),
    "get_pipeline_entities_status": _path_tool("/pipelines/{}/entities-status", _PID),
    # Agents
    "list_pipeline_agents": _path_tool("/pipelines/{}/agents", _PID),
    "get_agent": _path_tool("/pipelines/{}/agents/{}", _PID, _AID),
    "get_agent_raw": _path_tool("/pipelines/{}/agents/{}?includeSecrets=true", _PID, _AID),
    "get_agent_node_info": _path_tool("/pipelines/{}/agents/{}/discovery/node-info", _PID, _AID),
    "discover_schemas": _tool_discover_schemas,
    "discover_tables": _tool_discover_tables,
    "discover_columns": _tool_discover_columns,
    "assign_agent": _tool_assign_agent,
    "unassign_agent": _path_tool("/pipelines/{}/agents/{}", _PID, _AID, method="DELETE"),
    # Entities
    "get_pipeline_entities": _path_tool("/pipelines/{}/entities", _PID),
    "get_entity": _path_tool("/pipelines/{}/entities/{}", _PID, "entity_id"),
    "upsert_entities": _tool_upsert_entities,
    "delete_entities": _tool_delete_entities,
    # Sync commands
    "start_sync": _tool_start_sync,
    "stop_sync": _tool_stop_sync,
    "redo_sync": _tool_redo_sync,
    "one_time_snapshot": _tool_one_time_snapshot,
    "start_group_sync": _tool_start_group_sync,
    "stop_group_sync": _path_tool(
        "/pipelines/{}/commands/sync/stop-group?groupId={}", _PID, "group_id", method="POST"
    ),
    # Maintenance
    "enter_maintenance_mode": _path_tool("/pipelines/{}/commands/maintenance/enter", _PID, method="POST"),
    "exit_maintenance_mode": _path_tool("/pipelines/{}/commands/maintenance/exit", _PID, method="POST"),
    # Groups
    "list_groups": _path_tool("/pipelines/{}/config/groups", _PID),
    # Global config
    "get_global_config": _path_tool("/global-config"),
    "get_global_config_keys": _path_tool("/global-config/keys"),
    "get_release_channel": _path_tool("/global-config/release-channel"),
    "set_log_level": _path_tool("/global-config/logging/level/{}", "level", method="PUT"),
    # Notifications
    "get_notifications": _tool_get_notifications,
    "get_notification": _path_tool("/notifications/{}", "notification_id"),
    "get_notification_count": _path_tool("/notifications/count"),
    "mark_notifications_read": _tool_mark_notifications_read,
    # Mapping functions (UDF)
    "list_mapping_functions": _tool_list_mapping_functions,
    "get_mapping_function_code": _path_tool(
        "/pipelines/{}/config/entities/mapping-functions/{}", _PID, "udf_name"
    ),
    # Metrics (JSON)
    "get_pipeline_metrics": _path_tool("/metrics/{}", _PID),
    "get_agent_metrics": _path_tool("/metrics/{}/agents/{}", _PID, _AID),
    "get_entity_metrics": _path_tool("/metrics/{}/entities/{}", _PID, "entity_id"),
    "get_global_metrics": _path_tool("/metrics"),
    # Metrics (Prometheus / OpenMetrics text). The base /metrics endpoint returns
    # Prometheus text when basicMetricsRoute is mounted (via prometheusMetricsRoutes).
    "get_prometheus_metrics": _path_tool("/metrics", raw=True),
    "get_agent_prometheus_metrics": _path_tool("/metrics/{}/agents/{}", _PID, _AID, raw=True),
    # Export
    "export_pipeline_yaml": _tool_export_pipeline_yaml,
    # License
    "get_license_info": _path_tool("/global-config/license"),
    # Version
    "get_corehub_version": _path_tool("/version"),
}


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> Sequence[types.TextContent]:
    try:
        token = _token(arguments)
    except ValueError as exc:
        return _text(f"ERROR: {exc}")

    try:
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return _text(f"ERROR: Unknown tool '{name}'")
        return handler(arguments, token)

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
    from io import TextIOWrapper
    import anyio
    from mcp.server.stdio import stdio_server
    from mcp.server.models import ServerCapabilities

    # Reserve the real stdout exclusively for the JSON-RPC protocol and
    # redirect sys.stdout to stderr so stray print() calls in imported
    # modules (e.g. commons.py) cannot corrupt the protocol stream.
    protocol_stdout = anyio.wrap_file(
        TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    )
    sys.stdout = sys.stderr

    async with stdio_server(stdout=protocol_stdout) as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="gluesync-automator",
                server_version="1.0.0",
                capabilities=ServerCapabilities(
                    experimental_capabilities={},
                ),
            ),
        )


def run_stdio() -> None:
    """Entry point for stdio transport (Claude Desktop, OpenClaw, etc.)."""
    import asyncio
    # Configure logging to stderr for debugging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stderr)]
    )
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    run_stdio()

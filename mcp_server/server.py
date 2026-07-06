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
  Mapping fn : list, get base-code
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
from typing import Any, Dict, List, Optional, Sequence

import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Global token provider for SSE transport (set by Automator)
_token_provider: Optional[callable] = None
# Global refresh callback for SSE transport (set by Automator)
_refresh_callback: Optional[callable] = None

def set_token_provider(provider: callable) -> None:
    """Set a callable that returns the CoreHub token (for SSE transport)."""
    global _token_provider
    _token_provider = provider

def set_refresh_callback(callback: callable) -> None:
    """Set a callable that re-authenticates and returns a fresh token (for SSE transport)."""
    global _refresh_callback
    _refresh_callback = callback

def _find_token_file() -> Optional[Path]:
    """Locate .gluesync_mcp_token using multiple strategies.

    Claude Desktop and other MCP clients may set CWD to / or ignore the
    configured cwd, so Path(__file__) alone is not reliable.  We try:
      1. MCP_TOKEN_FILE env var (explicit override)
      2. <project_root>/.gluesync_mcp_token  (relative to this file)
      3. <cwd>/.gluesync_mcp_token           (current working directory)
      4. ~/.gluesync_mcp_token               (home directory)
    """
    candidates: list = []

    # 1. Explicit override
    env_path = os.environ.get("MCP_TOKEN_FILE")
    if env_path:
        candidates.append(Path(env_path))

    # 2. Relative to this file (works when __file__ is the full path)
    try:
        here = Path(__file__).resolve().parent.parent / ".gluesync_mcp_token"
        if here != Path("/.gluesync_mcp_token"):
            candidates.append(here)
    except Exception:
        pass

    # 3. CWD (works when the client sets the correct working directory)
    candidates.append(Path.cwd() / ".gluesync_mcp_token")

    # 4. Home directory (last resort)
    candidates.append(Path.home() / ".gluesync_mcp_token")

    for p in candidates:
        try:
            if p.exists():
                logger.info("Found token file at: %s", p)
                return p
        except Exception:
            pass

    logger.info(
        "Token file not found. Searched: %s",
        ", ".join(str(c) for c in candidates),
    )
    return None


def _read_token_from_file() -> Optional[str]:
    """Read token from .gluesync_mcp_token in the project root (written by Automator)."""
    try:
        token_file = _find_token_file()
        if token_file is None:
            return None
        with open(token_file, "r") as f:
            lines = f.readlines()
            logger.info("Token file has %d lines", len(lines))
            if lines:
                token = lines[0].strip()
                logger.info("Token read successfully (length=%d)", len(token))
                if len(lines) > 1:
                    base_url = lines[1].strip()
                    os.environ["CORE_HUB_URL"] = base_url
                    logger.info("Set CORE_HUB_URL=%s", base_url)
                return token
    except Exception as exc:
        logger.warning("Failed to read token file: %s", exc)
    return None


def _read_credentials_from_file() -> tuple:
    """Read (username, password, base_url, use_ssl, skip_verify) from the token file.

    Returns a tuple of (username, password, base_url, use_ssl, skip_verify).
    Any field that is not present in the file is returned as None.
    """
    try:
        token_file = _find_token_file()
        if token_file is None:
            return (None, None, None, None, None)
        with open(token_file, "r") as f:
            lines = f.readlines()
        username = None
        password = None
        base_url = None
        use_ssl = None
        skip_verify = None
        for line in lines[1:]:  # skip token line
            line = line.strip()
            if line.startswith("USERNAME="):
                username = line[len("USERNAME="):]
            elif line.startswith("PASSWORD="):
                password = line[len("PASSWORD="):]
            elif line.startswith("SSL_ENABLED="):
                use_ssl = line[len("SSL_ENABLED="):].lower() == "true"
            elif line.startswith("SSL_SKIP_VERIFY="):
                skip_verify = line[len("SSL_SKIP_VERIFY="):].lower() == "true"
            elif not line.startswith("SSL_") and not any(line.startswith(p) for p in ("USERNAME=", "PASSWORD=")):
                if base_url is None:
                    base_url = line
        return (username, password, base_url, use_ssl, skip_verify)
    except Exception as exc:
        logger.warning("Failed to read credentials from token file: %s", exc)
        return (None, None, None, None, None)

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


def _refresh_token() -> Optional[str]:
    """Attempt to refresh the CoreHub auth token.

    Tries the SSE refresh callback first (set by Automator), then falls back
    to re-authenticating using credentials from the token file (for stdio).
    Returns the new token on success, or None on failure.
    """
    # SSE transport: use the refresh callback set by Automator
    if _refresh_callback:
        try:
            new_token = _refresh_callback()
            if new_token:
                logger.info("Token refreshed via SSE callback")
                return new_token
        except Exception as exc:
            logger.warning("SSE token refresh callback failed: %s", exc)

    # stdio transport: re-authenticate using credentials from file
    username, password, base_url, use_ssl, skip_verify = _read_credentials_from_file()
    if username and password and base_url:
        try:
            from automator_app.corehub import authenticate
            new_token = authenticate(
                base_url=base_url,
                username=username,
                password=password,
                use_ssl=use_ssl,
                skip_verify=skip_verify,
            )
            if new_token:
                logger.info("Token refreshed via file credentials re-authentication")
                # Update the token file with the new token
                token_file = _find_token_file()
                if token_file is None:
                    token_file = Path.cwd() / ".gluesync_mcp_token"
                try:
                    with open(token_file, "r") as f:
                        old_lines = f.readlines()
                    with open(token_file, "w") as f:
                        f.write(f"{new_token}\n")
                        for line in old_lines[1:]:
                            f.write(line)
                except Exception:
                    pass
                return new_token
        except Exception as exc:
            logger.warning("File-based token refresh failed: %s", exc)

    return None


def _call(path: str, token: str, method: str = "GET", body: Optional[Dict] = None) -> Any:
    from commons import fetch_core_hub
    try:
        return fetch_core_hub(path, method=method, token=token, body=body)
    except Exception as exc:
        # Check for 401 Unauthorized - attempt token refresh and retry once
        is_401 = False
        if hasattr(exc, 'response') and exc.response is not None:
            is_401 = exc.response.status_code == 401
        if not is_401:
            raise
        logger.info("Received 401 from CoreHub, attempting token refresh...")
        new_token = _refresh_token()
        if not new_token:
            raise
        logger.info("Token refreshed, retrying request...")
        return fetch_core_hub(path, method=method, token=new_token, body=body)


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
            "Use this to fix entity configurations programmatically."
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

        elif name == "get_pipeline_snapshot_status":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/snapshot-status", token)))

        elif name == "delete_pipeline":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}", token, method="DELETE")))

        elif name == "reset_pipeline_checkpoint":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/checkpoint", token, method="PATCH")))

        elif name == "get_pipeline_entities_status":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/entities-status", token)))

        # ── Agents ─────────────────────────────────────────────────────────
        elif name == "list_pipeline_agents":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/agents", token)))

        elif name == "get_agent":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/agents/{aid}", token)))

        elif name == "get_agent_raw":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/agents/{aid}?includeSecrets=true", token)))

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

        elif name == "assign_agent":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            agent_type = arguments["agent_type"].upper()
            return _text(_fmt(_call(
                f"/pipelines/{pid}/agents/{aid}?agentType={agent_type}",
                token,
                method="PUT",
            )))

        elif name == "unassign_agent":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/agents/{aid}",
                token,
                method="DELETE",
            )))

        # ── Entities ───────────────────────────────────────────────────────
        elif name == "get_pipeline_entities":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/entities", token)))

        elif name == "get_entity":
            pid = arguments["pipeline_id"]
            eid = arguments["entity_id"]
            return _text(_fmt(_call(f"/pipelines/{pid}/entities/{eid}", token)))

        elif name == "upsert_entities":
            pid = arguments["pipeline_id"]
            entities = arguments["entities"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/config/entities",
                token,
                method="PUT",
                body={"entities": entities},
            )))

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
            if arguments.get("text_to_search"):
                parts.append(f"textToSearch={arguments['text_to_search']}")
            if arguments.get("pipeline_id"):
                parts.append(f"pipelineId={arguments['pipeline_id']}")
            if arguments.get("agent_id"):
                parts.append(f"agentId={arguments['agent_id']}")
            if arguments.get("entity_id"):
                parts.append(f"entityId={arguments['entity_id']}")
            if arguments.get("group_id"):
                parts.append(f"groupId={arguments['group_id']}")
            for lv in (arguments.get("level") or []):
                parts.append(f"level={lv}")
            if arguments.get("from_date"):
                parts.append(f"fromDate={arguments['from_date']}")
            if arguments.get("to_date"):
                parts.append(f"toDate={arguments['to_date']}")
            return _text(_fmt(_call(f"/notifications?{'&'.join(parts)}", token)))

        elif name == "get_notification":
            nid = arguments["notification_id"]
            return _text(_fmt(_call(f"/notifications/{nid}", token)))

        elif name == "get_notification_count":
            return _text(_fmt(_call("/notifications/count", token)))

        elif name == "mark_notifications_read":
            nids = arguments.get("notification_ids") or []
            if len(nids) == 1:
                # Single notification → POST /notifications/{id}/read
                return _text(_fmt(_call(
                    f"/notifications/{nids[0]}/read", token, method="POST"
                )))
            elif nids:
                # Multiple → POST /notifications/read with ids in body
                return _text(_fmt(_call(
                    "/notifications/read",
                    token,
                    method="POST",
                    body={"notificationsId": nids},
                )))
            else:
                # All → POST /notifications/read with empty body
                return _text(_fmt(_call("/notifications/read", token, method="POST")))

        # ── Mapping functions (UDF) ────────────────────────────────────────
        elif name == "list_mapping_functions":
            pid = arguments["pipeline_id"]
            entities_resp = _call(f"/pipelines/{pid}/entities", token)
            udfs: Dict[str, Dict[str, Any]] = {}

            def _scan_custom_props(obj: Any, entity_name: str) -> None:
                if isinstance(obj, dict):
                    udf_entries = obj.get("udf")
                    if isinstance(udf_entries, list):
                        for u in udf_entries:
                            if isinstance(u, dict) and u.get("name"):
                                entry = udfs.setdefault(
                                    str(u["name"]),
                                    {"name": u["name"], "type": u.get("type"), "usedByEntities": []},
                                )
                                if entity_name not in entry["usedByEntities"]:
                                    entry["usedByEntities"].append(entity_name)
                    for v in obj.values():
                        _scan_custom_props(v, entity_name)
                elif isinstance(obj, list):
                    for v in obj:
                        _scan_custom_props(v, entity_name)

            items = entities_resp if isinstance(entities_resp, list) else entities_resp.get("entities", []) if isinstance(entities_resp, dict) else []
            for item in items:
                ent = item.get("entity", item) if isinstance(item, dict) else item
                if isinstance(ent, dict):
                    ent_name = str(ent.get("entityName") or ent.get("id") or "unknown")
                    _scan_custom_props(ent, ent_name)
            return _text(_fmt(list(udfs.values())))

        elif name == "get_mapping_function_code":
            pid = arguments["pipeline_id"]
            udf_name = arguments["udf_name"]
            return _text(_fmt(_call(
                f"/pipelines/{pid}/config/entities/mapping-functions/{udf_name}",
                token,
            )))

        # ── Metrics (JSON) ───────────────────────────────────────────────
        elif name == "get_pipeline_metrics":
            pid = arguments["pipeline_id"]
            return _text(_fmt(_call(f"/metrics/{pid}", token)))

        elif name == "get_agent_metrics":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            return _text(_fmt(_call(f"/metrics/{pid}/agents/{aid}", token)))

        elif name == "get_entity_metrics":
            pid = arguments["pipeline_id"]
            eid = arguments["entity_id"]
            return _text(_fmt(_call(f"/metrics/{pid}/entities/{eid}", token)))

        elif name == "get_global_metrics":
            return _text(_fmt(_call("/metrics", token)))

        # ── Metrics (Prometheus / OpenMetrics text) ────────────────────────
        elif name == "get_prometheus_metrics":
            # The base /metrics endpoint returns Prometheus text when
            # basicMetricsRoute is mounted (via prometheusMetricsRoutes).
            raw = _call("/metrics", token)
            return _text(raw if isinstance(raw, str) else _fmt(raw))

        elif name == "get_agent_prometheus_metrics":
            pid = arguments["pipeline_id"]
            aid = arguments["agent_id"]
            raw = _call(f"/metrics/{pid}/agents/{aid}", token)
            return _text(raw if isinstance(raw, str) else _fmt(raw))

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

        # ── License ────────────────────────────────────────────────────
        elif name == "get_license_info":
            return _text(_fmt(_call("/global-config/license", token)))

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
    try:
        token = _token({})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    try:
        if uri == "gluesync://pipelines":
            return _fmt(_call("/pipelines", token))
        elif uri == "gluesync://global-config":
            return _fmt(_call("/global-config", token))
        return json.dumps({"error": f"Unknown resource URI: {uri}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── ENTRY POINT (stdio transport) ───────────────────────────────────────────

async def _run_stdio(real_stdout) -> None:
    from io import TextIOWrapper
    import anyio
    from mcp.server.stdio import stdio_server
    from mcp.server.models import ServerCapabilities

    # Use the real stdout (captured before redirect) exclusively for the
    # JSON-RPC protocol.  sys.stdout is already redirected to sys.stderr
    # by run_stdio() so stray print() calls cannot corrupt the protocol.
    protocol_stdout = anyio.wrap_file(
        TextIOWrapper(real_stdout.buffer, encoding="utf-8")
    )

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

    # Redirect sys.stdout → sys.stderr BEFORE any other imports so that
    # modules loaded lazily during tool calls (e.g. commons.py, utils/log.py)
    # capture the redirected stream and cannot corrupt the JSON-RPC protocol.
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    # Configure logging to stderr for debugging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stderr)]
    )
    asyncio.run(_run_stdio(_real_stdout))


if __name__ == "__main__":
    run_stdio()

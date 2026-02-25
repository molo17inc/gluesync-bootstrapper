# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
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

"""CoreHub authentication and entity execution helpers for the automator UI."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import shutil
import zipfile
import tempfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

import requests
from commons import (
    configure_core_hub,
    set_scheduling_enabled,
    fetch_core_hub,
    get_pipeline_agents,
    get_agent_tables,
    get_table_columns,
    extract_all_schemas_from_yaml,
    extract_schema_types_from_yaml,
)
from create_all_entities import (
    CREATE_TABLE_IF_NOT_EXISTS,
    create_entities,
    main as create_entities_main,
    set_create_table_if_not_exists,
)
from export_template_from_corehub import (
    fetch_pipeline_entities,
    build_entities_maps,
    fetch_groups_map,
    fetch_pipeline_jobs,
    build_schemas_from_entities,
    attach_schedules_from_jobs,
    build_yaml_structure,
)
import time
import yaml

logger = logging.getLogger(__name__)


class DuplicateCancelledError(RuntimeError):
    """Raised when a duplicate pipeline request is cancelled by the user."""

_AGENT_TYPE_BY_NAME: Dict[str, str] | None = None

def _get_agents_file_path() -> str:
    """Resolve path to agents.json in both dev and PyInstaller bundle."""
    import sys
    if getattr(sys, 'frozen', False):
        # Running in PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running in development
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, "agents.json")

def _write_temp_yaml(contents: str) -> str:
    """Persist YAML text to a temporary file and return its path."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".yaml")
    tmp.write(contents.encode("utf-8"))
    tmp.flush()
    tmp.close()
    return tmp.name


def _load_agent_type_catalog() -> Dict[str, str]:
    global _AGENT_TYPE_BY_NAME
    if _AGENT_TYPE_BY_NAME is not None:
        return _AGENT_TYPE_BY_NAME
    try:
        agents_file = _get_agents_file_path()
        with open(agents_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Failed to load agents catalog: {exc}") from exc
    if isinstance(data, dict):
        items = data.get("data") or data.get("items") or []
    else:
        items = data
    catalog: Dict[str, str] = {}
    if not isinstance(items, list):
        items = []
    for agent in items:
        if not isinstance(agent, dict):
            continue
        name = agent.get("internalName")
        agent_type = agent.get("type")  # Field is 'type' not 'agentType' in agents.json
        if name and agent_type:
            catalog[name.lower()] = agent_type.upper()
    _AGENT_TYPE_BY_NAME = catalog
    return catalog


def _normalize_conductor_url(conductor_url: str) -> str:
    """Ensure conductor URL points to the API root (adds /api if missing)."""
    if not conductor_url:
        return conductor_url
    normal = conductor_url.rstrip("/")
    if not normal.endswith("/api"):
        normal = f"{normal}/api"
    return normal


def _build_service_url(base_url: Optional[str], suffix: str) -> Optional[str]:
    if not base_url:
        return None
    return urljoin(base_url.rstrip('/') + '/', suffix.lstrip('/'))


def _should_verify(service_url: Optional[str], skip_verify: Optional[bool]) -> bool:
    if not service_url:
        return True
    if service_url.lower().startswith('https://') and skip_verify:
        return False
    return True


def _fetch_chronos_stats(
    base_url: Optional[str],
    *,
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    stats = {
        "available": False,
        "totalJobs": 0,
        "enabledJobs": 0,
    }
    chronos_url = _build_service_url(base_url, 'chronos')
    if not chronos_url:
        return stats

    api_url = f"{chronos_url.rstrip('/')}/api/jobs/"
    verify = _should_verify(chronos_url, skip_verify)
    try:
        response = requests.get(api_url, timeout=5, verify=verify)
        response.raise_for_status()
        payload = response.json()

        jobs_payload: Any = None
        if isinstance(payload, list):
            jobs_payload = payload
        elif isinstance(payload, dict):
            # Chronos Scheduler returns {"items": [...], "total": N}
            if isinstance(payload.get("items"), list):
                jobs_payload = payload["items"]
                if isinstance(payload.get("total"), int):
                    stats["totalJobs"] = payload["total"]
            elif isinstance(payload.get("data"), list):
                jobs_payload = payload["data"]

        if isinstance(jobs_payload, list):
            stats["available"] = True
            if stats["totalJobs"] == 0:
                stats["totalJobs"] = len(jobs_payload)
            stats["enabledJobs"] = sum(1 for job in jobs_payload if job.get("enabled", True))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch Chronos jobs: %s", exc)
    return stats


def _fetch_conductor_stats(
    base_url: Optional[str],
    *,
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    stats = {
        "available": False,
        "runningContainers": 0,
        "totalContainers": 0,
    }
    conductor_url = _build_service_url(base_url, 'conductor')
    if not conductor_url:
        return stats

    api_url = f"{conductor_url.rstrip('/')}/api/containers"
    verify = _should_verify(conductor_url, skip_verify)
    try:
        response = requests.get(api_url, timeout=5, verify=verify)
        response.raise_for_status()
        payload = response.json()
        containers = []
        if isinstance(payload, dict):
            if payload.get("success") and isinstance(payload.get("data"), dict):
                containers = payload["data"].get("containers") or []
            elif isinstance(payload.get("containers"), list):
                containers = payload["containers"]

        stats["available"] = True
        stats["totalContainers"] = len(containers)
        stats["runningContainers"] = sum(
            1
            for container in containers
            if isinstance(container, dict)
            and isinstance(container.get("info"), dict)
            and container["info"].get("state") == "running"
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch Conductor containers: %s", exc)
    return stats


def authenticate(
    *,
    base_url: str,
    username: str,
    password: str,
    use_ssl: Optional[bool] = None,
    skip_verify: Optional[bool] = None,
) -> str:
    """Authenticate against CoreHub and return an API token."""

    client = configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)
    logger.info("Configured CoreHub client for authentication")

    response = client.request(
        "/authentication/login",
        method="POST",
        body={"username": username, "password": password},
    )

    token = response.get("apiToken") if isinstance(response, dict) else None
    if not token:
        raise RuntimeError("Authentication failed: no token returned")

    logger.info("Authentication successful for user %s", username)
    return token


def run_create_entities(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    source_schema: str,
    target_schema: str,
    source_type: str,
    target_type: str,
    yaml_file: str,
    skip_errors: bool,
    chunk_size: int,
    enable_scheduling: bool,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Invoke create_all_entities.main and capture output."""

    set_scheduling_enabled(enable_scheduling)
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    prev_env_create_tables = os.environ.get('CREATE_TABLE_IF_NOT_EXISTS')
    prev_flag_create_tables = CREATE_TABLE_IF_NOT_EXISTS
    set_create_table_if_not_exists(create_tables)
    os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = 'true' if create_tables else 'false'

    handler = None
    root_logger = logging.getLogger()
    if log_callback is not None:
        class CallbackHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
                message = record.getMessage()
                if message:
                    log_callback(message)

        handler = CallbackHandler(level=logging.INFO)
        root_logger.addHandler(handler)

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            create_entities_main(
                pipeline_id,
                source_schema,
                target_schema,
                source_type,
                target_type,
                yaml_file,
                token,
                skip_errors,
                chunk_size,
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("create_all_entities execution failed")
        if handler:
            root_logger.removeHandler(handler)
        return {
            "success": False,
            "logs": buffer.getvalue().splitlines(),
            "error": str(exc),
        }
    finally:
        if handler:
            root_logger.removeHandler(handler)
        if prev_env_create_tables is not None:
            os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = prev_env_create_tables
        else:
            os.environ.pop('CREATE_TABLE_IF_NOT_EXISTS', None)
        set_create_table_if_not_exists(prev_flag_create_tables)

    if log_callback is not None:
        for line in buffer.getvalue().splitlines():
            log_callback(line)
    return {
        "success": True,
        "logs": buffer.getvalue().splitlines(),
    }


def run_create_entities_for_tables(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    source_schema: str,
    target_schema: str,
    source_type: str,
    target_type: str,
    table_names: list[str],
    skip_errors: bool,
    chunk_size: int,
    enable_scheduling: bool,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Create entities only for the specified source tables.

    This is a bulk helper used by the Automator "Bulk operations" UI. It
    mirrors run_create_entities, but restricts the processing to the
    user-selected subset of tables instead of all discovered tables.
    """

    set_scheduling_enabled(enable_scheduling)
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Resolve source/target types from agents.json catalog (same as pipeline backup)
    inferred_source_type, inferred_target_type = infer_agent_schema_types(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )
    if inferred_target_type and inferred_target_type.upper() == "NOSQL":
        logger.info(
            "Pipeline %s has a NoSQL target (%s) - disabling table creation",
            pipeline_id,
            inferred_target_type,
        )
        create_tables = False
    # Override the caller-supplied types with the authoritative inferred values
    if inferred_source_type:
        source_type = inferred_source_type
    if inferred_target_type:
        target_type = inferred_target_type

    prev_env_create_tables = os.environ.get("CREATE_TABLE_IF_NOT_EXISTS")
    prev_flag_create_tables = CREATE_TABLE_IF_NOT_EXISTS
    set_create_table_if_not_exists(create_tables)
    os.environ["CREATE_TABLE_IF_NOT_EXISTS"] = "true" if create_tables else "false"

    buffer = io.StringIO()

    try:
        # Discover source/target agents for the pipeline
        agents = get_pipeline_agents(token, pipeline_id)
        if not isinstance(agents, list):
            raise RuntimeError(f"Unexpected agents payload for pipeline {pipeline_id!r}: {agents!r}")

        source_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "SOURCE"), None)
        target_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "TARGET"), None)
        if not source_agent or not target_agent:
            raise RuntimeError(
                f"Could not find required agents. Source ({source_type}): {source_agent}, Target ({target_type}): {target_agent}"
            )

        raw_source_id = source_agent.get("agentId") or source_agent.get("id")
        raw_target_id = target_agent.get("agentId") or target_agent.get("id")
        if not raw_source_id or not raw_target_id:
            raise RuntimeError("Source/target agents are missing IDs")

        # Discover all tables for this schema, then restrict to the selected ones
        discovered_tables = get_agent_tables(token, pipeline_id, raw_source_id, source_schema)
        if not isinstance(discovered_tables, list):
            raise RuntimeError(f"Unexpected tables payload for {source_schema!r}: {discovered_tables!r}")

        wanted = {t.lower() for t in table_names}
        filtered_tables: list[Any] = []
        for tbl in discovered_tables:
            name: Optional[str] = None
            if isinstance(tbl, str):
                name = tbl
            elif isinstance(tbl, dict):
                name = tbl.get("name") or tbl.get("tableName")
            if not name:
                continue
            if name.lower() in wanted:
                filtered_tables.append(tbl)

        if not filtered_tables:
            raise RuntimeError(
                f"No matching tables found in discovery for schema {source_schema!r} and selection {sorted(table_names)!r}"
            )

        # Invoke the lower-level create_entities helper directly so we can
        # pass the filtered tables list. YAML configuration is optional here;
        # when omitted, create_entities will use discovery defaults.
        with redirect_stdout(buffer):
            result = create_entities(
                token,
                pipeline_id,
                source_schema,
                target_schema,
                filtered_tables,
                raw_source_id,
                raw_target_id,
                source_type,
                target_type,
                yaml_config=None,
                skip_errors=skip_errors,
                chunk_size=chunk_size,
            )

        logs = buffer.getvalue().splitlines()
        success = not result or not result.get("failed")
        payload: Dict[str, Any] = {"success": success, "logs": logs, "result": result or {}}
        if not success:
            payload["error"] = f"Some entities failed: {result.get('failed')} out of {result.get('total')}"
        return payload

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Bulk create_entities_for_tables execution failed")
        # Extract full CoreHub exceptionMessage from HTTPError responses
        error_detail = str(exc)
        try:
            import requests as _requests
            if isinstance(exc, _requests.exceptions.HTTPError) and exc.response is not None:
                body = exc.response.json()
                corehub_msg = body.get("exceptionMessage") or body.get("message")
                if corehub_msg:
                    error_detail = f"{exc}: {corehub_msg}"
        except Exception:
            pass
        logs = buffer.getvalue().splitlines()
        logs.append(f"ERROR: {error_detail}")
        return {"success": False, "logs": logs, "error": error_detail}
    finally:
        if prev_env_create_tables is not None:
            os.environ["CREATE_TABLE_IF_NOT_EXISTS"] = prev_env_create_tables
        else:
            os.environ.pop("CREATE_TABLE_IF_NOT_EXISTS", None)
        set_create_table_if_not_exists(prev_flag_create_tables)


def _get_source_agent(token: str, pipeline_id: str) -> Dict[str, Any]:
    """Return the SOURCE agent definition for a pipeline.

    Raises RuntimeError if a suitable agent cannot be found.
    """

    agents = get_pipeline_agents(token, pipeline_id)
    if not isinstance(agents, list):
        raise RuntimeError(f"Unexpected agents payload for pipeline {pipeline_id!r}: {agents!r}")

    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("agentType") == "SOURCE":
            return agent

    raise RuntimeError(f"No SOURCE agent found for pipeline {pipeline_id!r}")


def list_source_schemas(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[str]:
    """List available schemas from the SOURCE agent for a pipeline."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    source_agent = _get_source_agent(token, pipeline_id)
    raw_agent_id = source_agent.get("agentId") or source_agent.get("id")
    if not raw_agent_id:
        raise RuntimeError(f"SOURCE agent for pipeline {pipeline_id!r} is missing an ID")

    response = fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{raw_agent_id}/discovery/schemas",
        token=token,
    )

    items: list[Any]
    if isinstance(response, dict):
        items = response.get("schemas") or response.get("items") or []
    elif isinstance(response, list):
        items = response
    else:
        logger.warning("Unexpected schemas discovery response for pipeline %s: %r", pipeline_id, response)
        return []

    schemas: list[str] = []
    for item in items:
        name: Optional[str] = None
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = (
                item.get("schema")
                or item.get("schemaName")
                or item.get("name")
            )
        if name:
            schemas.append(str(name))

    return schemas


def list_source_tables(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    schema: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[str]:
    """List available tables from the SOURCE agent for a given schema."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    source_agent = _get_source_agent(token, pipeline_id)
    raw_agent_id = source_agent.get("agentId") or source_agent.get("id")
    if not raw_agent_id:
        raise RuntimeError(f"SOURCE agent for pipeline {pipeline_id!r} is missing an ID")

    tables = get_agent_tables(token, pipeline_id, raw_agent_id, schema)

    names: list[str] = []
    if isinstance(tables, list):
        for tbl in tables:
            if isinstance(tbl, str):
                name = tbl
            elif isinstance(tbl, dict):
                name = tbl.get("name") or tbl.get("tableName")
            else:
                continue
            if name:
                names.append(str(name))
    else:
        logger.warning("Unexpected tables discovery response for pipeline %s schema %s: %r", pipeline_id, schema, tables)

    return names


def discover_primary_key_names(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    schema: str,
    table_name: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[str]:
    """Return primary key column names for a given table using discovery.

    This mirrors the fallback behavior in create_all_entities when no explicit
    keys are provided in YAML: primary key columns (isPrimaryKey=true) are
    treated as the entity keys.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    source_agent = _get_source_agent(token, pipeline_id)
    raw_agent_id = source_agent.get("agentId") or source_agent.get("id")
    if not raw_agent_id:
        raise RuntimeError(f"SOURCE agent for pipeline {pipeline_id!r} is missing an ID")

    columns = get_table_columns(token, pipeline_id, raw_agent_id, schema, table_name)

    if not isinstance(columns, dict):
        logger.warning(
            "Unexpected columns discovery response for pipeline %s schema %s table %s: %r",
            pipeline_id,
            schema,
            table_name,
            columns,
        )
        return []

    raw_cols = columns.get("columns")
    if not isinstance(raw_cols, list):
        logger.warning(
            "Columns discovery payload missing 'columns' list for pipeline %s schema %s table %s: %r",
            pipeline_id,
            schema,
            table_name,
            columns,
        )
        return []

    seen: set[str] = set()
    pk_names: list[str] = []
    for col in raw_cols:
        if not isinstance(col, dict):
            continue
        if not col.get("isPrimaryKey"):
            continue
        name = col.get("name")
        if not name:
            continue
        text = str(name)
        if text in seen:
            continue
        seen.add(text)
        pk_names.append(text)

    return pk_names


def discover_column_metadata_for_table(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    schema: str,
    table_name: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[Dict[str, Any]]:
    """Return column metadata suitable for YAML templates for a given table.

    The shape of each entry matches what create_all_tables.py expects when
    using metadata-based column definitions: name, type, dataLength,
    numericPrecision, numericScale, isNullable, id and ordinalPosition.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    source_agent = _get_source_agent(token, pipeline_id)
    raw_agent_id = source_agent.get("agentId") or source_agent.get("id")
    if not raw_agent_id:
        raise RuntimeError(f"SOURCE agent for pipeline {pipeline_id!r} is missing an ID")

    columns = get_table_columns(token, pipeline_id, raw_agent_id, schema, table_name)

    if not isinstance(columns, dict):
        logger.warning(
            "Unexpected columns discovery response for pipeline %s schema %s table %s: %r",
            pipeline_id,
            schema,
            table_name,
            columns,
        )
        return []

    raw_cols = columns.get("columns")
    if not isinstance(raw_cols, list):
        logger.warning(
            "Columns discovery payload missing 'columns' list for pipeline %s schema %s table %s: %r",
            pipeline_id,
            schema,
            table_name,
            columns,
        )
        return []

    metadata: list[Dict[str, Any]] = []
    for idx, col in enumerate(raw_cols, 1):
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if not name:
            continue

        # Use the actual id from the API response
        col_id = col.get("id")
        if col_id is None:
            logger.warning(
                "Column '%s' in table %s.%s is missing 'id' field in API response, using fallback index %d",
                name,
                schema,
                table_name,
                idx
            )
            col_id = idx
        
        # Use ordinalPosition from API if available, otherwise use id
        ordinal = col.get("ordinalPosition", col_id)

        meta: Dict[str, Any] = {
            "name": name,
            "type": col.get("type"),
            "dataLength": col.get("dataLength", 0),
            "numericPrecision": col.get("numericPrecision", 0),
            "numericScale": col.get("numericScale", 0),
            "isNullable": col.get("isNullable", False),
            "id": col_id,
            "ordinalPosition": ordinal,
        }
        metadata.append(meta)

    return metadata


def infer_agent_schema_types(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> tuple[Optional[str], Optional[str]]:
    """Infer (sourceType, targetType) as "SQL"/"NoSQL" from pipeline agents.

    This is a best-effort helper used by the Automator UI for bulk operations.
    When types cannot be determined, it falls back to ("SQL", "SQL").
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    agents = get_pipeline_agents(token, pipeline_id)
    if not isinstance(agents, list):
        return "SQL", "SQL"

    catalog = _load_agent_type_catalog()

    def _classify_tag(tag: str) -> str:
        key = (tag or "").lower()
        if not key:
            return "SQL"

        if key in catalog:
            return catalog[key]

        # If the agent tag is unknown to the catalog, fall back to SQL.
        return "SQL"

    source_agent = next(
        (a for a in agents if isinstance(a, dict) and a.get("agentType") == "SOURCE"),
        None,
    )
    target_agent = next(
        (a for a in agents if isinstance(a, dict) and a.get("agentType") == "TARGET"),
        None,
    )

    source_tag = str(source_agent.get("agentTag")) if isinstance(source_agent, dict) else ""
    target_tag = str(target_agent.get("agentTag")) if isinstance(target_agent, dict) else ""

    return _classify_tag(source_tag), _classify_tag(target_tag)


def list_pipelines(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[Dict[str, Any]]:
    """Return a normalized list of pipelines for the export UI."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    response = fetch_core_hub("/pipelines", token=token)
    pipelines: list[Dict[str, Any]] = []

    if isinstance(response, list):
        for item in response:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("pipelineId") or item.get("id") or item.get("pipeline_id")
            if raw_id is None:
                continue
            pid = str(raw_id)
            name = item.get("name")
            description = item.get("description")
            pipelines.append(
                {
                    "id": pid,
                    "name": str(name) if name is not None else None,
                    "description": str(description) if description is not None else None,
                }
            )

    return pipelines


def get_environment_summary(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Return aggregated CoreHub information for the overview tab."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    pipelines = list_pipelines(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )

    totals = {
        "pipelines": len(pipelines),
        "agents": 0,
        "entities": 0,
    }
    pipeline_details: list[Dict[str, Any]] = []

    for pipeline in pipelines:
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            continue

        summary: Dict[str, Any] = {
            "id": pipeline_id,
            "name": pipeline.get("name"),
            "description": pipeline.get("description"),
            "agentCount": 0,
            "entityCount": 0,
            "agents": [],
        }

        # Collect agent information from pipeline config
        try:
            pipeline_config = fetch_core_hub(
                f"/pipelines/{pipeline_id}/config",
                token=token,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch pipeline config for overview (pipeline=%s): %s", pipeline_id, exc)
            pipeline_config = None

        if isinstance(pipeline_config, dict):
            agents = []
            for agent in pipeline_config.get("agents") or []:
                if not isinstance(agent, dict):
                    continue
                agents.append(
                    {
                        "tag": agent.get("agentTag"),
                        "type": agent.get("agentType"),
                        "status": agent.get("status"),
                    }
                )
            summary["agents"] = agents
            summary["agentCount"] = len(agents)
            totals["agents"] += len(agents)

        # Count entities for the pipeline
        try:
            entities = fetch_pipeline_entities(token, pipeline_id)
            entity_count = len(entities) if isinstance(entities, list) else 0
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch entities for overview (pipeline=%s): %s", pipeline_id, exc)
            entity_count = 0

        summary["entityCount"] = entity_count
        totals["entities"] += entity_count

        pipeline_details.append(summary)

    chronos_stats = _fetch_chronos_stats(base_url, skip_verify=skip_verify)
    conductor_stats = _fetch_conductor_stats(base_url, skip_verify=skip_verify)
    totals["schedules"] = chronos_stats.get("enabledJobs", 0)
    totals["runningContainers"] = conductor_stats.get("runningContainers", 0)

    return {
        "totals": totals,
        "pipelines": pipeline_details,
        "services": {
            "chronos": chronos_stats,
            "conductor": conductor_stats,
        },
    }


def export_pipeline_yaml(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> str:
    """Export a single pipeline configuration to YAML text for download."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    entities = fetch_pipeline_entities(token, pipeline_id)
    entities_by_id = build_entities_maps(entities)
    group_id_to_name, _, groups_by_name = fetch_groups_map(token, pipeline_id)
    schemas = build_schemas_from_entities(entities, group_id_to_name)

    # Align schema-level type hints with agents.json-based inference used elsewhere
    source_type, target_type = infer_agent_schema_types(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )
    for schema_cfg in schemas.values():
        schema_cfg["sourceType"] = source_type
        schema_cfg["targetType"] = target_type

    jobs = fetch_pipeline_jobs(pipeline_id)
    attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

    yaml_data = build_yaml_structure(schemas, groups_by_name)

    buffer = io.StringIO()
    yaml.safe_dump(
        yaml_data or {},
        buffer,
        sort_keys=False,
        allow_unicode=True,
    )
    return buffer.getvalue()


def export_pipeline_full_backup(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> bytes:
    """Export a full backup (YAML + agents-config + UDFs) for a single pipeline."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    pipeline_name = pipeline_id
    try:
        response = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to fetch pipeline %s metadata", pipeline_id)
        raise
    if isinstance(response, dict):
        name = response.get("name")
        if isinstance(name, str) and name:
            pipeline_name = name

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        all_agents: list[dict] = []
        seen_agents: set[tuple] = set()
        pipelines_meta: list[dict] = []
        udfs_to_export: Dict[str, Dict[str, Any]] = {}

        # Top-level folder for this pipeline inside the ZIP
        root_prefix = f"pipeline_{pipeline_id}/"

        pipeline_meta: Dict[str, Any] = {
            "pipelineId": pipeline_id,
            "pipelineName": pipeline_name,
        }
        pipelines_meta.append(pipeline_meta)

        # Export pipeline YAML configuration
        yaml_content = export_pipeline_yaml(
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
        )

        safe_name = "".join(c for c in pipeline_name if c.isalnum() or c in (" ", "-", "_")).rstrip()
        if not safe_name:
            safe_name = pipeline_id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{root_prefix}backup_{safe_name}_{pipeline_id}_{timestamp}.yaml"
        zip_file.writestr(filename, yaml_content)
        logger.info("Exported pipeline %s (%s) YAML", pipeline_name, pipeline_id)

        # Discover mapping functions (UDFs) referenced by this pipeline
        try:
            entities = fetch_pipeline_entities(token, pipeline_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch entities for UDF export in pipeline %s: %s", pipeline_id, exc)
            entities = []

        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue

                # Find the target agent entity for this logical entity
                target_ae: Optional[Dict[str, Any]] = None
                for ae in ent.get("agentEntities", []) or []:
                    et = ae.get("entityType") or {}
                    if et.get("type") == "Target":
                        target_ae = ae
                        break
                if not target_ae:
                    continue

                target_et = target_ae.get("entityType") or {}

                # Preferred: single mappingFunctionInfo entry
                mf_info = target_et.get("mappingFunctionInfo")
                if isinstance(mf_info, dict):
                    udf_name = mf_info.get("name")
                    if udf_name:
                        key = str(udf_name)
                        if key not in udfs_to_export:
                            raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
                            udfs_to_export[key] = {
                                "name": str(udf_name),
                                "type": mf_info.get("type"),
                                "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
                            }
                    continue

                # Backwards-compat: array-style "udf" configuration on entityType
                udf_cfg = target_et.get("udf") or []
                if isinstance(udf_cfg, list):
                    for udf in udf_cfg:
                        if not isinstance(udf, dict):
                            continue
                        udf_name = udf.get("name")
                        if not udf_name:
                            continue
                        key = str(udf_name)
                        if key in udfs_to_export:
                            continue
                        raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
                        udfs_to_export[key] = {
                            "name": str(udf_name),
                            "type": udf.get("type"),
                            "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
                        }

        # Collect agents for this pipeline
        try:
            pipeline_agents = get_pipeline_agents(token, pipeline_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch agents for pipeline %s: %s", pipeline_id, exc)
            pipeline_agents = []

        if isinstance(pipeline_agents, list):
            pipeline_agent_refs: list[dict] = []
            for agent in pipeline_agents:
                if not isinstance(agent, dict):
                    continue

                agent_type = agent.get("agentType")
                agent_tag = agent.get("agentTag")
                host_credentials = agent.get("hostCredentials") or {}
                raw_agent_id = agent.get("agentId") or agent.get("id")

                if not agent_type or not agent_tag or not host_credentials:
                    continue

                ref: Dict[str, Any] = {
                    "agentType": agent_type,
                    "agentTag": agent_tag,
                }
                if raw_agent_id is not None:
                    ref["agentId"] = str(raw_agent_id)
                pipeline_agent_refs.append(ref)

                if raw_agent_id is not None:
                    key = ("id", str(raw_agent_id))
                else:
                    key = (
                        "props",
                        str(agent_type),
                        str(agent_tag),
                        str(host_credentials.get("connectionName")),
                        str(host_credentials.get("host")),
                        str(host_credentials.get("port")),
                    )
                if key in seen_agents:
                    continue
                seen_agents.add(key)

                masked_host_credentials = dict(host_credentials)
                if "password" in masked_host_credentials and masked_host_credentials["password"]:
                    masked_host_credentials["password"] = "*******"

                agent_payload: Dict[str, Any] = {
                    "agentType": agent_type,
                    "agentTag": agent_tag,
                    "hostCredentials": masked_host_credentials,
                    "customHostCredentials": agent.get("customHostCredentials") or {},
                    "specificConfiguration": agent.get("specificConfiguration") or {},
                }
                if raw_agent_id is not None:
                    agent_payload["agentId"] = str(raw_agent_id)
                all_agents.append(agent_payload)

            if pipeline_agent_refs:
                pipeline_meta["agents"] = pipeline_agent_refs

        if all_agents:
            agents_payload: Dict[str, Any] = {"agents": all_agents}
            if pipelines_meta:
                agents_payload["pipelines"] = pipelines_meta

            buffer = io.StringIO()
            yaml.safe_dump(
                agents_payload,
                buffer,
                sort_keys=False,
                allow_unicode=True,
            )

            yaml_text_lines = []
            for line in buffer.getvalue().splitlines():
                stripped = line.lstrip()
                if stripped.startswith("password:") and "*******" in stripped:
                    line = f"{line}  # original password omitted"
                yaml_text_lines.append(line)

            yaml_text = "\n".join(yaml_text_lines) + "\n"
            zip_file.writestr(f"{root_prefix}agents-config.yaml", yaml_text)

        # Export mapping function (UDF) source files, if any were discovered.
        for udf_name, meta in udfs_to_export.items():
            try:
                response = fetch_core_hub(
                    f"/pipelines/{pipeline_id}/config/entities/mapping-functions/{udf_name}",
                    token=token,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to fetch mapping function %s for pipeline %s: %s", udf_name, pipeline_id, exc)
                continue

            if not isinstance(response, dict):
                logger.warning(
                    "Unexpected response while fetching mapping function %s for pipeline %s: %r",
                    udf_name,
                    pipeline_id,
                    response,
                )
                continue

            code = response.get("code")
            mf_type = response.get("type") or meta.get("type")
            if not isinstance(code, str) or not code:
                logger.warning("Mapping function %s for pipeline %s has no code to export", udf_name, pipeline_id)
                continue

            # Determine file extension based on mapping function type.
            # Mapping aligned with MappingFunctionsType in CoreHub:
            # - Java      -> .java
            # - Kotlin    -> .kt
            # - Python    -> .js
            # - Javascript-> .py
            # - Ruby      -> .rb
            ext = ".java"
            if isinstance(mf_type, str):
                t = mf_type.lower()
                if t == "java":
                    ext = ".java"
                elif t == "kotlin":
                    ext = ".kt"
                elif t == "python":
                    ext = ".js"
                elif t == "javascript":
                    ext = ".py"
                elif t == "ruby":
                    ext = ".rb"

            safe_udf_name = "".join(c for c in str(udf_name) if c.isalnum() or c in ("_", "-")) or "udf"
            # Place each UDF under a folder named 'udf-<agentId>', with the file
            # named as the UDF name plus extension so it can be reused with the
            # UDF_PATH-based lookup used by create_user_defined_functions.
            agent_id = meta.get("agentId")
            if agent_id is not None:
                safe_agent_id = "".join(c for c in str(agent_id) if c.isalnum() or c in ("_", "-")) or "unknown"
                folder = f"udf-{safe_agent_id}"
            else:
                folder = "udf-unknown"
            udf_filename = f"{root_prefix}{folder}/{safe_udf_name}{ext}"

            zip_file.writestr(udf_filename, code.encode("utf-8"))

    zip_buffer.seek(0)
    return zip_buffer.read()


def export_all_pipelines_yaml(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> bytes:
    """Export all pipelines to a ZIP file containing individual YAML files."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Get all pipelines
    pipelines = list_pipelines(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )

    # Create ZIP file in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Accumulate agent configurations across pipelines
        all_agents: list[dict] = []
        seen_agents: set[tuple] = set()
        pipelines_meta: list[dict] = []

        # Track mapping functions (UDFs) referenced by entities so we can export
        # their source code once per (pipeline, udfName) pair.
        # Key: (pipeline_id, udf_name) -> {"name": str, "type": Optional[Any]}
        udfs_to_export: Dict[tuple[str, str], Dict[str, Any]] = {}

        for pipeline in pipelines:
            pipeline_id = pipeline['id']
            pipeline_name = pipeline.get('name', pipeline_id)

            pipeline_meta: Dict[str, Any] = {
                "pipelineId": pipeline_id,
                "pipelineName": pipeline_name,
            }
            pipelines_meta.append(pipeline_meta)

            try:
                # Export individual pipeline
                yaml_content = export_pipeline_yaml(
                    token=token,
                    base_url=base_url,
                    pipeline_id=pipeline_id,
                    use_ssl=use_ssl,
                    skip_verify=skip_verify,
                )

                # Create filename with pipeline name if available
                safe_name = "".join(c for c in pipeline_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
                if not safe_name:
                    safe_name = pipeline_id
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"backup_{safe_name}_{pipeline_id}_{timestamp}.yaml"

                # Add to ZIP
                zip_file.writestr(filename, yaml_content)
                logger.info("Exported pipeline %s (%s)", pipeline_name, pipeline_id)

            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to export pipeline %s: %s", pipeline_id, exc)
                # Continue with other pipelines

            # Discover mapping functions (UDFs) referenced by this pipeline so we
            # can export their source code as part of the full backup.
            try:
                entities = fetch_pipeline_entities(token, pipeline_id)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to fetch entities for UDF export in pipeline %s: %s", pipeline_id, exc)
                entities = []

            if isinstance(entities, list):
                for ent in entities:
                    if not isinstance(ent, dict):
                        continue

                    # Find the target agent entity for this logical entity
                    target_ae: Optional[Dict[str, Any]] = None
                    for ae in ent.get("agentEntities", []) or []:
                        et = ae.get("entityType") or {}
                        if et.get("type") == "Target":
                            target_ae = ae
                            break
                    if not target_ae:
                        continue

                    target_et = target_ae.get("entityType") or {}

                    # Preferred: single mappingFunctionInfo entry
                    mf_info = target_et.get("mappingFunctionInfo")
                    if isinstance(mf_info, dict):
                        udf_name = mf_info.get("name")
                        if udf_name:
                            key = (pipeline_id, str(udf_name))
                            if key not in udfs_to_export:
                                raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
                                udfs_to_export[key] = {
                                    "name": str(udf_name),
                                    "type": mf_info.get("type"),
                                    "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
                                }
                        continue

                    # Backwards-compat: array-style "udf" configuration on entityType
                    udf_cfg = target_et.get("udf") or []
                    if isinstance(udf_cfg, list):
                        for udf in udf_cfg:
                            if not isinstance(udf, dict):
                                continue
                            udf_name = udf.get("name")
                            if not udf_name:
                                continue
                            key = (pipeline_id, str(udf_name))
                            if key in udfs_to_export:
                                continue
                            raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
                            udfs_to_export[key] = {
                                "name": str(udf_name),
                                "type": udf.get("type"),
                                "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
                            }

            # Try to collect agents for this pipeline
            try:
                pipeline_agents = get_pipeline_agents(token, pipeline_id)
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to fetch agents for pipeline %s: %s", pipeline_id, exc)
                pipeline_agents = []

            if isinstance(pipeline_agents, list):
                # Per-pipeline agent references (by type/tag)
                pipeline_agent_refs: list[dict] = []
                for agent in pipeline_agents:
                    if not isinstance(agent, dict):
                        continue

                    agent_type = agent.get("agentType")
                    agent_tag = agent.get("agentTag")
                    host_credentials = agent.get("hostCredentials") or {}
                    raw_agent_id = agent.get("agentId") or agent.get("id")

                    if not agent_type or not agent_tag or not host_credentials:
                        continue

                    # Add lightweight reference for this pipeline
                    ref: Dict[str, Any] = {
                        "agentType": agent_type,
                        "agentTag": agent_tag,
                    }
                    if raw_agent_id is not None:
                        ref["agentId"] = str(raw_agent_id)
                    pipeline_agent_refs.append(ref)

                    # Deduplicate primarily by agentId when available, otherwise by
                    # a composite key of type, tag and basic connection details
                    if raw_agent_id is not None:
                        key = ("id", str(raw_agent_id))
                    else:
                        key = (
                            "props",
                            str(agent_type),
                            str(agent_tag),
                            str(host_credentials.get("connectionName")),
                            str(host_credentials.get("host")),
                            str(host_credentials.get("port")),
                        )
                    if key in seen_agents:
                        continue
                    seen_agents.add(key)

                    # Mask secrets when exporting
                    masked_host_credentials = dict(host_credentials)
                    if "password" in masked_host_credentials and masked_host_credentials["password"]:
                        masked_host_credentials["password"] = "*******"

                    agent_payload: Dict[str, Any] = {
                        "agentType": agent_type,
                        "agentTag": agent_tag,
                        "hostCredentials": masked_host_credentials,
                        # Keep the same naming as config.json/example-config.json
                        "customHostCredentials": agent.get("customHostCredentials") or {},
                        "specificConfiguration": agent.get("specificConfiguration") or {},
                    }
                    if raw_agent_id is not None:
                        agent_payload["agentId"] = str(raw_agent_id)
                    all_agents.append(agent_payload)

                if pipeline_agent_refs:
                    pipeline_meta["agents"] = pipeline_agent_refs

        # After processing all pipelines, write a consolidated agents-config.yaml
        # if any agents were found, and export UDF source files when present.
        if all_agents:
            agents_payload: Dict[str, Any] = {"agents": all_agents}
            if pipelines_meta:
                agents_payload["pipelines"] = pipelines_meta

            buffer = io.StringIO()
            yaml.safe_dump(
                agents_payload,
                buffer,
                sort_keys=False,
                allow_unicode=True,
            )

            # Add inline comments to make password obfuscation clear
            yaml_text_lines = []
            for line in buffer.getvalue().splitlines():
                stripped = line.lstrip()
                if stripped.startswith("password:") and "*******" in stripped:
                    # Preserve indentation when appending the comment
                    line = f"{line}  # original password omitted"
                yaml_text_lines.append(line)

            yaml_text = "\n".join(yaml_text_lines) + "\n"
            zip_file.writestr("agents-config.yaml", yaml_text)

        # Export mapping function (UDF) source files, if any were discovered.
        for (pipeline_id, udf_name), meta in udfs_to_export.items():
            try:
                response = fetch_core_hub(
                    f"/pipelines/{pipeline_id}/config/entities/mapping-functions/{udf_name}",
                    token=token,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to fetch mapping function %s for pipeline %s: %s", udf_name, pipeline_id, exc)
                continue

            if not isinstance(response, dict):
                logger.warning(
                    "Unexpected response while fetching mapping function %s for pipeline %s: %r",
                    udf_name,
                    pipeline_id,
                    response,
                )
                continue

            code = response.get("code")
            mf_type = response.get("type") or meta.get("type")
            if not isinstance(code, str) or not code:
                logger.warning("Mapping function %s for pipeline %s has no code to export", udf_name, pipeline_id)
                continue

            # Determine file extension based on mapping function type.
            # Mapping aligned with MappingFunctionsType in CoreHub:
            # - Java      -> .java
            # - Kotlin    -> .kt
            # - Python    -> .js
            # - Javascript-> .py
            # - Ruby      -> .rb
            ext = ".java"
            if isinstance(mf_type, str):
                t = mf_type.lower()
                if t == "java":
                    ext = ".java"
                elif t == "kotlin":
                    ext = ".kt"
                elif t == "python":
                    ext = ".js"
                elif t == "javascript":
                    ext = ".py"
                elif t == "ruby":
                    ext = ".rb"

            safe_udf_name = "".join(c for c in str(udf_name) if c.isalnum() or c in ("_", "-")) or "udf"
            # Place each UDF under a folder named 'udf-<agentId>', with the file
            # named as the UDF name plus extension so it can be reused with the
            # UDF_PATH-based lookup used by create_user_defined_functions.
            agent_id = meta.get("agentId")
            if agent_id is not None:
                safe_agent_id = "".join(c for c in str(agent_id) if c.isalnum() or c in ("_", "-")) or "unknown"
                folder = f"udf-{safe_agent_id}"
            else:
                folder = "udf-unknown"
            udf_filename = f"{folder}/{safe_udf_name}{ext}"

            # Store source code as UTF-8 text inside the ZIP
            zip_file.writestr(udf_filename, code.encode("utf-8"))

    zip_buffer.seek(0)
    return zip_buffer.read()


def import_pipeline_config_only(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a pipeline and bind agents from a single config object.

    The config is expected to look like example-config.json (or its YAML
    equivalent):

    {
      "pipelineName": "demo",
      "pipelineId": "",   # optional, metadata only
      "agents": [
        { "agentType": "SOURCE", "agentTag": "...", ... },
        { "agentType": "TARGET", "agentTag": "...", ... }
      ]
    }

    This function:
    - Creates a new pipeline in Core Hub (never reuses an existing one).
    - Attaches matching unassigned agents by (agentType, agentTag).
    - Applies host credentials and specificConfiguration for each agent.
    - Does NOT create entities or start any syncs.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    if not isinstance(config, dict):
        raise RuntimeError("Invalid config format: expected an object")

    agents_cfg = config.get("agents")
    if not isinstance(agents_cfg, list) or not agents_cfg:
        raise RuntimeError("Config must contain a non-empty 'agents' list")

    # Determine base pipeline name from config or fall back to a generic label
    base_name = config.get("pipelineName") or "Imported pipeline"

    # Fetch existing pipelines to avoid name collisions
    existing = fetch_core_hub("/pipelines", token=token)
    existing_names = set()
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    existing_names.add(name)

    pipeline_name = base_name
    if pipeline_name in existing_names:
        # Apply '(restored)', '(restored 2)', ... suffixes until unique
        idx = 1
        while True:
            suffix = " (restored)" if idx == 1 else f" (restored {idx})"
            candidate = f"{base_name}{suffix}"
            if candidate not in existing_names:
                pipeline_name = candidate
                break
            idx += 1

    pipeline_description = f"Pipeline {pipeline_name}"

    # Create the pipeline
    body = {
        "name": pipeline_name,
        "description": pipeline_description,
        "configurationCompleted": False,
    }
    response = fetch_core_hub("/pipelines", method="POST", token=token, body=body)
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected response while creating pipeline: {response}")

    raw_id = response.get("pipelineId") or response.get("id") or response.get("pipeline_id")
    if not raw_id:
        raise RuntimeError("Pipeline creation succeeded but no ID was returned")
    pipeline_id = str(raw_id)

    # Discover unassigned agents so we can bind them to the new pipeline
    unassigned = fetch_core_hub("/unassigned-agents", token=token)
    if not isinstance(unassigned, list):
        raise RuntimeError("Failed to retrieve unassigned agents from Core Hub")

    def _find_matching_agent(agent_type: str, agent_tag: str) -> Dict[str, Any]:
        for item in unassigned:
            if not isinstance(item, dict):
                continue
            if item.get("agentType") == agent_type and item.get("agentTag") == agent_tag:
                return item
        raise RuntimeError(
            f"No unassigned agent found with agentType={agent_type!r}, agentTag={agent_tag!r}"
        )

    # Bind each configured agent to the pipeline and apply its settings
    for conf_agent in agents_cfg:
        if not isinstance(conf_agent, dict):
            continue

        agent_type = conf_agent.get("agentType")
        agent_tag = conf_agent.get("agentTag")
        if not agent_type or not agent_tag:
            raise RuntimeError("Each agent must declare 'agentType' and 'agentTag'")

        match = _find_matching_agent(agent_type, agent_tag)
        raw_agent_id = match.get("agentId") or match.get("id")
        if not raw_agent_id:
            raise RuntimeError(
                f"Matched agent for tag={agent_tag!r}, type={agent_type!r} is missing an ID"
            )
        agent_id = str(raw_agent_id)

        # Attach agent to pipeline
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent_id}",
            method="PUT",
            token=token,
        )

        # Apply credentials
        host_credentials = conf_agent.get("hostCredentials") or {}
        custom_host_credentials = conf_agent.get("customHostCredentials") or {}
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent_id}/config/credentials",
            method="PUT",
            token=token,
            body={
                "hostCredentials": host_credentials,
                "customHostCredentials": custom_host_credentials,
            },
        )

        # Apply specific configuration if present
        specific_conf = conf_agent.get("specificConfiguration") or {}
        if specific_conf:
            fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent_id}/config/specific",
                method="PUT",
                token=token,
                body={"configuration": specific_conf},
            )

    logger.info("Imported config-only pipeline %s (%s)", pipeline_name, pipeline_id)
    return {"pipelineId": pipeline_id, "pipelineName": pipeline_name}


def duplicate_pipeline(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    new_pipeline_name: Optional[str],
    source_agent_tag: str,
    target_agent_tag: str,
    source_agent_password: str,
    target_agent_password: str,
    clone_entities: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    conductor_url: Optional[str] = None,
    source_host_override: Optional[str] = None,
    target_host_override: Optional[str] = None,
    source_port_override: Optional[int] = None,
    target_port_override: Optional[int] = None,
    override_schemas: bool = False,
    override_source_schema: Optional[str] = None,
    override_target_schema: Optional[str] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Duplicate a pipeline with new source and target agent tags (keeping same agent types).

    This function:
    1. Gets the original pipeline's agent types from CoreHub
    2. Exports the YAML configuration of the original pipeline
    3. Creates a new pipeline with the same agent types but new tags
    4. If agents don't exist, can deploy them via conductor APIs
    5. Imports the YAML configuration to the new pipeline

    Args:
        token: CoreHub authentication token
        base_url: CoreHub base URL
        pipeline_id: Original pipeline ID to duplicate
        new_pipeline_name: Name for the new duplicated pipeline
        source_agent_tag: New agent tag for the source (same type as original)
        target_agent_tag: New agent tag for the target (same type as original)
        use_ssl: Whether to use SSL for CoreHub connection
        skip_verify: Whether to skip SSL verification
        conductor_url: Optional conductor URL override for agent deployment

    Returns:
        Dict containing the new pipeline information and any deployment details
    """

    def _raise_if_cancelled():
        if cancel_checker and cancel_checker():
            logger.info("Duplicate pipeline request cancelled by user")
            raise DuplicateCancelledError("Duplicate pipeline cancelled by user")

    _raise_if_cancelled()

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Step 1: Get the original pipeline's agent configuration
    try:
        pipeline_config = fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)
        if not isinstance(pipeline_config, dict):
            raise RuntimeError("Failed to retrieve pipeline configuration")

        agents = pipeline_config.get("agents") or []
        if not isinstance(agents, list) or len(agents) < 2:
            raise RuntimeError("Pipeline must have at least 2 agents (source and target)")

        # Find source and target agents
        source_agent = next((agent for agent in agents if isinstance(agent, dict) and agent.get("agentType") == "SOURCE"), None)
        target_agent = next((agent for agent in agents if isinstance(agent, dict) and agent.get("agentType") == "TARGET"), None)

        if not source_agent or not target_agent:
            raise RuntimeError("Pipeline must have both SOURCE and TARGET agents")

        source_agent_type = source_agent.get("agentType")
        target_agent_type = target_agent.get("agentType")

        if not source_agent_type or not target_agent_type:
            raise RuntimeError("Agent types not found in pipeline configuration")

        logger.info("Original pipeline agents: source=%s, target=%s", source_agent_type, target_agent_type)
        _raise_if_cancelled()

    except Exception as exc:
        logger.exception("Failed to retrieve original pipeline agent configuration: %s", exc)
        raise RuntimeError(f"Failed to retrieve pipeline agent configuration: {exc}") from exc

    original_pipeline_name = (
        pipeline_config.get("pipelineName")
        or pipeline_config.get("pipeline", {}).get("name")
        or f"Pipeline {pipeline_id}"
    )
    if not new_pipeline_name:
        new_pipeline_name = f"{original_pipeline_name} (copy)"

    logger.info("Starting pipeline duplication: %s -> %s", pipeline_id, new_pipeline_name)
    _raise_if_cancelled()

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    logger.info("Starting pipeline duplication: %s -> %s", pipeline_id, new_pipeline_name)

    # Step 1: Export the original pipeline configuration
    try:
        yaml_config = export_pipeline_yaml(
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
        )
    except Exception as exc:
        logger.exception("Failed to export pipeline %s configuration: %s", pipeline_id, exc)
        raise RuntimeError(f"Failed to export pipeline configuration: {exc}") from exc

    # Step 2: Parse the YAML to modify agent configuration
    try:
        config_dict = yaml.safe_load(yaml_config)
    except Exception as exc:
        logger.exception("Failed to parse YAML configuration: %s", exc)
        raise RuntimeError(f"Failed to parse pipeline configuration: {exc}") from exc

    _raise_if_cancelled()

    # Step 3: Check if the required agents exist
    agents_available = _check_agents_available(
        token=token,
        base_url=base_url,
        source_agent_type=source_agent_type,
        source_agent_tag=source_agent_tag,
        target_agent_type=target_agent_type,
        target_agent_tag=target_agent_tag,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )

    _raise_if_cancelled()

    # Step 4: If agents don't exist and conductor is available, deploy them
    deployed_agents = []
    if not agents_available:
        unassigned_agents = _get_unassigned_agents(
            token=token,
            base_url=base_url,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
        )

        logger.info("Current unassigned agents before deployment attempt:")
        _log_unassigned_snapshot(
            token=token,
            base_url=base_url,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
            context="pre-deploy",
        )

        def _has_unassigned(agent_type: str, agent_tag: str) -> bool:
            return any(
                isinstance(agent, dict)
                and agent.get("agentType") == agent_type
                and agent.get("agentTag") == agent_tag
                for agent in unassigned_agents
            )

        source_needed = not _has_unassigned(source_agent_type, source_agent_tag)
        target_needed = not _has_unassigned(target_agent_type, target_agent_tag)

        if not source_needed and not target_needed:
            logger.info(
                "Required agents already available in unassigned list; skipping conductor deployment"
            )
            agents_available = True
        else:
            verify_conductor_ssl = True
            if skip_verify is True:
                verify_conductor_ssl = False
        if conductor_url:
            conductor_url = _normalize_conductor_url(conductor_url)
        if not conductor_url:
            try:
                from urllib.parse import urljoin
                conductor_url = _normalize_conductor_url(urljoin(base_url.rstrip('/'), '/conductor'))
                logger.info("Using corehub-derived conductor URL for duplication: %s", conductor_url)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("Failed to derive conductor URL from corehub: %s", exc)
                conductor_url = None

            if conductor_url:
                logger.info("Required agents not available, attempting deployment via conductor")
                deployed_agents = _deploy_agents_via_conductor(
                    conductor_url=conductor_url,
                    source_agent_type=source_agent_type,
                    source_agent_tag=source_agent_tag,
                    target_agent_type=target_agent_type,
                    target_agent_tag=target_agent_tag,
                    deploy_source_agent=source_needed,
                    deploy_target_agent=target_needed,
                    verify_ssl=verify_conductor_ssl,
                    ssl_enabled_for_agents=bool(use_ssl),
                )

                if deployed_agents:
                    attempt = 0
                    while True:
                        _raise_if_cancelled()
                        attempt += 1
                        logger.info(
                            "Waiting for conductor-deployed agents to become available (attempt %s)",
                            attempt,
                        )
                        time.sleep(5)
                        time.sleep(5)
                        _raise_if_cancelled()
                        if _check_agents_available(
                            token=token,
                            base_url=base_url,
                            source_agent_type=source_agent_type,
                            source_agent_tag=source_agent_tag,
                            target_agent_type=target_agent_type,
                            target_agent_tag=target_agent_tag,
                            use_ssl=use_ssl,
                            skip_verify=skip_verify,
                        ):
                            agents_available = True
                            logger.info(
                                "Conductor-deployed agents are now available after %s attempts",
                                attempt,
                            )
                            break
                        _log_unassigned_snapshot(
                            token=token,
                            base_url=base_url,
                            use_ssl=use_ssl,
                            skip_verify=skip_verify,
                            context=f"attempt-{attempt}",
                        )
                else:
                    logger.warning("Conductor deployment did not return any service names")

            if not agents_available:
                raise RuntimeError(
                    "Required agents are not available. Provide a conductorUrl override or ensure "
                    "the agents are deployed manually before duplicating."
                )

        _raise_if_cancelled()

    # Step 5: Modify the configuration for the new pipeline
    # Update pipeline name
    if "pipelineName" in config_dict:
        config_dict["pipelineName"] = new_pipeline_name

    def _convert_agent_payload(
        agent_template: dict,
        new_tag: str,
        password: str,
        label: str,
        host_override: Optional[str],
        port_override: Optional[int],
    ) -> dict:
        if not password:
            raise RuntimeError(f"{label} agent password is required to duplicate the pipeline.")
        base_host_credentials = (agent_template.get("hostCredentials") or {}).copy()
        if host_override:
            logger.info("Overriding %s host to %s", label.lower(), host_override)
            base_host_credentials["host"] = host_override
        if port_override:
            logger.info("Overriding %s port to %s", label.lower(), port_override)
            base_host_credentials["port"] = port_override
        payload = {
            "agentType": agent_template.get("agentType"),
            "agentTag": new_tag,
            "hostCredentials": {
                **base_host_credentials,
                "password": password,
            },
            "customHostCredentials": agent_template.get("customHostCredentials")
            or agent_template.get("hostCredentialsCustomProperties")
            or {},
            "specificConfiguration": agent_template.get("specificConfiguration") or {},
        }
        return payload

    config_dict["agents"] = [
        _convert_agent_payload(
            source_agent,
            source_agent_tag,
            source_agent_password,
            "Source",
            source_host_override,
            source_port_override,
        ),
        _convert_agent_payload(
            target_agent,
            target_agent_tag,
            target_agent_password,
            "Target",
            target_host_override,
            target_port_override,
        ),
    ]

    # If we deployed agents, update the configuration with their details
    if deployed_agents:
        # This would need to be implemented based on the conductor response
        pass

    # Step 6: Import the modified configuration to create the new pipeline
    try:
        _raise_if_cancelled()
        import_payload = {
            "pipelineName": new_pipeline_name,
            "agents": config_dict["agents"],
        }

        result = import_pipeline_config_only(
            token=token,
            base_url=base_url,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
            config=import_payload,
        )
    except Exception as exc:
        logger.exception("Failed to create duplicated pipeline: %s", exc)
        raise RuntimeError(f"Failed to create duplicated pipeline: {exc}") from exc

    new_pipeline_id = result.get("pipelineId")
    entity_clone_status = "skipped"
    entity_clone_errors: list[str] = []
    udf_clone_status = "skipped"
    udf_clone_errors: list[str] = []
    
    logger.info("clone_entities parameter value: %s", clone_entities)
    if clone_entities:
        logger.info("ENTERING clone_entities block - will clone entities and UDFs")
        try:
            _raise_if_cancelled()
            snapshot_path = _write_temp_yaml(yaml_config)
            try:
                schema_pairs = extract_all_schemas_from_yaml(snapshot_path)
                if override_schemas:
                    if override_source_schema and override_target_schema:
                        logger.info(
                            "Overriding schema pairs to %s -> %s for duplicated pipeline",
                            override_source_schema,
                            override_target_schema,
                        )
                        schema_pairs = [(override_source_schema, override_target_schema)]
                    elif override_source_schema:
                        logger.info(
                            "Overriding source schema to %s for all entity clones",
                            override_source_schema,
                        )
                        schema_pairs = [
                            (override_source_schema, tgt) for (_, tgt) in schema_pairs or [("", "")]
                        ]
                    elif override_target_schema:
                        logger.info(
                            "Overriding target schema to %s for all entity clones",
                            override_target_schema,
                        )
                        schema_pairs = [
                            (src, override_target_schema) for (src, _) in schema_pairs or [("", "")]
                        ]
                source_type_hint, target_type_hint = extract_schema_types_from_yaml(snapshot_path)
                source_type_hint = source_type_hint or "SQL"
                target_type_hint = target_type_hint or "SQL"
                entity_clone_status = "completed"
                if not schema_pairs:
                    entity_clone_status = "empty"
                for source_schema, target_schema in schema_pairs:
                    _raise_if_cancelled()
                    result = run_create_entities(
                        token=token,
                        base_url=base_url,
                        pipeline_id=new_pipeline_id,
                        source_schema=source_schema,
                        target_schema=target_schema,
                        source_type=source_type_hint,
                        target_type=target_type_hint,
                        yaml_file=snapshot_path,
                        skip_errors=True,
                        chunk_size=50,
                        enable_scheduling=False,
                        create_tables=False,
                        use_ssl=use_ssl,
                        skip_verify=skip_verify,
                    )
                    if not result.get("success"):
                        entity_clone_status = "failed"
                        error_msg = result.get("error") or "Unknown error during entity creation"
                        entity_clone_errors.append(f"{source_schema}->{target_schema}: {error_msg}")
                        logger.warning("Entity creation failed for %s -> %s: %s", source_schema, target_schema, error_msg)
                
                # After ALL entities are created, discover and compile UDFs
                logger.info("=== STARTING UDF DISCOVERY FOR PIPELINE %s ===", new_pipeline_id)
                logger.info("Discovering UDFs for duplicated pipeline %s", new_pipeline_id)
                udf_clone_status = "skipped"
                udf_clone_errors: list[str] = []
                try:
                    entities = fetch_pipeline_entities(token, pipeline_id)
                    udfs_to_export: Dict[str, Dict[str, Any]] = {}
                    
                    if isinstance(entities, list):
                        for ent in entities:
                            if not isinstance(ent, dict):
                                continue
                            
                            target_ae: Optional[Dict[str, Any]] = None
                            for ae in ent.get("agentEntities", []) or []:
                                et = ae.get("entityType") or {}
                                if et.get("type") == "Target":
                                    target_ae = ae
                                    break
                            if not target_ae:
                                continue
                            
                            target_et = target_ae.get("entityType") or {}
                            mf_info = target_et.get("mappingFunctionInfo")
                            if isinstance(mf_info, dict):
                                udf_name = mf_info.get("name")
                                if udf_name:
                                    key = str(udf_name)
                                    if key not in udfs_to_export:
                                        raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
                                        # Get entity name for later matching
                                        entity_name = ent.get("entityName") or ""
                                        udfs_to_export[key] = {
                                            "name": str(udf_name),
                                            "type": mf_info.get("type"),
                                            "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
                                            "entityName": entity_name,
                                            "mappingFunctionInfo": mf_info,
                                        }
                    
                    if udfs_to_export:
                        logger.info("Found %d UDF(s) to clone: %s", len(udfs_to_export), list(udfs_to_export.keys()))
                        udf_root = Path(tempfile.gettempdir()) / f"gluesync_duplicate_udfs_{new_pipeline_id}"
                        if udf_root.exists():
                            shutil.rmtree(udf_root)
                        udf_root.mkdir(parents=True, exist_ok=True)
                        
                        try:
                            for udf_name, meta in udfs_to_export.items():
                                try:
                                    response = fetch_core_hub(
                                        f"/pipelines/{pipeline_id}/config/entities/mapping-functions/{udf_name}",
                                        token=token,
                                    )
                                    
                                    if not isinstance(response, dict):
                                        logger.warning("Unexpected response for UDF %s: %r", udf_name, response)
                                        continue
                                    
                                    code = response.get("code")
                                    mf_type = response.get("type") or meta.get("type")
                                    if not isinstance(code, str) or not code:
                                        logger.warning("UDF %s has no code to export", udf_name)
                                        continue
                                    
                                    ext = ".java"
                                    if isinstance(mf_type, str):
                                        t = mf_type.lower()
                                        if t == "java":
                                            ext = ".java"
                                        elif t == "kotlin":
                                            ext = ".kt"
                                        elif t == "python":
                                            ext = ".py"
                                        elif t == "javascript":
                                            ext = ".js"
                                        elif t == "ruby":
                                            ext = ".rb"
                                    
                                    safe_udf_name = "".join(c for c in str(udf_name) if c.isalnum() or c in ("_", "-")) or "udf"
                                    agent_id = meta.get("agentId")
                                    if agent_id is not None:
                                        safe_agent_id = "".join(c for c in str(agent_id) if c.isalnum() or c in ("_", "-")) or "unknown"
                                        folder = udf_root / f"udf-{safe_agent_id}"
                                    else:
                                        folder = udf_root / "udf-unknown"
                                    
                                    folder.mkdir(parents=True, exist_ok=True)
                                    udf_file = folder / f"{safe_udf_name}{ext}"
                                    udf_file.write_text(code, encoding="utf-8")
                                    logger.info("Exported UDF %s to %s", udf_name, udf_file)
                                    
                                except Exception as exc_udf:  # pylint: disable=broad-except
                                    logger.exception("Failed to export UDF %s: %s", udf_name, exc_udf)
                                    udf_clone_errors.append(f"Failed to export UDF {udf_name}: {exc_udf}")
                            
                            # Now compile the UDFs for the new pipeline using direct API calls
                            logger.info("Compiling UDFs for new pipeline %s", new_pipeline_id)
                            
                            for udf_name, meta in udfs_to_export.items():
                                try:
                                    # Find the UDF file we just saved
                                    udf_type = meta.get("type") or "Java"
                                    ext = ".java"
                                    t = udf_type.lower() if isinstance(udf_type, str) else "java"
                                    if t == "kotlin":
                                        ext = ".kt"
                                    elif t == "python":
                                        ext = ".py"
                                    elif t == "javascript":
                                        ext = ".js"
                                    elif t == "ruby":
                                        ext = ".rb"
                                    
                                    safe_udf_name = "".join(c for c in str(udf_name) if c.isalnum() or c in ("_", "-")) or "udf"
                                    agent_id = meta.get("agentId")
                                    if agent_id is not None:
                                        safe_agent_id = "".join(c for c in str(agent_id) if c.isalnum() or c in ("_", "-")) or "unknown"
                                        udf_file = udf_root / f"udf-{safe_agent_id}" / f"{safe_udf_name}{ext}"
                                    else:
                                        udf_file = udf_root / "udf-unknown" / f"{safe_udf_name}{ext}"
                                    
                                    if not udf_file.exists():
                                        logger.warning("UDF file not found: %s", udf_file)
                                        continue
                                    
                                    # Read and encode the UDF code
                                    udf_code = udf_file.read_text(encoding="utf-8")
                                    b64_code = base64.b64encode(udf_code.encode("utf-8")).decode("utf-8")
                                    
                                    # Compile the UDF directly via API
                                    compile_payload = {
                                        "code": b64_code,
                                        "type": udf_type,
                                        "udfName": udf_name,
                                    }
                                    
                                    fetch_core_hub(
                                        f"/pipelines/{new_pipeline_id}/config/entities/mapping-functions/compile-mapping-function",
                                        method="POST",
                                        token=token,
                                        body=compile_payload,
                                    )
                                    logger.info("Successfully compiled UDF %s for new pipeline", udf_name)
                                    
                                except Exception as exc_compile:  # pylint: disable=broad-except
                                    logger.exception("Failed to compile UDF %s: %s", udf_name, exc_compile)
                                    udf_clone_errors.append(f"Failed to compile UDF {udf_name}: {exc_compile}")
                            
                            # Now update entities in the new pipeline to link them to the compiled UDFs
                            logger.info("Linking UDFs to entities in new pipeline %s", new_pipeline_id)
                            try:
                                new_entities = fetch_pipeline_entities(token, new_pipeline_id)
                                if isinstance(new_entities, list):
                                    for new_ent in new_entities:
                                        if not isinstance(new_ent, dict):
                                            continue
                                        
                                        # Find the target agent entity
                                        for ae in new_ent.get("agentEntities", []) or []:
                                            et = ae.get("entityType") or {}
                                            if et.get("type") != "Target":
                                                continue
                                            
                                            # Check if this entity should have a UDF
                                            # Match by table name (last part of entity name)
                                            new_entity_name = new_ent.get("entityName") or ""
                                            new_table_name = new_entity_name.split(".")[-1] if "." in new_entity_name else new_entity_name
                                            
                                            for udf_name, udf_meta in udfs_to_export.items():
                                                orig_entity_name = udf_meta.get("entityName") or ""
                                                orig_table_name = orig_entity_name.split(".")[-1] if "." in orig_entity_name else orig_entity_name
                                                
                                                if new_table_name and orig_table_name and new_table_name == orig_table_name:
                                                    # This entity should have this UDF
                                                    mf_info = udf_meta.get("mappingFunctionInfo")
                                                    if mf_info:
                                                        et["mappingFunctionInfo"] = mf_info
                                                        et["udf"] = [mf_info]
                                                        logger.info("Linked UDF %s to entity %s", udf_name, new_entity_name)
                                                    break
                                        
                                        # Update the entity
                                        try:
                                            fetch_core_hub(
                                                f"/pipelines/{new_pipeline_id}/config/entities",
                                                method="PUT",
                                                token=token,
                                                body={"entities": [new_ent]},
                                            )
                                        except Exception as exc_update:  # pylint: disable=broad-except
                                            logger.warning("Failed to update entity %s with UDF: %s", new_ent.get("entityName"), exc_update)
                                            
                            except Exception as exc_link:  # pylint: disable=broad-except
                                logger.exception("Failed to link UDFs to entities: %s", exc_link)
                                udf_clone_errors.append(f"Failed to link UDFs to entities: {exc_link}")
                            
                            udf_clone_status = "completed" if not udf_clone_errors else "partial"
                            
                        finally:
                            if udf_root.exists():
                                shutil.rmtree(udf_root)
                                logger.info("Cleaned up temporary UDF directory")
                    else:
                        logger.info("No UDFs found to clone")
                        udf_clone_status = "none"
                        
                except Exception as exc_udf_discovery:  # pylint: disable=broad-except
                    logger.exception("Failed to discover/clone UDFs: %s", exc_udf_discovery)
                    udf_clone_status = "failed"
                    udf_clone_errors.append(str(exc_udf_discovery))
                    
            finally:
                os.unlink(snapshot_path)
        except DuplicateCancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            entity_clone_status = "failed"
            entity_clone_errors.append(str(exc))
            logger.exception("Failed to clone entities for pipeline %s: %s", pipeline_id, exc)

    try:
        fetch_core_hub(
            f"/pipelines/{new_pipeline_id}",
            method="PUT",
            token=token,
            body={"configurationCompleted": True, "name": new_pipeline_name},
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to mark pipeline %s as configuration completed: %s", new_pipeline_id, exc)

    logger.info("Successfully duplicated pipeline %s to %s (%s)", pipeline_id, new_pipeline_name, new_pipeline_id)

    return {
        "originalPipelineId": pipeline_id,
        "newPipelineId": new_pipeline_id,
        "newPipelineName": new_pipeline_name,
        "agentsAvailable": agents_available,
        "deployedAgents": deployed_agents,
        "sourceAgent": {"type": source_agent_type, "tag": source_agent_tag},
        "targetAgent": {"type": target_agent_type, "tag": target_agent_tag},
        "entityCloneStatus": entity_clone_status,
        "entityCloneErrors": entity_clone_errors or None,
        "udfCloneStatus": udf_clone_status,
        "udfCloneErrors": udf_clone_errors or None,
    }


def _check_agents_available(
    *,
    token: str,
    base_url: str,
    source_agent_type: str,
    source_agent_tag: str,
    target_agent_type: str,
    target_agent_tag: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> bool:
    """Check if the required source and target agents are available (unassigned or assignable)."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Get unassigned agents
    unassigned = fetch_core_hub("/unassigned-agents", token=token)
    if not isinstance(unassigned, list):
        logger.warning("Failed to retrieve unassigned agents")
        return False

    # Check for source agent
    source_found = any(
        isinstance(agent, dict) and
        agent.get("agentType") == source_agent_type and
        agent.get("agentTag") == source_agent_tag
        for agent in unassigned
    )

    # Check for target agent
    target_found = any(
        isinstance(agent, dict) and
        agent.get("agentType") == target_agent_type and
        agent.get("agentTag") == target_agent_tag
        for agent in unassigned
    )

    logger.info("Agent availability check: source=%s, target=%s", source_found, target_found)
    return source_found and target_found


def _get_unassigned_agents(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list:
    """Return the list of unassigned agents from CoreHub (or empty list on error)."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)
    try:
        unassigned = fetch_core_hub("/unassigned-agents", token=token)
        if isinstance(unassigned, list):
            return unassigned
        logger.warning("Unexpected response when fetching unassigned agents: %s", type(unassigned))
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to fetch unassigned agents: %s", exc)
    return []


def _log_unassigned_snapshot(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    context: str = "",
) -> None:
    """Log a snapshot of the current unassigned agent inventory for debugging."""

    agents = _get_unassigned_agents(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )
    if not agents:
        logger.info("Unassigned snapshot%s: (empty)", f" [{context}]" if context else "")
        return

    summary: Dict[str, int] = {}
    details = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        key = f"{agent.get('agentType')}/{agent.get('agentTag')}"
        summary[key] = summary.get(key, 0) + 1
        if len(details) < 10:
            details.append(f"{key}:{agent.get('agentId')}")

    counts = ", ".join(f"{k} x{v}" for k, v in summary.items())
    logger.info(
        "Unassigned snapshot%s: %s",
        f" [{context}]" if context else "",
        counts or "(none)",
    )
    if details:
        logger.debug("Unassigned details%s: %s", f" [{context}]" if context else "", details)


def _deploy_agents_via_conductor(
    *,
    conductor_url: str,
    source_agent_type: str,
    source_agent_tag: str,
    target_agent_type: str,
    target_agent_tag: str,
    deploy_source_agent: bool,
    deploy_target_agent: bool,
    verify_ssl: bool = True,
    ssl_enabled_for_agents: bool = True,
) -> list:
    """Deploy agents via conductor APIs if they are not available."""

    logger.info("Deploying agents via conductor: source %s/%s, target %s/%s",
                source_agent_type, source_agent_tag, target_agent_type, target_agent_tag)

    # Import the conductor function from the parent directory
    import sys
    import os
    parent_dir = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, parent_dir)
    from add_agents_with_conductor import add_agents_with_conductor

    # Create a temporary config.json for the agents
    ssl_env_value = "true" if ssl_enabled_for_agents else "false"

    def _agent_entry(agent_type: str, agent_tag: str) -> dict:
        return {
            "agentTag": agent_tag,
            "agentType": agent_type.lower(),
            "environment": {
                "TYPE": agent_type.lower(),
                "SSL_ENABLED": ssl_env_value,
            },
        }

    agent_entries = []
    if deploy_source_agent:
        agent_entries.append(_agent_entry(source_agent_type, source_agent_tag))
    if deploy_target_agent:
        agent_entries.append(_agent_entry(target_agent_type, target_agent_tag))

    if not agent_entries:
        logger.info("No new agents requested for conductor deployment")
        return []

    config_data = {
        "globals": {
            "testName": f"duplicated_pipeline_{source_agent_tag}_{target_agent_tag}",
            "jobId": "duplication_job"
        },
        "agents": agent_entries,
    }
    logger.info(
        "Conductor deployment payload: test=%s, agents=%s",
        config_data["globals"]["testName"],
        [f"{a['agentType']}:{a['agentTag']}" for a in agent_entries],
    )

    # Write temporary config file
    import tempfile
    import json
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config_data, f)
        config_path = f.name

    try:
        # Call the conductor function
        result = add_agents_with_conductor(
            config_path=config_path,
            conductor_url=_normalize_conductor_url(conductor_url),
            verify_ssl=verify_ssl,
        )

        if result.get("error"):
            logger.error("Conductor deployment failed: %s", result["error"])
            return []

        service_names = result.get("service_names", [])
        logger.info(
            "Conductor services response: services=%s, containers_started=%s",
            service_names,
            result.get("containers_started"),
        )
        if "start_error" in result:
            logger.warning("Conductor start error: %s", result["start_error"])
        if "start_response" in result:
            logger.debug("Conductor start response: %s", result["start_response"])
        return service_names

    except ImportError as exc:
        logger.warning("Could not import conductor functions: %s", exc)
        return []
    except Exception as exc:
        logger.exception("Failed to deploy agents via conductor: %s", exc)
        return []

    finally:
        # Clean up temporary file
        os.unlink(config_path)


def validate_token(token: str) -> bool:
    try:
        response = fetch_core_hub("/pipelines", token=token)
        return isinstance(response, list)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Token validation failed")
        return False

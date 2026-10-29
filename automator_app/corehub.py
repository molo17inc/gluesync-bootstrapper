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

import io
import json
import logging
import os
import re
import shutil
import zipfile
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
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
    catalog_type_for_agent_tag,
)
from create_all_entities import (
    CREATE_TABLE_IF_NOT_EXISTS,
    create_entities,
    main as create_entities_main,
    set_create_table_if_not_exists,
)
import create_user_defined_functions
import function_collection
import import_options
import ai_settings
from export_template_from_corehub import (
    fetch_pipeline_entities,
    build_entities_maps,
    fetch_groups_map,
    fetch_pipeline_jobs,
    build_schemas_from_entities,
    attach_schedules_from_jobs,
    build_yaml_structure,
    build_export_header,
    enrich_null_column_types_from_discovery,
)
import time
import yaml

logger = logging.getLogger(__name__)

PIPELINES_PATH = "/pipelines"

# CoreHub REST paths used more than once.
_SMTP_CONFIG_PATH = "/global-config/smtp"
_USERS_PATH = "/users"

# Placeholder written instead of a secret when secrets are not exported.
_MASKED_SECRET = "*******"

_UDF_ENTITIES_FETCH_FAILED_LOG = "Failed to fetch entities for UDF export in pipeline %s: %s"

# Files of the full CoreHub backup ZIP, one per configuration domain.
_GLOBAL_CONFIGS_BACKUP_FILE = "global-configs.yaml"
_SMTP_BACKUP_FILE = "smtp.yaml"
_WEBHOOKS_BACKUP_FILE = "webhooks.yaml"
_THRESHOLDS_BACKUP_FILE = "thresholds.yaml"
_SCHEDULES_BACKUP_FILE = "schedules.yaml"
_USERS_BACKUP_FILE = "users.yaml"
_OIDC_BACKUP_FILE = "oidc.yaml"

# Reasons reported when a restore skips a configuration section.
_REASON_NOT_PRESENT_IN_BACKUP = "not present in backup"
_REASON_EXPORTED_WITH_ERROR = "exported with error"


class DuplicateCancelledError(RuntimeError):
    """Raised when a duplicate pipeline request is cancelled by the user."""


class PipelineInMaintenanceError(RuntimeError):
    """Raised when a backup is requested for a pipeline that is in maintenance mode.

    A pipeline in maintenance mode has its agents suspended and its entities
    stopped, so its configuration cannot be reliably exported. The pipeline
    must be skipped from any backup operation until maintenance is exited.
    """

_AGENT_TYPE_BY_NAME: Optional[Dict[str, str]] = None

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


def _normalize_agent_category(agent_category: Optional[str]) -> Optional[str]:
    """Normalize agent categories coming from agents.json to SQL/NoSQL labels."""

    if not agent_category:
        return None

    normalized = agent_category.strip().upper()
    if normalized in {"RDBMS", "SQL"}:
        return "SQL"
    return "NoSQL"


def _infer_schema_types_from_agents_list(agents: Any) -> Tuple[Optional[str], Optional[str]]:
    """Infer (source_type, target_type) directly from a pipeline's agents payload."""

    if not isinstance(agents, list):
        return None, None

    catalog = _load_agent_type_catalog()

    def _classify(agent: Any) -> Optional[str]:
        if not isinstance(agent, dict):
            return None
        tag = str(agent.get("agentTag") or "").lower()
        if not tag:
            return None
        return _normalize_agent_category(catalog_type_for_agent_tag(catalog, tag))

    source_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "SOURCE"), None)
    target_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "TARGET"), None)

    return _classify(source_agent), _classify(target_agent)


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
    token: Optional[str] = None,
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
    headers: Dict[str, str] = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
        headers['Cookie'] = f'gs-auth={token}'
    try:
        response = requests.get(api_url, timeout=5, verify=verify, headers=headers)
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


class _CallbackLogHandler(logging.Handler):
    """Logging handler that forwards every non-empty log message to a callback."""

    def __init__(self, callback: Callable[[str], None], level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
        message = record.getMessage()
        if message:
            self._callback(message)


def _is_nosql_target(target_type: Optional[str]) -> bool:
    """True when the inferred target type is a non-RDBMS (NoSQL) one."""
    return bool(target_type) and target_type.upper() == "NOSQL"


def _apply_create_tables_setting(create_tables: bool) -> None:
    """Set the CREATE_TABLE_IF_NOT_EXISTS flag and env variable read by create_all_entities."""
    set_create_table_if_not_exists(create_tables)
    os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = 'true' if create_tables else 'false'


def _restore_create_tables_setting(prev_env_value: Optional[str], prev_flag_value: bool) -> None:
    """Put back the CREATE_TABLE_IF_NOT_EXISTS env variable and flag saved before a run."""
    if prev_env_value is not None:
        os.environ['CREATE_TABLE_IF_NOT_EXISTS'] = prev_env_value
    else:
        os.environ.pop('CREATE_TABLE_IF_NOT_EXISTS', None)
    set_create_table_if_not_exists(prev_flag_value)


def _replay_logs(log_callback: Optional[Callable[[str], None]], logs: list) -> None:
    """Send each captured stdout line to ``log_callback``, when there is one."""
    if log_callback is not None:
        for line in logs:
            log_callback(line)


def _create_entities_outcome(result_payload: Dict[str, Any], logs: list) -> Dict[str, Any]:
    """Build the response of run_create_entities from the create_all_entities result."""
    success = bool(result_payload.get("success", True))
    errors = result_payload.get("errors") or []
    if errors:
        success = False
    payload: Dict[str, Any] = {
        "success": success,
        "logs": logs,
        "result": result_payload,
    }
    if not success:
        summary_error = result_payload.get("error")
        if not summary_error and errors:
            summary_error = "; ".join(errors)
        if summary_error:
            payload["error"] = summary_error
    return payload


@dataclass
class EntityRunOptions:
    """The settings of :func:`run_create_entities` a caller may leave at their default.

    ``restore_function_collection``: publish and install the function collection that comes with the YAML before
    anything else (False when the caller has done it, or has none to restore).
    """

    skip_errors: bool = True
    chunk_size: int = 50
    log_callback: Optional[Callable[[str], None]] = None
    chronos_token: Optional[str] = None
    restore_function_collection: bool = True
    # import_options.ImportOptions (remap, dry run, --on-existing, ...); None keeps the old behaviour.
    import_options: Optional[Any] = None


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
    enable_scheduling: bool,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    options: Optional[EntityRunOptions] = None,
) -> Dict[str, Any]:
    """Invoke create_all_entities.main and capture output."""
    options = options or EntityRunOptions()
    log_callback = options.log_callback

    set_scheduling_enabled(enable_scheduling)
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Resolve source/target types from agents.json catalog so non-RDBMS targets
    # (NoSQL, Object Store, etc.) are correctly classified and table creation
    # is skipped for them. Without this, a YAML hint like "SQL" or the "SQL"
    # fallback would cause the bootstrapper to attempt CREATE TABLE against
    # targets that don't support it (e.g. Azure Data Lake Storage).
    inferred_source_type, inferred_target_type = infer_agent_schema_types(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )
    if _is_nosql_target(inferred_target_type):
        logger.info(
            "Pipeline %s has a non-RDBMS target (%s) - disabling table creation",
            pipeline_id,
            inferred_target_type,
        )
        create_tables = False
    source_type = inferred_source_type or source_type
    target_type = inferred_target_type or target_type

    prev_env_create_tables = os.environ.get('CREATE_TABLE_IF_NOT_EXISTS')
    prev_flag_create_tables = CREATE_TABLE_IF_NOT_EXISTS
    _apply_create_tables_setting(create_tables)

    handler = None
    root_logger = logging.getLogger()
    if log_callback is not None:
        handler = _CallbackLogHandler(log_callback, level=logging.INFO)
        root_logger.addHandler(handler)

    buffer = io.StringIO()
    result_payload: Optional[Dict[str, Any]] = None
    try:
        with redirect_stdout(buffer):
            result_payload = create_entities_main(
                pipeline_id,
                source_schema,
                target_schema,
                source_type,
                target_type,
                yaml_file,
                token,
                options.skip_errors,
                options.chunk_size,
                chronos_token=options.chronos_token,
                restore_function_collection=options.restore_function_collection,
                **({"import_opts": options.import_options} if options.import_options is not None else {}),
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
        _restore_create_tables_setting(prev_env_create_tables, prev_flag_create_tables)

    logs = buffer.getvalue().splitlines()
    _replay_logs(log_callback, logs)

    return _create_entities_outcome(result_payload or {}, logs)


@dataclass
class EntityTablesRunOptions:
    """Optional settings of :func:`run_create_entities_for_tables` (S107: keep the public kwargs ≤13).

    ``entity_import_options`` / ``yaml_config``: entity Customize plan (remap, on-existing, dry-run,
    overrides). Omitted = current behaviour.
    """

    skip_errors: bool = True
    chunk_size: int = 50
    chronos_token: Optional[str] = None
    entity_import_options: Optional[Any] = None
    yaml_config: Optional[Dict[str, Any]] = None


def _bulk_apply_inferred_types(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    source_type: str,
    target_type: str,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Tuple[str, str, bool]:
    """Catalog-inferred source/target types; disable CREATE TABLE for non-RDBMS targets."""
    inferred_source_type, inferred_target_type = infer_agent_schema_types(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )
    # Object store / streaming / NoSQL: never CREATE TABLE (same helper as run_create_entities).
    if _is_nosql_target(inferred_target_type):
        logger.info(
            "Pipeline %s has a non-RDBMS target (%s) - disabling table creation",
            pipeline_id,
            inferred_target_type,
        )
        create_tables = False
    if inferred_source_type:
        source_type = inferred_source_type
    if inferred_target_type:
        target_type = inferred_target_type
    return source_type, target_type, create_tables


def _bulk_source_target_agent_ids(
    token: str, pipeline_id: str, source_type: str, target_type: str,
) -> Tuple[list, Any, Any]:
    """Pipeline agents list plus SOURCE/TARGET agent ids (raises if missing)."""
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
    return agents, raw_source_id, raw_target_id


def _bulk_override_types_from_agents(agents: list, source_type: str, target_type: str) -> Tuple[str, str]:
    """Prefer agents-list inferred catalog types; default missing sides to SQL."""
    inferred_source_type, inferred_target_type = _infer_schema_types_from_agents_list(agents)
    if inferred_source_type and inferred_source_type != source_type:
        logger.info(
            "Overriding provided source_type '%s' with inferred '%s' based on pipeline agents",
            source_type,
            inferred_source_type,
        )
        source_type = inferred_source_type
    if inferred_target_type and inferred_target_type != target_type:
        logger.info(
            "Overriding provided target_type '%s' with inferred '%s' based on pipeline agents",
            target_type,
            inferred_target_type,
        )
        target_type = inferred_target_type
    return source_type or "SQL", target_type or "SQL"


def _bulk_resolve_agents(
    token: str, pipeline_id: str, source_type: str, target_type: str,
) -> Tuple[Any, Any, str, str]:
    """SOURCE/TARGET agent ids and types after agents-list inference (defaults to SQL)."""
    agents, raw_source_id, raw_target_id = _bulk_source_target_agent_ids(
        token, pipeline_id, source_type, target_type,
    )
    source_type, target_type = _bulk_override_types_from_agents(agents, source_type, target_type)
    return raw_source_id, raw_target_id, source_type, target_type


def _bulk_filter_discovered_tables(
    token: str, pipeline_id: str, source_agent_id: Any, source_schema: str, table_names: list[str],
) -> list[Any]:
    """Discovery tables restricted to the caller selection (case-insensitive name match)."""
    discovered_tables = get_agent_tables(token, pipeline_id, source_agent_id, source_schema)
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
        if name and name.lower() in wanted:
            filtered_tables.append(tbl)

    if not filtered_tables:
        raise RuntimeError(
            f"No matching tables found in discovery for schema {source_schema!r} and selection {sorted(table_names)!r}"
        )
    return filtered_tables


def _bulk_invoke_create_entities(
    *,
    token: str,
    pipeline_id: str,
    source_schema: str,
    target_schema: str,
    filtered_tables: list[Any],
    source_agent_id: Any,
    target_agent_id: Any,
    source_type: str,
    target_type: str,
    options: "EntityTablesRunOptions",
) -> Any:
    """Call create_entities, wrapping with ImportOptions when the plan supplied any."""
    kwargs = {
        "yaml_config": options.yaml_config,
        "skip_errors": options.skip_errors,
        "chunk_size": options.chunk_size,
        "chronos_token": options.chronos_token,
    }
    args = (
        token, pipeline_id, source_schema, target_schema, filtered_tables,
        source_agent_id, target_agent_id, source_type, target_type,
    )
    if options.entity_import_options is not None:
        with import_options.use(options.entity_import_options):
            return create_entities(*args, **kwargs)
    return create_entities(*args, **kwargs)


def _bulk_tables_success_payload(result: Any, logs: list) -> Dict[str, Any]:
    """Success/failure payload of a bulk table create (same shape as before the options split)."""
    errors = (result or {}).get("errors") or []
    success = not result or not (result.get("failed") or errors)
    payload: Dict[str, Any] = {"success": success, "logs": logs, "result": result or {}}
    if not success:
        payload["error"] = f"Some entities failed: {result.get('failed')} out of {result.get('total')}"
        if errors:
            payload["error"] += ": " + "; ".join(errors)
    return payload


def _bulk_tables_http_error_detail(exc: BaseException) -> str:
    """Prefer Core Hub exceptionMessage / message on HTTPError; else str(exc)."""
    error_detail = str(exc)
    try:
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            body = exc.response.json()
            corehub_msg = body.get("exceptionMessage") or body.get("message")
            if corehub_msg:
                return f"{exc}: {corehub_msg}"
    except Exception:  # pylint: disable=broad-except
        pass
    return error_detail


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
    enable_scheduling: bool,
    create_tables: bool,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    options: Optional[EntityTablesRunOptions] = None,
) -> Dict[str, Any]:
    """Create entities only for the specified source tables.

    This is a bulk helper used by the Automator "Bulk operations" UI. It
    mirrors run_create_entities, but restricts the processing to the
    user-selected subset of tables instead of all discovered tables.
    """
    options = options or EntityTablesRunOptions()

    set_scheduling_enabled(enable_scheduling)
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    source_type, target_type, create_tables = _bulk_apply_inferred_types(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        source_type=source_type,
        target_type=target_type,
        create_tables=create_tables,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )

    prev_env_create_tables = os.environ.get("CREATE_TABLE_IF_NOT_EXISTS")
    prev_flag_create_tables = CREATE_TABLE_IF_NOT_EXISTS
    _apply_create_tables_setting(create_tables)

    buffer = io.StringIO()

    try:
        raw_source_id, raw_target_id, source_type, target_type = _bulk_resolve_agents(
            token, pipeline_id, source_type, target_type,
        )
        filtered_tables = _bulk_filter_discovered_tables(
            token, pipeline_id, raw_source_id, source_schema, table_names,
        )
        with redirect_stdout(buffer):
            result = _bulk_invoke_create_entities(
                token=token,
                pipeline_id=pipeline_id,
                source_schema=source_schema,
                target_schema=target_schema,
                filtered_tables=filtered_tables,
                source_agent_id=raw_source_id,
                target_agent_id=raw_target_id,
                source_type=source_type,
                target_type=target_type,
                options=options,
            )
        return _bulk_tables_success_payload(result, buffer.getvalue().splitlines())

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Bulk create_entities_for_tables execution failed")
        error_detail = _bulk_tables_http_error_detail(exc)
        logs = buffer.getvalue().splitlines()
        logs.append(f"ERROR: {error_detail}")
        return {"success": False, "logs": logs, "error": error_detail}
    finally:
        _restore_create_tables_setting(prev_env_create_tables, prev_flag_create_tables)


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

    return sorted(names)


def _discovered(col: Dict[str, Any], name: str, legacy_name: str, default: Any = None) -> Any:
    """A field of a discovered column under the name discovery uses (``Column.kt`` serial names), else ``legacy_name``."""
    for key in (name, legacy_name):
        if col.get(key) is not None:
            return col[key]
    return default


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
    keys are provided in YAML: primary key columns (``isPK`` in discovery) are
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
        if not _discovered(col, "isPK", "isPrimaryKey"):
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
    numericPrecision, numericScale, isNullable, id and ordinalPosition, plus
    charSet when the source publishes one. Discovery publishes them as
    dataType, charMaxLength, numPrec, numScale and position.
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
        
        # Discovery names the ordinal position "position"; fall back to the id
        ordinal = _discovered(col, "position", "ordinalPosition", default=col_id)

        meta: Dict[str, Any] = {
            "name": name,
            "type": _discovered(col, "dataType", "type"),
            "dataLength": _discovered(col, "charMaxLength", "dataLength", default=0),
            "numericPrecision": _discovered(col, "numPrec", "numericPrecision", default=0),
            "numericScale": _discovered(col, "numScale", "numericScale", default=0),
            "isNullable": col.get("isNullable", False),
            "id": col_id,
            "ordinalPosition": ordinal,
        }
        # The character set the source publishes for the column — its CCSID on AS400.
        # Carried into the template so it can be edited there to have the column read
        # with another one. Unlike the backup, only written when discovery has one:
        # here an absent key and a null one would restore the same value anyway.
        char_set = col.get("charSet")
        if char_set is not None:
            meta["charSet"] = char_set
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
    source_type, target_type = _infer_schema_types_from_agents_list(agents)

    return source_type or "SQL", target_type or "SQL"


def list_pipelines(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> list[Dict[str, Any]]:
    """Return a normalized list of pipelines for the export UI."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    response = fetch_core_hub(PIPELINES_PATH, token=token)
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
            # CoreHub exposes the maintenance state as isInMaintenanceMode on the
            # pipeline response. Older CoreHub builds may omit the field; treat
            # absence as "not in maintenance" so we never skip a healthy pipeline.
            in_maintenance = bool(item.get("isInMaintenanceMode", False))
            pipelines.append(
                {
                    "id": pid,
                    "name": str(name) if name is not None else None,
                    "description": str(description) if description is not None else None,
                    "isInMaintenanceMode": in_maintenance,
                }
            )

    return pipelines


def get_pipeline_maintenance_state(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> bool:
    """Return True when the given pipeline is currently in maintenance mode.

    Falls back to False when the field is missing (older CoreHub builds) or the
    pipeline metadata cannot be fetched, so a transient lookup failure never
    blocks a backup that would otherwise succeed.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)
    try:
        response = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Could not determine maintenance state for pipeline %s (assuming "
            "not in maintenance): %s",
            pipeline_id,
            exc,
        )
        return False
    if isinstance(response, dict):
        return bool(response.get("isInMaintenanceMode", False))
    return False


def get_environment_summary(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    chronos_token: Optional[str] = None,
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

    chronos_stats = _fetch_chronos_stats(base_url, skip_verify=skip_verify, token=chronos_token or token)
    totals["schedules"] = chronos_stats.get("enabledJobs", 0)

    return {
        "totals": totals,
        "pipelines": pipeline_details,
        "services": {
            "chronos": chronos_stats,
        },
    }


def fetch_global_notification_config(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    include_secrets: bool = False,
) -> Optional[dict]:
    """Fetch global notification settings (SMTP) from CoreHub.

    Returns a dict with the SMTP configuration, or None if unavailable.

    When include_secrets is True, CoreHub returns the real password ONLY if the
    authenticated user is a SUPER_ADMIN (see GlobalConfigurations.kt smtp GET:
    revealSecret = include_secrets && isSuperAdmin). Otherwise CoreHub returns
    the password as "<hidden>". When include_secrets is False the password is
    masked locally to "*******" for consistency with agent credentials exports.
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Forward include_secrets to CoreHub so super-admins receive the real
    # password instead of the "<hidden>" placeholder.
    params = {"include_secrets": "true"} if include_secrets else None

    try:
        smtp = fetch_core_hub(_SMTP_CONFIG_PATH, token=token, params=params)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch global SMTP config: %s", exc)
        return None

    if not isinstance(smtp, dict):
        return None

    cfg = dict(smtp)
    if not include_secrets and "password" in cfg and cfg["password"]:
        cfg["password"] = _MASKED_SECRET
    return cfg



def _safe_path_component(value: Optional[str], default: str = "unknown") -> str:
    """Sanitize a value for use as a ZIP/path component."""
    if value is None:
        return default
    cleaned = "".join(c for c in str(value) if c.isalnum() or c in ("_", "-"))
    return cleaned or default


def _mapping_function_extension(mf_type: Optional[Any]) -> str:
    """Map CoreHub MappingFunctionsType to the file extension Automator import expects.

    Mapping aligned with MappingFunctionsType in CoreHub (and historical Automator
    ZIP layout):
    - Java       -> .java
    - Kotlin     -> .kt
    - Python     -> .js
    - Javascript -> .py
    - Ruby       -> .rb
    """
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
    return ext


def _target_agent_entity(entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first agentEntity of ``entity`` whose entityType is Target, if any."""
    for ae in entity.get("agentEntities", []) or []:
        et = ae.get("entityType") or {}
        if et.get("type") == "Target":
            return ae
    return None


def _referenced_udfs(target_et: Dict[str, Any]) -> List[Tuple[Any, Any]]:
    """``(name, type)`` of the UDFs a Target entityType refers to, in declaration order.

    ``mappingFunctionInfo`` wins when present; otherwise the legacy ``udf`` array is used.
    Entries without a name are ignored.
    """
    mf_info = target_et.get("mappingFunctionInfo")
    if isinstance(mf_info, dict):
        udf_name = mf_info.get("name")
        return [(udf_name, mf_info.get("type"))] if udf_name else []

    udf_cfg = target_et.get("udf") or []
    if not isinstance(udf_cfg, list):
        return []
    return [
        (udf.get("name"), udf.get("type"))
        for udf in udf_cfg
        if isinstance(udf, dict) and udf.get("name")
    ]


def _udf_export_entry(target_ae: Dict[str, Any], udf_name: Any, udf_type: Any) -> Dict[str, Any]:
    """Describe one UDF to export: its name, type and the agentId of its Target agent."""
    raw_agent_id = target_ae.get("agentId") or target_ae.get("id")
    return {
        "name": str(udf_name),
        "type": udf_type,
        "agentId": str(raw_agent_id) if raw_agent_id is not None else None,
    }


def discover_udfs_from_entities(
    entities: Any,
) -> Dict[str, Dict[str, Any]]:
    """Collect entity-level mapping functions (UDFs) referenced by Target agentEntities.

    Prefers ``mappingFunctionInfo``; falls back to the legacy ``udf`` array on
    entityType. Returns a dict keyed by UDF name with ``name``, ``type``, and
    ``agentId`` (used for the ``udf-<agentId>/`` ZIP folder layout).
    """
    udfs_to_export: Dict[str, Dict[str, Any]] = {}
    if not isinstance(entities, list):
        return udfs_to_export

    for ent in entities:
        if not isinstance(ent, dict):
            continue

        target_ae = _target_agent_entity(ent)
        if not target_ae:
            continue

        target_et = target_ae.get("entityType") or {}
        for udf_name, udf_type in _referenced_udfs(target_et):
            key = str(udf_name)
            if key not in udfs_to_export:
                udfs_to_export[key] = _udf_export_entry(target_ae, udf_name, udf_type)

    return udfs_to_export


def _udf_source_files(
    *,
    token: str,
    pipeline_id: str,
    udfs_to_export: Dict[str, Dict[str, Any]],
):
    """Yield ``(relative_path, code)`` for each UDF source CoreHub returns, under ``udf-<agentId>/``.

    Layout matches what Automator import already understands
    (``create_user_defined_functions.UDF_PATH`` + ``udf-*`` folder scan).
    """
    for udf_name, meta in udfs_to_export.items():
        try:
            response = fetch_core_hub(
                f"/pipelines/{pipeline_id}/config/entities/mapping-functions/{udf_name}",
                token=token,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "Failed to fetch mapping function %s for pipeline %s: %s",
                udf_name,
                pipeline_id,
                exc,
            )
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
            logger.warning(
                "Mapping function %s for pipeline %s has no code to export",
                udf_name,
                pipeline_id,
            )
            continue

        ext = _mapping_function_extension(mf_type)
        safe_udf_name = _safe_path_component(str(udf_name), default="udf")
        agent_id = meta.get("agentId")
        if agent_id is not None:
            folder = f"udf-{_safe_path_component(str(agent_id))}"
        else:
            folder = "udf-unknown"
        yield f"{folder}/{safe_udf_name}{ext}", code


def write_udf_sources_to_zip(
    zip_file: zipfile.ZipFile,
    *,
    token: str,
    pipeline_id: str,
    udfs_to_export: Dict[str, Dict[str, Any]],
    root_prefix: str = "",
) -> int:
    """Fetch UDF source from CoreHub and write under ``udf-<agentId>/`` in the ZIP.

    Returns the number of UDF files written.
    """
    written = 0
    for relative_path, code in _udf_source_files(token=token, pipeline_id=pipeline_id, udfs_to_export=udfs_to_export):
        udf_filename = f"{root_prefix}{relative_path}"
        zip_file.writestr(udf_filename, code.encode("utf-8"))
        written += 1
        logger.info("Exported mapping function of pipeline %s to %s", pipeline_id, udf_filename)
    return written


def write_udf_sources_to_dir(
    root: Path,
    *,
    token: str,
    pipeline_id: str,
    udfs_to_export: Dict[str, Dict[str, Any]],
) -> int:
    """Fetch UDF source from CoreHub and write it under ``root/udf-<agentId>/``, the layout of a backup ZIP.

    Returns the number of UDF files written.
    """
    written = 0
    for relative_path, code in _udf_source_files(token=token, pipeline_id=pipeline_id, udfs_to_export=udfs_to_export):
        udf_file = root / relative_path
        udf_file.parent.mkdir(parents=True, exist_ok=True)
        udf_file.write_text(code, encoding="utf-8")
        written += 1
        logger.info("Exported mapping function of pipeline %s to %s", pipeline_id, udf_file)
    return written


def _function_collection_files(token: str, pipeline_id: str) -> List[Tuple[str, str]]:
    """``(path, code)`` of the sources of the function collection of a pipeline; empty when it has none.

    A pipeline without a collection (CoreHub answers 404, as a CoreHub before 2.3 does) has no files. Any other
    failure raises :class:`function_collection.FunctionCollectionError`, so a backup does not silently lose the
    collection a restore needs.
    """
    return function_collection.function_collection_files(token, pipeline_id, fetch=fetch_core_hub)


def write_function_collection_to_zip(
    zip_file: zipfile.ZipFile,
    *,
    pipeline_id: str,
    files: List[Tuple[str, str]],
    root_prefix: str = "",
) -> int:
    """Write the function collection sources of a pipeline under ``function-collection/`` in the ZIP.

    Returns the number of files written.
    """
    for relative_path, code in files:
        member = f"{root_prefix}{function_collection.FUNCTION_COLLECTION_FOLDER}/{relative_path}"
        zip_file.writestr(member, code.encode("utf-8"))
        logger.info("Exported function collection source of pipeline %s to %s", pipeline_id, member)
    return len(files)


def _entity_udf_names(entities: Any) -> Dict[str, str]:
    """``{entityName: UDF name}`` of the entities whose target refers to a mapping function."""
    names: Dict[str, str] = {}
    for ent in entities if isinstance(entities, list) else []:
        if not isinstance(ent, dict):
            continue
        for udf_name in discover_udfs_from_entities([ent]):
            names[str(ent.get("entityName") or "")] = udf_name
    return names


def export_pipeline_entities_package(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    include_secrets: bool = False,
    chronos_token: Optional[str] = None,
) -> Tuple[bytes, str, str]:
    """Export entities YAML and, when referenced, UDF sources (GSSD-1355) and the function collection.

    Entity-only export historically returned bare YAML, which references UDFs
    via ``customProperties.target.udf`` / ``mappingFunctionInfo`` but omitted
    the source files. Full pipeline backup already wrote ``udf-<agentId>/``.
    This package brings entity-only export in line: when UDFs are referenced,
    return a ZIP containing the YAML plus those sources so import can compile
    them; otherwise return plain YAML for backward compatibility. A pipeline with a function collection
    (Gluesync 2.3) always gets the ZIP, with the collection sources under ``function-collection/``: the entities
    bind its functions, so they cannot be restored without it.

    Returns:
        (payload_bytes, media_type, download_filename)
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    yaml_content = export_pipeline_yaml(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
        include_secrets=include_secrets,
        chronos_token=chronos_token,
    )

    # Discover UDFs from live entities (same source full backup uses).
    try:
        entities = fetch_pipeline_entities(token, pipeline_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(
            _UDF_ENTITIES_FETCH_FAILED_LOG,
            pipeline_id,
            exc,
        )
        entities = []

    udfs_to_export = discover_udfs_from_entities(entities)
    collection_files = _function_collection_files(token, pipeline_id)

    pipeline_name = pipeline_id
    try:
        pipeline_resp = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
        if isinstance(pipeline_resp, dict) and pipeline_resp.get("name"):
            pipeline_name = str(pipeline_resp["name"])
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug(
            "export_pipeline_entities_package: could not fetch pipeline name: %s",
            exc,
        )

    safe_name = "".join(
        c for c in pipeline_name if c.isalnum() or c in (" ", "-", "_")
    ).rstrip() or pipeline_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if not udfs_to_export and not collection_files:
        filename = f"backup_{pipeline_id}_{timestamp}.yaml"
        return yaml_content.encode("utf-8"), "application/x-yaml", filename

    # ZIP: YAML + udf-<agentId>/ sources + function-collection/ sources (flat layout; import already handles this).
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        yaml_name = f"backup_{safe_name}_{pipeline_id}_{timestamp}.yaml"
        zip_file.writestr(yaml_name, yaml_content)
        write_udf_sources_to_zip(
            zip_file,
            token=token,
            pipeline_id=pipeline_id,
            udfs_to_export=udfs_to_export,
            root_prefix="",
        )
        write_function_collection_to_zip(zip_file, pipeline_id=pipeline_id, files=collection_files)

    zip_buffer.seek(0)
    filename = f"backup_{safe_name}_{pipeline_id}_{timestamp}.zip"
    logger.info(
        "Entity-only export for pipeline %s includes %d UDF source(s) and %d function collection source(s)",
        pipeline_id,
        len(udfs_to_export),
        len(collection_files),
    )
    return zip_buffer.read(), "application/zip", filename


def _enrich_null_column_types(
    schemas: Dict[str, Any],
    entities: Any,
    token: str,
    pipeline_id: str,
) -> None:
    """Fill null column types of ``schemas`` from live CoreHub discovery; failures are only logged."""
    try:
        enriched = enrich_null_column_types_from_discovery(
            schemas, entities, token, pipeline_id
        )
        if enriched:
            logger.info(
                "export_pipeline_yaml: enriched null column types for %d table(s) "
                "via live CoreHub discovery for pipeline %s",
                enriched,
                pipeline_id,
            )
    except Exception as enrich_exc:  # pylint: disable=broad-except
        logger.warning(
            "export_pipeline_yaml: column-type enrichment failed for pipeline %s: %s",
            pipeline_id,
            enrich_exc,
        )


def _add_global_notifications(
    yaml_data: Dict[str, Any],
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    include_secrets: bool,
) -> None:
    """Embed the global notification settings (SMTP) into ``yaml_data``; failures are only logged."""
    try:
        notification_cfg = fetch_global_notification_config(
            token=token,
            base_url=base_url,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
            include_secrets=include_secrets,
        )
        if notification_cfg and yaml_data:
            yaml_data["globalNotifications"] = notification_cfg
            logger.info(
                "Added global notification config to YAML export for pipeline %s",
                pipeline_id,
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Failed to add global notification config to YAML export for pipeline %s: %s",
            pipeline_id,
            exc,
        )


def export_pipeline_yaml(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    skip_schedules: bool = False,
    include_secrets: bool = False,
    include_global_config: bool = True,
    chronos_token: Optional[str] = None,
) -> str:
    """Export a single pipeline configuration to YAML text for download.

    Duplicate source tables are handled by creating unique keys (e.g., table@@2, table@@3)
    within the same YAML file to avoid key conflicts.

    Args:
        skip_schedules: If True, do not embed pipeline/group/entity schedules.
                        Useful when schedules are backed up separately (e.g. full CoreHub backup).
        include_secrets: If True, preserve encrypted secrets in agent credentials backup.
        include_global_config: If True, embed global notification settings (SMTP) in this YAML.
                        Set to False when the global config is written to a separate file
                        (e.g. inside a ZIP backup) to avoid duplication.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Refuse to export a pipeline that is in maintenance mode: its agents are
    # suspended and entities stopped, so the exported configuration would be
    # incomplete/unreliable. The caller is expected to surface a clear message.
    if get_pipeline_maintenance_state(
        token=token,
        base_url=base_url,
        pipeline_id=pipeline_id,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    ):
        logger.info(
            "Refusing to export pipeline %s: it is currently in maintenance mode.",
            pipeline_id,
        )
        raise PipelineInMaintenanceError(
            f"Pipeline {pipeline_id} is currently in maintenance mode and cannot "
            "be backed up. Exit maintenance mode on CoreHub and try again."
        )

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

    if not skip_schedules:
        jobs = fetch_pipeline_jobs(pipeline_id, token=chronos_token or token)
        attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

    # Backfill null column types that are caused by legacy entities whose
    # DataTypeInterface was not registered for serialisation and therefore
    # stored as null in SQLite.  We call live discovery for each affected
    # table so the exported YAML always carries real type information.
    _enrich_null_column_types(schemas, entities, token, pipeline_id)

    # Clean up internal metadata before export
    for schema_cfg in schemas.values():
        # Remove duplicate tracking metadata (internal use only)
        schema_cfg.pop("has_duplicates", None)
        # Note: sourceTableName is preserved as it's needed during import

    yaml_data = build_yaml_structure(schemas, groups_by_name)

    # Embed pipeline identity as a first-class YAML key so imports can
    # resolve the original pipeline without parsing the backup filename.
    pipeline_name = pipeline_id
    try:
        pipeline_resp = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
        if isinstance(pipeline_resp, dict) and pipeline_resp.get("name"):
            pipeline_name = str(pipeline_resp["name"])
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("export_pipeline_yaml: could not fetch pipeline name: %s", exc)
    yaml_data = {
        "exportMetadata": {
            "pipelineId": pipeline_id,
            "pipelineName": pipeline_name,
        },
        **(yaml_data or {}),
    }

    # Add global notification settings (SMTP) to the YAML export.
    # Skipped when the caller writes global config to a separate file
    # (e.g. inside a ZIP backup) to avoid duplication.
    if include_global_config:
        _add_global_notifications(
            yaml_data,
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
            include_secrets=include_secrets,
        )

    buffer = io.StringIO()
    yaml.safe_dump(
        yaml_data or {},
        buffer,
        sort_keys=False,
        allow_unicode=True,
    )

    # Prepend provenance header so recipients can identify the source instance
    header = build_export_header(token, pipeline_id, base_url=base_url)
    return header + buffer.getvalue()


@dataclass
class _PipelinesBackup:
    """Settings of a pipelines backup ZIP and the agent configurations gathered while writing it."""

    token: str
    base_url: str
    use_ssl: Optional[bool]
    skip_verify: Optional[bool]
    include_secrets: bool
    chronos_token: Optional[str]
    skip_schedules: bool = False
    agents: List[Dict[str, Any]] = field(default_factory=list)
    seen_agents: set = field(default_factory=set)
    pipelines_meta: List[Dict[str, Any]] = field(default_factory=list)
    function_collection_errors: List[str] = field(default_factory=list)


def _backup_pipeline_meta(backup: _PipelinesBackup, pipeline_id: str, pipeline_name: str) -> Dict[str, Any]:
    """Register a pipeline in the backup and return its metadata (agents are referenced from it later)."""
    pipeline_meta: Dict[str, Any] = {
        "pipelineId": pipeline_id,
        "pipelineName": pipeline_name,
    }
    backup.pipelines_meta.append(pipeline_meta)
    return pipeline_meta


def _backup_yaml_filename(pipeline_name: str, pipeline_id: str) -> str:
    """``backup_<name>_<pipelineId>_<timestamp>.yaml``, the name of a pipeline YAML inside a backup ZIP."""
    safe_name = "".join(c for c in pipeline_name if c.isalnum() or c in (" ", "-", "_")).rstrip()
    if not safe_name:
        safe_name = pipeline_id
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"backup_{safe_name}_{pipeline_id}_{timestamp}.yaml"


def _fetch_udf_entities(token: str, pipeline_id: str) -> Any:
    """Entities of a pipeline, used to discover its mapping functions; empty when CoreHub fails."""
    try:
        return fetch_pipeline_entities(token, pipeline_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(_UDF_ENTITIES_FETCH_FAILED_LOG, pipeline_id, exc)
        return []


def _backup_agent_identity(agent: Any) -> Optional[Tuple[Any, Any, Any, Any]]:
    """``(agentType, agentTag, hostCredentials, agentId)`` of an agent worth backing up, else None."""
    if not isinstance(agent, dict):
        return None
    agent_type = agent.get("agentType")
    agent_tag = agent.get("agentTag")
    host_credentials = agent.get("hostCredentials") or {}
    raw_agent_id = agent.get("agentId") or agent.get("id")
    if not agent_type or not agent_tag or not host_credentials:
        return None
    return agent_type, agent_tag, host_credentials, raw_agent_id


def _backup_agent_key(agent_type: Any, agent_tag: Any, host_credentials: Any, raw_agent_id: Any) -> tuple:
    """Deduplication key of an agent: its agentId when available, otherwise type, tag and connection details.

    Both kinds of key have the same length; the first item keeps them apart, so an agentId key only ever
    equals the key of the same agentId.
    """
    if raw_agent_id is not None:
        return ("id", str(raw_agent_id), "", "", "", "")
    return (
        "props",
        str(agent_type),
        str(agent_tag),
        str(host_credentials.get("connectionName")),
        str(host_credentials.get("host")),
        str(host_credentials.get("port")),
    )


def _backup_agent_entry(agent: Dict[str, Any], identity: Tuple[Any, Any, Any, Any], include_secrets: bool) -> Dict[str, Any]:
    """agents-config.yaml entry of an agent; its password is masked unless ``include_secrets``."""
    agent_type, agent_tag, host_credentials, raw_agent_id = identity
    export_host_credentials = dict(host_credentials)
    if not include_secrets and "password" in export_host_credentials and export_host_credentials["password"]:
        export_host_credentials["password"] = _MASKED_SECRET

    agent_payload: Dict[str, Any] = {
        "agentType": agent_type,
        "agentTag": agent_tag,
        "hostCredentials": export_host_credentials,
        # Keep the same naming as config.json/example-config.json
        "customHostCredentials": agent.get("customHostCredentials") or {},
        "specificConfiguration": agent.get("specificConfiguration") or {},
    }
    if raw_agent_id is not None:
        agent_payload["agentId"] = str(raw_agent_id)
    return agent_payload


def _collect_backup_agents(backup: _PipelinesBackup, pipeline_agents: List[Any]) -> List[Dict[str, Any]]:
    """Add the agents of one pipeline to the backup (once each) and return the pipeline's references to them."""
    pipeline_agent_refs: List[Dict[str, Any]] = []
    for agent in pipeline_agents:
        identity = _backup_agent_identity(agent)
        if identity is None:
            continue
        agent_type, agent_tag, _, raw_agent_id = identity

        ref: Dict[str, Any] = {
            "agentType": agent_type,
            "agentTag": agent_tag,
        }
        if raw_agent_id is not None:
            ref["agentId"] = str(raw_agent_id)
        pipeline_agent_refs.append(ref)

        key = _backup_agent_key(*identity)
        if key in backup.seen_agents:
            continue
        backup.seen_agents.add(key)
        backup.agents.append(_backup_agent_entry(agent, identity, backup.include_secrets))
    return pipeline_agent_refs


def _add_backup_pipeline_agents(backup: _PipelinesBackup, pipeline_id: str, pipeline_meta: Dict[str, Any]) -> None:
    """Collect the agents of a pipeline into the backup and reference them from the pipeline metadata."""
    try:
        pipeline_agents = get_pipeline_agents(backup.token, pipeline_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to fetch agents for pipeline %s: %s", pipeline_id, exc)
        pipeline_agents = []

    if isinstance(pipeline_agents, list):
        pipeline_agent_refs = _collect_backup_agents(backup, pipeline_agents)
        if pipeline_agent_refs:
            pipeline_meta["agents"] = pipeline_agent_refs


def _annotate_masked_passwords(yaml_text: str) -> str:
    """Append a comment to every masked password line so the obfuscation is clear to readers."""
    yaml_text_lines = []
    for line in yaml_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("password:") and _MASKED_SECRET in stripped:
            # Preserve indentation when appending the comment
            line = f"{line}  # original password omitted"
        yaml_text_lines.append(line)
    return "\n".join(yaml_text_lines) + "\n"


def _write_backup_agents_config(zip_file: zipfile.ZipFile, backup: _PipelinesBackup, root_prefix: str = "") -> None:
    """Write the collected agents and the pipelines referencing them to agents-config.yaml, if any agent was found."""
    if not backup.agents:
        return
    agents_payload: Dict[str, Any] = {"agents": backup.agents}
    if backup.pipelines_meta:
        agents_payload["pipelines"] = backup.pipelines_meta

    buffer = io.StringIO()
    yaml.safe_dump(
        agents_payload,
        buffer,
        sort_keys=False,
        allow_unicode=True,
    )
    zip_file.writestr(f"{root_prefix}agents-config.yaml", _annotate_masked_passwords(buffer.getvalue()))


def _write_backup_global_notifications(zip_file: zipfile.ZipFile, backup: _PipelinesBackup, root_prefix: str = "") -> bool:
    """Write the global notification settings (SMTP) to global-config.yaml; False when CoreHub has none."""
    notification_cfg = fetch_global_notification_config(
        token=backup.token,
        base_url=backup.base_url,
        use_ssl=backup.use_ssl,
        skip_verify=backup.skip_verify,
        include_secrets=backup.include_secrets,
    )
    if not notification_cfg:
        return False
    buffer = io.StringIO()
    yaml.safe_dump(
        {"globalNotifications": notification_cfg},
        buffer,
        sort_keys=False,
        allow_unicode=True,
    )
    zip_file.writestr(f"{root_prefix}global-config.yaml", buffer.getvalue())
    return True


def _full_backup_pipeline_name(token: str, pipeline_id: str) -> str:
    """Name of the pipeline to back up; raise PipelineInMaintenanceError when it is in maintenance mode."""
    pipeline_name = pipeline_id
    try:
        response = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Failed to fetch pipeline %s metadata", pipeline_id)
        raise
    if isinstance(response, dict):
        name = response.get("name")
        if isinstance(name, str) and name:
            pipeline_name = name
        # A pipeline in maintenance mode has suspended agents and stopped
        # entities, so its configuration cannot be exported reliably. Refuse
        # the backup with a clear message instead of producing an incomplete
        # archive.
        if bool(response.get("isInMaintenanceMode", False)):
            logger.info(
                "Refusing to export full backup for pipeline %s (%s): it is "
                "currently in maintenance mode.",
                pipeline_name,
                pipeline_id,
            )
            raise PipelineInMaintenanceError(
                f"Pipeline {pipeline_name} ({pipeline_id}) is currently in "
                "maintenance mode and cannot be backed up. Exit maintenance "
                "mode on CoreHub and try again."
            )
    return pipeline_name


def export_pipeline_full_backup(
    *,
    token: str,
    base_url: str,
    pipeline_id: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    include_secrets: bool = False,
    chronos_token: Optional[str] = None,
) -> bytes:
    """Export a full backup (YAML + agents-config + UDFs + function collection) for a single pipeline."""

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    pipeline_name = _full_backup_pipeline_name(token, pipeline_id)
    backup = _PipelinesBackup(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
        include_secrets=include_secrets,
        chronos_token=chronos_token,
    )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Top-level folder for this pipeline inside the ZIP
        root_prefix = f"pipeline_{pipeline_id}/"
        pipeline_meta = _backup_pipeline_meta(backup, pipeline_id, pipeline_name)

        # Export pipeline YAML configuration. Global config is written to a
        # separate global-config.yaml below, so don't embed it here.
        yaml_content = export_pipeline_yaml(
            token=token,
            base_url=base_url,
            pipeline_id=pipeline_id,
            use_ssl=use_ssl,
            skip_verify=skip_verify,
            include_secrets=include_secrets,
            include_global_config=False,
            chronos_token=chronos_token,
        )
        zip_file.writestr(f"{root_prefix}{_backup_yaml_filename(pipeline_name, pipeline_id)}", yaml_content)
        logger.info("Exported pipeline %s (%s) YAML", pipeline_name, pipeline_id)

        # Discover mapping functions (UDFs) referenced by this pipeline
        udfs_to_export = discover_udfs_from_entities(_fetch_udf_entities(token, pipeline_id))

        # The function collection the entities and UDFs call (a failure to read it fails the backup)
        write_function_collection_to_zip(
            zip_file,
            pipeline_id=pipeline_id,
            files=_function_collection_files(token, pipeline_id),
            root_prefix=root_prefix,
        )

        _add_backup_pipeline_agents(backup, pipeline_id, pipeline_meta)
        _write_backup_agents_config(zip_file, backup, root_prefix)

        # Export global notification settings (SMTP)
        try:
            if _write_backup_global_notifications(zip_file, backup, root_prefix):
                logger.info(
                    "Exported global notification config for pipeline %s",
                    pipeline_id,
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Failed to export global notification config for pipeline %s: %s",
                pipeline_id,
                exc,
            )

        # Export mapping function (UDF) source files, if any were discovered.
        write_udf_sources_to_zip(
            zip_file,
            token=token,
            pipeline_id=pipeline_id,
            udfs_to_export=udfs_to_export,
            root_prefix=root_prefix,
        )

    zip_buffer.seek(0)
    return zip_buffer.read()


def _export_pipeline_into_backup(
    zip_file: zipfile.ZipFile,
    backup: _PipelinesBackup,
    pipeline_id: str,
    pipeline_name: str,
    udfs_to_export: Dict[Tuple[str, str], Dict[str, Any]],
) -> None:
    """Add one pipeline to an all-pipelines backup: its YAML, function collection, mapping functions and agents."""
    pipeline_meta = _backup_pipeline_meta(backup, pipeline_id, pipeline_name)

    try:
        # Export individual pipeline
        yaml_content = export_pipeline_yaml(
            token=backup.token,
            base_url=backup.base_url,
            pipeline_id=pipeline_id,
            use_ssl=backup.use_ssl,
            skip_verify=backup.skip_verify,
            skip_schedules=backup.skip_schedules,
            include_secrets=backup.include_secrets,
            include_global_config=False,
            chronos_token=backup.chronos_token,
        )
        zip_file.writestr(_backup_yaml_filename(pipeline_name, pipeline_id), yaml_content)
        logger.info("Exported pipeline %s (%s)", pipeline_name, pipeline_id)

    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export pipeline %s: %s", pipeline_id, exc)
        # Continue with other pipelines

    # The function collection stays with its pipeline, under pipeline_<id>/ (UDFs are told apart by agentId, a
    # collection is not). One that cannot be read is listed in the ZIP and logged, and the other pipelines go on.
    try:
        write_function_collection_to_zip(
            zip_file,
            pipeline_id=pipeline_id,
            files=_function_collection_files(backup.token, pipeline_id),
            root_prefix=f"pipeline_{pipeline_id}/",
        )
    except function_collection.FunctionCollectionError as exc:
        logger.exception("Function collection of pipeline %s is NOT in the backup: %s", pipeline_id, exc)
        backup.function_collection_errors.append(f"{pipeline_name} ({pipeline_id}): {exc}")

    # Discover mapping functions (UDFs) referenced by this pipeline so we can
    # export their source code once per (pipeline, udfName) pair.
    entities = _fetch_udf_entities(backup.token, pipeline_id)
    for udf_name, meta in discover_udfs_from_entities(entities).items():
        udfs_to_export.setdefault((pipeline_id, udf_name), meta)

    _add_backup_pipeline_agents(backup, pipeline_id, pipeline_meta)


def _write_all_pipelines_udf_sources(
    zip_file: zipfile.ZipFile,
    token: str,
    udfs_to_export: Dict[Tuple[str, str], Dict[str, Any]],
) -> None:
    """Write the source of every discovered mapping function under ``udf-<agentId>/`` in the ZIP."""
    for (pipeline_id, udf_name), meta in udfs_to_export.items():
        for udf_filename, code in _udf_source_files(
            token=token,
            pipeline_id=pipeline_id,
            udfs_to_export={udf_name: meta},
        ):
            # Store source code as UTF-8 text inside the ZIP
            zip_file.writestr(udf_filename, code.encode("utf-8"))


def _write_function_collection_errors_note(zip_file: zipfile.ZipFile, errors: List[str]) -> None:
    """List in the ZIP the pipelines whose function collection could not be read, so the gap is not silent."""
    if not errors:
        return
    lines = [
        "# The function collection of the following pipelines could not be read at backup",
        "# time and is NOT in this archive: restoring their entities that call its functions",
        "# fails until the collection is published again. Fix the cause and run the backup again.",
        "",
    ]
    lines.extend(f"- {error}" for error in errors)
    zip_file.writestr("function-collection-errors.txt", ("\n".join(lines) + "\n").encode("utf-8"))
    logger.warning(
        "Backup completed but the function collection of %d pipeline(s) is missing: see "
        "function-collection-errors.txt in the archive",
        len(errors),
    )


def _write_skipped_maintenance_note(
    zip_file: zipfile.ZipFile,
    skipped_maintenance: List[str],
    exported_count: int,
) -> None:
    """Log how the backup went and list in the ZIP the pipelines skipped because of maintenance mode."""
    if not skipped_maintenance:
        logger.info(
            "Backup completed: %d pipeline(s) exported, none in maintenance mode.",
            exported_count,
        )
        return

    # Leave a clear, human-readable trace of any pipeline that was excluded
    # from the backup because it was in maintenance mode. This makes the
    # skip visible to operators who inspect the backup archive without
    # having to dig through server logs.
    skipped_lines = [
        "# The following pipelines were in maintenance mode at backup "
        "# time and have been excluded from this archive.",
        "# Exit maintenance mode on CoreHub and run the backup again to "
        "# include them.",
        "",
    ]
    skipped_lines.extend(f"- {name}" for name in skipped_maintenance)
    zip_file.writestr(
        "skipped-maintenance-pipelines.txt",
        ("\n".join(skipped_lines) + "\n").encode("utf-8"),
    )
    logger.warning(
        "Backup completed but %d pipeline(s) were skipped because they "
        "are in maintenance mode: %s",
        len(skipped_maintenance),
        ", ".join(skipped_maintenance),
    )


def export_all_pipelines_yaml(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    skip_schedules: bool = False,
    include_secrets: bool = False,
    chronos_token: Optional[str] = None,
) -> bytes:
    """Export all pipelines to a ZIP file containing individual YAML files (and UDF and function collection sources).

    Args:
        skip_schedules: If True, do not embed schedules in pipeline YAMLs.
                        Useful when schedules are backed up separately (e.g. full CoreHub backup).
        include_secrets: If True, preserve encrypted secrets in agent credentials backup.
    """

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    # Get all pipelines
    pipelines = list_pipelines(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
    )

    # Accumulate agent configurations across pipelines
    backup = _PipelinesBackup(
        token=token,
        base_url=base_url,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
        include_secrets=include_secrets,
        chronos_token=chronos_token,
        skip_schedules=skip_schedules,
    )

    # Create ZIP file in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Mapping functions (UDFs) referenced by entities, exported once per
        # (pipeline, udfName) pair.
        # Key: (pipeline_id, udf_name) -> {"name": str, "type": Optional[Any], "agentId": Optional[str]}
        udfs_to_export: Dict[Tuple[str, str], Dict[str, Any]] = {}

        skipped_maintenance: List[str] = []

        for pipeline in pipelines:
            pipeline_id = pipeline['id']
            pipeline_name = pipeline.get('name', pipeline_id)

            # Pipelines in maintenance mode have their agents suspended and
            # entities stopped, so their configuration cannot be exported
            # reliably. Skip them from the backup and leave a clear trace in
            # the logs so operators know why a pipeline is missing.
            if pipeline.get("isInMaintenanceMode"):
                logger.info(
                    "Skipping pipeline %s (%s): it is currently in maintenance "
                    "mode and has been excluded from the backup.",
                    pipeline_name,
                    pipeline_id,
                )
                skipped_maintenance.append(
                    f"{pipeline_name} ({pipeline_id})"
                )
                continue

            _export_pipeline_into_backup(zip_file, backup, pipeline_id, pipeline_name, udfs_to_export)

        # After processing all pipelines, write a consolidated agents-config.yaml
        # if any agents were found, and export UDF source files when present.
        _write_backup_agents_config(zip_file, backup)

        # Export global notification settings (SMTP) once for the whole backup.
        try:
            if _write_backup_global_notifications(zip_file, backup):
                logger.info("Exported global notification config to all-pipelines backup")
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Failed to export global notification config to all-pipelines backup: %s",
                exc,
            )

        _write_all_pipelines_udf_sources(zip_file, token, udfs_to_export)
        _write_function_collection_errors_note(zip_file, backup.function_collection_errors)
        _write_skipped_maintenance_note(zip_file, skipped_maintenance, len(backup.pipelines_meta))

    zip_buffer.seek(0)
    return zip_buffer.read()


def export_global_configs(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export all global CoreHub configuration settings."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "grafana": "/global-config/grafana",
        "logging": "/global-config/logging",
        "logging_level": "/global-config/logging/level",
        "telemetry": "/global-config/logging/telemetry",
        "release_channel": "/global-config/release-channel",
        "license": "/global-config/license",
        "session": "/global-config/session",
    }

    configs: Dict[str, Any] = {}
    for key, path in endpoints.items():
        try:
            response = fetch_core_hub(path, token=token)
            if isinstance(response, dict):
                configs[key] = response
            else:
                configs[key] = {"value": response}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to fetch global config %s: %s", key, exc)
            configs[key] = {"error": str(exc)}

    return configs


def export_smtp_settings(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export SMTP configuration from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    try:
        response = fetch_core_hub(_SMTP_CONFIG_PATH, token=token, params={"include_secrets": "true"})
        if isinstance(response, dict):
            return {"smtp": response}
        return {"smtp": {"value": response}}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch SMTP settings: %s", exc)
        return {"smtp": {"error": str(exc)}}


def export_webhooks_config(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export webhooks configuration from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "webhooks": "/global-config/webhooks",
        "retention": "/global-config/webhooks/retention",
        "event_types": "/global-config/webhooks/event-types",
        "delivery_logs": "/global-config/webhooks/delivery-logs",
        "dead_letters": "/global-config/webhooks/dead-letters",
    }

    configs: Dict[str, Any] = {}
    for key, path in endpoints.items():
        try:
            response = fetch_core_hub(path, token=token)
            if isinstance(response, dict):
                configs[key] = response
            else:
                configs[key] = {"value": response}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to fetch webhooks config %s: %s", key, exc)
            configs[key] = {"error": str(exc)}

    return configs


def export_thresholds(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export thresholds configuration from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "thresholds": "/global-config/thresholds",
        "pattern_status": "/global-config/thresholds/pattern-status",
        "simulate": "/global-config/thresholds/simulate",
    }

    configs: Dict[str, Any] = {}
    for key, path in endpoints.items():
        try:
            response = fetch_core_hub(path, token=token)
            if isinstance(response, dict):
                configs[key] = response
            else:
                configs[key] = {"value": response}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to fetch thresholds config %s: %s", key, exc)
            configs[key] = {"error": str(exc)}

    return configs


def _normalize_paginated_response(response: Any) -> Any:
    """Normalize a Chronos response for the backup.

    A paginated ``{"items": [...], "total": N}`` becomes the plain list of items, any other
    dict or a list is kept as is, and a scalar is wrapped as ``{"value": ...}``.
    """
    if isinstance(response, dict):
        items = response.get("items")
        return items if isinstance(items, list) else response
    if isinstance(response, list):
        return response
    return {"value": response}


def export_schedules(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    chronos_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Export Chronos scheduler jobs and settings from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    configs: Dict[str, Any] = {}
    try:
        jobs_response = fetch_core_hub("/chronos/api/jobs", token=chronos_token or token)
        configs["jobs"] = _normalize_paginated_response(jobs_response)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch scheduler jobs: %s", exc)
        configs["jobs"] = {"error": str(exc)}

    try:
        settings_response = fetch_core_hub("/chronos/api/settings", token=chronos_token or token)
        configs["settings"] = _normalize_paginated_response(settings_response)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch scheduler settings: %s", exc)
        configs["settings"] = {"error": str(exc)}

    return configs


def export_users(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export user management configuration from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    try:
        response = fetch_core_hub(_USERS_PATH, token=token, params={"include_secrets": "true"})
        if isinstance(response, dict):
            return {"users": response}
        if isinstance(response, list):
            return {"users": response}
        return {"users": {"value": response}}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch users: %s", exc)
        return {"users": {"error": str(exc)}}


def export_oidc_config(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export OIDC configuration from CoreHub."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    configs: Dict[str, Any] = {}

    # OIDC configuration - include secrets for backup/restore
    try:
        response = fetch_core_hub("/oidc/configuration", token=token, params={"include_secrets": "true"})
        if isinstance(response, dict):
            configs["oidc_configuration"] = response
        else:
            configs["oidc_configuration"] = {"value": response}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch OIDC configuration: %s", exc)
        configs["oidc_configuration"] = {"error": str(exc)}

    # OIDC auth URL (no secrets here)
    try:
        response = fetch_core_hub("/oidc/auth-url", token=token)
        if isinstance(response, dict):
            configs["oidc_auth_url"] = response
        else:
            configs["oidc_auth_url"] = {"value": response}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch OIDC auth URL: %s", exc)
        configs["oidc_auth_url"] = {"error": str(exc)}

    return configs


def _dump_to_yaml(data: Dict[str, Any]) -> str:
    """Serialize a dict to a YAML string."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def export_full_corehub_backup(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    include_secrets: bool = False,
    chronos_token: Optional[str] = None,
    ai_settings_passphrase: Optional[str] = None,
    ai_settings_current_password: Optional[str] = None,
) -> bytes:
    """Export a complete CoreHub backup including pipelines, global configs, SMTP, webhooks, thresholds, and schedules.

    Each domain is stored in its own YAML file inside the ZIP for modularity:
      - global-configs.yaml
      - smtp.yaml
      - webhooks.yaml
      - thresholds.yaml
      - schedules.yaml
      - pipelines/ (individual pipeline YAMLs)
      - agents-config.yaml
      - user-groups.yaml (user groups with their members and data-access policies, Core Hub 2.3.0.1+)
      - data-access-policies.yaml (the Query Studio data-access policies of each user)
      - ai-settings.json (Core Hub 2.3.0.1+; its secrets are included only with
        include_secrets and an ai_settings_passphrase, sealed with it, and need
        ai_settings_current_password, the signed-in user's password)
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Global configs
        try:
            global_configs = export_global_configs(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            zip_file.writestr(_GLOBAL_CONFIGS_BACKUP_FILE, _dump_to_yaml(global_configs).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export global configs: %s", exc)
            zip_file.writestr(_GLOBAL_CONFIGS_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # SMTP settings
        try:
            smtp = export_smtp_settings(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            zip_file.writestr(_SMTP_BACKUP_FILE, _dump_to_yaml(smtp).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export SMTP settings: %s", exc)
            zip_file.writestr(_SMTP_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # Webhooks config
        try:
            webhooks = export_webhooks_config(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            zip_file.writestr(_WEBHOOKS_BACKUP_FILE, _dump_to_yaml(webhooks).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export webhooks config: %s", exc)
            zip_file.writestr(_WEBHOOKS_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # Thresholds config
        try:
            thresholds = export_thresholds(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            zip_file.writestr(_THRESHOLDS_BACKUP_FILE, _dump_to_yaml(thresholds).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export thresholds: %s", exc)
            zip_file.writestr(_THRESHOLDS_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # Schedules config
        try:
            schedules = export_schedules(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify,
                chronos_token=chronos_token,
            )
            zip_file.writestr(_SCHEDULES_BACKUP_FILE, _dump_to_yaml(schedules).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export schedules: %s", exc)
            zip_file.writestr(_SCHEDULES_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # Users config
        try:
            users = export_users(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            users_comment = (
                "# NOTE: Passwords are exported as hashes only. CoreHub does not accept\n"
                "# password hashes for creation (a plaintext password is required).\n"
                "# After importing this backup:\n"
                "#   - Existing users retain their current password but profile is updated.\n"
                "#   - New users are created with a random temporary password.\n"
                "#   - ALL affected users MUST reset their password via the CoreHub UI.\n"
            )
            zip_file.writestr(
                _USERS_BACKUP_FILE,
                (users_comment + _dump_to_yaml(users)).encode("utf-8"),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export users: %s", exc)
            zip_file.writestr(_USERS_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # OIDC config
        try:
            oidc = export_oidc_config(
                token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify
            )
            zip_file.writestr(_OIDC_BACKUP_FILE, _dump_to_yaml(oidc).encode("utf-8"))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to export OIDC config: %s", exc)
            zip_file.writestr(_OIDC_BACKUP_FILE, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

        # User groups (members and data-access policies) and the data-access policies of each user
        _write_section(zip_file, USER_GROUPS_FILENAME, "user groups", export_user_groups,
                       token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify)
        _write_section(zip_file, DATA_ACCESS_POLICIES_FILENAME, "data-access policies", export_data_access_policies,
                       token=token, base_url=base_url, use_ssl=use_ssl, skip_verify=skip_verify)

        # AI settings: one bundle from Core Hub. An older Core Hub has none to back up.
        _write_ai_settings(
            zip_file,
            token=token,
            include_secrets=include_secrets and bool(ai_settings_passphrase),
            passphrase=ai_settings_passphrase,
            current_password=ai_settings_current_password,
        )

        # Pipelines backup (already a ZIP, extract contents).
        # We skip embedding schedules in pipeline YAMLs because the full backup
        # already includes a separate schedules.yaml with all Chronos jobs.
        try:
            pipelines_zip_bytes = export_all_pipelines_yaml(
                token=token,
                base_url=base_url,
                use_ssl=use_ssl,
                skip_verify=skip_verify,
                skip_schedules=True,
                include_secrets=include_secrets,
                chronos_token=chronos_token,
            )
            with io.BytesIO(pipelines_zip_bytes) as inner_buf:
                with zipfile.ZipFile(inner_buf, "r") as inner_zip:
                    for name in inner_zip.namelist():
                        zip_file.writestr(f"pipelines/{name}", inner_zip.read(name))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to include pipelines in full backup: %s", exc)
            zip_file.writestr("pipelines/error.yaml", _dump_to_yaml({"error": str(exc)}).encode("utf-8"))

    zip_buffer.seek(0)
    return zip_buffer.read()


AI_SETTINGS_ERROR_FILENAME = "ai-settings.error.yaml"


def _write_section(zip_file: zipfile.ZipFile, filename: str, label: str, export: Callable[..., Dict[str, Any]], **kwargs: Any) -> None:
    try:
        data = export(**kwargs)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to export %s: %s", label, exc)
        data = {"error": str(exc)}
    zip_file.writestr(filename, _dump_to_yaml(data).encode("utf-8"))


def _write_ai_settings(
    zip_file: zipfile.ZipFile,
    *,
    token: str,
    include_secrets: bool,
    passphrase: Optional[str],
    current_password: Optional[str] = None,
) -> None:
    try:
        bundle = ai_settings.export_ai_settings(
            token, include_secrets=include_secrets, passphrase=passphrase, current_password=current_password
        )
    except ai_settings.AiSettingsUnsupported as exc:
        logger.info("Skipping AI settings: %s", exc)
        return
    except Exception as exc:  # pylint: disable=broad-except
        # The restore reports this file, so a failed export cannot pass for "no AI settings".
        logger.warning("Failed to export AI settings: %s", exc)
        zip_file.writestr(AI_SETTINGS_ERROR_FILENAME, _dump_to_yaml({"error": str(exc)}).encode("utf-8"))
        return
    zip_file.writestr(
        ai_settings.BACKUP_FILENAME,
        json.dumps(bundle, indent=2, ensure_ascii=False).encode("utf-8"),
    )


def restore_ai_settings(
    *,
    token: str,
    bundle: Dict[str, Any],
    passphrase: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Import an AI settings bundle; the result keeps Core Hub's counts and warnings."""
    try:
        result = ai_settings.import_ai_settings(token, bundle, passphrase=passphrase, dry_run=dry_run)
    except ai_settings.AiSettingsUnsupported as exc:
        return {"status": "skipped", "reason": str(exc)}
    return {
        "status": "checked" if dry_run else "restored",
        "summary": ai_settings.summarize_import(result),
        "sections": result.get("sections", []),
        "warnings": result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# Import / restore helpers for CoreHub global configuration
# ---------------------------------------------------------------------------

def _unwrap_value_if_needed(data: Any) -> Any:
    """If the exported value was wrapped in {'value': ...}, unwrap it."""
    if isinstance(data, dict) and set(data.keys()) == {"value"}:
        return data["value"]
    return data


def import_global_configs(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    configs: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore global CoreHub configuration settings from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "grafana": "/global-config/grafana",
        "logging": "/global-config/logging",
        "logging_level": "/global-config/logging/level",
        "telemetry": "/global-config/logging/telemetry",
        "release_channel": "/global-config/release-channel",
        "license": "/global-config/license",
        "session": "/global-config/session",
    }

    results: Dict[str, Any] = {}
    for key, path in endpoints.items():
        data = configs.get(key)
        if data is None:
            results[key] = {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
            continue
        if isinstance(data, dict) and "error" in data:
            results[key] = {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}
            continue

        payload = _unwrap_value_if_needed(data)
        try:
            fetch_core_hub(path, method="PUT", token=token, body=payload)
            results[key] = {"status": "restored"}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore global config %s: %s", key, exc)
            results[key] = {"status": "failed", "error": str(exc)}

    return results


def import_smtp_settings(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    smtp_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore SMTP configuration from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    smtp = smtp_data.get("smtp")
    if smtp is None:
        return {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
    if isinstance(smtp, dict) and "error" in smtp:
        return {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}

    payload = _unwrap_value_if_needed(smtp)
    try:
        fetch_core_hub(_SMTP_CONFIG_PATH, method="PUT", token=token, body=payload)
        return {"status": "restored"}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to restore SMTP settings: %s", exc)
        return {"status": "failed", "error": str(exc)}


def import_global_notifications(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    notification_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore global notification settings (SMTP) from a backup.

    Accepts either a dict already shaped like the SMTP config, or one wrapped
    under a ``globalNotifications`` key (as written to global-config.yaml).
    A masked password ("*******" or "<hidden>") is dropped so the existing
    CoreHub password is preserved instead of being overwritten with a sentinel.
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    if not isinstance(notification_cfg, dict):
        return {"status": "skipped", "reason": "invalid notification config"}

    payload = notification_cfg.get("globalNotifications", notification_cfg)
    if not isinstance(payload, dict) or not payload:
        return {"status": "skipped", "reason": "no globalNotifications in backup"}

    payload = dict(payload)
    pwd = payload.get("password")
    if pwd in (_MASKED_SECRET, "<hidden>", None, ""):
        payload.pop("password", None)

    try:
        fetch_core_hub(_SMTP_CONFIG_PATH, method="PUT", token=token, body=payload)
        return {"status": "restored"}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to restore global notification settings: %s", exc)
        return {"status": "failed", "error": str(exc)}


def import_webhooks_config(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    webhooks_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore webhooks configuration from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "webhooks": "/global-config/webhooks",
        "retention": "/global-config/webhooks/retention",
        "event_types": "/global-config/webhooks/event-types",
        "delivery_logs": "/global-config/webhooks/delivery-logs",
        "dead_letters": "/global-config/webhooks/dead-letters",
    }

    results: Dict[str, Any] = {}
    for key, path in endpoints.items():
        data = webhooks_data.get(key)
        if data is None:
            results[key] = {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
            continue
        if isinstance(data, dict) and "error" in data:
            results[key] = {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}
            continue

        payload = _unwrap_value_if_needed(data)
        try:
            fetch_core_hub(path, method="PUT", token=token, body=payload)
            results[key] = {"status": "restored"}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore webhooks config %s: %s", key, exc)
            results[key] = {"status": "failed", "error": str(exc)}

    return results


def import_thresholds(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    thresholds_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore thresholds configuration from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "thresholds": "/global-config/thresholds",
        "pattern_status": "/global-config/thresholds/pattern-status",
        "simulate": "/global-config/thresholds/simulate",
    }

    results: Dict[str, Any] = {}
    for key, path in endpoints.items():
        data = thresholds_data.get(key)
        if data is None:
            results[key] = {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
            continue
        if isinstance(data, dict) and "error" in data:
            results[key] = {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}
            continue

        payload = _unwrap_value_if_needed(data)
        try:
            fetch_core_hub(path, method="PUT", token=token, body=payload)
            results[key] = {"status": "restored"}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore thresholds config %s: %s", key, exc)
            results[key] = {"status": "failed", "error": str(exc)}

    return results


def _restore_scheduler_settings(settings: Any, token: str) -> Dict[str, Any]:
    """PUT the backed-up Chronos settings, unless they are missing or were exported with an error."""
    if settings is None or (isinstance(settings, dict) and "error" in settings):
        return {"status": "skipped"}
    payload = _unwrap_value_if_needed(settings)
    try:
        fetch_core_hub("/chronos/api/settings", method="PUT", token=token, body=payload)
        return {"status": "restored"}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to restore scheduler settings: %s", exc)
        return {"status": "failed", "error": str(exc)}


def _restore_scheduler_jobs(jobs: List[Any], token: str) -> Dict[str, Any]:
    """POST every backed-up Chronos job and count how many were restored or failed."""
    restored = 0
    failed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        try:
            fetch_core_hub("/chronos/api/jobs", method="POST", token=token, body=job)
            restored += 1
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore scheduler job %s: %s", job.get("id", "?"), exc)
            failed += 1
    return {"status": "restored", "restored": restored, "failed": failed}


def import_schedules(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    schedules_data: Dict[str, Any],
    chronos_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore Chronos scheduler jobs and settings from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    results: Dict[str, Any] = {}

    # Restore settings first
    results["settings"] = _restore_scheduler_settings(schedules_data.get("settings"), chronos_token or token)

    # Restore jobs
    jobs = schedules_data.get("jobs")
    if isinstance(jobs, list):
        results["jobs"] = _restore_scheduler_jobs(jobs, chronos_token or token)
    else:
        results["jobs"] = {"status": "skipped", "reason": "no jobs in backup"}

    return results


def _collect_importable_users(raw_users: List[Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split the backed-up users into every non-OIDC user and those that carry a password hash.

    Returns ``(all_users, bulk_users)``: the normalized entries of every importable user, and
    the payload entries of ``POST /users/import`` for the users with a ``passwordHash``.
    """
    all_users: List[Dict[str, Any]] = []
    bulk_users: List[Dict[str, Any]] = []
    for user in raw_users:
        if not isinstance(user, dict):
            continue
        if user.get("isOidcUser"):
            continue  # OIDC users are managed by external identity provider
        entry = {
            "id": user.get("id", ""),
            "username": user.get("username", ""),
            "role": user.get("role", "VIEWER"),
            "changeRequired": user.get("changeRequired", False),
            "isOidcUser": False,
            "name": user.get("name") or "",
            "surname": user.get("surname") or "",
            "email": user.get("email") or "",
            "passwordHash": user.get("passwordHash"),
        }
        all_users.append(entry)
        if entry["passwordHash"]:
            bulk_users.append({
                "id": entry["id"],
                "username": entry["username"],
                "passwordHash": str(entry["passwordHash"]),
                "role": entry["role"],
                "changeRequired": entry["changeRequired"],
                "isOidcUser": False,
                "name": entry["name"],
                "surname": entry["surname"],
                "email": entry["email"],
            })
    return all_users, bulk_users


def _bulk_import_users(token: str, bulk_users: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Restore users with their password hashes through ``POST /users/import`` (CoreHub 1.4+).

    Returns the import result, or None when the endpoint is unavailable or did not
    answer with the list of imported users, so the caller falls back to per-user restore.
    """
    try:
        response = fetch_core_hub(
            "/users/import",
            method="POST",
            token=token,
            body={"users": bulk_users},
            expected_error_statuses={404},
        )
        if isinstance(response, list):
            return {
                "status": "restored",
                "count": len(response),
                "note": "passwords restored from backup hashes",
            }
    except Exception as exc:  # pylint: disable=broad-except
        err_str = str(exc)
        if "404" in err_str or "Not Found" in err_str:
            logger.info("POST /users/import not available (CoreHub < 1.4); falling back to per-user restore")
        else:
            logger.warning("POST /users/import failed: %s; falling back", exc)
    return None


def _fetch_existing_users(token: str) -> List[Any]:
    """The users currently defined on CoreHub, or an empty list when they cannot be fetched."""
    try:
        existing_resp = fetch_core_hub(_USERS_PATH, token=token)
        if isinstance(existing_resp, list):
            return existing_resp
        if isinstance(existing_resp, dict):
            return existing_resp.get("users") or []
        return []
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch existing users during import: %s", exc)
        return []


def _existing_users_by_username(token: str) -> Dict[str, dict]:
    """The users currently defined on CoreHub, keyed by username."""
    return {
        str(u.get("username")): u
        for u in _fetch_existing_users(token)
        if isinstance(u, dict) and u.get("username")
    }


def _restore_users_one_by_one(
    token: str,
    all_users: List[Dict[str, Any]],
    existing_by_username: Dict[str, dict],
) -> Tuple[int, int, List[str]]:
    """Update the users that already exist and create the others, one request per user.

    Users without a username are ignored. Returns ``(created, updated, errors)``.
    """
    counts = {"created": 0, "updated": 0}
    errors: List[str] = []
    for user in all_users:
        if not user["username"]:
            continue
        outcome, error = _restore_one_user(token, user, existing_by_username)
        if error is None:
            counts[outcome] += 1
        else:
            errors.append(error)
    return counts["created"], counts["updated"], errors


def _restore_one_user(
    token: str,
    user: Dict[str, Any],
    existing_by_username: Dict[str, dict],
) -> Tuple[str, Optional[str]]:
    """Update ``user`` when it already exists on CoreHub, create it otherwise.

    Returns ``("updated" | "created", error)``, where error is None on success.
    """
    username = user["username"]
    if username in existing_by_username:
        existing_id = existing_by_username[username].get("id")
        return "updated", _update_existing_user(token, existing_id, user)
    return "created", _create_user_with_temp_password(token, user)


def _update_existing_user(token: str, existing_id: Any, user: Dict[str, Any]) -> Optional[str]:
    """Update the profile of an existing CoreHub user; returns an error description on failure."""
    username = user["username"]
    try:
        fetch_core_hub(
            f"{_USERS_PATH}/{existing_id}",
            method="PUT",
            token=token,
            body={
                "role": user["role"],
                "name": user["name"],
                "surname": user["surname"],
                "email": user["email"],
            },
        )
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to update user %s: %s", username, exc)
        return f"update {username}: {exc}"


def _create_user_with_temp_password(token: str, user: Dict[str, Any]) -> Optional[str]:
    """Create a user with a random temporary password; returns an error description on failure."""
    import secrets
    import string
    username = user["username"]
    temp_password = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
    )
    try:
        fetch_core_hub(
            _USERS_PATH,
            method="POST",
            token=token,
            body={
                "username": username,
                "password": temp_password,
                "role": user["role"],
                "name": user["name"],
                "surname": user["surname"],
                "email": user["email"],
            },
        )
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to create user %s: %s", username, exc)
        return f"create {username}: {exc}"


def import_users(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    users_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore user management configuration from a previously exported dict.

    Uses the dedicated POST /users/import endpoint (available in CoreHub 1.4+)
    which accepts password hashes directly, restoring original passwords.
    Falls back to per-user PUT/POST when the endpoint is unavailable.
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    users = users_data.get("users")
    if users is None:
        return {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
    if isinstance(users, dict) and "error" in users:
        return {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}

    raw = _unwrap_value_if_needed(users)
    if not isinstance(raw, list):
        return {"status": "skipped", "reason": "invalid users format"}

    # Collect all non-OIDC users from the backup.
    all_users, bulk_users = _collect_importable_users(raw)

    if not all_users:
        return {"status": "skipped", "reason": "no importable users"}

    # Try the new bulk import endpoint first (only for users with passwordHash).
    if bulk_users:
        bulk_result = _bulk_import_users(token, bulk_users)
        if bulk_result is not None:
            return bulk_result

    # Fallback: per-user PUT/POST when the import endpoint is unavailable
    # or when some users lack a passwordHash.
    created, updated, errors = _restore_users_one_by_one(token, all_users, _existing_users_by_username(token))

    result: Dict[str, Any] = {
        "status": "restored",
        "created": created,
        "updated": updated,
        "skipped": 0,
    }
    if errors:
        result["errors"] = errors
    result["note"] = "passwords NOT restored; users must reset passwords via CoreHub UI"
    return result


def import_oidc_config(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    oidc_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore OIDC configuration from a previously exported dict."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    endpoints = {
        "oidc_configuration": "/oidc/configuration",
        "oidc_auth_url": "/oidc/auth-url",
    }

    results: Dict[str, Any] = {}
    for key, path in endpoints.items():
        data = oidc_data.get(key)
        if data is None:
            results[key] = {"status": "skipped", "reason": _REASON_NOT_PRESENT_IN_BACKUP}
            continue
        if isinstance(data, dict) and "error" in data:
            results[key] = {"status": "skipped", "reason": _REASON_EXPORTED_WITH_ERROR}
            continue

        payload = _unwrap_value_if_needed(data)
        try:
            fetch_core_hub(path, method="PUT", token=token, body=payload)
            results[key] = {"status": "restored"}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore OIDC config %s: %s", key, exc)
            results[key] = {"status": "failed", "error": str(exc)}

    return results


def _read_backup_yaml(extract_dir: Path, filename: str) -> Any:
    """Load ``filename`` from an extracted backup, or None when the backup has no such file."""
    path = extract_dir / filename
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _pipelines_zip_status(extract_dir: Path) -> Dict[str, Any]:
    """Report the pipelines.zip of an extracted backup; app.py imports it via the import-all flow."""
    pipelines_zip_path = extract_dir / "pipelines.zip"
    if not pipelines_zip_path.exists():
        return {"status": "skipped", "reason": "pipelines.zip not found"}
    with open(pipelines_zip_path, "rb") as f:
        pipelines_zip = f.read()
    # The caller (app.py) handles pipeline import via the existing import_all flow
    return {"status": "pending", "zip_size": len(pipelines_zip)}


# ---------------------------------------------------------------------------
# User groups and Query Studio data-access policies (Core Hub 2.3.0.1+ for groups)
# ---------------------------------------------------------------------------

USER_GROUPS_FILENAME = "user-groups.yaml"
DATA_ACCESS_POLICIES_FILENAME = "data-access-policies.yaml"
DATA_ACCESS_BASE_PATH = "/query-studio/data-access"
USER_GROUPS_PATH = "/user-groups"
_CONNECTIONS_PATH = "/connections"
_UNSUPPORTED = "unsupported"
_POLICY_FIELDS = ("mode", "rules", "piiUnmaskGrants")


def _is_not_found(exc: Exception) -> bool:
    """True when Core Hub answered 404, which for these endpoints means a Core Hub without the feature."""
    cause = exc.__cause__
    response = getattr(cause, "response", None) or getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 404:
        return True
    return "404" in str(exc) or "Not Found" in str(exc)


def _as_list(response: Any, key: str = "items") -> list:
    """Core Hub answers either a bare list or an object holding it under ``key``."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        items = response.get(key)
        if isinstance(items, list):
            return items
    return []


def _usernames_by_id(token: str) -> Dict[str, str]:
    users = _as_list(fetch_core_hub(_USERS_PATH, token=token), key="users")
    return {
        str(user["id"]): str(user["username"])
        for user in users
        if isinstance(user, dict) and user.get("id") and user.get("username")
    }


def _connection_names(token: str) -> Dict[str, str]:
    """Connection name by connection id on this Core Hub; empty when the connections cannot be listed."""
    try:
        connections = _as_list(fetch_core_hub(_CONNECTIONS_PATH, token=token))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to list the connections of the data-access policies: %s", exc)
        return {}
    return {
        str(connection["connectionId"]): str(connection["name"])
        for connection in connections
        if isinstance(connection, dict) and connection.get("connectionId") and connection.get("name")
    }


def _policy_entry(policy: Dict[str, Any], connection_names: Dict[str, str]) -> Dict[str, Any]:
    """The part of a policy a restore sends back: the connection (id and name) and the matrix, no principal or audit fields."""
    connection_id = str(policy.get("connectionId"))
    entry = {"connectionId": connection_id, "connectionName": connection_names.get(connection_id)}
    entry.update({field: policy[field] for field in _POLICY_FIELDS if field in policy})
    return entry


def _policies_of(token: str, principal_path: str, connection_names: Dict[str, str]) -> list:
    response = fetch_core_hub(f"{DATA_ACCESS_BASE_PATH}/{principal_path}", token=token)
    return [
        _policy_entry(policy, connection_names)
        for policy in _as_list(response)
        if isinstance(policy, dict) and policy.get("connectionId")
    ]


def export_user_groups(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export the user groups with their members and their Query Studio data-access policies.

    Members are kept with their username next to their id, so a restore can find them again on a
    Core Hub where the same user got another id. A Core Hub without user groups answers 404 and the
    export says so instead of failing.
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    try:
        groups = _as_list(fetch_core_hub(USER_GROUPS_PATH, token=token, expected_error_statuses={404}))
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return {"user_groups": {_UNSUPPORTED: "this Core Hub has no user groups"}}
        logger.warning("Failed to fetch user groups: %s", exc)
        return {"user_groups": {"error": str(exc)}}

    try:
        usernames = _usernames_by_id(token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch the users of the user groups: %s", exc)
        usernames = {}
    connection_names = _connection_names(token)

    exported: list[dict] = []
    for group in groups:
        if not isinstance(group, dict) or not group.get("id"):
            continue
        group_id = str(group["id"])
        entry: Dict[str, Any] = {
            "id": group_id,
            "name": group.get("name"),
            "description": group.get("description"),
            "members": [
                {"id": str(member_id), "username": usernames.get(str(member_id))}
                for member_id in group.get("memberIds") or []
            ],
        }
        try:
            entry["policies"] = _policies_of(token, f"groups/{group_id}", connection_names)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to fetch the data-access policies of user group %s: %s", group_id, exc)
            entry["policies"] = {"error": str(exc)}
        exported.append(entry)

    return {"user_groups": exported}


def export_data_access_policies(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
) -> Dict[str, Any]:
    """Export the Query Studio data-access policies of every user, by user id and username."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    try:
        usernames = _usernames_by_id(token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch users for the data-access policies: %s", exc)
        return {"user_policies": {"error": str(exc)}}
    connection_names = _connection_names(token)

    exported: list[dict] = []
    errors: list[str] = []
    for user_id, username in usernames.items():
        try:
            policies = _policies_of(token, f"users/{user_id}", connection_names)
        except Exception as exc:  # pylint: disable=broad-except
            if _is_not_found(exc):
                return {"user_policies": {_UNSUPPORTED: "this Core Hub has no data-access policies"}}
            logger.warning("Failed to fetch the data-access policies of user %s: %s", username, exc)
            errors.append(f"{username}: {exc}")
            continue
        if policies:
            exported.append({"userId": user_id, "username": username, "policies": policies})

    result: Dict[str, Any] = {"user_policies": exported}
    if errors:
        result["errors"] = errors
    return result


def _skip_reason(section: Any) -> Optional[str]:
    if section is None:
        return _REASON_NOT_PRESENT_IN_BACKUP
    if isinstance(section, dict) and _UNSUPPORTED in section:
        return f"not exported: {section[_UNSUPPORTED]}"
    if isinstance(section, dict) and "error" in section:
        return _REASON_EXPORTED_WITH_ERROR
    if not isinstance(section, list):
        return "invalid format"
    return None


def _target_user_ids(token: str) -> Tuple[Dict[str, str], set]:
    """The users of the Core Hub being restored: id by username, and every id."""
    by_id = _usernames_by_id(token)
    return {username: user_id for user_id, username in by_id.items()}, set(by_id)


def _resolve_user(backup_id: Optional[str], username: Optional[str], ids_by_username: Dict[str, str], known_ids: set) -> Optional[str]:
    """The id a backed-up user has on this Core Hub: found by username first, by the same id otherwise."""
    if username and username in ids_by_username:
        return ids_by_username[username]
    if backup_id and backup_id in known_ids:
        return backup_id
    return None


class _TargetConnections:
    """The connections of the Core Hub being restored, to point each backed-up policy at one of them.

    A policy keeps its connection when this Core Hub has the same connection id; otherwise it moves to the
    only connection with the same name, which is what an import of the pipelines recreates. A policy whose
    connection is not found is not restored: it would protect nothing and still switch data-access
    enforcement on for the whole installation.
    """

    def __init__(self, token: str):
        try:
            connections = _as_list(fetch_core_hub(_CONNECTIONS_PATH, token=token))
            self.error: Optional[str] = None
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to list the connections to restore the data-access policies on: %s", exc)
            connections = []
            self.error = str(exc)
        connections = [c for c in connections if isinstance(c, dict) and c.get("connectionId")]
        self.ids = {str(c["connectionId"]) for c in connections}
        ids_by_name: Dict[str, list] = {}
        for connection in connections:
            ids_by_name.setdefault(str(connection.get("name")), []).append(str(connection["connectionId"]))
        self.id_by_name = {name: ids[0] for name, ids in ids_by_name.items() if len(ids) == 1}

    def resolve(self, policy: Dict[str, Any]) -> Optional[str]:
        connection_id = str(policy.get("connectionId"))
        if connection_id in self.ids:
            return connection_id
        return self.id_by_name.get(str(policy.get("connectionName")))


def _restore_policies(
    token: str,
    principal_path: str,
    policies: Any,
    label: str,
    connections: _TargetConnections,
    report: Dict[str, list],
) -> int:
    errors = report["errors"]
    if not isinstance(policies, list):
        if policies is not None:
            errors.append(f"{label}: policies exported with error")
        return 0
    restored = 0
    for policy in policies:
        if not isinstance(policy, dict) or not policy.get("connectionId"):
            continue
        connection_id = connections.resolve(policy)
        if not connection_id:
            report["skipped_policies"].append(
                f"{label} on {policy.get('connectionName') or policy['connectionId']}: connection not found on this Core Hub"
            )
            continue
        body = {field: policy[field] for field in _POLICY_FIELDS if field in policy}
        try:
            fetch_core_hub(
                f"{DATA_ACCESS_BASE_PATH}/{principal_path}/connections/{connection_id}",
                method="PUT",
                token=token,
                body=body,
            )
            restored += 1
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore the data-access policy of %s on %s: %s", label, connection_id, exc)
            errors.append(f"{label} on {connection_id}: {exc}")
    return restored


def _new_report() -> Dict[str, list]:
    return {"errors": [], "warnings": [], "skipped_policies": []}


def _restore_result(counts: Dict[str, int], report: Dict[str, list], connections: _TargetConnections) -> Dict[str, Any]:
    skipped = report["skipped_policies"]
    summary = ", ".join(f"{count} {name}" for name, count in counts.items())
    if skipped:
        summary += f", {len(skipped)} policies skipped (connection not found; import the pipelines, then restore again)"
    result: Dict[str, Any] = {"status": "restored", "summary": summary, **counts}
    if skipped and connections.error:
        report["warnings"].append(f"the connections of this Core Hub could not be listed: {connections.error}")
    result.update({key: values for key, values in report.items() if values})
    return result


def import_user_groups(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    groups_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore the user groups, their members and their data-access policies.

    Runs after the users: a member is found by username first, then by id, and a member this Core Hub
    does not have is left out with a warning. A group is matched by name, ignoring case: an existing one
    gets the description and the members of the backup, a missing one is created. The policies of the
    backup are then saved on the group; policies the group has and the backup does not are left alone.
    """
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    groups = (groups_data or {}).get("user_groups")
    reason = _skip_reason(groups)
    if reason:
        return {"status": "skipped", "reason": reason}

    try:
        existing = _as_list(fetch_core_hub(USER_GROUPS_PATH, token=token, expected_error_statuses={404}))
    except Exception as exc:  # pylint: disable=broad-except
        if _is_not_found(exc):
            return {"status": "skipped", "reason": "this Core Hub has no user groups"}
        raise
    existing_by_name = {
        str(group.get("name", "")).casefold(): group for group in existing if isinstance(group, dict) and group.get("id")
    }
    ids_by_username, known_ids = _target_user_ids(token)
    connections = _TargetConnections(token)

    counts = {"created": 0, "updated": 0, "policies": 0}
    report = _new_report()
    for group in groups:
        name = group.get("name") if isinstance(group, dict) else None
        if not name:
            continue
        member_ids = _resolve_members(group, ids_by_username, known_ids, report["warnings"])
        current = existing_by_name.get(str(name).casefold())
        try:
            group_id = _upsert_group(token, group, current, member_ids)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore user group %s: %s", name, exc)
            report["errors"].append(f"{name}: {exc}")
            continue
        counts["updated" if current else "created"] += 1
        counts["policies"] += _restore_policies(
            token, f"groups/{group_id}", group.get("policies"), f"group {name}", connections, report,
        )

    return _restore_result(counts, report, connections)


def _resolve_members(group: Dict[str, Any], ids_by_username: Dict[str, str], known_ids: set, warnings: list) -> list:
    member_ids: list[str] = []
    for member in group.get("members") or []:
        resolved = _resolve_user(member.get("id"), member.get("username"), ids_by_username, known_ids)
        if resolved:
            member_ids.append(resolved)
        else:
            warnings.append(f"{group['name']}: member {member.get('username') or member.get('id')} not found, left out")
    return member_ids


def _upsert_group(token: str, group: Dict[str, Any], current: Optional[Dict[str, Any]], member_ids: list) -> str:
    """Updates the group this Core Hub already has under that name, or creates it; returns its id here."""
    if current:
        group_id = str(current["id"])
        fetch_core_hub(
            f"{USER_GROUPS_PATH}/{group_id}",
            method="PUT",
            token=token,
            body={"description": group.get("description") or ""},
        )
        fetch_core_hub(f"{USER_GROUPS_PATH}/{group_id}/members", method="PUT", token=token, body={"memberIds": member_ids})
        return group_id
    created = fetch_core_hub(
        USER_GROUPS_PATH,
        method="POST",
        token=token,
        body={"name": group["name"], "description": group.get("description"), "memberIds": member_ids},
    )
    if not isinstance(created, dict) or not created.get("id"):
        raise RuntimeError("Core Hub did not answer the id of the created group")
    return str(created["id"])


def import_data_access_policies(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    policies_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Restore the data-access policies of each user, found by username first and by id otherwise."""
    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    entries = (policies_data or {}).get("user_policies")
    reason = _skip_reason(entries)
    if reason:
        return {"status": "skipped", "reason": reason}

    ids_by_username, known_ids = _target_user_ids(token)
    connections = _TargetConnections(token)
    counts = {"users": 0, "policies": 0}
    report = _new_report()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = entry.get("username") or entry.get("userId")
        user_id = _resolve_user(entry.get("userId"), entry.get("username"), ids_by_username, known_ids)
        if not user_id:
            report["warnings"].append(f"user {label} not found, policies left out")
            continue
        restored = _restore_policies(token, f"users/{user_id}", entry.get("policies"), f"user {label}", connections, report)
        if restored:
            counts["users"] += 1
            counts["policies"] += restored

    return _restore_result(counts, report, connections)


def import_full_corehub_backup(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    zip_bytes: bytes,
    chronos_token: Optional[str] = None,
    ai_settings_passphrase: Optional[str] = None,
) -> Dict[str, Any]:
    """Restore a complete CoreHub backup from a ZIP archive.

    Each domain is restored independently. Pipelines are restored via the
    existing import-all logic (agents-config + per-pipeline YAMLs + UDFs).
    AI settings come last, once the users they belong to exist.
    """
    import tempfile
    import shutil

    configure_core_hub(base_url, use_ssl=use_ssl, skip_verify=skip_verify)

    results: Dict[str, Any] = {}

    # Extract ZIP contents to a temporary directory
    extract_dir = Path(tempfile.gettempdir()) / f"corehub_restore_{int(time.time())}"
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            zf.extractall(str(extract_dir))

        connection = {
            "token": token,
            "base_url": base_url,
            "use_ssl": use_ssl,
            "skip_verify": skip_verify,
        }
        # (result key, backup file, restore function), restored in this order.
        sections = (
            ("global_configs", _GLOBAL_CONFIGS_BACKUP_FILE,
             lambda data: import_global_configs(**connection, configs=data)),
            ("smtp", _SMTP_BACKUP_FILE,
             lambda data: import_smtp_settings(**connection, smtp_data=data)),
            ("webhooks", _WEBHOOKS_BACKUP_FILE,
             lambda data: import_webhooks_config(**connection, webhooks_data=data)),
            ("thresholds", _THRESHOLDS_BACKUP_FILE,
             lambda data: import_thresholds(**connection, thresholds_data=data)),
            ("schedules", _SCHEDULES_BACKUP_FILE,
             lambda data: import_schedules(**connection, schedules_data=data, chronos_token=chronos_token)),
            ("users", _USERS_BACKUP_FILE,
             lambda data: import_users(**connection, users_data=data)),
            ("oidc", _OIDC_BACKUP_FILE,
             lambda data: import_oidc_config(**connection, oidc_data=data)),
            # User groups, then the policies of each user: both need the users restored above
            ("user_groups", USER_GROUPS_FILENAME,
             lambda data: restore_safely(USER_GROUPS_FILENAME, lambda: import_user_groups(**connection, groups_data=data))),
            ("data_access_policies", DATA_ACCESS_POLICIES_FILENAME,
             lambda data: restore_safely(
                 DATA_ACCESS_POLICIES_FILENAME, lambda: import_data_access_policies(**connection, policies_data=data))),
        )
        for key, filename, restore in sections:
            data = _read_backup_yaml(extract_dir, filename)
            if data is not None:
                results[key] = restore(data)
            else:
                results[key] = {"status": "skipped", "reason": f"{filename} not found"}

        # Restore pipelines (reuse existing import-all logic)
        results["pipelines"] = _pipelines_zip_status(extract_dir)

        results["ai_settings"] = _restore_ai_settings_file(extract_dir, token, ai_settings_passphrase)

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)

    return results


def restore_safely(filename: str, restore: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Runs one restore; a failure becomes a failed result instead of stopping the other sections."""
    try:
        return restore()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to restore %s: %s", filename, exc)
        return {"status": "failed", "error": str(exc)}


def _restore_ai_settings_file(extract_dir: Path, token: str, passphrase: Optional[str]) -> Dict[str, Any]:
    bundle_path = extract_dir / ai_settings.BACKUP_FILENAME
    error_path = extract_dir / AI_SETTINGS_ERROR_FILENAME
    if bundle_path.exists():
        try:
            with open(bundle_path, "r", encoding="utf-8") as f:
                bundle = json.load(f)
            return restore_ai_settings(token=token, bundle=bundle, passphrase=passphrase)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to restore AI settings: %s", exc)
            return {"status": "failed", "reason": str(exc)}
    if error_path.exists():
        return {"status": "skipped", "reason": "the backup could not export them, see ai-settings.error.yaml"}
    return {"status": "skipped", "reason": f"{ai_settings.BACKUP_FILENAME} not found"}


def _normalize_agent_type_for_add(agent_type: str) -> str:
    normalized = str(agent_type or "").strip().lower()
    if normalized in {"source", "target"}:
        return normalized
    raise ValueError(f"Unsupported agentType {agent_type!r}. Expected SOURCE or TARGET")


def _add_agent_to_pipeline_via_corehub(
    *,
    token: str,
    pipeline_id: str,
    agent_type: str,
    agent_user_tag: str,
    agent_internal_name: str,
) -> str:
    """Create/attach an agent using CoreHub's /agents/add endpoint."""

    normalized_type = _normalize_agent_type_for_add(agent_type)
    response = fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/add",
        method="GET",
        token=token,
        params={
            "agentType": normalized_type,
            "agentUserTag": agent_user_tag,
            "agentInternalName": agent_internal_name,
        },
    )

    if not isinstance(response, dict):
        raise RuntimeError(
            f"Unexpected response while adding {normalized_type} agent "
            f"{agent_user_tag!r}/{agent_internal_name!r}: {response}"
        )

    raw_agent_id = response.get("agentId") or response.get("id")
    if not raw_agent_id:
        raise RuntimeError(
            f"CoreHub did not return an agentId for {normalized_type} agent "
            f"{agent_user_tag!r}/{agent_internal_name!r}: {response}"
        )

    agent_id = str(raw_agent_id)
    logger.info(
        "Agent created via /agents/add: %s agent %s (id=%s)",
        normalized_type,
        agent_user_tag,
        agent_id,
    )

    fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{agent_id}",
        method="PUT",
        token=token,
        params={"agentType": normalized_type.upper()},
    )
    logger.info(
        "Successfully attached %s agent %s (id=%s) to pipeline %s",
        normalized_type,
        agent_user_tag,
        agent_id,
        pipeline_id,
    )
    return agent_id


def _fetch_agent_credentials(
    *,
    token: str,
    pipeline_id: str,
    agent: Dict[str, Any],
) -> tuple[dict, dict]:
    """Retrieve host/custom credentials for a specific agent, falling back to config."""

    fallback_host = (agent.get("hostCredentials") or {}).copy()
    fallback_custom = (
        agent.get("customHostCredentials")
        or agent.get("hostCredentialsCustomProperties")
        or {}
    ).copy()

    agent_id = agent.get("agentId") or agent.get("id")
    if not agent_id:
        return fallback_host, fallback_custom

    try:
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent_id}/config/credentials",
            token=token,
        )
        if isinstance(response, dict):
            host_creds = response.get("hostCredentials") or fallback_host
            custom_creds = response.get("customHostCredentials") or fallback_custom
            return host_creds, custom_creds
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "Failed to fetch credentials for agent %s: %s",
            agent_id,
            exc,
        )
    return fallback_host, fallback_custom


class PartialPipelineImportError(RuntimeError):
    """An import that failed after its pipeline was created: ``pipeline_id`` / ``agents`` say what is left."""

    def __init__(self, message, pipeline_id, pipeline_name, agents, cleaned_up, detail=None):
        super().__init__(message)
        self.detail = detail
        self.pipeline_id = pipeline_id
        self.pipeline_name = pipeline_name
        self.agents = agents
        self.cleaned_up = cleaned_up


def _short_import_failure_reason(error: BaseException) -> str:
    """One short line for an import failure: the connection reason when CoreHub could not reach the agent's
    database, else the ``exceptionMessage`` of the CoreHub answer (the full text goes to the debug log)."""
    text = str(error)
    if credentials_failure_is_recoverable(error):
        return short_connection_reason(text)
    reason = " ".join(_exception_message(text).split())
    return reason if len(reason) <= 200 else reason[:197].rstrip() + "..."


def _partial_import_error(token, pipeline_id, pipeline_name, agents, error, cleanup):
    cleaned_up = False
    if cleanup:
        try:
            fetch_core_hub(f"/pipelines/{pipeline_id}", method="DELETE", token=token)
            cleaned_up = True
        except Exception as cleanup_error:  # pylint: disable=broad-except
            logger.exception("Cleanup of half-created pipeline %s failed: %s", pipeline_id, cleanup_error)
    left = "deleted (cleanup on failure)" if cleaned_up else "LEFT IN PLACE: delete it or reuse it"
    reason = _short_import_failure_reason(error)
    message = (f"Import of pipeline {pipeline_name!r} failed after it was created: {reason}. Half-created "
               f"pipeline id {pipeline_id} ({left}); agents bound: {', '.join(agents) or 'none'}")
    if not cleaned_up:
        message += (f". Next: fix the cause, then resume with GS_IMPORT_RESUME_PIPELINE_ID={pipeline_id} "
                    "- or delete the pipeline")
    logger.error(message)
    logger.debug("Full CoreHub error for pipeline %s: %s", pipeline_id, error)
    return PartialPipelineImportError(message, pipeline_id, pipeline_name, list(agents), cleaned_up, str(error))


def continue_on_credentials_failure_requested() -> bool:
    """``GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE``: go on with an import when CoreHub refuses an agent's credentials."""
    return os.getenv("GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE", "").strip().lower() in ("1", "true", "yes", "y", "t")


def credentials_failure_is_recoverable(error: BaseException) -> bool:
    """True for a credentials PUT that failed on a connection problem or a 5xx (CoreHub could not reach the
    agent's database: VERIFIED on Core Hub 2.3.1, 500 "Failed to initialize pool ... Connection refused");
    False for a 4xx (wrong payload, auth), which still stops the import."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
        if isinstance(error, requests.exceptions.HTTPError):
            status = getattr(getattr(error, "response", None), "status_code", None)
            return status is not None and status >= 500
        error = error.__cause__ or error.__context__
    return False


def record_credentials_pending(pending: list, pipeline_id: Any, agent_id: Any, agent_type: Any, agent_tag: Any,
                               error: BaseException) -> None:
    """Log and record an agent whose credentials CoreHub refused: they must be entered again in Core Hub."""
    entry = {"pipelineId": str(pipeline_id), "agentId": str(agent_id), "agentType": str(agent_type or "").upper(),
             "agentTag": agent_tag, "error": str(error), "reason": short_connection_reason(str(error))}
    pending.append(entry)
    logger.warning("Credentials of %s agent %s (id %s) on pipeline %s NOT saved: %s. Continuing "
                   "(GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE); re-enter them in Core Hub.",
                   entry["agentType"], agent_tag, agent_id, pipeline_id, entry["reason"])
    logger.debug("Credentials of agent %s refused, full Core Hub error: %s", agent_id, error)


_HOST_PORT = (re.compile(r"host\s+([\w.\-]+),?\s+port\s+(\d+)", re.I),
              re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}|[a-z][\w\-]*(?:\.[\w\-]+)+):(\d{2,5})\b", re.I))
_UNKNOWN_HOST = "unknown host"
_CAUSES = (("timed out", "timed out"), ("connection refused", "Connection refused"),
           ("unknownhost", _UNKNOWN_HOST), (_UNKNOWN_HOST, _UNKNOWN_HOST), ("no route to host", "no route to host"),
           ("login failed", "login failed"), ("password", "wrong user or password"),
           ("authentication", "authentication failed"), ("does not exist", "database does not exist"))
_BOILERPLATE = re.compile(r"^(failed to initialize pool:\s*|.*?exception:\s*)", re.I)


def _exception_message(error_text: str) -> str:
    """The ``exceptionMessage`` of the Core Hub JSON response inside an error text, else the text itself."""
    start = error_text.find("{")
    while start != -1:
        try:
            body, _ = json.JSONDecoder().raw_decode(error_text[start:])
            if isinstance(body, dict) and (body.get("exceptionMessage") or body.get("message")):
                return str(body.get("exceptionMessage") or body.get("message"))
        except ValueError:
            pass
        start = error_text.find("{", start + 1)
    return error_text


def short_connection_reason(error_text: str) -> str:
    """A short human reason for a refused agent connection: "can't connect to HOST:PORT (timed out)".

    Reads ``exceptionMessage`` of the Core Hub response, drops the pool boilerplate, keeps host:port when the
    message names it; the full text stays in the debug log."""
    message = _BOILERPLATE.sub("", _exception_message(error_text or "").strip(), count=1).strip()
    lowered = message.lower()
    where = "database"
    for pattern in _HOST_PORT:
        match = pattern.search(message)
        if match:
            where = f"{match.group(1)}:{match.group(2)}"
            break
    for needle, cause in _CAUSES:
        if needle in lowered:
            return f"can't connect to {where} ({cause})"
    first = re.split(r"(?<=[.!?])\s", message, maxsplit=1)[0].strip() or "unknown error"
    return f"can't connect to {where} ({first[:120].rstrip('.')})"


def _put_agent_credentials_or_record(
    token, pipeline_id, agent_id, agent_type, agent_tag, host_credentials, custom_host_credentials,
    credentials_pending, continue_on_credentials_failure,
):
    """PUT the agent's credentials; a recoverable failure is recorded in ``credentials_pending`` when asked to."""
    try:
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent_id}/config/credentials",
            method="PUT",
            token=token,
            body={
                "hostCredentials": host_credentials,
                "customHostCredentials": custom_host_credentials,
            },
        )
    except Exception as error:  # pylint: disable=broad-except
        if not (continue_on_credentials_failure and credentials_failure_is_recoverable(error)):
            raise
        record_credentials_pending(credentials_pending, pipeline_id, agent_id, agent_type, agent_tag,
                                   error)


def _put_agent_specific_config(token, pipeline_id, agent_id, specific_conf, credentials_pending):
    """PUT the agent's specific configuration. Sent even when the credentials are pending: whether
    CoreHub takes it without working credentials is not known, so a refusal is recorded, not fatal."""
    try:
        fetch_core_hub(
            f"/pipelines/{pipeline_id}/agents/{agent_id}/config/specific",
            method="PUT",
            token=token,
            body={"configuration": specific_conf},
        )
    except Exception as error:  # pylint: disable=broad-except
        pending = [p for p in credentials_pending if p["agentId"] == str(agent_id)]
        if not pending:
            raise
        pending[0]["specificConfigurationError"] = str(error)
        logger.warning("Specific configuration of agent %s not saved (credentials pending)", agent_id)
        logger.debug("Specific configuration of agent %s refused, full Core Hub error: %s",
                     agent_id, error)


def _bind_config_agent(
    token, pipeline_id, conf_agent, bound_agents, credentials_pending, continue_on_credentials_failure,
):
    """Bind one configured agent to the pipeline and apply its settings (body of import_pipeline_config_only's loop)."""
    agent_type = conf_agent.get("agentType") or ""
    agent_tag = conf_agent.get("agentTag")
    agent_user_tag = conf_agent.get("agentUserTag") or agent_tag
    agent_internal_name = conf_agent.get("agentInternalName") or agent_tag
    if not agent_type or not agent_tag:
        raise RuntimeError("Each agent must declare 'agentType' and 'agentTag'")

    agent_id = _add_agent_to_pipeline_via_corehub(
        token=token,
        pipeline_id=pipeline_id,
        agent_type=str(agent_type).upper(),
        agent_user_tag=str(agent_user_tag),
        agent_internal_name=str(agent_internal_name),
    )

    # Attach agent to pipeline
    fetch_core_hub(
        f"/pipelines/{pipeline_id}/agents/{agent_id}",
        method="PUT",
        token=token,
        params={"agentType": agent_type.upper()}  # agentType is a query parameter, not body
    )
    bound_agents.append(f"{agent_type.upper()} {agent_tag} (id={agent_id})")

    # Extract certificate info before processing credentials
    host_credentials = dict(conf_agent.get("hostCredentials") or {})
    custom_host_credentials = conf_agent.get("customHostCredentials") or {}

    # Extract certificate path and type (will be handled separately)
    certificate_path = host_credentials.pop("certificatePath", None)
    certificate_type = host_credentials.pop("certificateType", None)

    # Store certificate info and credentials for later processing
    # IMPORTANT: If certificate is present, credentials MUST be sent AFTER certificate upload
    # Order: 1. Add agent, 2. Upload certificate, 3. Send credentials
    if certificate_path and certificate_type:
        logger.info(
            f"Agent {agent_id} has certificate - deferring credential upload until after certificate"
        )
        conf_agent["_certificate_path"] = certificate_path
        conf_agent["_certificate_type"] = certificate_type
        conf_agent["_agent_id"] = agent_id
        # Store credentials to send later (after certificate upload)
        conf_agent["_host_credentials"] = host_credentials
        conf_agent["_custom_host_credentials"] = custom_host_credentials
    else:
        # No certificate - send credentials immediately
        _put_agent_credentials_or_record(
            token, pipeline_id, agent_id, agent_type, agent_tag, host_credentials, custom_host_credentials,
            credentials_pending, continue_on_credentials_failure,
        )

    # Apply specific configuration if present. Sent even when the credentials are pending: whether
    # CoreHub takes it without working credentials is not known, so a refusal is recorded, not fatal.
    specific_conf = conf_agent.get("specificConfiguration") or {}
    if specific_conf:
        _put_agent_specific_config(token, pipeline_id, agent_id, specific_conf, credentials_pending)


def _existing_pipeline_names(token):
    """Names of the pipelines already on Core Hub."""
    existing = fetch_core_hub(PIPELINES_PATH, token=token)
    existing_names = set()
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    existing_names.add(name)

    return existing_names


def _choose_import_pipeline_name(base_name, existing_names, on_existing_name):
    """``base_name``, or "<name> (restored[ N])" when taken; raises when ``on_existing_name`` is "fail"."""
    pipeline_name = base_name
    if on_existing_name not in ("suffix", "fail"):
        raise ValueError(f"on_existing_name must be 'suffix' or 'fail', got {on_existing_name!r}")
    if pipeline_name in existing_names and on_existing_name == "fail":
        raise RuntimeError(f"A pipeline named {base_name!r} already exists: nothing created "
                           f"(on-existing-pipeline=fail)")
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

    return pipeline_name


def _create_config_only_pipeline(token, pipeline_name):
    """POST the new pipeline and return its id."""
    pipeline_description = f"Pipeline {pipeline_name}"

    # Create the pipeline
    body = {
        "name": pipeline_name,
        "description": pipeline_description,
        "configurationCompleted": False,
    }
    response = fetch_core_hub(PIPELINES_PATH, method="POST", token=token, body=body)
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected response while creating pipeline: {response}")

    raw_id = response.get("pipelineId") or response.get("id") or response.get("pipeline_id")
    if not raw_id:
        raise RuntimeError("Pipeline creation succeeded but no ID was returned")
    pipeline_id = str(raw_id)
    return pipeline_id


def import_pipeline_config_only(
    *,
    token: str,
    base_url: str,
    use_ssl: Optional[bool],
    skip_verify: Optional[bool],
    config: Dict[str, Any],
    on_existing_name: str = "suffix",
    cleanup_on_failure: bool = False,
    continue_on_credentials_failure: bool = False,
) -> Dict[str, Any]:
    """Create a pipeline and bind agents from a single config object.

    ``continue_on_credentials_failure`` (env GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE): a credentials PUT that
    fails on a connection error or a 5xx no longer stops the import: the agent is listed under
    ``credentialsPending`` of the result and the other agents are still bound. Default: as before.

    ``on_existing_name``: when a pipeline already has the name, ``"suffix"`` (default, as before) creates
    "<name> (restored)", "<name> (restored 2)", ...; ``"fail"`` raises before anything is created (env
    GS_IMPORT_ON_EXISTING_PIPELINE). When a step fails after the pipeline was created, the error names the
    half-created pipeline id and the agents bound so far (:class:`PartialPipelineImportError`);
    ``cleanup_on_failure`` (env GS_IMPORT_CLEANUP_ON_FAILURE) then also DELETEs that pipeline (the
    ``/pipelines/{id}`` route mcp_server already uses) - UNVERIFIED against a live Core Hub.

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
    existing_names = _existing_pipeline_names(token)
    pipeline_name = _choose_import_pipeline_name(base_name, existing_names, on_existing_name)
    pipeline_id = _create_config_only_pipeline(token, pipeline_name)

    bound_agents: List[str] = []
    credentials_pending: List[Dict[str, Any]] = []
    try:
        # Bind each configured agent to the pipeline and apply its settings
        for conf_agent in agents_cfg:
            if not isinstance(conf_agent, dict):
                continue

            _bind_config_agent(
                token, pipeline_id, conf_agent, bound_agents, credentials_pending,
                continue_on_credentials_failure,
            )

    except Exception as exc:
        raise _partial_import_error(token, pipeline_id, pipeline_name, bound_agents, exc, cleanup_on_failure) from exc

    logger.info("Imported config-only pipeline %s (%s)", pipeline_name, pipeline_id)
    result = {"pipelineId": pipeline_id, "pipelineName": pipeline_name}
    if credentials_pending:
        result["credentialsPending"] = credentials_pending
    return result


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
    source_host_override: Optional[str] = None,
    target_host_override: Optional[str] = None,
    source_port_override: Optional[int] = None,
    target_port_override: Optional[int] = None,
    override_schemas: bool = False,
    override_source_schema: Optional[str] = None,
    override_target_schema: Optional[str] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
    chronos_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Duplicate a pipeline with new source and target agent tags (keeping same agent types).

    This function:
    1. Gets the original pipeline's agent types from CoreHub
    2. Exports the YAML configuration of the original pipeline
    3. Creates a new pipeline with the same agent types but new tags
    4. Imports the YAML configuration to the new pipeline

    Args:
        token: CoreHub authentication token
        base_url: CoreHub base URL
        pipeline_id: Original pipeline ID to duplicate
        new_pipeline_name: Name for the new duplicated pipeline
        source_agent_tag: New agent tag for the source (same type as original)
        target_agent_tag: New agent tag for the target (same type as original)
        use_ssl: Whether to use SSL for CoreHub connection
        skip_verify: Whether to skip SSL verification

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
            chronos_token=chronos_token,
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

    # Step 3: Modify the configuration for the new pipeline
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
        base_host_credentials: dict,
        base_custom_credentials: dict,
    ) -> dict:
        if not password:
            raise RuntimeError(f"{label} agent password is required to duplicate the pipeline.")
        base_host_credentials = (base_host_credentials or {}).copy()
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
            "customHostCredentials": (base_custom_credentials or {}).copy(),
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
            *_fetch_agent_credentials(
                token=token,
                pipeline_id=pipeline_id,
                agent=source_agent,
            ),
        ),
        _convert_agent_payload(
            target_agent,
            target_agent_tag,
            target_agent_password,
            "Target",
            target_host_override,
            target_port_override,
            *_fetch_agent_credentials(
                token=token,
                pipeline_id=pipeline_id,
                agent=target_agent,
            ),
        ),
    ]

    # Step 4: Import the modified configuration to create the new pipeline
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
    detailed_entity_errors: list[str] = []

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

                # The function collection goes first: the UDFs may call its classes and the entities bind its
                # functions, so a clone without it is refused (a pipeline without one copies nothing).
                function_collection.copy_function_collection(token, pipeline_id, new_pipeline_id, fetch=fetch_core_hub)

                # The UDF sources go in place before any entity is created: create_entities compiles each
                # table's UDF into the new pipeline before saving its entity, and a Gluesync 2.3 CoreHub
                # drops the reference to a UDF it cannot find before its mapping stage, which then refuses
                # an entity that relies on the UDF (an N:1 with no custom field function, a column no
                # source feeds, a target key the UDF produces).
                try:
                    original_entities = fetch_pipeline_entities(token, pipeline_id)
                except Exception as exc_udf_discovery:  # pylint: disable=broad-except
                    logger.exception("Failed to read the UDFs of pipeline %s: %s", pipeline_id, exc_udf_discovery)
                    udf_clone_status = "failed"
                    udf_clone_errors.append(str(exc_udf_discovery))
                    original_entities = []
                original_udfs = _entity_udf_names(original_entities)
                udfs_to_clone = discover_udfs_from_entities(original_entities)
                udf_root = Path(tempfile.mkdtemp(prefix="gluesync_duplicate_udfs_")) if udfs_to_clone else None
                previous_udf_path = create_user_defined_functions.UDF_PATH
                if udf_root is not None:
                    written = write_udf_sources_to_dir(
                        udf_root, token=token, pipeline_id=pipeline_id, udfs_to_export=udfs_to_clone
                    )
                    logger.info("Wrote %d of %d UDF source(s) to clone into %s", written, len(udfs_to_clone), udf_root)
                    create_user_defined_functions.UDF_PATH = str(udf_root)
                try:
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
                            enable_scheduling=False,
                            create_tables=False,
                            use_ssl=use_ssl,
                            skip_verify=skip_verify,
                            options=EntityRunOptions(chronos_token=chronos_token, restore_function_collection=False),
                        )
                        if not result.get("success"):
                            entity_clone_status = "failed"
                            result_data = result.get("result") or {}
                            errors = []
                            if isinstance(result_data, dict):
                                errors = result_data.get("errors") or []
                            if not errors:
                                errors = result.get("errors") or []
                            error_msg = result.get("error") or "; ".join(errors) if errors else "Unknown error during entity creation"
                            formatted = f"{source_schema}->{target_schema}: {error_msg}"
                            entity_clone_errors.append(formatted)
                            detailed_entity_errors.append(formatted)
                            logger.warning("Entity creation failed for %s -> %s: %s", source_schema, target_schema, error_msg)

                finally:
                    create_user_defined_functions.UDF_PATH = previous_udf_path
                    if udf_root is not None:
                        shutil.rmtree(udf_root, ignore_errors=True)

                # Every entity that referred to a UDF in the original pipeline refers to it in the clone
                # (a failed UDF read keeps its "failed" status).
                if udf_clone_status != "failed" and not original_udfs:
                    udf_clone_status = "none"
                elif udf_clone_status != "failed":
                    cloned_udfs = _entity_udf_names(fetch_pipeline_entities(token, new_pipeline_id))
                    for entity_name, udf_name in sorted(original_udfs.items()):
                        if cloned_udfs.get(entity_name) != udf_name:
                            udf_clone_errors.append(f"Entity {entity_name} was cloned without its UDF {udf_name}")
                    udf_clone_status = "completed" if not udf_clone_errors else "partial"

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

    # Raise exception if entity cloning failed
    if entity_clone_status == "failed":
        error_detail = "Entity cloning failed"
        if entity_clone_errors:
            error_detail = f"Entity cloning failed: {'; '.join(entity_clone_errors)}"
        elif detailed_entity_errors:
            error_detail = f"Entity cloning failed: {'; '.join(detailed_entity_errors)}"
        logger.error(error_detail)
        raise Exception(error_detail)

    return {
        "originalPipelineId": pipeline_id,
        "newPipelineId": new_pipeline_id,
        "newPipelineName": new_pipeline_name,
        "agentsAvailable": True,
        "deployedAgents": [],
        "sourceAgent": {"type": source_agent_type, "tag": source_agent_tag},
        "targetAgent": {"type": target_agent_type, "tag": target_agent_tag},
        "entityCloneStatus": entity_clone_status,
        "entityCloneErrors": entity_clone_errors or None,
        "udfCloneStatus": udf_clone_status,
        "udfCloneErrors": udf_clone_errors or None,
        "entityCloneDetailedErrors": detailed_entity_errors or None,
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
        response = fetch_core_hub(PIPELINES_PATH, token=token)
        return isinstance(response, list)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Token validation failed")
        return False


def upload_agent_certificate(
    *,
    token: str,
    pipeline_id: str,
    agent_id: str,
    certificate_type: str,
    certificate_data: bytes,
    certificate_ext: str,
) -> dict:
    """Upload a certificate file for the specified agent.

    Args:
        token: Authentication token
        pipeline_id: Pipeline ID
        agent_id: Agent ID
        certificate_type: Type of certificate (e.g., 'truststore', 'keystore', 'certificate')
        certificate_data: Certificate file content as bytes
        certificate_ext: Certificate file extension (e.g., 'crt', 'pem', 'jks', 'json')

    Returns:
        Response from CoreHub API
    """
    logger.info(
        "Uploading %s certificate for agent %s in pipeline %s (%d bytes, ext: %s)",
        certificate_type,
        agent_id,
        pipeline_id,
        len(certificate_data),
        certificate_ext,
    )
    
    endpoint = f"/pipelines/{pipeline_id}/agents/{agent_id}/config/certificate/{certificate_type}"
    logger.debug(f"Certificate upload endpoint: {endpoint}")
    logger.debug(f"Certificate-Ext header: {certificate_ext}")

    try:
        response = fetch_core_hub(
            endpoint,
            method='PUT',
            token=token,
            body=certificate_data,
            headers={'Certificate-Ext': certificate_ext},
        )
        
        logger.info(f"Certificate upload successful for agent {agent_id}")
        logger.debug(f"Certificate upload response: {response}")
        return response
    except Exception as e:
        logger.error(
            f"Certificate upload failed for agent {agent_id} in pipeline {pipeline_id}: {e}",
            exc_info=True
        )
        raise

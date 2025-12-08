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
import logging
import os
import zipfile
from contextlib import redirect_stdout
from typing import Any, Callable, Dict, Optional

from commons import configure_core_hub, set_scheduling_enabled, fetch_core_hub, get_pipeline_agents
from create_all_entities import (
    CREATE_TABLE_IF_NOT_EXISTS,
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
import yaml

logger = logging.getLogger(__name__)


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
                filename = f"backup_{safe_name}_{pipeline_id}.yaml"

                # Add to ZIP
                zip_file.writestr(filename, yaml_content)
                logger.info("Exported pipeline %s (%s)", pipeline_name, pipeline_id)

            except Exception as exc:  # pylint: disable=broad-excepts
                logger.exception("Failed to export pipeline %s: %s", pipeline_id, exc)
                # Continue with other pipelines

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

        # After processing all pipelines, write a consolidated agents-config.yaml if any agents were found
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


def validate_token(token: str) -> bool:
    try:
        response = fetch_core_hub("/pipelines", token=token)
        return isinstance(response, list)
    except Exception:  # pylint: disable=broad-except
        logger.exception("Token validation failed")
        return False

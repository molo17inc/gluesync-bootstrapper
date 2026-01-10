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

"""FastAPI application providing a lightweight web UI for create_all_entities."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
import create_user_defined_functions

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator

from commons import extract_all_schemas_from_yaml, extract_schema_types_from_yaml
from . import corehub
from .state import state
from .version import get_version

logger = logging.getLogger(__name__)

CHANGELOG_API_BASE_URL = "https://api.backoffice.molo17.com"


def _resource_path(*parts: str) -> Path:
    """Return an absolute path to packaged resources (PyInstaller compatible)."""

    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def _static_dir() -> Path:
    return _resource_path("static")


class LoginRequest(BaseModel):
    base_url: str = Field(..., alias="baseUrl")
    username: str
    password: str
    use_ssl: bool = Field(False, alias="useSsl")
    skip_verify: bool = Field(False, alias="skipVerify")
    enable_scheduling: bool = Field(True, alias="enableScheduling")
    create_tables: bool = Field(True, alias="createTables")

    @validator("base_url")
    def _strip_url(cls, value: str) -> str:  # pylint: disable=no-self-argument
        return value.strip()

    class Config:
        allow_population_by_field_name = True


class BulkTemplateRequest(BaseModel):
    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    table_names: list[str] = Field(..., alias="tableNames")

    class Config:
        allow_population_by_field_name = True


class RunRequest(BaseModel):
    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    target_schema: str = Field(..., alias="targetSchema")
    source_type: str = Field(..., alias="sourceType")
    target_type: str = Field(..., alias="targetType")
    yaml_file_id: str = Field(..., alias="yamlFileId")
    skip_errors: bool = Field(False, alias="skipErrors")
    chunk_size: int = Field(50, alias="chunkSize", ge=1, le=500)
    enable_scheduling: Optional[bool] = Field(None, alias="enableScheduling")
    create_tables: Optional[bool] = Field(None, alias="createTables")
    use_ssl: Optional[bool] = Field(None, alias="useSsl")
    skip_verify: Optional[bool] = Field(None, alias="skipVerify")
    auto_schemas: bool = Field(True, alias="autoSchemas")

    class Config:
        allow_population_by_field_name = True


class UploadResponse(BaseModel):
    file_id: str = Field(..., alias="fileId")
    filename: str


class StateResponse(BaseModel):
    token_present: bool = Field(..., alias="tokenPresent")
    base_url: Optional[str] = Field(None, alias="baseUrl")
    use_ssl: Optional[bool] = Field(None, alias="useSsl")
    skip_verify: Optional[bool] = Field(None, alias="skipVerify")
    enable_scheduling: bool = Field(..., alias="enableScheduling")
    create_tables: bool = Field(..., alias="createTables")
    run: Optional[dict]
    duplicate: Optional[dict] = None


class RunSnapshot(BaseModel):
    id: str
    status: str
    log_count: int = Field(..., alias="logCount")
    error: Optional[str] = None
    logs: Optional[list[str]] = None


class ApiMessage(BaseModel):
    success: bool = True
    message: Optional[str] = None


class PipelineInfo(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None


class PipelinesResponse(BaseModel):
    pipelines: list[PipelineInfo]


class VersionResponse(BaseModel):
    version: str


class BulkSchemasResponse(BaseModel):
    schemas: list[str]
    source_type: Optional[str] = Field(None, alias="sourceType")
    target_type: Optional[str] = Field(None, alias="targetType")


class BulkTablesResponse(BaseModel):
    tables: list[str]


class BulkCreateRequest(BaseModel):
    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    target_schema: str = Field(..., alias="targetSchema")
    source_type: str = Field(..., alias="sourceType")
    target_type: str = Field(..., alias="targetType")
    table_names: list[str] = Field(..., alias="tableNames")
    chunk_size: int = Field(50, alias="chunkSize", ge=1, le=500)
    skip_errors: bool = Field(True, alias="skipErrors")
    enable_scheduling: Optional[bool] = Field(None, alias="enableScheduling")
    create_tables: Optional[bool] = Field(None, alias="createTables")

    class Config:
        allow_population_by_field_name = True


class DuplicatePipelineRequest(BaseModel):
    new_pipeline_name: Optional[str] = Field(None, alias="newPipelineName")
    source_agent_tag: str = Field(..., alias="sourceAgentTag")
    target_agent_tag: str = Field(..., alias="targetAgentTag")
    source_agent_password: str = Field(..., alias="sourceAgentPassword")
    target_agent_password: str = Field(..., alias="targetAgentPassword")
    conductor_url: Optional[str] = Field(None, alias="conductorUrl")
    clone_entities: bool = Field(True, alias="cloneEntities")

    class Config:
        allow_population_by_field_name = True


class DuplicatePipelineResponse(BaseModel):
    original_pipeline_id: str = Field(..., alias="originalPipelineId")
    new_pipeline_id: str = Field(..., alias="newPipelineId")
    new_pipeline_name: str = Field(..., alias="newPipelineName")
    agents_available: bool = Field(..., alias="agentsAvailable")
    deployed_agents: list[str] = Field(..., alias="deployedAgents")
    source_agent: dict = Field(..., alias="sourceAgent")
    target_agent: dict = Field(..., alias="targetAgent")
    entity_clone_status: Optional[str] = Field(None, alias="entityCloneStatus")
    entity_clone_errors: Optional[list[str]] = Field(None, alias="entityCloneErrors")

    class Config:
        allow_population_by_field_name = True


class PipelineAgentsResponse(BaseModel):
    source_agent_type: Optional[str] = Field(None, alias="sourceAgentType")
    source_agent_tag: Optional[str] = Field(None, alias="sourceAgentTag")
    target_agent_type: Optional[str] = Field(None, alias="targetAgentType")
    target_agent_tag: Optional[str] = Field(None, alias="targetAgentTag")

    class Config:
        allow_population_by_field_name = True


def _ensure_static_assets() -> None:
    static_directory = _static_dir()
    if not static_directory.exists():
        raise RuntimeError(f"Static assets not found at {static_directory}")


def _load_config_from_bytes(contents: bytes, filename: str) -> Any:
    """Parse a config file from bytes, supporting JSON and YAML.

    Format is detected from extension when possible, otherwise JSON is tried
    first and YAML is used as a fallback.
    """

    text = contents.decode("utf-8")
    lower_name = (filename or "").lower()

    if lower_name.endswith((".yaml", ".yml")):
        return yaml.safe_load(text)
    if lower_name.endswith(".json"):
        return json.loads(text)

    # Unknown extension: auto-detect
    try:
        return json.loads(text)
    except Exception:
        return yaml.safe_load(text)


def _has_masked_password(value: Any) -> bool:
    """Recursively detect any password set to the masked sentinel value.

    We specifically look for hostCredentials.password == "*******" or any
    generic "password" field with that literal value.
    """

    sentinel = "*******"

    if isinstance(value, dict):
        # Direct password key
        if str(value.get("password")) == sentinel:
            return True

        # Look inside hostCredentials / customHostCredentials
        for key in ("hostCredentials", "customHostCredentials"):
            sub = value.get(key)
            if isinstance(sub, dict) and str(sub.get("password")) == sentinel:
                return True

        # Recurse into all values
        return any(_has_masked_password(v) for v in value.values())

    if isinstance(value, (list, tuple, set)):
        return any(_has_masked_password(v) for v in value)

    return False


def create_app() -> FastAPI:
    _ensure_static_assets()

    app = FastAPI(title="Gluesync Automator", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        index_path = _static_dir() / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="UI assets missing")
        return FileResponse(index_path)

    @app.get("/api/healthz")
    async def healthcheck() -> dict:
        return {"status": "ok"}

    @app.get("/api/version", response_model=VersionResponse)
    async def version() -> VersionResponse:
        return VersionResponse(version=get_version())

    @app.get("/api/state", response_model=StateResponse)
    async def get_state() -> StateResponse:
        snapshot = state.snapshot()
        return StateResponse(**snapshot)

    @app.get("/api/changelog/automator/{version}")
    async def get_automator_changelog(version: str):
        url = f"{CHANGELOG_API_BASE_URL}/changelog/automator/{version}"
        try:
            resp = requests.get(url, timeout=5)
        except requests.RequestException as exc:  # type: ignore[attr-defined]
            logger.exception("Failed to fetch automator changelog")
            raise HTTPException(status_code=502, detail="Failed to reach changelog service") from exc

        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Changelog not found")

        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Changelog service error: {resp.status_code}",
            )

        try:
            data = resp.json()
        except ValueError as exc:
            logger.exception("Invalid JSON from changelog service")
            raise HTTPException(
                status_code=502,
                detail="Invalid response from changelog service",
            ) from exc

        return data

    @app.post("/api/login", response_model=ApiMessage)
    async def login(payload: LoginRequest) -> ApiMessage:
        try:
            token = corehub.authenticate(
                base_url=payload.base_url,
                username=payload.username,
                password=payload.password,
                use_ssl=payload.use_ssl,
                skip_verify=payload.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Authentication failed")
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        state.set_auth(
            token,
            payload.base_url,
            use_ssl=payload.use_ssl,
            skip_verify=payload.skip_verify,
        )
        state.set_preferences(
            enable_scheduling=payload.enable_scheduling,
            create_tables=payload.create_tables,
        )
        return ApiMessage(message="Authentication successful")

    @app.post("/api/logout", response_model=ApiMessage)
    async def logout() -> ApiMessage:
        state.clear_auth()
        return ApiMessage(message="Logged out")

    @app.post("/api/upload", response_model=UploadResponse)
    async def upload_yaml(file: UploadFile = File(...)) -> UploadResponse:
        if not file.filename.lower().endswith(('.yaml', '.yml')):
            raise HTTPException(status_code=400, detail="Only YAML files are supported")

        temp_dir = Path(tempfile.gettempdir()) / "gluesync_automator"
        temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename).suffix or ".yaml"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=temp_dir) as tmp:
            contents = await file.read()
            tmp.write(contents)
            temp_path = Path(tmp.name)

        file_id = state.register_upload(temp_path, file.filename)
        return UploadResponse(fileId=file_id, filename=file.filename)

    @app.get("/api/run/current")
    async def current_run(include_logs: bool = False):
        snapshot = state.current_run_snapshot()
        if not snapshot:
            return {"run": None}
        response = RunSnapshot(**snapshot)
        if include_logs:
            logs = state.get_logs(response.id)
            response.logs = logs or []
        return response

    @app.get("/api/pipelines", response_model=PipelinesResponse)
    async def list_pipelines() -> PipelinesResponse:
        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            pipelines = corehub.list_pipelines(
                token=state.token,
                base_url=state.base_url,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to list pipelines")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return PipelinesResponse(pipelines=[PipelineInfo(**p) for p in pipelines])

    @app.get("/api/bulk/schemas", response_model=BulkSchemasResponse)
    async def bulk_list_schemas(pipelineId: str) -> BulkSchemasResponse:  # pylint: disable=invalid-name
        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            schemas = corehub.list_source_schemas(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipelineId,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to list source schemas for pipeline %s", pipelineId)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        source_type, target_type = corehub.infer_agent_schema_types(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipelineId,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
        )

        return BulkSchemasResponse(schemas=schemas, source_type=source_type, target_type=target_type)

    @app.get("/api/bulk/tables", response_model=BulkTablesResponse)
    async def bulk_list_tables(pipelineId: str, schema: str) -> BulkTablesResponse:  # pylint: disable=invalid-name
        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            tables = corehub.list_source_tables(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipelineId,
                schema=schema,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to list source tables for pipeline %s schema %s", pipelineId, schema)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return BulkTablesResponse(tables=tables)

    @app.post("/api/bulk/create", response_model=ApiMessage)
    async def bulk_create_entities(request: BulkCreateRequest) -> ApiMessage:
        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        prefs = state.preferences()
        enable_scheduling = request.enable_scheduling
        if enable_scheduling is None:
            enable_scheduling = prefs["enableScheduling"]
        create_tables = request.create_tables
        if create_tables is None:
            create_tables = prefs["createTables"]

        try:
            result = corehub.run_create_entities_for_tables(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=request.pipeline_id,
                source_schema=request.source_schema,
                target_schema=request.target_schema,
                source_type=request.source_type,
                target_type=request.target_type,
                table_names=request.table_names,
                skip_errors=request.skip_errors,
                chunk_size=request.chunk_size,
                enable_scheduling=bool(enable_scheduling),
                create_tables=bool(create_tables),
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Bulk create entities failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        ok = result.get("success", False)
        logs = result.get("logs") or []
        if logs:
            for line in logs:
                logger.info("[bulk-create] %s", line)

        msg = "Bulk entity creation completed successfully" if ok else result.get("error") or "Bulk entity creation failed"
        return ApiMessage(success=ok, message=msg)

    @app.post("/api/bulk/template")
    async def bulk_export_template(request: BulkTemplateRequest):
        """Generate an on-the-fly table-list-style YAML for the selected tables.

        This does *not* export an existing pipeline configuration. Instead it
        produces a minimal table-list-template.yaml-shaped document containing
        only the chosen source schema and tables, so it can be refined and
        later used with the CLI bootstrapper.
        """

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        pipeline_id = request.pipeline_id
        source_schema = request.source_schema
        table_names = [name for name in request.table_names or [] if name]

        if not pipeline_id:
            raise HTTPException(status_code=400, detail="pipelineId is required")
        if not source_schema:
            raise HTTPException(status_code=400, detail="sourceSchema is required")
        if not table_names:
            raise HTTPException(status_code=400, detail="tableNames must contain at least one table")

        # Infer default source/target types from the pipeline agents so the
        # generated template is immediately usable by the CLI.
        try:
            source_type, target_type = corehub.infer_agent_schema_types(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipeline_id,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "Failed to infer schema types for bulk template on pipeline %s", pipeline_id
            )
            source_type, target_type = "SQL", "SQL"

        # Deduplicate tables while preserving selection order
        seen = set()
        unique_tables: list[str] = []
        for raw in table_names:
            name = str(raw)
            if name in seen:
                continue
            seen.add(name)
            unique_tables.append(name)

        custom_cfg: dict[str, Any] = {}
        for name in unique_tables:
            try:
                pk_names = corehub.discover_primary_key_names(
                    token=state.token,
                    base_url=state.base_url,
                    pipeline_id=pipeline_id,
                    schema=source_schema,
                    table_name=name,
                    use_ssl=state.use_ssl,
                    skip_verify=state.skip_verify,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to discover primary keys for %s.%s on pipeline %s: %s",
                    source_schema,
                    name,
                    pipeline_id,
                    exc,
                )
                pk_names = []

            try:
                columns_meta = corehub.discover_column_metadata_for_table(
                    token=state.token,
                    base_url=state.base_url,
                    pipeline_id=pipeline_id,
                    schema=source_schema,
                    table_name=name,
                    use_ssl=state.use_ssl,
                    skip_verify=state.skip_verify,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to discover column metadata for %s.%s on pipeline %s: %s",
                    source_schema,
                    name,
                    pipeline_id,
                    exc,
                )
                columns_meta = []

            entry: dict[str, Any] = {"keys": pk_names}
            if columns_meta:
                entry["columns"] = columns_meta
            custom_cfg[name] = entry

        schema_cfg: dict[str, Any] = {
            "target": source_schema,
            "tables": {
                "whitelist": unique_tables,
                "custom": custom_cfg,
            },
        }

        if source_type:
            schema_cfg["sourceType"] = source_type
        if target_type:
            schema_cfg["targetType"] = target_type

        # Direct schema configuration (no top-level "schemas" wrapper)
        yaml_doc: dict[str, Any] = {source_schema: schema_cfg}

        yaml_text = yaml.safe_dump(
            yaml_doc,
            sort_keys=False,
            allow_unicode=True,
        )

        filename = f"template_{pipeline_id}_{source_schema}.yaml"
        return StreamingResponse(
            io.BytesIO(yaml_text.encode("utf-8")),
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/api/export/pipeline/{pipeline_id}")
    async def export_pipeline(pipeline_id: str):
        """Export only the YAML metadata for a single pipeline."""

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            yaml_text = corehub.export_pipeline_yaml(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipeline_id,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to export pipeline %s", pipeline_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        filename = f"backup_{pipeline_id}.yaml"
        return StreamingResponse(
            io.BytesIO(yaml_text.encode("utf-8")),
            media_type="application/x-yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/api/export/pipeline/{pipeline_id}/full")
    async def export_pipeline_full_backup(pipeline_id: str):
        """Export a full backup (YAML + agents-config + UDFs) for a single pipeline."""

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            zip_data = corehub.export_pipeline_full_backup(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipeline_id,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to export full backup for pipeline %s", pipeline_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Derive a human-friendly filename aligned with the internal YAML name:
        # backup_<safePipelineName>_<pipelineId>.zip
        safe_name = pipeline_id
        try:
            details = corehub.fetch_core_hub(f"/pipelines/{pipeline_id}", token=state.token)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to fetch pipeline %s metadata for filename: %s", pipeline_id, exc)
            details = None
        if isinstance(details, dict):
            name = details.get("name")
            if isinstance(name, str) and name:
                cleaned = "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).rstrip()
                if cleaned:
                    safe_name = cleaned

        filename = f"backup_{safe_name}_{pipeline_id}.zip"
        return StreamingResponse(
            io.BytesIO(zip_data),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post("/api/duplicate/pipeline/{pipeline_id}", response_model=DuplicatePipelineResponse)
    async def duplicate_pipeline(pipeline_id: str, request: DuplicatePipelineRequest):
        """Duplicate a pipeline with new source and target agent configuration.

        This endpoint allows users to clone an existing pipeline with different agents.
        If the specified agents are not available, they can be deployed via conductor APIs.
        """

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        if not request.source_agent_tag:
            raise HTTPException(status_code=400, detail="sourceAgentTag is required")
        if not request.target_agent_tag:
            raise HTTPException(status_code=400, detail="targetAgentTag is required")
        if not request.source_agent_password:
            raise HTTPException(status_code=400, detail="sourceAgentPassword is required")
        if not request.target_agent_password:
            raise HTTPException(status_code=400, detail="targetAgentPassword is required")

        try:
            state.begin_duplicate()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        duplicate_started = True

        try:
            result = corehub.duplicate_pipeline(
                token=state.token,
                base_url=state.base_url,
                pipeline_id=pipeline_id,
                new_pipeline_name=request.new_pipeline_name,
                source_agent_tag=request.source_agent_tag,
                target_agent_tag=request.target_agent_tag,
                source_agent_password=request.source_agent_password,
                target_agent_password=request.target_agent_password,
                clone_entities=request.clone_entities,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
                conductor_url=request.conductor_url,
                cancel_checker=state.is_duplicate_cancelled,
            )
        except corehub.DuplicateCancelledError as exc:
            logger.info("Duplicate pipeline %s cancelled by user", pipeline_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to duplicate pipeline %s", pipeline_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            if duplicate_started:
                state.end_duplicate()

        return DuplicatePipelineResponse(**result)

    @app.post("/api/duplicate/cancel", response_model=ApiMessage)
    async def cancel_duplicate() -> ApiMessage:
        """Request cancellation of an in-flight duplicate pipeline operation."""

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        requested = state.request_duplicate_cancel()
        if not requested:
            return ApiMessage(success=False, message="No duplicate pipeline is currently running")
        return ApiMessage(success=True, message="Duplicate cancellation requested")

    @app.get("/api/pipeline/{pipeline_id}/agents", response_model=PipelineAgentsResponse)
    async def get_pipeline_agents(pipeline_id: str) -> PipelineAgentsResponse:
        """Get pipeline agent details for auto-populating duplicate form."""

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            pipeline_config = corehub.fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=state.token)
            logger.info("Fetched pipeline config for pipeline %s", pipeline_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch pipeline config %s", pipeline_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if not isinstance(pipeline_config, dict):
            raise HTTPException(status_code=500, detail="Unexpected response from CoreHub")

        agents = pipeline_config.get("agents")
        if not isinstance(agents, list):
            agents = []

        source_agent = next(
            (agent for agent in agents if isinstance(agent, dict) and agent.get("agentType") == "SOURCE"),
            None,
        )
        target_agent = next(
            (agent for agent in agents if isinstance(agent, dict) and agent.get("agentType") == "TARGET"),
            None,
        )

        return PipelineAgentsResponse(
            sourceAgentType=source_agent.get("agentType") if source_agent else None,
            sourceAgentTag=source_agent.get("agentTag") if source_agent else None,
            targetAgentType=target_agent.get("agentType") if target_agent else None,
            targetAgentTag=target_agent.get("agentTag") if target_agent else None,
        )

    @app.post("/api/import/all", response_model=ApiMessage)
    async def import_all(
        file: UploadFile = File(...),
        conductor_url: Optional[str] = None,
        conductor_auth_token: Optional[str] = None
    ) -> ApiMessage:
        """Validate a full backup ZIP (pipelines + agents-config).

        The ZIP is expected to come from the Export All feature and contain
        at least an agents-config.yaml file. As with /api/import/config, this
        endpoint currently only validates and enforces password masking rules.
        """

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        # Extract UDF source files (if present) into a temporary directory so they
        # can be compiled automatically during import. Both Export All and
        # single-pipeline Full backup ZIPs place UDFs under folders named
        # 'udf-<agentId>' (optionally nested under pipeline_<id>/ for single
        # pipeline backups).
        udf_root: Optional[Path] = None
        try:
            udf_root = Path(tempfile.gettempdir()) / "gluesync_automator_udfs"
            if udf_root.exists():
                shutil.rmtree(udf_root)
            udf_root.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(io.BytesIO(contents)) as zf_udf:
                for member in zf_udf.namelist():
                    lower = member.lower()
                    # Skip macOS resource-fork entries
                    if member.startswith("__MACOSX/") or member.rsplit("/", 1)[-1].startswith("._"):
                        continue
                    # Only consider files under udf-* folders
                    if "udf-" not in member and not member.lstrip("/").startswith("udf-"):
                        continue
                    # Only extract recognized UDF source extensions
                    if not (
                        lower.endswith(".java")
                        or lower.endswith(".kt")
                        or lower.endswith(".js")
                        or lower.endswith(".py")
                        or lower.endswith(".rb")
                    ):
                        continue

                    dest_path = udf_root / member
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf_udf.open(member) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            # Point the UDF helper module to this directory so it can resolve
            # files via UDF_NAME + extension lookups.
            create_user_defined_functions.UDF_PATH = str(udf_root)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "Failed to extract UDF source files from backup; UDF compilation will be skipped: %s",
                exc,
            )
            udf_root = None

        # First pass: find and parse agents-config.yaml for shared agents + metadata
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                agents_name = None
                for name in zf.namelist():
                    lower = name.lower()
                    if lower.endswith("agents-config.yaml") or lower.endswith("agents-config.yml"):
                        agents_name = name
                        break

                if not agents_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Archive does not contain agents-config.yaml",
                    )

                agents_text = zf.read(agents_name).decode("utf-8")
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to process uploaded ZIP during import-all")
            raise HTTPException(status_code=400, detail=f"Invalid ZIP archive: {exc}") from exc

        try:
            agents_config = yaml.safe_load(agents_text)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to parse agents-config.yaml during import-all")
            raise HTTPException(status_code=400, detail=f"Failed to parse agents-config.yaml: {exc}") from exc

        if _has_masked_password(agents_config):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Config contains masked passwords (*******). "
                    "Please replace them with real passwords before importing."
                ),
            )

        agents_list = agents_config.get("agents")
        if not isinstance(agents_list, list) or not agents_list:
            raise HTTPException(status_code=400, detail="agents-config.yaml does not contain a non-empty 'agents' list")

        # Check agent availability and attempt deployment if needed
        deployed_agents = []
        missing_agents = []

        try:
            unassigned = corehub.fetch_core_hub("/unassigned-agents", token=state.token)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch unassigned agents during import-all")
            errors.append(f"Failed to fetch unassigned agents: {exc}")
            unassigned = []

        def _has_unassigned(agent_type: str, agent_tag: str) -> bool:
            if not isinstance(unassigned, list):
                return False
            for item in unassigned:
                if not isinstance(item, dict):
                    continue
                if item.get("agentType") == agent_type and item.get("agentTag") == agent_tag:
                    return True
            return False

        for cfg in agents_list:
            if not isinstance(cfg, dict):
                continue
            a_type = cfg.get("agentType")
            a_tag = cfg.get("agentTag")
            if not a_type or not a_tag:
                errors.append("One agent definition is missing agentType/agentTag.")
                continue
            if not _has_unassigned(a_type, a_tag):
                missing_agents.append((a_type, a_tag))

        # If agents are missing and conductor is available, attempt deployment
        if missing_agents:
            # Use corehub URL as default for conductor if not specified
            if conductor_url is None:
                # Build conductor URL from corehub URL: same host/protocol/port + /conductor path
                try:
                    from urllib.parse import urljoin
                    conductor_url = urljoin(state.base_url.rstrip('/'), '/conductor')
                    logger.info(f"Using corehub-derived conductor URL: {conductor_url}")
                except Exception as exc:
                    logger.warning(f"Failed to derive conductor URL from corehub: {exc}")
                    conductor_url = None

            if conductor_url and conductor_auth_token:
                logger.info(f"Found {len(missing_agents)} missing agents, attempting deployment via conductor for import...")
                try:
                    deployed_agents = _deploy_agents_for_import(
                        conductor_url=conductor_url,
                        conductor_auth_token=conductor_auth_token,
                        missing_agents=missing_agents,
                    )
                    if deployed_agents:
                        logger.info(f"Successfully deployed {len(deployed_agents)} agents via conductor for import")
                        # Refresh unassigned agents list after deployment
                        try:
                            unassigned = corehub.fetch_core_hub("/unassigned-agents", token=state.token)
                        except Exception as exc:  # pylint: disable=broad-except
                            logger.warning("Failed to refresh unassigned agents after deployment: %s", exc)
                    else:
                        errors.append("Failed to deploy missing agents via conductor")
                        raise HTTPException(status_code=400, detail="Failed to deploy required agents via conductor")
                except Exception as exc:  # pylint: disable=broad-except
                    logger.exception("Failed to deploy agents via conductor during import")
                    errors.append(f"Failed to deploy agents via conductor: {exc}")
                    raise HTTPException(status_code=400, detail=f"Failed to deploy required agents: {exc}")
            else:
                # Report missing agents as errors if no conductor deployment attempted
                for a_type, a_tag in missing_agents:
                    errors.append(
                        f"No unassigned agent available for agentType={a_type!r}, agentTag={a_tag!r}. "
                        "Import requires matching agents to be available first."
                    )
                raise HTTPException(status_code=400, detail="Required agents are not available for import")

        # Build lookup for original pipeline metadata, if present
        pipelines_meta = agents_config.get("pipelines") or []
        meta_by_old_id = {}
        if isinstance(pipelines_meta, list):
            for item in pipelines_meta:
                if not isinstance(item, dict):
                    continue
                old_id = item.get("pipelineId")
                if old_id is not None:
                    meta_by_old_id[str(old_id)] = item

        created_pipelines: list[dict] = []
        errors: list[str] = []

        # Second pass: process each per-pipeline YAML and recreate pipeline + entities
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                for name in zf.namelist():
                    lower = name.lower()
                    # Skip macOS resource-fork entries (e.g. __MACOSX/ or ._filename)
                    if name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1].startswith("._"):
                        continue
                    if not (lower.endswith(".yaml") or lower.endswith(".yml")):
                        continue
                    if "agents-config" in lower:
                        continue

                    try:
                        yaml_text = zf.read(name).decode("utf-8")
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.exception("Failed to read YAML %s from archive", name)
                        errors.append(f"{name}: failed to read from archive: {exc}")
                        continue

                    # Derive original pipeline ID from filename: backup_<safe_name>_<pipelineId>.yaml
                    stem = name.rsplit("/", 1)[-1]  # strip any path
                    stem_no_ext = stem.rsplit(".", 1)[0]
                    old_pipeline_id = None
                    if stem_no_ext.startswith("backup_"):
                        tail = stem_no_ext[len("backup_") :]
                        if "_" in tail:
                            old_pipeline_id = tail.rsplit("_", 1)[-1]
                        else:
                            old_pipeline_id = tail

                    meta = meta_by_old_id.get(str(old_pipeline_id)) if old_pipeline_id is not None else None
                    base_name = None
                    if isinstance(meta, dict):
                        base_name = meta.get("pipelineName")
                    if not base_name:
                        base_name = f"Imported pipeline {old_pipeline_id}" if old_pipeline_id else "Imported pipeline"

                    # Select agents for this pipeline: use per-pipeline mapping when present,
                    # otherwise fall back to the full agents list from agents-config.yaml.
                    agents_for_pipeline = agents_list
                    pipeline_agents_meta = meta.get("agents") if isinstance(meta, dict) else None
                    if isinstance(pipeline_agents_meta, list) and pipeline_agents_meta:
                        selected: list[dict] = []
                        for want in pipeline_agents_meta:
                            if not isinstance(want, dict):
                                continue

                            a_type = want.get("agentType")
                            a_tag = want.get("agentTag")
                            a_id = want.get("agentId") or want.get("id")

                            if not a_type or not a_tag:
                                errors.append(
                                    f"{name}: pipeline agents mapping has an entry without agentType/agentTag; skipping that entry."
                                )
                                continue

                            def _matches_agent(candidate: dict) -> bool:
                                if not isinstance(candidate, dict):
                                    return False
                                if candidate.get("agentType") != a_type or candidate.get("agentTag") != a_tag:
                                    return False
                                if not a_id:
                                    return True
                                cand_id = candidate.get("agentId") or candidate.get("id")
                                return cand_id is not None and str(cand_id) == str(a_id)

                            # Prefer matching by agentId when present to avoid merging distinct
                            # agents that share the same tag/type. Fall back to tag/type only
                            # for backwards compatibility with older exports.
                            match = next((a for a in agents_list if _matches_agent(a)), None)
                            if not match and a_id is not None:
                                match = next(
                                    (
                                        a
                                        for a in agents_list
                                        if isinstance(a, dict)
                                        and a.get("agentType") == a_type
                                        and a.get("agentTag") == a_tag
                                    ),
                                    None,
                                )

                            if not match:
                                errors.append(
                                    f"{name}: agents-config.yaml declares agentType={a_type!r}, agentTag={a_tag!r} for this pipeline "
                                    "but it is not present in the top-level agents list."
                                )
                            else:
                                selected.append(match)

                        if selected:
                            # Deduplicate in case of repeated mappings
                            seen_local: set[tuple] = set()
                            deduped: list[dict] = []
                            for a in selected:
                                key = (a.get("agentType"), a.get("agentTag"))
                                if key in seen_local:
                                    continue
                                seen_local.add(key)
                                deduped.append(a)
                            agents_for_pipeline = deduped
                        else:
                            # If mapping exists but nothing could be resolved, skip this pipeline
                            errors.append(
                                f"{name}: no valid agents could be resolved from the per-pipeline agents mapping; skipping this pipeline."
                            )
                            continue

                    # Create pipeline + bind agents
                    try:
                        cfg = {"pipelineName": base_name, "agents": agents_for_pipeline}
                        result = corehub.import_pipeline_config_only(
                            token=state.token,
                            base_url=state.base_url,
                            use_ssl=state.use_ssl,
                            skip_verify=state.skip_verify,
                            config=cfg,
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.exception("Failed to import config for %s", name)
                        errors.append(f"{name}: failed to create pipeline and bind agents: {exc}")
                        continue

                    new_pipeline_id = result.get("pipelineId")
                    new_pipeline_name = result.get("pipelineName")
                    created_pipelines.append(result)

                    # Write YAML to a temporary file for schema extraction and entity creation
                    try:
                        temp_dir = Path(tempfile.gettempdir()) / "gluesync_automator_restore"
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".yaml", dir=temp_dir) as tmp:
                            tmp.write(yaml_text.encode("utf-8"))
                            yaml_path = Path(tmp.name)
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.exception("Failed to materialize YAML %s to disk", name)
                        errors.append(f"{name}: failed to materialize YAML to disk: {exc}")
                        continue

                    # Determine schemas and optional type hints from YAML
                    schema_pairs = extract_all_schemas_from_yaml(str(yaml_path))
                    if not schema_pairs:
                        errors.append(f"{name}: no schemas found in YAML; skipping entity creation")
                        continue

                    src_type_hint, tgt_type_hint = extract_schema_types_from_yaml(str(yaml_path))
                    source_type = src_type_hint or "SQL"
                    target_type = tgt_type_hint or "SQL"

                    prefs = state.preferences()
                    enable_scheduling = prefs["enableScheduling"]
                    create_tables = prefs["createTables"]

                    overall_success = True
                    for src_schema, tgt_schema in schema_pairs:
                        try:
                            result_run = corehub.run_create_entities(
                                token=state.token,
                                base_url=state.base_url,
                                pipeline_id=new_pipeline_id,
                                source_schema=src_schema,
                                target_schema=tgt_schema,
                                source_type=source_type,
                                target_type=target_type,
                                yaml_file=str(yaml_path),
                                skip_errors=True,
                                chunk_size=50,
                                enable_scheduling=enable_scheduling,
                                create_tables=create_tables,
                                use_ssl=state.use_ssl,
                                skip_verify=state.skip_verify,
                                log_callback=None,
                            )
                            if not result_run.get("success"):
                                overall_success = False
                                if result_run.get("error"):
                                    errors.append(
                                        f"{name}: entity creation error for {src_schema}->{tgt_schema}: {result_run['error']}"
                                    )

                            # After entities are created for this schema pair, automatically
                            # compile and register any UDFs referenced in the YAML using the
                            # exported source files from the backup.
                            if udf_root is not None:
                                try:
                                    create_user_defined_functions.main(
                                        new_pipeline_id,
                                        src_schema,
                                        tgt_schema,
                                        source_type,
                                        target_type,
                                        str(yaml_path),
                                        state.token,
                                        True,  # skip_errors
                                        50,  # chunk_size
                                    )
                                except Exception as exc_udf:  # pylint: disable=broad-except
                                    overall_success = False
                                    logger.exception(
                                        "Failed to process UDFs for pipeline %s (%s) schema %s->%s from %s",
                                        new_pipeline_name,
                                        new_pipeline_id,
                                        src_schema,
                                        tgt_schema,
                                        name,
                                    )
                                    errors.append(
                                        f"{name}: UDF compilation error for {src_schema}->{tgt_schema}: {exc_udf}"
                                    )
                        except Exception as exc:  # pylint: disable=broad-except
                            overall_success = False
                            logger.exception(
                                "Failed to create entities for pipeline %s (%s) from %s", new_pipeline_name, new_pipeline_id, name
                            )
                            errors.append(
                                f"{name}: exception during entity creation for {src_schema}->{tgt_schema}: {exc}"
                            )

                    if not overall_success:
                        logger.warning(
                            "Import-all: entity creation had errors for pipeline %s (%s) from %s",
                            new_pipeline_name,
                            new_pipeline_id,
                            name,
                        )
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Unexpected error during import-all processing")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Build a human-friendly summary message
        if created_pipelines:
            summary_parts = [
                f"{p.get('pipelineName') or '(unnamed)'} ({p.get('pipelineId') or '?'})"
                for p in created_pipelines
            ]
            summary = "; ".join(summary_parts)
            base_msg = f"Imported {len(created_pipelines)} pipeline(s): {summary}."
        else:
            base_msg = "No pipelines were imported from the archive."

        if errors:
            error_msg = " Some items encountered errors: " + "; ".join(errors)
            base_msg += error_msg

        return ApiMessage(message=base_msg)

    @app.post("/api/import/validate-all", response_model=ApiMessage)
    async def validate_all(
        file: UploadFile = File(...),
        conductor_url: Optional[str] = None,
        conductor_auth_token: Optional[str] = None
    ) -> ApiMessage:
        """Dry-run validation for an Export All ZIP archive.

        This endpoint parses the archive, checks that agents-config.yaml is
        present and well formed, verifies that no masked passwords are
        present, and ensures that each per-pipeline YAML can be read and has
        at least one schema pair defined. It does **not** create pipelines,
        bind agents or create entities.
        """

        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        logs: list[str] = []
        errors: list[str] = []

        # Locate and parse agents-config.yaml
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                names = zf.namelist()
                logs.append(f"Archive contains {len(names)} entries.")

                agents_name = None
                for name in names:
                    lower = name.lower()
                    if lower.endswith("agents-config.yaml") or lower.endswith("agents-config.yml"):
                        agents_name = name
                        break

                if not agents_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Archive does not contain agents-config.yaml",
                    )

                logs.append(f"Found agents config: {agents_name}")
                agents_text = zf.read(agents_name).decode("utf-8")
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to process uploaded ZIP during validate-all")
            raise HTTPException(status_code=400, detail=f"Invalid ZIP archive: {exc}") from exc

        try:
            agents_config = yaml.safe_load(agents_text)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to parse agents-config.yaml during validate-all")
            raise HTTPException(status_code=400, detail=f"Failed to parse agents-config.yaml: {exc}") from exc

        if _has_masked_password(agents_config):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Config contains masked passwords (*******). "
                    "Please replace them with real passwords before importing."
                ),
            )

        agents_list = agents_config.get("agents")
        if not isinstance(agents_list, list) or not agents_list:
            raise HTTPException(status_code=400, detail="agents-config.yaml does not contain a non-empty 'agents' list")

        logs.append(f"agents-config.yaml defines {len(agents_list)} agent configuration(s).")

        # Check agent availability and attempt deployment if needed
        deployed_agents = []
        missing_agents = []

        try:
            unassigned = corehub.fetch_core_hub("/unassigned-agents", token=state.token)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch unassigned agents during validate-all")
            errors.append(f"Failed to fetch unassigned agents: {exc}")
            unassigned = []

        def _has_unassigned(agent_type: str, agent_tag: str) -> bool:
            if not isinstance(unassigned, list):
                return False
            for item in unassigned:
                if not isinstance(item, dict):
                    continue
                if item.get("agentType") == agent_type and item.get("agentTag") == agent_tag:
                    return True
            return False

        for cfg in agents_list:
            if not isinstance(cfg, dict):
                continue
            a_type = cfg.get("agentType")
            a_tag = cfg.get("agentTag")
            if not a_type or not a_tag:
                errors.append("One agent definition is missing agentType/agentTag.")
                continue
            if not _has_unassigned(a_type, a_tag):
                missing_agents.append((a_type, a_tag))

        # If agents are missing and conductor is available, attempt deployment
        if missing_agents:
            # Use corehub URL as default for conductor if not specified
            if conductor_url is None:
                # Build conductor URL from corehub URL: same host/protocol/port + /conductor path
                try:
                    from urllib.parse import urljoin
                    conductor_url = urljoin(state.base_url.rstrip('/'), '/conductor')
                    logger.info(f"Using corehub-derived conductor URL: {conductor_url}")
                except Exception as exc:
                    logger.warning(f"Failed to derive conductor URL from corehub: {exc}")
                    conductor_url = None

            if conductor_url and conductor_auth_token:
                logs.append(f"Found {len(missing_agents)} missing agents, attempting deployment via conductor...")
                try:
                    deployed_agents = _deploy_agents_for_import(
                        conductor_url=conductor_url,
                        conductor_auth_token=conductor_auth_token,
                        missing_agents=missing_agents,
                    )
                    if deployed_agents:
                        logs.append(f"Successfully deployed {len(deployed_agents)} agents via conductor")
                        # Refresh unassigned agents list after deployment
                        try:
                            unassigned = corehub.fetch_core_hub("/unassigned-agents", token=state.token)
                        except Exception as exc:  # pylint: disable=broad-except
                            logger.warning("Failed to refresh unassigned agents after deployment: %s", exc)
                    else:
                        errors.append("Failed to deploy missing agents via conductor")
                except Exception as exc:  # pylint: disable=broad-except
                    logger.exception("Failed to deploy agents via conductor during validation")
                    errors.append(f"Failed to deploy agents via conductor: {exc}")
            else:
                # Report missing agents as errors if no conductor deployment attempted
                for a_type, a_tag in missing_agents:
                    errors.append(
                        f"No unassigned agent available for agentType={a_type!r}, agentTag={a_tag!r}. "
                        "Import may fail unless matching agents are created first."
                    )

        # Fetch existing pipelines to estimate final pipeline names that would be created
        existing_names: set[str] = set()
        try:
            existing = corehub.fetch_core_hub("/pipelines", token=state.token)
            if isinstance(existing, list):
                for item in existing:
                    if isinstance(item, dict):
                        name = item.get("name")
                        if isinstance(name, str) and name:
                            existing_names.add(name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to fetch existing pipelines during validate-all")
            errors.append(f"Failed to fetch existing pipelines: {exc}")

        reserved_names = set(existing_names)

        # Validate each per-pipeline YAML can be parsed and has schemas
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as zf:
                for name in zf.namelist():
                    lower = name.lower()
                    # Skip macOS resource-fork entries (e.g. __MACOSX/ or ._filename)
                    if name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1].startswith("._"):
                        continue
                    if not (lower.endswith(".yaml") or lower.endswith(".yml")):
                        continue
                    if "agents-config" in lower:
                        continue

                    logs.append(f"Validating pipeline definition: {name}")

                    try:
                        yaml_text = zf.read(name).decode("utf-8")
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.exception("Failed to read YAML %s from archive during validate-all", name)
                        errors.append(f"{name}: failed to read from archive: {exc}")
                        continue

                    try:
                        temp_dir = Path(tempfile.gettempdir()) / "gluesync_automator_validate"
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".yaml", dir=temp_dir) as tmp:
                            tmp.write(yaml_text.encode("utf-8"))
                            yaml_path = Path(tmp.name)
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.exception("Failed to materialize YAML %s to disk during validate-all", name)
                        errors.append(f"{name}: failed to materialize YAML to disk: {exc}")
                        continue

                    # Estimate the pipeline name that would be used on import
                    stem = name.rsplit("/", 1)[-1]
                    stem_no_ext = stem.rsplit(".", 1)[0]
                    old_pipeline_id = None
                    if stem_no_ext.startswith("backup_"):
                        tail = stem_no_ext[len("backup_") :]
                        if "_" in tail:
                            old_pipeline_id = tail.rsplit("_", 1)[-1]
                        else:
                            old_pipeline_id = tail

                    base_name = f"Imported pipeline {old_pipeline_id}" if old_pipeline_id else "Imported pipeline"
                    pipeline_name = base_name
                    if pipeline_name in reserved_names:
                        idx = 1
                        while True:
                            suffix = " (restored)" if idx == 1 else f" (restored {idx})"
                            candidate = f"{base_name}{suffix}"
                            if candidate not in reserved_names:
                                pipeline_name = candidate
                                break
                            idx += 1
                    reserved_names.add(pipeline_name)

                    logs.append(
                        f"{name}: would create pipeline '{pipeline_name}'"
                        + (f" from original ID {old_pipeline_id}" if old_pipeline_id else "")
                    )

                    schema_pairs = extract_all_schemas_from_yaml(str(yaml_path))
                    if not schema_pairs:
                        errors.append(f"{name}: no schemas found in YAML; entities cannot be recreated.")
                    else:
                        logs.append(
                            f"{name}: discovered {len(schema_pairs)} schema pair(s): "
                            + ", ".join(f"{s}->{t}" for s, t in schema_pairs)
                        )
        except HTTPException:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Unexpected error during validate-all processing")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        success = not errors
        if success:
            logs.insert(0, "Validation OK: archive is structurally consistent and ready to import.")
        else:
            logs.insert(0, "Validation failed: one or more problems were detected.")
            logs.append("Errors:")
            logs.extend(f"- {e}" for e in errors)

        return ApiMessage(success=success, message="\n".join(logs))

    @app.get("/api/export/all-pipelines")
    async def export_all_pipelines():
        if not state.token or not state.base_url:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            zip_data = corehub.export_all_pipelines_yaml(
                token=state.token,
                base_url=state.base_url,
                use_ssl=state.use_ssl,
                skip_verify=state.skip_verify,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to export all pipelines")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        filename = "pipeline_backups.zip"
        return StreamingResponse(
            io.BytesIO(zip_data),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    async def _execute_run(run_id: str, request: RunRequest) -> None:
        token = state.token
        base_url = state.base_url
        if not token or not base_url:
            state.finalize_run(run_id, "failed", "Authentication required")
            return

        prefs = state.preferences()
        enable_scheduling = request.enable_scheduling if request.enable_scheduling is not None else prefs["enableScheduling"]
        create_tables = request.create_tables if request.create_tables is not None else prefs["createTables"]
        use_ssl = request.use_ssl if request.use_ssl is not None else state.use_ssl
        skip_verify = request.skip_verify if request.skip_verify is not None else state.skip_verify

        yaml_path = state.get_upload_path(request.yaml_file_id)
        if not yaml_path:
            state.finalize_run(run_id, "failed", "Uploaded YAML not found")
            return

        def _log_callback(message: str) -> None:
            state.append_log(run_id, message)

        loop = asyncio.get_running_loop()

        def _run_sync() -> dict:
            yaml_file_path = str(yaml_path)

            if request.auto_schemas:
                schema_pairs = extract_all_schemas_from_yaml(yaml_file_path)
                if not schema_pairs:
                    return {
                        "success": False,
                        "logs": [],
                        "error": f"No schemas found in YAML file: {yaml_file_path}",
                    }

                overall_success = True
                errors: list[str] = []

                for idx, (src_schema, tgt_schema) in enumerate(schema_pairs, start=1):
                    if _log_callback is not None:
                        _log_callback(
                            f"[Schema {idx}/{len(schema_pairs)}] Creating entities for {src_schema} -> {tgt_schema}"
                        )

                    result = corehub.run_create_entities(
                        token=token,
                        base_url=base_url,
                        pipeline_id=request.pipeline_id,
                        source_schema=src_schema,
                        target_schema=tgt_schema,
                        source_type=request.source_type,
                        target_type=request.target_type,
                        yaml_file=yaml_file_path,
                        skip_errors=request.skip_errors,
                        chunk_size=request.chunk_size,
                        enable_scheduling=enable_scheduling,
                        create_tables=create_tables,
                        use_ssl=use_ssl,
                        skip_verify=skip_verify,
                        log_callback=_log_callback,
                    )

                    if not result.get("success"):
                        overall_success = False
                        if result.get("error"):
                            errors.append(str(result["error"]))

                return {
                    "success": overall_success,
                    "logs": [],
                    "error": "; ".join(errors) if errors else None,
                }

            # Custom schemas provided by user
            return corehub.run_create_entities(
                token=token,
                base_url=base_url,
                pipeline_id=request.pipeline_id,
                source_schema=request.source_schema,
                target_schema=request.target_schema,
                source_type=request.source_type,
                target_type=request.target_type,
                yaml_file=yaml_file_path,
                skip_errors=request.skip_errors,
                chunk_size=request.chunk_size,
                enable_scheduling=enable_scheduling,
                create_tables=create_tables,
                use_ssl=use_ssl,
                skip_verify=skip_verify,
                log_callback=_log_callback,
            )

        result = await loop.run_in_executor(None, _run_sync)

        status = "completed" if result.get("success") else "failed"
        state.finalize_run(run_id, status, result.get("error"))

    @app.post("/api/run", response_model=ApiMessage)
    async def start_run(request: RunRequest) -> ApiMessage:
        if not state.token:
            raise HTTPException(status_code=401, detail="Authentication required")

        try:
            run_status = state.start_run()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        asyncio.create_task(_execute_run(run_status.run_id, request))
        return ApiMessage(message="Entity creation started")

    return app


def _deploy_agents_for_import(
    *,
    conductor_url: str,
    conductor_auth_token: str,
    missing_agents: list[tuple[str, str]],
) -> list[str]:
    """Deploy missing agents via conductor APIs for backup import.

    Args:
        conductor_url: Conductor API base URL
        conductor_auth_token: Authentication token for conductor
        missing_agents: List of (agent_type, agent_tag) tuples for missing agents

    Returns:
        List of deployed agent service names
    """
    logger.info("Deploying %d missing agents via conductor for backup import", len(missing_agents))

    # Import the conductor function here to avoid circular imports
    import sys
    import os
    parent_dir = os.path.dirname(os.path.dirname(__file__))
    sys.path.insert(0, parent_dir)

    try:
        from add_agents_with_conductor import add_agents_with_conductor

        # Create a temporary config.json for the agents
        config_data = {
            "globals": {
                "testName": f"backup_import_{len(missing_agents)}_agents",
                "jobId": "backup_import_job"
            },
            "agents": [
                {
                    "agentTag": agent_tag,
                    "agentType": agent_type.lower(),
                } for agent_type, agent_tag in missing_agents
            ]
        }

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
                conductor_url=conductor_url,
                auth_token=conductor_auth_token
            )

            if result.get("error"):
                logger.error("Conductor deployment failed for backup import: %s", result["error"])
                return []

            logger.info("Successfully deployed agents via conductor for backup import")
            return result.get("service_names", [])

        finally:
            # Clean up temporary file
            os.unlink(config_path)

    except ImportError as exc:
        logger.warning("Could not import conductor functions for backup import: %s", exc)
        return []
    except Exception as exc:
        logger.exception("Failed to deploy agents via conductor for backup import: %s", exc)
        return []

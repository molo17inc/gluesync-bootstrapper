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
import logging
import requests
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator

from commons import extract_all_schemas_from_yaml
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


def _ensure_static_assets() -> None:
    static_directory = _static_dir()
    if not static_directory.exists():
        raise RuntimeError(f"Static assets not found at {static_directory}")


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

    @app.get("/api/export/pipeline/{pipeline_id}")
    async def export_pipeline(pipeline_id: str):
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


app = create_app()
"""ASGI application instance for Uvicorn."""

"""FastAPI application providing a lightweight web UI for create_all_entities."""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator

from . import corehub
from .state import state

logger = logging.getLogger(__name__)


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

    @app.get("/api/state", response_model=StateResponse)
    async def get_state() -> StateResponse:
        snapshot = state.snapshot()
        return StateResponse(**snapshot)

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
            return corehub.run_create_entities(
                token=token,
                base_url=base_url,
                pipeline_id=request.pipeline_id,
                source_schema=request.source_schema,
                target_schema=request.target_schema,
                source_type=request.source_type,
                target_type=request.target_type,
                yaml_file=str(yaml_path),
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

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
import mimetypes
import logging
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Optional
import hashlib
import re

import requests
import yaml
import create_user_defined_functions

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from commons import extract_all_schemas_from_yaml, extract_schema_types_from_yaml
from . import corehub
import ai_settings
from .corehub import PipelineInMaintenanceError
from .state import state
from .version import get_version

logger = logging.getLogger(__name__)

LOGIN_REQUIRED = "Log in to a Core Hub first"
AUTH_REQUIRED = "Authentication required"
APPLICATION_ZIP = "application/zip"
UPLOADED_FILE_EMPTY = "Uploaded file is empty"
IMPORT_ALL_PARSE_FAILED = "Failed to parse %s during import-all: %s"
CLAUDE_DESKTOP_CONFIG = "claude_desktop_config.json"
UDF_SOURCE_EXTENSIONS = (".java", ".kt", ".js", ".py", ".rb")
YAML_SUFFIX = ".yaml"
YAML_EXTENSIONS = (YAML_SUFFIX, ".yml")
ZIP_SUFFIX = ".zip"
ZIP_MEDIA_TYPE = mimetypes.types_map[ZIP_SUFFIX]
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

CHANGELOG_API_BASE_URL = "https://api.backoffice.molo17.com"

# File stems that should be skipped when scanning a backup ZIP for pipeline YAMLs
# because they are global CoreHub config files, not pipeline definitions.
GLOBAL_CONFIG_NAMES = {
    "global-config", "global-configs", "smtp", "webhooks",
    "thresholds", "schedules", "users", "oidc",
}


def get_workspace_root() -> Path:
    """Return the stable, app-owned working directory for uploads/imports.

    Files extracted from uploaded YAML/ZIP backups (YAML configs + UDF source
    files) must survive between the upload request and the later entity-creation
    run. The OS temp directory (tempfile.gettempdir() -> %TEMP% on Windows) is a
    poor choice for this because:
      * Windows Disk Cleanup / Storage Sense / 'delete temp files older than N
        days' policies wipe it, so a UDF file extracted earlier can vanish
        before the entity is recreated;
      * %TEMP% is per-Windows-user, so the path shown to a user may not be the
        one the running process (possibly a service account) reads from.

    Instead we use a dedicated, persistent application directory:
      * Windows: %LOCALAPPDATA%\\Gluesync\\Automator\\work
      * macOS:   ~/Library/Application Support/Gluesync/Automator/work
      * Linux:   $XDG_DATA_HOME/gluesync/automator/work
                 (or ~/.local/share/gluesync/automator/work)

    Override with the GLUESYNC_AUTOMATOR_WORKDIR environment variable.
    """
    override = os.environ.get("GLUESYNC_AUTOMATOR_WORKDIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = (Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local") \
            / "Gluesync" / "Automator" / "work"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Gluesync" / "Automator" / "work"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".local" / "share") \
            / "gluesync" / "automator" / "work"
    base.mkdir(parents=True, exist_ok=True)
    return base


def new_session_dir(kind: str) -> Path:
    """Create and return a fresh, self-contained session directory.

    Each upload/import gets a single root that holds BOTH the YAML and its UDF
    source files together, so UDF lookups never have to span sibling folders.
    """
    session_dir = get_workspace_root() / f"{kind}_{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def prune_workspace_sessions(max_age_hours: int = 48) -> None:
    """Best-effort cleanup of stale session directories under the workspace root.

    The app owns this directory, so cleanup is explicit and predictable
    (unlike relying on the OS to wipe %TEMP% at an unknown time).
    """
    try:
        root = get_workspace_root()
        cutoff = datetime.now().timestamp() - max_age_hours * 3600
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
    except Exception:  # pylint: disable=broad-except
        # Cleanup must never break an upload/import.
        pass


def _copy_zip_member(zf: zipfile.ZipFile, member: str, dest: Path) -> None:
    """Write one member of an open ZIP archive to dest."""
    with zf.open(member) as src, open(dest, "wb") as dst:
        shutil.copyfileobj(src, dst)


def _write_session_file(directory: Path, suffix: str, data: bytes) -> Path:
    """Write data to a new uniquely named file in directory and return its path."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=directory) as tmp:
        tmp.write(data)
        return Path(tmp.name)


def _extract_pipeline_id_from_backup_stem(stem_no_ext: str) -> Optional[str]:
    """Extract the original pipeline ID from a backup YAML filename stem.

    Supported formats:
    - backup_<safe_name>_<pipelineId>_<YYYYMMDD>_<HHMMSS>  (current exports)
    - backup_<safe_name>_<pipelineId>                       (legacy exports)
    - backup_<pipelineId>
    """

    if not stem_no_ext.startswith("backup_"):
        return None

    tail = stem_no_ext[len("backup_"):]
    parts = tail.split("_")

    # Strip trailing timestamp segments (HHMMSS then YYYYMMDD) when present
    if len(parts) >= 3 and re.fullmatch(r"\d{6}", parts[-1]) and re.fullmatch(r"\d{8}", parts[-2]):
        parts = parts[:-2]

    return (parts[-1] or None) if parts else None


def _extract_export_metadata_from_yaml_text(yaml_text: str) -> tuple[Optional[str], Optional[str]]:
    """Return (pipelineId, pipelineName) from the embedded exportMetadata key, if present.

    Newer exports embed the original pipeline identity directly in the YAML so
    imports do not depend on the backup filename. Returns (None, None) for
    legacy backups or unparsable YAML.
    """

    try:
        parsed = yaml.safe_load(yaml_text)
    except Exception:  # pylint: disable=broad-except
        return None, None

    if not isinstance(parsed, dict):
        return None, None

    meta = parsed.get("exportMetadata")
    if not isinstance(meta, dict):
        return None, None

    pipeline_id = meta.get("pipelineId")
    pipeline_name = meta.get("pipelineName")
    return (
        str(pipeline_id) if pipeline_id else None,
        str(pipeline_name) if pipeline_name else None,
    )


def _resource_path(*parts: str) -> Path:
    """Return an absolute path to packaged resources (PyInstaller compatible)."""

    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def _static_dir() -> Path:
    return _resource_path("static")


_FINGERPRINT_TARGETS = ("styles.css", "app.js")
_FINGERPRINT_PATTERN = re.compile(r"^(?P<name>.+)\.[0-9a-f]{8,}\.(?P<suffix>[^.]+)$")


def _fingerprint_static_assets() -> Dict[str, str]:
    """Generate hashed filenames for key static assets to enable cache busting."""

    static_directory = _static_dir()
    rewrites: Dict[str, str] = {}

    if not static_directory.exists():
        return rewrites

    for filename in _FINGERPRINT_TARGETS:
        source_path = static_directory / filename
        if not source_path.exists():
            continue

        contents = source_path.read_bytes()
        digest = hashlib.sha256(contents).hexdigest()[:12]
        fingerprint_name = f"{source_path.stem}.{digest}{source_path.suffix}"
        fingerprint_path = source_path.with_name(fingerprint_name)

        # Only write the fingerprinted file if missing or outdated
        if not fingerprint_path.exists() or fingerprint_path.read_bytes() != contents:
            fingerprint_path.write_bytes(contents)

        _cleanup_old_fingerprints(static_directory, source_path.name, fingerprint_path.name)

        rewrites[f"/static/{filename}"] = f"/static/{fingerprint_name}"

    return rewrites


def _cleanup_old_fingerprints(static_dir: Path, original_name: str, keep_name: str) -> None:
    base_stem = original_name.split('.')[0]
    suffix = original_name.split('.')[-1]
    pattern = re.compile(rf"^{re.escape(base_stem)}\.[0-9a-f]{{8,}}\.{re.escape(suffix)}$")

    for candidate in static_dir.glob(f"{base_stem}.*.{suffix}"):
        name = candidate.name
        if name in {original_name, keep_name}:
            continue
        if pattern.match(name):
            try:
                candidate.unlink()
            except OSError:
                pass


class LoginRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    base_url: str = Field(..., alias="baseUrl")
    username: str
    password: str
    use_ssl: bool = Field(False, alias="useSsl")
    skip_verify: bool = Field(False, alias="skipVerify")
    enable_scheduling: bool = Field(True, alias="enableScheduling")
    create_tables: bool = Field(True, alias="createTables")

    @field_validator("base_url")
    def _strip_url(cls, value: str) -> str:  # pylint: disable=no-self-argument
        return value.strip()


class BulkTemplateRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    target_schema: str = Field(..., alias="targetSchema")
    table_names: list[str] = Field(..., alias="tableNames")


class RunRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    target_schema: str = Field(..., alias="targetSchema")
    source_type: str = Field("", alias="sourceType")
    target_type: str = Field("", alias="targetType")
    yaml_file_id: str = Field(..., alias="yamlFileId")
    skip_errors: bool = Field(False, alias="skipErrors")
    chunk_size: int = Field(50, alias="chunkSize", ge=1, le=500)
    enable_scheduling: Optional[bool] = Field(None, alias="enableScheduling")
    create_tables: Optional[bool] = Field(None, alias="createTables")
    use_ssl: Optional[bool] = Field(None, alias="useSsl")
    skip_verify: Optional[bool] = Field(None, alias="skipVerify")
    auto_schemas: bool = Field(True, alias="autoSchemas")


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
    corehub_overview: Optional[dict] = Field(None, alias="corehubOverview")


class RunSnapshot(BaseModel):
    id: str
    status: str
    log_count: int = Field(..., alias="logCount")
    error: Optional[str] = None
    logs: Optional[list[str]] = None


class ApiMessage(BaseModel):
    success: bool = True
    message: Optional[str] = None
    logs: Optional[list[str]] = None


class AiSettingsExportRequest(BaseModel):
    """The passphrase travels in the body, never in a URL that proxies and logs keep."""

    include_secrets: bool = False
    passphrase: Optional[str] = None


class FullBackupRequest(BaseModel):
    include_secrets: bool = False
    ai_settings_passphrase: Optional[str] = None


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
    model_config = ConfigDict(validate_by_name=True)

    pipeline_id: str = Field(..., alias="pipelineId")
    source_schema: str = Field(..., alias="sourceSchema")
    target_schema: str = Field(..., alias="targetSchema")
    source_type: str = Field("", alias="sourceType")
    target_type: str = Field("", alias="targetType")
    table_names: list[str] = Field(..., alias="tableNames")
    chunk_size: int = Field(50, alias="chunkSize", ge=1, le=500)
    skip_errors: bool = Field(True, alias="skipErrors")
    enable_scheduling: Optional[bool] = Field(None, alias="enableScheduling")
    create_tables: Optional[bool] = Field(None, alias="createTables")


class DuplicatePipelineRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    new_pipeline_name: Optional[str] = Field(None, alias="newPipelineName")
    source_agent_tag: str = Field(..., alias="sourceAgentTag")
    target_agent_tag: str = Field(..., alias="targetAgentTag")
    source_agent_password: str = Field(..., alias="sourceAgentPassword")
    target_agent_password: str = Field(..., alias="targetAgentPassword")
    clone_entities: bool = Field(True, alias="cloneEntities")
    source_host_override: Optional[str] = Field(None, alias="sourceHost")
    target_host_override: Optional[str] = Field(None, alias="targetHost")
    source_port_override: Optional[int] = Field(None, alias="sourcePort")
    target_port_override: Optional[int] = Field(None, alias="targetPort")
    override_schemas: bool = Field(False, alias="overrideSchemas")
    override_source_schema: Optional[str] = Field(None, alias="overrideSourceSchema")
    override_target_schema: Optional[str] = Field(None, alias="overrideTargetSchema")


class DuplicatePipelineResponse(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    original_pipeline_id: str = Field(..., alias="originalPipelineId")
    new_pipeline_id: str = Field(..., alias="newPipelineId")
    new_pipeline_name: str = Field(..., alias="newPipelineName")
    agents_available: bool = Field(..., alias="agentsAvailable")
    deployed_agents: list[str] = Field(..., alias="deployedAgents")
    source_agent: dict = Field(..., alias="sourceAgent")
    target_agent: dict = Field(..., alias="targetAgent")
    entity_clone_status: Optional[str] = Field(None, alias="entityCloneStatus")
    entity_clone_errors: Optional[list[str]] = Field(None, alias="entityCloneErrors")


class PipelineAgentsResponse(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    source_agent_type: Optional[str] = Field(None, alias="sourceAgentType")
    source_agent_tag: Optional[str] = Field(None, alias="sourceAgentTag")
    target_agent_type: Optional[str] = Field(None, alias="targetAgentType")
    target_agent_tag: Optional[str] = Field(None, alias="targetAgentTag")


class UploadCertificateRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    pipeline_id: str = Field(..., alias="pipelineId")
    agent_id: str = Field(..., alias="agentId")
    certificate_type: str = Field(..., alias="certificateType")


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

    if lower_name.endswith(YAML_EXTENSIONS):
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


_NOT_LOGGED_IN_RESPONSE: Dict[str, Any] = {"description": LOGIN_REQUIRED}
_FAILURE_RESPONSE: Dict[str, Any] = {"description": "Core Hub or the Automator failed"}

_ERROR_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The request or the bundle is refused (passphrase, format, references)"},
    401: _NOT_LOGGED_IN_RESPONSE,
    409: {"description": "The Core Hub has no AI settings backup (older than 2.3.0.1)"},
    500: _FAILURE_RESPONSE,
}
_UPLOAD_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The file is not a YAML or ZIP configuration, is empty, or cannot be extracted"},
}
_COREHUB_CALL_RESPONSES: Dict[Any, Dict[str, Any]] = {
    401: _NOT_LOGGED_IN_RESPONSE,
    500: _FAILURE_RESPONSE,
}
_PIPELINE_EXPORT_RESPONSES: Dict[Any, Dict[str, Any]] = {
    401: _NOT_LOGGED_IN_RESPONSE,
    409: {"description": "The pipeline is in maintenance"},
    500: _FAILURE_RESPONSE,
}
_FILE_IMPORT_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The uploaded file is empty or cannot be read"},
    401: _NOT_LOGGED_IN_RESPONSE,
    500: _FAILURE_RESPONSE,
}
_MCP_INSTALL_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "No MCP client is selected"},
    500: {"description": "No selected MCP client could be configured"},
}
_UI_RESPONSES: Dict[Any, Dict[str, Any]] = {
    500: {"description": "The UI assets are missing"},
}
_CHANGELOG_RESPONSES: Dict[Any, Dict[str, Any]] = {
    404: {"description": "No changelog is published for this version"},
    502: {"description": "The changelog service is unreachable or answered with an error"},
}
_LOGIN_RESPONSES: Dict[Any, Dict[str, Any]] = {
    401: {"description": "Core Hub refused the credentials"},
}
_TEMPLATE_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The pipeline, the schemas or the tables are missing"},
    401: _NOT_LOGGED_IN_RESPONSE,
}
_DUPLICATE_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "An agent tag, password or override schema is missing, or the duplicate was cancelled"},
    401: _NOT_LOGGED_IN_RESPONSE,
    409: {"description": "Another pipeline duplicate is running"},
    500: _FAILURE_RESPONSE,
}
_SESSION_RESPONSES: Dict[Any, Dict[str, Any]] = {
    401: _NOT_LOGGED_IN_RESPONSE,
}
_ARCHIVE_IMPORT_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "The archive is empty, is not a ZIP, or has no usable agents-config.yaml"},
    401: _NOT_LOGGED_IN_RESPONSE,
    500: _FAILURE_RESPONSE,
}
_RUN_RESPONSES: Dict[Any, Dict[str, Any]] = {
    401: _NOT_LOGGED_IN_RESPONSE,
    409: {"description": "An entity creation run is already in progress"},
}
_CERTIFICATE_RESPONSES: Dict[Any, Dict[str, Any]] = {
    400: {"description": "No certificate file was sent, or it is empty"},
    401: _NOT_LOGGED_IN_RESPONSE,
    500: _FAILURE_RESPONSE,
}


def _require_login() -> None:
    if not state.token or not state.base_url:
        raise HTTPException(status_code=401, detail=LOGIN_REQUIRED)


def _require_session() -> None:
    """Refuse the request when no Core Hub session is active."""
    if not state.token or not state.base_url:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED)


async def _read_nonempty_upload(file: UploadFile) -> bytes:
    """Read an uploaded file, refusing it when it is empty."""
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail=UPLOADED_FILE_EMPTY)
    return contents


def _attachment(payload: bytes, media_type: str, filename: str) -> StreamingResponse:
    """Stream payload as a file download named filename."""
    return StreamingResponse(
        io.BytesIO(payload),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _is_macos_metadata(member: str) -> bool:
    """Tell the macOS resource-fork entries of a ZIP (__MACOSX/ or ._filename) apart."""
    return member.startswith("__MACOSX/") or member.rsplit("/", 1)[-1].startswith("._")


def _is_udf_source(member: str) -> bool:
    """Tell a UDF source file of a backup ZIP (under a udf-* folder, known extension)."""
    return ("udf-" in member or member.lstrip("/").startswith("udf-")) and member.lower().endswith(
        UDF_SOURCE_EXTENSIONS
    )


def _extract_udf_source(zf: zipfile.ZipFile, member: str, root: Path) -> None:
    """Extract one UDF source file under root, keeping its path inside the ZIP."""
    dest = root / member
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_zip_member(zf, member, dest)


def _summarize_restore(results: Dict[str, Any]) -> str:
    """One part per restored domain of a full backup, with the AI settings summary when there is one."""
    parts: list[str] = []
    for domain, result in results.items():
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        if status == "restored":
            parts.append(f"{domain}: {result.get('summary') or 'restored'}")
        elif status == "skipped":
            parts.append(f"{domain}: skipped ({result.get('reason', '')})")
        elif status == "pending":
            parts.append(f"{domain}: pending (requires manual pipeline import)")
        else:
            parts.append(f"{domain}: {status}")
    return "; ".join(parts)


def _export_full_backup_with_options(request: FullBackupRequest):
    """Like GET /api/export/full-backup, with the passphrase that seals the AI settings secrets."""
    _require_login()
    if request.include_secrets and request.ai_settings_passphrase:
        try:
            ai_settings.check_passphrase(request.ai_settings_passphrase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        zip_data = corehub.export_full_corehub_backup(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            include_secrets=request.include_secrets,
            chronos_token=state.chronos_token,
            ai_settings_passphrase=request.ai_settings_passphrase,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export full CoreHub backup")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    return StreamingResponse(
        io.BytesIO(zip_data),
        media_type=ZIP_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="corehub_full_backup_{timestamp}.zip"'},
    )

def _export_ai_settings(request: AiSettingsExportRequest):
    """Download the AI settings bundle of the connected Core Hub (2.3.0.1+)."""
    _require_login()
    corehub.configure_core_hub(state.base_url, use_ssl=state.use_ssl, skip_verify=state.skip_verify)
    try:
        bundle = ai_settings.export_ai_settings(
            state.token,
            include_secrets=request.include_secrets,
            passphrase=request.passphrase,
        )
    except ai_settings.AiSettingsUnsupported as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ai_settings.AiSettingsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export AI settings")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    return Response(
        content=json.dumps(bundle, indent=2, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="ai_settings_{timestamp}.json"'},
    )

async def _import_ai_settings(
    file: UploadFile = File(...),
    passphrase: Optional[str] = Form(None),
    dry_run: bool = Form(False),
) -> ApiMessage:
    """Restore an AI settings bundle; dry_run only reports what would change."""
    _require_login()
    try:
        bundle = json.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="The file is not an AI settings bundle (JSON)") from exc

    corehub.configure_core_hub(state.base_url, use_ssl=state.use_ssl, skip_verify=state.skip_verify)
    try:
        result = corehub.restore_ai_settings(
            token=state.token,
            bundle=bundle,
            passphrase=passphrase or None,
            dry_run=dry_run,
        )
    except (ai_settings.AiSettingsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to import AI settings")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.get("status") == "skipped":
        raise HTTPException(status_code=409, detail=result.get("reason"))
    return ApiMessage(message=result.get("summary"), logs=result.get("warnings") or None)


def _register_ai_settings_routes(app: FastAPI) -> None:
    """The AI settings backup routes, and the full backup with the passphrase that seals their secrets."""
    app.add_api_route(
        "/api/export/full-backup", _export_full_backup_with_options, methods=["POST"], responses=_ERROR_RESPONSES
    )
    app.add_api_route("/api/export/ai-settings", _export_ai_settings, methods=["POST"], responses=_ERROR_RESPONSES)
    app.add_api_route("/api/import/ai-settings", _import_ai_settings, methods=["POST"], responses=_ERROR_RESPONSES)


def _fetch_automator_changelog(version: str) -> Any:
    """Fetch the changelog of an Automator version from the changelog service."""
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


def _register_page_routes(app: FastAPI, asset_rewrites: Dict[str, str]) -> None:
    """The UI page, health, version, state and changelog routes."""

    @app.get("/", responses=_UI_RESPONSES)
    async def root() -> Response:
        index_path = _static_dir() / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=500, detail="UI assets missing")

        content = index_path.read_text(encoding="utf-8")
        for original, hashed in asset_rewrites.items():
            content = content.replace(original, hashed)

        return Response(content=content, media_type="text/html")

    @app.get("/api/healthz")
    async def healthcheck() -> dict:
        return {"status": "ok"}

    @app.get("/api/version")
    async def version() -> VersionResponse:
        try:
            version = get_version()
        except Exception as exc:
            logger.exception("Failed to get version: %s", exc)
            version = "unknown"
        return VersionResponse(version=version)

    @app.get("/api/state")
    async def get_state() -> StateResponse:
        snapshot = state.snapshot()
        return StateResponse(**snapshot)

    @app.get("/api/changelog/automator/{version}", responses=_CHANGELOG_RESPONSES)
    async def get_automator_changelog(version: str):
        return _fetch_automator_changelog(version)


def _login(payload: LoginRequest) -> ApiMessage:
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


def _is_entity_yaml(lower: str) -> bool:
    """Tell an entity-creation YAML of an uploaded ZIP (not agents-config or global-config)."""
    return lower.endswith(YAML_EXTENSIONS) and "agents-config" not in lower and "global-config" not in lower


def _extract_upload_members(contents: bytes, extract_dir: Path) -> Optional[Path]:
    """Extract the YAML and UDF source files of an uploaded ZIP; return the primary YAML, if any."""
    yaml_path: Optional[Path] = None
    with zipfile.ZipFile(io.BytesIO(contents)) as zf:
        for member in zf.namelist():
            if _is_macos_metadata(member):
                continue
            # Extract YAML files (skip agents-config and global-config
            # which are not entity-creation YAMLs)
            if _is_entity_yaml(member.lower()):
                dest = extract_dir / Path(member).name
                _copy_zip_member(zf, member, dest)
                # Prefer backup_*.yaml as the primary YAML
                if yaml_path is None or dest.name.startswith("backup_"):
                    yaml_path = dest
            # Extract UDF source files (under udf-* paths)
            elif _is_udf_source(member):
                _extract_udf_source(zf, member, extract_dir)
    return yaml_path


def _store_uploaded_zip(contents: bytes) -> Path:
    """Extract an uploaded ZIP into a new session directory and return its primary YAML."""
    # Extract YAML and UDF source files from the ZIP into a single
    # self-contained, app-owned session directory (NOT the OS temp dir)
    # so that both the YAML and its UDF source files live together and
    # survive until the entity-creation run reads them.
    extract_dir = new_session_dir("upload")
    try:
        yaml_path = _extract_upload_members(contents, extract_dir)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to extract ZIP: {exc}") from exc

    if yaml_path is None:
        raise HTTPException(status_code=400, detail="ZIP does not contain a YAML configuration file")
    # Set UDF_PATH so create_all_entities.main can find UDF source files
    create_user_defined_functions.UDF_PATH = str(extract_dir)
    return yaml_path


def _store_uploaded_yaml(filename: str, contents: bytes) -> Path:
    """Write an uploaded YAML into a new session directory and return its path."""
    # Plain YAML upload: write it into its own session directory so any
    # UDF files the user drops alongside it are found by the same lookup.
    session_dir = new_session_dir("upload")
    suffix = Path(filename).suffix or YAML_SUFFIX
    temp_path = _write_session_file(session_dir, suffix, contents)
    # Point UDF lookups at this session directory too, so a YAML-only
    # upload still resolves UDF files placed next to the YAML.
    create_user_defined_functions.UDF_PATH = str(session_dir)
    return temp_path


async def upload_yaml(file: UploadFile = File(...)) -> UploadResponse:
    if not file.filename.lower().endswith(YAML_EXTENSIONS + (ZIP_SUFFIX,)):
        raise HTTPException(status_code=400, detail="Only YAML or ZIP files are supported")

    # Best-effort prune of stale sessions before creating a new one.
    prune_workspace_sessions()

    contents = await _read_nonempty_upload(file)

    if file.filename.lower().endswith(ZIP_SUFFIX):
        temp_path = _store_uploaded_zip(contents)
    else:
        temp_path = _store_uploaded_yaml(file.filename, contents)

    file_id = state.register_upload(temp_path, file.filename)
    return UploadResponse(fileId=file_id, filename=file.filename)


def _current_run(include_logs: bool = False):
    snapshot = state.current_run_snapshot()
    if not snapshot:
        return {"run": None}
    response = RunSnapshot(**snapshot)
    if include_logs:
        logs = state.get_logs(response.id)
        response.logs = logs or []
    return response


def _register_session_routes(app: FastAPI) -> None:
    """The login, logout, YAML upload and current run routes."""

    @app.post("/api/login", responses=_LOGIN_RESPONSES)
    async def login(payload: LoginRequest) -> ApiMessage:
        return _login(payload)

    @app.post("/api/logout")
    async def logout() -> ApiMessage:
        state.clear_auth()
        return ApiMessage(message="Logged out")

    app.add_api_route("/api/upload", upload_yaml, methods=["POST"], responses=_UPLOAD_RESPONSES)

    @app.get("/api/run/current")
    async def current_run(include_logs: bool = False):
        return _current_run(include_logs)


def _list_pipelines() -> PipelinesResponse:
    _require_session()

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


def _bulk_list_schemas(pipeline_id: str) -> BulkSchemasResponse:
    _require_session()

    try:
        schemas = corehub.list_source_schemas(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to list source schemas for pipeline %s", pipeline_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    source_type, target_type = corehub.infer_agent_schema_types(
        token=state.token,
        base_url=state.base_url,
        pipeline_id=pipeline_id,
        use_ssl=state.use_ssl,
        skip_verify=state.skip_verify,
    )

    return BulkSchemasResponse(schemas=schemas, source_type=source_type, target_type=target_type)


def _bulk_list_tables(pipeline_id: str, schema: str) -> BulkTablesResponse:
    _require_session()

    try:
        tables = corehub.list_source_tables(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            schema=schema,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to list source tables for pipeline %s schema %s", pipeline_id, schema)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return BulkTablesResponse(tables=tables)


def _schema_types_from_pipeline_yaml(pipeline_id: str, source_type: str, target_type: str) -> tuple[str, str]:
    """Fill the empty source/target type from the pipeline's YAML configuration, when one is known."""
    try:
        # Try to get the pipeline's YAML configuration
        yaml_path = state.get_pipeline_yaml_path(pipeline_id)
        if yaml_path and yaml_path.exists():
            from commons import extract_schema_types_from_yaml
            src_hint, tgt_hint = extract_schema_types_from_yaml(str(yaml_path))
            if not source_type and src_hint:
                source_type = src_hint
                logger.info(f"Extracted source_type from YAML: {source_type}")
            if not target_type and tgt_hint:
                target_type = tgt_hint
                logger.info(f"Extracted target_type from YAML: {target_type}")
    except Exception as e:
        logger.warning(f"Could not extract types from YAML: {e}")
    return source_type, target_type


def _bulk_schema_types(request: BulkCreateRequest) -> tuple[str, str]:
    """Source and target type of a bulk creation: the request's, else the YAML's, else SQL."""
    # If source_type or target_type are empty strings, try to extract from pipeline's YAML
    source_type = request.source_type
    target_type = request.target_type

    if not source_type or not target_type:
        logger.info(f"source_type or target_type empty (source='{source_type}', target='{target_type}'), attempting to extract from pipeline YAML")
        source_type, target_type = _schema_types_from_pipeline_yaml(request.pipeline_id, source_type, target_type)

    # Final fallback to SQL if still empty
    if not source_type:
        source_type = "SQL"
        logger.info(f"Using fallback source_type: {source_type}")
    if not target_type:
        target_type = "SQL"
        logger.info(f"Using fallback target_type: {target_type}")
    return source_type, target_type


def _bulk_create_entities(request: BulkCreateRequest) -> ApiMessage:
    _require_session()

    prefs = state.preferences()
    enable_scheduling = request.enable_scheduling
    if enable_scheduling is None:
        enable_scheduling = prefs["enableScheduling"]
    create_tables = request.create_tables
    if create_tables is None:
        create_tables = prefs["createTables"]

    source_type, target_type = _bulk_schema_types(request)

    # Disable table creation for NoSQL targets (same logic as duplicate pipeline)
    effective_create_tables = bool(create_tables)
    if target_type and target_type.upper() == 'NOSQL':
        logger.info("Target is NoSQL - disabling table creation")
        effective_create_tables = False

    try:
        result = corehub.run_create_entities_for_tables(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=request.pipeline_id,
            source_schema=request.source_schema,
            target_schema=request.target_schema,
            source_type=source_type,
            target_type=target_type,
            table_names=request.table_names,
            skip_errors=request.skip_errors,
            chunk_size=request.chunk_size,
            enable_scheduling=bool(enable_scheduling),
            create_tables=effective_create_tables,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            chronos_token=state.chronos_token,
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
    return ApiMessage(success=ok, message=msg, logs=logs)


def _template_schema_types(pipeline_id: str) -> tuple[Any, Any]:
    """Default source/target types of a bulk template, inferred from the pipeline agents."""
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
    return source_type, target_type


def _unique_table_names(table_names: list[str]) -> list[str]:
    """Deduplicate tables while preserving selection order."""
    seen = set()
    unique_tables: list[str] = []
    for raw in table_names:
        name = str(raw)
        if name in seen:
            continue
        seen.add(name)
        unique_tables.append(name)
    return unique_tables


def _template_table_entry(pipeline_id: str, source_schema: str, name: str) -> dict[str, Any]:
    """Template entry of one table: its primary keys and, when discovered, its columns."""
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
    return entry


def _template_yaml(request: BulkTemplateRequest, table_names: list[str]) -> str:
    """Table-list-template YAML of the selected tables of one source schema."""
    pipeline_id = request.pipeline_id
    source_schema = request.source_schema
    source_type, target_type = _template_schema_types(pipeline_id)

    unique_tables = _unique_table_names(table_names)

    custom_cfg: dict[str, Any] = {}
    for name in unique_tables:
        custom_cfg[name] = _template_table_entry(pipeline_id, source_schema, name)

    schema_cfg: dict[str, Any] = {
        "target": request.target_schema,
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

    return yaml.safe_dump(
        yaml_doc,
        sort_keys=False,
        allow_unicode=True,
    )


def _bulk_export_template(request: BulkTemplateRequest):
    _require_session()

    pipeline_id = request.pipeline_id
    source_schema = request.source_schema
    target_schema = request.target_schema
    table_names = [name for name in request.table_names or [] if name]

    if not pipeline_id:
        raise HTTPException(status_code=400, detail="pipelineId is required")
    if not source_schema:
        raise HTTPException(status_code=400, detail="sourceSchema is required")
    if not target_schema:
        raise HTTPException(status_code=400, detail="targetSchema is required")
    if not table_names:
        raise HTTPException(status_code=400, detail="tableNames must contain at least one table")

    yaml_text = _template_yaml(request, table_names)

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    filename = f"template_{pipeline_id}_{source_schema}_{timestamp}.yaml"
    return _attachment(yaml_text.encode("utf-8"), "application/x-yaml", filename)


def _register_bulk_routes(app: FastAPI) -> None:
    """The pipeline list and the bulk entity creation routes."""

    @app.get("/api/pipelines", responses=_COREHUB_CALL_RESPONSES)
    async def list_pipelines() -> PipelinesResponse:
        return _list_pipelines()

    @app.get("/api/bulk/schemas", responses=_COREHUB_CALL_RESPONSES)
    async def bulk_list_schemas(pipeline_id: Annotated[str, Query(alias="pipelineId")]) -> BulkSchemasResponse:
        return _bulk_list_schemas(pipeline_id)

    @app.get("/api/bulk/tables", responses=_COREHUB_CALL_RESPONSES)
    async def bulk_list_tables(
        pipeline_id: Annotated[str, Query(alias="pipelineId")], schema: str
    ) -> BulkTablesResponse:
        return _bulk_list_tables(pipeline_id, schema)

    @app.post("/api/bulk/create", responses=_COREHUB_CALL_RESPONSES)
    async def bulk_create_entities(request: BulkCreateRequest) -> ApiMessage:
        return _bulk_create_entities(request)

    @app.post("/api/bulk/template", responses=_TEMPLATE_RESPONSES)
    async def bulk_export_template(request: BulkTemplateRequest):
        """Generate an on-the-fly table-list-style YAML for the selected tables.

        This does *not* export an existing pipeline configuration. Instead it
        produces a minimal table-list-template.yaml-shaped document containing
        only the chosen source schema and tables, so it can be refined and
        later used with the CLI bootstrapper.
        """
        return _bulk_export_template(request)


def _export_pipeline(pipeline_id: str, include_secrets: bool = False):
    _require_session()

    try:
        payload, media_type, filename = corehub.export_pipeline_entities_package(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            include_secrets=include_secrets,
            chronos_token=state.chronos_token,
        )
    except PipelineInMaintenanceError as exc:
        logger.info("Pipeline %s skipped from backup: %s", pipeline_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export pipeline %s", pipeline_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _attachment(payload, media_type, filename)


def _pipeline_backup_name(pipeline_id: str) -> str:
    """File-safe name of a pipeline for its backup file name; the pipeline ID when it has none."""
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
    return safe_name


def _export_pipeline_full_backup(pipeline_id: str, include_secrets: bool = False):
    _require_session()

    try:
        zip_data = corehub.export_pipeline_full_backup(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            include_secrets=include_secrets,
            chronos_token=state.chronos_token,
        )
    except PipelineInMaintenanceError as exc:
        logger.info("Pipeline %s skipped from full backup: %s", pipeline_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export full backup for pipeline %s", pipeline_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Derive a human-friendly filename aligned with the internal YAML name:
    # backup_<safePipelineName>_<pipelineId>.zip
    safe_name = _pipeline_backup_name(pipeline_id)

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    filename = f"backup_{safe_name}_{pipeline_id}_{timestamp}.zip"
    return _attachment(zip_data, APPLICATION_ZIP, filename)


def _check_duplicate_request(request: DuplicatePipelineRequest) -> None:
    """Refuse a duplicate request that misses an agent tag or password, or the override schema."""
    if not request.source_agent_tag:
        raise HTTPException(status_code=400, detail="sourceAgentTag is required")
    if not request.target_agent_tag:
        raise HTTPException(status_code=400, detail="targetAgentTag is required")
    if not request.source_agent_password:
        raise HTTPException(status_code=400, detail="sourceAgentPassword is required")
    if not request.target_agent_password:
        raise HTTPException(status_code=400, detail="targetAgentPassword is required")
    if request.override_schemas and not (
        request.override_source_schema or request.override_target_schema
    ):
        raise HTTPException(
            status_code=400,
            detail="At least one of overrideSourceSchema or overrideTargetSchema must be provided when overrideSchemas is enabled",
        )


def _duplicate_pipeline(pipeline_id: str, request: DuplicatePipelineRequest):
    _require_session()
    _check_duplicate_request(request)

    try:
        state.begin_duplicate()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    duplicate_started = True

    logger.info("Duplicate request: clone_entities=%s", request.clone_entities)

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
            source_host_override=request.source_host_override,
            target_host_override=request.target_host_override,
            source_port_override=request.source_port_override,
            target_port_override=request.target_port_override,
            override_schemas=request.override_schemas,
            override_source_schema=request.override_source_schema,
            override_target_schema=request.override_target_schema,
            cancel_checker=state.is_duplicate_cancelled,
            chronos_token=state.chronos_token,
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


def _cancel_duplicate() -> ApiMessage:
    _require_session()

    requested = state.request_duplicate_cancel()
    if not requested:
        return ApiMessage(success=False, message="No duplicate pipeline is currently running")
    return ApiMessage(success=True, message="Duplicate cancellation requested")


def _get_corehub_overview() -> dict:
    _require_session()

    try:
        summary = corehub.get_environment_summary(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            chronos_token=state.chronos_token,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to gather CoreHub overview")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    prefs = state.preferences()
    summary["environment"] = {
        "baseUrl": state.base_url,
        "useSsl": state.use_ssl,
        "skipVerify": state.skip_verify,
        "enableScheduling": prefs.get("enableScheduling"),
        "createTables": prefs.get("createTables"),
    }

    state.set_corehub_overview(summary)
    return summary


def _get_pipeline_agents(pipeline_id: str) -> PipelineAgentsResponse:
    _require_session()

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


def _register_pipeline_routes(app: FastAPI) -> None:
    """The single pipeline export, duplicate, Core Hub overview and pipeline agents routes."""

    @app.get("/api/export/pipeline/{pipeline_id}", responses=_PIPELINE_EXPORT_RESPONSES)
    async def export_pipeline(pipeline_id: str, include_secrets: bool = False):
        """Export entity metadata for a single pipeline.

        Returns plain YAML when no entity-level UDFs are referenced. When UDFs
        are present, returns a ZIP containing the YAML plus ``udf-<agentId>/``
        source folders (same layout full backup / import already understand)
        so entity-only export round-trips mapping-function sources (GSSD-1355).

        Duplicate source tables are handled by creating unique keys (e.g., table@@2, table@@3)
        within the same YAML file to avoid key conflicts.
        """
        return _export_pipeline(pipeline_id, include_secrets)

    @app.get("/api/export/pipeline/{pipeline_id}/full", responses=_PIPELINE_EXPORT_RESPONSES)
    async def export_pipeline_full_backup(pipeline_id: str, include_secrets: bool = False):
        """Export a full backup (YAML + agents-config + UDFs) for a single pipeline."""
        return _export_pipeline_full_backup(pipeline_id, include_secrets)

    @app.post(
        "/api/duplicate/pipeline/{pipeline_id}",
        response_model=DuplicatePipelineResponse,
        responses=_DUPLICATE_RESPONSES,
    )
    async def duplicate_pipeline(pipeline_id: str, request: DuplicatePipelineRequest):
        """Duplicate a pipeline with new source and target agent configuration.

        This endpoint allows users to clone an existing pipeline with different agents.
        Agents are provisioned via CoreHub API during pipeline creation.
        """
        return _duplicate_pipeline(pipeline_id, request)

    @app.post("/api/duplicate/cancel", responses=_SESSION_RESPONSES)
    async def cancel_duplicate() -> ApiMessage:
        """Request cancellation of an in-flight duplicate pipeline operation."""
        return _cancel_duplicate()

    @app.get("/api/corehub/overview", responses=_COREHUB_CALL_RESPONSES)
    async def get_corehub_overview() -> dict:
        return _get_corehub_overview()

    @app.get("/api/pipeline/{pipeline_id}/agents", responses=_COREHUB_CALL_RESPONSES)
    async def get_pipeline_agents(pipeline_id: str) -> PipelineAgentsResponse:
        """Get pipeline agent details for auto-populating duplicate form."""
        return _get_pipeline_agents(pipeline_id)


@dataclass
class _ArchiveImport:
    """What an Export All import shares across the pipelines of the archive."""

    contents: bytes
    agents_list: list
    meta_by_old_id: Dict[str, Any]
    udf_root: Optional[Path]
    errors: list[str] = field(default_factory=list)
    created_pipelines: list[dict] = field(default_factory=list)


@dataclass
class _PipelineEntities:
    """One imported pipeline whose entities are recreated from its YAML."""

    name: str
    yaml_path: Path
    pipeline_id: Any
    pipeline_name: Any
    source_type: str
    target_type: str
    enable_scheduling: Any
    create_tables: Any


def _member_stem(name: str) -> str:
    """File name of a ZIP member without its folders and its extension."""
    return name.rsplit("/", 1)[-1].rsplit(".", 1)[0]


def _pipeline_yaml_stem(name: str) -> Optional[str]:
    """Stem of a per-pipeline YAML of a backup ZIP; None for any other member."""
    lower = name.lower()
    # Skip macOS resource-fork entries (e.g. __MACOSX/ or ._filename)
    if _is_macos_metadata(name):
        return None
    if not lower.endswith(YAML_EXTENSIONS):
        return None
    if "agents-config" in lower:
        return None

    # Skip global config YAMLs from full CoreHub backup format
    # (these are backed up separately and not pipeline configs)
    stem_no_ext = _member_stem(name)
    if stem_no_ext in GLOBAL_CONFIG_NAMES:
        return None
    return stem_no_ext


def _resolve_pipeline_identity(yaml_text: str, stem_no_ext: str) -> tuple[Optional[str], Optional[str]]:
    """Original pipeline ID and name of a backup YAML.

    Prefer the exportMetadata key embedded in the YAML (current exports); fall
    back to parsing the backup filename for legacy backups.
    """
    old_pipeline_id, embedded_name = _extract_export_metadata_from_yaml_text(yaml_text)
    if not old_pipeline_id:
        old_pipeline_id = _extract_pipeline_id_from_backup_stem(stem_no_ext)
    return old_pipeline_id, embedded_name


def _default_pipeline_name(old_pipeline_id: Optional[str]) -> str:
    return f"Imported pipeline {old_pipeline_id}" if old_pipeline_id else "Imported pipeline"


def _read_agents_config_text(contents: bytes, operation: str, logs: Optional[list[str]] = None) -> str:
    """Text of the agents-config.yaml of a backup ZIP; 400 when there is none or the ZIP is invalid."""
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as zf:
            names = zf.namelist()
            if logs is not None:
                logs.append(f"Archive contains {len(names)} entries.")

            agents_name = next(
                (name for name in names if name.lower().endswith(("agents-config.yaml", "agents-config.yml"))),
                None,
            )
            if not agents_name:
                raise HTTPException(
                    status_code=400,
                    detail="Archive does not contain agents-config.yaml",
                )

            if logs is not None:
                logs.append(f"Found agents config: {agents_name}")
            return zf.read(agents_name).decode("utf-8")
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to process uploaded ZIP during %s", operation)
        raise HTTPException(status_code=400, detail=f"Invalid ZIP archive: {exc}") from exc


def _parse_agents_config(agents_text: str, operation: str) -> tuple[Any, list]:
    """Parsed agents-config.yaml and its agents list; 400 when it cannot be imported."""
    try:
        agents_config = yaml.safe_load(agents_text)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to parse agents-config.yaml during %s", operation)
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
    return agents_config, agents_list


def _extract_import_udf_sources(contents: bytes) -> Optional[Path]:
    """Extract the UDF source files of a backup ZIP into a new session directory and point UDF_PATH at it."""
    # Extract UDF source files (if present) into an app-owned session
    # directory so they can be compiled automatically during import. Both
    # Export All and single-pipeline Full backup ZIPs place UDFs under
    # folders named 'udf-<agentId>' (optionally nested under pipeline_<id>/
    # for single pipeline backups).
    udf_root: Optional[Path] = None
    try:
        udf_root = new_session_dir("import")

        with zipfile.ZipFile(io.BytesIO(contents)) as zf_udf:
            for member in zf_udf.namelist():
                # Skip macOS resource-fork entries; only extract recognized UDF
                # source extensions found under udf-* folders
                if _is_macos_metadata(member) or not _is_udf_source(member):
                    continue
                _extract_udf_source(zf_udf, member, udf_root)

        # Point the UDF helper module to this directory so it can resolve
        # files via UDF_NAME + extension lookups.
        create_user_defined_functions.UDF_PATH = str(udf_root)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(
            "Failed to extract UDF source files from backup; UDF compilation will be skipped: %s",
            exc,
        )
        # Still set UDF_PATH to the (possibly empty) directory so that
        # create_all_entities.main doesn't fall back to the YAML temp dir
        # (where UDF files are never present) and produce confusing errors.
        if udf_root is not None and udf_root.is_dir():
            create_user_defined_functions.UDF_PATH = str(udf_root)
    return udf_root


def _pipeline_meta_by_old_id(agents_config: Any) -> Dict[str, Any]:
    """Original pipeline metadata of agents-config.yaml, by original pipeline ID."""
    pipelines_meta = agents_config.get("pipelines") or []
    meta_by_old_id = {}
    if isinstance(pipelines_meta, list):
        for item in pipelines_meta:
            if not isinstance(item, dict):
                continue
            old_id = item.get("pipelineId")
            if old_id is not None:
                meta_by_old_id[str(old_id)] = item
    return meta_by_old_id


def _global_config_notifications(zf_gc: zipfile.ZipFile) -> Optional[Dict[str, Any]]:
    """Notification settings of the dedicated global-config.yaml of a backup ZIP."""
    for name in zf_gc.namelist():
        if _is_macos_metadata(name):
            continue
        if _member_stem(name) != "global-config":
            continue
        try:
            parsed = yaml.safe_load(zf_gc.read(name).decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(IMPORT_ALL_PARSE_FAILED, name, exc)
    return None


def _embedded_notifications(zf_gc: zipfile.ZipFile) -> Optional[Dict[str, Any]]:
    """The globalNotifications key embedded in a per-pipeline YAML of a backup ZIP."""
    for name in zf_gc.namelist():
        lower = name.lower()
        if not lower.endswith(YAML_EXTENSIONS) or "agents-config" in lower or _is_macos_metadata(name):
            continue
        try:
            parsed = yaml.safe_load(zf_gc.read(name).decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("globalNotifications"), dict):
            return {"globalNotifications": parsed["globalNotifications"]}
    return None


def _find_notification_config(contents: bytes) -> Optional[Dict[str, Any]]:
    """Global notification settings (SMTP) of a backup ZIP, if any.

    Prefer a dedicated global-config.yaml file; otherwise fall back to a
    globalNotifications key embedded in any per-pipeline YAML.
    """
    with zipfile.ZipFile(io.BytesIO(contents)) as zf_gc:
        notification_cfg = _global_config_notifications(zf_gc)

        # Fallback: scan per-pipeline YAMLs for an embedded globalNotifications key
        if notification_cfg is None:
            notification_cfg = _embedded_notifications(zf_gc)
    return notification_cfg


def _find_config_by_stem(contents: bytes, stem: str) -> Any:
    """Parsed content of the first readable backup ZIP member named stem (any folder, any extension)."""
    found = None
    with zipfile.ZipFile(io.BytesIO(contents)) as zf:
        for name in zf.namelist():
            if _member_stem(name) == stem:
                try:
                    found = yaml.safe_load(zf.read(name).decode("utf-8"))
                    break
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning(IMPORT_ALL_PARSE_FAILED, name, exc)
    return found


def _restore_from_archive(
    find: Callable[[], Any],
    restore: Callable[[Any], Dict[str, Any]],
    label: str,
    restored_message: str,
    failure_message: str,
    errors: list[str],
) -> None:
    """Restore one global configuration found in a backup ZIP; a failure is logged, never raised."""
    try:
        config = find()
        if config:
            result = restore(config)
            if result.get("status") == "restored":
                logger.info(restored_message)
            elif result.get("status") == "failed":
                errors.append(f"{label}: {result.get('error')}")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(failure_message, exc)


def _restore_archive_globals(contents: bytes, errors: list[str]) -> None:
    """Restore the global notifications, users and OIDC configuration of a backup ZIP."""
    # Restore global notification settings (SMTP) if present in the backup.
    _restore_from_archive(
        lambda: _find_notification_config(contents),
        lambda notification_cfg: corehub.import_global_notifications(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            notification_cfg=notification_cfg,
        ),
        "global notifications",
        "Restored global notification settings during import-all",
        "Failed to restore global notifications during import-all: %s",
        errors,
    )
    # Restore users config (full CoreHub backup) if present.
    _restore_from_archive(
        lambda: _find_config_by_stem(contents, "users"),
        lambda users_cfg: corehub.import_users(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            users_data=users_cfg,
        ),
        "users",
        "Restored users config during import-all",
        "Failed to restore users during import-all: %s",
        errors,
    )
    # Restore OIDC config (full CoreHub backup) if present.
    _restore_from_archive(
        lambda: _find_config_by_stem(contents, "oidc"),
        lambda oidc_cfg: corehub.import_oidc_config(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            oidc_data=oidc_cfg,
        ),
        "oidc",
        "Restored OIDC config during import-all",
        "Failed to restore OIDC during import-all: %s",
        errors,
    )


def _agent_matches(candidate: Any, a_type: Any, a_tag: Any, a_id: Any) -> bool:
    if not isinstance(candidate, dict):
        return False
    if candidate.get("agentType") != a_type or candidate.get("agentTag") != a_tag:
        return False
    if not a_id:
        return True
    cand_id = candidate.get("agentId") or candidate.get("id")
    return cand_id is not None and str(cand_id) == str(a_id)


def _find_mapped_agent(agents_list: list, a_type: Any, a_tag: Any, a_id: Any) -> Any:
    """The agents-config.yaml agent a per-pipeline agents mapping entry points at."""
    # Prefer matching by agentId when present to avoid merging distinct
    # agents that share the same tag/type. Fall back to tag/type only
    # for backwards compatibility with older exports.
    match = next((a for a in agents_list if _agent_matches(a, a_type, a_tag, a_id)), None)
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
    return match


def _select_mapped_agents(job: _ArchiveImport, name: str, pipeline_agents_meta: list) -> list[dict]:
    """The agents of the per-pipeline agents mapping that agents-config.yaml declares."""
    selected: list[dict] = []
    for want in pipeline_agents_meta:
        if not isinstance(want, dict):
            continue

        a_type = want.get("agentType")
        a_tag = want.get("agentTag")
        a_id = want.get("agentId") or want.get("id")

        if not a_type or not a_tag:
            job.errors.append(
                f"{name}: pipeline agents mapping has an entry without agentType/agentTag; skipping that entry."
            )
            continue

        match = _find_mapped_agent(job.agents_list, a_type, a_tag, a_id)

        if not match:
            job.errors.append(
                f"{name}: agents-config.yaml declares agentType={a_type!r}, agentTag={a_tag!r} for this pipeline "
                "but it is not present in the top-level agents list."
            )
        else:
            selected.append(match)
    return selected


def _dedupe_agents(selected: list[dict]) -> list[dict]:
    """Deduplicate agents by type and tag in case of repeated mappings."""
    seen_local: set[tuple] = set()
    deduped: list[dict] = []
    for a in selected:
        key = (a.get("agentType"), a.get("agentTag"))
        if key in seen_local:
            continue
        seen_local.add(key)
        deduped.append(a)
    return deduped


def _agents_for_pipeline(job: _ArchiveImport, name: str, meta: Any) -> Optional[list]:
    """Agents to bind to one imported pipeline; None when its agents mapping resolves to none."""
    # Select agents for this pipeline: use per-pipeline mapping when present,
    # otherwise fall back to the full agents list from agents-config.yaml.
    pipeline_agents_meta = meta.get("agents") if isinstance(meta, dict) else None
    if not isinstance(pipeline_agents_meta, list) or not pipeline_agents_meta:
        return job.agents_list

    selected = _select_mapped_agents(job, name, pipeline_agents_meta)
    if not selected:
        # If mapping exists but nothing could be resolved, skip this pipeline
        job.errors.append(
            f"{name}: no valid agents could be resolved from the per-pipeline agents mapping; skipping this pipeline."
        )
        return None
    return _dedupe_agents(selected)


def _read_archive_certificate(
    zf_cert: zipfile.ZipFile, name: str, cert_path: str, agent_id: Any, errors: list[str]
) -> Optional[bytes]:
    """Certificate file an agent of a backup ZIP points at; None (and an error) when it is missing."""
    # Try to find the certificate file in the ZIP
    # Support both absolute and relative paths
    cert_filename = cert_path.lstrip("./")
    logger.debug(f"Looking for certificate file: {cert_filename}")
    logger.debug(f"Available files in ZIP: {zf_cert.namelist()}")
    cert_data = None

    # Try exact match first
    if cert_filename in zf_cert.namelist():
        cert_data = zf_cert.read(cert_filename)
        logger.debug(f"Found certificate via exact match: {cert_filename}")
    else:
        # Try with pipeline folder prefix (e.g., pipeline_xxx/certificate.txt)
        # Extract pipeline folder from the YAML name
        pipeline_folder = name.split('/')[0] if '/' in name else None
        cert_data = _find_certificate_near_pipeline(zf_cert, pipeline_folder, cert_filename)

    if not cert_data:
        error_msg = f"{name}: Certificate file '{cert_path}' not found in ZIP for agent {agent_id}"
        logger.error(error_msg)
        logger.error(f"Searched for: {cert_filename}, {pipeline_folder}/{cert_filename if pipeline_folder else 'N/A'}")
        errors.append(error_msg)
        return None
    return cert_data


def _find_certificate_near_pipeline(
    zf_cert: zipfile.ZipFile, pipeline_folder: Optional[str], cert_filename: str
) -> Optional[bytes]:
    """Certificate under the pipeline folder of the ZIP, else the one with the same file name."""
    cert_data = None
    if pipeline_folder:
        prefixed_path = f"{pipeline_folder}/{cert_filename}"
        if prefixed_path in zf_cert.namelist():
            cert_data = zf_cert.read(prefixed_path)
            logger.debug(f"Found certificate with pipeline prefix: {prefixed_path}")

    # Try case-insensitive search as fallback
    if not cert_data:
        cert_data = _find_certificate_by_file_name(zf_cert, cert_filename)
    return cert_data


def _find_certificate_by_file_name(zf_cert: zipfile.ZipFile, cert_filename: str) -> Optional[bytes]:
    """Certificate whose ZIP path ends with cert_filename, ignoring case."""
    for zip_name in zf_cert.namelist():
        # Match by filename only (ignore path)
        if zip_name.lower().endswith(cert_filename.lower()):
            cert_data = zf_cert.read(zip_name)
            logger.debug(f"Found certificate via filename match: {zip_name}")
            return cert_data
    return None


def _send_agent_credentials(agent_cfg: dict, agent_id: Any, new_pipeline_id: Any) -> None:
    """Send the credentials of an agent, which must follow its certificate upload."""
    host_creds = agent_cfg.get("_host_credentials")
    custom_host_creds = agent_cfg.get("_custom_host_credentials")
    if host_creds is not None or custom_host_creds is not None:
        logger.info(
            f"Sending credentials for agent {agent_id} after certificate upload"
        )
        corehub.fetch_core_hub(
            f"/pipelines/{new_pipeline_id}/agents/{agent_id}/config/credentials",
            method="PUT",
            token=state.token,
            body={
                "hostCredentials": host_creds or {},
                "customHostCredentials": custom_host_creds or {},
            },
        )
        logger.info(
            f"Successfully sent credentials for agent {agent_id}"
        )


def _upload_archive_certificate(job: _ArchiveImport, name: str, agent_cfg: dict, new_pipeline_id: Any) -> None:
    """Upload the certificate of one agent from the backup ZIP, then send its credentials."""
    cert_path = agent_cfg.get("_certificate_path")
    cert_type = agent_cfg.get("_certificate_type")
    agent_id = agent_cfg.get("_agent_id")
    # Extract certificate from ZIP
    with zipfile.ZipFile(io.BytesIO(job.contents)) as zf_cert:
        cert_data = _read_archive_certificate(zf_cert, name, cert_path, agent_id, job.errors)
        if cert_data is None:
            return

        # Determine certificate extension
        cert_ext = Path(cert_path.lstrip("./")).suffix.lstrip('.').lower() or 'crt'
        logger.debug(
            f"Certificate extracted: {len(cert_data)} bytes, extension: {cert_ext}"
        )

        # Upload certificate to CoreHub
        corehub.upload_agent_certificate(
            token=state.token,
            pipeline_id=new_pipeline_id,
            agent_id=agent_id,
            certificate_type=cert_type,
            certificate_data=cert_data,
            certificate_ext=cert_ext,
        )
        logger.info(
            f"Successfully uploaded {cert_type} certificate ({len(cert_data)} bytes) "
            f"for agent {agent_id} in pipeline {new_pipeline_id}"
        )

        # Now send credentials AFTER certificate upload
        _send_agent_credentials(agent_cfg, agent_id, new_pipeline_id)


def _upload_pipeline_certificates(job: _ArchiveImport, name: str, agents_for_pipeline: list, new_pipeline_id: Any) -> None:
    """Upload the certificates the agents of one imported pipeline point at."""
    logger.debug(f"Checking {len(agents_for_pipeline)} agents for certificate uploads")
    for agent_cfg in agents_for_pipeline:
        cert_path = agent_cfg.get("_certificate_path")
        cert_type = agent_cfg.get("_certificate_type")
        agent_id = agent_cfg.get("_agent_id")

        if cert_path and cert_type and agent_id:
            logger.info(
                f"Processing certificate upload for agent {agent_id}: "
                f"path={cert_path}, type={cert_type}"
            )
            try:
                _upload_archive_certificate(job, name, agent_cfg, new_pipeline_id)
            except Exception as cert_exc:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to upload certificate for agent %s in pipeline %s",
                    agent_id,
                    new_pipeline_id,
                )
                job.errors.append(
                    f"{name}: Failed to upload certificate for agent {agent_id}: {cert_exc}"
                )


def _materialize_import_yaml(job: _ArchiveImport, name: str, yaml_text: str) -> Optional[Path]:
    """Write a pipeline YAML next to the extracted UDF sources; None (and an error) when it fails."""
    # Write YAML for schema extraction and entity creation into
    # the SAME session directory where this import's UDF files
    # were extracted (udf_root), so YAML and UDFs share one root
    # and UDF lookups resolve without spanning sibling folders.
    try:
        restore_dir = job.udf_root if job.udf_root is not None else new_session_dir("import")
        restore_dir.mkdir(parents=True, exist_ok=True)
        return _write_session_file(restore_dir, YAML_SUFFIX, yaml_text.encode("utf-8"))
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to materialize YAML %s to disk", name)
        job.errors.append(f"{name}: failed to materialize YAML to disk: {exc}")
        return None


def _compile_imported_udfs(job: _ArchiveImport, entities: _PipelineEntities, src_schema: Any, tgt_schema: Any) -> bool:
    """Compile and register the UDFs a schema pair references; False when that fails."""
    try:
        create_user_defined_functions.main(
            entities.pipeline_id,
            src_schema,
            tgt_schema,
            entities.source_type,
            entities.target_type,
            str(entities.yaml_path),
            state.token,
            True,  # skip_errors
            50,  # chunk_size
        )
    except Exception as exc_udf:  # pylint: disable=broad-except
        logger.exception(
            "Failed to process UDFs for pipeline %s (%s) schema %s->%s from %s",
            entities.pipeline_name,
            entities.pipeline_id,
            src_schema,
            tgt_schema,
            entities.name,
        )
        job.errors.append(
            f"{entities.name}: UDF compilation error for {src_schema}->{tgt_schema}: {exc_udf}"
        )
        return False
    return True


def _create_schema_pair_entities(job: _ArchiveImport, entities: _PipelineEntities, src_schema: Any, tgt_schema: Any) -> bool:
    """Create the entities of one schema pair of an imported pipeline; False when anything failed."""
    success = True
    name = entities.name
    try:
        result_run = corehub.run_create_entities(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=entities.pipeline_id,
            source_schema=src_schema,
            target_schema=tgt_schema,
            source_type=entities.source_type,
            target_type=entities.target_type,
            yaml_file=str(entities.yaml_path),
            skip_errors=True,
            chunk_size=50,
            enable_scheduling=entities.enable_scheduling,
            create_tables=entities.create_tables,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            log_callback=None,
            chronos_token=state.chronos_token,
        )
        if not result_run.get("success"):
            success = False
            if result_run.get("error"):
                job.errors.append(
                    f"{name}: entity creation error for {src_schema}->{tgt_schema}: {result_run['error']}"
                )

        # After entities are created for this schema pair, automatically
        # compile and register any UDFs referenced in the YAML using the
        # exported source files from the backup.
        if job.udf_root is not None and not _compile_imported_udfs(job, entities, src_schema, tgt_schema):
            success = False
    except Exception as exc:  # pylint: disable=broad-except
        success = False
        logger.exception(
            "Failed to create entities for pipeline %s (%s) from %s", entities.pipeline_name, entities.pipeline_id, name
        )
        job.errors.append(
            f"{name}: exception during entity creation for {src_schema}->{tgt_schema}: {exc}"
        )
    return success


def _mark_configuration_completed(new_pipeline_id: Any, new_pipeline_name: Any) -> None:
    """Mark pipeline as configuration completed to remove from draft status."""
    try:
        corehub.fetch_core_hub(
            f"/pipelines/{new_pipeline_id}",
            method="PUT",
            token=state.token,
            body={"configurationCompleted": True, "name": new_pipeline_name},
        )
        logger.info("Marked pipeline %s (%s) as configuration completed", new_pipeline_name, new_pipeline_id)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to mark pipeline %s as configuration completed: %s", new_pipeline_id, exc)


def _create_imported_entities(job: _ArchiveImport, name: str, yaml_path: Path, new_pipeline_id: Any, new_pipeline_name: Any) -> None:
    """Recreate the entities (and UDFs) of one imported pipeline from its YAML."""
    # Determine schemas and optional type hints from YAML
    schema_pairs = extract_all_schemas_from_yaml(str(yaml_path))
    if not schema_pairs:
        job.errors.append(f"{name}: no schemas found in YAML; skipping entity creation")
        return

    src_type_hint, tgt_type_hint = extract_schema_types_from_yaml(str(yaml_path))
    prefs = state.preferences()
    entities = _PipelineEntities(
        name=name,
        yaml_path=yaml_path,
        pipeline_id=new_pipeline_id,
        pipeline_name=new_pipeline_name,
        source_type=src_type_hint or "SQL",
        target_type=tgt_type_hint or "SQL",
        enable_scheduling=prefs["enableScheduling"],
        create_tables=prefs["createTables"],
    )

    overall_success = True
    for src_schema, tgt_schema in schema_pairs:
        if not _create_schema_pair_entities(job, entities, src_schema, tgt_schema):
            overall_success = False

    if not overall_success:
        logger.warning(
            "Import-all: entity creation had errors for pipeline %s (%s) from %s",
            new_pipeline_name,
            new_pipeline_id,
            name,
        )
    else:
        _mark_configuration_completed(new_pipeline_id, new_pipeline_name)


def _import_archive_pipeline(job: _ArchiveImport, name: str, stem_no_ext: str, yaml_text: str) -> None:
    """Recreate one pipeline of a backup ZIP: pipeline, agents, certificates, entities and UDFs."""
    # Resolve original pipeline identity.
    old_pipeline_id, embedded_name = _resolve_pipeline_identity(yaml_text, stem_no_ext)

    meta = job.meta_by_old_id.get(str(old_pipeline_id)) if old_pipeline_id is not None else None
    base_name = meta.get("pipelineName") if isinstance(meta, dict) else None
    base_name = base_name or embedded_name or _default_pipeline_name(old_pipeline_id)

    agents_for_pipeline = _agents_for_pipeline(job, name, meta)
    if agents_for_pipeline is None:
        return

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
        job.errors.append(f"{name}: failed to create pipeline and bind agents: {exc}")
        return

    new_pipeline_id = result.get("pipelineId")
    new_pipeline_name = result.get("pipelineName")
    job.created_pipelines.append(result)

    # Upload certificates for agents if specified
    _upload_pipeline_certificates(job, name, agents_for_pipeline, new_pipeline_id)

    yaml_path = _materialize_import_yaml(job, name, yaml_text)
    if yaml_path is None:
        return

    _create_imported_entities(job, name, yaml_path, new_pipeline_id, new_pipeline_name)


def _import_archive_pipelines(job: _ArchiveImport) -> None:
    """Second pass: process each per-pipeline YAML and recreate pipeline + entities."""
    with zipfile.ZipFile(io.BytesIO(job.contents)) as zf:
        for name in zf.namelist():
            stem_no_ext = _pipeline_yaml_stem(name)
            if stem_no_ext is None:
                continue

            try:
                yaml_text = zf.read(name).decode("utf-8")
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to read YAML %s from archive", name)
                job.errors.append(f"{name}: failed to read from archive: {exc}")
                continue

            _import_archive_pipeline(job, name, stem_no_ext, yaml_text)


def _import_summary(created_pipelines: list[dict], errors: list[str]) -> str:
    """Human-friendly summary message of an Export All import."""
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
    return base_msg


def _import_archive(contents: bytes) -> ApiMessage:
    """Recreate the pipelines, agents, entities, UDFs and global configs of an Export All ZIP."""
    udf_root = _extract_import_udf_sources(contents)

    # First pass: find and parse agents-config.yaml for shared agents + metadata
    agents_text = _read_agents_config_text(contents, "import-all")
    agents_config, agents_list = _parse_agents_config(agents_text, "import-all")

    # Build lookup for original pipeline metadata, if present
    job = _ArchiveImport(
        contents=contents,
        agents_list=agents_list,
        meta_by_old_id=_pipeline_meta_by_old_id(agents_config),
        udf_root=udf_root,
    )

    _restore_archive_globals(contents, job.errors)

    try:
        _import_archive_pipelines(job)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error during import-all processing")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    errors = job.errors
    return ApiMessage(message=_import_summary(job.created_pipelines, errors), logs=errors if errors else None)


async def import_all(
    file: UploadFile = File(...)
) -> ApiMessage:
    """Validate a full backup ZIP (pipelines + agents-config).

    The ZIP is expected to come from the Export All feature and contain
    at least an agents-config.yaml file. Agents are provisioned via CoreHub API.
    """

    _require_session()

    contents = await _read_nonempty_upload(file)

    prune_workspace_sessions()
    return _import_archive(contents)


def _check_agent_definitions(agents_list: list, logs: list[str], errors: list[str]) -> None:
    """Validate agent definitions in agents-config.yaml."""
    for cfg in agents_list:
        if not isinstance(cfg, dict):
            continue
        a_type = cfg.get("agentType")
        a_tag = cfg.get("agentTag")
        if not a_type or not a_tag:
            errors.append("One agent definition is missing agentType/agentTag.")
            continue
        logs.append(f"Agent definition found: {a_type}/{a_tag}")


def _existing_pipeline_names(errors: list[str]) -> set[str]:
    """Names of the pipelines of the Core Hub, to estimate the names an import would create."""
    existing_names: set[str] = set()
    try:
        existing = corehub.fetch_core_hub("/pipelines", token=state.token)
        if isinstance(existing, list):
            for item in existing:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str) and name:
                    existing_names.add(name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to fetch existing pipelines during validate-all")
        errors.append(f"Failed to fetch existing pipelines: {exc}")
    return existing_names


def _materialize_validation_yaml(name: str, yaml_text: str, errors: list[str]) -> Optional[Path]:
    """Write a pipeline YAML under the validate workspace; None (and an error) when it fails."""
    try:
        temp_dir = get_workspace_root() / "validate"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return _write_session_file(temp_dir, YAML_SUFFIX, yaml_text.encode("utf-8"))
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to materialize YAML %s to disk during validate-all", name)
        errors.append(f"{name}: failed to materialize YAML to disk: {exc}")
        return None


def _unique_pipeline_name(base_name: str, reserved_names: set[str]) -> str:
    """base_name, or its first '(restored N)' variant that is not taken yet."""
    if base_name not in reserved_names:
        return base_name
    idx = 1
    while True:
        suffix = " (restored)" if idx == 1 else f" (restored {idx})"
        candidate = f"{base_name}{suffix}"
        if candidate not in reserved_names:
            return candidate
        idx += 1


def _validate_pipeline_yaml(
    name: str, stem_no_ext: str, yaml_text: str, yaml_path: Path, reserved_names: set[str], logs: list[str], errors: list[str]
) -> None:
    """Report the pipeline one backup YAML would create and the schema pairs it defines."""
    # Estimate the pipeline name that would be used on import.
    old_pipeline_id, embedded_name = _resolve_pipeline_identity(yaml_text, stem_no_ext)

    base_name = embedded_name or _default_pipeline_name(old_pipeline_id)
    pipeline_name = _unique_pipeline_name(base_name, reserved_names)
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


def _validate_archive_pipelines(contents: bytes, reserved_names: set[str], logs: list[str], errors: list[str]) -> None:
    """Validate each per-pipeline YAML can be parsed and has schemas."""
    with zipfile.ZipFile(io.BytesIO(contents)) as zf:
        for name in zf.namelist():
            stem_no_ext = _pipeline_yaml_stem(name)
            if stem_no_ext is None:
                continue

            logs.append(f"Validating pipeline definition: {name}")

            try:
                yaml_text = zf.read(name).decode("utf-8")
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to read YAML %s from archive during validate-all", name)
                errors.append(f"{name}: failed to read from archive: {exc}")
                continue

            yaml_path = _materialize_validation_yaml(name, yaml_text, errors)
            if yaml_path is None:
                continue

            _validate_pipeline_yaml(name, stem_no_ext, yaml_text, yaml_path, reserved_names, logs, errors)


def _validate_archive(contents: bytes) -> ApiMessage:
    """Dry-run the import of an Export All ZIP and report what it would do."""
    logs: list[str] = []
    errors: list[str] = []

    # Locate and parse agents-config.yaml
    agents_text = _read_agents_config_text(contents, "validate-all", logs)
    _, agents_list = _parse_agents_config(agents_text, "validate-all")

    logs.append(f"agents-config.yaml defines {len(agents_list)} agent configuration(s).")

    _check_agent_definitions(agents_list, logs, errors)

    # Fetch existing pipelines to estimate final pipeline names that would be created
    reserved_names = _existing_pipeline_names(errors)

    try:
        _validate_archive_pipelines(contents, reserved_names, logs, errors)
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


async def validate_all(
    file: UploadFile = File(...)
) -> ApiMessage:
    """Dry-run validation for an Export All ZIP archive.

    This endpoint parses the archive, checks that agents-config.yaml is
    present and well formed, verifies that no masked passwords are
    present, and ensures that each per-pipeline YAML can be read and has
    at least one schema pair defined. It does **not** create pipelines,
    bind agents or create entities.
    """

    _require_session()

    contents = await _read_nonempty_upload(file)
    return _validate_archive(contents)


async def import_full_backup(
    file: UploadFile = File(...),
    ai_settings_passphrase: Optional[str] = Form(None),
) -> ApiMessage:
    """Restore a complete CoreHub backup (pipelines + global configs + schedules).

    Accepts the ZIP produced by /api/export/full-backup and restores each
    domain independently: global configs, SMTP, webhooks, thresholds,
    schedules, and pipelines (agents + entities + UDFs).
    """

    _require_session()

    contents = await _read_nonempty_upload(file)

    try:
        results = corehub.import_full_corehub_backup(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            zip_bytes=contents,
            chronos_token=state.chronos_token,
            ai_settings_passphrase=ai_settings_passphrase,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to import full CoreHub backup")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return ApiMessage(message="Full backup restore completed. " + _summarize_restore(results))


def _parse_uploaded_yaml(contents: bytes) -> Any:
    """Parse an uploaded YAML backup; 400 when it is not valid YAML."""
    try:
        return yaml.safe_load(contents.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc


async def import_users(file: UploadFile = File(...)) -> ApiMessage:
    """Restore user management configuration from a YAML backup."""
    _require_session()

    contents = await _read_nonempty_upload(file)
    users_data = _parse_uploaded_yaml(contents)

    try:
        result = corehub.import_users(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            users_data=users_data,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to import users")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    status = result.get("status", "unknown")
    return ApiMessage(message=f"Users import {status}")


async def import_oidc(file: UploadFile = File(...)) -> ApiMessage:
    """Restore OIDC configuration from a YAML backup."""
    _require_session()

    contents = await _read_nonempty_upload(file)
    oidc_data = _parse_uploaded_yaml(contents)

    try:
        result = corehub.import_oidc_config(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            oidc_data=oidc_data,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to import OIDC config")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    summary_parts: list[str] = []
    for key, res in result.items():
        status = res.get("status", "unknown")
        summary_parts.append(f"{key}: {status}")
    return ApiMessage(message="OIDC import completed. " + "; ".join(summary_parts))


def _register_import_routes(app: FastAPI) -> None:
    """The Export All import and validation routes and the backup restore routes."""
    app.add_api_route("/api/import/all", import_all, methods=["POST"], responses=_ARCHIVE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/validate-all", validate_all, methods=["POST"], responses=_ARCHIVE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/full-backup", import_full_backup, methods=["POST"], responses=_FILE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/users", import_users, methods=["POST"], responses=_FILE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/oidc", import_oidc, methods=["POST"], responses=_FILE_IMPORT_RESPONSES)


def _export_all_pipelines(include_secrets: bool = False):
    _require_session()

    try:
        zip_data = corehub.export_all_pipelines_yaml(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            include_secrets=include_secrets,
            chronos_token=state.chronos_token,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export all pipelines")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    filename = f"pipeline_backups_{timestamp}.zip"
    return _attachment(zip_data, APPLICATION_ZIP, filename)


def _export_corehub_config(export: Callable[..., Any], failure_message: str, with_chronos_token: bool = False):
    """Answer one global configuration of the Core Hub, read by export."""
    _require_session()

    try:
        extra = {"chronos_token": state.chronos_token} if with_chronos_token else {}
        config = export(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            **extra,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(failure_message)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(config)


def _export_full_backup(include_secrets: bool = False):
    _require_session()

    try:
        zip_data = corehub.export_full_corehub_backup(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            include_secrets=include_secrets,
            chronos_token=state.chronos_token,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to export full CoreHub backup")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)
    filename = f"corehub_full_backup_{timestamp}.zip"
    return _attachment(zip_data, APPLICATION_ZIP, filename)


def _register_export_routes(app: FastAPI) -> None:
    """The Export All, global configuration and full backup export routes."""

    @app.get("/api/export/all-pipelines", responses=_COREHUB_CALL_RESPONSES)
    async def export_all_pipelines(include_secrets: bool = False):
        return _export_all_pipelines(include_secrets)

    @app.get("/api/export/global-configs", responses=_COREHUB_CALL_RESPONSES)
    async def export_global_configs():
        """Export all global CoreHub configuration settings."""
        return _export_corehub_config(corehub.export_global_configs, "Failed to export global configs")

    @app.get("/api/export/smtp", responses=_COREHUB_CALL_RESPONSES)
    async def export_smtp():
        """Export SMTP configuration from CoreHub."""
        return _export_corehub_config(corehub.export_smtp_settings, "Failed to export SMTP settings")

    @app.get("/api/export/webhooks", responses=_COREHUB_CALL_RESPONSES)
    async def export_webhooks():
        """Export webhooks configuration from CoreHub."""
        return _export_corehub_config(corehub.export_webhooks_config, "Failed to export webhooks config")

    @app.get("/api/export/thresholds", responses=_COREHUB_CALL_RESPONSES)
    async def export_thresholds():
        """Export thresholds configuration from CoreHub."""
        return _export_corehub_config(corehub.export_thresholds, "Failed to export thresholds")

    @app.get("/api/export/schedules", responses=_COREHUB_CALL_RESPONSES)
    async def export_schedules():
        """Export Chronos scheduler jobs and settings from CoreHub."""
        return _export_corehub_config(corehub.export_schedules, "Failed to export schedules", with_chronos_token=True)

    @app.get("/api/export/users", responses=_COREHUB_CALL_RESPONSES)
    async def export_users():
        """Export user management configuration from CoreHub."""
        return _export_corehub_config(corehub.export_users, "Failed to export users")

    @app.get("/api/export/oidc", responses=_COREHUB_CALL_RESPONSES)
    async def export_oidc():
        """Export OIDC configuration from CoreHub."""
        return _export_corehub_config(corehub.export_oidc_config, "Failed to export OIDC config")

    @app.get("/api/export/full-backup", responses=_COREHUB_CALL_RESPONSES)
    async def export_full_backup(include_secrets: bool = False):
        """Export a complete CoreHub backup including pipelines, global configs, SMTP, webhooks, thresholds, and schedules."""
        return _export_full_backup(include_secrets)


# Strong references to the entity creation runs started in the background, so
# that they are not garbage collected before they finish.
_BACKGROUND_TASKS: set = set()


@dataclass
class _RunSettings:
    """Session and options one entity creation run works with."""

    token: str
    base_url: str
    enable_scheduling: Any
    create_tables: Any
    use_ssl: Any
    skip_verify: Any
    log_callback: Callable[[str], None]
    yaml_file: str = ""
    source_type: str = ""
    target_type: str = ""


def _run_schema_types(request: RunRequest, yaml_file_path: str) -> tuple[str, str]:
    """Source and target type of a run: the request's, else the YAML's, else SQL."""
    # Extract source_type and target_type from YAML if they're empty
    source_type = request.source_type
    target_type = request.target_type

    if not source_type or not target_type:
        from commons import extract_schema_types_from_yaml
        src_hint, tgt_hint = extract_schema_types_from_yaml(yaml_file_path)
        if not source_type and src_hint:
            source_type = src_hint
        if not target_type and tgt_hint:
            target_type = tgt_hint

    # Final fallback to SQL if still empty
    if not source_type:
        source_type = "SQL"
    if not target_type:
        target_type = "SQL"
    return source_type, target_type


def _run_create_entities(request: RunRequest, settings: _RunSettings, source_schema: str, target_schema: str) -> dict:
    """Create the entities of one schema pair of a run."""
    return corehub.run_create_entities(
        token=settings.token,
        base_url=settings.base_url,
        pipeline_id=request.pipeline_id,
        source_schema=source_schema,
        target_schema=target_schema,
        source_type=settings.source_type,
        target_type=settings.target_type,
        yaml_file=settings.yaml_file,
        skip_errors=request.skip_errors,
        chunk_size=request.chunk_size,
        enable_scheduling=settings.enable_scheduling,
        create_tables=settings.create_tables,
        use_ssl=settings.use_ssl,
        skip_verify=settings.skip_verify,
        log_callback=settings.log_callback,
        chronos_token=state.chronos_token,
    )


def _run_all_schema_pairs(request: RunRequest, settings: _RunSettings) -> dict:
    """Create the entities of every schema pair the run's YAML defines."""
    yaml_file_path = settings.yaml_file
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
        if settings.log_callback is not None:
            settings.log_callback(
                f"[Schema {idx}/{len(schema_pairs)}] Creating entities for {src_schema} -> {tgt_schema}"
            )

        result = _run_create_entities(request, settings, src_schema, tgt_schema)

        if not result.get("success"):
            overall_success = False
            if result.get("error"):
                errors.append(str(result["error"]))

    return {
        "success": overall_success,
        "logs": [],
        "error": "; ".join(errors) if errors else None,
    }


def _run_entity_creation(request: RunRequest, settings: _RunSettings, yaml_path: Path) -> dict:
    """Run the entity creation of a run, for every schema pair of its YAML or for the requested schemas."""
    yaml_file_path = str(yaml_path)
    source_type, target_type = _run_schema_types(request, yaml_file_path)
    settings = dataclass_replace(settings, yaml_file=yaml_file_path, source_type=source_type, target_type=target_type)

    if request.auto_schemas:
        return _run_all_schema_pairs(request, settings)

    # Custom schemas provided by user
    return _run_create_entities(request, settings, request.source_schema, request.target_schema)


async def _execute_run(run_id: str, request: RunRequest) -> None:
    token = state.token
    base_url = state.base_url
    if not token or not base_url:
        state.finalize_run(run_id, "failed", AUTH_REQUIRED)
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

    settings = _RunSettings(
        token=token,
        base_url=base_url,
        enable_scheduling=enable_scheduling,
        create_tables=create_tables,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
        log_callback=_log_callback,
    )

    loop = asyncio.get_running_loop()

    def _run_sync() -> dict:
        return _run_entity_creation(request, settings, yaml_path)

    result = await loop.run_in_executor(None, _run_sync)

    status = "completed" if result.get("success") else "failed"
    state.finalize_run(run_id, status, result.get("error"))


def _start_run(request: RunRequest) -> ApiMessage:
    if not state.token:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED)

    try:
        run_status = state.start_run()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task = asyncio.create_task(_execute_run(run_status.run_id, request))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return ApiMessage(message="Entity creation started")


async def upload_certificate(
    pipeline_id: str = Form(..., alias="pipelineId"),
    agent_id: str = Form(..., alias="agentId"),
    certificate_type: str = Form(..., alias="certificateType"),
    file: UploadFile = File(...)
) -> ApiMessage:
    """Upload a certificate file for an agent.

    Args:
        pipeline_id: Pipeline ID
        agent_id: Agent ID
        certificate_type: Type of certificate (truststore, keystore, certificate)
        file: Certificate file to upload

    Returns:
        ApiMessage with success status
    """
    _require_session()

    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    # Extract file extension
    certificate_ext = Path(file.filename).suffix.lstrip('.').lower() or 'crt'

    # Read file content
    certificate_data = await file.read()
    if not certificate_data:
        raise HTTPException(status_code=400, detail="Certificate file is empty")

    try:
        corehub.upload_agent_certificate(
            token=state.token,
            pipeline_id=pipeline_id,
            agent_id=agent_id,
            certificate_type=certificate_type,
            certificate_data=certificate_data,
            certificate_ext=certificate_ext,
        )

        return ApiMessage(
            success=True,
            message=f"Certificate uploaded successfully for agent {agent_id}"
        )
    except Exception as e:
        logger.exception(f"Failed to upload certificate: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload certificate: {str(e)}"
        ) from e


def _register_run_routes(app: FastAPI) -> None:
    """The entity creation run and the agent certificate upload routes."""

    @app.post("/api/run", responses=_RUN_RESPONSES)
    async def start_run(request: RunRequest) -> ApiMessage:
        return _start_run(request)

    app.add_api_route(
        "/api/agent/certificate/upload", upload_certificate, methods=["POST"], responses=_CERTIFICATE_RESPONSES
    )


# ── MCP client setup (one-click install) ───────────────────────────────

def _mcp_client_config_paths() -> Dict[str, Path]:
    """Return per-client MCP config file paths for the current OS."""
    home = Path.home()
    if sys.platform == "darwin":
        claude = home / "Library" / "Application Support" / "Claude" / CLAUDE_DESKTOP_CONFIG
    elif sys.platform == "win32":
        claude = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude" / CLAUDE_DESKTOP_CONFIG
    else:
        claude = home / ".config" / "Claude" / CLAUDE_DESKTOP_CONFIG
    return {
        "claude": claude,
        "windsurf": home / ".codeium" / "windsurf" / "mcp_config.json",
        "cursor": home / ".cursor" / "mcp.json",
    }


def _mcp_server_entry() -> Dict[str, Any]:
    """Build the MCP server config entry pointing at this installation."""
    project_root = str(Path(__file__).resolve().parent.parent)
    if getattr(sys, "frozen", False):
        # PyInstaller build: python interpreter not bundled separately
        python_cmd = "python3" if sys.platform != "win32" else "python"
    else:
        python_cmd = sys.executable
    return {
        "command": python_cmd,
        "args": ["-m", "mcp_server.server"],
        "env": {"PYTHONPATH": project_root},
    }


def _mcp_status() -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for client, path in _mcp_client_config_paths().items():
        installed = False
        detected = path.parent.exists()
        if path.exists():
            try:
                cfg = json.loads(path.read_text())
                installed = "gluesync" in cfg.get("mcpServers", {})
            except Exception:  # pylint: disable=broad-except
                pass
        result[client] = {"detected": detected, "installed": installed, "configPath": str(path)}
    return result


def _install_mcp_client(client: str, path: Optional[Path], entry: Dict[str, Any]) -> Optional[str]:
    """Add the gluesync MCP server entry to one client's config; the error, if it could not."""
    if path is None:
        return f"Unknown client: {client}"
    try:
        if path.exists():
            try:
                cfg = json.loads(path.read_text())
            except json.JSONDecodeError:
                return f"{client}: existing config is not valid JSON ({path})"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            cfg = {}
        cfg.setdefault("mcpServers", {})["gluesync"] = entry
        path.write_text(json.dumps(cfg, indent=2) + "\n")
    except Exception as exc:  # pylint: disable=broad-except
        return f"{client}: {exc}"
    return None


def _mcp_install(payload: Dict[str, Any]) -> ApiMessage:
    clients = payload.get("clients") or []
    if not clients:
        raise HTTPException(status_code=400, detail="No clients selected")

    paths = _mcp_client_config_paths()
    entry = _mcp_server_entry()
    configured: list[str] = []
    errors: list[str] = []

    for client in clients:
        path = paths.get(client)
        error = _install_mcp_client(client, path, entry)
        if error is not None:
            errors.append(error)
            continue
        configured.append(client)
        logger.info("MCP server configured for %s at %s", client, path)

    if not configured:
        raise HTTPException(status_code=500, detail="; ".join(errors) or "Nothing configured")

    msg = f"MCP server configured for: {', '.join(configured)}. Restart the client app(s) to activate."
    if errors:
        msg += f" Errors: {'; '.join(errors)}"
    return ApiMessage(message=msg)


def _register_mcp_routes(app: FastAPI) -> None:
    """The MCP client detection and one-click install routes."""

    @app.get("/api/mcp/status")
    async def mcp_status() -> Dict[str, Any]:
        """Report which MCP clients are detected and whether gluesync is configured."""
        return _mcp_status()

    @app.post("/api/mcp/install", responses=_MCP_INSTALL_RESPONSES)
    async def mcp_install(payload: Dict[str, Any]) -> ApiMessage:
        """Install the gluesync MCP server entry into the selected clients' configs."""
        return _mcp_install(payload)


def _mount_mcp_server(app: FastAPI) -> None:
    """Mount the Gluesync MCP server (SSE transport) at /mcp when the mcp package is available."""
    # Mount the Gluesync MCP server at /mcp so any MCP-capable AI agent can
    # connect while the Automator is running.  The dependency on the `mcp`
    # package is optional: if it is not installed the Automator continues to
    # work normally and only the /mcp endpoint is unavailable.
    try:
        from mcp.server.sse import SseServerTransport
        from starlette.routing import Mount, Route
        from starlette.applications import Starlette
        from mcp_server.server import server as _mcp_server, InitializationOptions, set_token_provider

        # Set token provider to use Automator's stored token
        set_token_provider(lambda: state.token or "")

        _sse_transport = SseServerTransport("/mcp/messages/")

        async def _mcp_handle_sse(request):
            async with _sse_transport.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await _mcp_server.run(
                    streams[0],
                    streams[1],
                    InitializationOptions(
                        server_name="gluesync-automator",
                        server_version="1.0.0",
                        capabilities=_mcp_server.get_capabilities(
                            notification_options=None,
                            experimental_capabilities={},
                        ),
                    ),
                )

        _mcp_starlette = Starlette(
            routes=[
                Route("/sse", endpoint=_mcp_handle_sse),
                Mount("/messages/", app=_sse_transport.handle_post_message),
            ]
        )
        app.mount("/mcp", _mcp_starlette)
        logger.info("Gluesync MCP server mounted at /mcp/sse (SSE transport)")
    except ImportError:
        logger.info(
            "mcp package not available – MCP server endpoint disabled. "
            "Install 'mcp' to enable AI agent integration."
        )
    except Exception as _mcp_exc:  # pylint: disable=broad-except
        logger.warning("Failed to mount MCP server: %s", _mcp_exc)


def create_app() -> FastAPI:
    _ensure_static_assets()
    asset_rewrites = _fingerprint_static_assets()

    app = FastAPI(title="Gluesync Automator", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    # FastAPI matches routes in registration order: keep this order.
    _register_page_routes(app, asset_rewrites)
    _register_session_routes(app)
    _register_bulk_routes(app)
    _register_pipeline_routes(app)
    _register_import_routes(app)
    _register_export_routes(app)
    _register_run_routes(app)
    _register_mcp_routes(app)
    _mount_mcp_server(app)
    _register_ai_settings_routes(app)

    return app



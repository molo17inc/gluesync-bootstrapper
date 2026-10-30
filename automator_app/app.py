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
import contextlib
import copy
import operator
import functools
import io
import json
import mimetypes
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Optional, cast
import hashlib
import re

import requests
import yaml
import create_user_defined_functions
import import_options
import agent_overrides
import import_plan
import function_collection
from . import entity_plan_runtime

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator

import commons
from commons import extract_all_schemas_from_yaml, extract_schema_types_from_yaml, load_yaml_config
from . import corehub
import ai_settings
from .corehub import PipelineInMaintenanceError
from .state import state
from .version import get_version

logger = logging.getLogger(__name__)


def _install_log_redaction() -> None:
    """Hide passwords from every log record of the process (``password=...`` / ``"password": ...`` and the exact
    agent passwords of the imports in progress), tracebacks included. Installed once."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_gs_redacting", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            return record
        redacted = import_plan.SECRETS.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = import_plan.SECRETS.redact(logging.Formatter().formatException(record.exc_info))
        return record

    factory._gs_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


_install_log_redaction()

# One backup import (or import preview) at a time: a second one is answered 409.
_IMPORT_LOCK = threading.Lock()
IMPORT_BUSY = "Another backup import or preview is running; try again when it has finished."


@contextlib.contextmanager
def _exclusive_import():
    if not _IMPORT_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=IMPORT_BUSY)
    try:
        yield
    finally:
        _IMPORT_LOCK.release()

LOGIN_REQUIRED = "Log in to a Core Hub first"
AUTH_REQUIRED = "Authentication required"
UPLOADED_YAML_NOT_FOUND = "Uploaded YAML not found"
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
    "user-groups", "data-access-policies",
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
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Owner-only: it holds uploaded backups and their UDF sources (mkdir's mode is not applied to an existing dir).
    with contextlib.suppress(OSError):
        os.chmod(base, 0o700)
    return base


def new_session_dir(kind: str) -> Path:
    """Create and return a fresh, self-contained session directory.

    Each upload/import gets a single root that holds BOTH the YAML and its UDF
    source files together, so UDF lookups never have to span sibling folders.
    """
    session_dir = get_workspace_root() / f"{kind}_{uuid.uuid4().hex[:8]}"
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(session_dir, 0o700)
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
    # Entity-import Customize plan (single-pipeline subset of a backup plan). ``{}`` / omitted = current behaviour.
    plan: Optional[Dict[str, Any]] = None
    dry_run: bool = Field(False, alias="dryRun")


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
    planUsed: Optional[Dict[str, Any]] = None
    # Things to do after a successful call, never failures (a restored connection that needs a sign-in)
    warnings: Optional[list[str]] = None


class AiSettingsExportRequest(BaseModel):
    """The passphrase and the password travel in the body, never in a URL that proxies and logs keep."""

    include_secrets: bool = False
    passphrase: Optional[str] = None
    # Core Hub 2.3.0.1 asks the signed-in user's current password again to export secrets.
    current_password: Optional[str] = None


class FullBackupRequest(BaseModel):
    include_secrets: bool = False
    ai_settings_passphrase: Optional[str] = None
    ai_settings_current_password: Optional[str] = None


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
    plan: Optional[Dict[str, Any]] = None
    dry_run: bool = Field(False, alias="dryRun")


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


_MASKED_PASSWORD = re.compile(r"^\*{3,}$")


def _is_masked(value: Any) -> bool:
    """A password masked by an export: asterisks only (Core Hub writes ``*******``; a hand edit may use more or fewer)."""
    return isinstance(value, str) and bool(_MASKED_PASSWORD.match(value.strip()))


def _has_masked_password(value: Any) -> bool:
    """Recursively detect any password or custom credential secret set to a masked value: a ``password`` field
    anywhere (asterisks only, see ``_is_masked``), hostCredentials / customHostCredentials included, and any secret of
    the custom host credentials holding ``*******`` or Core Hub's ``<hidden>`` placeholder."""
    if isinstance(value, dict):
        if _is_masked(value.get("password")):
            return True
        # Any secret of the custom host credentials, which Core Hub names by key (apiKey, ...Secret, ...)
        if corehub.has_masked_custom_secret(value.get("customHostCredentials")):
            return True
        return any(_has_masked_password(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_masked_password(v) for v in value)
    return False


NO_PIPELINES_IMPORTED = "No pipelines were imported from the archive."
MASKED_PASSWORDS = ("Config contains masked passwords or secrets (*******). "
                    "Please replace them with the real values before importing.")


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
    413: {"description": "The archive is larger than GS_IMPORT_MAX_UPLOAD_MB (analyze)"},
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


def _zip_destination_under(root: Path, member: str) -> Path:
    """root / member, refusing a ZIP member path that escapes root (zip slip)."""
    normalized = member.replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
        raise ValueError(f"Unsafe path in the backup ZIP, refusing to extract it: {member!r}")
    dest = (root / normalized).resolve()
    if not dest.is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe path in the backup ZIP, refusing to extract it: {member!r}")
    return dest


def _extract_udf_source(zf: zipfile.ZipFile, member: str, root: Path) -> None:
    """Extract one UDF source file under root, keeping its path inside the ZIP.

    Raises ValueError for a member that would land outside root (absolute path, ``..``, drive letter).
    """
    dest = _zip_destination_under(root, member)
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
            ai_settings.check_current_password(request.ai_settings_current_password)
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
            ai_settings_current_password=request.ai_settings_current_password,
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
            current_password=request.current_password,
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
    """Extract the YAML, UDF and function collection source files of an uploaded ZIP; return the primary YAML, if any."""
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
        # ... and the function collection (function-collection/), which a run publishes before it creates entities
        function_collection.extract_function_collection_members(zf, extract_dir)
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


def _schema_types_from_pipeline_agents(pipeline_id: str, source_type: str, target_type: str) -> tuple[str, str]:
    """Fill the empty source/target type from the pipeline's agents, as the bulk schemas and template routes do."""
    try:
        src_hint, tgt_hint = corehub.infer_agent_schema_types(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
        )
        return source_type or src_hint, target_type or tgt_hint
    except Exception as e:  # pylint: disable=broad-except
        logger.warning(f"Could not infer types from the pipeline agents: {e}")
    return source_type, target_type


def _bulk_schema_types(request: BulkCreateRequest) -> tuple[str, str]:
    """Source and target type of a bulk creation: the request's, else the pipeline agents', else SQL."""
    source_type = request.source_type
    target_type = request.target_type

    if not source_type or not target_type:
        logger.info(
            "source_type or target_type empty (source='%s', target='%s'), inferring them from the pipeline agents",
            source_type,
            target_type,
        )
        source_type, target_type = _schema_types_from_pipeline_agents(request.pipeline_id, source_type, target_type)

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

    plan = _load_entity_plan_or_400(request.plan)
    enable_scheduling, create_tables, _restore_fc = _entity_plan_flags(
        plan, request.pipeline_id, enable_scheduling=enable_scheduling, create_tables=create_tables,
    )
    table_names = entity_plan_runtime.filter_table_names(
        list(request.table_names), plan, request.pipeline_id, request.source_schema,
    )
    if not table_names:
        raise HTTPException(
            status_code=400,
            detail="Entity import plan: no table selected; leave entities.include out to take every selected table, "
                   "or list at least one SCHEMA.TABLE",
        )

    source_type, target_type = _bulk_schema_types(request)

    # Disable table creation for NoSQL / object-store / streaming targets (same as run_create_entities /
    # duplicate pipeline — agents.json catalog type, not the request's SQL/NoSQL hint alone).
    effective_create_tables = bool(create_tables)
    if target_type and target_type.upper() == 'NOSQL':
        logger.info("Target is NoSQL - disabling table creation")
        effective_create_tables = False

    pipeline = import_plan.entity_pipeline(plan, request.pipeline_id)
    import_opts = _entity_run_import_options(plan, pipeline, dry_run=bool(request.dry_run))
    bulk_yaml = _bulk_yaml_for_plan(
        plan, pipeline, request.source_schema, request.target_schema, table_names,
    )

    try:
        result = corehub.run_create_entities_for_tables(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=request.pipeline_id,
            source_schema=request.source_schema,
            target_schema=request.target_schema,
            source_type=source_type,
            target_type=target_type,
            table_names=table_names,
            enable_scheduling=bool(enable_scheduling),
            create_tables=effective_create_tables,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            options=corehub.EntityTablesRunOptions(
                skip_errors=request.skip_errors,
                chunk_size=request.chunk_size,
                chronos_token=state.chronos_token,
                entity_import_options=import_opts,
                yaml_config=bulk_yaml,
            ),
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
    return ApiMessage(success=ok, message=msg, logs=logs, planUsed=plan.to_dict() if not plan.empty else None)


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


def _discover_table_metadata(
    discover: Callable[..., Any], failure_message: str, pipeline_id: str, source_schema: str, name: str
) -> Any:
    """Metadata of one source table read by discover; an empty list (logged) when that fails."""
    try:
        return discover(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=pipeline_id,
            schema=source_schema,
            table_name=name,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception(failure_message, source_schema, name, pipeline_id, exc)
        return []


def _template_table_entry(pipeline_id: str, source_schema: str, name: str) -> dict[str, Any]:
    """Template entry of one table: its primary keys and, when discovered, its columns."""
    pk_names = _discover_table_metadata(
        corehub.discover_primary_key_names,
        "Failed to discover primary keys for %s.%s on pipeline %s: %s",
        pipeline_id,
        source_schema,
        name,
    )
    columns_meta = _discover_table_metadata(
        corehub.discover_column_metadata_for_table,
        "Failed to discover column metadata for %s.%s on pipeline %s: %s",
        pipeline_id,
        source_schema,
        name,
    )

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
        A pipeline with a function collection (Gluesync 2.3) always gets the ZIP, with
        the collection under ``function-collection/``.

        Duplicate source tables are handled by creating unique keys (e.g., table@@2, table@@3)
        within the same YAML file to avoid key conflicts.
        """
        return _export_pipeline(pipeline_id, include_secrets)

    @app.get("/api/export/pipeline/{pipeline_id}/full", responses=_PIPELINE_EXPORT_RESPONSES)
    async def export_pipeline_full_backup(pipeline_id: str, include_secrets: bool = False):
        """Export a full backup (YAML + agents-config + UDFs + function collection) for a single pipeline."""
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
    # GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE: agents whose credentials CoreHub refused (DB unreachable).
    continue_on_credentials_failure: bool = False
    credentials_pending: list[dict] = field(default_factory=list)
    # What the entity runs of a pipeline with credentials pending saved without discovery / Core Hub refused.
    offline_report: list[str] = field(default_factory=list)
    # GS_IMPORT_RESUME_PIPELINE_ID / resume_pipeline_id: an existing pipeline whose specific configuration,
    # entities and UDFs are restored again from the backup (no pipeline, agent or credentials is created).
    resume_pipeline_id: Optional[str] = None
    # Original pipeline id of the backup YAML to resume from, when the archive holds more than one.
    resume_source_pipeline_id: Optional[str] = None
    # Informational lines of the import (agent overrides applied, ...): never a password.
    notes: list[str] = field(default_factory=list)
    # Per pipeline id: what its entity runs created / failed / skipped, and whether anything went wrong.
    outcomes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # The custom import plan of this run (empty: Import All) and the agent passwords of this request only
    # ({backup pipeline id: {agentTag|agentId: password}}), never written anywhere.
    plan: import_plan.ImportPlan = field(default_factory=import_plan.ImportPlan)
    agent_passwords: Dict[str, Dict[str, str]] = field(default_factory=dict, repr=False)
    # Where a plan's passwordRef "env:NAME" is read (the CLI: os.environ).
    plan_env: Any = field(default=None, repr=False)
    # Things to do after a successful import, never failures (a restored connection that needs a sign-in)
    warnings: list[str] = field(default_factory=list)


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
    pending_agent_types: frozenset = frozenset()
    # --on-existing used when the environment sets none (resume: "skip", so a rerun never duplicates).
    default_on_existing: Optional[str] = None
    # Plan: compile the UDFs after each schema pair; the per-run options (onExistingEntity, validate, remap).
    compile_udfs: bool = True
    plan_choices: Optional[Dict[str, Any]] = None


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


def _agents_config_mapping(name: str, text: str) -> Dict[str, Any]:
    """The parsed agents-config ``name``; 400 when it is no YAML mapping."""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse {name}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail=f"{name} is not a YAML mapping")
    return parsed


def _unseen_agents(candidates: list, seen: set) -> list:
    """The agents whose agentId (or id) is not in ``seen`` yet, adding theirs; agents without one are all kept."""
    kept = []
    for agent in candidates:
        agent_id = agent.get("agentId") or agent.get("id") if isinstance(agent, dict) else None
        if agent_id in seen:
            continue
        if agent_id is not None:
            seen.add(agent_id)
        kept.append(agent)
    return kept


def _merge_agents_configs(names: list[str], texts: list[str]) -> str:
    """One agents-config.yaml of the several a ZIP of single-pipeline exports holds (pipeline_<id>/agents-config.yaml
    each): their agents (once per agentId) and their pipelines mappings, so that each pipeline is bound to the agents
    of its own export, not to those of the first one. 400 when one of them is not a YAML mapping."""
    agents: list = []
    pipelines: list = []
    seen: set = set()
    merged: Dict[str, Any] = {}
    for name, text in zip(names, texts):
        parsed = _agents_config_mapping(name, text)
        for key, value in parsed.items():
            if key not in ("agents", "pipelines"):
                merged.setdefault(key, value)
        agents.extend(_unseen_agents(parsed.get("agents") or [], seen))
        pipelines.extend(item for item in parsed.get("pipelines") or [] if isinstance(item, dict))
    merged["agents"] = agents
    merged["pipelines"] = pipelines
    return yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)


def _read_agents_config_text(contents: bytes, operation: str, logs: Optional[list[str]] = None) -> str:
    """Text of the agents-config.yaml of a backup ZIP; 400 when there is none or the ZIP is invalid."""
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as zf:
            names = zf.namelist()
            if logs is not None:
                logs.append(f"Archive contains {len(names)} entries.")

            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                # zipfile reads the last of same-named entries only: refuse rather than import one copy (or two)
                raise HTTPException(status_code=400, detail="Invalid ZIP archive: duplicate entries "
                                    + ", ".join(duplicates[:5]))
            agents_names = [name for name in names if not _is_macos_metadata(name)
                            and name.lower().endswith(("agents-config.yaml", "agents-config.yml"))]
            if not agents_names:
                raise HTTPException(
                    status_code=400,
                    detail="Archive does not contain agents-config.yaml",
                )

            if logs is not None:
                logs.extend(f"Found agents config: {agents_name}" for agents_name in agents_names)
            texts = [zf.read(agents_name).decode("utf-8") for agents_name in agents_names]
            return texts[0] if len(texts) == 1 else _merge_agents_configs(agents_names, texts)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to process uploaded ZIP during %s", operation)
        raise HTTPException(status_code=400, detail=f"Invalid ZIP archive: {exc}") from exc


def _parse_agents_config(agents_text: str, operation: str, allow_masked: bool = False) -> tuple[Any, list]:
    """Parsed agents-config.yaml and its agents list; 400 when it cannot be imported.

    ``allow_masked``: masked passwords are no refusal here (analysis, a resume that sends no credentials, or a
    custom import whose plan / passwords may give them: checked per pipeline before its credentials are sent)."""
    try:
        agents_config = yaml.safe_load(agents_text)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to parse agents-config.yaml during %s", operation)
        raise HTTPException(status_code=400, detail=f"Failed to parse agents-config.yaml: {exc}") from exc

    if not allow_masked and _has_masked_password(agents_config):
        raise HTTPException(status_code=400, detail=MASKED_PASSWORDS)

    agents_list = agents_config.get("agents")
    if not isinstance(agents_list, list) or not agents_list:
        raise HTTPException(status_code=400, detail="agents-config.yaml does not contain a non-empty 'agents' list")
    return agents_config, agents_list


def _extract_import_udf_sources(contents: bytes) -> Optional[Path]:
    """Extract the UDF and function collection sources of a backup ZIP into a new session directory; point UDF_PATH at it."""
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
            # The function collections, each under its pipeline_<id>/ folder of the ZIP (or at the root)
            function_collection.extract_function_collection_members(zf_udf, udf_root)

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


def _restore_archive_globals(contents: bytes, errors: list[str],
                             plan: Optional[import_plan.ImportPlan] = None) -> None:
    """Restore the global notifications, users and OIDC configuration of a backup ZIP (each unless the plan's
    ``globals`` turns it off)."""
    plan = plan or import_plan.ImportPlan()
    # Restore global notification settings (SMTP) if present in the backup.
    if plan.global_enabled("notifications"):
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
    if not plan.global_enabled("users"):
        return
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


def _restore_archive_access(contents: bytes, errors: list[str],
                            plan: Optional[import_plan.ImportPlan] = None) -> None:
    """Restore the user groups and the data-access policies of a full backup ZIP.

    Runs once the pipelines are imported, so that the policies find the connections the import recreated.
    A policy whose connection is still missing is left out and reported, like any other failure.
    """
    connection = {
        "token": state.token,
        "base_url": state.base_url,
        "use_ssl": state.use_ssl,
        "skip_verify": state.skip_verify,
    }
    sections = (
        ("user groups", "user-groups", lambda data: corehub.import_user_groups(**connection, groups_data=data)),
        ("data-access policies", "data-access-policies",
         lambda data: corehub.import_data_access_policies(**connection, policies_data=data)),
    )
    plan = plan or import_plan.ImportPlan()
    enabled = {"user-groups": plan.global_enabled("users"),
               "data-access-policies": plan.global_enabled("accessPolicies")}
    for label, stem, restore in sections:
        if enabled[stem]:
            _restore_access_section(contents, errors, label, stem, restore)


def _restore_access_section(contents: bytes, errors: list[str], label: str, stem: str, restore: Any) -> None:
    """Restore one access section (user groups or data-access policies) of a backup ZIP, if it has one."""
    try:
        data = _find_config_by_stem(contents, stem)
    except Exception as exc:  # pylint: disable=broad-except
        errors.append(f"{label}: {exc}")
        return
    if data is not None:
        result = corehub.restore_safely(f"{stem}.yaml", functools.partial(restore, data))
        _record_restore_result(errors, label, result)


def _record_restore_result(errors: list[str], label: str, result: Dict[str, Any]) -> None:
    if result.get("status") == "failed":
        errors.append(f"{label}: {result.get('error')}")
        return
    errors.extend(
        f"{label}: {message}"
        for key in ("errors", "warnings", "skipped_policies")
        for message in result.get(key) or []
    )
    if result.get("status") == "restored":
        logger.info("Restored %s during import-all: %s", label, result.get("summary"))


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
    """Certificate file an agent of a backup ZIP points at; None (and an error) when it is missing or empty."""
    # Try to find the certificate file in the ZIP
    # Support both absolute and relative paths
    cert_filename = cert_path.lstrip("./")
    logger.debug(f"Looking for certificate file: {cert_filename}")
    logger.debug(f"Available files in ZIP: {zf_cert.namelist()}")
    # Pipeline folder of the YAML (e.g. pipeline_xxx/certificate.txt), if any
    pipeline_folder = name.split('/')[0] if '/' in name else None

    # Try exact match first
    if cert_filename in zf_cert.namelist():
        cert_data = zf_cert.read(cert_filename)
        logger.debug(f"Found certificate via exact match: {cert_filename}")
    else:
        # Try with pipeline folder prefix (e.g., pipeline_xxx/certificate.txt)
        cert_data = _find_certificate_near_pipeline(zf_cert, pipeline_folder, cert_filename)

    if cert_data is None:
        error_msg = f"{name}: Certificate file '{cert_path}' not found in ZIP for agent {agent_id}"
        logger.error(error_msg)
        logger.error(f"Searched for: {cert_filename}, {pipeline_folder}/{cert_filename if pipeline_folder else 'N/A'}")
        errors.append(error_msg)
        return None
    if not cert_data:
        error_msg = f"{name}: Certificate file '{cert_path}' in the ZIP is empty; not uploaded for agent {agent_id}"
        logger.error(error_msg)
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
    if cert_data is None:
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
        try:
            _send_agent_credentials(agent_cfg, agent_id, new_pipeline_id)
        except Exception as error:  # pylint: disable=broad-except
            if not (job.continue_on_credentials_failure and corehub.credentials_failure_is_recoverable(error)):
                raise
            corehub.record_credentials_pending(job.credentials_pending, new_pipeline_id, agent_id,
                                               agent_cfg.get("agentType"), agent_cfg.get("agentTag"), error)


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
    commons.take_schedule_failures()  # only what this run fails to restore counts
    try:
        run_options = _entity_import_options(entities.pending_agent_types, entities.default_on_existing,
                                             entities.plan_choices)
        result_run = corehub.run_create_entities(
            token=state.token,
            base_url=state.base_url,
            pipeline_id=entities.pipeline_id,
            source_schema=src_schema,
            target_schema=tgt_schema,
            source_type=entities.source_type,
            target_type=entities.target_type,
            yaml_file=str(entities.yaml_path),
            enable_scheduling=entities.enable_scheduling,
            create_tables=entities.create_tables,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            options=corehub.EntityRunOptions(
                chronos_token=state.chronos_token,
                restore_function_collection=False,  # published once for the pipeline, before its first schema pair
                import_options=run_options,
            ),
        )
        _record_offline_outcome(job, entities, src_schema, tgt_schema, result_run)
        _record_entity_counts(job, entities.pipeline_id, result_run, run_options)
        _record_schedule_failures(job, entities, src_schema, tgt_schema)
        if not result_run.get("success"):
            success = False
            run_errors = (result_run.get("result") or {}).get("errors") if isinstance(result_run.get("result"), dict) \
                else None
            error_text = "; ".join(_condense_errors(run_errors)) if run_errors else result_run.get("error")
            if error_text:
                job.errors.append(f"{name}: entity creation error for {src_schema}->{tgt_schema}: {error_text}")

        # After entities are created for this schema pair, automatically
        # compile and register any UDFs referenced in the YAML using the
        # exported source files from the backup.
        if job.udf_root is not None and entities.compile_udfs and \
                not _compile_imported_udfs(job, entities, src_schema, tgt_schema):
            success = False
    except Exception as exc:  # pylint: disable=broad-except
        success = False
        _outcome(job, entities.pipeline_id)["runErrors"] += 1
        logger.exception(
            "Failed to create entities for pipeline %s (%s) from %s", entities.pipeline_name, entities.pipeline_id, name
        )
        job.errors.append(
            f"{name}: exception during entity creation for {src_schema}->{tgt_schema}: {exc}"
        )
    return success


def _pending_agent_types(job: _ArchiveImport, pipeline_id: Any) -> frozenset:
    """``{"SOURCE", "TARGET"}`` subset whose credentials CoreHub refused for this pipeline."""
    return frozenset(p.get("agentType") for p in job.credentials_pending
                     if str(p.get("pipelineId")) == str(pipeline_id) and p.get("agentType"))


def _with_plan_choices(options: Any, plan_choices: Dict[str, Any]) -> Any:
    """``options`` with the choices of a custom import plan applied over them."""
    if plan_choices.get("on_existing"):
        options.on_existing = plan_choices["on_existing"]
    if plan_choices.get("validate") is not None:
        options.validate = plan_choices["validate"]
    if plan_choices.get("strict_validate") is not None:
        options.strict_validate = plan_choices["strict_validate"]
    if plan_choices.get("remap"):
        options.remap = options.remap.merged(plan_choices["remap"])
    return options


def _entity_import_options(pending_agent_types: frozenset, default_on_existing: Optional[str] = None,
                           plan_choices: Optional[Dict[str, Any]] = None):
    """The import options of an entity run: from the environment, and, when the SOURCE agent's credentials are
    pending, no discovery at all (entities built from the backup YAML: Core Hub 2.3.1 refuses them with 400 "a
    modified entity is saved only when its keys can be checked" while the agents are unreachable).

    ``default_on_existing`` (resume): the --on-existing behaviour when GS_IMPORT_ON_EXISTING sets none.
    ``plan_choices`` (a custom import plan, this run only): onExistingEntity / validate / strictValidate / remap,
    over the environment."""
    options = import_options.from_env_if_requested()
    if plan_choices:
        options = _with_plan_choices(options or import_options.ImportOptions.from_env(), plan_choices)
    if default_on_existing and not (options and options.on_existing):
        options = options or import_options.ImportOptions.from_env()
        options.on_existing = default_on_existing
    if "SOURCE" not in pending_agent_types:
        return options
    options = options or import_options.ImportOptions.from_env()
    options.no_discovery = True
    options.allow_offline_entities = True
    return options


def _outcome(job: _ArchiveImport, pipeline_id: Any, pipeline_name: Any = None, resumed: bool = False) -> Dict[str, Any]:
    """The outcome record of one imported (or resumed) pipeline, created on first use."""
    key = str(pipeline_id)
    if key not in job.outcomes:
        job.outcomes[key] = {"pipelineId": key, "pipelineName": pipeline_name, "resumed": resumed, "created": 0,
                             "failed": 0, "skipped": 0, "runErrors": 0, "errors": 0, "entitiesRun": False}
    elif pipeline_name and not job.outcomes[key].get("pipelineName"):
        job.outcomes[key]["pipelineName"] = pipeline_name
    return job.outcomes[key]


def _record_entity_counts(job: _ArchiveImport, pipeline_id: Any, result_run: dict, run_options: Any) -> None:
    """Add what one entity run created / failed / skipped (already existing) to its pipeline's outcome."""
    outcome = _outcome(job, pipeline_id)
    outcome["entitiesRun"] = True
    counts = result_run.get("result") if isinstance(result_run.get("result"), dict) else None
    if counts is None or "successful" not in counts:
        # The run itself failed (exception, refused before counting): nothing is known to be created.
        if not result_run.get("success"):
            outcome["runErrors"] += 1
        return
    outcome["created"] += int(counts.get("successful") or 0)
    outcome["failed"] += int(counts.get("failed") or 0)
    report = getattr(run_options, "report", None)
    skipped = len(getattr(report, "skipped_existing", None) or [])
    outcome["skipped"] += skipped
    if counts.get("errors") and not counts.get("successful") and not counts.get("failed") and not skipped:
        outcome["runErrors"] += 1


def _record_schedule_failures(job: _ArchiveImport, entities: _PipelineEntities, src_schema: Any,
                              tgt_schema: Any) -> None:
    """Schedules of the backup this entity run could not restore: the pipeline is PARTIAL, not FULL. Nothing is
    recorded when the backup has no schedules or scheduling is off (preferences / plan), e.g. on a Core Hub
    without the scheduler: there is nothing lost to report then."""
    failures = commons.take_schedule_failures()
    if not failures:
        return
    _outcome(job, entities.pipeline_id)["schedulesNotRestored"] = \
        _outcome(job, entities.pipeline_id).get("schedulesNotRestored", 0) + len(failures)
    job.errors.append(f"{entities.name}: schedules not restored for {src_schema}->{tgt_schema}: "
                      f"{'; '.join(failures)}. The entities are in place: recreate these schedules in Core Hub")


def _outcome_is_full(outcome: Dict[str, Any]) -> bool:
    """A pipeline is fully imported when its entities ran, none failed, and nothing else went wrong."""
    return (outcome.get("entitiesRun") and not outcome.get("failed") and not outcome.get("runErrors")
            and not outcome.get("errors") and not outcome.get("credentialsPending")
            and not outcome.get("schedulesNotRestored"))


def _record_offline_outcome(job: _ArchiveImport, entities: _PipelineEntities, src_schema: Any, tgt_schema: Any,
                            result_run: dict) -> None:
    """For a pipeline with credentials pending: what was saved without discovery and what Core Hub refused."""
    if not entities.pending_agent_types:
        return
    label = f"{entities.name} {src_schema}->{tgt_schema}"
    payload = result_run.get("result") if isinstance(result_run.get("result"), dict) else result_run
    report = payload.get("report") if isinstance(payload, dict) else None
    offline = list(getattr(report, "offline_entities", None) or [])
    if offline:
        job.offline_report.append(f"{label}: {len(offline)} entities saved without discovery: {', '.join(offline)}")
    # What Core Hub refused is already in job.errors, with its message (entity creation error ...).


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


def _attempt_entities_when_pending() -> bool:
    """GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING=true: still send each entity while an agent's credentials are pending
    (the behaviour before; Core Hub 2.3.1 refuses them all while the agents are unreachable)."""
    return os.getenv("GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING", "").strip().lower() in ("1", "true", "yes", "on")


def _yaml_table_count(yaml_path: Path, schema_pairs: list) -> Optional[int]:
    """How many entities (tables) the schema pairs of a pipeline YAML hold; None when it cannot be told.

    A schema's ``tables`` is ``{whitelist: [names], blacklist: [names], custom: {name: settings}}``: its tables
    are the whitelisted and custom names (a custom table is usually whitelisted too), less the blacklisted ones.
    An empty or missing whitelist (allow-all discovery), or a schema with neither whitelist nor custom, takes
    every discovered table: unknown without discovery."""
    try:
        config = load_yaml_config(str(yaml_path)) or {}
    except Exception:  # pylint: disable=broad-except
        return None
    schemas = config.get("schemas") if isinstance(config.get("schemas"), dict) else config
    total = 0
    for src_schema, _tgt in schema_pairs:
        count = _schema_table_count(schemas, src_schema)
        if count is None:
            return None
        total += count
    return total


def _schema_table_count(schemas: Any, src_schema: str) -> Optional[int]:
    """How many tables schema ``src_schema`` of a YAML holds (see :func:`_yaml_table_count`); None when unknown.

    An empty or missing ``whitelist`` means the import takes every discovered table (custom only customizes
    some of them): the total cannot be told from the YAML alone, so None — otherwise a report would say
    ``9 of 2`` when discovery created nine 1:1 tables plus two named customs.
    """
    schema = schemas.get(src_schema) if isinstance(schemas, dict) else None
    tables = schema.get("tables") if isinstance(schema, dict) else None
    if isinstance(tables, list):
        return len(tables)
    if not isinstance(tables, dict):
        return None
    whitelist = tables.get("whitelist")
    if not whitelist:
        return None
    names = {str(name) for name in map(_whitelist_name, whitelist) if name}
    names.update(str(n) for n in (tables.get("custom") or {}))
    if not names:
        return None
    names.difference_update(str(n) for n in tables.get("blacklist") or [] if not isinstance(n, dict))
    return len(names)


def _whitelist_name(item: Any) -> Any:
    return item.get("name") or item.get("sourceName") if isinstance(item, dict) else item


def _record_entities_not_attempted(job: _ArchiveImport, name: str, yaml_path: Path, schema_pairs: list,
                                   pipeline_id: Any, pending: frozenset) -> None:
    """Credentials pending: no entity is sent (Core Hub refuses each while the agents are unreachable).

    Counted in the outcome only; the report states it once, on the pipeline's line (see ``_report_lines``)."""
    count = _yaml_table_count(yaml_path, schema_pairs)
    outcome = _outcome(job, pipeline_id)
    outcome["notAttempted"] = outcome.get("notAttempted", 0) + (count or 0)
    outcome["notAttemptedUnknown"] = count is None
    logger.info("%s: agents not reachable (%s credentials pending): %s entities not attempted", name,
                ", ".join(sorted(pending)), count if count is not None else "the")


_LABELLED = re.compile(r"^(?P<label>[^:\s][^:]{0,200}?): (?P<reason>.+)$", re.S)


def _condense_errors(errors: list, max_labels: int = 3) -> list:
    """Group the ``"<table>: <reason>"`` errors of an entity run by reason: one line per reason with its count
    (``reason x9 tables (A, B, C, ...)``), in first-seen order; identical lines are counted too."""
    groups: Dict[str, list] = {}
    for error in errors or []:
        label, reason = _label_and_reason(str(error))
        groups.setdefault(reason, []).append(label)
    return [_condensed_line(reason, labels, max_labels) for reason, labels in groups.items()]


def _label_and_reason(text: str) -> tuple:
    """The table label and reason of a ``"<table>: <reason>"`` error (label None when it has none)."""
    match = _LABELLED.match(text)
    if not match:
        return None, text
    label, reason = match.group("label"), match.group("reason")
    if label:
        # the reason repeats its table (" of SRC.T"); drop it so the same rule on many tables groups.
        reason = reason.replace(f" of {label}", "")
    return label, reason


def _condensed_line(reason: str, labels: list, max_labels: int) -> str:
    named = [l for l in dict.fromkeys(labels) if l]
    if len(labels) == 1:
        return f"{named[0]}: {reason}" if named else reason
    if not named:
        return f"{reason} x{len(labels)}"
    shown = ", ".join(named[:max_labels]) + (", ..." if len(named) > max_labels else "")
    return f"{reason} x{len(labels)} tables ({shown})"


def _plan_choices(job: _ArchiveImport, pipeline_plan: Optional[import_plan.PipelinePlan]) -> Optional[Dict[str, Any]]:
    """The entity-run options a plan sets for one pipeline; None when it sets none (Import All)."""
    if pipeline_plan is None or job.plan.empty:
        return None
    choices = {
        "on_existing": job.plan.on_existing_entity(pipeline_plan),
        "validate": job.plan.defaults.get("validate"),
        "strict_validate": job.plan.defaults.get("strictValidate"),
        "remap": pipeline_plan.remap if pipeline_plan.remap else None,
    }
    return choices if any(v is not None for v in choices.values()) else None


def _create_imported_entities(job: _ArchiveImport, name: str, yaml_path: Path, new_pipeline_id: Any, new_pipeline_name: Any,
                              default_on_existing: Optional[str] = None,
                              pipeline_plan: Optional[import_plan.PipelinePlan] = None) -> None:
    """Recreate the entities (and UDFs) of one imported pipeline from its YAML."""
    # Determine schemas and optional type hints from YAML
    schema_pairs = extract_all_schemas_from_yaml(str(yaml_path))
    if not schema_pairs:
        job.errors.append(f"{name}: no schemas found in YAML; skipping entity creation")
        return

    prefs = _entity_preferences(job, pipeline_plan)
    pending = _pending_agent_types(job, new_pipeline_id)
    entities = _pipeline_entities(job, name, yaml_path, new_pipeline_id, new_pipeline_name, prefs, pending,
                                  default_on_existing, pipeline_plan)
    if "TARGET" in pending and prefs["createTables"]:
        _mark_tables_not_created(job, new_pipeline_id)
    total = _yaml_table_count(yaml_path, schema_pairs)
    if total is not None:
        _outcome(job, new_pipeline_id, new_pipeline_name)["total"] = total

    overall_success = True
    if pending and not _attempt_entities_when_pending():
        _record_entities_not_attempted(job, name, yaml_path, schema_pairs, new_pipeline_id, pending)
        overall_success = False
        schema_pairs = []
    for src_schema, tgt_schema in schema_pairs:
        if not _create_schema_pair_entities(job, entities, src_schema, tgt_schema):
            overall_success = False
    _finish_imported_pipeline(name, new_pipeline_id, new_pipeline_name, pending, overall_success)


def _entity_preferences(job: _ArchiveImport, pipeline_plan: Optional[import_plan.PipelinePlan]) -> Dict[str, Any]:
    """The scheduling / table-creation preferences, narrowed by the plan's parts for this pipeline."""
    prefs = dict(state.preferences())
    if pipeline_plan is not None:
        prefs["enableScheduling"] = prefs["enableScheduling"] and job.plan.part(pipeline_plan, "schedules")
        prefs["createTables"] = prefs["createTables"] and job.plan.part(pipeline_plan, "createTables")
    return prefs


def _pipeline_entities(job: _ArchiveImport, name: str, yaml_path: Path, new_pipeline_id: Any,
                       new_pipeline_name: Any, prefs: Dict[str, Any], pending: Any,
                       default_on_existing: Optional[str],
                       pipeline_plan: Optional[import_plan.PipelinePlan]) -> _PipelineEntities:
    src_type_hint, tgt_type_hint = extract_schema_types_from_yaml(str(yaml_path))
    return _PipelineEntities(
        name=name,
        yaml_path=yaml_path,
        pipeline_id=new_pipeline_id,
        pipeline_name=new_pipeline_name,
        source_type=src_type_hint or "SQL",
        target_type=tgt_type_hint or "SQL",
        enable_scheduling=prefs["enableScheduling"],
        create_tables=prefs["createTables"] and "TARGET" not in pending,
        pending_agent_types=pending,
        default_on_existing=default_on_existing,
        compile_udfs=pipeline_plan is None or job.plan.part(pipeline_plan, "udfs"),
        plan_choices=_plan_choices(job, pipeline_plan),
    )


def _mark_tables_not_created(job: _ArchiveImport, new_pipeline_id: Any) -> None:
    for p in job.credentials_pending:
        if p.get("pipelineId") == str(new_pipeline_id) and p.get("agentType") == "TARGET":
            p["tablesNotCreated"] = True


def _finish_imported_pipeline(name: str, new_pipeline_id: Any, new_pipeline_name: Any, pending: Any,
                              overall_success: bool) -> None:
    """Mark the imported pipeline configured, unless credentials are pending or its entities had errors."""
    if pending:
        # Not marked complete: the pipeline cannot run before its credentials are entered again, and Core Hub's
        # draft state is what tells the user it still needs configuring.
        logger.warning("Import-all: pipeline %s (%s) left in draft: credentials pending for %s",
                       new_pipeline_name, new_pipeline_id, ", ".join(sorted(pending)))
    elif not overall_success:
        logger.warning(
            "Import-all: entity creation had errors for pipeline %s (%s) from %s",
            new_pipeline_name,
            new_pipeline_id,
            name,
        )
    else:
        _mark_configuration_completed(new_pipeline_id, new_pipeline_name)


def _restore_imported_function_collection(job: _ArchiveImport, name: str, old_pipeline_id: Optional[str], new_pipeline_id: Any) -> bool:
    """Publish and install the function collection of the backup on the new pipeline; False when CoreHub refuses it."""
    try:
        directory = function_collection.find_function_collection_dir([job.udf_root], old_pipeline_id)
        if directory is None:
            return True
        function_collection.restore_function_collection(
            state.token,
            new_pipeline_id,
            function_collection.read_function_collection_dir(directory),
            fetch=corehub.fetch_core_hub,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to restore the function collection of %s on pipeline %s", name, new_pipeline_id)
        job.errors.append(f"{name}: function collection not restored, so its entities were not created: {exc}")
        return False
    return True


def _pipeline_plan(job: _ArchiveImport, old_pipeline_id: Any) -> Optional[import_plan.PipelinePlan]:
    """The plan of one backup pipeline; None for Import All (an empty plan)."""
    if job.plan.empty and str(old_pipeline_id) not in job.agent_passwords:
        return None
    return job.plan.pipeline(old_pipeline_id)


def _planned_agents(job: _ArchiveImport, name: str, agents_for_pipeline: list,
                    pipeline_plan: Optional[import_plan.PipelinePlan]) -> list:
    """The agents of one pipeline with the plan's connection edits and passwords (a copy: the backup's agents
    stay as they are for the other pipelines). PlanError / AgentOverrideError on an unknown agent."""
    passwords = job.agent_passwords.get(pipeline_plan.old_id, {}) if pipeline_plan is not None else {}
    if pipeline_plan is None or not (pipeline_plan.agents or passwords):
        return agents_for_pipeline
    overrides, env = import_plan.agent_overrides_for(pipeline_plan, passwords, job.plan_env)
    agents = copy.deepcopy(agents_for_pipeline)
    for line in agent_overrides.apply(agents, overrides, env=env):
        job.notes.append(f"{name}: {line}")
    return agents


def _planned_yaml_text(job: _ArchiveImport, name: str, yaml_text: str,
                       pipeline_plan: Optional[import_plan.PipelinePlan]) -> str:
    """The pipeline YAML with the plan's selection, overrides and skipped parts; unchanged without a plan."""
    if pipeline_plan is None:
        return yaml_text
    config = yaml.safe_load(yaml_text)
    planned, notes = import_plan.apply_to_yaml(config, job.plan, pipeline_plan)
    job.notes.extend(f"{name}: {line}" for line in notes)
    if planned is config:
        return yaml_text
    return yaml.safe_dump(planned, sort_keys=False, allow_unicode=True)


def _restore_planned_function_collection(job: _ArchiveImport, name: str, old_pipeline_id: Any, new_pipeline_id: Any,
                                         pipeline_plan: Optional[import_plan.PipelinePlan]) -> bool:
    """The function collection restore, unless the plan leaves it out (then only a note)."""
    if pipeline_plan is not None and not job.plan.part(pipeline_plan, "functionCollection"):
        if job.udf_root is not None and function_collection.find_function_collection_dir(
                [job.udf_root], old_pipeline_id) is not None:
            job.notes.append(f"{name}: function collection not restored (plan: functionCollection off); entities "
                             "calling its functions are refused unless it is already installed on the pipeline")
        return True
    return _restore_imported_function_collection(job, name, old_pipeline_id, new_pipeline_id)


def _import_base_name(meta: Any, embedded_name: Optional[str], old_pipeline_id: Any, pipeline_plan: Any) -> Any:
    """The name of the recreated pipeline: the plan's, else the backup metadata's, the YAML's or a default."""
    base_name = meta.get("pipelineName") if isinstance(meta, dict) else None
    base_name = base_name or embedded_name or _default_pipeline_name(old_pipeline_id)
    if pipeline_plan is not None and pipeline_plan.name:
        base_name = pipeline_plan.name
    return base_name


def _reject_masked_agents(job: _ArchiveImport, name: str, agents_for_pipeline: list) -> bool:
    """Record an error and return True when an agent of the pipeline still has a masked password."""
    masked = [str(a.get("agentTag") or a.get("agentId") or "?") for a in agents_for_pipeline
              if isinstance(a, dict) and _has_masked_password(a)]
    if not masked:
        return False
    job.errors.append(f"{name}: not imported: the password of agent(s) {', '.join(masked)} is masked (*******) "
                      "in the backup; give it (Customize step, agent_passwords, passwordRef or "
                      "GS_IMPORT_AGENT_PASSWORD_<TAG>) - no pipeline was created")
    return True


def _pipeline_import_options(job: _ArchiveImport, pipeline_plan: Any) -> tuple:
    """(on_existing_name, cleanup_on_failure) for one pipeline: the plan's values, else the environment's."""
    on_existing_name = (job.plan.on_existing_pipeline(pipeline_plan) if pipeline_plan is not None else None) or \
        os.getenv("GS_IMPORT_ON_EXISTING_PIPELINE", "suffix").strip().lower() or "suffix"
    cleanup = job.plan.defaults.get("cleanupOnFailure")
    if cleanup is None:
        cleanup = os.getenv("GS_IMPORT_CLEANUP_ON_FAILURE", "").strip().lower() in ("1", "true", "yes")
    return on_existing_name, cleanup


def _import_archive_pipeline(job: _ArchiveImport, name: str, stem_no_ext: str, yaml_text: str) -> None:
    """Recreate one pipeline of a backup ZIP: pipeline, agents, certificates, entities and UDFs."""
    # Resolve original pipeline identity.
    old_pipeline_id, embedded_name = _resolve_pipeline_identity(yaml_text, stem_no_ext)
    pipeline_plan = _pipeline_plan(job, old_pipeline_id)

    meta = job.meta_by_old_id.get(str(old_pipeline_id)) if old_pipeline_id is not None else None
    base_name = _import_base_name(meta, embedded_name, old_pipeline_id, pipeline_plan)

    agents_for_pipeline = _agents_for_pipeline(job, name, meta)
    if agents_for_pipeline is None:
        return
    agents_for_pipeline = _planned_agents(job, name, agents_for_pipeline, pipeline_plan)
    if _reject_masked_agents(job, name, agents_for_pipeline):
        return
    on_existing_name, cleanup = _pipeline_import_options(job, pipeline_plan)

    # Create pipeline + bind agents
    try:
        cfg = {"pipelineName": base_name, "agents": agents_for_pipeline}
        result = corehub.import_pipeline_config_only(
            token=state.token,
            base_url=state.base_url,
            use_ssl=state.use_ssl,
            skip_verify=state.skip_verify,
            config=cfg,
            on_existing_name=on_existing_name,
            cleanup_on_failure=cleanup,
            continue_on_credentials_failure=job.continue_on_credentials_failure,
        )
    except corehub.PartialPipelineImportError as exc:
        # Short reason and resume hint only; the full CoreHub answer and traceback stay at debug level.
        logger.debug("Failed to import config for %s", name, exc_info=True)
        job.errors.append(f"{name}: failed to create pipeline and bind agents: {exc}")
        return
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to import config for %s", name)
        job.errors.append(f"{name}: failed to create pipeline and bind agents: {exc}")
        return

    new_pipeline_id = result.get("pipelineId")
    new_pipeline_name = result.get("pipelineName")
    job.created_pipelines.append(result)
    job.credentials_pending.extend(result.get("credentialsPending") or [])
    _outcome(job, new_pipeline_id, new_pipeline_name)["credentialsPending"] = bool(result.get("credentialsPending"))
    _merge_warnings(job, result.get("warnings"))

    # Upload certificates for agents if specified
    _upload_pipeline_certificates(job, name, agents_for_pipeline, new_pipeline_id)

    yaml_path = _materialize_import_yaml(job, name, _planned_yaml_text(job, name, yaml_text, pipeline_plan))
    if yaml_path is None:
        return

    # The function collection goes in before any UDF is compiled and any entity is saved: the UDFs may call its
    # classes and the entities bind its functions. A refused collection stops the restore of this pipeline.
    if not _restore_planned_function_collection(job, name, old_pipeline_id, new_pipeline_id, pipeline_plan):
        return

    if pipeline_plan is None:
        _create_imported_entities(job, name, yaml_path, new_pipeline_id, new_pipeline_name)
    else:
        _create_imported_entities(job, name, yaml_path, new_pipeline_id, new_pipeline_name,
                                  pipeline_plan=pipeline_plan)


def _existing_pipeline_agent(existing_agents: list, backup_agent: dict) -> Optional[dict]:
    """The agent of an existing pipeline that stands for one backup agent: same agentType (and tag when two)."""
    a_type = str(backup_agent.get("agentType") or "").upper()
    same_type = [a for a in existing_agents if str(a.get("agentType") or "").upper() == a_type]
    if len(same_type) <= 1:
        return same_type[0] if same_type else None
    tag = backup_agent.get("agentTag")
    return next((a for a in same_type if tag and a.get("agentTag") == tag), None)


_BOUND_AGENT_ID = re.compile(r"\(id=([^)]+)\)$")


def _unbind_resume_agents(pipeline_id: str, bound: list) -> list:
    """Unassign the agents a failed resume bind attached (e.g. bound, then credentials refused): left bound
    without credentials, the next resume would take them as already bound and never send their credentials."""
    unbound = []
    for entry in bound:
        match = _BOUND_AGENT_ID.search(str(entry))
        if not match:
            continue
        try:
            corehub.fetch_core_hub(f"/pipelines/{pipeline_id}/agents/{match.group(1)}", method="DELETE",
                                   token=state.token)
            unbound.append(entry)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Resume: could not unassign agent %s from pipeline %s", match.group(1), pipeline_id)
    return unbound


def _bind_missing_resume_agents(job: _ArchiveImport, name: str, agents_for_pipeline: list, existing_agents: list,
                                pipeline_id: str, pipeline_plan: Optional[import_plan.PipelinePlan]) -> bool:
    """A resumed pipeline may be half-created (e.g. only the SOURCE agent bound before a failure): bind the
    backup agents it lacks, with their credentials (plan / overrides / passwords applied), certificate and
    specific configuration, the same way a fresh import does. Agents already bound are left as they are.
    False (and an error) when a missing agent cannot be bound: the entities would all fail without it."""
    missing = [a for a in agents_for_pipeline
               if isinstance(a, dict) and _existing_pipeline_agent(existing_agents, a) is None]
    if not missing:
        return True
    missing_refs = {id(a) for a in missing}
    planned = _planned_agents(job, name, agents_for_pipeline, pipeline_plan)
    to_bind = [p for p, a in zip(planned, agents_for_pipeline) if id(a) in missing_refs]
    if _reject_masked_agents(job, name, to_bind):
        return False
    bound: list = []
    pending: list = []
    try:
        for conf_agent in to_bind:
            corehub._bind_config_agent(  # pylint: disable=protected-access
                state.token, pipeline_id, conf_agent, bound, pending, job.continue_on_credentials_failure)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to bind missing agents of resumed pipeline %s", pipeline_id)
        unbound = _unbind_resume_agents(pipeline_id, bound)
        job.errors.append(f"{name}: resume: failed to bind the missing agent(s) to pipeline {pipeline_id} "
                          f"(bound then unassigned again: {', '.join(unbound) or 'none'}): {exc}")
        return False
    job.credentials_pending.extend(pending)
    if pending:
        _outcome(job, pipeline_id, None)["credentialsPending"] = True
    _upload_pipeline_certificates(job, name, to_bind, pipeline_id)
    job.notes.append(f"{name}: resume: bound the missing agent(s) {', '.join(bound)} to pipeline {pipeline_id}")
    # Their specific configuration was sent by _bind_config_agent: do not send it twice.
    existing_agents.extend({"agentId": c.get("_agent_id"), "agentType": c.get("agentType"),
                            "agentTag": c.get("agentTag"), "_justBound": True} for c in to_bind)
    return True


def _resume_specific_configuration(job: _ArchiveImport, name: str, agents_for_pipeline: list, existing_agents: list,
                                   pipeline_id: str) -> None:
    """Send the backup's specificConfiguration of each agent again (the route the import already uses)."""
    for backup_agent in agents_for_pipeline:
        specific_conf = backup_agent.get("specificConfiguration") or {}
        if not specific_conf:
            continue
        label = f"{backup_agent.get('agentType')} agent {backup_agent.get('agentTag')}"
        existing = _existing_pipeline_agent(existing_agents, backup_agent)
        if (existing or {}).get("_justBound"):
            continue
        agent_id = (existing or {}).get("agentId") or (existing or {}).get("id")
        if not agent_id:
            job.errors.append(f"{name}: pipeline {pipeline_id} has no agent matching the backup's {label}; "
                              "its specific configuration was not sent")
            continue
        try:
            corehub.fetch_core_hub(
                f"/pipelines/{pipeline_id}/agents/{agent_id}/config/specific",
                method="PUT",
                token=state.token,
                body={"configuration": specific_conf},
            )
            job.notes.append(f"{name}: specific configuration of {label} (id {agent_id}) sent again")
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Resume: specific configuration of agent %s on pipeline %s refused", agent_id, pipeline_id)
            job.errors.append(f"{name}: specific configuration of {label} (id {agent_id}) refused: {exc}")


def _resume_pipeline_config(job: _ArchiveImport, name: str, pipeline_id: str) -> Optional[Dict[str, Any]]:
    """The config of the pipeline to resume; None (after recording the error) when it cannot be read."""
    try:
        config = corehub.fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=state.token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Resume: cannot read pipeline %s", pipeline_id)
        job.errors.append(f"{name}: cannot read pipeline {pipeline_id} to resume it: {exc}")
        return None
    if not isinstance(config, dict):
        job.errors.append(f"{name}: unexpected answer reading pipeline {pipeline_id} to resume it")
        return None
    return config


def _resumed_pipeline_name(config: Dict[str, Any], meta: Optional[Dict[str, Any]], pipeline_id: str) -> str:
    return (config.get("pipelineName") or (config.get("pipeline") or {}).get("name")
            or (meta or {}).get("pipelineName") or f"Pipeline {pipeline_id}")


def _resume_function_collection(job: _ArchiveImport, name: str, old_pipeline_id: Any, pipeline_id: str,
                                pipeline_plan: Optional[import_plan.PipelinePlan]) -> None:
    """The collection may already be installed by the first import: a refusal here is reported, not fatal."""
    errors_before = len(job.errors)
    if not _restore_planned_function_collection(job, name, old_pipeline_id, pipeline_id, pipeline_plan):
        refused = job.errors[errors_before:]
        del job.errors[errors_before:]
        job.notes.extend(f"{line} (resume: continuing, the collection may already be installed)" for line in refused)


def _resume_archive_pipeline(job: _ArchiveImport, name: str, stem_no_ext: str, yaml_text: str,
                             pipeline_id: Optional[str] = None) -> None:
    """Resume an existing pipeline (e.g. a draft left when its credentials were refused, since fixed in Core Hub):
    no pipeline, agent, certificate or credentials is created or sent; only the specific configuration, the
    function collection, the entities (existing ones skipped unless GS_IMPORT_ON_EXISTING says otherwise) and
    the UDFs of the backup are restored."""
    old_pipeline_id, _embedded_name = _resolve_pipeline_identity(yaml_text, stem_no_ext)
    pipeline_plan = _pipeline_plan(job, old_pipeline_id)
    meta = job.meta_by_old_id.get(str(old_pipeline_id)) if old_pipeline_id is not None else None
    agents_for_pipeline = _agents_for_pipeline(job, name, meta)
    if agents_for_pipeline is None:
        return

    pipeline_id = str(pipeline_id or job.resume_pipeline_id)
    config = _resume_pipeline_config(job, name, pipeline_id)
    if config is None:
        return
    existing_agents = [a for a in (config.get("agents") or []) if isinstance(a, dict)]
    pipeline_name = _resumed_pipeline_name(config, meta, pipeline_id)
    job.created_pipelines.append({"pipelineId": pipeline_id, "pipelineName": pipeline_name, "resumed": True})
    _outcome(job, pipeline_id, pipeline_name, resumed=True)
    job.notes.append(f"{name}: resuming existing pipeline {pipeline_name!r} (id {pipeline_id}) "
                     f"from backup pipeline {old_pipeline_id}: pipeline and its bound agents (with their credentials) "
                     f"left as they are; backup agents it lacks are bound")

    if not _bind_missing_resume_agents(job, name, agents_for_pipeline, existing_agents, pipeline_id, pipeline_plan):
        return
    _resume_specific_configuration(job, name, agents_for_pipeline, existing_agents, pipeline_id)

    yaml_path = _materialize_import_yaml(job, name, _planned_yaml_text(job, name, yaml_text, pipeline_plan))
    if yaml_path is None:
        return
    _resume_function_collection(job, name, old_pipeline_id, pipeline_id, pipeline_plan)
    if pipeline_plan is None:
        _create_imported_entities(job, name, yaml_path, pipeline_id, pipeline_name, default_on_existing="skip")
    else:
        _create_imported_entities(job, name, yaml_path, pipeline_id, pipeline_name, default_on_existing="skip",
                                  pipeline_plan=pipeline_plan)


def _resume_candidates(job: _ArchiveImport, members: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The pipeline YAML(s) of the archive a resume restores: the only one, or the one of resume_source_pipeline_id."""
    if job.resume_source_pipeline_id:
        wanted = str(job.resume_source_pipeline_id)
        members = [m for m in members if str(_resolve_pipeline_identity(m[2], m[1])[0]) == wanted]
        if not members:
            job.errors.append(f"resume: the archive has no pipeline YAML of backup pipeline {wanted}")
    elif len(members) > 1:
        ids = ", ".join(str(_resolve_pipeline_identity(m[2], m[1])[0]) for m in members)
        job.errors.append(f"resume: the archive holds {len(members)} pipelines ({ids}); set "
                          "GS_IMPORT_RESUME_FROM_PIPELINE (or --resume-from-pipeline) to the one to resume from")
        return []
    elif not members:
        job.errors.append("resume: the archive holds no pipeline YAML")
    return members


def _archive_pipeline_members(contents: bytes, errors: Optional[list[str]] = None) -> list[tuple[str, str, str]]:
    """``(member name, stem, YAML text)`` of each per-pipeline YAML of a backup ZIP."""
    members: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(io.BytesIO(contents)) as zf:
        for name in zf.namelist():
            stem_no_ext = _pipeline_yaml_stem(name)
            if stem_no_ext is None:
                continue

            try:
                yaml_text = zf.read(name).decode("utf-8")
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("Failed to read YAML %s from archive", name)
                if errors is not None:
                    errors.append(f"{name}: failed to read from archive: {exc}")
                continue
            members.append((name, stem_no_ext, yaml_text))
    return members


def _import_archive_pipelines(job: _ArchiveImport) -> None:
    """Second pass: process each per-pipeline YAML and recreate pipeline + entities (or resume one pipeline)."""
    members = _archive_pipeline_members(job.contents, job.errors)

    if job.resume_pipeline_id:
        members = _resume_candidates(job, members)
    for name, stem_no_ext, yaml_text in members:
        errors_before, created_before = len(job.errors), len(job.created_pipelines)
        pipeline_plan = _pipeline_plan(job, _resolve_pipeline_identity(yaml_text, stem_no_ext)[0])
        if pipeline_plan is not None and not pipeline_plan.import_:
            job.notes.append(f"{name}: not imported (plan: import off)")
            continue
        if job.resume_pipeline_id:
            _resume_archive_pipeline(job, name, stem_no_ext, yaml_text)
        elif pipeline_plan is not None and pipeline_plan.resume_into:
            _resume_archive_pipeline(job, name, stem_no_ext, yaml_text, pipeline_id=pipeline_plan.resume_into)
        else:
            _import_archive_pipeline(job, name, stem_no_ext, yaml_text)
        if len(job.created_pipelines) > created_before:
            created = job.created_pipelines[-1]
            outcome = _outcome(job, created.get("pipelineId"), created.get("pipelineName"))
            outcome["errors"] += len(job.errors) - errors_before


def _outcome_line(outcome: Dict[str, Any]) -> str:
    parts = [f"{outcome['created']} entities created", f"{outcome['failed']} failed"]
    if outcome.get("skipped"):
        parts.append(f"{outcome['skipped']} skipped (already existing)")
    if outcome.get("runErrors"):
        parts.append(f"{outcome['runErrors']} schema run(s) refused")
    if outcome.get("notAttempted"):
        parts.append(f"{outcome['notAttempted']} not attempted (agents not reachable)")
    elif not outcome.get("entitiesRun"):
        parts.append("entity creation not reached")
    if outcome.get("schedulesNotRestored"):
        parts.append(f"{outcome['schedulesNotRestored']} schedule(s) not restored")
    if outcome.get("credentialsPending"):
        parts.append("credentials pending")
    label = f"{outcome.get('pipelineName') or '(unnamed)'} ({outcome.get('pipelineId') or '?'})"
    return f"{label}{' [resumed]' if outcome.get('resumed') else ''}: {', '.join(parts)}"


def _outcomes_summary(created_pipelines: list[dict], outcomes: Dict[str, Dict[str, Any]]) -> str:
    listed = [outcomes.get(str(p.get("pipelineId"))) or _bare_outcome(p) for p in created_pipelines]
    full = [o for o in listed if _outcome_is_full(o)]
    partial = [o for o in listed if not _outcome_is_full(o)]
    base_msg = f"Pipelines fully imported: {len(full)}; partially imported: {len(partial)}."
    if full:
        base_msg += " Fully imported: " + "; ".join(_outcome_line(o) for o in full) + "."
    if partial:
        base_msg += " PARTIAL: " + "; ".join(_outcome_line(o) for o in partial) + "."
    return base_msg


def _import_summary(created_pipelines: list[dict], errors: list[str],
                    outcomes: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Human-friendly summary message of an Export All import.

    With ``outcomes`` the pipelines are reported as fully or partially imported, with their entity counts:
    a pipeline whose entities were not created is never reported as imported."""
    if not created_pipelines:
        base_msg = NO_PIPELINES_IMPORTED
    elif outcomes is None:
        summary_parts = [
            f"{p.get('pipelineName') or '(unnamed)'} ({p.get('pipelineId') or '?'})"
            for p in created_pipelines
        ]
        base_msg = f"Imported {len(created_pipelines)} pipeline(s): {'; '.join(summary_parts)}."
    else:
        base_msg = _outcomes_summary(created_pipelines, outcomes)

    if errors:
        error_msg = " Some items encountered errors: " + "; ".join(errors)
        base_msg += error_msg
    return base_msg


def _pipeline_report_line(outcome: Dict[str, Any]) -> str:
    """``PARTIAL: Synth1MSSQL (e61466b6) - 0 of 58 entities created (58 not attempted: agents not reachable).``"""
    created, total = outcome.get("created", 0), _report_total(outcome)
    head = "Imported" if _outcome_is_full(outcome) else "PARTIAL"
    label = f"{outcome.get('pipelineName') or '(unnamed)'} ({outcome.get('pipelineId') or '?'})"
    if outcome.get("resumed"):
        label += " [resumed]"
    counts = f"{created} of {total} entities created" if total is not None else f"{created} entities created"
    details = _report_details(outcome, head)
    return f"{head}: {label} - {counts}" + (f" ({'; '.join(details)})" if details else "") + "."


def _report_total(outcome: Dict[str, Any]) -> Any:
    """The entity total of a pipeline outcome; when unknown but entities ran, the sum of the counts (or None).

    When discovery created more entities than the YAML named (empty whitelist = allow all), prefer the
    counted sum so the report never says ``9 of 2``.
    """
    total = outcome.get("total")
    counted = (outcome.get("created", 0) + outcome.get("failed", 0) + outcome.get("skipped", 0)
               + outcome.get("notAttempted", 0))
    if outcome.get("entitiesRun") and counted and (total is None or counted > total):
        return counted
    return total


def _report_details(outcome: Dict[str, Any], head: str) -> list[str]:
    details = []
    if outcome.get("failed"):
        details.append(f"{outcome['failed']} failed")
    if outcome.get("skipped"):
        details.append(f"{outcome['skipped']} already existing")
    if outcome.get("runErrors"):
        details.append(f"{outcome['runErrors']} schema run(s) refused")
    if outcome.get("notAttempted") or outcome.get("notAttemptedUnknown"):
        n = outcome.get("notAttempted") if not outcome.get("notAttemptedUnknown") else "all"
        details.append(f"{n} not attempted: agents not reachable")
    elif not outcome.get("entitiesRun") and head == "PARTIAL":
        details.append("entity creation not reached")
    return details


CREDENTIALS_PENDING_HELP = (
    "Credentials pending: Core Hub did not save the agent credentials (it refuses them while it cannot connect "
    "with them), so the pipeline stays in draft and its entities were not created. Re-enter the credentials in the "
    "Core Hub UI, or put them in an agent overrides file (GS_IMPORT_AGENT_OVERRIDES_FILE / --agent-overrides), "
    "then resume.")


def _agent_report_line(pending: Dict[str, Any]) -> str:
    """``Target mssql-ct: can't connect to 192.0.2.10:1433 (timed out).`` with what it held back."""
    kind = (pending.get("agentType") or "agent").capitalize()
    reason = pending.get("reason") or corehub.short_connection_reason(pending.get("error") or "")
    held = ["specific configuration not saved"] if pending.get("specificConfigurationError") else []
    if pending.get("tablesNotCreated"):
        held.append("target tables not created")
    return f"{kind} {pending.get('agentTag') or pending.get('agentId')}: {reason}" + \
        (f" - {', '.join(held)}" if held else "") + "."


def _next_step_line(pid: str, pending: list) -> str:
    fix = "fix connections (overrides file or Core Hub UI)" if pending else "fix the errors above"
    return f"Next: {fix}, then resume with GS_IMPORT_RESUME_PIPELINE_ID={pid} - or delete the pipeline."


def _report_lines(job: _ArchiveImport) -> list[str]:
    """The short report of an Export All import (Automator result and CLI): each fact once.

    One line per pipeline, one per agent whose credentials Core Hub refused, the other errors grouped with
    counts, and what to do next. Full Core Hub requests and responses are in the debug log only."""
    lines = [] if job.created_pipelines else [NO_PIPELINES_IMPORTED]
    pending_by_pipeline: Dict[str, list] = {}
    for p in job.credentials_pending:
        pending_by_pipeline.setdefault(str(p.get("pipelineId")), []).append(p)
    nexts = []
    for pipeline in job.created_pipelines:
        pid = str(pipeline.get("pipelineId"))
        outcome = job.outcomes.get(pid) or _bare_outcome(pipeline)
        lines.append(_pipeline_report_line(outcome))
        pending = pending_by_pipeline.pop(pid, [])
        lines.extend(_agent_report_line(p) for p in pending)
        if not _outcome_is_full(outcome):
            nexts.append(_next_step_line(pid, pending))
    for rest in pending_by_pipeline.values():
        lines.extend(_agent_report_line(p) for p in rest)
    lines.extend(_condense_errors(job.errors))
    if job.credentials_pending:
        lines.extend(job.offline_report)
        lines.append(CREDENTIALS_PENDING_HELP)
    lines.extend(nexts)
    lines.extend(job.warnings)
    return lines


def _merge_warnings(job: _ArchiveImport, warnings: Optional[list[str]]) -> None:
    """Add the warnings of a restored pipeline to the job, each text once (see ``commons.SIGN_IN_RULES``)."""
    job.warnings.extend(w for w in warnings or [] if w not in job.warnings)


def _warnings_field(job: _ArchiveImport) -> Optional[list[str]]:
    return job.warnings or None


def _bare_outcome(pipeline: dict) -> Dict[str, Any]:
    return {"pipelineId": pipeline.get("pipelineId"), "pipelineName": pipeline.get("pipelineName"),
            "resumed": bool(pipeline.get("resumed")), "created": 0, "failed": 0, "skipped": 0, "runErrors": 0,
            "errors": 0, "entitiesRun": False}


def import_status(job: _ArchiveImport) -> str:
    """``"full"`` (every pipeline fully imported), ``"partial"`` (some pipeline created/resumed but not fully
    restored) or ``"failed"`` (no pipeline imported)."""
    if not job.created_pipelines:
        return "failed"
    for p in job.created_pipelines:
        if not _outcome_is_full(job.outcomes.get(str(p.get("pipelineId"))) or _bare_outcome(p)):
            return "partial"
    return "full"


def _apply_agent_overrides(job: _ArchiveImport, agents_list: list, agent_overrides_text: Optional[str]) -> None:
    """Point the backup's agents at the overridden host/port/database/username/password; 400 on a bad mapping."""
    try:
        overrides = agent_overrides.load(agent_overrides_text)
        job.notes.extend(agent_overrides.apply(agents_list, overrides))
    except agent_overrides.AgentOverrideError as exc:
        raise HTTPException(status_code=400, detail=f"Agent overrides: {exc}") from None


def _check_plan(job: _ArchiveImport, members: list[tuple[str, str, str]]) -> None:
    """Refuse (PlanError) a plan naming a pipeline, table or agent the archive does not have, before any write."""
    plan = job.plan
    by_id = {str(_resolve_pipeline_identity(text, stem)[0]): (name, text) for name, stem, text in members}
    for old_id in [*plan.pipelines, *job.agent_passwords]:
        if old_id not in by_id:
            raise import_plan.PlanError(f"pipelines.{old_id}: no pipeline {old_id!r} in the backup"
                                        f"{import_options.suggest(old_id, list(by_id))}")
    if by_id and not any(plan.pipeline(old_id).import_ for old_id in by_id):
        raise import_plan.PlanError("the plan selects no pipeline: nothing would be imported")
    for old_id, pipeline_plan in plan.pipelines.items():
        if pipeline_plan.import_:
            _check_pipeline_plan(job, old_id, pipeline_plan, *by_id[old_id])
    for old_id in job.agent_passwords:
        if old_id not in plan.pipelines:
            _check_plan_agents(job, old_id, by_id[old_id][0], plan.pipeline(old_id))


def _check_pipeline_plan(job: _ArchiveImport, old_id: str, pipeline_plan: Any, name: str, text: str) -> None:
    if pipeline_plan.resume_into and job.resume_pipeline_id:
        raise import_plan.PlanError(f"pipelines.{old_id}.resumeInto: not with resume_pipeline_id / "
                                    "GS_IMPORT_RESUME_PIPELINE_ID (pick one)")
    config = yaml.safe_load(text)
    import_plan.check_names(config, pipeline_plan)
    # Checked whatever the plan uses (include, exclude or both): the preview refuses the same case.
    if not import_plan.selected_ids(config, pipeline_plan):
        raise import_plan.PlanError(f"pipelines.{old_id}: no table selected; set \"import\": false to leave "
                                    "the pipeline out")
    import_plan.check_target_collisions(config, pipeline_plan, old_id)
    _check_plan_agents(job, old_id, name, pipeline_plan)
    if pipeline_plan.resume_into:
        _check_resume_target(old_id, str(pipeline_plan.resume_into))


def _check_resume_target(old_id: str, pipeline_id: str) -> None:
    """Refuse a ``resumeInto`` Core Hub cannot read, before the import writes anything (globals included).
    Reads the same ``/pipelines/{id}/config`` the resume reads (GET only, every Core Hub version)."""
    try:
        config = corehub.fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=state.token)
    except Exception as exc:  # pylint: disable=broad-except
        raise import_plan.PlanError(f"pipelines.{old_id}.resumeInto: cannot read pipeline {pipeline_id!r} on "
                                    f"Core Hub: {exc}") from None
    if not isinstance(config, dict):
        raise import_plan.PlanError(f"pipelines.{old_id}.resumeInto: unexpected answer reading pipeline "
                                    f"{pipeline_id!r} on Core Hub")


def _check_plan_agents(job: _ArchiveImport, old_id: str, name: str, pipeline_plan: Any) -> None:
    scratch = _scratch_job(job)
    agents = _agents_for_pipeline(scratch, name, job.meta_by_old_id.get(old_id))
    if agents is not None:
        _planned_agents(scratch, name, agents, pipeline_plan)


def _scratch_job(job: _ArchiveImport) -> _ArchiveImport:
    """A copy of ``job`` with its own errors and notes, to dry-run the agent selection of a plan check."""
    return cast(_ArchiveImport, dataclass_replace(job, errors=[], notes=[]))


def _run_archive_import(contents: bytes, agent_overrides_text: Optional[str] = None,
                        resume_pipeline_id: Optional[str] = None,
                        resume_source_pipeline_id: Optional[str] = None,
                        plan: Optional[import_plan.ImportPlan] = None,
                        agent_passwords: Optional[Dict[str, Dict[str, str]]] = None,
                        plan_env: Any = None) -> _ArchiveImport:
    """Run an Export All ZIP import and return its job (the Automator route and the CLI share it).

    ``plan`` (a custom import, this run only; None or empty: Import All), ``agent_passwords`` ({backup pipeline
    id: {agentTag|agentId: password}}, kept in memory) and ``plan_env`` (where passwordRef "env:NAME" is read).
    One import at a time: 409 while another runs."""
    with _exclusive_import():
        return _run_archive_import_locked(contents, agent_overrides_text, resume_pipeline_id,
                                          resume_source_pipeline_id, plan, agent_passwords, plan_env)


def _env_secret_values(plan: Optional[import_plan.ImportPlan], plan_env: Any,
                       agent_overrides_text: Optional[str] = None) -> list[str]:
    """The agent passwords an import may read from the environment, to hide them from the logs: the plan's
    passwordRef "env:NAME" values, every GS_IMPORT_AGENT_PASSWORD_* and the variables named under passwordEnv."""
    env = os.environ if plan_env is None else plan_env
    names = _plan_password_env_names(plan) | _override_password_env_names(agent_overrides_text)
    values = [str(value)
              for source in {id(env): env, id(os.environ): os.environ}.values()
              for name, value in source.items()
              if name in names or str(name).startswith(agent_overrides.PASSWORD_ENV_PREFIX)]
    return [v for v in values if v]


def _plan_password_env_names(plan: Optional[import_plan.ImportPlan]) -> set[str]:
    """The NAME of every passwordRef "env:NAME" of the plan."""
    refs = (str((entry or {}).get("passwordRef") or "")
            for pipeline_plan in (plan.pipelines.values() if plan else ())
            for entry in (pipeline_plan.agents or {}).values())
    return {ref[4:] for ref in refs if ref.startswith("env:")}


def _override_password_env_names(agent_overrides_text: Optional[str]) -> set[str]:
    """The variables the agent overrides name under passwordEnv (none when they do not load)."""
    names: set[str] = set()
    try:
        for entry in agent_overrides.load(agent_overrides_text).values():
            if entry.get("passwordEnv"):
                names.add(str(entry["passwordEnv"]))
    except Exception:  # pylint: disable=broad-except  # reported where the overrides are applied
        pass
    return names


def _run_archive_import_locked(contents: bytes, agent_overrides_text: Optional[str],
                               resume_pipeline_id: Optional[str], resume_source_pipeline_id: Optional[str],
                               plan: Optional[import_plan.ImportPlan],
                               agent_passwords: Optional[Dict[str, Dict[str, str]]],
                               plan_env: Any) -> _ArchiveImport:
    plan = plan or import_plan.ImportPlan()
    agent_passwords = {str(k): dict(v) for k, v in (agent_passwords or {}).items()}
    owner = object()
    import_plan.SECRETS.add(owner, list(import_plan.secret_values(agent_passwords))
                            + _env_secret_values(plan, plan_env, agent_overrides_text))
    try:
        return _run_archive_import_body(contents, agent_overrides_text, resume_pipeline_id,
                                        resume_source_pipeline_id, plan, agent_passwords, plan_env)
    finally:
        import_plan.SECRETS.discard(owner)


def _arg_or_env(value: Optional[str], env_name: str) -> Optional[str]:
    """``value``, else the environment variable ``env_name``, stripped; None when blank."""
    return (value or os.getenv(env_name) or "").strip() or None


def _check_plan_or_400(job: _ArchiveImport, contents: bytes) -> None:
    try:
        _check_plan(job, _archive_pipeline_members(contents))
    except (import_plan.PlanError, agent_overrides.AgentOverrideError) as exc:
        raise HTTPException(status_code=400, detail=f"Import plan: {exc}") from exc


def _run_archive_import_body(contents: bytes, agent_overrides_text: Optional[str],
                             resume_pipeline_id: Optional[str], resume_source_pipeline_id: Optional[str],
                             plan: import_plan.ImportPlan, agent_passwords: Dict[str, Dict[str, str]],
                             plan_env: Any) -> _ArchiveImport:
    resume_pipeline_id = _arg_or_env(resume_pipeline_id, "GS_IMPORT_RESUME_PIPELINE_ID")
    resume_source_pipeline_id = _arg_or_env(resume_source_pipeline_id, "GS_IMPORT_RESUME_FROM_PIPELINE")
    udf_root = _extract_import_udf_sources(contents)

    # First pass: find and parse agents-config.yaml for shared agents + metadata
    agents_text = _read_agents_config_text(contents, "import-all")
    # Masked passwords: a resume sends no credentials; a custom import may give them (checked per pipeline).
    custom = bool(resume_pipeline_id or not plan.empty or agent_passwords)
    agents_config, agents_list = _parse_agents_config(agents_text, "import-all", allow_masked=True)

    # Build lookup for original pipeline metadata, if present
    job = _ArchiveImport(
        contents=contents,
        agents_list=agents_list,
        meta_by_old_id=_pipeline_meta_by_old_id(agents_config),
        udf_root=udf_root,
        continue_on_credentials_failure=corehub.continue_on_credentials_failure_requested(),
        resume_pipeline_id=resume_pipeline_id,
        resume_source_pipeline_id=resume_source_pipeline_id,
        plan=plan,
        agent_passwords=agent_passwords,
        plan_env=os.environ if plan_env is None else plan_env,
    )
    # Before any credentials are sent (and before the globals, so a bad mapping changes nothing).
    _apply_agent_overrides(job, agents_list, agent_overrides_text)
    if not custom and _has_masked_password(agents_list):
        # Import All: every password must be in the backup (or in GS_IMPORT_AGENT_OVERRIDES / _PASSWORD_<TAG>)
        raise HTTPException(status_code=400, detail=MASKED_PASSWORDS)
    if not plan.empty or agent_passwords:
        _check_plan_or_400(job, contents)
    for line in job.notes:
        logger.info(line)

    if not resume_pipeline_id:
        # A resume touches one existing pipeline only: users, OIDC, notifications and access are left alone.
        _restore_archive_globals(contents, job.errors, plan)

    try:
        _import_archive_pipelines(job)
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Unexpected error during import-all processing")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if not resume_pipeline_id:
        _restore_archive_access(contents, job.errors, plan)
    return job


def _import_archive(contents: bytes, agent_overrides_text: Optional[str] = None,
                    resume_pipeline_id: Optional[str] = None,
                    plan: Optional[import_plan.ImportPlan] = None,
                    agent_passwords: Optional[Dict[str, Dict[str, str]]] = None) -> ApiMessage:
    """Recreate the pipelines, agents, entities, UDFs and global configs of an Export All ZIP.

    ``success`` is False when a pipeline is only partially imported (entities failed or not created), or when
    nothing was imported and something went wrong."""
    job = _run_archive_import(contents, agent_overrides_text, resume_pipeline_id, plan=plan,
                              agent_passwords=agent_passwords)
    status = import_status(job)
    report = _report_lines(job)
    logger.debug("Import report, full details: %s", "; ".join(job.errors + _credentials_pending_lines(job)))
    logs = job.notes + (report if job.errors or job.credentials_pending else [])
    return ApiMessage(
        success=status == "full" or (status == "failed" and not job.errors and not job.credentials_pending),
        message="\n".join(report),
        logs=logs if logs else None,
        planUsed=None if plan is None or plan.empty else plan.to_dict(),
        warnings=_warnings_field(job),
    )


def _credentials_pending_lines(job: _ArchiveImport) -> list[str]:
    """The final report of the agents whose credentials must be entered again, and their entities."""
    lines = []
    for pending in job.credentials_pending:
        lines.append(
            f"CREDENTIALS PENDING: pipeline {pending['pipelineId']}, {pending['agentType']} agent "
            f"{pending.get('agentTag')} (id {pending['agentId']}): Core Hub refused the credentials "
            f"({pending['error']}). Re-enter them in Core Hub; the pipeline is left in draft.")
        if pending.get("specificConfigurationError"):
            lines.append(f"  specific configuration of agent {pending['agentId']} not saved either: "
                         f"{pending['specificConfigurationError']}")
    if job.credentials_pending:
        lines.extend(job.offline_report)
        if _attempt_entities_when_pending():
            lines.append("Core Hub 2.3.1 refuses entities while the agents are unreachable (400 \"a modified entity "
                         "is saved only when its keys can be checked\"); older hubs are UNVERIFIED.")
    return lines


async def import_all(
    file: Optional[UploadFile] = File(None),
    agent_overrides: Optional[str] = Form(None),
    resume_pipeline_id: Optional[str] = Form(None),
    plan: Optional[str] = Form(None),
    upload_id: Optional[str] = Form(None, alias="uploadId"),
    agent_passwords: Optional[str] = Form(None),
) -> ApiMessage:
    """Validate a full backup ZIP (pipelines + agents-config).

    The ZIP is expected to come from the Export All feature and contain
    at least an agents-config.yaml file. Agents are provisioned via CoreHub API.

    ``agent_overrides`` (optional form field, JSON; also GS_IMPORT_AGENT_OVERRIDES[_FILE]) points agents at
    another host/port/database/username; ``resume_pipeline_id`` (also GS_IMPORT_RESUME_PIPELINE_ID) restores
    the specific configuration, entities and UDFs into that existing pipeline instead of creating one.

    Customizable import: ``plan`` (the plan JSON, this run only; see import_plan.py), ``uploadId`` (a backup
    analysed by /api/import/analyze, instead of ``file``; deleted after the import) and ``agent_passwords``
    (JSON ``{backup pipeline id: {agentTag or agentId: password}}``, kept in memory only). Without them it is
    the Import All of before. One import at a time (409).
    """

    _require_session()

    loaded_plan = _load_plan_or_400(plan) if plan is not None else None
    passwords = _passwords_or_400(agent_passwords)
    if upload_id:
        contents = load_import_upload(upload_id)
    elif file is not None:
        contents = await _read_nonempty_upload(file)
    else:
        raise HTTPException(status_code=400, detail="Upload a backup ZIP (file) or give the uploadId of an analysis.")

    prune_workspace_sessions()
    try:
        # in a worker thread: the event loop keeps serving (a concurrent import gets its 409 at once)
        result = await run_in_threadpool(functools.partial(
            _import_archive, contents, agent_overrides_text=agent_overrides,
            resume_pipeline_id=resume_pipeline_id, plan=loaded_plan, agent_passwords=passwords))
    except HTTPException as exc:
        # busy (409) or a plan refused before any write (400): the analysed backup stays for another try
        if upload_id and exc.status_code not in (400, 409):
            delete_import_upload(upload_id)
        raise
    except Exception:
        if upload_id:
            delete_import_upload(upload_id)
        raise
    if upload_id:
        delete_import_upload(upload_id)
    return result


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


# -- customizable import: stored uploads, analysis, preview ---------------------------------------------------

IMPORT_SESSION_TTL_SECONDS = 3600
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")


def _import_sessions_root() -> Path:
    root = get_workspace_root() / "import-sessions"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)
    return root


def prune_import_sessions(ttl_seconds: int = IMPORT_SESSION_TTL_SECONDS) -> None:
    """Delete the stored uploads older than the TTL (best effort)."""
    try:
        cutoff = time.time() - ttl_seconds
        for child in _import_sessions_root().iterdir():
            with contextlib.suppress(OSError):
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
    except Exception:  # pylint: disable=broad-except
        logger.debug("Could not prune the stored import uploads", exc_info=True)


IMPORT_PRUNE_INTERVAL_SECONDS = 300
_import_pruner_lock = threading.Lock()
_import_pruner_started = False


def _start_import_pruner() -> None:
    """Delete expired uploads every few minutes, not only when the next analysis or import comes (an upload
    analysed and abandoned would otherwise stay on disk until then). One daemon thread per process."""
    global _import_pruner_started  # pylint: disable=global-statement
    with _import_pruner_lock:
        if _import_pruner_started:
            return
        _import_pruner_started = True

    def loop():
        while True:
            time.sleep(IMPORT_PRUNE_INTERVAL_SECONDS)
            prune_import_sessions()

    threading.Thread(target=loop, name="import-upload-pruner", daemon=True).start()


def import_upload_max_bytes() -> int:
    """The largest backup ZIP /api/import/analyze accepts: GS_IMPORT_MAX_UPLOAD_MB (default 512 MB)."""
    try:
        megabytes = float(os.environ.get("GS_IMPORT_MAX_UPLOAD_MB", "512"))
    except ValueError:
        megabytes = 512.0
    return int(max(megabytes, 1) * 1024 * 1024)


async def _read_capped_upload(file: UploadFile, max_bytes: int) -> bytes:
    """The uploaded file, read in chunks; 413 as soon as it exceeds ``max_bytes``, 400 when empty."""
    chunks, size = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail=f"The backup is larger than {max_bytes // (1024 * 1024)} MB "
                                                        f"(GS_IMPORT_MAX_UPLOAD_MB).")
        chunks.append(chunk)
    if not size:
        raise HTTPException(status_code=400, detail=UPLOADED_FILE_EMPTY)
    return b"".join(chunks)


def store_import_upload(contents: bytes) -> str:
    """Keep an analysed backup ZIP for its preview and import: owner-only (0700 dir, 0600 file), one hour."""
    prune_import_sessions()
    _start_import_pruner()
    upload_id = uuid.uuid4().hex
    session = _import_sessions_root() / upload_id
    session.mkdir(mode=0o700)
    os.chmod(session, 0o700)
    path = session / "archive.zip"
    fd = os.open(path, functools.reduce(operator.or_, (os.O_WRONLY, os.O_CREAT, os.O_EXCL)), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)
    return upload_id


def load_import_upload(upload_id: Optional[str]) -> bytes:
    """The stored ZIP of an analysis; 404 when unknown or expired."""
    prune_import_sessions()
    if not upload_id or not _UPLOAD_ID.match(str(upload_id)):
        raise HTTPException(status_code=400, detail="Invalid uploadId.")
    path = _import_sessions_root() / upload_id / "archive.zip"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The analysed backup has expired; analyse it again.")
    return path.read_bytes()


def delete_import_upload(upload_id: Optional[str]) -> None:
    if upload_id and _UPLOAD_ID.match(str(upload_id)):
        shutil.rmtree(_import_sessions_root() / upload_id, ignore_errors=True)


def _agent_summary(agent: Any, password: str) -> Dict[str, Any]:
    creds = agent.get("hostCredentials") if isinstance(agent.get("hostCredentials"), dict) else {}
    return {
        "agentType": agent.get("agentType"),
        "agentTag": agent.get("agentTag"),
        "agentId": agent.get("agentId") or agent.get("id"),
        "host": creds.get("host"),
        "port": creds.get("port"),
        "database": creds.get("databaseName"),
        "username": creds.get("username"),
        "password": password,
    }


def _agent_ref(agent: Any) -> tuple:
    return (str(agent.get("agentTag") or ""), str(agent.get("agentId") or agent.get("id") or ""))


def _apply_preview_overrides(agents_list: list, errors: list[str]) -> set:
    """Apply the agent overrides an import would apply (GS_IMPORT_AGENT_OVERRIDES[_FILE], --agent-overrides and
    GS_IMPORT_AGENT_PASSWORD_<TAG>) to the parsed agents, so a dry run reports what the import would really send.
    Returns the refs of the agents whose password the overrides gave; no password leaves this function."""
    before = {_agent_ref(a): (a.get("hostCredentials") or {}).get("password")
              for a in agents_list if isinstance(a, dict)}
    try:
        agent_overrides.apply(agents_list, agent_overrides.load())
    except agent_overrides.AgentOverrideError as exc:
        errors.append(f"Agent overrides: {exc}")
        return set()
    return {_agent_ref(a) for a in agents_list if isinstance(a, dict)
            and (a.get("hostCredentials") or {}).get("password") != before.get(_agent_ref(a))
            and not _is_masked((a.get("hostCredentials") or {}).get("password"))}


def _backup_password_state(agent: Any, overridden: Optional[set] = None) -> str:
    if overridden and isinstance(agent, dict) and _agent_ref(agent) in overridden:
        return "from agent overrides"
    creds = agent.get("hostCredentials") if isinstance(agent, dict) else None
    if isinstance(creds, dict) and _is_masked(creds.get("password")):
        return "missing (masked in the backup)"
    return "from backup" if isinstance(creds, dict) and creds.get("password") else "missing"


def _has_udf(cfg: Any) -> bool:
    """Whether a custom table's settings give its target a UDF."""
    target = ((cfg.get("customProperties") or {}).get("target") if isinstance(cfg, dict) else None)
    return bool(isinstance(target, dict) and target.get("udf"))


def _yaml_parts(config: Any) -> Dict[str, int]:
    """How many UDFs, group schedules and groups a backup pipeline YAML carries (what the plan can skip)."""
    udfs = schedules = 0
    for entry in import_plan._schemas_root(config).values():  # pylint: disable=protected-access
        tables = entry.get("tables") if isinstance(entry.get("tables"), dict) else {}
        custom = tables.get("custom") if isinstance(tables.get("custom"), dict) else {}
        udfs += sum(1 for cfg in custom.values() if _has_udf(cfg))
        schedules += len(entry.get("group_schedules") or [])
    groups = config.get("groups") if isinstance(config, dict) else None
    return {"udfs": udfs, "schedules": schedules, "groups": len(groups) if isinstance(groups, (list, dict)) else 0}


def _analyze_archive(contents: bytes, apply_overrides: bool = False) -> Dict[str, Any]:
    """What a backup holds, for the Customize step: pipelines, tables, agents (no password) and the draft plan.
    ``apply_overrides`` (the dry run): report the passwords the agent overrides would give as given."""
    agents_text = _read_agents_config_text(contents, "analyze")
    agents_config, agents_list = _parse_agents_config(agents_text, "analyze", allow_masked=True)
    override_errors: list[str] = []
    overridden = _apply_preview_overrides(agents_list, override_errors) if apply_overrides else set()
    job = _ArchiveImport(contents=contents, agents_list=agents_list,
                         meta_by_old_id=_pipeline_meta_by_old_id(agents_config), udf_root=None,
                         continue_on_credentials_failure=False)
    index, errors = [], []
    for name, stem, text in _archive_pipeline_members(contents, errors):
        old_id, embedded_name = _resolve_pipeline_identity(text, stem)
        # Hand-written / fixture YAMLs without exportMetadata and without a backup_* stem
        # used to become plan key "None" (str(None)). Prefer the member stem instead.
        if not old_id:
            old_id = stem
        meta = job.meta_by_old_id.get(old_id)
        try:
            config = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"{name}: invalid YAML: {exc}")
            continue
        agents = _agents_for_pipeline(job, name, meta) or []
        index.append({
            "oldId": str(old_id),
            "name": (meta.get("pipelineName") if isinstance(meta, dict) else None) or embedded_name
            or _default_pipeline_name(old_id),
            "member": name,
            "entities": import_plan.entity_ids(config),
            "agents": [_agent_summary(a, _backup_password_state(a, overridden)) for a in agents if isinstance(a, dict)],
            "parts": _yaml_parts(config),
        })
    present = {stem: _find_config_by_stem(contents, stem) is not None
               for stem in ("users", "oidc", "user-groups", "data-access-policies")}
    globals_present = {"notifications": _find_notification_config(contents) is not None,
                       "users": present["users"] or present["oidc"] or present["user-groups"],
                       "accessPolicies": present["data-access-policies"]}
    errors.extend(job.errors)
    errors.extend(override_errors)
    return {"pipelines": index, "globals": globals_present, "errors": errors, "plan": import_plan.draft(index)}


def _existing_pipelines() -> list:
    existing = corehub.fetch_core_hub("/pipelines", token=state.token)
    return [p for p in existing if isinstance(p, dict)] if isinstance(existing, list) else []


def _existing_sources(pipeline_id: str, errors: list[str]) -> set:
    """The source ``(schema, table)`` (casefolded) of the entities of an existing pipeline (GET)."""
    import create_all_entities  # pylint: disable=import-outside-toplevel
    try:
        listing = corehub.fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=state.token)
    except Exception as exc:  # pylint: disable=broad-except
        errors.append(f"pipeline {pipeline_id}: could not list its entities ({exc})")
        return set()
    sources = set()
    for item in listing if isinstance(listing, list) else []:
        sources.update(
            create_all_entities._all_source_tables_of(  # pylint: disable=protected-access
                create_all_entities._listing_entity(item)))  # pylint: disable=protected-access
    return sources


def _planned_targets(planned: Any, entities: list) -> list:
    """Source and target (schema, table, entity name) of each planned table, as the YAML now says."""
    root = import_plan._schemas_root(planned)  # pylint: disable=protected-access
    out = []
    for entity in entities:
        entry = root.get(entity["schema"]) or {}
        tables = entry.get("tables") if isinstance(entry.get("tables"), dict) else {}
        cfg = (tables.get("custom") or {}).get(entity["key"]) if isinstance(tables.get("custom"), dict) else None
        cfg = cfg if isinstance(cfg, dict) else {}
        out.append({"id": entity["id"], "source": f'{entity["schema"]}.{entity["table"]}',
                    "targetSchema": cfg.get("targetSchema") or entry.get("targetSchema") or entity["schema"],
                    "targetTable": cfg.get("name") or entity["table"],
                    "entityName": cfg.get("entityName")})
    return out


def _preview_pipeline(job: _ArchiveImport, analysed: Dict[str, Any], text: str, existing_by_name: Dict[str, str],
                      reserved: set, errors: list[str]) -> Dict[str, Any]:
    plan = job.plan
    old_id = analysed["oldId"]
    pipeline_plan = plan.pipeline(old_id)
    out: Dict[str, Any] = {"oldId": old_id, "backupName": analysed["name"], "import": pipeline_plan.import_}
    if not pipeline_plan.import_:
        return out
    config = yaml.safe_load(text)
    try:
        import_plan.check_names(config, pipeline_plan)
        planned, notes = import_plan.apply_to_yaml(copy.deepcopy(config), plan, pipeline_plan)
        import_plan.check_target_collisions(config, pipeline_plan, old_id)
    except import_plan.PlanError as exc:
        out["error"] = str(exc)
        errors.append(str(exc))
        return out
    entities = import_plan.entity_ids(planned)
    out["notes"] = notes
    out["entities"] = _planned_targets(planned, entities)
    out["entityCount"] = len(entities)
    if not entities:
        errors.append(f"pipelines.{old_id}: no table selected; set \"import\": false to leave the pipeline out")
    out["parts"] = {part: plan.part(pipeline_plan, part) for part in import_plan.PARTS}
    target_id = pipeline_plan.resume_into or job.resume_pipeline_id
    name = pipeline_plan.name or analysed["name"]
    if target_id:
        target_id = _preview_resume_action(job, old_id, target_id, out, errors)
    else:
        name = _preview_create_action(plan, pipeline_plan, old_id, name, existing_by_name, reserved, out, errors)
    out["finalName"] = name
    if target_id:
        existing = _existing_sources(target_id, errors)
        clashes = [e["id"] for e in entities if (e["schema"].casefold(), e["table"].casefold()) in existing]
        out["existingEntities"] = len(existing)
        out["conflicts"] = clashes
        out["onExistingEntity"] = plan.on_existing_entity(pipeline_plan) or os.getenv("GS_IMPORT_ON_EXISTING") \
            or "default"
    passwords = job.agent_passwords.get(old_id, {})
    _check_agent_refs(analysed, pipeline_plan, passwords, old_id, errors)
    out["agents"] = [_preview_agent(job, agent, pipeline_plan, passwords, old_id, errors)
                     for agent in analysed["agents"]]
    return out


def _preview_resume_action(job: _ArchiveImport, old_id: str, target_id: Any, out: Dict[str, Any],
                           errors: list[str]) -> str:
    out["action"] = f"resume into existing pipeline {target_id}"
    target_id = str(target_id)
    existing_ids = getattr(job, "existing_ids", None)
    if existing_ids is not None and target_id not in existing_ids:
        errors.append(f"pipelines.{old_id}: resumeInto {target_id!r}: no such pipeline on Core Hub")
    return target_id


def _preview_create_action(plan: import_plan.ImportPlan, pipeline_plan: Any, old_id: str, name: str,
                           existing_by_name: Dict[str, str], reserved: set, out: Dict[str, Any],
                           errors: list[str]) -> str:
    """Record what creating the pipeline would do; returns its final name."""
    if name not in existing_by_name and name not in reserved:
        out["action"] = f"create {name!r}"
        reserved.add(name)
        return name
    on_existing = plan.on_existing_pipeline(pipeline_plan) or \
        os.getenv("GS_IMPORT_ON_EXISTING_PIPELINE", "suffix").strip().lower() or "suffix"
    if on_existing == "fail":
        out["action"] = f"fail: a pipeline named {name!r} exists (onExistingPipeline=fail)"
        errors.append(f"pipelines.{old_id}: {out['action']}")
        return name
    final = _unique_pipeline_name(name, reserved)
    out["action"] = f"create {final!r} ({name!r} exists)"
    reserved.add(final)
    return final


def _check_agent_refs(analysed: Dict[str, Any], pipeline_plan: Any, passwords: Dict[str, str], old_id: str,
                      errors: list[str]) -> None:
    known_refs = {str(r).casefold() for a in analysed["agents"] for r in (a.get("agentTag"), a.get("agentId")) if r}
    for ref in [*pipeline_plan.agents, *passwords]:
        if str(ref).casefold() not in known_refs:
            errors.append(f"pipelines.{old_id}: no agent {ref!r} in the backup (agentTag or agentId)")


def _agent_password_state(job: _ArchiveImport, agent: Dict[str, Any], entry: Dict[str, Any], refs: set,
                          passwords: Dict[str, str]) -> str:
    if any(passwords.get(r) for r in refs):
        return "override"
    if entry.get("passwordRef"):
        var = str(entry["passwordRef"]).split(":", 1)[-1]
        return "override" if (job.plan_env or {}).get(var) else f"missing (${var} not set)"
    return agent["password"]


def _preview_agent(job: _ArchiveImport, agent: Dict[str, Any], pipeline_plan: Any, passwords: Dict[str, str],
                   old_id: str, errors: list[str]) -> Dict[str, Any]:
    refs = {str(agent.get("agentTag") or ""), str(agent.get("agentId") or "")} - {""}
    folded_refs = {r.casefold() for r in refs}
    entry = next((v for k, v in pipeline_plan.agents.items() if k in refs or k.casefold() in folded_refs), {})
    state_ = _agent_password_state(job, agent, entry, refs, passwords)
    summary = dict(agent, password=state_)
    summary.update({field_: entry[field_] for field_ in ("host", "port", "database", "username") if field_ in entry})
    if state_.startswith("missing"):
        errors.append(f"pipelines.{old_id}: agent {agent.get('agentTag')}: password {state_}")
    return summary


def _pipeline_id_of(pipeline: Dict[str, Any]) -> str:
    return str(pipeline.get("id") or pipeline.get("pipelineId"))


def _pipeline_ids_by_name(existing: list) -> Dict[str, str]:
    return {str(p.get("name")): _pipeline_id_of(p) for p in existing if p.get("name")}


def _unknown_pipeline_errors(plan: import_plan.ImportPlan, job: _ArchiveImport, known: set) -> list[str]:
    """One error per pipeline the plan or the passwords name that the backup does not have."""
    return [f"pipelines.{old_id}: no pipeline {old_id!r} in the backup{import_options.suggest(old_id, sorted(known))}"
            for old_id in [*plan.pipelines, *job.agent_passwords] if old_id not in known]


def _existing_pipelines_or_error(errors: list[str]) -> tuple:
    """The existing pipelines and whether fetching them failed (the failure goes to ``errors``)."""
    try:
        return _existing_pipelines(), False
    except Exception as exc:  # pylint: disable=broad-except
        errors.append(f"Failed to fetch existing pipelines: {exc}")
        return [], True


def _preview_import(contents: bytes, plan: import_plan.ImportPlan,
                    agent_passwords: Optional[Dict[str, Dict[str, str]]] = None,
                    plan_env: Any = None, resume_pipeline_id: Optional[str] = None) -> Dict[str, Any]:
    """What an import with this plan would do. Read-only: Core Hub is only read (GET /pipelines and
    GET /pipelines/{id}/entities); nothing is created or changed. No password in the result."""
    analysis = _analyze_archive(contents, apply_overrides=True)
    errors = list(analysis["errors"])
    job = _ArchiveImport(contents=contents, agents_list=[], meta_by_old_id={}, udf_root=None,
                         continue_on_credentials_failure=False, resume_pipeline_id=resume_pipeline_id,
                         plan=plan, agent_passwords=agent_passwords or {},
                         plan_env=os.environ if plan_env is None else plan_env)
    known = {p["oldId"] for p in analysis["pipelines"]}
    errors.extend(_unknown_pipeline_errors(plan, job, known))
    existing, fetch_failed = _existing_pipelines_or_error(errors)
    existing_by_name = _pipeline_ids_by_name(existing)
    reserved = set(existing_by_name)
    job.existing_ids = None if fetch_failed else {_pipeline_id_of(p) for p in existing}
    texts = {str(_resolve_pipeline_identity(text, stem)[0]): text
             for _n, stem, text in _archive_pipeline_members(contents)}
    pipelines = [_preview_pipeline(job, item, texts[item["oldId"]], existing_by_name, reserved, errors)
                 for item in analysis["pipelines"]]
    selected = [p for p in pipelines if p.get("import")]
    if not selected:
        errors.append("The plan selects no pipeline: nothing would be imported.")
    return {
        "ok": not errors,
        "errors": errors,
        "globals": {g: plan.global_enabled(g) and analysis["globals"].get(g, False) for g in import_plan.GLOBALS},
        "pipelines": pipelines,
        "totals": {"pipelines": len(selected), "entities": sum(p.get("entityCount", 0) for p in selected)},
        "plan": plan.to_dict(),
    }


def _load_plan_or_400(text: Any) -> import_plan.ImportPlan:
    try:
        return import_plan.load(text)
    except import_plan.PlanError as exc:
        raise HTTPException(status_code=400, detail=f"Import plan: {exc}") from exc


def _load_entity_plan_or_400(source: Any) -> import_plan.ImportPlan:
    """Entity Customize plan: refuses backup-only keys and any password."""
    try:
        return entity_plan_runtime.load_plan(source)
    except import_plan.PlanError as exc:
        raise HTTPException(status_code=400, detail=f"Entity import plan: {exc}") from exc


def _entity_plan_flags(plan: import_plan.ImportPlan, pipeline_id: str,
                       *, enable_scheduling: bool, create_tables: bool) -> tuple[bool, bool, bool]:
    """``(enable_scheduling, create_tables, restore_function_collection)`` after plan.parts."""
    pipeline = import_plan.entity_pipeline(plan, pipeline_id)
    parts = import_plan.entity_part_flags(plan, pipeline)
    return (
        bool(enable_scheduling) and parts["schedules"],
        bool(create_tables) and parts["createTables"],
        parts["functionCollection"],
    )


def _entity_run_import_options(plan: import_plan.ImportPlan, pipeline: import_plan.PipelinePlan,
                               *, dry_run: bool = False):
    """ImportOptions for an entity run. Empty plan + no dry-run → same as today (env-if-requested)."""
    base = import_options.from_env_if_requested()
    if plan.empty and not dry_run:
        return base
    return entity_plan_runtime.options_for_run(
        plan, pipeline, dry_run=dry_run,
        base=base or import_options.ImportOptions.from_env(),
    )


def _bulk_yaml_for_plan(plan: import_plan.ImportPlan, pipeline: import_plan.PipelinePlan,
                        source_schema: str, target_schema: str, table_names: list[str]):
    """Minimal YAML for Bulk when the plan has overrides / part stripping; else None."""
    parts = import_plan.entity_part_flags(plan, pipeline)
    if not (pipeline.changes_yaml() or not parts["udfs"] or not parts["groups"]):
        return None
    bulk_yaml = {
        source_schema: {
            "target": target_schema,
            "tables": {"whitelist": list(table_names), "custom": {}},
        }
    }
    return entity_plan_runtime.apply_overrides_to_bulk_yaml(bulk_yaml, plan, pipeline.old_id)


def _prepare_entity_yaml_for_run(yaml_path: Path, plan: import_plan.ImportPlan,
                                 pipeline_id: str) -> tuple[Path, list[str], Any, Optional[Path]]:
    """Apply the entity plan to the uploaded YAML. Returns (path_to_use, notes, pipeline, temp_or_None).

    Empty plan leaves the original file untouched (parity with current behaviour).
    """
    if plan.empty:
        return yaml_path, [], import_plan.entity_pipeline(plan, pipeline_id), None
    yaml_config = load_yaml_config(str(yaml_path))
    planned, notes, pipeline, _parts = entity_plan_runtime.prepare_yaml(yaml_config, plan, pipeline_id)
    if planned is yaml_config and not notes:
        # apply_to_yaml returned the same object: nothing to rewrite.
        return yaml_path, notes, pipeline, None
    temp = entity_plan_runtime.write_planned_yaml(planned, suffix=yaml_path.suffix or ".yaml")
    return temp, notes, pipeline, temp


def _passwords_or_400(text: Optional[str]) -> Dict[str, Dict[str, str]]:
    try:
        return import_plan.parse_agent_passwords(text)
    except import_plan.PlanError as exc:
        raise HTTPException(status_code=400, detail=f"agent_passwords: {exc}") from exc


class ImportPreviewRequest(BaseModel):
    """The passwords travel in the body only, they are never stored."""

    uploadId: str
    plan: Optional[Dict[str, Any]] = None
    agent_passwords: Optional[Dict[str, Dict[str, str]]] = None

    def __repr__(self) -> str:  # never show the passwords
        return f"ImportPreviewRequest(uploadId={self.uploadId!r})"


class EntityPlanBody(BaseModel):
    """Plan body for entity Customize (draft / preview). Never holds a password."""

    pipeline_id: str = Field(..., alias="pipelineId")
    yaml_file_id: Optional[str] = Field(None, alias="yamlFileId")
    plan: Optional[Dict[str, Any]] = None
    # Bulk preview: table names instead of a YAML file.
    source_schema: Optional[str] = Field(None, alias="sourceSchema")
    target_schema: Optional[str] = Field(None, alias="targetSchema")
    table_names: Optional[list[str]] = Field(None, alias="tableNames")


async def import_analyze(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Analyse an Export All ZIP for a customizable import: keeps it (owner-only, one hour) under an uploadId
    and returns its pipelines, tables, agents (no password) and the draft plan. Nothing is sent to Core Hub."""
    _require_session()
    contents = await _read_capped_upload(file, import_upload_max_bytes())
    analysis = await run_in_threadpool(_analyze_archive, contents)
    analysis["uploadId"] = store_import_upload(contents)
    return analysis


async def import_preview(request: ImportPreviewRequest) -> Dict[str, Any]:
    """Preview (read-only) what importing the analysed backup with this plan would do."""
    _require_session()
    plan = _load_plan_or_400(request.plan)
    passwords = {str(k): dict(v) for k, v in (request.agent_passwords or {}).items()}
    contents = load_import_upload(request.uploadId)
    return await run_in_threadpool(_locked_preview, contents, plan, passwords)


def _locked_preview(contents: bytes, plan: import_plan.ImportPlan,
                    passwords: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    owner = object()
    import_plan.SECRETS.add(owner, list(import_plan.secret_values(passwords)) + _env_secret_values(plan, None))
    try:
        with _exclusive_import():
            return _preview_import(contents, plan, passwords)
    finally:
        import_plan.SECRETS.discard(owner)


def _register_import_routes(app: FastAPI) -> None:
    """The Export All import and validation routes and the backup restore routes."""
    app.add_api_route("/api/import/all", import_all, methods=["POST"], responses=_ARCHIVE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/analyze", import_analyze, methods=["POST"], responses=_ARCHIVE_IMPORT_RESPONSES)
    app.add_api_route("/api/import/preview", import_preview, methods=["POST"], responses=_ARCHIVE_IMPORT_RESPONSES)
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
    import_options: Any = None
    restore_function_collection: bool = True
    planned_temp: Optional[Path] = None


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
        enable_scheduling=settings.enable_scheduling,
        create_tables=settings.create_tables,
        use_ssl=settings.use_ssl,
        skip_verify=settings.skip_verify,
        options=corehub.EntityRunOptions(
            skip_errors=request.skip_errors,
            chunk_size=request.chunk_size,
            log_callback=settings.log_callback,
            chronos_token=state.chronos_token,
            restore_function_collection=settings.restore_function_collection,
            import_options=(
                settings.import_options
                if settings.import_options is not None
                else import_options.from_env_if_requested()
            ),
        ),
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
        state.finalize_run(run_id, "failed", UPLOADED_YAML_NOT_FOUND)
        return

    try:
        plan = _load_entity_plan_or_400(request.plan)
    except HTTPException as exc:
        state.finalize_run(run_id, "failed", str(exc.detail))
        return

    enable_scheduling, create_tables, restore_fc = _entity_plan_flags(
        plan, request.pipeline_id, enable_scheduling=enable_scheduling, create_tables=create_tables,
    )
    try:
        yaml_for_run, notes, pipeline, planned_temp = _prepare_entity_yaml_for_run(
            yaml_path, plan, request.pipeline_id,
        )
    except import_plan.PlanError as exc:
        state.finalize_run(run_id, "failed", f"Entity import plan: {exc}")
        return

    import_opts = _entity_run_import_options(plan, pipeline, dry_run=bool(request.dry_run))

    def _log_callback(message: str) -> None:
        state.append_log(run_id, message)

    for note in notes:
        _log_callback(f"[plan] {note}")

    settings = _RunSettings(
        token=token,
        base_url=base_url,
        enable_scheduling=enable_scheduling,
        create_tables=create_tables,
        use_ssl=use_ssl,
        skip_verify=skip_verify,
        log_callback=_log_callback,
        import_options=import_opts,
        restore_function_collection=restore_fc,
        planned_temp=planned_temp,
    )

    loop = asyncio.get_running_loop()

    def _run_sync() -> dict:
        try:
            return _run_entity_creation(request, settings, yaml_for_run)
        finally:
            if planned_temp is not None:
                with contextlib.suppress(OSError):
                    planned_temp.unlink(missing_ok=True)

    result = await loop.run_in_executor(None, _run_sync)

    status = "completed" if result.get("success") else "failed"
    state.finalize_run(run_id, status, result.get("error"))


def _start_run(request: RunRequest) -> ApiMessage:
    if not state.token:
        raise HTTPException(status_code=401, detail=AUTH_REQUIRED)

    # Refuse a bad plan before the run starts (empty plan == current behaviour).
    plan = _load_entity_plan_or_400(request.plan)
    if not state.get_upload_path(request.yaml_file_id):
        raise HTTPException(status_code=400, detail=UPLOADED_YAML_NOT_FOUND)
    # Cheap shape check: apply against the uploaded YAML so include/unknown tables fail fast.
    try:
        yaml_path = state.get_upload_path(request.yaml_file_id)
        _prepare_entity_yaml_for_run(yaml_path, plan, request.pipeline_id)
    except import_plan.PlanError as exc:
        raise HTTPException(status_code=400, detail=f"Entity import plan: {exc}") from exc

    try:
        run_status = state.start_run()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    task = asyncio.create_task(_execute_run(run_status.run_id, request))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return ApiMessage(message="Entity creation started", planUsed=plan.to_dict() if not plan.empty else None)


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


def _existing_entity_sources(pipeline_id: str) -> set:
    """``{(schema, table)}`` already in the pipeline. GET-only; empty set when listing fails.

    Core Hub lists entities as ``{"entity": {...}}`` with source tables under
    ``agentEntities`` (and ``tables`` for MultiTable). Match the backup-import
    path: unwrap via create_all_entities helpers so alreadyExists is correct.
    """
    import create_all_entities  # pylint: disable=import-outside-toplevel
    try:
        listing = corehub.fetch_core_hub(
            f"/pipelines/{pipeline_id}/entities", token=state.token,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.info("Entity preview: could not list existing entities: %s", exc)
        return set()
    sources = set()
    for item in listing if isinstance(listing, list) else []:
        entity = create_all_entities._listing_entity(item)  # pylint: disable=protected-access
        sources.update(
            create_all_entities._all_source_tables_of(entity)  # pylint: disable=protected-access
        )
    return sources


def _yaml_config_for_entity_preview(body: EntityPlanBody):
    """YAML config for draft/preview: uploaded file, or a Bulk table list."""
    if body.yaml_file_id:
        yaml_path = state.get_upload_path(body.yaml_file_id)
        if not yaml_path:
            raise HTTPException(status_code=400, detail=UPLOADED_YAML_NOT_FOUND)
        return load_yaml_config(str(yaml_path))
    if body.table_names is not None and body.source_schema:
        return {
            body.source_schema: {
                "target": body.target_schema or body.source_schema,
                "tables": {"whitelist": list(body.table_names), "custom": {}},
            }
        }
    raise HTTPException(
        status_code=400,
        detail="Provide yamlFileId, or sourceSchema with tableNames for a Bulk preview",
    )


def entity_plan_draft(body: EntityPlanBody) -> Dict[str, Any]:
    """Draft entity Customize plan JSON (every table included). Nothing is sent to Core Hub."""
    _require_session()
    yaml_config = _yaml_config_for_entity_preview(body)
    return import_plan.draft_entity(yaml_config, body.pipeline_id)


async def entity_plan_preview(body: EntityPlanBody) -> Dict[str, Any]:
    """Read-only preview of an entity Customize plan. Core Hub is GET-only (existing entities)."""
    _require_session()
    plan = _load_entity_plan_or_400(body.plan)
    yaml_config = _yaml_config_for_entity_preview(body)
    try:
        existing = await run_in_threadpool(_existing_entity_sources, body.pipeline_id)
        return await run_in_threadpool(
            entity_plan_runtime.preview_payload,
            yaml_config=yaml_config,
            plan=plan,
            pipeline_id=body.pipeline_id,
            existing_sources=existing,
        )
    except import_plan.PlanError as exc:
        raise HTTPException(status_code=400, detail=f"Entity import plan: {exc}") from exc


def _register_run_routes(app: FastAPI) -> None:
    """The entity creation run and the agent certificate upload routes."""

    @app.post("/api/run", responses=_RUN_RESPONSES)
    async def start_run(request: RunRequest) -> ApiMessage:
        return _start_run(request)

    @app.post("/api/entities/plan/draft", responses=_RUN_RESPONSES)
    async def draft_entity_plan(body: EntityPlanBody) -> Dict[str, Any]:
        return entity_plan_draft(body)

    @app.post("/api/entities/plan/preview", responses=_RUN_RESPONSES)
    async def preview_entity_plan(body: EntityPlanBody) -> Dict[str, Any]:
        return await entity_plan_preview(body)

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



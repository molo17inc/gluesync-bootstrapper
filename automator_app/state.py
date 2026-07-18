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

"""Application state management for the Gluesync Automator UI."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Any


@dataclass
class UploadedFile:
    file_id: str
    name: str
    path: Path


@dataclass
class RunStatus:
    run_id: str
    status: str = "idle"  # idle | running | completed | failed
    logs: List[str] = field(default_factory=list)
    error: Optional[str] = None


class AutomatorState:
    """In-memory state shared across API handlers."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._token: Optional[str] = None
        self._chronos_token: Optional[str] = None
        self._base_url: Optional[str] = os.getenv("CORE_HUB_URL", "https://localhost")
        self._use_ssl: Optional[bool] = True
        self._skip_verify: Optional[bool] = True
        self._enable_scheduling: bool = True
        self._create_tables: bool = True
        self.uploads: Dict[str, UploadedFile] = {}
        self.current_run: Optional[RunStatus] = None
        self._duplicate_in_progress: bool = False
        self._duplicate_cancel_requested: bool = False
        self._corehub_overview: Optional[Dict[str, Any]] = None

    def _try_get_sdk_token(
        self,
        base_url: str,
        use_ssl: Optional[bool],
        skip_verify: Optional[bool],
    ) -> Optional[str]:
        """Try to obtain an SDK token with subject gluesync-bootstrapper for Chronos.

        Returns the SDK token on success, None on failure (falls back to user token).
        """
        try:
            import os as _os
            license_file = _os.getenv("GLUESYNC_LICENSE_FILE")
            if not license_file or not _os.path.exists(license_file):
                return None

            from utils.gluesync_sdk_client import get_token, initialize_gluesync_sdk

            # Set env vars for SDK initialization
            _os.environ["CORE_HUB_URL"] = base_url
            if use_ssl is not None:
                _os.environ["SSL_ENABLED"] = str(use_ssl)
            if skip_verify is not None:
                _os.environ["SSL_SKIP_VERIFY"] = str(skip_verify)
            _os.environ["GLUESYNC_MODULE_TAG"] = "gluesync-bootstrapper"

            initialize_gluesync_sdk()
            sdk_token = get_token()
            if sdk_token:
                return sdk_token
        except Exception:
            pass
        return None

    # Authentication -----------------------------------------------------
    def _write_token_file(
        self,
        token: str,
        base_url: str,
        use_ssl: Optional[bool],
        skip_verify: Optional[bool],
    ) -> None:
        """Write authentication credentials to a file for MCP server (stdio transport)."""
        try:
            # Write to project directory so stdio transport can find it without
            # relying on the HOME env var (which Claude Desktop doesn't pass).
            import os
            cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            token_file = Path(cwd) / ".gluesync_mcp_token"
            with open(token_file, "w") as f:
                f.write(f"{token}\n")
                f.write(f"{base_url}\n")
                if use_ssl is not None:
                    f.write(f"SSL_ENABLED={use_ssl}\n")
                if skip_verify is not None:
                    f.write(f"SSL_SKIP_VERIFY={skip_verify}\n")
        except Exception:
            # Silently fail - this is optional
            pass

    def _remove_token_file(self) -> None:
        """Remove the token file on logout."""
        try:
            import os
            cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            token_file = Path(cwd) / ".gluesync_mcp_token"
            if token_file.exists():
                token_file.unlink()
        except Exception:
            # Silently fail
            pass

    def set_auth(
        self,
        token: str,
        base_url: str,
        *,
        use_ssl: Optional[bool] = None,
        skip_verify: Optional[bool] = None,
    ) -> None:
        with self._lock:
            self._token = token
            self._base_url = base_url
            self._use_ssl = use_ssl
            self._skip_verify = skip_verify
            # Try to obtain an SDK token with subject _bootstrapper for Chronos calls.
            # Falls back to the user token if SDK is unavailable or no license file.
            self._chronos_token = self._try_get_sdk_token(base_url, use_ssl, skip_verify)
            # Write token to file for MCP server (stdio transport)
            self._write_token_file(token, base_url, use_ssl, skip_verify)

    def clear_auth(self) -> None:
        with self._lock:
            self._token = None
            self._chronos_token = None
            self._corehub_overview = None
            # Remove token file
            self._remove_token_file()

    # Upload management --------------------------------------------------
    def register_upload(self, file_path: Path, name: str) -> str:
        file_id = uuid.uuid4().hex
        self.uploads[file_id] = UploadedFile(file_id=file_id, name=name, path=file_path)
        return file_id

    def get_upload_path(self, file_id: str) -> Optional[Path]:
        uploaded = self.uploads.get(file_id)
        return uploaded.path if uploaded else None

    # Run lifecycle ------------------------------------------------------
    def start_run(self) -> RunStatus:
        with self._lock:
            if self.current_run and self.current_run.status == "running":
                raise RuntimeError("A run is already in progress")
            run = RunStatus(run_id=uuid.uuid4().hex, status="running")
            self.current_run = run
            return run

    def append_log(self, run_id: str, message: str) -> None:
        with self._lock:
            if not self.current_run or self.current_run.run_id != run_id:
                return
            self.current_run.logs.append(message)

    def finalize_run(self, run_id: str, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            if not self.current_run or self.current_run.run_id != run_id:
                return
            self.current_run.status = status
            self.current_run.error = error

    def run_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.current_run or self.current_run.run_id != run_id:
                return None
            return {
                "id": self.current_run.run_id,
                "status": self.current_run.status,
                "logs": list(self.current_run.logs),
                "error": self.current_run.error,
            }

    def current_run_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.current_run:
                return None
            return {
                "id": self.current_run.run_id,
                "status": self.current_run.status,
                "logCount": len(self.current_run.logs),
                "error": self.current_run.error,
            }

    def get_logs(self, run_id: str) -> Optional[List[str]]:
        with self._lock:
            if not self.current_run or self.current_run.run_id != run_id:
                return None
            return list(self.current_run.logs)

    def set_preferences(self, *, enable_scheduling: bool, create_tables: bool) -> None:
        with self._lock:
            self._enable_scheduling = enable_scheduling
            self._create_tables = create_tables

    def preferences(self) -> Dict[str, bool]:
        with self._lock:
            return {
                "enableScheduling": self._enable_scheduling,
                "createTables": self._create_tables,
            }

    # Core Hub overview --------------------------------------------------
    def set_corehub_overview(self, overview: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._corehub_overview = overview

    def corehub_overview(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._corehub_overview

    # Duplicate pipeline coordination -----------------------------------
    def begin_duplicate(self) -> None:
        with self._lock:
            if self._duplicate_in_progress:
                raise RuntimeError("Duplicate already running")
            self._duplicate_in_progress = True
            self._duplicate_cancel_requested = False

    def end_duplicate(self) -> None:
        with self._lock:
            self._duplicate_in_progress = False
            self._duplicate_cancel_requested = False

    def request_duplicate_cancel(self) -> bool:
        with self._lock:
            if not self._duplicate_in_progress:
                return False
            self._duplicate_cancel_requested = True
            return True

    def is_duplicate_cancelled(self) -> bool:
        with self._lock:
            return self._duplicate_cancel_requested

    def duplicate_status(self) -> Dict[str, bool]:
        with self._lock:
            return {
                "inProgress": self._duplicate_in_progress,
                "cancelRequested": self._duplicate_cancel_requested,
            }

    # Accessors ----------------------------------------------------------
    @property
    def token(self) -> Optional[str]:
        with self._lock:
            return self._token

    @property
    def chronos_token(self) -> Optional[str]:
        with self._lock:
            return self._chronos_token or self._token

    @property
    def base_url(self) -> Optional[str]:
        with self._lock:
            return self._base_url

    @property
    def use_ssl(self) -> Optional[bool]:
        with self._lock:
            return self._use_ssl

    @property
    def skip_verify(self) -> Optional[bool]:
        with self._lock:
            return self._skip_verify

    def snapshot(self) -> dict:
        with self._lock:
            run_snapshot = None
            if self.current_run is not None:
                run_snapshot = {
                    "id": self.current_run.run_id,
                    "status": self.current_run.status,
                    "logs": list(self.current_run.logs),
                    "error": self.current_run.error,
                }

            data = {
                "tokenPresent": self._token is not None,
                "baseUrl": self._base_url,
                "useSsl": self._use_ssl,
                "skipVerify": self._skip_verify,
                "enableScheduling": self._enable_scheduling,
                "createTables": self._create_tables,
                "corehubOverview": self._corehub_overview,
                "run": run_snapshot,
            }
            return data


state = AutomatorState()
"""Module-level singleton state."""

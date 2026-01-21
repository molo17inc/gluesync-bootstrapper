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

    # Authentication -----------------------------------------------------
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

    def clear_auth(self) -> None:
        with self._lock:
            self._token = None
            self._corehub_overview = None

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

    def snapshot(self) -> Dict[str, Optional[str]]:
        with self._lock:
            return {
                "tokenPresent": self._token is not None,
                "baseUrl": self._base_url,
                "useSsl": self._use_ssl,
                "skipVerify": self._skip_verify,
                "enableScheduling": self._enable_scheduling,
                "createTables": self._create_tables,
                "corehubOverview": self._corehub_overview,
                "duplicate": {
                    "inProgress": self._duplicate_in_progress,
                    "cancelRequested": self._duplicate_cancel_requested,
                },
                "run": None
                if not self.current_run
                else {
                    "id": self.current_run.run_id,
                    "status": self.current_run.status,
                    "logCount": len(self.current_run.logs),
                    "error": self.current_run.error,
                },
            }


state = AutomatorState()
"""Module-level singleton state."""

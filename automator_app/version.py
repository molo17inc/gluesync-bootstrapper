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

"""Automator application version utilities."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_VERSION = "0.0.0"


def _read_version_file() -> str:
    """Read version from the bundled VERSION file if available."""

    try:
      version_path = Path(__file__).with_name("VERSION")
      if version_path.exists():
          text = version_path.read_text(encoding="utf-8").strip()
          if text:
              return text
    except Exception:
        # Best-effort only; fall back to env/default
        pass
    return _DEFAULT_VERSION


def get_version() -> str:
    """Return the application version, preferring VERSION file, then env, then default."""

    file_version = _read_version_file()
    return os.getenv("AUTOMATOR_VERSION", file_version)


__all__ = ["get_version"]

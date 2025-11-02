"""Automator application version utilities."""

from __future__ import annotations

import os

_DEFAULT_VERSION = "0.0.0"


def get_version() -> str:
    """Return the application version, falling back to a default."""

    return os.getenv("AUTOMATOR_VERSION", _DEFAULT_VERSION)


__all__ = ["get_version"]

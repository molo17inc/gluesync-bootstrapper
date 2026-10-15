# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
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

"""Hide credentials before a request or its headers reach a log."""

from typing import Any

REDACTED = "***"

# A key holding any of these (case and separators ignored) is a credential: its whole value is hidden.
_SENSITIVE_PARTS = ("password", "passphrase", "secret", "token", "apikey", "privatekey", "authorization")


def _is_sensitive(key: Any) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _holds_credential(value: Any) -> bool:
    """A flag (``includeSecrets: true``, ``hasApiKey: false``) or an empty value says nothing secret."""
    return not (value is None or isinstance(value, bool))


def redact(value: Any) -> Any:
    """A copy of ``value`` (dicts and lists, at any depth) with every credential hidden."""
    if isinstance(value, dict):
        return {key: REDACTED if _is_sensitive(key) and _holds_credential(item) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value

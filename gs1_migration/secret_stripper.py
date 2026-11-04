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

"""Removes every key that looks like a secret from a GS1 configuration, at any depth, before anything reads it.

The values are dropped unread and only their JSON paths are kept, in document order. The entries of a
``fields`` or ``keys`` map are exempt: they pair a document field with a column (GS1 nosql-to-sql), both
names, and a field named after a token (``TokenId``) is no secret.
"""

import re
from typing import Any, Dict, List, Tuple

from .json_values import is_primitive

_SECRET_KEY = re.compile(
    r"password|passwd|pwd|passphrase|secret|token|certificate|credential|keystore|auth"
    r"|api[-_]?key|private[-_]?key|access[-_]?key|secret[-_]?key",
    re.IGNORECASE,
)
_NAME_MAPS = {"fields", "keys"}


def strip(config: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The configuration without its secret-looking keys, and the paths of the keys removed."""
    paths: List[str] = []
    return _strip(config, "", paths, names=False), paths


def _strip(element: Any, prefix: str, paths: List[str], names: bool) -> Any:
    if isinstance(element, dict):
        kept = {}
        for key, value in element.items():
            path = key if not prefix else f"{prefix}.{key}"
            if names and is_primitive(value):
                kept[key] = value
            elif _SECRET_KEY.search(key):
                paths.append(path)
            else:
                kept[key] = _strip(value, path, paths, names=key in _NAME_MAPS)
        return kept
    if isinstance(element, list):
        return [_strip(item, f"{prefix}[{index}]", paths, names=False) for index, item in enumerate(element)]
    return element

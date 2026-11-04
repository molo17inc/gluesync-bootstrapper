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

"""The issue codes the GS1 converter itself reports, with Core Hub's hint for each.

Structure issues (``KEY_NOT_PLAIN_COLUMN``, ``PARENT_LINK_MISSING``...) come from Core Hub with their hint.
"""

from typing import Any, Dict, Optional

from .model import issue

# The converter refuses the entity: it stays on GS1.
CONVERTER_HINTS = {
    "GS1_SEPARATOR_NOT_SUPPORTED":
        "Keep the entity on GS1, or change its separator to one GS2 renders and rewrite its documents.",
    "GS1_TTL_TOUCH_NOT_SUPPORTED": "Keep the entity on GS1 until GS2 refreshes document expiry.",
    "GS1_ENTITY_KIND_NOT_SUPPORTED":
        "Keep the entity on GS1, or rewrite it as a QueryDataModeling entity with one statement per level.",
}

WARNING_HINTS = {
    "GS1_SETTING_NOT_CARRIED": "Nothing to do: GS1 ignored the setting as well.",
    "GS1_SETTING_ELSEWHERE": "Set the GS2 agent or entity setting the message names, if the GS1 value mattered.",
    "GS1_FLATTENED_FIELD_TO_PLACE":
        "Set the path of the MERGED level to the sub-object holding the fields, split it per sub-object, and move the "
        "item's own fields to the level.",
    "GS1_DOCUMENT_ID_TO_CONFIRM":
        "Confirm the prefix and separator that lay the root key out in the document ids, or key the entity on metaId.",
    "GS1_ROOT_KEY_TO_CONFIRM":
        "Describe the converted entity and confirm the proposed root key: GS1 used the primary key of the FROM table.",
}


def converter_error(code: str, message: str) -> Dict[str, Any]:
    if code not in CONVERTER_HINTS:
        raise ValueError(f"{code} is not a converter code")
    return issue(code, "ERROR", message, CONVERTER_HINTS[code])


def warning(code: str, message: str, location: Optional[str] = None) -> Dict[str, Any]:
    return issue(code, "WARNING", message, WARNING_HINTS[code], location=location)

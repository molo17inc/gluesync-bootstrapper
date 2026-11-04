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

"""Types the columns of a document-split draft from what the agents' discovery reports.

The converter has no agent to ask: the columns of a document draft carry placeholder types (``VARCHAR``), which Core
Hub refuses on save (``Unknown data type VARCHAR for CouchbaseDataType``). The flow asks each agent's discovery for the
columns of every collection and table of the draft and copies the type of the column of the same name (compared
without case) onto the draft's column. A column discovery does not know (a container for an array no sampled document
holds) keeps the draft's type; a column discovery answers without a type is reported.

No I/O here: ``columns_of(side, schema, table)`` is the flow's discovery call and answers ``{lower-case name: column}``.
"""

import copy
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import issue

CODE = "GS1_COLUMNS_NOT_DISCOVERED"
HINT = ("Check the columns on the source and the target: a column discovery does not know keeps the converter's type, "
        "and Core Hub refuses a type its agent does not have when the entity is saved.")
# The type of a column and what goes with it (length, precision, flags).
TYPE_FIELDS = ("dataType", "charMaxLength", "charOctetLength", "charSet", "collation", "numPrec", "numPrecRadix",
               "numScale", "datetimePrecision", "isIdentity", "isForeignKey", "fkTarget", "isUnique")
_MAX_LISTED = 20

ColumnsOf = Callable[[str, str, str], Dict[str, Dict[str, Any]]]


def retype(columns: List[Dict[str, Any]], found: Dict[str, Dict[str, Any]], where: str) -> Tuple[List[str], List[str]]:
    """Types ``columns`` in place from ``found``; the names (prefixed by ``where``) discovery did not know, and untyped."""
    unknown, untyped = [], []
    for column in columns:
        match = found.get(str(column.get("name", "")).lower())
        if match is None:
            unknown.append(f"{where}.{column.get('name')}")
            continue
        for field in TYPE_FIELDS:
            column.pop(field, None)
            if field in match:
                column[field] = match[field]
        if not column.get("dataType"):
            untyped.append(f"{where}.{column.get('name')}")
    return unknown, untyped


def _sides(draft: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    entities = draft.get("agentEntities") or []
    source = next((a for a in entities if a.get("documentModel") is not None), None)
    target = next((a for a in entities if a is not source and isinstance(a.get("tables"), list)), None)
    return source, target


def type_document_draft(draft: Dict[str, Any], columns_of: ColumnsOf) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A typed copy of the draft and the WARNING issues for the columns discovery did not know or left untyped.

    The target's ``columns`` pairs each table with the list of its columns (``[table, columns, table, columns...]``).
    """
    typed = copy.deepcopy(draft)
    source, target = _sides(typed)
    unknown: List[str] = []
    untyped: List[str] = []
    if source is not None:
        scope = source["entityObject"]["scope"]
        collection = source["entityObject"]["collection"]
        found = retype(source["columns"], columns_of("source", scope, collection), f"source {scope}.{collection}")
        unknown += found[0]
        untyped += found[1]
    if target is not None:
        pairs = target.get("columns") or []
        for table, columns in zip(pairs[0::2], pairs[1::2]):
            if not isinstance(table, dict) or not isinstance(columns, list):
                continue
            where = f"{table['schema']}.{table['name']}"
            found = retype(columns, columns_of("target", table["schema"], table["name"]), where)
            unknown += found[0]
            untyped += found[1]
    return typed, _warnings(unknown, untyped)


def _listed(names: List[str]) -> str:
    extra = f" and {len(names) - _MAX_LISTED} more" if len(names) > _MAX_LISTED else ""
    return ", ".join(names[:_MAX_LISTED]) + extra


def _warnings(unknown: List[str], untyped: List[str]) -> List[Dict[str, Any]]:
    parts = []
    if unknown:
        parts.append(f"discovery does not know the columns {_listed(unknown)}: they keep the draft's type")
    if untyped:
        parts.append(f"discovery reports no type for {_listed(untyped)}")
    if not parts:
        return []
    return [issue(CODE, "WARNING", "Typing the draft from the agents' discovery: " + "; ".join(parts), HINT)]

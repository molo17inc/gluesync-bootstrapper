# This program is part of Gluesync.
#
# Reserved Core Hub technical source fields (commons Constants / agent getTechnicalFields).
#
# BaseAgentService.getTableColumns appends getTechnicalFields ONLY for names listed in the
# agent's specificConfiguration.technicalFields (empty by default). Shared CDC names:
# _TRANSACTION_ID, _TRANSACTION_TIMESTAMP, _TRANSACTION_OPERATION, _TRANSACTION_USER
# (MySQL drops _TRANSACTION_USER). IBM i also: _RRN, _RECEIVER_*, _SEQUENCE_NUMBER.
# Oracle CDC: _ROWID. Docs wrongly say _RRN_COLUMN — code / keys use _RRN (alias below).
#
# Automator: when YAML maps a reserved name that is NOT in the agent's technicalFields, fail
# early with a clear enable message. When it IS enabled but discovery still omits it, synthesise
# a discovery-shaped column with Constants.to64BitId so 2.3 grouped import can proceed.

"""Reserved Gluesync technical source columns, aliases, and agent-gated synthesis."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, MutableMapping, Optional

# Commons Constants (gluesync-kotlin commons/Constants.kt) — shared CDC technical fields.
TRANSACTION_ID = "_TRANSACTION_ID"
TRANSACTION_TIMESTAMP = "_TRANSACTION_TIMESTAMP"
TRANSACTION_OPERATION = "_TRANSACTION_OPERATION"  # I/U/D; docs also S for snapshot
TRANSACTION_USER = "_TRANSACTION_USER"  # not on every agent (e.g. MySQL omits it)

# Agent-specific names that also use name.to64BitId() (AS400 As400Constants, Oracle ROWID).
AS400_RRN = "_RRN"
AS400_RECEIVER_LIBRARY = "_RECEIVER_LIBRARY"
AS400_RECEIVER_NAME = "_RECEIVER_NAME"
AS400_SEQUENCE_NUMBER = "_SEQUENCE_NUMBER"
ORACLE_ROWID = "_ROWID"

# Doc naming gotcha (technical-fields.html): ``_RRN_COLUMN`` → code / keys use ``_RRN``.
_TECHNICAL_FIELD_ALIASES = {
    "_RRN_COLUMN": AS400_RRN,
}

# Default dataType when a helper builds a discovery-shaped column (matches common getTechnicalFields).
_DEFAULT_DATA_TYPES = {
    TRANSACTION_ID: "VARCHAR",
    TRANSACTION_TIMESTAMP: "BIG_INT",
    TRANSACTION_OPERATION: "CHAR",
    TRANSACTION_USER: "VARCHAR",
    AS400_RRN: "BIG_INT",
    AS400_RECEIVER_LIBRARY: "VARCHAR",
    AS400_RECEIVER_NAME: "VARCHAR",
    AS400_SEQUENCE_NUMBER: "BIG_INT",
    ORACLE_ROWID: "VARCHAR",
}

RESERVED_TECHNICAL_FIELD_NAMES = frozenset(_DEFAULT_DATA_TYPES)


def to64_bit_id(value: str) -> int:
    """Port of Kotlin ``String.to64BitId()``: SHA-256 → first 8 bytes as signed 64-bit long."""
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    unsigned = int.from_bytes(digest[:8], "big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def canonical_technical_field_name(name: Any) -> Optional[str]:
    """The agent/Constants spelling of ``name``, or None when it is not a reserved technical field.

    Accepts doc aliases (``_RRN_COLUMN`` → ``_RRN``). Comparison is exact on the catalog strings.
    """
    if not isinstance(name, str) or not name:
        return None
    if name in RESERVED_TECHNICAL_FIELD_NAMES:
        return name
    aliased = _TECHNICAL_FIELD_ALIASES.get(name)
    if aliased is not None:
        return aliased
    return None


def is_reserved_technical_field(name: Any) -> bool:
    """True when ``name`` is a known Core Hub / agent reserved technical source column (or alias)."""
    return canonical_technical_field_name(name) is not None


def reserved_technical_field_id(name: str) -> int:
    """Stable Core Hub column id for a reserved technical field (``Constants.*_ID``)."""
    canonical = canonical_technical_field_name(name) or name
    return to64_bit_id(canonical)


def missing_technical_field_message(table_name: str, source_name: str) -> str:
    """Clear import refuse when YAML maps a reserved tech field the agent does not produce."""
    canonical = canonical_technical_field_name(source_name) or source_name
    alias_note = ""
    if isinstance(source_name, str) and source_name != canonical:
        alias_note = f" (YAML `{source_name}` is the documented alias of `{canonical}`)"
    return (
        f"Table {table_name}: column {canonical}{alias_note} is a reserved Core Hub technical field "
        f"but is not listed in the source agent's specificConfiguration.technicalFields "
        f"(discovery only returns fields enabled there). Enable `{canonical}` on the source agent, "
        f"rediscover the table, then re-import."
    )


def reserved_technical_column(
    name: str,
    *,
    position: int = 0,
    yaml_row: Optional[Mapping[str, Any]] = None,
) -> Optional[MutableMapping[str, Any]]:
    """A discovery-shaped column for a reserved technical field, or None when ``name`` is unknown.

    Prefer the YAML row's ``type`` / nullability / ordinal when present. The ``id`` is always the
    Constants ``to64BitId`` of the canonical name. Used by tests and helpers; import refuses when
    discovery omitted the field rather than injecting this into live payloads.
    """
    canonical = canonical_technical_field_name(name)
    if canonical is None:
        return None
    row = yaml_row if isinstance(yaml_row, Mapping) else {}
    data_type = row.get("type") or row.get("dataType") or _DEFAULT_DATA_TYPES[canonical]
    ordinal = row.get("ordinalPosition")
    if ordinal is None:
        ordinal = position
    col: MutableMapping[str, Any] = {
        "id": reserved_technical_field_id(canonical),
        "position": ordinal,
        "name": canonical,
        "dataType": data_type,
        "isPK": False,
        "isIdentity": False,
        "isNullable": bool(row["isNullable"]) if "isNullable" in row else True,
    }
    for src_key, dst_key in (
        ("dataLength", "charMaxLength"),
        ("numericPrecision", "numPrec"),
        ("numericScale", "numScale"),
        ("charSet", "charSet"),
    ):
        if src_key in row and row[src_key] is not None and row[src_key] != "":
            col[dst_key] = row[src_key]
    return col


def enabled_technical_field_names(agent: Optional[Mapping[str, Any]]) -> Optional[frozenset]:
    """The agent's ``specificConfiguration.technicalFields`` as a frozenset, or None when unknown.

    ``None`` means the agent object was not available (offline / unread config) — callers may
    synthesise known reserved names. An explicit empty list means the agent produces none.
    """
    if not isinstance(agent, Mapping):
        return None
    specific = agent.get("specificConfiguration")
    if not isinstance(specific, Mapping) or "technicalFields" not in specific:
        # Key absent: treat as Hub default (empty) when we have an agent object.
        return frozenset()
    raw = specific.get("technicalFields") or []
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    names = set()
    for item in raw:
        canonical = canonical_technical_field_name(item) if isinstance(item, str) else None
        if canonical:
            names.add(canonical)
        elif isinstance(item, str) and item:
            names.add(item)
    return frozenset(names)


def _yaml_rows_naming_sources(custom_config: Optional[Mapping[str, Any]]):
    """Yield ``(source_name, yaml_row)`` for every source a YAML columns section names.

    Covers explicit ``source`` / ``sourceName`` rows and ``SourceColumnArg`` inside expressions.
    """
    if not isinstance(custom_config, Mapping):
        return
    columns = custom_config.get("columns")
    if not isinstance(columns, list):
        return
    for row in columns:
        if not isinstance(row, Mapping):
            continue
        for key in ("source", "sourceName"):
            name = row.get(key)
            if isinstance(name, str) and name:
                yield name, row
        expression = row.get("expression")
        if not isinstance(expression, Mapping):
            continue
        for arg in expression.get("arguments") or []:
            if not isinstance(arg, Mapping):
                continue
            if arg.get("type") == "SourceColumnArg":
                name = arg.get("name")
                if isinstance(name, str) and name:
                    yield name, row


def merge_reserved_technical_columns(
    columns: Optional[MutableMapping[str, Any]],
    custom_config: Optional[Mapping[str, Any]] = None,
    *,
    enabled_technical_fields: Optional[frozenset] = None,
    table_name: str = "",
) -> MutableMapping[str, Any]:
    """Append reserved technical source columns the YAML names that discovery omitted.

    Mutates and returns ``columns``. Target assembly walks discovery only; without this merge a
    YAML mapping of ``_TRANSACTION_OPERATION`` onto a target would build the source side and leave
    the target / matrix without that feed.

    ``enabled_technical_fields`` is the source agent's ``specificConfiguration.technicalFields``.
    When given (including empty), a YAML-mapped reserved name that is not enabled is refused.
    When ``None`` (agent config unread), known reserved names are synthesised so unit tests and
    offline paths still work.
    """
    from entity_mapping import EntityMappingError  # local: avoid import cycle at module load

    if columns is None:
        columns = {"columns": []}
    discovered = columns.setdefault("columns", [])
    if not isinstance(discovered, list):
        columns["columns"] = discovered = []
    present = {c.get("name") for c in discovered if isinstance(c, Mapping)}
    pending: dict[str, Mapping[str, Any]] = {}
    for name, row in _yaml_rows_naming_sources(custom_config):
        canonical = canonical_technical_field_name(name)
        if canonical is None:
            continue
        if canonical in present or canonical in pending:
            continue
        if enabled_technical_fields is not None and canonical not in enabled_technical_fields:
            raise EntityMappingError(missing_technical_field_message(table_name or "?", name))
        pending[canonical] = row
    next_position = max((c.get("position") or 0 for c in discovered if isinstance(c, Mapping)), default=0) + 1
    for name, row in pending.items():
        tech = reserved_technical_column(name, position=next_position, yaml_row=row)
        if tech is None:
            continue
        discovered.append(tech)
        present.add(name)
        next_position = max(next_position, (tech.get("position") or 0) + 1)
    return columns


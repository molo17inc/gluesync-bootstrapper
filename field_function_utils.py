"""Helpers for CoreHub Field Functions (entityType.fieldFunctions) ↔ YAML column expressions.

CoreHub stores per-column Field Functions on the Target entityType:

    fieldFunctions: [
      { columnId, tableOrObjectId, expression: { type: "Date2Str", pattern: "..." } }
    ]

Bootstrapper YAML documents them per column under ``expression`` (see field-functions docs).
This module is shared by export_template_from_corehub.py and create_all_entities.py.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from entity_mapping import EntityMappingError

# Field functions that give a column a value of their own, whatever its source brings: the fixed values and the
# technical ones (gluesync-kotlin FieldFunction.kt, isTechnicalField true but CustomFn and ScalarFn). Gluesync 2.3
# refuses them on a target column a source column feeds (P16) and accepts them on one no source feeds (null:1).
FIXED_VALUE_EXPRESSION_TYPES = frozenset({
    "StaticShort", "StaticInt", "StaticLong", "StaticFloat", "StaticDouble", "StaticBD", "StaticStr",
    "LocalTs", "LocalDate", "TsOff", "RecordID",
})

# Field functions refused on a target column a source column feeds (P16): the fixed and technical values, and the
# target scalar function, which rule (f) of the scalar function validation already refuses there at every save.
REFUSED_ON_FED_TARGET_TYPES = FIXED_VALUE_EXPRESSION_TYPES.union({"ScalarFn"})

# Field functions that produce a column no source feeds (the producer rows of Gluesync 2.3, P7-P12): the fixed and
# technical values, a custom field function and a target scalar function.
PRODUCING_EXPRESSION_TYPES = FIXED_VALUE_EXPRESSION_TYPES.union({"CustomFn", "ScalarFn"})

# UI / kotlinx noise that must not round-trip into YAML or create payloads.
_EXPRESSION_DROP_KEYS = frozenset(
    {
        "isTechnicalField",
        "requiresUserInput",
        "requireUserInput",
        "options",
        "validationRegex",
        "default",
    }
)

# Docs use charsetName; CoreHub kotlinx model uses charset.
_YAML_TO_COREHUB_PARAM_ALIASES = {
    "charsetName": "charset",
}
_COREHUB_TO_YAML_PARAM_ALIASES = {
    "charset": "charsetName",
}


def _ff_column_id(ff: Mapping[str, Any]) -> Any:
    return ff.get("columnId", ff.get("column_id"))


def _ff_table_or_object_id(ff: Mapping[str, Any]) -> Any:
    return ff.get("tableOrObjectId", ff.get("table_or_object_id"))


def normalize_expression_for_yaml(expression: Any) -> Optional[Dict[str, Any]]:
    """Strip UI-only keys and rename CoreHub params to the documented YAML shape."""
    if not isinstance(expression, dict):
        return None
    out: Dict[str, Any] = {}
    for key, value in expression.items():
        if key in _EXPRESSION_DROP_KEYS:
            continue
        yaml_key = _COREHUB_TO_YAML_PARAM_ALIASES.get(key, key)
        if isinstance(value, dict):
            nested = normalize_expression_for_yaml(value)
            out[yaml_key] = nested if nested is not None else value
        else:
            out[yaml_key] = value
    # Polymorphic kotlinx discriminator must survive.
    if "type" not in out and expression.get("type"):
        out["type"] = expression["type"]
    return out or None


def normalize_expression_for_corehub(expression: Any) -> Optional[Dict[str, Any]]:
    """Convert a YAML expression object into the CoreHub JSON shape."""
    if not isinstance(expression, dict):
        return None
    out: Dict[str, Any] = {}
    for key, value in expression.items():
        if key in _EXPRESSION_DROP_KEYS:
            continue
        core_key = _YAML_TO_COREHUB_PARAM_ALIASES.get(key, key)
        if isinstance(value, dict):
            nested = normalize_expression_for_corehub(value)
            out[core_key] = nested if nested is not None else value
        else:
            out[core_key] = value
    if "type" not in out:
        return None
    return out


def index_target_columns_by_id(columns: Sequence[Mapping[str, Any]]) -> Dict[Any, Mapping[str, Any]]:
    indexed: Dict[Any, Mapping[str, Any]] = {}
    for col in columns or []:
        if not isinstance(col, dict):
            continue
        cid = col.get("id")
        if cid is not None:
            indexed[cid] = col
    return indexed


def expressions_by_target_column_id(
    field_functions: Sequence[Mapping[str, Any]],
) -> Dict[Any, Dict[str, Any]]:
    """Map target columnId → cleaned YAML expression."""
    by_id: Dict[Any, Dict[str, Any]] = {}
    for ff in field_functions or []:
        if not isinstance(ff, dict):
            continue
        cid = _ff_column_id(ff)
        expr = normalize_expression_for_yaml(ff.get("expression"))
        if cid is None or not expr:
            continue
        by_id[cid] = expr
    return by_id


def _index_mappings_by_target_name(
    column_mappings: List[MutableMapping[str, Any]],
    target_name_key: str,
) -> Dict[str, MutableMapping[str, Any]]:
    """Index the exported column mappings by their target name (lower-cased)."""
    name_to_mapping: Dict[str, MutableMapping[str, Any]] = {}
    for mapping in column_mappings:
        tname = mapping.get(target_name_key) or mapping.get("name")
        if not tname:
            continue
        name_to_mapping[str(tname).lower()] = mapping
    return name_to_mapping


def _target_only_mapping_entry(tname: Any, expr: Dict[str, Any], tcol: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the mapping of a target-only / technical column not present in source→target mappings."""
    entry: Dict[str, Any] = {
        "target": tname,
        "expression": expr,
    }
    dtype = tcol.get("dataType")
    if dtype is None:
        dtype = tcol.get("type")
    if dtype is not None:
        entry["type"] = dtype
    return entry


def apply_field_functions_to_column_mappings(
    column_mappings: List[MutableMapping[str, Any]],
    target_columns: Sequence[Mapping[str, Any]],
    field_functions: Sequence[Mapping[str, Any]],
    *,
    target_name_key: str = "target",
) -> List[MutableMapping[str, Any]]:
    """Attach ``expression`` onto exported column mappings; append target-only FF columns.

    Matching is by target column id → name. Field functions whose target column is not
    already present in ``column_mappings`` are appended as target-only entries
    (``source`` omitted) so technical fields (StaticStr, LocalTs, …) survive export.
    """
    if not field_functions:
        return column_mappings

    target_by_id = index_target_columns_by_id(target_columns)
    expr_by_col_id = expressions_by_target_column_id(field_functions)
    if not expr_by_col_id:
        return column_mappings

    name_to_mapping = _index_mappings_by_target_name(column_mappings, target_name_key)

    for col_id, expr in expr_by_col_id.items():
        tcol = target_by_id.get(col_id)
        tname = tcol.get("name") if tcol else None
        if not tname:
            continue
        existing = name_to_mapping.get(str(tname).lower())
        if existing is not None:
            existing["expression"] = expr
            continue

        # Target-only / technical column not present in source→target mappings.
        entry = _target_only_mapping_entry(tname, expr, tcol)
        column_mappings.append(entry)
        name_to_mapping[str(tname).lower()] = entry

    return column_mappings


def _yaml_entry_target_name(entry: Dict[str, Any]) -> Any:
    """Target column name a YAML column entry carries an expression for (None when it cannot carry one)."""
    if "target" in entry and entry.get("target") is not None:
        return entry.get("target")
    if "source" in entry:
        return entry.get("target") or entry.get("source")
    if "name" in entry:
        return entry.get("name")
    # Legacy {SRC: "TGT"} cannot carry expression alongside the mapping key.
    return None


def iter_yaml_column_expressions(
    yaml_columns: Optional[Sequence[Any]],
) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (target_column_name, corehub_expression) from YAML column entries."""
    for entry in yaml_columns or []:
        if not isinstance(entry, dict):
            continue
        raw_expr = entry.get("expression")
        if not raw_expr:
            continue
        expr = normalize_expression_for_corehub(raw_expr)
        if not expr:
            continue

        target_name = _yaml_entry_target_name(entry)
        if target_name is None or target_name == "":
            continue
        yield str(target_name), expr


def produced_target_names(yaml_columns: Optional[Sequence[Any]]) -> List[str]:
    """Target column names whose YAML ``expression`` produces a value (see ``PRODUCING_EXPRESSION_TYPES``)."""
    return [
        target_name
        for target_name, expr in iter_yaml_column_expressions(yaml_columns)
        if expr.get("type") in PRODUCING_EXPRESSION_TYPES
    ]


def _refuse_values_on_fed_targets(
    yaml_columns: Optional[Sequence[Any]],
    fed_target_names: Iterable[str],
    label: str,
) -> None:
    """Raise ``EntityMappingError`` when a fixed/technical value or scalar function sits on a fed target column."""
    fed = {str(name).lower() for name in fed_target_names}
    for target_name, expr in iter_yaml_column_expressions(yaml_columns):
        if expr.get("type") in REFUSED_ON_FED_TARGET_TYPES and target_name.lower() in fed:
            raise EntityMappingError(
                f"Table {label}: the {expr.get('type')} field function on target column {target_name} gives it a "
                f"value of its own while a source column feeds it, which a Gluesync 2.3 CoreHub refuses (P16): "
                f"remove the source from that row, so that no source column feeds the column (null:1)"
            )


def _index_target_columns_by_name(target_columns: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """Index target columns by name (or alias), lower-cased."""
    by_name: Dict[str, Mapping[str, Any]] = {}
    for col in target_columns or []:
        if not isinstance(col, dict):
            continue
        name = col.get("name") or col.get("alias")
        if name:
            by_name[str(name).lower()] = col
    return by_name


def build_field_functions_for_entity_type(
    yaml_columns: Optional[Sequence[Any]],
    target_columns: Sequence[Mapping[str, Any]],
    table_or_object_id: Any,
    *,
    fed_target_names: Optional[Iterable[str]] = None,
    label: str = "",
) -> List[Dict[str, Any]]:
    """Rebuild CoreHub ``fieldFunctions`` from YAML column expressions.

    ``target_columns`` must carry real target column ``id`` / ``name`` values
    (discovery or the assembled target columns_def).

    ``fed_target_names``: on a Gluesync 2.3 CoreHub, the target columns a source column feeds. A fixed or
    technical value, or a target scalar function, on one of them is refused (P16, BOP-14 A), raising
    ``EntityMappingError``: the runtime would write it over the value the source brings.
    """
    if table_or_object_id is None:
        return []
    if fed_target_names is not None:
        _refuse_values_on_fed_targets(yaml_columns, fed_target_names, label)

    by_name = _index_target_columns_by_name(target_columns)

    field_functions: List[Dict[str, Any]] = []
    seen_column_ids = set()
    for target_name, expr in iter_yaml_column_expressions(yaml_columns):
        tcol = by_name.get(target_name.lower())
        if not tcol:
            continue
        col_id = tcol.get("id")
        if col_id is None or col_id in seen_column_ids:
            continue
        field_functions.append(
            {
                "columnId": col_id,
                "tableOrObjectId": table_or_object_id,
                "expression": expr,
            }
        )
        seen_column_ids.add(col_id)
    return field_functions

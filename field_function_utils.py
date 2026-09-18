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

    # name(lower) → column id for columns already present in mappings
    mapped_target_ids = set()
    name_to_mapping: Dict[str, MutableMapping[str, Any]] = {}
    for mapping in column_mappings:
        tname = mapping.get(target_name_key) or mapping.get("name")
        if not tname:
            continue
        name_to_mapping[str(tname).lower()] = mapping

    for col_id, expr in expr_by_col_id.items():
        tcol = target_by_id.get(col_id)
        tname = tcol.get("name") if tcol else None
        if not tname:
            continue
        existing = name_to_mapping.get(str(tname).lower())
        if existing is not None:
            existing["expression"] = expr
            mapped_target_ids.add(col_id)
            continue

        # Target-only / technical column not present in source→target mappings.
        entry: Dict[str, Any] = {
            "target": tname,
            "expression": expr,
        }
        dtype = tcol.get("dataType") if tcol else None
        if dtype is None and tcol is not None:
            dtype = tcol.get("type")
        if dtype is not None:
            entry["type"] = dtype
        column_mappings.append(entry)
        mapped_target_ids.add(col_id)
        name_to_mapping[str(tname).lower()] = entry

    return column_mappings


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

        target_name = None
        if "target" in entry and entry.get("target") is not None:
            target_name = entry.get("target")
        elif "source" in entry:
            target_name = entry.get("target") or entry.get("source")
        elif "name" in entry:
            target_name = entry.get("name")
        else:
            # Legacy {SRC: "TGT"} cannot carry expression alongside the mapping key.
            continue

        if target_name is None or target_name == "":
            continue
        yield str(target_name), expr


def build_field_functions_for_entity_type(
    yaml_columns: Optional[Sequence[Any]],
    target_columns: Sequence[Mapping[str, Any]],
    table_or_object_id: Any,
) -> List[Dict[str, Any]]:
    """Rebuild CoreHub ``fieldFunctions`` from YAML column expressions.

    ``target_columns`` must carry real target column ``id`` / ``name`` values
    (discovery or the assembled target columns_def).
    """
    if table_or_object_id is None:
        return []

    by_name: Dict[str, Mapping[str, Any]] = {}
    for col in target_columns or []:
        if not isinstance(col, dict):
            continue
        name = col.get("name") or col.get("alias")
        if name:
            by_name[str(name).lower()] = col

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

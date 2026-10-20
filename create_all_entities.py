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

import os
import json
import urllib3
import argparse
import copy
import itertools
from dataclasses import dataclass
from functools import cached_property
from typing import Any
from commons import get_node_info, get_table_columns, fetch_core_hub, get_pipeline_config, get_pipeline_agents, \
    get_agent_tables, create_entity_schedules, map_data_type, create_pipeline_schedules, create_group_schedules, load_yaml_config, \
    process_filter_clauses, create_group, assign_entities_to_group, get_table_id, extract_target_column_types, \
    corehub_takes_grouped_matrix
from create_all_tables import handle_table_creation
from create_user_defined_functions import handle_udf_function_definition
from field_function_utils import build_field_functions_for_entity_type, normalize_expression_for_corehub, \
    produced_target_names
from entity_mapping import ColumnNames, EntityMappingError, build_entries, derive_keys, drop_forbidden_key_links, \
    encode_entries, find_by_name, group_relations, grouped_only_forms, read_entry, yaml_relations
import create_user_defined_functions as udf_module
import function_collection
from utils.log import get_logger, create_log_file, log_success, log_failure, lockfile_failure, lockfile_complete, exit_on_fail
from utils.core_hub_client import CoreHubClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Metadata-only fields used in the export format {source: ..., target: ..., type: ..., ...}
# These are never column names in the explicit export format.
_EXPORT_METADATA_FIELDS = {"source", "target", "type", "dataLength", "numericPrecision", "numericScale", "isNullable", "id", "ordinalPosition", "targetPosition", "targetIsNullable", "targetDataType", "charSet", "expression"}

# Tells "the YAML declared charSet: null" apart from "the YAML said nothing about charSet".
# The first must clear the character set discovered on the column, the second must leave it
# untouched, and None cannot express both.
_CHAR_SET_CLEARED = object()


def _build_target_data_type_overrides(yaml_columns, names=None):
    """Build a lookup of user-declared target dataType overrides keyed by source column name.

    Users can declare a ``targetDataType`` key alongside a column mapping or whitelist
    entry to force a specific target native dataType, bypassing automatic mapping via
    the gluesync data type matrix. This is useful when the target agent's matrix does
    not advertise a mapping for the source's gluesyncDataType, or when the user wants
    to override the computed value.

    Supported YAML shapes:

    1. Export/explicit mapping:
       ``{source: COL, target: tgt, targetDataType: varchar(100)}``
    2. Whitelist format:
       ``{name: tgt, sourceName: COL, type: ..., targetDataType: varchar(100)}``

    The legacy shorthand mapping ``{COL: "tgt"}`` has no room for extra keys and thus
    cannot carry a ``targetDataType`` override; switch to the explicit form to use it.

    ``names`` (:class:`entity_mapping.ColumnNames` of the source columns) tells names that differ by case only apart.

    Returns:
        dict: { source_column_name: targetDataType_string }, the name as ``names.key`` gives it (lower-cased when
        it names no source column or ``names`` is None)
    """
    key = (names or ColumnNames(())).key
    overrides = {}
    if not isinstance(yaml_columns, list):
        return overrides
    for entry in yaml_columns:
        if not isinstance(entry, dict):
            continue
        override_value = entry.get('targetDataType')
        if not override_value:
            continue
        src = _yaml_row_source_name(entry)
        if src:
            overrides[key(src)] = override_value
    return overrides


def _yaml_row_source_name(entry):
    """The source column a YAML ``columns:`` row names: ``source`` (export/explicit mapping shape), else
    ``sourceName`` or ``name`` (whitelist shape); None for a row of neither shape."""
    if 'source' in entry:
        return entry.get('source')
    if 'name' in entry:
        return entry.get('sourceName') or entry.get('name')
    return None


def _build_char_set_overrides(yaml_columns, names=None):
    """Build a lookup of user-declared ``charSet`` values keyed by source column name.

    The source agent publishes on every column the character set the column declares —
    a charset name on most databases, the CCSID as a decimal string on AS400 (Db2 for i) —
    and the user can ask for the data to be read with a different one by editing that value.
    Declaring it here is the YAML equivalent of that edit, so a pipeline created from a
    template reads the column exactly like one configured from the UI.

    Supported YAML shapes (same as ``targetDataType``):

    1. Export/explicit mapping:
       ``{source: COL, target: tgt, charSet: "280"}``
    2. Whitelist format:
       ``{name: tgt, sourceName: COL, type: ..., charSet: "280"}``

    An explicit ``charSet: null`` clears the value discovered on the column, which is not
    the same as omitting the key: omitting it keeps whatever the source published. The
    legacy shorthand mapping ``{COL: "tgt"}`` has no room for extra keys — any key it
    carries is read as another column mapping — so switch to the explicit form to use it.

    ``names`` (:class:`entity_mapping.ColumnNames` of the source columns) tells names that differ by case only apart.

    Returns:
        dict: { source_column_name: charSet_string | _CHAR_SET_CLEARED }, the name as ``names.key`` gives it
        (lower-cased when it names no source column or ``names`` is None)
    """
    key = (names or ColumnNames(())).key
    overrides = {}
    if not isinstance(yaml_columns, list):
        return overrides
    for entry in yaml_columns:
        if not isinstance(entry, dict) or 'charSet' not in entry:
            continue
        # Export/explicit mapping shape, then whitelist shape
        src = _yaml_row_source_name(entry)
        if not src:
            continue
        value = entry.get('charSet')
        overrides[key(src)] = _CHAR_SET_CLEARED if value is None else str(value)
    return overrides


def _apply_char_set_overrides(columns, overrides, table_name=None, names=None):
    """Apply ``charSet`` overrides from :func:`_build_char_set_overrides` in place.

    Runs on the *source* columns after they have been built from discovery, so the
    user-declared value wins over the one the source published. Columns the YAML says
    nothing about are left exactly as discovered. ``names`` as for :func:`_build_char_set_overrides`.
    """
    if not overrides or not columns:
        return
    key = (names or ColumnNames(())).key
    where = f' of table {table_name}' if table_name else ''
    for col in columns:
        if not isinstance(col, dict):
            continue
        override = overrides.get(key(col.get('name', '')))
        if override is not None:
            _apply_char_set_override(col, override, where)


def _apply_char_set_override(col, override, where):
    """Set (or clear, for :data:`_CHAR_SET_CLEARED`) the ``charSet`` of one source column, logging it."""
    if override is _CHAR_SET_CLEARED:
        # Omitted rather than sent as null: that is how a column with no character set
        # is represented in the entity payload.
        col.pop('charSet', None)
        logger.info(f"Column '{col.get('name')}'{where}: charSet cleared by YAML")
    else:
        col['charSet'] = override
        logger.info(f"Column '{col.get('name')}'{where}: charSet overridden to '{override}' by YAML")


def _is_target_known_type(data_type, target_node_info):
    """Return True if ``data_type`` is advertised by the target agent's matrix.

    A type is considered known when it appears (case-insensitive) as a
    ``defaultType`` or in any entry's ``supportedTypes`` of the target's
    ``dataTypesMatrix``. Used to detect whether ``map_data_type`` produced a
    target-compatible value, even when that value happens to be identical to
    the source dataType (e.g. PostgreSQL ``numeric`` -> Vertica ``numeric``).
    """
    return _canonicalize_target_type(data_type, target_node_info) is not None


def _canonicalize_target_type(data_type, target_node_info):
    """Return the canonical target-matrix casing for ``data_type`` or None.

    Looks up ``data_type`` (case-insensitively) in the target's
    ``dataTypesMatrix`` ``defaultType``/``supportedTypes`` lists and returns
    the value with the matrix's original casing. Returns None when no match
    is found. ``defaultType`` matches take precedence over ``supportedTypes``.
    """
    if not data_type or not target_node_info:
        return None
    matrix = target_node_info.get('dataTypesMatrix') or []
    needle = str(data_type).strip().lower()
    if not needle:
        return None
    supported_match = None
    for entry in matrix:
        if not isinstance(entry, dict):
            continue
        default_type = entry.get('defaultType')
        if default_type and str(default_type).strip().lower() == needle:
            return str(default_type)
        if supported_match is None:
            supported_match = _supported_type_match(entry, needle)
    return supported_match


def _supported_type_match(matrix_entry, needle):
    """The first of the ``supportedTypes`` of a ``dataTypesMatrix`` entry spelt ``needle`` case-insensitively."""
    for st in matrix_entry.get('supportedTypes') or []:
        if st and str(st).strip().lower() == needle:
            return str(st)
    return None


def _is_in_keys(col_name, custom_config, names=None):
    """Whether the YAML ``keys:`` name the column ``col_name``: spelt so, else, when no column is, case-insensitively.

    ``names`` are the :class:`entity_mapping.ColumnNames` of the table: with ``id`` and ``ID`` both columns, the key
    ``id`` is the column ``id`` alone. Without them a key is compared case-insensitively.
    """
    if not custom_config or 'keys' not in custom_config or custom_config['keys'] is None:
        return False
    key = (names or ColumnNames(())).key
    keys = custom_config['keys']
    for k in keys:
        if isinstance(k, str) and key(k) == key(col_name):
            return True
        elif isinstance(k, dict):
            # keys can be like [{'id': {'name': 'ID'}}] or something else, but let's handle the common case
            key_name = next(iter(k)) if not k.get('name') else k['name']
            if key(key_name) == key(col_name):
                return True
    return False

def _enrich_column(col_dict, source_col):
    if not source_col:
        return col_dict
    for field in ['charMaxLength', 'charOctetLength', 'charSet', 'collation', 'numPrec', 'numPrecRadix', 'numScale', 'datetimePrec', 'udtCategory', 'udtName']:
        if field in source_col and source_col[field] is not None:
            col_dict[field] = source_col[field]
    return col_dict

def _is_column_mappings(columns_list):
    """
    Return True if the columns list contains column mappings (not column definitions).

    Detects both:
    - Export format:  [{source: "col", target: "COL", type: ..., ...}]
    - User format:    [{col_name: "target_name"}, ...]  e.g. [{id: "ID"}]

    Column *definitions* (not mappings) have a 'name' key.
    """
    if not columns_list:
        return False
    first = columns_list[0]
    if not isinstance(first, dict):
        return False
    # Export format: has explicit 'source' key
    if "source" in first:
        return True
    # User format: no 'name' key (column definitions always have 'name')
    return "name" not in first


def _is_column_whitelist(columns_list):
    """
    Return True if the columns list contains column definitions (whitelist format).

    This format is used by parse_dbmoto_metadata_xml.py and represents
    an explicit whitelist of columns to include (not mappings).

    Detects:
    - Whitelist format: [{name: "col", type: "VARCHAR", sourceName: "COL"}, ...]

    Returns True when entries have 'name' and 'type' keys (column definitions).
    """
    if not columns_list:
        return False
    first = columns_list[0]
    if not isinstance(first, dict):
        return False
    # Whitelist format: has both 'name' and 'type' keys
    return "name" in first and "type" in first


def _relation_pairs(relations):
    """``(source_name, target_name)`` for each source of each YAML relation, in YAML order.

    The pairs the per-table assembly walks to build columns: an N:1 relation gives one pair per source, a
    1:null relation pairs its source with itself and a null:1 relation gives none (its column is added on
    its own). ``relations`` comes from :func:`entity_mapping.yaml_relations`.
    """
    for relation in relations or []:
        target_name = relation["targets"][0] if relation["targets"] else None
        for source_name in relation["sources"]:
            yield source_name, target_name if target_name is not None else source_name


# Agent tags whose key flags are no primary key: the key rules read no physical key there, as CoreHub does
# (gluesync-kotlin CheckEntitiesPhysicalKeysSymmetryComponentSetup isPhysicalKeyHonoured).
_NO_PHYSICAL_KEY_AGENT_TAGS = frozenset({"cassandra", "scylladb"})


def _name_relations(relations, columns_def, table_name, target_names=None):
    """The relations of a Gluesync 2.3 entity by column name.

    The YAML relations (:func:`entity_mapping.group_relations`), and one relation onto the target of its name
    (guessed, so ``explicit`` is false) for each source column no YAML row names: every column when the YAML maps
    none, a key column its rows leave out otherwise. ``target_names`` are the :class:`entity_mapping.ColumnNames` of
    the target columns: names that differ by case only are different columns.
    """
    source_names = ColumnNames(c["name"] for c in columns_def)
    stated = [r for r in relations or [] if r["sources"]]
    named = {source_names.key(s) for r in stated for s in r["sources"]}
    by_name = [
        {"sources": [c["name"]], "targets": [c.get("alias") or c["name"]], "explicit": False}
        for c in columns_def
        if source_names.key(c["name"]) not in named
    ]
    if stated:
        # A key column the YAML keys name but its rows leave out is mapped by name, as it always was.
        return group_relations(relations, label=table_name, source_names=source_names,
                               target_names=target_names) + by_name
    return by_name + group_relations([r for r in relations or [] if not r["sources"]], label=table_name,
                                     source_names=source_names, target_names=target_names)


def _id_resolver(columns):
    """``resolve(name)``: the id of the column spelt ``name``, else of the one column spelt so case-insensitively.

    A NoSQL source may hold columns that differ by case only (Cosmos DB's ``id`` next to a document field ``ID``):
    the exact spelling wins, so they stay two columns. Returns None for no column, or for several case-insensitive
    matches and no exact one.
    """
    exact, folded = {}, {}
    for col in columns:
        name = str(col.get("name", ""))
        exact.setdefault(name, col.get("id"))
        ids = folded.setdefault(name.lower(), [])
        if col.get("id") not in ids:
            ids.append(col.get("id"))

    def resolve(name):
        if name in exact:
            return exact[name]
        ids = folded.get(str(name).lower(), [])
        return ids[0] if len(ids) == 1 else None
    return resolve


def _relations_by_id(name_relations, columns_def, target_columns_def, table_name):
    """``name_relations`` over the column ids of the entity: the exact spelling first, then case-insensitively."""
    source_id, target_id = _id_resolver(columns_def), _id_resolver(target_columns_def)
    by_id = []
    for relation in name_relations:
        resolved = {}
        for side, names, resolve in (("source", relation["sources"], source_id), ("target", relation["targets"], target_id)):
            ids = [resolve(n) for n in names]
            missing = [n for n, i in zip(names, ids) if i is None]
            if missing:
                raise EntityMappingError(
                    f"Table {table_name}: the column mapping names the {side} column(s) {', '.join(missing)}, which "
                    f"the entity does not hold, or holds under several spellings that differ by case only"
                )
            resolved[side] = ids
        by_id.append({"sources": resolved["source"], "targets": resolved["target"], "explicit": relation.get("explicit", True)})
    return by_id


def _target_table_name(custom_config, table_name, yaml_table_key):
    """The target table of a source table: the YAML ``name``, else the source table name.

    The YAML ``name`` stores the target table name as CoreHub recorded it. For AS/400 sources the agent may return a
    library-qualified name (e.g. FILELIB.TABLE), stored verbatim: when it is a library prefix prepended to the YAML
    key, the prefix is stripped so the target entity gets the unqualified name.
    """
    raw_target_name = custom_config.get('name', table_name)
    if raw_target_name != table_name and '.' in raw_target_name and raw_target_name.rsplit('.', 1)[-1] == yaml_table_key:
        target_table_name = raw_target_name.rsplit('.', 1)[-1]
        logger.info(
            f"Stripped library prefix from target table name: '{raw_target_name}' → '{target_table_name}' "
            f"(source table key: '{yaml_table_key}')"
        )
        return target_table_name
    return raw_target_name


def _refuse_forms_of_gluesync_23(relations, custom_config, table_name, source_names=None):
    """Refuse the column mapping forms only a Gluesync 2.3 CoreHub can store, for an older one."""
    forms = grouped_only_forms(relations, custom_config, source_names)
    if forms:
        raise EntityMappingError(
            f"Table {table_name} uses column mapping forms that a CoreHub older than Gluesync 2.3 cannot store: "
            f"{'; '.join(forms)}"
        )


def _http_status(error):
    """The HTTP status of a failed CoreHub request (commons.fetch_core_hub chains requests' error), or None."""
    while error is not None:
        response = getattr(error, "response", None)
        if response is not None and getattr(response, "status_code", None) is not None:
            return response.status_code
        error = error.__cause__
    return None


def _coerce_is_pk(value):
    """Normalize discovery/YAML isPK values to a real bool for CoreHub."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _yaml_key_names(custom_config):
    """Lower-cased key names declared in YAML ``keys:`` (strings or dict shapes)."""
    if not custom_config or custom_config.get("keys") is None:
        return set()
    names = set()
    for k in custom_config.get("keys") or []:
        if isinstance(k, str):
            names.add(k.lower())
        elif isinstance(k, dict):
            key_name = k.get("name")
            if key_name is None and k:
                key_name = next(iter(k))
            if key_name is not None:
                names.add(str(key_name).lower())
    return names


def _paint_is_pk_on_columns(columns_def, key_column_dicts):
    """Force isPK=True on columns that appear in the resolved keys list.

    CoreHub derives SingleTable primary keys solely from ``columns.filter(isPK)``.
    The bootstrapper also builds a separate ``keys`` list for CREATE TABLE; if the
    two ever diverge (Field Functions mapping extraction, case mismatch on pgsql,
    null discovery isPK), the PUT fails with "Missing entity primary keys".
    Painting after keys are resolved keeps both views aligned.
    """
    if not columns_def or not key_column_dicts:
        return
    # a name spelt as a column is that column alone: ``id`` and ``ID`` are two columns (a Cosmos DB container has both)
    names = ColumnNames(c.get("name") for c in columns_def if isinstance(c, dict) and c.get("name") is not None)
    key_names = {
        names.key(k.get("name"))
        for k in key_column_dicts
        if isinstance(k, dict) and k.get("name") is not None
    }
    if not key_names:
        return
    for col in columns_def:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        if name is not None and names.key(name) in key_names:
            col["isPK"] = True


_MISSING_ID_DETAIL = " This indicates a serious issue with the discovery API."


def _raise_missing_column_id(column_name, table_name, detail=True):
    """Log and raise the error of a discovered column that CoreHub returned without its ``id``."""
    error_msg = f"CRITICAL ERROR: Column '{column_name}' in table {table_name} is missing 'id' field in CoreHub API response."
    if detail:
        error_msg += _MISSING_ID_DETAIL
    logger.error(error_msg)
    raise ValueError(error_msg)


def _require_column_id(col, table_name, label=None, detail=True):
    """The ``id`` of a discovered column, refused (see :func:`_raise_missing_column_id`) when CoreHub left it out.

    ``label`` names the column in the error, the discovered name when None.
    """
    col_id = col.get('id')
    if col_id is None:
        _raise_missing_column_id(col.get('name') if label is None else label, table_name, detail)
    return col_id


def _source_is_pk(col, name, custom_config, names=None):
    """Whether a discovered source column is a key: discovery flags it, or the YAML ``keys:`` name ``name``.

    ``names``: the :class:`entity_mapping.ColumnNames` of the discovered columns."""
    return _coerce_is_pk(col.get("isPK")) or _is_in_keys(name, custom_config, names)


def _source_column_def(col, col_id, name, alias, is_pk, nullable_default=False):
    """A source (or target) column of a SingleTable entity built from the discovered column ``col``."""
    return _enrich_column({
        "id": col_id,
        "position": col.get("position", 0),
        "name": name,
        "alias": alias,
        "dataType": col.get("dataType"),
        "isPK": is_pk,
        "isIdentity": col.get("isIdentity", False),
        "isNullable": col.get("isNullable", nullable_default),
    }, col)


def _source_key_def(col, col_id, alias):
    """A source key of a SingleTable entity built from the discovered column ``col``."""
    return _enrich_column({
        "id": col_id,  # Use actual ordinal position from database
        "position": col.get("position", 0),
        "name": col["name"],
        "alias": alias,
        "dataType": col.get("dataType")
    }, col)


def _legacy_discovery_types(columns, custom_config, table_name):
    """The discovered type of each source column, when every YAML column type is null (legacy backup), else {}.

    An old Gluesync version stored Column with a DataTypeInterface field that was not registered for
    serialisation, so null was written to SQLite and propagated to exports. The discovery-based lookup lets the
    entity creation resolve types without relying on the (broken) YAML type field.
    """
    _yaml_cols = custom_config.get('columns') or []
    _null_type_cols = [
        c for c in _yaml_cols
        if isinstance(c, dict) and "source" in c and c.get("type") is None
    ]
    _discovery_type_by_source = {}
    if not (_null_type_cols and len(_null_type_cols) == len(_yaml_cols) and _yaml_cols):
        return _discovery_type_by_source
    logger.warning(
        f"Table {table_name}: all {len(_yaml_cols)} YAML column type fields are null – "
        f"this backup was exported from a Gluesync instance with legacy entity storage. "
        f"Falling back to live CoreHub discovery for column type resolution."
    )
    for disc_col in (columns.get("columns") or []):
        _disc_name = disc_col.get("name") or ""
        _disc_dt = disc_col.get("dataType") or disc_col.get("type")
        if _disc_name and _disc_dt:
            _discovery_type_by_source[_disc_name] = _disc_dt
    if _discovery_type_by_source:
        logger.info(
            f"Table {table_name}: resolved {len(_discovery_type_by_source)} column types "
            f"from live source discovery (will be used as fallback for null YAML types)"
        )
    return _discovery_type_by_source


def _mapped_source_column(matched_col, target_name, columns_def, custom_config, table_name, grouped, names=None):
    """The source column of a YAML mapping pair whose source discovery found; None for a repeat on 2.3."""
    col_id = _require_column_id(matched_col, table_name, detail=False)
    if grouped and any(c.get("name") == matched_col["name"] for c in columns_def):
        return None
    return _source_column_def(matched_col, col_id, matched_col["name"], target_name,
                              _source_is_pk(matched_col, matched_col["name"], custom_config, names))


def _unmatched_mapping_column(source_name, target_name, idx, custom_config, table_name, source_schema, grouped,
                              names=None):
    """The source column of a YAML mapping pair whose source discovery did not find (refused on 2.3)."""
    if grouped:
        raise EntityMappingError(
            f"Table {table_name}: column {source_name} of the YAML columns is not a column of the source "
            f"table discovery returns, and a Gluesync 2.3 CoreHub refuses a mapping onto no column"
        )
    logger.warning(f"Warning: Column '{source_name}' from YAML mapping not found in discovery for table {table_name}. Using YAML metadata.")
    # Fallback for when discovery didn't find the column
    # (e.g. table creation pending or discovery glitch)
    return {
        "id": get_table_id(source_schema, source_name), # Generated ID
        "position": idx,
        "name": source_name,
        "alias": target_name,
        "dataType": "VARCHAR", # Fallback type
        "isPK": _is_in_keys(source_name, custom_config, names),
        "isIdentity": False,
        "isNullable": True
    }


def _source_columns_from_mappings(columns, custom_config, relations, table_name, source_schema, grouped, names=None):
    """The source columns of custom column mappings (source→target)."""
    columns_def = []
    yaml_columns = custom_config.get('columns', [])
    row_index = {id(row): idx for idx, row in enumerate(yaml_columns, 1)}
    for relation in relations or []:
        idx = row_index.get(id(relation["row"]), 0)
        for source_name, target_name in _relation_pairs([relation]):
            # Find the matching column from discovered columns: spelt as the YAML spells it, else case-insensitively
            matched_col = find_by_name(columns["columns"], source_name)
            if matched_col:
                col_def = _mapped_source_column(matched_col, target_name, columns_def, custom_config, table_name,
                                                grouped, names)
            else:
                col_def = _unmatched_mapping_column(source_name, target_name, idx, custom_config, table_name,
                                                    source_schema, grouped, names)
            if col_def is not None:
                columns_def.append(col_def)
    return columns_def


def _source_columns_from_whitelist(columns, custom_config, relations, table_name, grouped, names=None):
    """The source columns of a column whitelist, and the relations of the rows the source holds.

    Only the specified columns are used, not all discovered ones.
    Format: [{name: "col", type: "VARCHAR", sourceName: "SOURCE_COL"}, ...]
    """
    columns_def = []
    yaml_columns = custom_config.get('columns', [])
    unmatched_rows = set()
    for yaml_col in yaml_columns:
        # sourceName is the source column name; name is the target column name
        source_col_name = yaml_col.get('sourceName') or yaml_col.get('name')
        # Find the matching column from discovered columns
        col = find_by_name(columns["columns"], source_col_name)
        if col is None:
            logger.warning(f"Column '{source_col_name}' from YAML whitelist not found in discovered columns for table {table_name}")
            unmatched_rows.add(id(yaml_col))
            continue
        col_id = _require_column_id(col, table_name, detail=False)
        # Source columns MUST reflect the discovered native type from the
        # source database. The YAML `type:` / `targetDataType:` keys describe
        # the TARGET column and must never bleed into the source payload.
        # Discovered columns expose their native type under `dataType`.
        # On 2.3 the key rules compare names case-sensitively: the spelling discovery reports (BOP-15).
        columns_def.append(_source_column_def(
            col, col_id, col["name"] if grouped else source_col_name, yaml_col.get('name'),
            _source_is_pk(col, source_col_name, custom_config, names), nullable_default=True,
        ))

    logger.info(f"Using column whitelist for {table_name}: {len(columns_def)} of {len(yaml_columns)} specified columns matched")
    # A whitelist row the source does not hold is left out of the entity (it may still define a column of the
    # target table created from the YAML), so it states no relation either, as before Gluesync 2.3.
    if relations is not None and unmatched_rows:
        relations = [r for r in relations if id(r.get("row")) not in unmatched_rows]
    return columns_def, relations


def _source_columns_from_discovery(columns, custom_config, table_name, names=None):
    """Every discovered column as-is (one-to-one source→target mapping)."""
    columns_def = []
    for col in columns["columns"]:
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, table_name)
        columns_def.append(_source_column_def(col, col_id, col["name"], col["name"],
                                              _source_is_pk(col, col["name"], custom_config, names)))
    return columns_def


def _append_mapped_source_keys(keys, col, col_id, relations, custom_config, names):
    """Append the keys the YAML mapping pairs make of the discovered column ``col``."""
    in_keys = _is_in_keys(col["name"], custom_config, names)
    for column_map in relations or []:
        for source_name, target_name in _relation_pairs([column_map]):
            name_match = names.key(source_name) == col["name"]
            if name_match and in_keys:
                keys.append(_source_key_def(col, col_id, target_name))
                logger.info(f"Key matched: col={col['name']!r}, source_name={source_name!r}, target_name={target_name!r}")
            elif in_keys:
                logger.warning(f"Key col={col['name']!r} is in keys list but source_name={source_name!r} did not match (name_match={name_match}, in_keys={in_keys})")


def _source_key_from_columns_def(key_name, columns_def, keys, found_key_names, table_name, source_schema, names):
    """Resolve a YAML key discovery does not hold from the columns already built, else refuse it."""
    matched_def = find_by_name(columns_def, key_name)
    if matched_def:
        keys.append(copy.deepcopy(matched_def))
        found_key_names.add(names.key(key_name))
        logger.info(
            f"Key {key_name!r} resolved from columns_def after mapping miss for {table_name}"
        )
        return
    error_msg = (
        f"CRITICAL ERROR: Key '{key_name}' not found in discovery or columns_def "
        f"for table '{table_name}' in schema '{source_schema}'. This key is required "
        f"by the YAML configuration but does not exist in the source database discovery."
    )
    logger.error(error_msg)
    raise ValueError(error_msg)


def _resolve_missed_source_key(key_name, columns, columns_def, keys, found_key_names, table_name, source_schema, names):
    """Resolve from discovery a YAML key the mapping pairs did not find (e.g. historical id-as-metadata skip, or
    key omitted from ``columns:``), re-adding its column to ``columns_def`` when the mapping dropped it."""
    if isinstance(key_name, dict):
        key_name = key_name.get("name") or (next(iter(key_name)) if key_name else None)
    if key_name is None or names.key(key_name) in found_key_names:
        return
    matched = find_by_name(columns["columns"], key_name)
    if not matched:
        _source_key_from_columns_def(key_name, columns_def, keys, found_key_names, table_name, source_schema, names)
        return
    col_id = _require_column_id(matched, table_name, label=key_name)
    keys.append(_source_key_def(matched, col_id, matched["name"]))
    found_key_names.add(names.key(matched["name"]))
    logger.info(
        f"Key {key_name!r} resolved from discovery after mapping miss for {table_name}"
    )
    # Ensure the PK column is present on columns_def (mapping may have dropped it)
    if not any(c.get("name") == matched["name"] for c in columns_def):
        columns_def.append(_source_column_def(matched, col_id, matched["name"], matched["name"], True))
        logger.info(
            f"Re-added key column {matched['name']!r} to columns_def for {table_name}"
        )


def _source_keys_from_mappings(columns, custom_config, relations, columns_def, table_name, source_schema, names):
    """The source keys of a table with column mappings: the YAML keys through the mapping pairs."""
    logger.info(f"Key extraction via column mappings for {table_name}. Keys to find: {custom_config['keys']}")
    keys = []
    for col in columns["columns"]:
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, table_name)
        _append_mapped_source_keys(keys, col, col_id, relations, custom_config, names)
    # If YAML keys: were not paired via mappings, resolve remaining keys from discovery.
    found_key_names = {
        names.key(k.get("name"))
        for k in keys
        if isinstance(k, dict) and k.get("name") is not None
    }
    for key_name in custom_config["keys"]:
        _resolve_missed_source_key(key_name, columns, columns_def, keys, found_key_names, table_name, source_schema,
                                   names)
    return keys


def _append_named_source_key(keys, key_name, columns, columns_def, table_name, source_schema):
    """Append the source key a YAML key names, found in discovery, else in the columns already built."""
    # Find the column: spelt as the key is, else case-insensitively (cross-db scenarios like pgsql→oracle)
    col = find_by_name(columns["columns"], key_name)
    if col is not None:
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, table_name, label=key_name)
        keys.append(_source_key_def(col, col_id, col["name"]))
        logger.debug(f"Found key '{key_name}' in columns with id={col_id}")
        return
    logger.warning(f"Warning: Key '{key_name}' not found in columns for table '{table_name}'. Searching in columns_def...")
    # Try to find it in the already built columns_def (which might have come from YAML metadata)
    col_def = find_by_name(columns_def, key_name)
    if col_def is not None:
        keys.append(copy.deepcopy(col_def))
        logger.info(f"Found key '{key_name}' in columns_def")
        return
    error_msg = f"CRITICAL ERROR: Key '{key_name}' not found in discovery or columns_def for table '{table_name}' in schema '{source_schema}'. This key is required by the YAML configuration but does not exist in the source database discovery."
    logger.error(error_msg)
    raise ValueError(error_msg)


def _source_keys_from_pk(columns, table_name):
    """The source keys discovery flags as primary key."""
    keys = []
    for col in columns["columns"]:
        if _coerce_is_pk(col.get("isPK")):
            # Use the id field from CoreHub API as the column ID
            col_id = _require_column_id(col, table_name)
            keys.append(_source_key_def(col, col_id, col["name"]))
    return keys


def _assemble_source_keys(columns, custom_config, relations, columns_def, table_name, source_schema, names):
    """The source keys of a SingleTable entity: the YAML ``keys:`` when given, else discovery's primary key."""
    if not (custom_config and 'keys' in custom_config and custom_config['keys'] is not None):
        keys = _source_keys_from_pk(columns, table_name)
        logger.debug(f"Using primary keys for {table_name}: {keys}")
        return keys
    logger.info(f"Processing keys for table {table_name}. Requested keys: {custom_config['keys']}")
    logger.info(f"Has columns section: {bool(custom_config.get('columns'))}")
    if _is_column_mappings(custom_config.get('columns', [])):
        # Use column mappings for keys
        keys = _source_keys_from_mappings(columns, custom_config, relations, columns_def, table_name, source_schema,
                                          names)
    else:
        # No column mappings, use keys directly from source columns
        logger.debug(f"Processing keys for {table_name}: {custom_config['keys']}")
        logger.debug(f"Available columns: {[col['name'] for col in columns['columns']]}")
        keys = []
        for key_name in custom_config['keys']:
            _append_named_source_key(keys, key_name, columns, columns_def, table_name, source_schema)
    logger.info(f"Final keys array for {table_name}: {keys} (count: {len(keys)})")
    return keys


def _assemble_source_columns(columns, custom_config, table_name, source_schema, grouped=False, check_forms=True):
    """The source columns and keys of a SingleTable entity, from discovery and the YAML ``columns:`` and ``keys:``.

    Shared by the first loop of :func:`create_entities` and the ``@@`` duplicate loop, so both read the YAML the
    same way. Returns ``(columns_def, keys, has_column_mappings, is_column_whitelist, discovery_type_by_source,
    relations)``: ``keys`` also feeds the target table creation, the fifth value resolves null types of legacy
    backups and ``relations`` are the YAML relations (:func:`entity_mapping.yaml_relations`, None without a mapping).

    ``grouped`` (a Gluesync 2.3 CoreHub): a source listed by several rows is one column, and a YAML source that
    discovery does not find is refused, since 2.3 refuses an id that is no column of the entity (BS-5). Otherwise a
    form only 2.3 can store is refused, unless ``check_forms`` is false. Refusals raise
    :class:`entity_mapping.EntityMappingError`.
    """
    relations = yaml_relations(custom_config.get('columns'), label=table_name)
    # Columns that differ by case only (Cosmos DB's ``id`` and a field ``ID``) are two: a name is the one spelt so
    names = ColumnNames(c["name"] for c in columns["columns"])
    if not grouped and check_forms:
        _refuse_forms_of_gluesync_23(relations, custom_config, table_name, names)

    # Detect legacy backup YAML where all column types are null.
    _discovery_type_by_source = _legacy_discovery_types(columns, custom_config, table_name)

    has_column_mappings = _is_column_mappings(custom_config.get('columns', []))
    is_column_whitelist = _is_column_whitelist(custom_config.get('columns', []))

    if has_column_mappings:
        columns_def = _source_columns_from_mappings(columns, custom_config, relations, table_name, source_schema,
                                                    grouped, names)
    elif is_column_whitelist:
        columns_def, relations = _source_columns_from_whitelist(columns, custom_config, relations, table_name,
                                                                grouped, names)
    else:
        # If there are no column mappings or whitelist, fall back to
        # using discovery columns as-is (one-to-one source→target mapping).
        # IMPORTANT: Only fall back when NO filter was specified. If a whitelist
        # or mapping was explicitly specified but columns_def is empty, do NOT
        # fall back to all columns - that would silently include unwanted columns.
        columns_def = _source_columns_from_discovery(columns, custom_config, table_name, names)

    # GSSD-1355 regression guard: target-only Field Function entries make
    # _is_column_mappings() true but give no source column. Without
    # a discovery fallback, columns_def (and therefore CoreHub tablesKeys via
    # isPK) stay empty → "Missing entity primary keys" on plain SQL tables.
    if has_column_mappings and not columns_def:
        logger.warning(
            f"Table {table_name}: column mappings produced no source columns "
            f"(likely target-only Field Function entries). Falling back to discovery."
        )
        columns_def = _source_columns_from_discovery(columns, custom_config, table_name, names)

    logger.debug(f"Columns definition for {table_name}: {columns_def}")

    keys = _assemble_source_keys(columns, custom_config, relations, columns_def, table_name, source_schema, names)

    # Keep column isPK flags aligned with the resolved keys list (CoreHub reads isPK).
    _paint_is_pk_on_columns(columns_def, keys)

    # Apply the charSet declared in the YAML on the source columns. Done here, on the
    # already built list, so it covers every shape it can be built from: explicit
    # mappings, whitelist or plain discovery. Only the source entity's columns are
    # patched: `keys` feeds the target table creation below, where a source-side
    # character set has no business.
    char_set_overrides = _build_char_set_overrides((custom_config or {}).get('columns', []), names)
    _apply_char_set_overrides(columns_def, char_set_overrides, table_name, names)

    return columns_def, keys, has_column_mappings, is_column_whitelist, _discovery_type_by_source, relations


@dataclass
class _PipelineContext:
    """What the entity assembly of a :func:`create_entities` run reads from the pipeline and its two agents."""
    token: Any
    pipeline_id: Any
    target_agent_id: Any
    yaml_target_schema: Any
    source_node_info: Any
    target_node_info: Any
    source_agent_tag: Any = None
    target_agent_tag: Any = None
    skip_errors: bool = True
    source_is_nosql: bool = False
    target_is_nosql: bool = False
    # The functions the pipeline's installed function collection exports, when the YAML binds a column to one of them:
    # a ``CustomFn`` naming another one is refused before the request. ``function_collection.NO_COLLECTION``: none is
    # installed (a warning, as CoreHub accepts the entity). None: nothing to check (see
    # :func:`_exported_custom_functions`).
    exported_functions: Any = None


@dataclass
class _TablePair:
    """A source table and the target table its SingleTable entity (or MultiTable chain pair) writes."""
    table_name: Any
    target_table_name: Any
    columns: Any  # the source discovery: {"columns": [...]}
    custom_config: Any  # the YAML entry of the table
    source_table_id: Any
    target_table_id: Any
    is_unlocked_schema: bool = False

    @cached_property
    def source_names(self):
        """The :class:`entity_mapping.ColumnNames` of the source columns of the table."""
        return ColumnNames(c["name"] for c in (self.columns or {}).get("columns") or [] if c.get("name"))


def _map_target_type(ctx, data_type, target_table_column_types):
    """The target native type of the source native type ``data_type`` (:func:`commons.map_data_type`)."""
    return map_data_type(data_type, ctx.source_node_info, ctx.target_node_info,
                         target_table_column_types=target_table_column_types)


def _is_physical_key_honoured(tag):
    """Whether the key flags of the agent tagged ``tag`` are a primary key (see _NO_PHYSICAL_KEY_AGENT_TAGS)."""
    return str(tag or "").strip().lower() not in _NO_PHYSICAL_KEY_AGENT_TAGS


class _InferredColumn(dict):
    """A column a NoSQL target reports, kept for its id and name only.

    A NoSQL target has no schema to read back: it infers a column from the documents it holds, so its type is empty
    for a field that is always null, "number" for an int and a long alike, and every field is "not nullable". A
    collection that already exists must not change the columns of an entity, so only the id and the spelling of the
    name are taken from it; the type, nullability and the rest come from the source column and the YAML, as for a
    collection that does not exist yet.
    """


def _enrich_from(discovered_target_col, source_col):
    """The column the optional fields (lengths, precision, character set) of a target column are copied from."""
    if discovered_target_col and not isinstance(discovered_target_col, _InferredColumn):
        return discovered_target_col
    return source_col


_LONG_MIN, _LONG_MAX = -2 ** 63, 2 ** 63 - 1  # the ids of CoreHub are Java Longs


def _fresh_target_column_id(held, name, table_name):
    """An id no target column holds: the one above the highest, else (a Java Long overflows) the one below the lowest."""
    fresh = max([*held, 0]) + 1
    if fresh <= _LONG_MAX:
        return fresh
    fresh = min(held) - 1
    if fresh < _LONG_MIN:
        raise EntityMappingError(f"Table {table_name}: no id is left for the target column {name}")
    return fresh


class _TargetBuild:
    """The working state of :func:`_assemble_target_side` for one table pair.

    Holds the discovered target table and the target columns built so far. ``last_discovered`` and
    ``last_column`` are the discovered target column and the source column the column loops looked at last: the
    target-only columns and the target primary keys copy their optional fields (lengths, precision, character set)
    from them, as they always have.
    """

    def __init__(self, ctx, pair, discovered, discovery_type_by_source, relations=None):
        self.ctx = ctx
        self.pair = pair
        self.discovered = discovered
        self.exact = {
            c.get('name'): c for c in reversed((discovered or {}).get('columns') or []) if c.get('name')
        }
        # The target columns a name may stand for: those discovery reports, and the names the YAML writes onto that
        # differ by case only (a Cosmos DB ``id`` next to a field ``ID``), which are two columns even where the target
        # does not hold both yet. A name no column is spelt as stands for the one column that spells it so
        # case-insensitively.
        yaml_targets = {t for r in relations or [] for t in r["targets"]}
        folded = {}
        for name in sorted(yaml_targets):
            folded.setdefault(name.lower(), []).append(name)
        self.known_target_names = [*self.exact, *(n for names in folded.values() if len(names) > 1 for n in names)]
        self.base_target_names = ColumnNames(self.known_target_names)
        if ctx.target_is_nosql:
            self.exact = {name: _InferredColumn(id=c.get('id'), name=name) for name, c in self.exact.items()}
            discovered = None  # inferred types are no types a source type should be kept as
        # Set of dataTypes already present in the target table, for the type-mapping fallback
        self.column_types = extract_target_column_types(discovered)
        self.discovery_type_by_source = discovery_type_by_source
        self.overrides = {}
        self.columns_def = []
        # Source columns that differ by case only (Cosmos DB's ``id`` next to a field ``ID``) are mapped onto target
        # columns that do, whether or not the target holds them yet.
        by_lower = {}
        for source_col in (pair.columns or {}).get("columns") or []:
            by_lower.setdefault(str(source_col.get("name", "")).lower(), set()).add(source_col.get("name", ""))
        self._case_only_source_names = [n for names in by_lower.values() if len(names) > 1 for n in sorted(names)]
        # The id of every target column by name: those discovery reports first, so that no other column takes one
        # (see target_column_id).
        self._target_ids = {name: c.get('id') for name, c in self.exact.items() if c.get('id') is not None}
        self.last_discovered = None
        self.last_column = None

    def target_col(self, name, default=None):
        """The discovered target column spelt ``name``, else the one spelt so case-insensitively (``default`` if none),
        unless the YAML names a column spelt ``name`` next to one that differs by case only."""
        found = self.exact.get(self.base_target_names.resolve(name))
        return found if found is not None else default

    def seen(self, discovered_target_col):
        """Remember ``discovered_target_col`` as the last one looked at, and return it."""
        self.last_discovered = discovered_target_col
        return discovered_target_col

    def enrichment_source(self):
        """The column the target-only columns and the target primary keys copy their optional fields from."""
        return _enrich_from(self.last_discovered, self.last_column) if self.last_discovered else self.last_column

    def map_type(self, data_type):
        return _map_target_type(self.ctx, data_type, self.column_types)

    def yaml_target_type(self, col):
        """The target type an exported mapping row (``{source, target, type, ...}``) gives the source column ``col``.

        Only for a NoSQL target, whose columns are not read back from the target (see :func:`_assemble_target_side`):
        the export writes the target dataType of each row, and a type the target advertises is restored as it was,
        where mapping the source type alone would lose a type that was changed after the entity was created. None
        for any other target, and when the row has no type or one the target does not know.
        """
        if not self.ctx.target_is_nosql:
            return None
        names = self.pair.source_names
        for row in self.pair.custom_config.get('columns') or []:
            if isinstance(row, dict) and isinstance(row.get('source'), str) and names.key(row['source']) == col.get("name"):
                return _canonicalize_target_type(row.get('type'), self.ctx.target_node_info)
        return None

    def target_names(self):
        """The :class:`entity_mapping.ColumnNames` of the target columns known so far: discovered, written onto by
        the YAML and built."""
        return ColumnNames([*self.known_target_names, *(c["name"] for c in self.columns_def if c.get("name"))])

    @property
    def max_id(self):
        """The highest id a target column holds, 0 when none does."""
        return max([i for i in self._target_ids.values() if isinstance(i, int)] + [0])

    def _target_id_owner(self, name):
        """The spelling the id of the target column ``name`` is kept under: the target column ``name`` stands for
        (see :class:`entity_mapping.ColumnNames`), else ``name`` itself."""
        names = ColumnNames([*self.known_target_names, *self._case_only_source_names,
                             *(c["name"] for c in self.columns_def if c.get("name")), *self._target_ids])
        return names.resolve(name) or str(name)

    def registered_target_id(self, name):
        """The id the target column ``name`` holds, None when it holds none yet."""
        return self._target_ids.get(self._target_id_owner(name))

    def target_column_id(self, name, preferred_id=None):
        """The id of the target column ``name``: the only place a target column (or key) takes its id from.

        A column keeps the id it first got, so a target key and its target column share one whatever order they are
        built in. A new one takes ``preferred_id`` (the id of the column discovery reports, else of the source column
        that feeds it) unless another target column holds it already: one source column fanned out onto several
        target columns would give them all its id. Else it takes a fresh id: above every id held, or below them all
        when that would overflow a Java Long.
        """
        owner = self._target_id_owner(name)
        if owner in self._target_ids:
            return self._target_ids[owner]
        held = {i for i in self._target_ids.values() if isinstance(i, int)}
        if preferred_id is None or preferred_id in held:
            preferred_id = _fresh_target_column_id(held, owner, self.pair.table_name)
        self._target_ids[owner] = preferred_id
        return preferred_id

    def has_target_column(self, name):
        """Whether the target column spelt ``name``, else the only one spelt so case-insensitively, is built."""
        built = self.target_names().resolve(name)
        return built is not None and any(c.get("name") == built for c in self.columns_def)

    def override_for(self, source_name):
        """The ``targetDataType`` the YAML declares for the source column ``source_name`` (see ``overrides``)."""
        return self.overrides.get(self.pair.source_names.key(source_name))


def _discover_target_columns(ctx, target_table_name):
    """The discovered columns of the target table (needed for columnsMappingMatrix), None when it has none."""
    target_discovered_columns = None
    try:
        target_discovered_columns = get_table_columns(ctx.token, ctx.pipeline_id, ctx.target_agent_id,
                                                      ctx.yaml_target_schema, target_table_name)
        if target_discovered_columns and isinstance(target_discovered_columns.get('columns'), list):
            target_discovered_columns_by_name = {
                c.get('name').lower(): c
                for c in target_discovered_columns['columns']
                if c.get('name')
            }
            logger.debug(
                f"Found {len(target_discovered_columns_by_name)} existing columns in target table {ctx.yaml_target_schema}.{target_table_name}"
            )
    except Exception as e:
        logger.debug(f"Could not get target columns for {ctx.yaml_target_schema}.{target_table_name}: {str(e)}")
    return target_discovered_columns


def _set_unlocked_schema_arrays(target_entity_type, pair):
    """Write the unlocked schema arrays of the target entityType (the target table id when unlocked)."""
    if pair.is_unlocked_schema:
        # Add tablesWithUnlockedSchema array with target table ID
        target_entity_type["tablesWithUnlockedSchema"] = [pair.target_table_id]
        target_entity_type["tablesWithUnlockedDataTypes"] = []
        logger.info(f"Table {pair.table_name} configured with unlocked schema. Target table ID: {pair.target_table_id}")
    else:
        # Add empty arrays for locked schema
        target_entity_type["tablesWithUnlockedSchema"] = []
        target_entity_type["tablesWithUnlockedDataTypes"] = []


def _preferred_target_id(discovered_target_col, source_col_id):
    """The id a target column asks for: the discovered target column's, else the id of the source column."""
    if discovered_target_col and discovered_target_col.get('id') is not None:
        return discovered_target_col.get('id')
    return source_col_id


def _discovered_or_mapped(tb, col, discovered_target_col, source_col_id, target_name):
    """``(type, id)`` of the target column ``target_name``: the discovered target column's, else the mapped type, and
    the id :meth:`_TargetBuild.target_column_id` gives it (the source id when no other target column holds it).

    A discovered column without a type (a NoSQL target infers the type of a field from its values: a field that
    is always null has none) is no type at all: the mapped one stands in, so the payload never carries a null.
    """
    resolved_target_type = None
    if discovered_target_col and discovered_target_col.get('dataType'):
        resolved_target_type = discovered_target_col.get('dataType')
    target_col_id = tb.target_column_id(target_name, _preferred_target_id(discovered_target_col, source_col_id))
    if not resolved_target_type:
        resolved_target_type = tb.yaml_target_type(col) or tb.map_type(col.get("dataType"))
    return resolved_target_type, target_col_id


def _is_position(value, least=1):
    """Whether ``value`` is a position of a column in its table: an int of at least ``least`` (0 is what a target that
    keeps no order reports, and what an entity the bootstrapper built without positions carries)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def _yaml_mapping_row(tb, source_name, target_name):
    """The exported mapping row ``{source, target, ...}`` of the YAML that writes ``source_name`` onto ``target_name``.

    Empty for a column the YAML states in any other form (shorthand ``{SRC: tgt}`` or whitelist rows), whose keys
    are column names or describe the source side. A row naming several sources (N:1) is found by any of them.
    """
    source_key, target_key = tb.pair.source_names.key, tb.target_names().key
    wanted_source, wanted_target = source_key(source_name), target_key(target_name)
    for row in tb.pair.custom_config.get('columns') or []:
        sides = _mapping_row_sides(row)
        if sides and wanted_source in {source_key(s) for s in sides[0]} \
                and wanted_target in {target_key(t) for t in sides[1]}:
            return row
    return {}


def _mapping_row_sides(row):
    """``(sources, targets)`` an exported mapping row names (a row without ``target`` writes its source's name),
    None for anything else than such a row."""
    if not isinstance(row, dict) or 'source' not in row:
        return None
    sources = row['source'] if isinstance(row['source'], list) else [row['source']]
    targets = row.get('target')
    if not isinstance(targets, list):
        targets = [targets if targets is not None else sources[0]]
    return sources, targets


def _target_position(discovered_target_col, col, yaml_row=None):
    """The position of a target column in its table.

    In order: the ``targetPosition`` the export wrote on the YAML row (0 included: the entity had none), the
    position of the discovered target column (a target that keeps no order reports 0 for every column, a NoSQL one
    none) and the position of the source column ``col``, which is what a column built from a whitelist row has
    always had.
    """
    exported = (yaml_row or {}).get("targetPosition")
    if _is_position(exported, least=0):
        return exported
    discovered = (discovered_target_col or {}).get("position")
    return discovered if _is_position(discovered) else col.get("position", 0)


def _target_is_nullable(discovered_target_col, col, yaml_row=None):
    """Whether a target column is nullable.

    In order: the ``targetIsNullable`` the export wrote on the YAML row (the entity had it: a target that does not
    keep nullability reports every column not nullable), the nullability of the discovered target column and the one
    of the source column ``col``, which is what a column built from a whitelist row has always had.
    """
    exported = (yaml_row or {}).get("targetIsNullable")
    if isinstance(exported, bool):
        return exported
    if discovered_target_col and "isNullable" in discovered_target_col:
        return discovered_target_col.get("isNullable")
    return col.get("isNullable", False)


def _exported_source_positions(custom_config):
    """``{source name: ordinalPosition}`` of the exported mapping rows that state one."""
    positions = {}
    for row in (custom_config or {}).get('columns') or []:
        if isinstance(row, dict) and isinstance(row.get('source'), str) and _is_position(row.get('ordinalPosition')):
            positions.setdefault(row['source'], row['ordinalPosition'])
    return positions


def _exported_position_by_column(exported, discovered):
    """``({discovered name: exported position}, exported names matched)``.

    A name is matched as spelt, else case-insensitively, but only when it names one discovered column: a schemaless
    source may hold names that differ by case only (``id`` and ``ID``), which are two columns each with its own row, and
    a row never gives its position to two columns.
    """
    names = ColumnNames(c.get('name') for c in discovered if c.get('name'))
    position_by_column, matched = {}, set()
    for exact in (True, False):
        for name, position in exported.items():
            column = names.resolve(name)
            if column is not None and (column == name) == exact and column not in position_by_column:
                position_by_column[column] = position
                matched.add(name)
    return position_by_column, matched


def _with_exported_source_positions(columns, custom_config, table_name):
    """The source discovery ``columns`` of a schemaless source, with the order the exported rows give its columns.

    A source with no schema (MongoDB, Cosmos DB, Couchbase, DynamoDB) infers its columns by sampling documents,
    so the position discovery reports depends on the data at restore time, not on the configuration. The
    ``ordinalPosition`` of the rows is the place of each column in the table the export came from: the listed
    columns take, in that order, the positions discovery gave them (which keeps the numbering the agent uses, 0-based
    for MongoDB). A discovered column no row lists keeps its discovered position; a row naming a column discovery
    does not return gets none (the mapping reports it).
    """
    exported = _exported_source_positions(custom_config)
    discovered = (columns or {}).get('columns') or []
    if not exported or not discovered:
        return columns
    exported_position, matched = _exported_position_by_column(exported, discovered)
    listed = [c for c in discovered if exported_position.get(c.get('name')) is not None]
    if any(not _is_position(c.get('position'), least=0) for c in listed):
        logger.warning(f"Table {table_name}: a discovered column has no position, keeping the discovered order")
        return columns
    missing = sorted(name for name in exported if name not in matched)
    if missing:
        logger.warning(f"Table {table_name}: exported column(s) {missing} not found in discovery, they get no position")
    unlisted = [c.get('name') for c in discovered if exported_position.get(c.get('name')) is None]
    if unlisted:
        logger.warning(f"Table {table_name}: discovered column(s) {unlisted} not listed in the YAML, "
                       f"keeping their discovered position")
    slots = sorted(c['position'] for c in listed)
    in_exported_order = sorted(listed, key=lambda c: (exported_position[c.get('name')], c['position']))
    position_of = {id(col): slot for col, slot in zip(in_exported_order, slots)}
    return {**columns, 'columns': [{**c, 'position': position_of[id(c)]} if id(c) in position_of else c
                                   for c in discovered]}


def _target_is_pk(discovered_target_col, col, custom_config, coerce_target=False, names=None):
    """isPrimaryKey of a target column: from the discovered target column, else from the source column.

    ``names``: the :class:`entity_mapping.ColumnNames` of the source columns."""
    target_pk = discovered_target_col.get("isPK") if discovered_target_col else False
    if coerce_target:
        target_pk = _coerce_is_pk(target_pk)
    return target_pk or _coerce_is_pk(col.get("isPK")) or _is_in_keys(col["name"], custom_config, names)


def _target_column_def(col, discovered_target_col, target_col_id, name, data_type, is_pk, position=0,
                       is_nullable=None):
    """A target column of a SingleTable entity, fed by the source column ``col``.

    ``is_nullable``: the nullability to give it, when the caller knows it (see :func:`_target_is_nullable`); else the
    discovered target column's, else the source column's.
    """
    if is_nullable is None:
        is_nullable = _target_is_nullable(discovered_target_col, col)
    return _enrich_column({
        "id": target_col_id,  # Use target column ID
        "position": position,
        "name": name,
        "alias": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isIdentity": (discovered_target_col.get("isIdentity") if discovered_target_col else None) or col.get("isIdentity", False),
        "isNullable": is_nullable,
    }, _enrich_from(discovered_target_col, col))


def _legacy_fallback_target_type(tb, col, resolved_target_type):
    """Fallback for legacy null-type backups: the target type of the live discovery type of the source column."""
    _fallback_src_type = tb.discovery_type_by_source.get(col.get("name", ""))
    if not _fallback_src_type:
        return resolved_target_type
    resolved_target_type = tb.map_type(_fallback_src_type)
    logger.debug(
        f"Table {tb.pair.table_name}, column {col.get('name')}: resolved target type "
        f"via discovery fallback ({_fallback_src_type} → {resolved_target_type})"
    )
    return resolved_target_type


def _append_mapped_target_column(tb, col, source_col_id, discovered_target_col, target_name):
    """Append the target column of a YAML mapping pair whose source is the discovered column ``col``."""
    resolved_target_type, target_col_id = _discovered_or_mapped(tb, col, discovered_target_col, source_col_id,
                                                                target_name)
    yaml_row = _yaml_mapping_row(tb, col["name"], target_name)
    # The type an exported row gives the target column, when the target advertises it, is the one the entity had:
    # a target reports its own spelling of a type (Cassandra reads varchar back as text).
    exported_type = _canonicalize_target_type(yaml_row.get('type'), tb.ctx.target_node_info)
    # User-declared targetDataType override takes highest priority
    resolved_target_type = tb.override_for(col["name"]) or exported_type or resolved_target_type
    # Fallback for legacy null-type backups: if we still have no
    # resolved type, use the live discovery type for this source column
    if not resolved_target_type and tb.discovery_type_by_source:
        resolved_target_type = _legacy_fallback_target_type(tb, col, resolved_target_type)
    # A NoSQL target reports every column not nullable whatever it holds, so its columns take the nullability of the
    # source column, as they always have: the exported targetIsNullable is for a SQL target.
    nullable_row = {} if tb.ctx.target_is_nosql else yaml_row
    # Get isPrimaryKey from discovered target column or fall back to source column
    target_is_primary_key = _target_is_pk(discovered_target_col, col, tb.pair.custom_config,
                                          names=tb.pair.source_names)
    tb.columns_def.append(_target_column_def(col, discovered_target_col, target_col_id, target_name,
                                             resolved_target_type, target_is_primary_key,
                                             position=_target_position(discovered_target_col, col, yaml_row),
                                             is_nullable=_target_is_nullable(discovered_target_col, col, nullable_row)))


def _discovered_spelling(discovered_target_col, target_name):
    """``target_name`` as the discovered target column spells it, when discovery found one."""
    if discovered_target_col and discovered_target_col.get('name'):
        return discovered_target_col['name']
    return target_name


def _map_onto_target_column(tb, col, source_col_id, column_map, grouped):
    """Add the target column the YAML relation ``column_map`` writes from the discovered column ``col``, if any."""
    for source_name, target_name in _relation_pairs([column_map]):
        if not column_map["targets"]:
            return  # read, never written (1:null): no target column
        if tb.pair.source_names.key(source_name) != col["name"]:
            continue
        discovered_target_col = tb.seen(tb.target_col(target_name))
        if grouped:
            # The key rules compare names case-sensitively: take the spelling discovery reports (BOP-15).
            target_name = _discovered_spelling(discovered_target_col, target_name)
            if tb.has_target_column(target_name):
                return
        _append_mapped_target_column(tb, col, source_col_id, discovered_target_col, target_name)
        return


def _target_columns_from_mappings(tb, relations, grouped):
    """The target columns of custom column mappings."""
    for col in tb.pair.columns["columns"]:
        tb.last_column = col
        # Use the id field from CoreHub API as the source column ID
        source_col_id = _require_column_id(col, tb.pair.table_name)
        for column_map in relations or []:
            _map_onto_target_column(tb, col, source_col_id, column_map, grouped)


def _whitelist_target_type(tb, col, yaml_col, discovered_target_col):
    """The target dataType of a whitelist row.

    Resolution order:
    1. `targetDataType:` override from YAML (highest priority, verbatim).
    2. map_data_type(<discovered source dataType>) when its result
       is a type the target actually advertises (defaultType or
       supportedType in the target dataTypesMatrix).
    3. YAML `type:` as a target-side hint — used only when the auto
       mapping fell back to a value the target does not advertise.
    4. Discovered target column's dataType (last-resort, if available).
    5. The mapped value as-is (preserves observable behavior).
    """
    _source_dt = col.get("dataType")
    _override = yaml_col.get('targetDataType') or tb.override_for(col["name"])
    if _override:
        return _override
    target_node_info = tb.ctx.target_node_info
    _mapped = tb.map_type(_source_dt)
    _canonical = _canonicalize_target_type(_mapped, target_node_info) if _mapped else None
    if _canonical:
        # Prefer YAML `type:` when it is also a known target type
        # (gives the user authoritative control over target casing/variant).
        _yaml_type = yaml_col.get('type')
        _yaml_canonical = _canonicalize_target_type(_yaml_type, target_node_info) if _yaml_type else None
        return _yaml_canonical or _canonical
    if yaml_col.get('type'):
        return yaml_col.get('type')
    if discovered_target_col and discovered_target_col.get('dataType'):
        return discovered_target_col.get('dataType')
    return _mapped or _source_dt


def _append_whitelist_target_column(tb, col, yaml_col, grouped):
    """Append the target column of a whitelist row whose source is the discovered column ``col``."""
    source_col_id = _require_column_id(col, tb.pair.table_name, detail=False)
    target_col_name = yaml_col.get('name')
    # Look up target column info
    discovered_target_col = tb.seen(tb.target_col(target_col_name))
    if grouped:
        target_col_name = _discovered_spelling(discovered_target_col, target_col_name)  # BOP-15, as for mapping rows
    target_col_id = tb.target_column_id(target_col_name, _preferred_target_id(discovered_target_col, source_col_id))
    resolved_target_type = _whitelist_target_type(tb, col, yaml_col, discovered_target_col)
    # Target columns are serialised with `dataType` (not `type`) in the
    # UpsertEntitiesDto contract. Align with the mapping/default branches.
    target_is_primary_key = _target_is_pk(discovered_target_col, col, tb.pair.custom_config,
                                          names=tb.pair.source_names)
    tb.columns_def.append(_target_column_def(col, discovered_target_col, target_col_id, target_col_name,
                                             resolved_target_type, target_is_primary_key,
                                             position=col.get("position", 0)))


def _target_columns_from_whitelist(tb, grouped):
    """The target columns of a column whitelist: only the specified columns."""
    yaml_columns = tb.pair.custom_config.get('columns', [])
    source_columns = tb.pair.columns["columns"]
    for yaml_col in yaml_columns:
        source_col_name = yaml_col.get('sourceName') or yaml_col.get('name')
        # Find the matching column from discovered source columns
        col = find_by_name(source_columns, source_col_name)
        if col is None:
            if source_columns:
                tb.last_column = source_columns[-1]
            logger.warning(f"Column '{source_col_name}' from YAML whitelist not found in discovered columns for table {tb.pair.table_name}")
            continue
        tb.last_column = col
        _append_whitelist_target_column(tb, col, yaml_col, grouped)

    logger.info(f"Using column whitelist for target {tb.pair.table_name}: {len(tb.columns_def)} of {len(yaml_columns)} specified columns matched")


def _target_columns_from_discovery(tb, apply_override, coerce_target_pk):
    """Every discovered source column onto the target column of its name, with mapped types.

    Uses the target-discovered column name (preserves original case from the target DB), else the source name.
    """
    for col in tb.pair.columns["columns"]:
        tb.last_column = col
        # Use the id field from CoreHub API as the source column ID
        source_col_id = _require_column_id(col, tb.pair.table_name)
        discovered_target_col = tb.seen(tb.target_col(col["name"]))
        target_col_name = discovered_target_col.get('name') if discovered_target_col else col["name"]
        resolved_target_type, target_col_id = _discovered_or_mapped(tb, col, discovered_target_col, source_col_id,
                                                                    target_col_name)
        if apply_override:
            # User-declared targetDataType override takes highest priority
            resolved_target_type = tb.override_for(col["name"]) or resolved_target_type
        target_is_primary_key = _target_is_pk(discovered_target_col, col, tb.pair.custom_config,
                                              coerce_target=coerce_target_pk, names=tb.pair.source_names)
        tb.columns_def.append(_target_column_def(col, discovered_target_col, target_col_id, target_col_name,
                                                 resolved_target_type, target_is_primary_key,
                                                 position=_target_position(discovered_target_col, col)))


def _build_target_columns(tb, relations, grouped):
    """The target columns fed by a source column, from the YAML ``columns:`` shape."""
    yaml_columns = tb.pair.custom_config.get('columns', [])
    has_column_mappings_target = _is_column_mappings(yaml_columns)
    if has_column_mappings_target:
        _target_columns_from_mappings(tb, relations, grouped)
    elif _is_column_whitelist(yaml_columns):
        _target_columns_from_whitelist(tb, grouped)
    else:
        # No column mappings - use columns as-is with mapped types
        _target_columns_from_discovery(tb, apply_override=True, coerce_target_pk=False)

    if has_column_mappings_target and not tb.columns_def:
        logger.warning(
            f"Table {tb.pair.table_name}: target column mappings produced no columns "
            f"(likely target-only Field Function entries). Falling back to discovery."
        )
        _target_columns_from_discovery(tb, apply_override=False, coerce_target_pk=True)


def _produced_column(row, discovered_target_col, target_name, target_col_id):
    """The target column ``target_col_id`` of a ``{target, expression}`` row (null:1), fed by no source column."""
    if not discovered_target_col:
        return {
            "id": target_col_id,
            "position": 0,
            "name": target_name,
            "alias": target_name,
            "dataType": row.get("targetDataType") or row.get("type"),
            "isPK": False,
            "isIdentity": False,
            "isNullable": row.get("isNullable", True),
        }
    produced_col = {
        "id": target_col_id,
        "position": 0,
        "name": discovered_target_col.get("name"),
        "alias": discovered_target_col.get("name"),
        "dataType": discovered_target_col.get("dataType") or row.get("targetDataType") or row.get("type"),
        "isPK": False,
        "isIdentity": bool(discovered_target_col.get("isIdentity")),
        "isNullable": discovered_target_col.get("isNullable", True),
    }
    return _enrich_column(produced_col, discovered_target_col)


def _add_produced_columns(tb, relations, grouped):
    """Columns fed by no source ({target, expression} rows, null:1): produced by their field function or the UDF."""
    produced_rows = [r for r in relations or [] if not r["sources"]]
    if produced_rows and not grouped:
        logger.warning(
            f"Table {tb.pair.table_name}: target column(s) {', '.join(r['targets'][0] for r in produced_rows)} fed by no source "
            f"column are created only on a Gluesync 2.3 CoreHub; their field functions are not restored here"
        )
        return
    if not produced_rows or tb.pair.is_unlocked_schema:
        return
    for relation in produced_rows:
        target_name = relation["targets"][0]
        if tb.has_target_column(target_name):
            continue
        row = relation["row"] or {}
        discovered_target_col = tb.seen(tb.target_col(target_name))
        target_col_id = tb.target_column_id(_discovered_spelling(discovered_target_col, target_name),
                                            discovered_target_col.get("id") if discovered_target_col else None)
        produced_col = _produced_column(row, discovered_target_col, target_name, target_col_id)
        tb.columns_def.append(produced_col)
        logger.info(f"Table {tb.pair.table_name}: target column {produced_col['name']} is fed by no source column (null:1)")


def _target_only_defaults(target_col):
    """``(name, type, dataLength, numPrec, numScale, isNullable)`` a target-only column declares (string or object)."""
    if isinstance(target_col, str):
        # Simple string format: just the column name, with default properties
        return target_col, 'varchar', 1024, 0, 0, False
    # Object format with user-defined properties
    return (
        target_col.get('name'),
        target_col.get('dataType') or target_col.get('type') or 'varchar',  # null-safe: treat explicit null as missing
        target_col.get('dataLength', 1024),  # Default data length
        target_col.get('numPrec', 0),  # Default numeric precision
        target_col.get('numScale', 0),  # Default numeric scale
        target_col.get('isNullable', False),  # Default nullable
    )


def _target_only_position(target_col):
    """The position a target-only column declares (the export writes it), 0 when it declares none."""
    exported = target_col.get('position') if isinstance(target_col, dict) else None
    return exported if _is_position(exported, least=0) else 0


def _check_target_only_properties(target_col, col_name, skip_errors, where):
    """Refuse a user-defined target-only column the target table lacks unless it declares every property."""
    if not isinstance(target_col, dict):
        return
    required_props = ['name', 'type', 'dataLength', 'numericPrecision', 'numericScale', 'isNullable']
    missing_props = [prop for prop in required_props if prop not in target_col or target_col.get(prop) is None]
    if missing_props:
        error_msg = f"Target-only column '{col_name}'{where} is missing required properties: {missing_props}. All properties (name, type, dataLength, numericPrecision, numericScale, isNullable) must be specified when the column doesn't exist in the target table."
        logger.error(error_msg)
        if not skip_errors:
            raise ValueError(error_msg)


def _target_only_properties(target_col, target_columns, skip_errors, where=""):
    """``(name, type, dataLength, numPrec, numScale, isNullable)`` of a target-only column.

    The properties of the column of that name in the discovered target table win over the user-defined ones;
    a column the target table lacks must declare every property. ``where`` names the table in the error.
    """
    col_name, col_type, col_data_length, col_numeric_precision, col_numeric_scale, col_is_nullable = \
        _target_only_defaults(target_col)

    # Check if this column already exists in the target table
    discovered_column = None
    if target_columns and 'columns' in target_columns:
        discovered_column = next(
            (col for col in target_columns['columns'] if col.get('name') == col_name),
            None
        )

    if not discovered_column:
        # Column doesn't exist in target table, use user-defined properties
        logger.debug(f"Using user-defined properties for target-only column '{col_name}' (not found in target table)")
        # Validate that required properties are provided for user-defined target-only columns
        _check_target_only_properties(target_col, col_name, skip_errors, where)
        return col_name, col_type, col_data_length, col_numeric_precision, col_numeric_scale, col_is_nullable

    # Use discovered column properties (priority over user-defined)
    logger.info(f"Using discovered properties for target-only column '{col_name}' from target table")
    return (
        col_name,
        discovered_column.get('dataType', discovered_column.get('type', col_type)),
        discovered_column.get('charMaxLength', col_data_length),
        discovered_column.get('numPrec', col_numeric_precision),
        discovered_column.get('numScale', col_numeric_scale),
        discovered_column.get('isNullable', col_is_nullable),
    )


def _add_target_only_columns(tb):
    """Add the target-only columns of the YAML (only supported with unlocked schema)."""
    pair, ctx = tb.pair, tb.ctx
    target_only_columns = pair.custom_config.get(_TARGET_ONLY_COLUMNS_KEY, [])
    if not target_only_columns:
        return
    if not pair.is_unlocked_schema:
        logger.warning(f"Target-only columns are specified for table {pair.table_name} but unlockedSchema is not true. Skipping target-only columns.")
        logger.warning(f"To use target-only columns, set 'unlockedSchema: true' for table {pair.table_name}")
        return
    logger.info(f"Adding {len(target_only_columns)} target-only columns for table {pair.table_name}")

    # Try to get target columns if the target table exists
    target_columns = None
    try:
        target_columns = get_table_columns(ctx.token, ctx.pipeline_id, ctx.target_agent_id, ctx.yaml_target_schema,
                                           pair.target_table_name)
        logger.debug(f"Found {len(target_columns.get('columns', []))} existing columns in target table {ctx.yaml_target_schema}.{pair.target_table_name}")
    except Exception as e:
        logger.debug(f"Could not get target columns for {ctx.yaml_target_schema}.{pair.target_table_name}: {str(e)}")

    for target_col in target_only_columns:
        col_name, col_type, col_data_length, col_numeric_precision, col_numeric_scale, col_is_nullable = \
            _target_only_properties(target_col, target_columns, ctx.skip_errors)
        # A column the target table holds keeps its id, a new one takes the next free one
        col_id = tb.target_column_id(col_name)

        # Map the column type to target node type
        mapped_type = tb.map_type(col_type)

        tb.columns_def.append(_enrich_column({
            "id": col_id,
            "position": _target_only_position(target_col),
            "name": col_name,
            "alias": col_name,
            "dataType": mapped_type,
            "charMaxLength": col_data_length,
            "numPrec": col_numeric_precision,
            "numScale": col_numeric_scale,
            "isNullable": col_is_nullable, "isIdentity": False,
            "isPK": False  # Target-only columns are not primary keys
        }, tb.enrichment_source()))
        logger.debug(f"Added target-only column: {col_name} (type: {mapped_type}, id: {col_id})")


def _add_name_relation_column(tb, relation, columns_def):
    """A source column mapped by name: its target column, as the branch without ``columns:`` builds it."""
    if relation.get("explicit", True) or not relation["targets"]:
        return
    target_name = relation["targets"][0]
    if tb.has_target_column(target_name):
        return
    col = next(c for c in columns_def if c["name"] == relation["sources"][0])
    discovered_target_col = tb.seen(tb.target_col(target_name))
    resolved_target_type = discovered_target_col.get('dataType') if discovered_target_col else None
    if not resolved_target_type:
        resolved_target_type = tb.yaml_target_type(col) or tb.map_type(col.get("dataType"))
    resolved_target_type = tb.override_for(col["name"]) or resolved_target_type
    relation["targets"] = [discovered_target_col.get('name') if discovered_target_col else target_name]
    target_col_id = tb.target_column_id(relation["targets"][0], _preferred_target_id(discovered_target_col, col.get('id')))
    tb.columns_def.append(_target_column_def(col, discovered_target_col, target_col_id, relation["targets"][0],
                                             resolved_target_type, False))


def _physical_keys(tb):
    """``(physical_source, physical_target)``: the primary key column names discovery reports on each side."""
    ctx = tb.ctx
    physical_source = [c["name"] for c in tb.pair.columns["columns"] if _coerce_is_pk(c.get("isPK"))] \
        if not ctx.source_is_nosql and _is_physical_key_honoured(ctx.source_agent_tag) else []
    physical_target = [c["name"] for c in (tb.discovered or {}).get("columns") or []
                       if c.get("name") and _coerce_is_pk(c.get("isPK"))] \
        if _is_physical_key_honoured(ctx.target_agent_tag) else []
    return physical_source, physical_target


def _add_udf_target_keys(tb, physical_target):
    """The UDF writes every target column, a target primary key no source feeds included."""
    for key_name in physical_target:
        if not tb.has_target_column(key_name):
            key_col = tb.target_col(key_name)
            tb.columns_def.append(_enrich_column({
                "id": tb.target_column_id(key_col["name"], key_col.get("id")), "position": 0, "name": key_col["name"],
                "alias": key_col["name"],
                "dataType": key_col.get("dataType"), "isPK": True, "isIdentity": key_col.get("isIdentity", False),
                "isNullable": key_col.get("isNullable", False),
            }, key_col))


def _add_unnamed_source_keys(columns, columns_def, source_key_names):
    """A source key no relation names is read and never written (1:null), as the field editor keeps it."""
    for key_name in source_key_names:
        if not any(str(c.get("name", "")) == key_name for c in columns_def):
            key_col = next(c for c in columns["columns"] if c.get("name") == key_name)
            columns_def.append(_source_column_def(key_col, key_col.get("id"), key_col["name"], key_col["name"], True))


def _apply_key_rules(tb, columns_def, name_relations, target_entity_type):
    """The keys of a Gluesync 2.3 entity on a SQL target with a locked schema (the R12 key rules).

    Flags isPK on the source and target columns and returns ``(target_keys, name_relations)``, the relations
    without the key links the rules forbid.
    """
    pair = tb.pair
    physical_source, physical_target = _physical_keys(tb)
    name_relations, dropped_targets, link_warnings = drop_forbidden_key_links(
        name_relations, physical_source=physical_source, physical_target=physical_target, label=pair.table_name,
    )
    for warning in link_warnings:
        logger.warning(warning)
    if dropped_targets:
        dropped = {t.lower() for t in dropped_targets}
        tb.columns_def[:] = [c for c in tb.columns_def if str(c.get("name", "")).lower() not in dropped]

    has_udf = bool(target_entity_type.get("mappingFunctionInfo"))
    if has_udf:
        _add_udf_target_keys(tb, physical_target)
    produced = [c["name"] for c in tb.columns_def] if has_udf else produced_target_names(pair.custom_config.get("columns"))

    source_key_names, target_key_names = derive_keys(
        relations=name_relations,
        source_names=[c["name"] for c in columns_def] + [c["name"] for c in pair.columns["columns"]],
        target_names=[c["name"] for c in tb.columns_def],
        physical_source=physical_source,
        physical_target=physical_target,
        yaml_keys=sorted(_yaml_key_names(pair.custom_config)) if pair.custom_config.get("keys") else [],
        yaml_target_keys=[str(k) for k in pair.custom_config.get("targetKeys") or []],
        produced_targets=produced,
        label=pair.table_name,
        source_is_nosql=tb.ctx.source_is_nosql,
    )
    _add_unnamed_source_keys(pair.columns, columns_def, source_key_names)
    for col in columns_def:
        col["isPK"] = col.get("name") in source_key_names
    for col in tb.columns_def:
        col["isPK"] = col.get("name") in target_key_names
    return [col for col in tb.columns_def if col["isPK"]], name_relations


def _target_key_def(target_col_id, name, data_type, enrich_from, position=0):
    """A target key of a SingleTable entity."""
    return _enrich_column({
        "id": target_col_id,  # Use target column ID
        "position": position,
        "name": name,
        "alias": name,
        "dataType": data_type
    }, enrich_from)


def _append_mapped_target_keys(tb, target_keys, col, source_col_id, relations):
    """Append the target keys the YAML mapping pairs make of the discovered source column ``col``."""
    names = tb.pair.source_names
    for column_map in relations or []:
        for source_name, target_name in _relation_pairs([column_map]):
            if names.key(source_name) == col["name"] and _is_in_keys(col["name"], tb.pair.custom_config, names):
                target_keys.append(_mapped_target_key(tb, col, source_col_id, source_name, target_name))
                break


def _mapped_target_key(tb, col, source_col_id, source_name, target_name):
    """The target key a YAML mapping pair makes of the discovered source key column ``col``."""
    discovered_target_col = tb.target_col(target_name)
    resolved_target_type, target_col_id = _discovered_or_mapped(tb, col, discovered_target_col, source_col_id,
                                                                target_name)
    # User-declared targetDataType override takes highest priority
    resolved_target_type = tb.override_for(col["name"]) or resolved_target_type
    target_key = _target_key_def(target_col_id, target_name, resolved_target_type,
                                 _enrich_from(discovered_target_col, col))
    logger.debug(f"Target key: {source_name} -> {target_name} (source ID={source_col_id}, target ID={target_col_id})")
    return target_key


def _missed_target_key_source(tb, key_name):
    """The discovered source column of a YAML key the mapping pairs missed, refused when there is none."""
    pair = tb.pair
    matched = find_by_name(pair.columns["columns"], key_name)
    if not matched:
        error_msg = (
            f"CRITICAL ERROR: Target key '{key_name}' not found in discovery for table "
            f"'{pair.table_name}' in schema '{tb.ctx.yaml_target_schema}'."
        )
        logger.error(error_msg)
        raise ValueError(error_msg)
    return matched, _require_column_id(matched, pair.table_name, label=key_name, detail=False)


def _resolve_missed_target_key(tb, key_name, target_keys, found_target_key_names, columns_def):
    """Resolve a YAML key missed by the mapping pairs (same defense as the source path) onto its target column."""
    if isinstance(key_name, dict):
        key_name = key_name.get("name") or (next(iter(key_name)) if key_name else None)
    if key_name is None or tb.target_names().key(key_name) in found_target_key_names:
        return
    matched, source_col_id = _missed_target_key_source(tb, key_name)
    # Prefer mapped target alias if present on source columns_def; else source name.
    src_def = find_by_name(columns_def, matched["name"])
    target_name = (src_def.get("alias") if src_def and src_def.get("alias") else matched["name"])
    discovered_target_col = tb.target_col(str(target_name))
    resolved_target_type, target_col_id = _discovered_or_mapped(tb, matched, discovered_target_col, source_col_id,
                                                                target_name)
    resolved_target_type = tb.override_for(matched["name"]) or resolved_target_type
    enrich_from = _enrich_from(discovered_target_col, matched)
    target_keys.append(_target_key_def(target_col_id, target_name, resolved_target_type, enrich_from))
    found_target_key_names.add(tb.target_names().key(target_name))
    if not tb.has_target_column(target_name):
        tb.columns_def.append(_enrich_column({
            "id": target_col_id,
            "position": 0,
            "name": target_name,
            "alias": target_name,
            "dataType": resolved_target_type,
            "isPK": True,
            "isIdentity": matched.get("isIdentity", False),
            "isNullable": matched.get("isNullable", False),
        }, enrich_from))
    logger.info(
        f"Target key {key_name!r} -> {target_name!r} resolved from discovery after mapping miss"
    )


def _target_keys_from_mappings(tb, columns_def, relations):
    """The target keys of a table with column mappings: source key names mapped to target key names."""
    target_keys = []
    for col in tb.pair.columns["columns"]:
        # Use the id field from CoreHub API as the column ID
        source_col_id = _require_column_id(col, tb.pair.table_name)
        _append_mapped_target_keys(tb, target_keys, col, source_col_id, relations)
    # Resolve YAML keys: missed by mapping pairs (same defense as source path).
    target_names = tb.target_names()
    found_target_key_names = {
        target_names.key(k.get("name"))
        for k in target_keys
        if isinstance(k, dict) and k.get("name") is not None
    }
    for key_name in tb.pair.custom_config["keys"]:
        _resolve_missed_target_key(tb, key_name, target_keys, found_target_key_names, columns_def)
    return target_keys


def _append_named_target_key(tb, target_keys, col, key_name):
    """Append the target key of the discovered source column ``col`` a YAML key names."""
    # Use the id field from CoreHub API as the column ID
    source_col_id = _require_column_id(col, tb.pair.table_name, label=key_name)
    discovered_target_col = tb.target_col(key_name)
    target_key_name = discovered_target_col.get('name') if discovered_target_col else col["name"]
    resolved_target_type, target_col_id = _discovered_or_mapped(tb, col, discovered_target_col, source_col_id,
                                                                target_key_name)
    # User-declared targetDataType override takes highest priority
    resolved_target_type = tb.override_for(col["name"]) or resolved_target_type
    target_keys.append(_enrich_column({
        "id": target_col_id,  # Use target column ID
        "name": target_key_name,
        "alias": target_key_name,
        "dataType": resolved_target_type
    }, _enrich_from(discovered_target_col, col)))
    logger.debug(f"Target key: {key_name} (source ID={source_col_id}, target ID={target_col_id})")


def _target_key_from_columns(tb, target_keys, key_name):
    """Whether a YAML key discovery lacks is a target column already built (appended to ``target_keys`` if so)."""
    logger.warning(f"Warning: Target key '{key_name}' not found in columns for table '{tb.pair.table_name}'. Searching in target_columns_def...")
    col_def = find_by_name(tb.columns_def, key_name)
    if col_def is None:
        return False
    target_key = copy.deepcopy(col_def)
    target_key["id"] = tb.target_column_id(col_def["name"], col_def.get("id"))
    target_keys.append(target_key)
    logger.info(f"Found target key '{key_name}' in target_columns_def")
    return True


def _target_keys_from_names(tb):
    """The target keys of a table without column mappings: the YAML keys directly from the source columns.

    A key missing from both the source columns and the target columns is refused, naming it.
    """
    target_keys = []
    for key_name in tb.pair.custom_config['keys']:
        # Find the column and its ID (spelt as the key is, else case-insensitively)
        col = find_by_name(tb.pair.columns["columns"], key_name)
        found = col is not None
        if found:
            _append_named_target_key(tb, target_keys, col, key_name)
        else:
            found = _target_key_from_columns(tb, target_keys, key_name)
        if not found:
            error_msg = f"CRITICAL ERROR: Target key '{key_name}' not found in discovery or target_columns_def for table '{tb.pair.table_name}' in schema '{tb.ctx.yaml_target_schema}'. This key is required by the YAML configuration but does not exist in the target database discovery."
            logger.error(error_msg)
            raise ValueError(error_msg)
    return target_keys


def _target_keys_from_pk(tb):
    """The target keys of the source columns discovery flags as primary key."""
    target_keys = []
    for col in tb.pair.columns["columns"]:
        if _coerce_is_pk(col.get("isPK")):
            # Use the id field from CoreHub API as the column ID
            col_id = _require_column_id(col, tb.pair.table_name)
            _resolved_dt = tb.target_col(col["name"], {}).get('type') or tb.map_type(col.get("dataType"))
            # User-declared targetDataType override takes highest priority
            _resolved_dt = tb.override_for(col["name"]) or _resolved_dt
            target_keys.append(_target_key_def(tb.target_column_id(col["name"], col_id), col["name"], _resolved_dt,
                                               tb.last_discovered or col,
                                               position=col.get("position", 0)))
    logger.debug(f"Using primary target keys for {tb.pair.table_name}: {target_keys}")
    return target_keys


def _target_keys_without_rules(tb, columns_def, relations):
    """The target keys of an entity the key rules do not apply to: the YAML keys, else discovery's primary key."""
    custom_config = tb.pair.custom_config
    if not (custom_config and 'keys' in custom_config and custom_config['keys'] is not None):
        return _target_keys_from_pk(tb)
    if _is_column_mappings(custom_config.get('columns', [])):
        return _target_keys_from_mappings(tb, columns_def, relations)
    return _target_keys_from_names(tb)


def _assemble_target_keys(tb, columns_def, relations, grouped, target_entity_type):
    """``(target_keys, name_relations)`` of a SingleTable entity, flagging isPK on its columns.

    A Gluesync 2.3 CoreHub checks the key rules on a SQL target with a locked schema; elsewhere, and on older
    CoreHubs, the keys are the union of discovery and the YAML keys, as they have always been.
    """
    pair = tb.pair
    rules_apply = grouped and not pair.is_unlocked_schema and not tb.ctx.target_is_nosql
    name_relations = _name_relations(relations, columns_def, pair.table_name, tb.target_names()) \
        if grouped and not pair.is_unlocked_schema else []
    for relation in name_relations:
        _add_name_relation_column(tb, relation, columns_def)
    if rules_apply:
        return _apply_key_rules(tb, columns_def, name_relations, target_entity_type)
    target_keys = _target_keys_without_rules(tb, columns_def, relations)
    _paint_is_pk_on_columns(tb.columns_def, target_keys)
    return target_keys, name_relations


def _legacy_matrix_relations(tb, columns_def, has_filter):
    """One pair per mapped source column, the target id from discovery by name (else the source id), as CoreHubs
    before Gluesync 2.3 have always received it."""
    legacy_relations = []
    for col_def in (columns_def if has_filter else tb.pair.columns.get('columns') or []):
        source_col_name = col_def.get('name')
        source_col_id = col_def.get('id')
        if source_col_id is None or not source_col_name:
            if has_filter:
                logger.warning(f"Skipping mapping matrix entry with missing id/name: {col_def}")
                continue
            _raise_missing_column_id(col_def.get('name'), tb.pair.table_name)
        target_col_name = col_def.get('alias', source_col_name) if has_filter else source_col_name
        target_col_id = _target_id_by_name(tb, target_col_name, source_col_id)
        legacy_relations.append({"sources": [source_col_id], "targets": [target_col_id], "explicit": has_filter})
    return legacy_relations


def _target_id_by_name(tb, target_col_name, default_id):
    """The id the target column named ``target_col_name`` got when it was built, else ``default_id``."""
    registered = tb.registered_target_id(str(target_col_name))
    return registered if registered is not None else default_id


def _mapping_entries(tb, columns_def, name_relations, has_filter, grouped):
    """The entries of the column mapping matrix of a SingleTable entity."""
    pair = tb.pair
    if grouped:
        relations_by_id = _relations_by_id(name_relations, columns_def, tb.columns_def, pair.table_name)
        return build_entries(
            pair.source_table_id, pair.target_table_id, relations_by_id,
            columns_def, tb.columns_def, grouped=True, unlocked=pair.is_unlocked_schema, label=pair.table_name,
        )
    legacy_relations = _legacy_matrix_relations(tb, columns_def, has_filter)
    return build_entries(pair.source_table_id, pair.target_table_id, legacy_relations, columns_def, tb.columns_def,
                         grouped=False, unlocked=pair.is_unlocked_schema, label=pair.table_name)


def _refuse_unbound_custom_functions(tb, field_functions):
    """Refuse a table whose ``CustomFn`` columns name a function the installed function collection does not export.

    A Gluesync 2.3 target agent refuses such an entity with a message about the column alone; this one names the
    table, the column and the function, and says the collection must be restored or published first. Raises
    :class:`entity_mapping.EntityMappingError`. CoreHub does not check the functions while the pipeline has no
    collection installed (one may be published after the entity is configured), and neither does this: it only
    warns, naming what is bound.
    """
    exported = tb.ctx.exported_functions
    if exported is None:
        return
    none_installed = exported is function_collection.NO_COLLECTION
    column_names = {c.get("id"): c.get("name") for c in tb.columns_def}
    unbound = {}
    for field_function in field_functions:
        missing = [name for name in function_collection.custom_function_names(field_function.get("expression"))
                   if none_installed or name not in exported]
        if missing:
            unbound[column_names.get(field_function.get("columnId"), field_function.get("columnId"))] = missing
    if not unbound:
        return
    if none_installed:
        logger.warning(function_collection.no_collection_message(tb.pair.table_name, unbound, tb.ctx.pipeline_id))
        return
    raise EntityMappingError(
        function_collection.unbound_functions_message(tb.pair.table_name, unbound, tb.ctx.pipeline_id, exported))


def _attach_field_functions(tb, target_entity_type, name_relations, grouped):
    """Field Functions from YAML columns[].expression → entityType.fieldFunctions (GSSD-1355)."""
    pair = tb.pair
    _ff = build_field_functions_for_entity_type(
        pair.custom_config.get("columns"),
        tb.columns_def,
        pair.target_table_id,
        fed_target_names=[t for r in name_relations if r["sources"] for t in r["targets"]] if grouped else None,
        label=pair.table_name,
    )
    if _ff:
        _refuse_unbound_custom_functions(tb, _ff)
        target_entity_type["fieldFunctions"] = _ff
        logger.info(
            f"Table {pair.table_name}: attaching {len(_ff)} field function(s) on target entityType"
        )


def _assemble_target_side(ctx, pair, columns_def, has_filter, discovery_type_by_source, target_entity_type,
                          relations=None, grouped=False):
    """The target columns, target keys, column mapping matrix and field functions of a SingleTable entity.

    Shared by the first loop of :func:`create_entities` and the ``@@`` duplicate loop. Discovers the target table,
    writes ``columnsMappingMatrix``, the unlocked schema arrays and ``fieldFunctions`` on ``target_entity_type`` and
    returns ``(target_columns_def, target_keys)``. ``has_filter``: the YAML ``columns:`` are column mappings or a
    whitelist (see :func:`_assemble_source_columns`).

    ``grouped`` (a Gluesync 2.3 CoreHub, which treats every entity the bootstrapper saves as new and runs its whole
    mapping stage on it): the matrix holds the YAML ``relations`` as grouped entries naming every column of the
    entity, a column fed by no source is added for each ``{target, expression}`` row, YAML target names take the
    spelling discovery reports, keys follow the R12 rules on a SQL target with a locked schema, and a fixed or
    technical field function on a target a source feeds is refused (P16). Otherwise the payload is the one CoreHubs
    before 2.3 have always received. Refusals raise :class:`entity_mapping.EntityMappingError`.
    """
    # Discover target columns first (needed for columnsMappingMatrix)
    tb = _TargetBuild(ctx, pair, _discover_target_columns(ctx, pair.target_table_name), discovery_type_by_source,
                      relations)

    # The column mapping matrix is written once the target columns are known (see below).
    _set_unlocked_schema_arrays(target_entity_type, pair)

    # User-declared per-column target dataType overrides (by source column name)
    tb.overrides = _build_target_data_type_overrides(pair.custom_config.get('columns', []), pair.source_names)
    if tb.overrides:
        logger.info(
            f"Table {pair.table_name}: applying {len(tb.overrides)} user-declared targetDataType overrides"
        )

    _build_target_columns(tb, relations, grouped)
    _add_produced_columns(tb, relations, grouped)
    _add_target_only_columns(tb)
    _fill_missing_target_types(tb, columns_def)

    print(f"Columns definition for {pair.table_name}: {columns_def}")

    target_keys, name_relations = _assemble_target_keys(tb, columns_def, relations, grouped, target_entity_type)

    entries = _mapping_entries(tb, columns_def, name_relations, has_filter, grouped)
    target_entity_type["columnsMappingMatrix"] = encode_entries(entries, grouped)
    logger.info(f"Table {pair.table_name}: columnsMappingMatrix with {len(target_entity_type['columnsMappingMatrix'])} entries")

    _attach_field_functions(tb, target_entity_type, name_relations, grouped)

    return tb.columns_def, target_keys


def _fill_missing_target_types(tb, columns_def):
    """Give every target column a dataType: CoreHub refuses a payload with a null one (400 on the whole PUT).

    A column is left without one when no mapping, discovery or YAML row supplied it; it takes the type its source
    column maps to, else the target's string type.
    """
    source_types = {c.get("name"): c.get("dataType") for c in columns_def}
    source_names = ColumnNames(source_types)
    for target_col in tb.columns_def:
        if target_col.get("dataType"):
            continue
        source_type = source_types.get(source_names.resolve(target_col.get("name", "")))
        target_col["dataType"] = tb.map_type(source_type)
        logger.warning(f"Table {tb.pair.table_name}: target column {target_col.get('name')} had no dataType, "
                       f"using {target_col['dataType']}")


def _assemble_chain_pair(ctx, pair, source_schema):
    """One table pair of a MultiTable chain for a Gluesync 2.3 CoreHub, through the SingleTable assembly (BOP-16 A).

    The 2.3 key rules run per table pair on a MultiTable target too, so the pair is assembled as the same table's
    SingleTable entity: same relations, keys, target columns and field functions. Returns ``(target_columns_def,
    relations, field_functions, source_key_names)``, the relations over column ids, for the chain loop to add its
    target-only columns and every discovered source column. Refusals raise :class:`entity_mapping.EntityMappingError`.
    """
    columns_def, _keys, has_mappings, is_whitelist, discovery_types, relations = _assemble_source_columns(
        pair.columns, pair.custom_config, pair.table_name, source_schema, grouped=True)
    pair_entity_type = {}
    target_columns_def, _target_keys = _assemble_target_side(
        ctx, pair, columns_def, has_mappings or is_whitelist, discovery_types, pair_entity_type,
        relations=relations, grouped=True,
    )
    pair_relations = []
    for entry in pair_entity_type["columnsMappingMatrix"]:
        _, _, sources, targets = read_entry(entry)
        pair_relations.append({"sources": sources, "targets": targets, "explicit": True})
    source_key_names = {c["name"] for c in columns_def if c.get("isPK")}
    return target_columns_def, pair_relations, pair_entity_type.get("fieldFunctions", []), source_key_names


# Initialize logger
log_file = create_log_file()
logger = get_logger(log_file)

# Environment variables with default values
CORE_HUB_URL = os.getenv('CORE_HUB_URL', 'https://localhost:1717')
ENTITY_START_TIMEOUT = float(os.getenv('ENTITY_START_TIMEOUT', '1'))
CREATE_TABLE_IF_NOT_EXISTS = os.getenv('CREATE_TABLE_IF_NOT_EXISTS', 'true').lower() == 'true'
DEFAULT_MAX_LOGICAL_PARTITIONS = int(os.getenv('MAX_LOGICAL_PARTITIONS', '10'))


def set_create_table_if_not_exists(enabled: bool) -> None:
    """Allow callers to toggle automatic table creation at runtime."""

    global CREATE_TABLE_IF_NOT_EXISTS
    CREATE_TABLE_IF_NOT_EXISTS = bool(enabled)


# Target categories for which the bootstrapper must NOT attempt to issue a
# CREATE TABLE statement. These are non-RDBMS targets (e.g. NoSQL stores,
# object stores like Azure Data Lake Storage, S3, GCS) where the CoreHub
# create-table endpoint will fail with "This function must not be called.".
_NO_CREATE_TABLE_TARGET_TYPES = frozenset({"NOSQL", "OBJECT STORE"})


def is_no_create_table_target(target_type) -> bool:
    """Return True when the given target type does not support CREATE TABLE.

    The check is case-insensitive and tolerates None / empty values. Any
    target category not classified as RDBMS/SQL should fall under this
    predicate so the bootstrapper skips table creation for it.
    """

    if not target_type:
        return False
    return str(target_type).strip().upper() in _NO_CREATE_TABLE_TARGET_TYPES


# ProtocolAwareAdapter and CoreHubClient have been moved to utils/core_hub_client.py

# Initialize the CoreHub client
core_hub_client = CoreHubClient(CORE_HUB_URL)

def get_allowed_operations(target_custom_properties):
    """
    Get allowedOperations array from target custom properties.
    
    Args:
        target_custom_properties (dict): Target custom properties from YAML config
        
    Returns:
        list: Array of allowed operations, defaults to ["INSERT", "DELETE", "UPDATE", "TRUNCATE"] if not specified
    """
    # Check if allowedOperations is explicitly defined
    if 'allowedOperations' in target_custom_properties:
        allowed_ops = target_custom_properties['allowedOperations']
        if isinstance(allowed_ops, list) and len(allowed_ops) > 0:
            logger.info(f"Using configured allowedOperations: {allowed_ops}")
            return allowed_ops
    
    # Check for legacy skipDeletion property for backward compatibility
    if target_custom_properties.get('skipDeletion', False):
        logger.info("Found legacy skipDeletion=true, converting to allowedOperations without DELETE")
        return ["INSERT", "UPDATE", "TRUNCATE"]
    
    # Default values if nothing is declared
    default_ops = ["INSERT", "DELETE", "UPDATE", "TRUNCATE"]
    logger.info(f"Using default allowedOperations: {default_ops}")
    return default_ops

_DEFAULT_GROUP = '_default'
_ENTITIES_CONFIG_PATH = "/pipelines/{pipeline_id}/config/entities"
_ENTITIES_PATH = "/pipelines/{pipeline_id}/entities"
_UNKNOWN_ENTITY = 'unknown'
_SOURCE_ROLE = "source"
_TARGET_ROLE = "target"
_TARGET_ONLY_COLUMNS_KEY = 'targetOnlyColumns'

# customProperties.target keys the target entityType handles itself, excluded from the passthrough spread
_TARGET_HANDLED_KEYS = frozenset({'allowedOperations', 'skipDeletion', 'snapshotWritingConcurrency',
                                  'useBulkOperationsDuringCDC', 'useBulkOperationsWhileSnapshot', 'udf'})


def _build_table_lookup(table_list, default_schema_name):
    """Discovered tables by ``(schema, name)`` lower-cased; a bare string is a table of ``default_schema_name``."""
    lookup = {}
    for tbl in table_list or []:
        if isinstance(tbl, dict):
            name = tbl.get("name")
            schema_name = tbl.get("schema") or default_schema_name
            table_info = tbl
        elif isinstance(tbl, str):
            name = tbl
            schema_name = default_schema_name
            table_info = {"name": name, "schema": schema_name}
        else:
            continue

        if not name or not schema_name:
            continue

        lookup[(schema_name.lower(), name.lower())] = table_info
    return lookup


def _find_discovered_table(lookup, schema_name, table_name):
    if not lookup or not schema_name or not table_name:
        return None
    return lookup.get((schema_name.lower(), table_name.lower()))


def _resolve_table_id(schema_name, table_name, lookup, table_role):
    """Try to reuse discovery IDs before falling back to generated IDs."""
    discovered_table = _find_discovered_table(lookup, schema_name, table_name)
    if discovered_table:
        discovered_id = discovered_table.get('id')
        if discovered_id is not None:
            logger.debug(
                f"Using discovered {table_role} table ID for {schema_name}.{table_name}: {discovered_id}"
            )
            return discovered_id

    generated_id = get_table_id(schema_name, table_name)
    logger.debug(
        f"Using generated {table_role} table ID for {schema_name}.{table_name}: {generated_id}"
    )
    return generated_id


def _parse_partition_config(partition_value):
    """Return (column_name, max_partitions) from YAML configuration."""

    column_name = None
    max_partitions = DEFAULT_MAX_LOGICAL_PARTITIONS

    if isinstance(partition_value, dict):
        column_name = partition_value.get('column') or partition_value.get('name')
        raw_max = partition_value.get('maxPartitionsNumber') or partition_value.get('maxPartitions')
        if raw_max is not None:
            try:
                max_partitions = max(1, int(raw_max))
            except (TypeError, ValueError):
                logger.warning(f"Invalid maxPartitionsNumber '{raw_max}'. Falling back to {DEFAULT_MAX_LOGICAL_PARTITIONS}.")
    elif isinstance(partition_value, str):
        column_name = partition_value
    elif partition_value is not None:
        logger.warning(f"Unsupported partitions configuration type: {type(partition_value)}. Expected string or dict.")

    return column_name, max_partitions


def _format_partition_object(partition_obj):
    if not isinstance(partition_obj, dict):
        return None

    partition_id = partition_obj.get('id')
    try:
        partition_id = int(partition_id)
    except (TypeError, ValueError):
        pass

    if 'startValue' not in partition_obj or 'endValue' not in partition_obj:
        return None

    return {
        "id": partition_id,
        "startValue": str(partition_obj.get('startValue')),
        "endValue": str(partition_obj.get('endValue'))
    }


def _partition_of_list_entry(entry):
    """The partition object of an entry of a ``partitions`` list: the entry itself, or its ``partition``."""
    if not isinstance(entry, dict):
        return None
    if {'id', 'startValue', 'endValue'}.issubset(entry.keys()):
        return entry
    if 'partition' in entry and isinstance(entry['partition'], dict):
        return entry['partition']
    return None


def _partition_of_dict_key(key):
    """The partition object of a key of a ``partitions`` map: the key itself, or the JSON it holds."""
    if isinstance(key, dict):
        return key
    if isinstance(key, str):
        try:
            return json.loads(key)
        except json.JSONDecodeError:
            logger.debug(f"Unable to decode partition key: {key}")
    return None


def _extract_computed_partitions(response):
    """Normalize compute-logical-partitions API responses into Gluesync partition lists."""

    partitions = []
    if not isinstance(response, dict):
        return partitions

    raw_partitions = response.get('partitions')
    if isinstance(raw_partitions, list):
        partition_objects = [_partition_of_list_entry(entry) for entry in raw_partitions]
    elif isinstance(raw_partitions, dict):
        partition_objects = [_partition_of_dict_key(key) for key in raw_partitions.keys()]
    else:
        partition_objects = []
    for partition_obj in partition_objects:
        formatted = _format_partition_object(partition_obj)
        if formatted:
            partitions.append(formatted)

    partitions.sort(key=lambda part: part.get('id') if isinstance(part.get('id'), int) else 0)
    return partitions


def _compute_logical_partitions(token, pipeline_id, entity_id, column_payload, max_partitions_number):
    """Invoke CoreHub to compute logical partitions for a given column."""

    endpoint = f"/pipelines/{pipeline_id}/config/entities/{entity_id}/compute-logical-partitions"
    safe_column_payload = json.loads(json.dumps(column_payload))
    body = {
        "maxPartitionsNumber": max(1, max_partitions_number),
        "column": safe_column_payload
    }

    logger.info(
        "Computing logical partitions for entity %s (column=%s, max=%s)",
        entity_id,
        safe_column_payload.get('name'),
        body["maxPartitionsNumber"]
    )

    response = fetch_core_hub(endpoint, method='POST', token=token, body=body)
    partitions = _extract_computed_partitions(response)

    if not partitions:
        logger.warning(
            "Logical partition computation returned no partitions for entity %s (column=%s)",
            entity_id,
            safe_column_payload.get('name')
        )

    return partitions


def _entities_by_name(pipeline_entities):
    """The entities of a pipeline listing by entityName."""
    entity_map = {}
    if not isinstance(pipeline_entities, list):
        logger.warning("Unexpected response while fetching entities for logical partitions: %s", pipeline_entities)
        return entity_map
    for item in pipeline_entities:
        entity_data = item.get('entity') if isinstance(item, dict) else None
        if not entity_data:
            continue
        entity_name = entity_data.get('entityName')
        if entity_name:
            entity_map[entity_name] = entity_data
    return entity_map


def _skip_partition_request(skip_errors, warning, error_message):
    """Log why a logical partition request is skipped, refusing it unless errors are skipped."""
    logger.warning(warning)
    if not skip_errors:
        raise RuntimeError(error_message)


def _partitioned_entity_payload(entity_name, entity_from_core, request, partitions, skip_errors):
    """The entity listed by CoreHub with the partitionSettings of its source agent entity, None if it has none."""
    entity_payload = json.loads(json.dumps(entity_from_core))
    source_entity = next(
        (agent for agent in entity_payload.get('agentEntities', [])
         if agent.get('agentId') == request["source_agent_id"] or agent.get('entityType', {}).get('type') == 'Source'),
        None
    )

    if not source_entity:
        _skip_partition_request(skip_errors, f"Source agent entity not found in payload for {entity_name}",
                                f"Source agent entity missing for {entity_name}")
        return None

    source_entity_type = source_entity.setdefault('entityType', {})
    source_entity_type['partitionSettings'] = {
        "column": request["column"],
        "partitions": partitions
    }
    return entity_payload


def _computed_partitions_of(token, pipeline_id, entity_name, entity_id, request, skip_errors):
    """The logical partitions CoreHub computes for a request, None when there are none or the call failed."""
    try:
        partitions = _compute_logical_partitions(token, pipeline_id, entity_id, request["column"],
                                                 request["max_partitions"])
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(f"Failed to compute logical partitions for {entity_name}: {exc}")
        if not skip_errors:
            raise
        return None

    if not partitions:
        _skip_partition_request(skip_errors, f"No partitions returned for {entity_name}. Skipping partitionSettings update.",
                                f"Logical partitions computation returned empty result for {entity_name}")
        return None
    return partitions


def _apply_partition_request(token, pipeline_id, request, entity_map, skip_errors):
    """Compute the logical partitions of one entity and save them on its source agent entity."""
    entity_name = request["entity_name"]

    entity_from_core = entity_map.get(entity_name)
    if not entity_from_core:
        _skip_partition_request(skip_errors, f"Cannot compute logical partitions: entity '{entity_name}' not found in pipeline",
                                f"Entity '{entity_name}' not found for logical partition computation")
        return

    entity_id = entity_from_core.get('entityId')
    if not entity_id:
        _skip_partition_request(skip_errors, f"Entity '{entity_name}' from pipeline listing has no entityId",
                                f"Entity '{entity_name}' missing ID in pipeline response")
        return

    partitions = _computed_partitions_of(token, pipeline_id, entity_name, entity_id, request, skip_errors)
    if not partitions:
        return

    entity_payload = _partitioned_entity_payload(entity_name, entity_from_core, request, partitions, skip_errors)
    if entity_payload is None:
        return

    update_body = {"entities": [entity_payload]}

    try:
        fetch_core_hub(
            _ENTITIES_CONFIG_PATH.format(pipeline_id=pipeline_id),
            method='PUT',
            token=token,
            body=update_body
        )
        logger.info(f"Applied logical partitions for entity {entity_name} (ID: {entity_id})")
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(f"Failed to update entity {entity_name} with logical partitions: {exc}")
        if not skip_errors:
            raise


def _apply_logical_partitions(token, pipeline_id, partition_requests, skip_errors):
    if not partition_requests:
        return

    try:
        pipeline_entities = fetch_core_hub(_ENTITIES_PATH.format(pipeline_id=pipeline_id), token=token)
    except Exception as exc:
        logger.error(f"Failed to fetch entities for logical partitions: {exc}")
        if not skip_errors:
            raise
        return

    entity_map = _entities_by_name(pipeline_entities)

    for request in partition_requests:
        _apply_partition_request(token, pipeline_id, request, entity_map, skip_errors)


def _build_partition_settings(column_name, table_columns, schema_name, table_name, table_id):
    """Create PartitionSettings payload for the specified column."""

    if not column_name:
        return None

    if not table_columns:
        logger.warning(f"Partition column '{column_name}' requested for {schema_name}.{table_name} but no columns were discovered")
        return None

    normalized_column = str(column_name).strip()
    if not normalized_column:
        return None

    matched_column = next((col for col in table_columns if col.get('name') == normalized_column), None)
    if not matched_column:
        logger.warning(f"Partition column '{normalized_column}' not found in discovery for {schema_name}.{table_name}. Skipping partitionSettings.")
        return None

    # Use the id field from CoreHub API as the column ID
    column_id = _require_column_id(matched_column, f"{schema_name}.{table_name}", label=normalized_column)

    partition_column = {
        "id": column_id,
        "name": normalized_column,
        "tableId": int(table_id),
        "position": matched_column.get('position', 0),
        "dataType": matched_column.get('dataType'),
    }

    # Include optional fields from matched_column
    for field in ['charMaxLength', 'charOctetLength', 'charSet', 'collation', 'numPrec', 'numPrecRadix', 'numScale', 'datetimePrec', 'isIdentity', 'isNullable', 'udtCategory', 'udtName']:
        if field in matched_column:
            partition_column[field] = matched_column[field]

    partition_column["isPK"] = matched_column.get('isPK', False)
    if 'default' in matched_column:
        partition_column["default"] = matched_column['default']

    partition_settings = {
        "column": partition_column,
        "partitions": []
    }

    logger.info(f"Configured partitionSettings for {schema_name}.{table_name} on column '{normalized_column}'")
    return partition_settings


def _yaml_schemas(yaml_config):
    """The schema sections of the YAML: under ``schemas``, else every top-level mapping."""
    schemas_dict = yaml_config.get('schemas', {})
    if not schemas_dict:
        schemas_dict = {k: v for k, v in yaml_config.items() if isinstance(v, dict)}
    return schemas_dict


def _schema_custom_tables(schema_config):
    """The custom tables of a schema section, in both direct and nested 'tables.custom' structures."""
    if 'tables' in schema_config and 'custom' in schema_config['tables']:
        return schema_config['tables']['custom']
    if 'custom' in schema_config:
        return schema_config['custom']
    return {}


def _collect_table_group(group_names, schema_name, table_key, table_data):
    """Add the groupId of a YAML table to ``group_names``."""
    if 'groupId' in table_data and table_data['groupId'] != _DEFAULT_GROUP:
        group_name = str(table_data['groupId']).strip()
        if group_name:  # Only add non-empty group names
            group_names.add(group_name)
            logger.debug(f"Found group '{group_name}' for table {schema_name}.{table_key}")
        else:
            logger.warning(f"Empty group ID found for table {schema_name}.{table_key}")


def _collect_groups_and_chains(yaml_config, source_schema):
    """The group names of the whole YAML (groups belong to the pipeline) and the MultiTable chains of this schema.

    create_entities runs once per schema, and a chain of another schema would otherwise be built from this
    schema's tables. Returns ``(group_names, chained_tables)``, the tables of each chainId in YAML order.
    """
    group_names = set()
    chained_tables = {}  # Dictionary to store tables by chainId
    if not yaml_config:
        return group_names, chained_tables

    for schema_name, schema_config in _yaml_schemas(yaml_config).items():
        # Process each table in the config
        for table_key, table_data in _schema_custom_tables(schema_config).items():
            _collect_table_group(group_names, schema_name, table_key, table_data)
            # Collect chainId information (independent of groupId), for this schema only
            if 'chainId' in table_data and schema_name == source_schema:
                chained_tables.setdefault(table_data['chainId'], []).append((table_key, table_data))

        # Note: Do NOT add group names from group-level schedules to group_names here,
        # because keys might be group IDs and we must not try to create groups using IDs as names.
        # We'll resolve schedule keys to IDs later, during schedule creation.
    return group_names, chained_tables


def _create_groups(token, pipeline_id, yaml_config, group_names):
    """Create the groups before creating entities; returns the group ID of each group name."""
    groups_config = {}
    if yaml_config:
        raw_groups = yaml_config.get('groups')
        if isinstance(raw_groups, dict):
            groups_config = raw_groups

    group_id_map = {}
    logger.debug(f"Groups to create: {group_names}")

    for group_name in group_names:
        logger.debug(f"Processing group: '{group_name}' (type: {type(group_name)})")

        group_meta = groups_config.get(group_name)
        description = None
        color = None
        if isinstance(group_meta, dict):
            description = group_meta.get('description')
            color = group_meta.get('color')

        group_id = create_group(token, pipeline_id, group_name, description=description, color=color)
        logger.debug(f"Group '{group_name}' creation result: {group_id}")

        if group_id and group_id != _DEFAULT_GROUP:
            # Map the group name to its ID for later use
            group_id_map[group_name] = group_id
            logger.info(f"Created/retrieved group '{group_name}' with ID: {group_id}")
        else:
            logger.warning(f"Failed to create/retrieve group '{group_name}'. Using default group.")
            group_id_map[group_name] = _DEFAULT_GROUP

    # Log all group mappings for debugging
    logger.info(f"Group name to ID mappings: {json.dumps(group_id_map, indent=2)}")
    return group_id_map


def _node_infos(token, pipeline_id, source_agent_id, target_agent_id, yaml_config):
    """The node info of both agents (for data type mapping), logged with the YAML."""
    source_node_info = get_node_info(token, pipeline_id, source_agent_id)
    target_node_info = get_node_info(token, pipeline_id, target_agent_id)

    logger.info("Source Node Info:")
    logger.info(json.dumps(source_node_info, indent=2))
    logger.info("Target Node Info:")
    logger.info(json.dumps(target_node_info, indent=2))

    logger.debug(f"Full YAML config: {json.dumps(yaml_config, indent=2)}")
    return source_node_info, target_node_info


class _SchemaSettings:
    """The YAML settings of the source schema a create_entities run works on."""

    def __init__(self, yaml_config, source_schema, target_schema):
        schemas_root = yaml_config.get('schemas', yaml_config)
        schema_config = schemas_root.get(source_schema, {})
        logger.debug(f"Schema config for {source_schema}: {json.dumps(schema_config, indent=2)}")

        self.yaml_target_schema = schema_config.get('target', target_schema)
        self.whitelist = schema_config.get('tables', {}).get('whitelist', [])
        self.blacklist = schema_config.get('tables', {}).get('blacklist', [])
        # Handle empty custom tables attribute - convert None to empty dict
        tables_config = schema_config.get('tables', {})
        self.custom_tables = tables_config.get('custom', {})
        if self.custom_tables is None:
            self.custom_tables = {}
            logger.warning("Warning: 'custom' attribute is present but empty in YAML. Converting to empty dict.")

        # Get schema-level custom properties
        schema_custom_properties = schema_config.get('customProperties', {})
        self.global_source_custom_properties = schema_custom_properties.get('source', {})
        self.global_target_custom_properties = schema_custom_properties.get('target', {})

        logger.debug(f"Target schema: {self.yaml_target_schema}")
        logger.debug(f"Whitelist: {self.whitelist}")
        logger.debug(f"Blacklist: {self.blacklist}")
        logger.debug(f"Custom tables: {self.custom_tables}")
        logger.debug(f"Global source custom properties: {self.global_source_custom_properties}")
        logger.debug(f"Global target custom properties: {self.global_target_custom_properties}")


class _EntityRun:
    """One create_entities run: its pipeline context, the YAML settings of its schema, and what it builds.

    ``last_table_name`` and ``last_col`` are the table name and the discovered column the table loops looked at
    last: the chain loop names that table in some of its errors, and copies the optional fields of that column
    onto some table headers, as it always has.
    """

    def __init__(self, ctx, schema, source_schema, target_schema, source_agent_id, source_type, target_type, grouped):
        self.ctx = ctx
        self.schema = schema
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.source_agent_id = source_agent_id
        self.source_type = source_type
        self.target_type = target_type
        self.grouped = grouped
        self.group_id_map = {}
        self.source_tables_lookup = {}
        self.target_tables_lookup = {}
        self.entities = []
        # Tables whose entity is refused before any request, with the reason logged; counted as failed.
        self.refused_tables = []
        # Every entity of the run that was not created, as "<entity>: <reason>", returned to the caller (BOP-18 A).
        self.errors = []
        self.pending_partition_requests = []
        # Track which YAML keys have been processed to avoid duplicates
        self.processed_yaml_keys = set()
        # Track discovered table IDs for chain processing
        self.chain_source_ids = {}
        self.chain_target_ids = {}
        self.last_table_name = None
        self.last_col = None

    def refuse_table(self, table_label, error):
        logger.error(f"Entity for table {table_label} not created: {error}")
        self.refused_tables.append(table_label)
        self.errors.append(f"{table_label}: {error}")
        if not self.ctx.skip_errors:
            raise error

    def table_columns(self, table_name, custom_config=None):
        """The source discovery of a table of the schema, in the order the YAML ``custom_config`` of the table gives
        the columns when the source has no schema (see :func:`_with_exported_source_positions`)."""
        ctx = self.ctx
        columns = get_table_columns(ctx.token, ctx.pipeline_id, self.source_agent_id, self.source_schema, table_name)
        if ctx.source_is_nosql and custom_config:
            columns = _with_exported_source_positions(columns, custom_config, table_name)
        return columns

    def queue_partition_request(self, entity_name, partition_settings, max_partitions):
        if partition_settings:
            self.pending_partition_requests.append({
                "entity_name": entity_name,
                "column": partition_settings["column"],
                "max_partitions": max_partitions,
                "source_agent_id": self.source_agent_id
            })


def _discover_target_tables(ctx):
    """The tables of the target schema by ``(schema, name)``, {} when they cannot be listed."""
    target_tables_lookup = {}
    try:
        target_discovered_tables = get_agent_tables(ctx.token, ctx.pipeline_id, ctx.target_agent_id,
                                                    ctx.yaml_target_schema)
        target_tables_lookup = _build_table_lookup(target_discovered_tables, ctx.yaml_target_schema)
        logger.info(f"Discovered {len(target_tables_lookup)} tables in target schema {ctx.yaml_target_schema}")
    except Exception as e:
        logger.warning(f"Could not retrieve target tables for schema {ctx.yaml_target_schema}: {str(e)}")
    return target_tables_lookup


@dataclass
class _TableProperties:
    """The custom properties of a YAML table merged over the schema ones, and what is taken out of them."""
    source: Any
    target: Any
    partition_column: Any
    partition_max: Any
    udf_config: Any
    is_unlocked_schema: Any


def _log_snapshot_write_method(custom_config, label, duplicate):
    """Log the snapshot write method (UPSERT or INSERT, default is UPSERT) of a table.

    It is kept separately and NOT added to custom properties: it is read directly from the YAML config when
    needed during sync operations.
    """
    snapshot_write_method = custom_config.get('snapshotWriteMethod', 'UPSERT').upper()
    if snapshot_write_method not in ['UPSERT', 'INSERT']:
        if duplicate:
            logger.warning(f"Invalid snapshotWriteMethod '{snapshot_write_method}' for {label}. Using 'UPSERT'")
        else:
            logger.warning(f"Warning: Invalid snapshotWriteMethod '{snapshot_write_method}' for {label}. Using default 'UPSERT'")
    logger.info(f"Snapshot write method for {label}: {snapshot_write_method}")


def _table_properties(run, custom_config, label, duplicate=False):
    """The merged custom properties of a table, None when it is refused (unlocked schema without UDF)."""
    _log_snapshot_write_method(custom_config, label, duplicate)

    # Get table-specific custom properties and merge with global properties
    table_custom_properties = custom_config.get('customProperties', {})
    source_custom_properties = {**run.schema.global_source_custom_properties, **table_custom_properties.get('source', {})}
    target_custom_properties = {**run.schema.global_target_custom_properties, **table_custom_properties.get('target', {})}

    partition_config = source_custom_properties.pop('partitions', None)
    partition_column_name, partition_max_partitions = _parse_partition_config(partition_config)

    # Store UDF configuration separately (will be added to entityType, not customProperties)
    udf_config = target_custom_properties.pop('udf', None)

    # Check if this table is configured for unlocked schema
    is_unlocked_schema = custom_config.get('unlockedSchema', False)

    # Validate: UDF is mandatory for unlocked schema
    if is_unlocked_schema and not udf_config:
        if duplicate:
            logger.error(f"Table {label} has unlockedSchema=true but no UDF. Skipping.")
        else:
            logger.error(f"Table {label} is configured with unlockedSchema=true but no UDF is defined. UDF is mandatory for unlocked schema.")
        if not run.ctx.skip_errors:
            raise ValueError(f"UDF is mandatory for table {label} with unlocked schema")
        return None

    return _TableProperties(source_custom_properties, target_custom_properties, partition_column_name,
                            partition_max_partitions, udf_config, is_unlocked_schema)


def _document_key_column_id(run, columns, key_name, table_label, duplicate):
    """The id of the discovered column a documentKey key names, None if there is none."""
    for col in columns["columns"]:
        run.last_col = col
        if col.get('name') == key_name:
            # Use the id field from CoreHub API as the column ID
            column_id = col.get('id')
            if column_id is None and duplicate:
                error_msg = f"Column '{key_name}' in {table_label} missing 'id' field"
                logger.error(error_msg)
                raise ValueError(error_msg)
            if column_id is None:
                _raise_missing_column_id(col.get('name'), table_label)
            return column_id
    return None


def _document_key(run, custom_config, columns, table_label, duplicate=False):
    """The document key configuration of a table, with its key column names converted to column IDs."""
    if not (custom_config and 'documentKey' in custom_config):
        return None
    doc_key_config = custom_config['documentKey']

    key_ids = []
    for key_name in doc_key_config['keys'] if 'keys' in doc_key_config else []:
        column_id = _document_key_column_id(run, columns, key_name, table_label, duplicate)
        if column_id is not None:
            key_ids.append(column_id)
        elif not duplicate:
            logger.warning(f"Warning: Column '{key_name}' not found in table columns for document key")

    document_key = {
        "prefix": doc_key_config.get('prefix', ''),
        "suffix": doc_key_config.get('suffix', ''),
        "separator": doc_key_config.get('separator', '-'),
        "keys": key_ids  # Use column IDs instead of names
    }
    if not duplicate:
        logger.debug(f"Document key configuration for {table_label}: {document_key}")
    return document_key


def _process_filters(run, custom_config, columns, target_table_id, label):
    """The filter and snapshot delete filter of a table (they live in the target entityType)."""
    ctx = run.ctx
    filter_config = custom_config.get('filter')
    snapshot_delete_filter_config = custom_config.get('snapshotDeleteFilter')
    processed_filters = process_filter_clauses(filter_config, columns, table_id=target_table_id,
        source_node_info=ctx.source_node_info, target_node_info=ctx.target_node_info) if filter_config else None
    logger.debug(f"Processed filters for {label}: {processed_filters}")
    processed_snapshot_delete_filters = process_filter_clauses(snapshot_delete_filter_config, columns, table_id=target_table_id,
        source_node_info=ctx.source_node_info, target_node_info=ctx.target_node_info) if snapshot_delete_filter_config else None
    if processed_snapshot_delete_filters:
        logger.debug(f"Processed snapshot delete filters for {label}: {processed_snapshot_delete_filters}")
    return processed_filters, processed_snapshot_delete_filters


def _where_clause_properties(table_config, source_table_key=None):
    """The tablesProperties of a source table: its ``whereClause``, logged unless ``source_table_key`` is None."""
    table_properties = {}
    if table_config and 'whereClause' in table_config:
        where_clause = table_config['whereClause']
        if where_clause:
            table_properties["whereClause"] = str(where_clause)
            if source_table_key is not None:
                logger.info(f"Added whereClause for {source_table_key}: {where_clause}")
    return table_properties


def _source_entity(run, pair, columns_def, source_custom_properties, verbose):
    """The source agent entity of a SingleTable entity; ``verbose`` logs its type and whereClause."""
    source_schema, table_name = run.source_schema, pair.table_name
    source_table_key = f"{source_schema}.{table_name}"
    source_entity_type = {**source_custom_properties, "type": "Source"}

    # Determine source entity type based on source_type
    # Check if source_type indicates NoSQL (case-insensitive)
    is_source_nosql = run.source_type and "nosql" in run.source_type.lower()
    source_entity_type_name = "NoSqlEntity" if is_source_nosql else "SingleTable"
    if verbose:
        logger.debug(f"Source entity type determination: source_type='{run.source_type}', is_source_nosql={is_source_nosql}, selected type='{source_entity_type_name}'")

    # Build source table properties with optional whereClause
    source_table_properties = _where_clause_properties(pair.custom_config, source_table_key if verbose else None)

    return {
        "type": source_entity_type_name,
        "entityType": source_entity_type,
        "agentId": run.source_agent_id,
        "entityObject": {
            "id": str(pair.source_table_id),
            "scope": source_schema,
            "collection": table_name
        },
        "table": {
            "id": str(pair.source_table_id),
            "name": table_name,
            "schema": source_schema
        },
        "columns": columns_def,
        "customProperties": source_custom_properties,
        "tablesProperties": {source_table_key: source_table_properties}
    }


def _target_entity_type(target_custom_properties, processed_filters, processed_snapshot_delete_filters, udf_config):
    """The target entityType of a SingleTable entity, before its columns are assembled.

    Holds allowedOperations and the bulk operations settings; any unknown customProperties.target keys are
    spread in dynamically. UDFs must be in entityType, not in customProperties.
    """
    # Get allowed operations for the target entity
    allowed_operations = get_allowed_operations(target_custom_properties)

    _target_extra = {k: v for k, v in target_custom_properties.items() if k not in _TARGET_HANDLED_KEYS}

    target_entity_type = {
        **_target_extra,
        "type": "Target",
        "allowedOperations": allowed_operations,
        "snapshotWritingConcurrency": target_custom_properties.get('snapshotWritingConcurrency', 1),
        "useBulkOperationsDuringCDC": target_custom_properties.get('useBulkOperationsDuringCDC', False),
        "useBulkOperationsWhileSnapshot": target_custom_properties.get('useBulkOperationsWhileSnapshot', False)
    }

    # Add filters if they exist
    if processed_filters:
        target_entity_type["filter"] = processed_filters
    if processed_snapshot_delete_filters:
        target_entity_type["snapshotDeleteFilter"] = processed_snapshot_delete_filters

    # Add UDFs to entityType if they exist
    if udf_config:
        target_entity_type["udf"] = udf_config
        # Also add mappingFunctionInfo for the first UDF
        target_entity_type["mappingFunctionInfo"] = udf_config[0]
    return target_entity_type


def _target_entity(run, pair, target_columns_def, target_custom_properties, target_entity_type, table_obj,
                   table_properties, verbose):
    """The target agent entity of a SingleTable entity; ``verbose`` logs its type."""
    yaml_target_schema = run.ctx.yaml_target_schema
    # Determine target entity type based on target_type
    # Check if target_type indicates NoSQL (case-insensitive)
    is_target_nosql = run.target_type and "nosql" in run.target_type.lower()
    target_entity_type_name = "NoSqlEntity" if is_target_nosql else "SingleTable"
    if verbose:
        logger.debug(f"Target entity type determination: target_type='{run.target_type}', is_target_nosql={is_target_nosql}, selected type='{target_entity_type_name}'")

    return {
        "type": target_entity_type_name,
        "entityType": target_entity_type,
        "agentId": run.ctx.target_agent_id,
        "entityObject": {
            "id": str(pair.target_table_id),
            "scope": yaml_target_schema,
            "collection": pair.target_table_name
        },
        "table": table_obj,
        "columns": target_columns_def,  # Use target columns definition
        "customProperties": target_custom_properties,
        "tablesProperties": {f"{yaml_target_schema}.{pair.target_table_name}": table_properties}
    }


def _define_table_udfs(run, table_name, table_udfs, label):
    """Process the UDFs defined for a table (``label`` names it in the logs)."""
    logger.info(f"Found UDFs for {label}: {table_udfs}")
    try:
        handle_udf_function_definition(table_name, run.ctx.pipeline_id, table_udfs, run.ctx.token)
        logger.info(f"Successfully processed UDFs for {label}")
    except Exception as e:
        logger.error(f"Failed to process UDFs for {label}: {str(e)}")
        if not run.ctx.skip_errors:
            raise


def _table_name_and_schema(table):
    """The name and the schema (None when unknown, for string-only tables) of a discovered table."""
    if isinstance(table, str):
        return table, None
    return table.get("name"), table.get("schema")


def _bare_table_name(table_name):
    """The table name without the library qualifier an AS/400 agent may return (FILELIB.TABLE → TABLE)."""
    return table_name.rsplit('.', 1)[-1] if '.' in table_name else table_name


def _in_schema_whitelist(run, table_name, bare_table_name):
    """STRICT WHITELIST ENFORCEMENT: if a whitelist is defined, ONLY the tables it lists are processed.

    If the agent returns FILELIB.TABLE but the whitelist has TABLE, the bare name is checked as a fallback so
    tables are not silently dropped when the source uses qualified names.
    """
    whitelist = run.schema.whitelist
    if not whitelist:
        logger.debug(f"No whitelist defined for schema {run.source_schema}, allowing table {table_name}")
        return True
    if table_name not in whitelist and bare_table_name not in whitelist:
        logger.info(f"Skipping table {table_name}: not in whitelist for schema {run.source_schema}")
        return False
    logger.debug(f"Table {table_name} is in whitelist for schema {run.source_schema} - processing")
    return True


def _skip_discovered_table(run, table_name, table_schema):
    """Whether a discovered table gets no entity: no name, another schema, a system, blacklisted or not whitelisted table."""
    if not table_name:
        logger.warning("Warning: Table without name encountered. Skipping.")
        return True

    # Validate that the table belongs to the requested source schema
    # The API may return tables from all schemas, so we need to filter
    if table_schema and table_schema.lower() != run.source_schema.lower():
        logger.debug(f"Skipping table {table_name}: belongs to schema '{table_schema}' but processing schema '{run.source_schema}'")
        return True

    # Skip system tables
    if table_name.startswith("sys"):
        logger.info(f"Skipping system table: {table_name}")
        return True

    # Skip blacklisted tables, the bare table name included (a blacklist entry 'TABLE' still matches
    # 'FILELIB.TABLE' from discovery).
    bare_table_name = _bare_table_name(table_name)
    blacklist = run.schema.blacklist
    if blacklist and (table_name in blacklist or bare_table_name in blacklist):
        logger.info(f"Skipping blacklisted table: {table_name}")
        return True

    return not _in_schema_whitelist(run, table_name, bare_table_name)


def _discover_source_table(run, table_name, custom_config=None):
    """The source discovery of a table, None (refused unless errors are skipped) when it has no columns."""
    columns = run.table_columns(table_name, custom_config)

    # Debug logging for column discovery
    if columns and columns.get('columns'):
        logger.info(f"Discovered {len(columns['columns'])} columns for table {table_name}: {[col.get('name') for col in columns['columns']]}")
        return columns
    logger.warning(f"No columns discovered for table {table_name}. columns={columns}")

    # Validate that columns were discovered
    error_msg = f"Cannot create entity for table {table_name}: No columns discovered from source. Please verify the table exists and is accessible."
    logger.error(error_msg)
    if not run.ctx.skip_errors:
        raise ValueError(error_msg)
    return None


def _custom_config_of(custom_tables, table_name):
    """``(custom_config, yaml_table_key)``: the YAML entry of a discovered table and the key it is under.

    Handles both a direct key match and a ``sourceTableName`` match (duplicate table), then, for a
    library-qualified name (e.g. AS/400 FILELIB.TABLE), the bare table name as the YAML key: the backup YAML uses
    the unqualified name as the key but the source agent returns the library-prefixed form during discovery.
    """
    custom_config = custom_tables.get(table_name, {})
    yaml_table_key = table_name  # Track which YAML key we're using

    if not custom_config:
        for key, config in custom_tables.items():
            if isinstance(config, dict) and config.get('sourceTableName') == table_name:
                custom_config = config
                yaml_table_key = key
                logger.info(f"Found custom config for table {table_name} under YAML key '{key}' (duplicate table)")
                break

    bare_table_name = _bare_table_name(table_name)
    if not custom_config and '.' in table_name and bare_table_name in custom_tables:
        custom_config = custom_tables[bare_table_name]
        yaml_table_key = bare_table_name
        logger.info(
            f"Matched library-qualified table '{table_name}' to YAML key '{bare_table_name}' "
            "via bare-name fallback"
        )

    if custom_config is None:
        custom_config = {}
        logger.warning(f"Warning: Custom config for table {table_name} is present but empty in YAML. Converting to empty dict.")
    logger.debug(f"Custom config for {table_name}: {custom_config}")
    return custom_config, yaml_table_key


def _create_target_table(run, pair, keys):
    """Create the target table of a discovered table when CREATE_TABLE_IF_NOT_EXISTS is on (RDBMS targets only)."""
    if not CREATE_TABLE_IF_NOT_EXISTS:
        return
    ctx = run.ctx
    # Only create tables for RDBMS targets; skip for NoSQL, Object Store,
    # and any other non-RDBMS category. target_type is normalized upstream.
    if is_no_create_table_target(run.target_type):
        logger.info(f"Skipping table creation for {pair.target_table_name} - target is {run.target_type}")
        return
    logger.info(f"CREATE_TABLE_IF_NOT_EXISTS is enabled - creating table {pair.target_table_name}")
    handle_table_creation(
        ctx.pipeline_id,
        pair.target_table_name,
        ctx.yaml_target_schema,
        keys,
        ctx.token,
        pair.columns,
        pair.custom_config,
        ctx.source_node_info,
        ctx.target_node_info,
    )


def _discovered_source_table_id(run, table, table_name):
    """The source table ID of a discovered table: its own, else the one discovery lists, else a generated one."""
    if isinstance(table, dict) and table.get('id') is not None:
        source_table_id = table.get('id')
        logger.debug(f"Using discovered source table ID for {run.source_schema}.{table_name}: {source_table_id}")
        return source_table_id
    return _resolve_table_id(run.source_schema, table_name, run.source_tables_lookup, _SOURCE_ROLE)


def resolve_group_id(custom_config, group_id_map, token, pipeline_id):
    """The Core Hub group id for a table's YAML config, or '_default'.

    The YAML names the group under 'groupId' (older templates used 'group'). Groups are created
    up front into group_id_map; a group missing there is created/fetched here and cached.
    """
    requested_group = str(custom_config.get('groupId') or custom_config.get('group') or _DEFAULT_GROUP).strip()
    logger.debug(f"Requested group: '{requested_group}'")
    if not requested_group or requested_group == _DEFAULT_GROUP:
        logger.debug("Using default group as no specific group was requested")
        return _DEFAULT_GROUP

    # Use the pre-created group ID from the upfront group ID cache to avoid making a redundant API call per
    # entity (which causes the import to appear to loop indefinitely when many entities share the same group).
    group_id = group_id_map.get(requested_group)
    if group_id and group_id != _DEFAULT_GROUP:
        logger.debug(f"Using cached group ID for '{requested_group}': {group_id}")
    else:
        # Fallback: try to create/get the group (e.g. if upfront pre-creation failed)
        group_id = create_group(token, pipeline_id, requested_group)
        if group_id != _DEFAULT_GROUP:
            # Cache it so subsequent entities skip the API call
            group_id_map[requested_group] = group_id

    if group_id != _DEFAULT_GROUP:
        logger.info(f"Using group '{requested_group}' with ID: {group_id}")
    else:
        logger.warning(f"Could not use group '{requested_group}'. Using default group.")
    return group_id


def _requested_group_id(run, custom_config):
    """The group ID of the group a table requests ('_default' when none, or when it cannot be used)."""
    return resolve_group_id(custom_config, run.group_id_map, run.ctx.token, run.ctx.pipeline_id)


def _entity_payload(entity_name, source_entity, target_entity, group_id):
    """A SingleTable entity; the groupId is only added for a valid group ID."""
    entity = {
        "entityName": entity_name,
        "agentEntities": [source_entity, target_entity],
    }
    if group_id and group_id != _DEFAULT_GROUP:
        entity["groupId"] = group_id
    return entity


def _discovered_table_source_columns(run, pair):
    """The source assembly of a discovered table (see :func:`_assemble_source_columns`), None when refused.

    A table of a MultiTable chain gets its entity from the chain loop; the SingleTable entity built here for it
    is not sent, so it keeps the payload of older CoreHubs.
    """
    custom_config, table_name = pair.custom_config, pair.table_name
    chain_id = custom_config.get('chainId')
    try:
        return _assemble_source_columns(pair.columns, custom_config, table_name, run.source_schema,
                                        grouped=run.grouped and not chain_id, check_forms=not chain_id)
    except EntityMappingError as error:
        if chain_id:
            # Its chain is refused for the same reason by the chain loop below.
            logger.debug(f"Table {table_name} of chain {chain_id}: {error}")
        else:
            run.refuse_table(table_name, error)
        return None


def _discovered_table_target_side(run, pair, source, target_entity_type):
    """The target side assembly of a discovered table: ``(target_columns_def, target_keys)``, None when refused.

    A table of a chain is written, or refused, by the chain loop; the rest of the first loop (partitions, UDFs)
    still runs for it, whose SingleTable entity is not sent.
    """
    columns_def, _keys, has_column_mappings, is_column_whitelist, discovery_type_by_source, relations = source
    chain_id = pair.custom_config.get('chainId')
    try:
        return _assemble_target_side(
            run.ctx, pair, columns_def, has_column_mappings or is_column_whitelist, discovery_type_by_source,
            target_entity_type, relations=relations, grouped=run.grouped and not chain_id,
        )
    except EntityMappingError as error:
        if not chain_id:
            run.refuse_table(pair.table_name, error)
            return None
        logger.debug(f"Table {pair.table_name} of chain {chain_id}: {error}")
        return [], []


def _add_discovered_table_entity(run, table, pair, props, document_key):
    """Build the SingleTable entity of a discovered table (queued unless the table belongs to a chain)."""
    custom_config, table_name, ctx = pair.custom_config, pair.table_name, run.ctx
    source = _discovered_table_source_columns(run, pair)
    if source is None:
        return
    columns_def, keys = source[0], source[1]
    if not keys:
        logger.warning(f"Warning: No keys specified for {table_name}. Table will have no keys.")

    _create_target_table(run, pair, keys)

    # Generate table IDs for use in entities (prefer discovered IDs)
    pair.source_table_id = _discovered_source_table_id(run, table, table_name)
    pair.target_table_id = _resolve_table_id(ctx.yaml_target_schema, pair.target_table_name,
                                             run.target_tables_lookup, _TARGET_ROLE)

    # Process filter clauses using target_table_id (filters live in the target entityType)
    processed_filters, processed_snapshot_delete_filters = _process_filters(
        run, custom_config, pair.columns, pair.target_table_id, table_name)

    partition_settings = None
    if props.partition_column:
        partition_settings = _build_partition_settings(props.partition_column, pair.columns.get("columns"),
                                                       run.source_schema, table_name, pair.source_table_id)

    source_entity = _source_entity(run, pair, columns_def, props.source, verbose=True)
    target_entity_type = _target_entity_type(props.target, processed_filters, processed_snapshot_delete_filters,
                                             props.udf_config)

    target_side = _discovered_table_target_side(run, pair, source, target_entity_type)
    if target_side is None:
        return
    target_columns_def, _target_keys = target_side

    table_obj = {"id": str(pair.target_table_id), "schema": ctx.yaml_target_schema, "name": pair.target_table_name}
    target_entity = _target_entity(run, pair, target_columns_def, props.target, target_entity_type, table_obj, {},
                                   verbose=True)
    # Add document key mapping if configured
    if document_key:
        target_entity["keyMapping"] = document_key

    group_id = _requested_group_id(run, custom_config)

    # Create the entity
    # Use custom entityName from YAML if provided, otherwise fall back to schema.table format
    entity = _entity_payload(custom_config.get('entityName', f"{run.source_schema}.{table_name}"),
                             source_entity, target_entity, group_id)

    # A table of a MultiTable chain is written by its chain's entity (below), not as a SingleTable entity of
    # its own; the rest of this loop (table creation, partitions, UDFs) still runs for it.
    if custom_config.get('chainId'):
        logger.debug(f"Table {table_name} belongs to chain {custom_config.get('chainId')}: no SingleTable entity")
    else:
        run.entities.append(entity)

    run.queue_partition_request(entity["entityName"], partition_settings, props.partition_max)

    # Process UDFs if defined for this table
    table_udfs = custom_config.get('customProperties', {}).get('target', {}).get('udf', [])
    if table_udfs:
        _define_table_udfs(run, table_name, table_udfs, f"table {table_name}")
    else:
        logger.debug(f"No UDFs defined for table {table_name}")


def _process_discovered_table(run, table):
    """The first loop of :func:`create_entities`: the SingleTable entity of a table discovery returns."""
    table_name, table_schema = _table_name_and_schema(table)
    run.last_table_name = table_name
    if _skip_discovered_table(run, table_name, table_schema):
        return

    custom_config, yaml_table_key = _custom_config_of(run.schema.custom_tables, table_name)
    columns = _discover_source_table(run, table_name, custom_config)
    if columns is None:
        return

    # Mark this YAML key as processed
    run.processed_yaml_keys.add(yaml_table_key)

    props = _table_properties(run, custom_config, table_name)
    if props is None:
        return

    logger.debug(f"Source custom properties for {table_name}: {props.source}")
    logger.debug(f"Target custom properties for {table_name}: {props.target}")

    target_table_name = _target_table_name(custom_config, table_name, yaml_table_key)
    logger.info(f"Using target table name: {target_table_name} for source table: {table_name}")

    # Get document key configuration if it exists
    document_key = _document_key(run, custom_config, columns, table_name)

    pair = _TablePair(table_name, target_table_name, columns, custom_config, None, None, props.is_unlocked_schema)
    _add_discovered_table_entity(run, table, pair, props, document_key)


def _duplicate_table_label(run, yaml_table_key, custom_config):
    """The source table name of a YAML table the first loop did not process, None when it gets no entity."""
    # Skip if already processed in first loop
    if yaml_table_key in run.processed_yaml_keys:
        return None

    # Skip if not a dict
    if not isinstance(custom_config, dict):
        logger.warning(f"Skipping {yaml_table_key}: not a dict")
        return None

    # Get the actual source table name (should have sourceTableName for @@ tables)
    table_name = custom_config.get('sourceTableName', yaml_table_key)
    run.last_table_name = table_name

    # Skip system tables and blacklisted tables
    blacklist, whitelist = run.schema.blacklist, run.schema.whitelist
    if table_name.startswith("sys") or (blacklist and table_name in blacklist):
        logger.info(f"Skipping duplicate table {yaml_table_key}: system or blacklisted")
        return None

    # Check whitelist using the actual table name
    if whitelist and table_name not in whitelist:
        logger.info(f"Skipping duplicate table {yaml_table_key}: not in whitelist")
        return None

    # Check if this is part of a chain - if so, skip (will be processed in chain loop)
    if custom_config.get('chainId'):
        logger.info(f"Skipping duplicate table {yaml_table_key}: part of chain {custom_config.get('chainId')}")
        run.processed_yaml_keys.add(yaml_table_key)
        return None
    return table_name


def _duplicate_table_columns(run, yaml_table_key, table_name, custom_config):
    """``(discovered, columns)``: the source discovery of a duplicate table, by its actual source table name."""
    try:
        return True, run.table_columns(table_name, custom_config)
    except Exception as e:
        logger.error(f"Failed to get columns for duplicate table {yaml_table_key}: {str(e)}")
        if not run.ctx.skip_errors:
            raise
        return False, None


def _add_duplicate_table_entity(run, pair, props, document_key, yaml_table_key):
    """Build the SingleTable entity of a duplicate table, with the same logic as the first loop."""
    ctx, custom_config, table_name = run.ctx, pair.custom_config, pair.table_name
    try:
        columns_def, keys, has_column_mappings, is_column_whitelist, _discovery_type_by_source, relations = \
            _assemble_source_columns(pair.columns, custom_config, yaml_table_key, run.source_schema,
                                     grouped=run.grouped)
    except EntityMappingError as error:
        run.refuse_table(yaml_table_key, error)
        return

    if not keys:
        logger.warning(f"No keys specified for duplicate table {yaml_table_key}")

    # Generate table IDs
    pair.source_table_id = _resolve_table_id(run.source_schema, table_name, run.source_tables_lookup, _SOURCE_ROLE)
    pair.target_table_id = _resolve_table_id(ctx.yaml_target_schema, pair.target_table_name,
                                             run.target_tables_lookup, _TARGET_ROLE)

    # Process filter clauses using target_table_id (filters live in the target entityType)
    processed_filters, processed_snapshot_delete_filters = _process_filters(
        run, custom_config, pair.columns, pair.target_table_id, f"duplicate {yaml_table_key}")

    partition_settings = None
    if props.partition_column:
        partition_settings = _build_partition_settings(props.partition_column, pair.columns.get("columns"),
                                                       run.source_schema, table_name, pair.source_table_id)

    source_entity = _source_entity(run, pair, columns_def, props.source, verbose=False)
    target_entity_type = _target_entity_type(props.target, processed_filters, processed_snapshot_delete_filters,
                                             props.udf_config)

    try:
        side_pair = _TablePair(yaml_table_key, pair.target_table_name, pair.columns, custom_config,
                               pair.source_table_id, pair.target_table_id, pair.is_unlocked_schema)
        target_columns_def, _target_keys = _assemble_target_side(
            ctx, side_pair, columns_def, has_column_mappings or is_column_whitelist, _discovery_type_by_source,
            target_entity_type, relations=relations, grouped=run.grouped,
        )
    except EntityMappingError as error:
        run.refuse_table(yaml_table_key, error)
        return

    target_table_properties = {}
    if document_key:
        target_table_properties["documentKey"] = document_key

    table_obj = {"id": str(pair.target_table_id), "name": pair.target_table_name, "schema": ctx.yaml_target_schema}
    target_entity = _target_entity(run, pair, target_columns_def, props.target, target_entity_type, table_obj,
                                   target_table_properties, verbose=False)

    # Get entity name from custom config
    entity_name = custom_config.get('entityName', f"{pair.target_table_name} - {run.source_schema}.{table_name}")

    entity = _entity_payload(entity_name, source_entity, target_entity, _requested_group_id(run, custom_config))
    run.entities.append(entity)
    logger.info(f"Created entity for duplicate table {yaml_table_key}: {entity_name}")

    run.queue_partition_request(entity["entityName"], partition_settings, props.partition_max)

    # Process UDFs: 'udf' was popped from props.target, so read the table's own config as the first loop does
    table_udfs = custom_config.get('customProperties', {}).get('target', {}).get('udf', [])
    if table_udfs:
        _define_table_udfs(run, table_name, table_udfs, f"duplicate table {yaml_table_key}")

    # Mark as processed
    run.processed_yaml_keys.add(yaml_table_key)


def _process_duplicate_table(run, yaml_table_key, custom_config, unprocessed_count):
    """The second loop of :func:`create_entities`: a YAML table (``@@`` duplicate) the first loop did not process.

    Returns the number of duplicate tables processed so far.
    """
    table_name = _duplicate_table_label(run, yaml_table_key, custom_config)
    if table_name is None:
        return unprocessed_count

    unprocessed_count += 1
    logger.info(f"Processing duplicate table #{unprocessed_count}: {yaml_table_key} (source table: {table_name})")

    discovered, columns = _duplicate_table_columns(run, yaml_table_key, table_name, custom_config)
    if not discovered:
        return unprocessed_count

    props = _table_properties(run, custom_config, yaml_table_key, duplicate=True)
    if props is None:
        return unprocessed_count

    target_table_name = custom_config.get('name', table_name)
    logger.info(f"Using target table name: {target_table_name} for duplicate table {yaml_table_key}")

    # Process document key if exists
    document_key = _document_key(run, custom_config, columns, yaml_table_key, duplicate=True)

    pair = _TablePair(table_name, target_table_name, columns, custom_config, None, None, props.is_unlocked_schema)
    _add_duplicate_table_entity(run, pair, props, document_key, yaml_table_key)
    return unprocessed_count


class _ChainBuild:
    """The MultiTable entity of one chain being built: its source and target tables, columns and keys.

    Table headers and lists alternate in the ``columns`` and ``keys`` arrays: [header, [col, ...], header, ...].
    """

    def __init__(self, run, chain_id):
        self.run = run
        self.chain_id = chain_id
        # Gluesync 2.3: the grouped entries and the field functions of each table pair, and why the chain is refused.
        self.mt_entries = []
        self.mt_field_functions = []
        self.chain_refusals = []
        self.multi_tables = []
        self.multi_columns = []
        self.multi_keys = []
        self.tables_properties = {}
        # The source columns of each table, whose key flags the 2.3 key rules of its pair set (BOP-16 A).
        self.chain_source_columns = {}
        self.target_tables = []
        self.target_tables_properties = {}
        self.target_columns = []
        self.target_keys = []


def _actual_table_name(table_key, table_data):
    """The source table of a chain table: its sourceTableName (duplicate tables with unique YAML keys), else the key."""
    return table_data.get('sourceTableName', table_key) if table_data else table_key


def _chain_table_ref(table_id, table_key, schema):
    """The ``table`` reference of a MultiTable key, or the header of a table in the keys array."""
    return {
        "id": str(table_id),
        "name": table_key,
        "schema": schema
    }


def _match_column_tracking(run, columns, key_name):
    """The discovered column spelt ``key_name``, else the only one spelt so case-insensitively, remembering the last
    column looked at."""
    found = find_by_name(columns["columns"], key_name)
    if found is not None:
        run.last_col = found
    elif columns["columns"]:
        run.last_col = columns["columns"][-1]
    return found


def _chain_source_columns(run, columns, table_data):
    """The source columns of a chain table: every discovered column."""
    table_columns = []
    names = ColumnNames(c["name"] for c in columns["columns"])
    for col in columns["columns"]:
        run.last_col = col
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, run.last_table_name)
        table_columns.append(_source_column_def(col, col_id, col["name"], col["name"],
                                                col.get("isPK", False) or _is_in_keys(col["name"], table_data, names)))
    return table_columns


def _chain_key_spec(key_def):
    """``(key_name, key_alias, key_type)`` of a YAML key: a key name, or a dict with name/alias."""
    if not isinstance(key_def, dict):
        return key_def, key_def, None
    key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
    key_config = key_def.get(key_name, {}) if isinstance(key_def.get(key_name), dict) else {}

    key_name = key_name or key_config.get('name')
    key_alias = key_config.get('name', key_name)
    return key_name, key_alias, key_config.get('dataType')


def _chain_source_key(run, columns, key_def, table_columns, table_key, source_table_id):
    """The source key of a chain table a YAML key names, found in discovery, else in its columns."""
    key_name, key_alias, key_type = _chain_key_spec(key_def)
    source_schema = run.source_schema

    # Try to find the key in the columns to get its type and ID if not specified (case-insensitive)
    col = _match_column_tracking(run, columns, key_name)
    if col is not None:
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, table_key, label=key_name)
        return _enrich_column({
            "id": col_id,  # Use actual ordinal position from database
            "position": col.get("position", 0),
            "name": col["name"],
            "alias": key_alias if key_alias.lower() != key_name.lower() else col["name"],
            "table": _chain_table_ref(source_table_id, table_key, source_schema),
            "type": key_type or col.get("dataType")
        }, col)

    logger.warning(f"Warning: Key '{key_name}' not found in columns for table '{table_key}'. Searching in table_columns...")
    col_def = find_by_name(table_columns, key_name)
    if col_def is not None:
        key_entry = copy.deepcopy(col_def)
        key_entry['table'] = _chain_table_ref(source_table_id, table_key, source_schema)
        return key_entry

    error_msg = f"CRITICAL ERROR: Key '{key_name}' not found in discovery or table_columns for MultiTable component '{table_key}' in schema '{source_schema}'."
    logger.error(error_msg)
    raise ValueError(error_msg)


def _chain_source_keys(run, columns, table_data, table_columns, table_key, source_table_id):
    """The source keys of a chain table: the YAML ``keys:`` when given, else discovery's primary key."""
    if table_data and 'keys' in table_data and table_data['keys'] is not None:
        return [_chain_source_key(run, columns, key_def, table_columns, table_key, source_table_id)
                for key_def in table_data['keys']]
    keys = []
    for col in columns["columns"]:
        run.last_col = col
        if _coerce_is_pk(col.get("isPK")):
            # Use the id field from CoreHub API as the column ID
            col_id = col.get('id')
            if col_id is None:
                error_msg = f"CRITICAL ERROR: Primary key column '{col.get('name')}' in table {table_key} is missing 'id' field in CoreHub API response.{_MISSING_ID_DETAIL}"
                logger.error(error_msg)
                raise ValueError(error_msg)

            keys.append(_enrich_column({
                "id": col_id,  # Use actual ordinal position from database
                "position": col.get("position", 0),
                "name": col["name"],
                "alias": col["name"],
                "table": _chain_table_ref(source_table_id, table_key, run.source_schema),
                "type": col.get("dataType")
            }, col))
    return keys


def _add_chain_source_table(cb, table_key, table_data):
    """Add a chain table to the MultiTable source: its table, tablesProperties, columns and keys."""
    run = cb.run
    source_schema = run.source_schema
    actual_table_name = _actual_table_name(table_key, table_data)

    source_table_id = _resolve_table_id(source_schema, actual_table_name, run.source_tables_lookup, _SOURCE_ROLE)
    # Get columns for this table using the actual table name
    columns = run.table_columns(actual_table_name, table_data)

    # Add table to the list (use actual table name, not YAML key)
    cb.multi_tables.append({
        "id": int(source_table_id),
        "name": actual_table_name,
        "schema": source_schema
    })
    run.chain_source_ids[table_key] = source_table_id

    # Add table to tables_properties with optional whereClause, under the actual table name
    source_table_key = f"{source_schema}.{actual_table_name}"
    cb.tables_properties[source_table_key] = _where_clause_properties(table_data, source_table_key)

    table_columns = _chain_source_columns(run, columns, table_data)

    # Add table header metadata followed by the columns for this table
    cb.multi_columns.append({
        "id": int(source_table_id),
        "name": table_key,
        "schema": source_schema
    })
    cb.multi_columns.append(table_columns)
    cb.chain_source_columns[table_key] = table_columns

    keys = _chain_source_keys(run, columns, table_data, table_columns, table_key, source_table_id)

    # Apply the charSet declared in the YAML on this component's source columns.
    # table_columns is already referenced by multi_columns, so patching it in place
    # is enough.
    names = ColumnNames(c["name"] for c in columns["columns"])
    _apply_char_set_overrides(
        table_columns,
        _build_char_set_overrides((table_data or {}).get('columns', []), names),
        table_key,
        names
    )

    # Add keys for this table
    cb.multi_keys.append(_enrich_column(_chain_table_ref(source_table_id, table_key, source_schema), run.last_col))
    cb.multi_keys.append(keys)


def _chain_source_entity(cb, entity_name, last_table_data):
    """The source agent entity of a MultiTable entity."""
    source_custom_properties = last_table_data.get('customProperties', {}).get('source', {})
    return {
        "type": "MultiTable",
        "entityId": "",
        "entityName": entity_name,
        "agentEntityId": "",
        "entityType": {
            "type": "Source",
            "maxFetchItemsCountPerIteration": source_custom_properties.get('maxItemsCountPerIteration', 1000),
            "maxTransactionMessageKbSize": source_custom_properties.get('maxTransactionMessageKbSize', 1024),
            "pollingIntervalMilliseconds": source_custom_properties.get('pollingIntervalMilliseconds', 100),
            "unchangedDataFilterType": "ENTIRE_ROW"
        },
        "agentId": cb.run.source_agent_id,
        "orderIndex": 0,
        "customProperties": {},
        "tablesProperties": cb.tables_properties,
        "tables": cb.multi_tables,
        "columns": cb.multi_columns
    }


def _chain_target_discovery(run, pair_target_name):
    """The discovered columns of a chain target table (for the type-mapping fallback), None when it has none."""
    ctx = run.ctx
    try:
        return get_table_columns(ctx.token, ctx.pipeline_id, ctx.target_agent_id, run.target_schema, pair_target_name)
    except Exception as e:
        logger.debug(f"Could not get target columns for {run.target_schema}.{pair_target_name}: {str(e)}")
        return None


def _grouped_chain_pair_columns(cb, pair):
    """Gluesync 2.3: the pair goes through the SingleTable assembly, so its keys follow the R12 rules and it takes
    every 2.3 form (BOP-16 A).

    On 2.3 it replaces the target columns built from the YAML mapping, the refusal of the 2.3 forms in a chain and
    the 1:1 entries of every mapped column. Returns ``(target_table_columns, pair_relations, max_target_col_id)``.
    """
    run = cb.run
    try:
        target_table_columns, pair_relations, pair_field_functions, source_key_names = \
            _assemble_chain_pair(run.ctx, pair, run.source_schema)
    except EntityMappingError as error:
        cb.chain_refusals.append(str(error))
        target_table_columns, pair_relations, pair_field_functions, source_key_names = [], [], [], set()
    cb.mt_field_functions.extend(pair_field_functions)
    for col in cb.chain_source_columns[pair.table_name]:
        run.last_col = col
        col["isPK"] = col["name"] in source_key_names
    max_target_col_id = max([c["id"] for c in target_table_columns if isinstance(c.get("id"), int)] + [0])
    return target_table_columns, pair_relations, max_target_col_id


def _chain_source_to_target_mapping(table_has_mappings, table_has_whitelist, table_relations, table_yaml_columns,
                                    names):
    """Lookup from source column name -> (target_name, yaml_type) of a chain table's YAML columns.

    ``names``: the :class:`entity_mapping.ColumnNames` of the discovered source columns, which the lookup is by."""
    source_to_target_mapping = {}
    if table_has_mappings:
        for relation in table_relations or []:
            for source_name, target_name in _relation_pairs([relation]):
                source_to_target_mapping[names.key(source_name)] = {
                    'target_name': target_name,
                    'yaml_type': relation["row"].get('type')
                }
    elif table_has_whitelist:
        for yaml_col in table_yaml_columns:
            source_name = yaml_col.get('sourceName') or yaml_col.get('name')
            target_name = yaml_col.get('name')
            if source_name:
                source_to_target_mapping[names.key(source_name)] = {
                    'target_name': target_name,
                    'yaml_type': yaml_col.get('type')
                }
    return source_to_target_mapping


def _legacy_chain_target_column(run, col, col_id, mapping_info, table_data, overrides, column_types, names):
    """A target column of a chain table pair for a CoreHub before Gluesync 2.3."""
    # Use target name from mapping if available, otherwise source name
    target_name = mapping_info['target_name'] if mapping_info else col["name"]
    _resolved_dt = overrides.get(col["name"]) or _map_target_type(run.ctx, col.get("dataType"), column_types)
    return _enrich_column({
        "id": col_id,  # Use actual ordinal position from database
        "position": col.get("position", 0),
        "name": target_name,
        "dataType": _resolved_dt,
        "isPK": col.get("isPK", False) or _is_in_keys(col["name"], table_data, names), "isIdentity": col.get("isIdentity", False), "isNullable": col.get("isNullable", False)
    }, col)


def _legacy_chain_pair_columns(cb, table_key, table_data, columns, overrides, column_types):
    """The target columns of a chain table pair for a CoreHub before Gluesync 2.3, from its YAML mapping.

    Returns ``(target_table_columns, source_to_target_mapping, max_target_col_id)``.
    """
    run = cb.run
    target_table_columns = []
    max_target_col_id = 0

    # Check if this table has column mappings or whitelist
    table_yaml_columns = table_data.get('columns', []) if table_data else []
    table_has_mappings = _is_column_mappings(table_yaml_columns)
    table_has_whitelist = _is_column_whitelist(table_yaml_columns)

    try:
        table_relations = yaml_relations(table_yaml_columns, label=table_key)
    except EntityMappingError as error:
        cb.chain_refusals.append(str(error))
        table_relations = None

    names = ColumnNames(c["name"] for c in columns["columns"])
    source_to_target_mapping = _chain_source_to_target_mapping(table_has_mappings, table_has_whitelist,
                                                               table_relations, table_yaml_columns, names)

    has_filter = table_has_mappings or table_has_whitelist
    try:
        _refuse_forms_of_gluesync_23(table_relations, table_data, table_key, names)
    except EntityMappingError as error:
        cb.chain_refusals.append(str(error))

    # First add source columns mapped to target with their actual ordinal positions
    for col in columns["columns"]:
        run.last_col = col
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, run.last_table_name)

        # When filter is active, skip columns not in the whitelist/mapping
        mapping_info = source_to_target_mapping.get(col["name"])
        if has_filter and not mapping_info:
            logger.debug(f"Skipping column {col['name']} for MultiTable target: not in whitelist/mapping for table {table_key}")
            continue

        max_target_col_id = max(max_target_col_id, col_id)
        target_table_columns.append(_legacy_chain_target_column(run, col, col_id, mapping_info, table_data,
                                                                overrides, column_types, names))
    return target_table_columns, source_to_target_mapping, max_target_col_id


def _add_chain_target_only_columns(cb, table_key, table_data, discovered, target_table_columns, max_target_col_id,
                                   column_types):
    """Add the target-only columns of a chain table, if specified; returns them as the YAML declares them."""
    run = cb.run
    table_custom_config = table_data.get('customProperties', {}).get('target', {}) if table_data else {}
    table_target_only_columns = table_custom_config.get(_TARGET_ONLY_COLUMNS_KEY, [])
    if not table_target_only_columns:
        return table_target_only_columns

    logger.info(f"Adding {len(table_target_only_columns)} target-only columns for table {table_key} in MultiTable entity")
    # Reuse table_target_columns already fetched above for type-mapping fallback
    if discovered and 'columns' in discovered:
        logger.debug(f"Reusing {len(discovered.get('columns', []))} existing columns in target table {run.target_schema}.{table_key}")

    for target_col in table_target_only_columns:
        # Increment from the maximum target column ID
        max_target_col_id += 1
        col_name, col_type, col_data_length, col_numeric_precision, col_numeric_scale, col_is_nullable = \
            _target_only_properties(target_col, discovered, run.ctx.skip_errors, where=f" in table '{table_key}'")

        # Map the column type to target node type
        mapped_type = _map_target_type(run.ctx, col_type, column_types)

        target_table_columns.append(_enrich_column({
            "id": max_target_col_id,  # Continue from last target column ID
            "position": 0,
            "name": col_name,
            "type": mapped_type,
            "charMaxLength": col_data_length,
            "numPrec": col_numeric_precision,
            "numScale": col_numeric_scale,
            "isNullable": col_is_nullable, "isIdentity": False,
            "isPK": False  # Target-only columns are not primary keys
        }, run.last_col))
        logger.debug(f"Added target-only column to MultiTable: {col_name} (type: {mapped_type}, id: {max_target_col_id})")
    return table_target_only_columns


def _add_grouped_chain_entries(cb, pair, table_target_only_columns, target_table_columns, pair_relations):
    """Every column of a 2.3 pair in one entry, with the field function of each target-only column.

    The pair's relations, every discovered source column the MultiTable source holds (a filtered one read and
    never written, BOP-17.4), and each target-only column fed by no source, which needs a field function that
    produces it (P12, BOP-12 A).
    """
    run, table_key = cb.run, pair.table_name
    target_ids_by_name = {c["name"]: c["id"] for c in target_table_columns}
    for target_col in table_target_only_columns:
        col_name = target_col if isinstance(target_col, str) else target_col.get('name')
        expression = normalize_expression_for_corehub(target_col.get('expression')) \
            if isinstance(target_col, dict) else None
        if not expression:
            cb.chain_refusals.append(
                f"target-only column {col_name} of {table_key} is fed by no source column and has no "
                f"expression: a Gluesync 2.3 CoreHub needs a field function that produces it (P12)"
            )
            continue
        cb.mt_field_functions.append({
            "columnId": target_ids_by_name.get(col_name),
            "tableOrObjectId": int(pair.target_table_id),
            "expression": expression,
        })
    try:
        cb.mt_entries.extend(build_entries(
            run.chain_source_ids[table_key], pair.target_table_id, pair_relations, pair.columns["columns"],
            target_table_columns, grouped=True, label=table_key,
        ))
    except EntityMappingError as error:
        cb.chain_refusals.append(str(error))


def _legacy_chain_target_key(cb, key_def, columns, table_key, source_to_target_mapping, target_table_columns,
                             overrides, column_types):
    """The target key of a chain table a YAML key names, for a CoreHub before Gluesync 2.3."""
    run = cb.run
    if isinstance(key_def, dict):
        key_name = next(iter(key_def)) if not key_def.get('name') else key_def['name']
    else:
        key_name = key_def

    # Try to find the key in the columns to get its type and ID (case-insensitive)
    col = _match_column_tracking(run, columns, key_name)
    if col is not None:
        # Use the id field from CoreHub API as the column ID
        col_id = _require_column_id(col, table_key, label=key_name)

        # Use target name from mapping if available, otherwise source name
        key_mapping_info = source_to_target_mapping.get(col["name"])
        target_key_name = key_mapping_info['target_name'] if key_mapping_info else col["name"]

        _resolved_dt = overrides.get(col["name"]) or _map_target_type(run.ctx, col.get("dataType"), column_types)
        return _enrich_column({
            "id": col_id,  # Use actual ordinal position from database
            "position": col.get("position", 0),
            "name": target_key_name,
            "type": _resolved_dt
        }, col)

    logger.warning(f"Warning: Target key '{key_name}' not found in columns for table '{table_key}'. Searching in target_table_columns...")
    col_def = find_by_name(target_table_columns, key_name)
    if col_def is not None:
        return copy.deepcopy(col_def)

    error_msg = f"CRITICAL ERROR: Target key '{key_name}' not found in discovery or target_table_columns for MultiTable component '{table_key}' in schema '{run.ctx.yaml_target_schema}'."
    logger.error(error_msg)
    raise ValueError(error_msg)


def _legacy_chain_target_pk_keys(run, columns, overrides, column_types):
    """The target keys of the source columns of a chain table discovery flags as primary key."""
    keys = []
    for col in columns["columns"]:
        run.last_col = col
        if _coerce_is_pk(col.get("isPK")):
            # Use ordinalPosition from API if available
            col_id = col.get('position', col.get('id'))
            if col_id is None:
                # Fallback to finding position if not provided
                col_id = next((i for i, c in enumerate(columns["columns"], 1) if c == col), 1)

            _resolved_dt = overrides.get(col["name"]) or _map_target_type(run.ctx, col.get("dataType"), column_types)
            keys.append(_enrich_column({
                "id": col_id,  # Use actual ordinal position from database
                "position": col.get("position", 0),
                "name": col["name"],
                "type": _resolved_dt
            }, col))
    return keys


def _add_chain_target_table(cb, table_key, table_data):
    """Add a chain table to the MultiTable target: its table, tablesProperties, columns and keys."""
    run = cb.run
    target_schema = run.target_schema
    actual_table_name = _actual_table_name(table_key, table_data)
    # On 2.3 the target table is the one the YAML `name` gives, which the first loop creates and the export
    # writes; older CoreHubs keep the YAML key (BOP-19 B).
    pair_target_name = _target_table_name(table_data or {}, actual_table_name, table_key) if run.grouped else table_key

    target_table_id = _resolve_table_id(run.ctx.yaml_target_schema, pair_target_name, run.target_tables_lookup,
                                        _TARGET_ROLE)
    cb.target_tables.append({
        "id": int(target_table_id),
        "name": pair_target_name,
        "schema": target_schema
    })
    run.chain_target_ids[table_key] = target_table_id

    # Add table to tables_properties
    cb.target_tables_properties[f"{target_schema}.{pair_target_name}"] = {}

    # Get columns for this table using actual source table name
    columns = run.table_columns(actual_table_name, table_data)

    # Discover target columns for this specific table to enable type-mapping fallback
    table_target_columns = _chain_target_discovery(run, pair_target_name)
    target_table_column_types = extract_target_column_types(table_target_columns)

    # Per-table user-declared targetDataType overrides (by source column name)
    overrides = _build_target_data_type_overrides(table_data.get('columns', []) if table_data else [],
                                                  ColumnNames(c["name"] for c in columns["columns"]))

    pair = _TablePair(table_key, pair_target_name, columns, table_data or {}, run.chain_source_ids[table_key],
                      target_table_id)
    pair_relations, source_to_target_mapping = None, None
    if run.grouped:
        target_table_columns, pair_relations, max_target_col_id = _grouped_chain_pair_columns(cb, pair)
    else:
        target_table_columns, source_to_target_mapping, max_target_col_id = _legacy_chain_pair_columns(
            cb, table_key, table_data, columns, overrides, target_table_column_types)

    table_target_only_columns = _add_chain_target_only_columns(
        cb, table_key, table_data, table_target_columns, target_table_columns, max_target_col_id,
        target_table_column_types)

    if run.grouped:
        _add_grouped_chain_entries(cb, pair, table_target_only_columns, target_table_columns, pair_relations)

    # Add columns for this table
    cb.target_columns.append({
        "id": int(target_table_id),
        "name": pair_target_name,
        "schema": target_schema
    })
    cb.target_columns.append(target_table_columns)

    # The target keys of a 2.3 pair are its assembled target columns flagged isPK (above).
    if run.grouped:
        keys = [col for col in target_table_columns if col.get("isPK")]
    elif table_data and 'keys' in table_data and table_data['keys'] is not None:
        # Process keys for target with IDs
        keys = [_legacy_chain_target_key(cb, key_def, columns, table_key, source_to_target_mapping,
                                         target_table_columns, overrides, target_table_column_types)
                for key_def in table_data['keys']]
    else:
        keys = _legacy_chain_target_pk_keys(run, columns, overrides, target_table_column_types)

    # Add keys for this table
    cb.target_keys.append(_enrich_column(_chain_table_ref(target_table_id, table_key, target_schema), run.last_col))
    cb.target_keys.append(keys)


def _legacy_chain_matrix(run, tables_list):
    """The table mapping matrix of a MultiTable entity for a CoreHub before Gluesync 2.3: one pair per column."""
    columns_mapping_matrix = []
    for table_key, table_data in tables_list:
        actual_table_name = _actual_table_name(table_key, table_data)

        source_table_id = run.chain_source_ids.get(table_key) or _resolve_table_id(
            run.source_schema, actual_table_name, run.source_tables_lookup, _SOURCE_ROLE
        )
        target_table_id = run.chain_target_ids.get(table_key) or _resolve_table_id(
            run.ctx.yaml_target_schema, table_key, run.target_tables_lookup, _TARGET_ROLE
        )
        logger.info(f"Table mapping for {table_key}: source_id={source_table_id}, target_id={target_table_id}")

        # Get columns for this table to create mappings for each column (use actual table name)
        columns = run.table_columns(actual_table_name, table_data)
        if not (columns and 'columns' in columns):
            continue
        for col in columns['columns']:
            run.last_col = col
            # Use the id field from CoreHub API as the column ID
            col_id = _require_column_id(col, table_key)
            columns_mapping_matrix.append({
                "sourceTableObjectId": source_table_id,
                "targetTableObjectId": target_table_id,
                "sourceColumnId": col_id,
                "targetColumnId": col_id
            })
    return columns_mapping_matrix


def _chain_target_custom_properties(last_table_data):
    """The customProperties of a MultiTable target: ttlValue and the pre/post snapshot commands."""
    table_target_properties = last_table_data.get('customProperties', {}).get('target', {})
    target_custom_properties = {
        "ttlValue": table_target_properties.get('ttlValue', 0)
    }

    post_snapshot_cmd = last_table_data.get('customProperties', {}).get('target', {}).get('postSnapshotCommand', {})
    if isinstance(post_snapshot_cmd, dict) and post_snapshot_cmd:
        target_custom_properties["postSnapshotCommand"] = post_snapshot_cmd

    pre_snapshot_cmd = last_table_data.get('customProperties', {}).get('target', {}).get('preSnapshotCommand', {})
    if isinstance(pre_snapshot_cmd, dict) and pre_snapshot_cmd:
        target_custom_properties["preSnapshotCommand"] = pre_snapshot_cmd
    return target_custom_properties


def _chain_target_entity(cb, entity_name, tables_list, last_table_data):
    """The target agent entity of a MultiTable entity."""
    run = cb.run
    # Get allowed operations and other target settings
    mt_target_props = last_table_data.get('customProperties', {}).get('target', {}) if last_table_data else {}
    allowed_operations = get_allowed_operations(mt_target_props)

    # Create table mapping matrix for MultiTable entities
    if run.grouped:
        columns_mapping_matrix = encode_entries(cb.mt_entries, grouped=True)
    else:
        columns_mapping_matrix = _legacy_chain_matrix(run, tables_list)

    target_entity = {
        "type": "MultiTable",
        "entityId": "",
        "entityName": entity_name,
        "agentEntityId": "",
        "entityType": {
            "type": "Target",
            "allowedOperations": allowed_operations,
            "snapshotWritingConcurrency": mt_target_props.get('snapshotWritingConcurrency', 1),
            "columnsMappingMatrix": columns_mapping_matrix,
            "tablesWithUnlockedSchema": [],
            "tablesWithUnlockedDataTypes": [],
            "useBulkOperationsDuringCDC": mt_target_props.get('useBulkOperationsDuringCDC', False),
            "useBulkOperationsWhileSnapshot": mt_target_props.get('useBulkOperationsWhileSnapshot', False)
        },
        "agentId": run.ctx.target_agent_id,
        "orderIndex": 0,
        "customProperties": _chain_target_custom_properties(last_table_data),
        "tablesProperties": cb.target_tables_properties,
        "tables": cb.target_tables,
        "columns": cb.target_columns
    }

    if cb.mt_field_functions:
        target_entity["entityType"]["fieldFunctions"] = cb.mt_field_functions
    return target_entity


def _chain_tables_in_whitelist(run, chain_id, tables_list):
    """FILTER: only the chain's tables that are in the schema's whitelist; None when it has none."""
    whitelist = run.schema.whitelist
    if not whitelist:
        return tables_list
    filtered_tables_list = [(table_key, table_data) for table_key, table_data in tables_list if table_key in whitelist]
    if not filtered_tables_list:
        logger.info(f"Skipping chain '{chain_id}' - no tables in whitelist for schema '{run.source_schema}'")
        return None
    if len(filtered_tables_list) != len(tables_list):
        skipped_tables = [tk for tk, _ in tables_list if tk not in whitelist]
        logger.info(f"For chain '{chain_id}', filtered out tables not in whitelist for schema '{run.source_schema}': {skipped_tables}")
        logger.info(f"Remaining tables in chain: {[tk for tk, _ in filtered_tables_list]}")
    return filtered_tables_list


def _build_multi_table_entity(run, chain_id, tables_list):
    """The MultiTable entity of a chain (``{"entities": [...]}``), None when the chain gets none."""
    if not tables_list:
        return None
    tables_list = _chain_tables_in_whitelist(run, chain_id, tables_list)
    if tables_list is None:
        return None

    # Tables are processed in the order they appear in the YAML file
    # Log table ordering for debugging
    logger.info(f"Creating MultiTable entity for chainId: {chain_id} with {len(tables_list)} tables in YAML order:")
    for idx, (table_key, _) in enumerate(tables_list):
        logger.info(f"  {idx+1}. Table {table_key}")

    # We'll reuse the entity name captured during export if available
    first_table_key, first_table_data = tables_list[0]
    entity_name = first_table_data.get("entityName") or f"{run.source_schema}.{first_table_key}"

    cb = _ChainBuild(run, chain_id)
    # Process each table in the chain (in YAML order)
    for table_key, table_data in tables_list:
        _add_chain_source_table(cb, table_key, table_data)
    # The chain settings are read from its last table, as they always have been.
    last_table_data = tables_list[-1][1]
    source_entity = _chain_source_entity(cb, entity_name, last_table_data)

    # Process each table for the target (in YAML order)
    for table_key, table_data in tables_list:
        _add_chain_target_table(cb, table_key, table_data)
    target_entity = _chain_target_entity(cb, entity_name, tables_list, last_table_data)

    # Get group info for multi-table entity
    group_id = run.group_id_map.get(last_table_data.get('groupId', _DEFAULT_GROUP), last_table_data.get('groupId', _DEFAULT_GROUP))
    group_name = next((name for name, gid in run.group_id_map.items() if gid == group_id), group_id)
    logger.info(f"Assigning multi-table entity '{entity_name}' to group: {group_name} (ID: {group_id})")

    # Create the MultiTable entity
    entity_payload = {
        "entityId": "",
        "entityName": entity_name,
        "agentEntities": [source_entity, target_entity],
    }

    # Only add groupId if it's not the default
    if group_id and group_id != _DEFAULT_GROUP:
        entity_payload["groupId"] = group_id

    if cb.chain_refusals:
        run.refuse_table(f"MultiTable chain {chain_id}", EntityMappingError("; ".join(cb.chain_refusals)))
        return None
    return {"entities": [entity_payload]}


def _log_tables_order(entity_name, agent_entities):
    """Log the final tables ordering in the payload of each agent entity before sending."""
    for idx, agent_entity in enumerate(agent_entities):
        agent_type = "source" if idx == 0 else "target"
        if 'tables' in agent_entity:
            logger.info(f"Final tables order in payload for {entity_name} ({agent_type}):")
            for table_idx, table in enumerate(agent_entity.get('tables', [])):
                logger.info(f"  {table_idx+1}. {table.get('schema')}.{table.get('name')}")


def _strip_multi_table_columns(multi_entity):
    """Clean up a MultiTable payload before sending: strip 'alias' and inject 'tableId' into columns.

    MultiTable 'columns' is an alternating list: [header_dict, [col, col, ...], header_dict, [col, ...], ...]
    """
    for entity_payload in multi_entity.get('entities', []):
        for agent_entity in entity_payload.get('agentEntities', []):
            current_table_id = 0
            for item in agent_entity.get('columns', []):
                if isinstance(item, dict):
                    # Table header: capture the table id for the columns that follow
                    current_table_id = _header_table_id(item)
                elif isinstance(item, list):
                    _strip_columns([col for col in item if isinstance(col, dict)], current_table_id)


def _header_table_id(header):
    """The table id of a MultiTable table header, 0 when it is not a number."""
    try:
        return int(header.get('id', 0))
    except (TypeError, ValueError):
        return 0


def _strip_columns(columns, table_id):
    """Strip 'alias' from columns and inject their 'tableId' (when known)."""
    for col in columns:
        col.pop('alias', None)
        if table_id:
            col['tableId'] = table_id


def _strip_single_table_columns(chunk_data):
    """Clean up a chunk of entities before sending: strip 'alias' and inject 'tableId' into columns."""
    for entity_payload in chunk_data.get('entities', []):
        for agent_entity in entity_payload.get('agentEntities', []):
            # Handle SingleTable (columns are at agent_entity level)
            _strip_table_item_columns(agent_entity)

            # Handle MultiTable/NoSql (columns are inside tables array)
            for table_item in agent_entity.get('tables', []):
                _strip_table_item_columns(table_item)


def _strip_table_item_columns(item):
    """Strip the columns of an item holding its ``table`` and ``columns`` (see :func:`_strip_columns`)."""
    if 'table' in item and 'columns' in item:
        _strip_columns(item['columns'], int(item['table'].get('id', 0)))


def _put_entities(ctx, body):
    """Save entities on CoreHub."""
    return fetch_core_hub(
        _ENTITIES_CONFIG_PATH.format(pipeline_id=ctx.pipeline_id),
        method="PUT",
        token=ctx.token,
        body=body
    )


def _send_multi_table_entity(run, index, total_multi_tables, multi_entity):
    """Create one MultiTable entity; whether it was created."""
    ctx = run.ctx
    try:
        entity_name = multi_entity['entities'][0]['entityName']
        logger.info(f"Creating MultiTable entity {index+1}/{total_multi_tables}: {entity_name}")

        # Log entities being sent
        _log_tables_order(entity_name, multi_entity['entities'][0].get('agentEntities', []))

        _strip_multi_table_columns(multi_entity)

        logger.debug(f"MultiTable payload sent to /pipelines/{ctx.pipeline_id}/config/entities:")
        logger.debug(json.dumps(multi_entity, indent=2))

        _put_entities(ctx, multi_entity)
        logger.info(f"Successfully created MultiTable entity {index+1}/{total_multi_tables}")
        return True
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error creating MultiTable entity {index+1}/{total_multi_tables}: {error_msg}")
        run.errors.append(f"{multi_entity['entities'][0].get('entityName', _UNKNOWN_ENTITY)}: {error_msg}")
        if not ctx.skip_errors:
            raise
        logger.warning("Skipping MultiTable entity due to skip_errors=True")
        return False


def _send_multi_table_entities(run, multi_table_entities):
    """Create the MultiTable entities first; returns ``(successful, failed, total)``."""
    successful_multi_tables = 0
    failed_multi_tables = 0
    total_multi_tables = len(multi_table_entities)

    if total_multi_tables > 0:
        logger.info(f"Processing {total_multi_tables} MultiTable entities...")

        for i, multi_entity in enumerate(multi_table_entities):
            if _send_multi_table_entity(run, i, total_multi_tables, multi_entity):
                successful_multi_tables += 1
            else:
                failed_multi_tables += 1
    return successful_multi_tables, failed_multi_tables, total_multi_tables


def _send_entities_one_by_one(run, chunk):
    """Send the entities of a refused chunk one at a time; returns ``(successful, failed)``."""
    successful_entities = 0
    failed_entities = 0
    for entity_payload in chunk:
        try:
            _put_entities(run.ctx, {"entities": [entity_payload]})
            successful_entities += 1
        except Exception as entity_error:  # pylint: disable=broad-except
            logger.error(f"Entity {entity_payload.get('entityName', _UNKNOWN_ENTITY)} refused by CoreHub: {entity_error}")
            failed_entities += 1
            run.errors.append(f"{entity_payload.get('entityName', _UNKNOWN_ENTITY)}: {entity_error}")
    return successful_entities, failed_entities


def _send_entity_chunk(run, chunk, chunk_start, chunk_end, total_entities):
    """Create one chunk of regular entities; returns ``(successful, failed)``."""
    ctx = run.ctx
    chunk_data = {"entities": chunk}

    logger.info(f"\nProcessing chunk {chunk_start}-{chunk_end} of {total_entities} entities...")

    # Log entities being sent
    for entity_payload in chunk_data.get('entities', []):
        entity_name = entity_payload.get('entityName', _UNKNOWN_ENTITY)
        logger.info(f"Entity: {entity_name}")
        _log_tables_order(entity_name, entity_payload.get('agentEntities', []))

    # Clean up payload before sending
    _strip_single_table_columns(chunk_data)

    try:
        logger.debug(f"Payload sent to /pipelines/{ctx.pipeline_id}/config/entities:")
        logger.debug(json.dumps(chunk_data, indent=2))

        _put_entities(ctx, chunk_data)
        logger.info(f"Successfully created entities {chunk_start}-{chunk_end}")
        return len(chunk), 0
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error creating entities {chunk_start}-{chunk_end}: {error_msg}")
        if not ctx.skip_errors:
            raise
        if _http_status(e) == 400 and len(chunk) > 1:
            # A Gluesync 2.3 CoreHub refuses the whole request at the first entity its mapping stage
            # refuses: send the chunk again one entity at a time, so the valid ones are saved (C-6).
            logger.warning(f"Chunk {chunk_start}-{chunk_end} refused; sending its {len(chunk)} entities one by one")
            return _send_entities_one_by_one(run, chunk)
        run.errors.extend(f"{entity_payload.get('entityName', _UNKNOWN_ENTITY)}: {error_msg}" for entity_payload in chunk)
        logger.warning("Skipping chunk due to skip_errors=True")
        return 0, len(chunk)


def _send_entity_chunks(run, chunk_size):
    """Create the regular entities in chunks; returns ``(total, successful, failed)``."""
    entities = run.entities
    total_entities = len(entities)
    successful_entities = 0
    failed_entities = 0

    if total_entities > 0:
        logger.info(f"Processing {total_entities} regular entities in chunks of {chunk_size}...")

        for i in range(0, total_entities, chunk_size):
            successful, failed = _send_entity_chunk(run, entities[i:i + chunk_size], i + 1,
                                                    min(i + chunk_size, total_entities), total_entities)
            successful_entities += successful
            failed_entities += failed
    return total_entities, successful_entities, failed_entities


def _entity_group_id(run, group_id):
    """The group ID of the group an entity names: a group name mapped to its ID, else the group ID itself."""
    # If group_id is a name, try to get the mapped ID
    if group_id in run.group_id_map:
        mapped_group_id = run.group_id_map[group_id]
        logger.debug(f"Mapped group name '{group_id}' to ID: {mapped_group_id}")
        group_id = mapped_group_id
    return group_id


def _created_entities_by_group(run, response):
    """The created entities of a pipeline listing by the group they belong to."""
    entities_by_group = {}
    if not isinstance(response, list):
        return entities_by_group
    for entity in response:
        if 'entity' not in entity or 'entityId' not in entity['entity']:
            continue
        entity_id = entity['entity']['entityId']
        group_id = entity['entity'].get('groupId', _DEFAULT_GROUP)

        # Skip if no group is specified (will use _default)
        if not group_id or group_id == _DEFAULT_GROUP:
            logger.debug(f"Entity {entity_id} has no group specified, will use _default")
            continue

        entities_by_group.setdefault(_entity_group_id(run, group_id), []).append(entity_id)
    return entities_by_group


def _assign_group(run, group_id, entity_ids):
    """Assign entities to a group (never the default one)."""
    if not entity_ids:  # Only proceed if we have entities to assign
        return
    # Skip if group_id is _default or None
    if not group_id or group_id == _DEFAULT_GROUP:
        logger.debug("Skipping assignment for default group")
        return

    logger.info(f"Assigning {len(entity_ids)} entities to group ID: {group_id}")
    success = assign_entities_to_group(run.ctx.token, run.ctx.pipeline_id, group_id, entity_ids)
    if not success and not run.ctx.skip_errors:
        raise RuntimeError(f"Failed to assign entities to group {group_id}")
    elif success:
        logger.info(f"Successfully assigned {len(entity_ids)} entities to group {group_id}")


def _assign_entities_to_groups(run):
    """Assign the created entities to their groups."""
    try:
        # Get all created entities, grouped by their target group
        response = fetch_core_hub(_ENTITIES_PATH.format(pipeline_id=run.ctx.pipeline_id), token=run.ctx.token)
        entities_by_group = _created_entities_by_group(run, response)

        # Make assignment requests for each group
        for group_id, entity_ids in entities_by_group.items():
            _assign_group(run, group_id, entity_ids)

    except Exception as e:
        logger.error(f"Error during group assignment: {str(e)}")
        if not run.ctx.skip_errors:
            raise


def _group_schedules_declared(group_scheds):
    """Whether a ``group_schedules`` section declares a schedule."""
    if isinstance(group_scheds, dict):
        return any(isinstance(v, list) and v for v in group_scheds.values())
    return isinstance(group_scheds, list) and bool(group_scheds)


def _declares_schedules(schema_config):
    """Whether a schema section declares pipeline-level, group-level or entity-level schedules."""
    # Pipeline-level schedules
    has_schedules = 'schedules' in schema_config
    # Group-level schedules
    if 'group_schedules' in schema_config and _group_schedules_declared(schema_config['group_schedules']):
        has_schedules = True
    # Entity-level schedules
    if 'tables' in schema_config and 'custom' in schema_config['tables'] \
            and any('schedules' in table_data for table_data in schema_config['tables']['custom'].values()):
        has_schedules = True
    return has_schedules


def _schedules_schema_config(yaml_config, source_schema):
    """``(has_schedules, schema_config)``: whether the YAML defines schedules for the current source_schema."""
    if not yaml_config:
        return False, None
    schemas_dict = _yaml_schemas(yaml_config)

    current_schema_config = schemas_dict.get(source_schema)
    if not current_schema_config:
        logger.debug(f"Schema '{source_schema}' not found in schemas_dict keys: {list(schemas_dict.keys())}")
        # As a fallback, try direct lookup
        current_schema_config = yaml_config.get(source_schema, {})

    if not isinstance(current_schema_config, dict):
        return False, current_schema_config
    return _declares_schedules(current_schema_config), current_schema_config


def _entity_ids_by_table(response):
    """The entity ID of each entity of a pipeline listing, by the table name its entity name ends with."""
    entities_map = {}
    for item in response:
        if 'entity' in item and isinstance(item['entity'], dict) \
                and 'entityId' in item['entity'] and 'entityName' in item['entity']:
            entity = item['entity']
            entity_id = entity['entityId']
            entity_name = entity['entityName']
            # Extract table name from entity name (typically schema.table)
            # Split at first dot only to preserve dots inside table names.
            parts = entity_name.split('.', 1)
            table_name = parts[-1] if len(parts) > 1 else entity_name
            entities_map[table_name] = entity_id
            logger.info(f"Found entity: {entity_name} (ID: {entity_id})")
    return entities_map


def _create_entity_level_schedules(run, schema_config, entities_map, chronos_token):
    """Create the schedules of each entity found in the YAML config (only for the current source_schema)."""
    if not ('tables' in schema_config and 'custom' in schema_config['tables']):
        return
    for table_key, table_data in schema_config['tables']['custom'].items():
        # Use the table_key directly instead of looking for 'name' field
        table_name = table_key

        if table_name in entities_map and 'schedules' in table_data:
            entity_id = entities_map[table_name]
            logger.info(f"Creating schedules for table {table_name} (Entity ID: {entity_id})")
            create_entity_schedules(run.ctx.token, run.ctx.pipeline_id, entity_id, table_name,
                                    table_data['schedules'], chronos_token=chronos_token)
        else:
            logger.warning(
                f"Unable to create schedules for {table_name}. Entity not found or no schedules defined.")


def _resolve_group_reference(candidate, known_ids, group_id_map):
    """The group IDs a schedule's group reference names: '*' for every group of the run, a group name or ID."""
    candidate_str = str(candidate).strip()
    if not candidate_str:
        return []
    if candidate_str == '*':
        if known_ids:
            return list(known_ids)
        logger.warning("Wildcard group_ids requested but no groups were created in this run")
        return []
    if candidate_str in group_id_map and group_id_map[candidate_str] and group_id_map[candidate_str] != _DEFAULT_GROUP:
        return [group_id_map[candidate_str]]
    if candidate_str in known_ids:
        return [candidate_str]
    logger.warning(f"Skipping unknown group reference '{candidate_str}' in schedule configuration")
    return []


def _expand_group_ids(raw_ids, known_ids, group_id_map):
    """The group IDs, without duplicates and in order, that the group references of a schedule resolve to."""
    if raw_ids is None:
        return []
    candidates = [raw_ids] if isinstance(raw_ids, str) else list(raw_ids)
    resolved = []
    for candidate in candidates:
        resolved.extend(_resolve_group_reference(candidate, known_ids, group_id_map))
    # Deduplicate while preserving order
    return list(dict.fromkeys(resolved))


def _group_keyed_schedules(group_scheds_cfg):
    """The schedules of a ``group_schedules`` mapping, each for the group it is listed under."""
    base_schedule_list = []
    for grp_key, schedules_cfg in group_scheds_cfg.items():
        if not isinstance(schedules_cfg, list):
            logger.warning(
                f"Skipping group schedule block for '{grp_key}': expected a list of schedule objects"
            )
            continue
        for schedule_cfg in schedules_cfg:
            if isinstance(schedule_cfg, dict):
                schedule_copy = copy.deepcopy(schedule_cfg)
                schedule_copy['group_ids'] = [grp_key]
                base_schedule_list.append(schedule_copy)
    return base_schedule_list


def _base_group_schedules(group_scheds_cfg):
    """The schedules of a ``group_schedules`` section: a mapping by group, or a list naming their groups."""
    if isinstance(group_scheds_cfg, dict):
        return _group_keyed_schedules(group_scheds_cfg)
    if isinstance(group_scheds_cfg, list):
        return [copy.deepcopy(schedule_cfg) for schedule_cfg in group_scheds_cfg if isinstance(schedule_cfg, dict)]
    return []


def _prepare_group_schedules(base_schedule_list, known_ids, group_id_map):
    """The group schedules with their group references resolved to group IDs; one with none is skipped."""
    prepared_schedules = []
    for schedule_cfg in base_schedule_list:
        raw_ids = schedule_cfg.get('group_ids')
        if not raw_ids and 'groups' in schedule_cfg:
            raw_ids = schedule_cfg['groups']
        resolved_ids = _expand_group_ids(raw_ids, known_ids, group_id_map)
        if not resolved_ids:
            logger.warning("No valid group IDs resolved for schedule; skipping entry")
            continue
        schedule_copy = copy.deepcopy(schedule_cfg)
        schedule_copy['group_ids'] = resolved_ids
        schedule_copy.pop('groups', None)
        prepared_schedules.append(schedule_copy)
    return prepared_schedules


def _create_group_level_schedules(run, schema_config, chronos_token):
    """Create the group-level schedules if defined (only for the current source_schema)."""
    group_scheds_cfg = schema_config.get('group_schedules')
    if not group_scheds_cfg:
        return
    logger.info("Creating group-level schedules...")
    try:
        known_ids = {gid for gid in run.group_id_map.values() if gid and gid != _DEFAULT_GROUP}
        prepared_schedules = _prepare_group_schedules(_base_group_schedules(group_scheds_cfg), known_ids,
                                                      run.group_id_map)
        if prepared_schedules:
            create_group_schedules(run.ctx.token, run.ctx.pipeline_id, prepared_schedules, chronos_token=chronos_token)
        else:
            logger.debug("No valid group schedules to create after resolving targets")
    except Exception as e:
        logger.error(f"Error creating group-level schedules: {str(e)}")


def _create_schedules(run, yaml_config, chronos_token):
    """Create the entity, group and pipeline schedules the YAML defines for the current source_schema."""
    has_schedules, current_schema_config = _schedules_schema_config(yaml_config, run.source_schema)
    if not has_schedules:
        logger.debug("No schedules defined in YAML config, skipping schedule creation")
        return

    logger.info("Creating schedules for current schema...")

    # Get updated entity IDs from the pipeline config
    try:
        response = fetch_core_hub(_ENTITIES_PATH.format(pipeline_id=run.ctx.pipeline_id), token=run.ctx.token)
        logger.info(f"Retrieved the following entities: {response}")

        if not isinstance(response, list) or not response:
            logger.warning(f"Unexpected response when fetching entities: {response}")
            return

        # Process entities in the format used in main.py
        entities_map = _entity_ids_by_table(response)

        _create_entity_level_schedules(run, current_schema_config, entities_map, chronos_token)

        if 'group_schedules' in current_schema_config:
            _create_group_level_schedules(run, current_schema_config, chronos_token)

        # Create pipeline-level schedules if defined
        if 'schedules' in current_schema_config:
            logger.info(f"Creating pipeline-level schedules for schema {run.source_schema}")
            create_pipeline_schedules(run.ctx.token, run.ctx.pipeline_id, current_schema_config['schedules'],
                                      chronos_token=chronos_token)

    except Exception as e:
        logger.error(f"Error creating schedules: {str(e)}")
        # Don't fail the whole process just because scheduling failed


def _process_duplicate_tables(run):
    """Second loop: the duplicate table configurations with @@ the first loop did not process."""
    logger.info(f"First loop completed. Processed {len(run.processed_yaml_keys)} YAML keys.")
    logger.info("Checking for unprocessed duplicate tables with @@ separator...")

    unprocessed_count = 0
    for yaml_table_key, custom_config in run.schema.custom_tables.items():
        unprocessed_count = _process_duplicate_table(run, yaml_table_key, custom_config, unprocessed_count)

    logger.info(f"Second loop completed. Processed {unprocessed_count} duplicate tables with @@ separator.")
    logger.info(f"Total YAML keys processed: {len(run.processed_yaml_keys)}")


def _print_summary(counts):
    print("\nProcessing complete:")
    print(f"- Total regular entities: {counts['total']}")
    print(f"- Successfully created regular entities: {counts['successful']}")
    print(f"- Failed regular entities: {counts['failed']}")
    print(f"- Total MultiTable entities: {counts['total_multi']}")
    print(f"- Successfully created MultiTable entities: {counts['successful_multi']}")
    print(f"- Failed MultiTable entities: {counts['failed_multi']}")


def _exported_custom_functions(token, pipeline_id, yaml_config, grouped):
    """The names the pipeline's installed function collection exports, when the YAML has a ``CustomFn`` bound to one.

    A CoreHub before Gluesync 2.3 has no function collections and no ``CustomFn``, and a YAML without one has nothing
    to check: None. A pipeline with no collection installed gives ``function_collection.NO_COLLECTION``. When the
    collection cannot be read the check is left to CoreHub, which refuses the entity itself.
    """
    if not grouped or not function_collection.custom_function_names(yaml_config):
        return None
    try:
        return function_collection.installed_function_names(
            function_collection.fetch_function_collection(token, pipeline_id, fetch=fetch_core_hub))
    except function_collection.FunctionCollectionError as error:
        logger.warning(f"{error}; the functions the YAML binds are not checked before saving the entities")
        return None


def _start_entity_run(token, pipeline_id, yaml_config, source_schema, target_schema, source_agent_id, target_agent_id,
                      source_type, target_type, skip_errors, source_agent_tag, target_agent_tag):
    """The run state of create_entities, once the node infos and the CoreHub version are read."""
    # Get node info for data type mapping
    source_node_info, target_node_info = _node_infos(token, pipeline_id, source_agent_id, target_agent_id, yaml_config)

    # A Gluesync 2.3 CoreHub takes the grouped column mapping matrix and runs its whole mapping stage (matrix,
    # producer and key rules) on every entity saved here, since none carries an entityId: read once (BOP-5 A).
    grouped = corehub_takes_grouped_matrix(token, fetch=fetch_core_hub)
    logger.info(f"Column mapping matrix: {'grouped (Gluesync 2.3)' if grouped else 'single pairs (CoreHub before 2.3)'}")
    is_source_nosql_type = bool(source_type and "nosql" in source_type.lower())
    is_target_nosql_type = bool(target_type and "nosql" in target_type.lower())

    schema = _SchemaSettings(yaml_config, source_schema, target_schema)
    ctx = _PipelineContext(token, pipeline_id, target_agent_id, schema.yaml_target_schema, source_node_info,
                           target_node_info, source_agent_tag, target_agent_tag, skip_errors, is_source_nosql_type,
                           is_target_nosql_type, _exported_custom_functions(token, pipeline_id, yaml_config, grouped))
    return _EntityRun(ctx, schema, source_schema, target_schema, source_agent_id, source_type, target_type, grouped)



def create_entities(token, pipeline_id, source_schema, target_schema, tables, source_agent_id, target_agent_id, source_type, target_type, yaml_config, skip_errors=True, chunk_size=50, source_agent_tag=None, target_agent_tag=None, chronos_token=None):
    """Create entities for the pipeline.

    Returns the successful, failed and total entity counts, and under ``errors`` each entity not created with its
    reason (BOP-18 A).
    """
    # Debug logging to see what source_type and target_type values are received
    logger.debug(f"create_entities called with: source_type='{source_type}', target_type='{target_type}'")
    logger.debug(f"source_type type: {type(source_type)}, target_type type: {type(target_type)}")

    # Log CREATE_TABLE_IF_NOT_EXISTS status for debugging
    if CREATE_TABLE_IF_NOT_EXISTS:
        logger.info("CREATE_TABLE_IF_NOT_EXISTS is enabled - will create missing tables during entity discovery")
    else:
        logger.info("CREATE_TABLE_IF_NOT_EXISTS is disabled - assuming all tables exist")

    # First, collect the group names of the whole YAML and the MultiTable chains of this source schema, and
    # create the groups before creating entities.
    group_names, chained_tables = _collect_groups_and_chains(yaml_config, source_schema)
    group_id_map = _create_groups(token, pipeline_id, yaml_config, group_names)

    if not yaml_config:
        yaml_config = {}

    run = _start_entity_run(token, pipeline_id, yaml_config, source_schema, target_schema, source_agent_id,
                            target_agent_id, source_type, target_type, skip_errors, source_agent_tag,
                            target_agent_tag)
    run.group_id_map = group_id_map

    run.source_tables_lookup = _build_table_lookup(tables, source_schema)
    run.target_tables_lookup = _discover_target_tables(run.ctx)

    for table in tables:
        _process_discovered_table(run, table)

    _process_duplicate_tables(run)

    # For each chainId, create a MultiTable entity
    multi_table_entities = []
    for chain_id, tables_list in chained_tables.items():
        multi_table_entity = _build_multi_table_entity(run, chain_id, tables_list)
        if multi_table_entity is not None:
            multi_table_entities.append(multi_table_entity)

    # Process MultiTable entities first, then the regular entities in chunks
    successful_multi_tables, failed_multi_tables, total_multi_tables = \
        _send_multi_table_entities(run, multi_table_entities)
    total_entities, successful_entities, failed_entities = _send_entity_chunks(run, chunk_size)

    if run.refused_tables:
        logger.error(f"{len(run.refused_tables)} entities refused before any request: {', '.join(run.refused_tables)}")
        failed_entities += len(run.refused_tables)
        total_entities += len(run.refused_tables)

    _apply_logical_partitions(token, pipeline_id, run.pending_partition_requests, skip_errors)

    _print_summary({
        "total": total_entities, "successful": successful_entities, "failed": failed_entities,
        "total_multi": total_multi_tables, "successful_multi": successful_multi_tables,
        "failed_multi": failed_multi_tables,
    })

    # MultiTable entities count with the regular ones, as their chains refused before any request already do.
    result = {
        "successful": successful_entities + successful_multi_tables,
        "failed": failed_entities + failed_multi_tables,
        "total": total_entities + total_multi_tables,
        "errors": run.errors,
    }

    # Assign entities to their groups and create schedules if successful
    if successful_entities > 0:
        _assign_entities_to_groups(run)
        _create_schedules(run, yaml_config, chronos_token)

    return result


def _function_collection_sources(yaml_file, yaml_config):
    """The ``{fileName, code}`` sources of the function collection that comes with the YAML, [] when there is none.

    ``FUNCTION_COLLECTION_PATH`` names a folder of sources. Otherwise the ``function-collection`` folder of a backup
    ZIP is looked for under ``UDF_PATH`` and next to the YAML (what the Automator extracts a ZIP into), in the
    ``pipeline_<id>`` folder of the pipeline the YAML was exported from when it says which.
    """
    explicit = os.getenv('FUNCTION_COLLECTION_PATH')
    if explicit:
        directory = explicit if os.path.isdir(explicit) else None
        if directory is None:
            raise FileNotFoundError(f"FUNCTION_COLLECTION_PATH {explicit!r} is not a directory")
    else:
        export_metadata = (yaml_config or {}).get('exportMetadata')
        exported_from = export_metadata.get('pipelineId') if isinstance(export_metadata, dict) else None
        roots = [udf_module.UDF_PATH, os.path.dirname(os.path.abspath(yaml_file)) if yaml_file else None]
        directory = function_collection.find_function_collection_dir(roots, exported_from)
    return function_collection.read_function_collection_dir(directory) if directory else []


def _restore_function_collection(token, pipeline_id, yaml_file, yaml_config):
    """Publish and install the function collection of the backup before any UDF is compiled or entity saved.

    The UDFs may call its classes and the entities bind its functions. Raises
    :class:`function_collection.FunctionCollectionError` when CoreHub refuses it.
    """
    sources = _function_collection_sources(yaml_file, yaml_config)
    if sources:
        function_collection.restore_function_collection(token, pipeline_id, sources, fetch=fetch_core_hub)


def _resolve_udf_path(yaml_file):
    """Resolve UDF_PATH when it is not already set.

    With the app-owned workspace design, the Automator UI extracts each upload/import's YAML + UDF files into a single
    session directory under the workspace root, and sets UDF_PATH to it. When running standalone (no UDF_PATH), fall
    back to the YAML's own directory — which, for Automator-driven runs, IS that session directory and therefore
    already contains the UDF files. find_udf_definition_in_path additionally scans the whole workspace root, so UDFs
    are found even without this hint.
    """
    if yaml_file and not udf_module.UDF_PATH:
        yaml_dir = os.path.dirname(os.path.abspath(yaml_file))
        if yaml_dir and os.path.isdir(yaml_dir):
            udf_module.UDF_PATH = yaml_dir
            logger.info(f"UDF_PATH not set; falling back to YAML directory: {yaml_dir}")


def _fail_run(error_msg):
    """Log a failure of :func:`main` and leave the failure lockfile; raises it."""
    log_failure(logger, error_msg)
    lockfile_failure()
    raise Exception(error_msg)


def _pipeline_agents(token, pipeline_id, source_type, target_type):
    """``(source_agent, target_agent, source_agent_id, target_agent_id)`` of a pipeline; :func:`_fail_run` without them."""
    agents = get_pipeline_agents(token, pipeline_id)

    if not agents or len(agents) != 2:
        _fail_run(f"Expected 2 agents, found {len(agents) if agents else 0}")

    # Identify source and target agents based on type
    source_agent = next((agent for agent in agents if agent['agentType'] == 'SOURCE'), None)
    target_agent = next((agent for agent in agents if agent['agentType'] == 'TARGET'), None)

    if not source_agent or not target_agent:
        _fail_run(f"Could not find required agents. Source ({source_type}): {source_agent}, Target ({target_type}): {target_agent}")

    # Extract agent IDs with fallback for different field names
    source_agent_id = source_agent.get('agentId') or source_agent.get('id')
    target_agent_id = target_agent.get('agentId') or target_agent.get('id')

    if not source_agent_id or not target_agent_id:
        _fail_run(f"Agents missing IDs. Source ID: {source_agent_id}, Target ID: {target_agent_id}. Source agent: {source_agent}, Target agent: {target_agent}")

    logger.info(f"Using source agent ID: {source_agent_id}, target agent ID: {target_agent_id}")
    return source_agent, target_agent, source_agent_id, target_agent_id


def main(pipeline_id, source_schema, target_schema, source_type, target_type, yaml_file, token, skip_errors=True,
         chunk_size=50, chronos_token=None, restore_function_collection=True):
    """
    Main function to create entities for a pipeline.

    Returns the result of create_entities (successful, failed and total counts, and under ``errors`` each entity
    not created with its reason), or, when an error is skipped, that error alone, so a caller such as the
    Automator can tell a failed run from a successful one (BOP-18 A).

    The function collection that comes with the YAML (see :func:`_function_collection_sources`) is published and
    installed first, unless ``restore_function_collection`` is False because the caller has done it.
    """
    logger.info(f"Starting entity creation for pipeline {pipeline_id}")
    try:
        yaml_config = load_yaml_config(yaml_file) if yaml_file else None

        _resolve_udf_path(yaml_file)

        if restore_function_collection:
            _restore_function_collection(token, pipeline_id, yaml_file, yaml_config)

        # Read the pipeline configuration: fails early on a pipeline that does not exist
        get_pipeline_config(token, pipeline_id)

        # Get agents information
        source_agent, target_agent, source_agent_id, target_agent_id = _pipeline_agents(
            token, pipeline_id, source_type, target_type)

        # Get tables from source agent
        tables = get_agent_tables(token, pipeline_id, source_agent_id, source_schema)

        if not tables:
            _fail_run(f"No tables found in schema {source_schema}")

        # Debug logging before calling create_entities
        logger.debug(f"main() about to call create_entities with: source_type='{source_type}', target_type='{target_type}'")

        # Create entities
        result = create_entities(
            token, pipeline_id, source_schema, target_schema,
            tables, source_agent_id, target_agent_id,
            source_type, target_type, yaml_config, skip_errors, chunk_size,
            source_agent_tag=source_agent.get('agentTag'),
            target_agent_tag=target_agent.get('agentTag'),
            chronos_token=chronos_token
        )

        if result.get('skipped_discovery', False):
            logger.info("Entity discovery was skipped as tables will be created on demand")

    except Exception as e:
        log_failure(logger, f"Error: {str(e)}")
        lockfile_failure()
        if not skip_errors:
            raise
        logger.warning("Skipping error due to skip_errors=True")
        return {"successful": 0, "failed": 0, "total": 0, "errors": [str(e)]}

    # Log successful completion
    log_success(logger, f"Entity creation completed successfully for pipeline {pipeline_id}")
    lockfile_complete()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gluesync Entity Creation Script")
    parser.add_argument('--pipeline', required=True, help="Pipeline ID")
    parser.add_argument('--source-schema', required=True, help="Source schema name")
    parser.add_argument('--chunk-size', type=int, default=50,
                        help="Number of entities to process in each chunk (default: 50)")
    parser.add_argument('--skip-errors', action='store_true', help="Continue execution even if errors occur")
    parser.add_argument('--target-schema', required=True, help="Target schema name")
    parser.add_argument('--source-type', required=True, help="Source agent type")
    parser.add_argument('--target-type', required=True, help="Target agent type")
    parser.add_argument('--yaml-file', help="YAML configuration file path")
    parser.add_argument('--token', required=True, help="Authentication token")
    parser.add_argument('--chronos-token', default=None, help="User token for Chronos authentication (falls back to --token if omitted)")

    args = parser.parse_args()

    main(
        args.pipeline,
        args.source_schema,
        args.target_schema,
        args.source_type,
        args.target_type,
        args.yaml_file,
        args.token,
        args.skip_errors,
        args.chunk_size,
        chronos_token=args.chronos_token
    )

#!/usr/bin/env python3

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

import argparse
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from commons import fetch_core_hub, get_table_columns
from field_function_utils import apply_field_functions_to_column_mappings, normalize_expression_for_yaml
from entity_mapping import read_entry, relations_of, yaml_rows
from utils.chronos_client import ChronosClient
from utils.log import create_log_file, get_logger, log_failure, log_success


log_file = create_log_file()
logger = get_logger(log_file)

_AGENT_TYPE_BY_NAME: Optional[Dict[str, str]] = None


def _column_data_type(col: Dict[str, Any]) -> Any:
    """Read a column's data type, preferring the 2.2.6.0 'dataType' field but
    falling back to the legacy 'type' field for backward compatibility."""
    return col.get("dataType") if col.get("dataType") is not None else col.get("type")


def _column_attr(col: Dict[str, Any], new_key: str, legacy_key: str, default: Any = 0) -> Any:
    """Return a column attribute preferring the 2.2.6.0 name then the legacy one."""
    val = col.get(new_key)
    if val is None:
        val = col.get(legacy_key)
    return default if val is None else val


def _keys_from_columns(columns: List[Dict[str, Any]]) -> List[str]:
    """Derive primary-key column names from a flat list of column dicts where
    isPK is true. This is the 2.2.6.0 replacement for the removed 'keys' array
    on agent entities. Order is preserved as encountered."""
    out: List[str] = []
    seen: set = set()
    for col in columns or []:
        if not isinstance(col, dict):
            continue
        is_pk = col.get("isPK") or col.get("isPrimaryKey")
        name = col.get("name")
        if is_pk and name and name not in seen:
            seen.add(name)
            out.append(str(name))
    return out


def _load_agent_type_catalog() -> Dict[str, str]:
    """Load agents.json catalog and normalize types to SQL/NoSQL."""

    global _AGENT_TYPE_BY_NAME
    if _AGENT_TYPE_BY_NAME is not None:
        return _AGENT_TYPE_BY_NAME

    mapping: Dict[str, str] = {}

    try:
        base_dir = Path(__file__).resolve().parent
        json_path = base_dir / "agents.json"
        if not json_path.exists():
            _AGENT_TYPE_BY_NAME = mapping
            return mapping

        with json_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

        items = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            _AGENT_TYPE_BY_NAME = mapping
            return mapping

        for entry in items:
            if not isinstance(entry, dict):
                continue
            name = entry.get("internalName")
            kind = entry.get("type")
            if not name or not kind:
                continue
            label = str(kind).strip().upper()
            if not label:
                continue
            if label == "RDBMS":
                normalized = "SQL"
            else:
                normalized = "NoSQL"
            mapping[str(name).lower()] = normalized
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to load agents.json catalog for type inference: %s", exc)
        mapping = {}

    _AGENT_TYPE_BY_NAME = mapping
    return mapping


def infer_pipeline_schema_types_from_agents(token: str, pipeline_id: str) -> Tuple[str, str]:
    """Infer (sourceType, targetType) as SQL/NoSQL from pipeline agents.

    Uses agents.json as the primary source of truth and falls back to SQL when
    the agent tag is unknown.
    """

    try:
        config = fetch_core_hub(f"/pipelines/{pipeline_id}/config", token=token)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch pipeline %s config for type inference: %s", pipeline_id, exc)
        return "SQL", "SQL"

    agents = config.get("agents") if isinstance(config, dict) else None
    if not isinstance(agents, list):
        return "SQL", "SQL"

    catalog = _load_agent_type_catalog()

    def _classify(agent: Dict[str, Any]) -> str:
        tag = str(agent.get("agentTag") or "").lower()
        if not tag:
            return "SQL"
        if tag in catalog:
            return catalog[tag]
        return "SQL"

    source_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "SOURCE"), None)
    target_agent = next((a for a in agents if isinstance(a, dict) and a.get("agentType") == "TARGET"), None)

    return _classify(source_agent or {}), _classify(target_agent or {})


def fetch_pipeline_entities(token: str, pipeline_id: str) -> List[Dict[str, Any]]:
    """Fetch entities for a pipeline from CoreHub and normalize the structure."""
    logger.info(f"Fetching entities for pipeline {pipeline_id}")
    response = fetch_core_hub(f"/pipelines/{pipeline_id}/entities", token=token)

    entities: List[Dict[str, Any]] = []
    if isinstance(response, list):
        for item in response:
            if isinstance(item, dict) and "entity" in item and isinstance(item["entity"], dict):
                entities.append(item["entity"])
            elif isinstance(item, dict):
                entities.append(item)
    elif isinstance(response, dict) and "entities" in response:
        for item in response.get("entities", []):
            if isinstance(item, dict):
                entities.append(item)
    else:
        logger.warning(f"Unexpected entities response format: {str(response)[:500]}")

    logger.info(f"Found {len(entities)} entities in pipeline {pipeline_id}")
    return entities


def fetch_groups_map(token: str, pipeline_id: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Return three maps for groups in the pipeline.

    - id_to_name: group_id -> name
    - name_to_id: name -> group_id
    - groups_by_name: name -> full metadata (id, description, color)
    """
    try:
        response = fetch_core_hub(
            f"/pipelines/{pipeline_id}/config/groups", token=token
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Failed to fetch groups for pipeline {pipeline_id}: {exc}")
        return {}, {}, {}

    id_to_name: Dict[str, str] = {}
    name_to_id: Dict[str, str] = {}
    groups_by_name: Dict[str, Dict[str, Any]] = {}

    if isinstance(response, list):
        for grp in response:
            if not isinstance(grp, dict):
                continue
            gid = str(grp.get("groupId")) if grp.get("groupId") is not None else None
            name = grp.get("name")
            if gid and name:
                name_str = str(name)
                id_to_name[gid] = name_str
                name_to_id[name_str] = gid
                groups_by_name[name_str] = {
                    "id": gid,
                    "description": grp.get("description", ""),
                    "color": grp.get("color"),
                }

    logger.info(
        f"Discovered {len(id_to_name)} groups for pipeline {pipeline_id}: {json.dumps(id_to_name)}"
    )
    return id_to_name, name_to_id, groups_by_name


def _chronos_clients(token: Optional[str]) -> List[ChronosClient]:
    """The Chronos endpoints to read from, in order.

    CoreHub's /chronos path first, as before; then CHRONOS_URL, where the import
    writes schedules (a Chronos container of its own, as in the demo kits).
    """
    clients = [ChronosClient(corehub_url=os.getenv("CORE_HUB_URL"), token=token)]
    chronos_url = os.getenv("CHRONOS_URL")
    if chronos_url and chronos_url.rstrip("/") + "/" != clients[0].base_url:
        clients.append(ChronosClient(base_url=chronos_url, token=token))
    return clients


def _jobs_from_response(jobs_response: Any) -> Optional[List[Dict[str, Any]]]:
    """The jobs of a Chronos answer: a plain list (legacy) or {"items": [...]}."""
    if isinstance(jobs_response, list):
        return [j for j in jobs_response if isinstance(j, dict)]
    items = jobs_response.get("items") if isinstance(jobs_response, dict) else None
    if isinstance(items, list):
        return [j for j in items if isinstance(j, dict)]
    logger.warning(f"Unexpected Chronos jobs response format: {str(jobs_response)[:500]}")
    return None


def read_pipeline_jobs(pipeline_id: str, token: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """The Chronos jobs of a pipeline, or None when no Chronos endpoint answered."""
    for client in _chronos_clients(token):
        try:
            jobs_response = client._request(  # type: ignore[attr-defined]
                "api/jobs/", params={"pipeline_id": pipeline_id, "limit": 1000}
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(f"Chronos at {client.base_url} did not answer for pipeline {pipeline_id}: {exc}")
            continue
        jobs = _jobs_from_response(jobs_response)
        if jobs is not None:
            # Chronos may ignore the pipeline_id filter and answer with every job.
            jobs = [j for j in jobs if str(j.get("pipeline_id", pipeline_id)) == str(pipeline_id)]
            logger.info(f"Fetched {len(jobs)} Chronos jobs for pipeline {pipeline_id} from {client.base_url}")
            return jobs
    return None


def fetch_pipeline_jobs(pipeline_id: str, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all Chronos jobs for the given pipeline ([] when Chronos cannot be read)."""
    jobs = read_pipeline_jobs(pipeline_id, token)
    if jobs is None:
        logger.warning(f"No Chronos endpoint answered for pipeline {pipeline_id}: its schedules are not exported")
        return []
    return jobs


def invert_filter(filter_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Convert CoreHub filter object back to YAML-style filter configuration."""
    if not filter_obj or "clauses" not in filter_obj:
        return {}

    clauses_yaml: List[Dict[str, Any]] = []
    for clause in filter_obj.get("clauses", []):
        if not isinstance(clause, dict):
            continue
        col = clause.get("column", {}) or {}
        op = clause.get("operation", {}) or {}
        op_type = op.get("type")
        column_name = col.get("name")
        if not column_name or not op_type:
            continue

        yaml_clause: Dict[str, Any] = {
            "column": column_name,
            "type": col.get("type", "string"),
            "operation": op_type,
        }

        if op_type not in ("IsNull", "IsNotNull") and "filterValue" in op:
            yaml_clause["value"] = op["filterValue"]

        clauses_yaml.append(yaml_clause)

    if not clauses_yaml:
        return {}
    return {"clauses": clauses_yaml}


def build_entities_maps(entities: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build a mapping from entityId to entity payload for quick lookup."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        eid = ent.get("entityId")
        if eid is None:
            continue
        by_id[str(eid)] = ent
    return by_id


def extract_source_and_target(ent: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (source_agent_entity, target_agent_entity) from an entity payload."""
    source = None
    target = None
    for ae in ent.get("agentEntities", []):
        etype = (ae.get("entityType") or {}).get("type")
        if etype == "Source" and source is None:
            source = ae
        elif etype == "Target" and target is None:
            target = ae
    return source or {}, target or {}


def init_schema_cfg(target_schema: str) -> Dict[str, Any]:
    return {
        "target": target_schema,
        "customProperties": {},
        "schedules": [],
        "group_schedules": [],
        "tables": {
            "whitelist": set(),
            "custom": {},
        },
    }


def build_schemas_from_entities(
    entities: List[Dict[str, Any]],
    group_id_to_name: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Reconstruct schema/table-level configuration from entities."""
    schemas: Dict[str, Dict[str, Any]] = {}
    source_types: set[str] = set()
    target_types: set[str] = set()

    for ent in entities:
        entity_name = ent.get("entityName", "")
        if not entity_name:
            continue

        source_ae, target_ae = extract_source_and_target(ent)
        if not source_ae or not target_ae:
            logger.warning(f"Entity {entity_name} missing source/target agent entities. Skipping.")
            continue

        # Track effective source/target *schema* type based on entity type
        # - NoSqlEntity -> "NoSQL"
        # - SingleTable / MultiTable / others -> "SQL" (relational)
        src_type = source_ae.get("type")
        tgt_type = target_ae.get("type")

        source_types.add("NoSQL" if src_type == "NoSqlEntity" else "SQL")
        target_types.add("NoSQL" if tgt_type == "NoSqlEntity" else "SQL")

        # Detect MultiTable vs SingleTable
        entity_type = source_ae.get("type") or target_ae.get("type")
        if entity_type == "MultiTable":
            _process_multitable_entity(ent, source_ae, target_ae, schemas, group_id_to_name)
        else:
            _process_single_entity(ent, source_ae, target_ae, schemas, group_id_to_name)

    # If we were able to infer a single source/target type across all entities,
    # propagate them to each schema configuration so they can be exported to YAML.
    if source_types and target_types:
        if len(source_types) == 1 and len(target_types) == 1:
            inferred_source_type = next(iter(source_types))
            inferred_target_type = next(iter(target_types))
            logger.info(
                "Inferred pipeline types from entities: sourceType=%s, targetType=%s",
                inferred_source_type,
                inferred_target_type,
            )
            for schema_cfg in schemas.values():
                schema_cfg["sourceType"] = inferred_source_type
                schema_cfg["targetType"] = inferred_target_type
        else:
            logger.warning(
                "Unable to infer a single source/target type from entities. "
                "Found sourceTypes=%s, targetTypes=%s",
                sorted(source_types),
                sorted(target_types),
            )

    return schemas


def _is_grouped_matrix(matrix: List[Dict[str, Any]]) -> bool:
    """Whether a stored matrix holds the column id lists of Gluesync 2.3 (an older CoreHub stores single pairs)."""
    return any(isinstance(entry, dict) and ("sourceColumnIds" in entry or "targetColumnIds" in entry) for entry in matrix)


def _mapping_row(scol: Optional[Dict[str, Any]], tcol: Optional[Dict[str, Any]], ordinal: Optional[int]) -> Dict[str, Any]:
    """The YAML row of source column ``scol`` onto target column ``tcol``, with the metadata of both."""
    source_name = scol.get("name") if scol else None
    row: Dict[str, Any] = {"source": source_name, "target": tcol.get("name", source_name) if tcol else source_name}
    row["type"] = (_column_data_type(tcol) if tcol else None) or (_column_data_type(scol) if scol else None)
    if scol is None:
        return row
    if row["type"] is None:
        logger.warning(
            f"Column '{source_name}' has no data type in stored entity data (legacy entity with unserialised "
            f"DataTypeInterface). The type field will be null in the export; use a recent version of Gluesync "
            f"or re-create the entity to populate the type."
        )
    row.update({
        "dataLength": _column_attr(scol, "charMaxLength", "dataLength", 0),
        "numericPrecision": _column_attr(scol, "numPrec", "numericPrecision", 0),
        "numericScale": _column_attr(scol, "numScale", "numericScale", 0),
        "isNullable": scol.get("isNullable", False),
        "isPK": bool(scol.get("isPK") or scol.get("isPrimaryKey")),
        "id": scol.get("id"),
        "ordinalPosition": ordinal,
        # The character set the column is read with, which the user may have
        # overridden (an AS400 per-column CCSID, for one). Always written, null
        # included: on import an absent key keeps whatever discovery publishes,
        # so leaving it out would silently restore a column the source's way
        # instead of the way it was backed up.
        "charSet": scol.get("charSet"),
    })
    return row


def _grouped_mapping_rows(
    matrix: List[Dict[str, Any]],
    ordered_source: List[Tuple[int, Dict[str, Any]]],
    target_columns: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The YAML rows of a Gluesync 2.3 matrix, and the relations they come from (C-5)."""
    source_columns = [col for _, col in ordered_source]
    relations = relations_of([read_entry(entry) for entry in matrix], source_columns, target_columns)
    sources_by_name = {col.get("name"): (index, col) for index, col in ordered_source}
    targets_by_name = {col.get("name"): col for col in target_columns}

    def row_of(source_name, target_name):
        index, scol = sources_by_name.get(source_name, (None, None))
        return _mapping_row(scol, targets_by_name.get(target_name), index)

    order = [col.get("name") for _, col in ordered_source if col.get("name")]
    return yaml_rows(relations, order, row_of), relations


def _target_keys_to_export(relations: Dict[str, Any], source_columns: List[Dict[str, Any]],
                           target_columns: List[Dict[str, Any]]) -> List[str]:
    """``targetKeys`` of a Gluesync 2.3 entity: its stored target keys when they are not the ones it inherits.

    The keys a target inherits are the targets of the relations of a source key column; any other stored set
    (the target's own primary key, a key produced by a function) is written so the import keeps it.
    """
    source_keys = {col.get("name") for col in source_columns if col.get("isPK") or col.get("isPrimaryKey")}
    inherited = {t for rel in relations["relations"] if source_keys.intersection(rel["sources"]) for t in rel["targets"]}
    stored = [col.get("name") for col in target_columns if col.get("isPK") or col.get("isPrimaryKey")]
    return stored if set(stored) != inherited and stored else []


def _target_columns_by_id(ordered_target: List[Tuple[int, Dict[str, Any]]]) -> Dict[Any, Dict[str, Any]]:
    """Target columns by id, logging the first few of them."""
    target_by_id = {}
    for idx, (_, tcol) in enumerate(ordered_target):
        tid = tcol.get("id")
        tname = tcol.get("name")
        if tid is not None:
            target_by_id[tid] = tcol
            if idx < 3:  # Log only first 3 to avoid spam
                logger.info(f"Target column: id={tid}, name={tname}")
    return target_by_id


def _matrix_target_column_id(columns_mapping_matrix: List[Dict[str, Any]], source_id: Any) -> Any:
    """Target column id of the first pair of the mapping matrix that names source column ``source_id``."""
    for mapping in columns_mapping_matrix:
        if mapping.get("sourceColumnId") == source_id:
            return mapping.get("targetColumnId")
    return None


def _positional_target_column(
    ordered_target: List[Tuple[int, Dict[str, Any]]],
    col_index: int,
    source_name: str,
    source_table_name: str,
) -> Optional[Dict[str, Any]]:
    """Fallback: pair source/target columns by order when mapping matrix missing."""
    target_col = None
    target_idx = col_index - 1
    if target_idx < len(ordered_target):
        _, target_col = ordered_target[target_idx]
        if target_col.get("name", source_name) != source_name:
            logger.info(f"Fallback column mapping by position: {source_name} -> {target_col.get('name')}")
    if col_index == 1:
        logger.info(f"No columnsMappingMatrix for {source_table_name}; using positional fallback")
    return target_col


def _legacy_mapping_rows(
    columns_mapping_matrix: List[Dict[str, Any]],
    ordered_source: List[Tuple[int, Dict[str, Any]]],
    ordered_target: List[Tuple[int, Dict[str, Any]]],
    source_table_name: str,
) -> List[Dict[str, Any]]:
    """The YAML rows of a matrix of single pairs (a CoreHub older than Gluesync 2.3), as they have always been.

    Each source column goes to the target of the first pair that names it, else to its own name; with no
    matrix at all, source and target columns are paired by position.
    """
    has_mapping_matrix = len(columns_mapping_matrix) > 0
    target_by_id = _target_columns_by_id(ordered_target)

    column_mappings = []
    # Use index in sorted array as ordinalPosition (1-based)
    for col_index, scol in ordered_source:
        source_name = scol.get("name")
        source_id = scol.get("id")
        if not source_name or source_id is None:
            continue

        # Find corresponding target column ID from mapping matrix
        target_col_id = _matrix_target_column_id(columns_mapping_matrix, source_id)

        target_col = None
        if target_col_id is not None and target_col_id in target_by_id:
            target_col = target_by_id[target_col_id]
            if target_col.get("name", source_name) != source_name:
                logger.info(f"Column mapping: {source_name} -> {target_col.get('name')}")
        elif not has_mapping_matrix:
            target_col = _positional_target_column(ordered_target, col_index, source_name, source_table_name)
        elif col_index == 1:  # Log only once when matrix exists but id missing
            logger.info(f"No target mapping found for {source_name} (using same name)")

        column_mappings.append(_mapping_row(scol, target_col, col_index))
    return column_mappings


def _column_position(col: Dict[str, Any], stored_index: int) -> int:
    """Where a column sits in its table.

    Discovered columns carry "position"; "id" is a hash of the column, so it is
    no order at all: without a position the stored order is kept.
    """
    raw_position = col.get("ordinalPosition")
    if raw_position is None:
        raw_position = col.get("position")
    try:
        return int(raw_position)
    except (TypeError, ValueError):
        return stored_index


def _columns_in_table_order(columns: List[Dict[str, Any]]) -> List[Tuple[int, Dict[str, Any]]]:
    """``(1-based stored index, column)`` pairs sorted by position in the table."""
    return sorted(enumerate(columns, start=1), key=lambda pair: _column_position(pair[1], pair[0]))


# "type" is implied by the side, and partitions are recomputed on import.
_SOURCE_EXCLUDED_SETTINGS = frozenset({"type", "partitionSettings"})


def _source_custom_properties(source_ae: Dict[str, Any]) -> Dict[str, Any]:
    """``customProperties.source`` of a table: the settings its source runs with.

    The source entityType holds what CoreHub runs with (the import writes
    customProperties.source there). Keys found only in the agent entity's own
    customProperties are kept too, so nothing the user wrote is lost.
    """
    source_et = source_ae.get("entityType", {}) or {}
    user_only = {
        k: v for k, v in (source_ae.get("customProperties") or {}).items() if k not in source_et
    }
    settings = {**user_only, **source_et}
    return {k: v for k, v in settings.items() if k not in _SOURCE_EXCLUDED_SETTINGS}


def _single_source_location(entity_name: str, source_table: Dict[str, Any]) -> Tuple[Any, Any]:
    """Source schema and table name of a SingleTable/NoSqlEntity entity.

    Fallbacks from entityName if table metadata missing. Split at the first dot only so table names that
    themselves contain dots (e.g. "my.table") are preserved intact.
    """
    source_schema = source_table.get("schema")
    source_table_name = source_table.get("name")
    if not source_schema or not source_table_name:
        parts = entity_name.split(".", 1)
        if len(parts) >= 2:
            source_schema = source_schema or parts[0]
            source_table_name = source_table_name or parts[-1]
    return source_schema, source_table_name


def _single_target_location(
    target_table: Dict[str, Any],
    target_entity_object: Dict[str, Any],
    source_schema: str,
    source_table_name: str,
) -> Tuple[Any, Any]:
    """Target schema and table name, checking both table and entityObject (the source ones by default)."""
    target_schema = target_table.get("schema") or target_entity_object.get("scope") or source_schema
    # For NoSQL, collection name is in entityObject.collection, for SQL it's in table.name
    target_table_name = target_table.get("name") or target_entity_object.get("collection") or source_table_name
    return target_schema, target_table_name


def _is_custom_group(group_id: Any) -> bool:
    """Whether an entity belongs to a group of its own (not the default one)."""
    return bool(group_id and group_id != "_default")


def _schema_cfg_for(schemas: Dict[str, Dict[str, Any]], source_schema: str, target_schema: str) -> Dict[str, Any]:
    """The configuration of ``source_schema``, created onto ``target_schema`` when it does not exist yet."""
    schema_cfg = schemas.get(source_schema)
    if not schema_cfg:
        schema_cfg = init_schema_cfg(target_schema)
        schemas[source_schema] = schema_cfg
    return schema_cfg


def _unique_table_key(schema_cfg: Dict[str, Any], source_schema: str, source_table_name: str, entity_name: str) -> str:
    """The YAML key of a source table: its name, or ``name@@N`` when another entity already reads that table."""
    custom_tables: Dict[str, Any] = schema_cfg["tables"]["custom"]
    existing_table_cfg = custom_tables.get(source_table_name)
    if not existing_table_cfg:
        return source_table_name
    # Table already exists - check if it's a duplicate with different config
    existing_entity_name = existing_table_cfg.get("entityName", "")
    if not existing_entity_name or existing_entity_name == entity_name:
        return source_table_name
    # This is a duplicate table with different entity - create unique key
    # Use @@ as separator to avoid conflicts with tables that have underscores
    suffix_num = 2
    while f"{source_table_name}@@{suffix_num}" in custom_tables:
        suffix_num += 1
    table_key = f"{source_table_name}@@{suffix_num}"
    logger.warning(
        f"Duplicate source table detected: {source_schema}.{source_table_name} "
        f"used in multiple entities ({existing_entity_name}, {entity_name}). "
        f"Creating separate entry as '{table_key}'"
    )
    # Mark schema as having duplicates for later splitting
    schema_cfg.setdefault("has_duplicates", []).append({
        "original_table": source_table_name,
        "unique_key": table_key,
        "entity_name": entity_name
    })
    return table_key


def _single_table_keys(source_ae: Dict[str, Any]) -> List[str]:
    """Keys: prefer the legacy 'keys' array if present (older CoreHub versions),
    otherwise derive from columns where isPK == true (2.2.6.0+ column model)."""
    keys: List[str] = []
    for key in source_ae.get("keys", []) or []:
        name = key.get("name")
        if name:
            keys.append(name)
    if not keys:
        keys = _keys_from_columns(source_ae.get("columns", []) or [])
    return keys


def _export_where_clause(
    table_cfg: Dict[str, Any],
    source_ae: Dict[str, Any],
    source_schema: str,
    table_name: str,
    label: str,
) -> None:
    """Extract whereClause from source tablesProperties of ``source_schema.table_name``."""
    source_tables_properties = source_ae.get("tablesProperties", {}) or {}
    source_table_key = f"{source_schema}.{table_name}"
    table_properties = source_tables_properties.get(source_table_key, {}) or {}
    if "whereClause" in table_properties and table_properties["whereClause"]:
        where_clause = table_properties["whereClause"]
        table_cfg["whereClause"] = str(where_clause)
        logger.debug(f"Exported whereClause for {label}: {where_clause}")


def _export_single_column_mappings(
    table_cfg: Dict[str, Any],
    source_ae: Dict[str, Any],
    target_ae: Dict[str, Any],
    source_table_name: str,
) -> None:
    """Columns: Export source->target column mappings (and the target keys of a Gluesync 2.3 matrix)."""
    source_columns = source_ae.get("columns", []) or []
    target_columns = target_ae.get("columns", []) or []
    if not (source_columns and target_columns):
        return

    # Sort both source and target columns by their position in the table
    ordered_source = _columns_in_table_order(source_columns)
    ordered_target = _columns_in_table_order(target_columns)

    # The matrix of the target entity type: lists of column ids on Gluesync 2.3, single pairs before.
    columns_mapping_matrix = target_ae.get("entityType", {}).get("columnsMappingMatrix", []) or []
    logger.info(
        f"Table {source_table_name}: columnsMappingMatrix has {len(columns_mapping_matrix)} entries"
    )
    unlocked_tables = (target_ae.get("entityType", {}) or {}).get("tablesWithUnlockedSchema") or []

    if _is_grouped_matrix(columns_mapping_matrix) and not unlocked_tables:
        column_mappings, relations = _grouped_mapping_rows(
            columns_mapping_matrix, ordered_source, [col for _, col in ordered_target]
        )
        target_keys = _target_keys_to_export(relations, source_columns, target_columns)
        if target_keys:
            table_cfg["targetKeys"] = target_keys
    else:
        column_mappings = _legacy_mapping_rows(
            columns_mapping_matrix, ordered_source, ordered_target, source_table_name
        )

    # Always export column mappings with full metadata
    if column_mappings:
        table_cfg["columns"] = column_mappings
        logger.debug(f"Exported {len(column_mappings)} column mappings with metadata for {source_table_name}")


def _export_single_field_functions(table_cfg: Dict[str, Any], target_ae: Dict[str, Any], source_table_name: str) -> None:
    """Field Functions (per-column expressions on Target entityType) — GSSD-1355.

    CoreHub stores these on entityType.fieldFunctions as {columnId, tableOrObjectId, expression:{type,...}};
    YAML documents them as columns[].expression (Date2Str / TrimStr / StaticStr / …).
    """
    target_et = target_ae.get("entityType", {}) or {}
    field_functions = target_et.get("fieldFunctions") or []
    if not field_functions:
        return
    column_mappings = table_cfg.get("columns") or []
    if not isinstance(column_mappings, list):
        column_mappings = []
    column_mappings = apply_field_functions_to_column_mappings(
        column_mappings,
        target_ae.get("columns", []) or [],
        field_functions,
    )
    if column_mappings:
        table_cfg["columns"] = column_mappings
        logger.info(
            f"Exported {sum(1 for c in column_mappings if isinstance(c, dict) and c.get('expression'))} "
            f"field function expression(s) for {source_table_name}"
        )


def _export_target_filters(table_cfg: Dict[str, Any], target_et: Dict[str, Any]) -> None:
    """Filters on target entityType, and unlockedSchema detection."""
    for filter_key in ("filter", "snapshotDeleteFilter"):
        if filter_key in target_et:
            inv = invert_filter(target_et.get(filter_key, {}))
            if inv:
                table_cfg[filter_key] = inv

    tables_with_unlocked = target_et.get("tablesWithUnlockedSchema") or []
    if tables_with_unlocked:
        table_cfg["unlockedSchema"] = True


def _column_names_by_int_id(columns: List[Dict[str, Any]]) -> Dict[int, str]:
    """Column names by integer column id (columns without a usable id are left out)."""
    id_to_name: Dict[int, str] = {}
    for col in columns:
        cid = col.get("id")
        name = col.get("name")
        if cid is None or name is None:
            continue
        try:
            cid_int = int(cid)
        except (TypeError, ValueError):
            continue
        id_to_name[cid_int] = name
    return id_to_name


def _document_key_names(key_ids: List[Any], id_to_name: Dict[int, str], source_table_name: str) -> List[str]:
    """Column names of the document key column ids."""
    key_names: List[str] = []
    for kid in key_ids:
        try:
            kid_int = int(kid)
        except (TypeError, ValueError):
            logger.debug(f"Document key has non-integer ID: {kid}")
            continue
        name = id_to_name.get(kid_int)
        if name:
            key_names.append(name)
        else:
            logger.warning(
                f"Unable to map document key column ID {kid_int} back to a column name for table {source_table_name}"
            )
    return key_names


def _document_key_cfg(
    source_ae: Dict[str, Any],
    target_ae: Dict[str, Any],
    source_table_name: str,
) -> Optional[Dict[str, Any]]:
    """Document key (keyMapping uses column IDs), with the column names in place of the ids."""
    doc_key = target_ae.get("keyMapping") or {}
    if not (doc_key and isinstance(doc_key, dict)):
        return None
    key_ids = doc_key.get("keys", []) or []
    id_to_name = _column_names_by_int_id(source_ae.get("columns", []) or [])
    key_names = _document_key_names(key_ids, id_to_name, source_table_name)
    if not key_names:
        return None
    return {
        "prefix": doc_key.get("prefix", ""),
        "suffix": doc_key.get("suffix", ""),
        "separator": doc_key.get("separator", "-"),
        "keys": key_names,
    }


def _udf_config(target_et: Dict[str, Any]) -> Any:
    """UDFs are stored on entityType, but YAML expects them in customProperties.target.udf.

    Prefer the explicit "udf" array when present; otherwise, fall back to
    mappingFunctionInfo (the single-UDT representation used by Core Hub).
    """
    udf_cfg = target_et.get("udf")
    if udf_cfg:
        return udf_cfg
    mf_info = target_et.get("mappingFunctionInfo")
    if not isinstance(mf_info, dict):
        return udf_cfg
    udf_name = mf_info.get("name")
    if not udf_name:
        return udf_cfg
    udf_entry: Dict[str, Any] = {"name": udf_name}
    mf_type = mf_info.get("type")
    if mf_type is not None:
        udf_entry["type"] = mf_type
    return [udf_entry]


_DEFAULT_ALLOWED_OPERATIONS = ["INSERT", "DELETE", "UPDATE", "TRUNCATE"]


def _target_custom_properties(target_ae: Dict[str, Any], target_et: Dict[str, Any]) -> Dict[str, Any]:
    """``customProperties.target`` of a table: its own custom properties and the non-default target settings."""
    target_cp = (target_ae.get("customProperties") or {}).copy()

    udf_cfg = _udf_config(target_et)
    if udf_cfg:
        target_cp["udf"] = udf_cfg

    # Snapshot concurrency: expose only when different from default 1
    snapshot_conc = target_et.get("snapshotWritingConcurrency")
    if isinstance(snapshot_conc, int) and snapshot_conc != 1:
        target_cp["snapshotWritingConcurrency"] = snapshot_conc

    # Bulk operations flags (default False) - include only when enabled
    for bulk_key in ("useBulkOperationsDuringCDC", "useBulkOperationsWhileSnapshot"):
        bulk_flag = target_et.get(bulk_key)
        if isinstance(bulk_flag, bool) and bulk_flag:
            target_cp[bulk_key] = bulk_flag

    # allowedOperations: expose effective operations when different from default
    allowed_ops = target_et.get("allowedOperations")
    if isinstance(allowed_ops, list) and allowed_ops and allowed_ops != _DEFAULT_ALLOWED_OPERATIONS:
        target_cp["allowedOperations"] = allowed_ops
    return target_cp


def _attach_custom_properties(table_cfg: Dict[str, Any], source_cp: Dict[str, Any], target_cp: Dict[str, Any]) -> None:
    """Write the source/target custom properties of a table, when it has any."""
    if source_cp or target_cp:
        cp = table_cfg.setdefault("customProperties", {})
        if source_cp:
            cp["source"] = source_cp
        if target_cp:
            cp["target"] = target_cp


def _process_single_entity(
    ent: Dict[str, Any],
    source_ae: Dict[str, Any],
    target_ae: Dict[str, Any],
    schemas: Dict[str, Dict[str, Any]],
    group_id_to_name: Dict[str, str],
) -> None:
    """Process a SingleTable/NoSqlEntity entity into schema/table configuration."""
    entity_name = ent.get("entityName", "")

    source_table = source_ae.get("table", {}) or {}
    target_table = target_ae.get("table", {}) or {}

    # For NoSQL entities, also check entityObject which contains collection name
    target_entity_object = target_ae.get("entityObject", {}) or {}

    source_schema, source_table_name = _single_source_location(entity_name, source_table)
    if not source_schema or not source_table_name:
        logger.warning(f"Unable to determine source schema/table for entity {entity_name}. Skipping.")
        return

    target_schema, target_table_name = _single_target_location(
        target_table, target_entity_object, source_schema, source_table_name
    )

    schema_cfg = _schema_cfg_for(schemas, source_schema, target_schema)

    # Check if this table already exists with different configuration
    # If so, create a unique key by appending a suffix
    table_key = _unique_table_key(schema_cfg, source_schema, source_table_name, entity_name)

    # Track whitelist tables using the final key (including any @@N suffix for duplicates)
    # so that whitelist and custom always have the same set of entries.
    schema_cfg["tables"]["whitelist"].add(table_key)

    table_cfg = {}
    schema_cfg["tables"]["custom"][table_key] = table_cfg

    # Table name (target name / collection)
    table_cfg["name"] = target_table_name
    table_cfg["entityName"] = entity_name

    # If the YAML key is different from the actual source table name (duplicate table case),
    # store the real source table name so import can find it in the database
    if table_key != source_table_name:
        table_cfg["sourceTableName"] = source_table_name

    # Group mapping
    group_id = ent.get("groupId")
    if _is_custom_group(group_id):
        table_cfg["groupId"] = group_id_to_name.get(str(group_id), str(group_id))

    keys = _single_table_keys(source_ae)
    if keys:
        table_cfg["keys"] = keys

    _export_where_clause(table_cfg, source_ae, source_schema, source_table_name, source_table_name)
    _export_single_column_mappings(table_cfg, source_ae, target_ae, source_table_name)
    _export_single_field_functions(table_cfg, target_ae, source_table_name)

    target_et = target_ae.get("entityType", {}) or {}
    _export_target_filters(table_cfg, target_et)

    document_key = _document_key_cfg(source_ae, target_ae, source_table_name)
    if document_key:
        table_cfg["documentKey"] = document_key

    # Custom properties
    source_cp = _source_custom_properties(source_ae)
    target_cp = _target_custom_properties(target_ae, target_et)
    _attach_custom_properties(table_cfg, source_cp, target_cp)


def _group_multitable_entries(entries: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Decode the alternating [table, [items]] structure used by MultiTable payloads."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(entries, list):
        return grouped
    idx = 0
    while idx < len(entries):
        header = entries[idx]
        body = entries[idx + 1] if idx + 1 < len(entries) else []
        idx += 2
        if not isinstance(header, dict) or not isinstance(body, list):
            continue
        table_name = header.get("name")
        if not table_name:
            continue
        grouped[str(table_name)] = [item for item in body if isinstance(item, dict)]
    return grouped


def _keys_by_table_from_pk_columns(
    key_groups: Dict[str, List[Dict[str, Any]]],
    column_groups: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[str]]:
    """In 2.2.6.0 the 'keys' array is removed from MultiTable agent entities; the
    primary-key information is encoded as isPK on each column. Derive a
    fallback {table_name: [pk_col_name, ...]} mapping so that downstream code
    can still emit a 'keys' list per chained table."""
    keys_by_table_from_is_pk: Dict[str, List[str]] = {}
    if not key_groups and column_groups:
        for tname, cols in column_groups.items():
            pk_names = _keys_from_columns(cols)
            if pk_names:
                keys_by_table_from_is_pk[tname] = pk_names
    return keys_by_table_from_is_pk


def _source_to_target_table_names(tables: List[Dict[str, Any]], target_tables: List[Dict[str, Any]]) -> Dict[Any, Any]:
    """Build a mapping from source table names to target table names (when both lists pair up)."""
    source_to_target_name = {}
    if target_tables and len(target_tables) == len(tables):
        for src_tbl, tgt_tbl in zip(tables, target_tables):
            src_name = src_tbl.get("name")
            tgt_name = tgt_tbl.get("name")
            if src_name and tgt_name:
                source_to_target_name[src_name] = tgt_name
    return source_to_target_name


@dataclass
class _MultiTableExport:
    """What the export of every table of one MultiTable entity shares."""

    entity_name: str
    chain_id: str
    group_name: Optional[str]
    source_schema: str
    schema_cfg: Dict[str, Any]
    source_ae: Dict[str, Any]
    target_tables: List[Dict[str, Any]]
    column_groups: Dict[str, List[Dict[str, Any]]]
    key_groups: Dict[str, List[Dict[str, Any]]]
    target_column_groups: Dict[str, List[Dict[str, Any]]]
    matrix: List[Any]
    field_functions: List[Any]
    keys_by_table_from_is_pk: Dict[str, List[str]]
    source_to_target_name: Dict[Any, Any]


def _multitable_grouped_columns(
    export: _MultiTableExport,
    table_cfg: Dict[str, Any],
    table_name: str,
    pair_matrix: List[Dict[str, Any]],
    target_cols: List[Dict[str, Any]],
    paired_target: Optional[Dict[str, Any]],
) -> None:
    """The rows, target keys and field functions of a SingleTable entity, so every 2.3 form comes back (BOP-16 A).

    A column fed by no source is a {target, expression} row, which the import checks with the key rules, instead
    of a targetOnlyColumns entry. The source of a MultiTable entity holds every column, so a column read and never
    written needs no target: [] row unless it is a source key, which the import would otherwise map by name;
    without one, a 1:1 chain still imports into 2.2.x.
    """
    source_columns = export.column_groups[table_name]
    rows, relations = _grouped_mapping_rows(pair_matrix, list(enumerate(source_columns, start=1)), target_cols)
    source_keys = set(_keys_from_columns(source_columns))
    rows = [row for row in rows if row.get("target") != [] or row.get("source") in source_keys]
    pair_functions = [
        ff for ff in export.field_functions
        if paired_target is None or str(ff.get("tableOrObjectId")) == str(paired_target.get("id"))
    ]
    table_cfg["columns"] = apply_field_functions_to_column_mappings(rows, target_cols, pair_functions)
    target_keys = _target_keys_to_export(relations, source_columns, target_cols)
    if target_keys:
        table_cfg["targetKeys"] = target_keys


def _export_multitable_mapping_rows(
    export: _MultiTableExport,
    table_cfg: Dict[str, Any],
    tbl: Dict[str, Any],
    table_name: str,
    target_table_name: str,
) -> None:
    """Mapping rows, as for a SingleTable entity, so renames and target-only columns survive (BOP-12 A)."""
    source_table_id = tbl.get("id")
    paired_target = next(
        (t for t in export.target_tables if str(t.get("name")) == str(target_table_name)), None
    )
    target_cols = export.target_column_groups.get(str(target_table_name))
    if table_name not in export.column_groups or target_cols is None or "columns" in table_cfg:
        return
    pair_matrix = [
        entry for entry in export.matrix
        if isinstance(entry, dict) and str(entry.get("sourceTableObjectId")) == str(source_table_id)
    ]
    if _is_grouped_matrix(pair_matrix):
        _multitable_grouped_columns(export, table_cfg, table_name, pair_matrix, target_cols, paired_target)
    else:
        ordered_source = list(enumerate(export.column_groups[table_name], start=1))
        table_cfg["columns"] = _legacy_mapping_rows(
            pair_matrix, ordered_source, list(enumerate(target_cols, start=1)), table_name
        )


def _multitable_column_metadata(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Column metadata with full details including id and ordinalPosition."""
    column_metadata: List[Dict[str, Any]] = []
    for col in columns:
        name = col.get("name")
        if not name:
            continue

        # Use the actual id from the entity column data
        col_id = col.get("id")
        # Use ordinalPosition from entity if available, otherwise use id
        ordinal = col.get("ordinalPosition") or col.get("position") or col_id

        col_meta: Dict[str, Any] = {
            "name": name,
            "type": _column_data_type(col),
            "dataLength": _column_attr(col, "charMaxLength", "dataLength", 0),
            "numericPrecision": _column_attr(col, "numPrec", "numericPrecision", 0),
            "numericScale": _column_attr(col, "numScale", "numericScale", 0),
            "isNullable": col.get("isNullable", False),
            "isPK": bool(col.get("isPK") or col.get("isPrimaryKey")),
            "id": col_id,
            "ordinalPosition": ordinal,
            # See the single table export above: written even when null so the
            # restore puts the column back exactly as it was backed up
            "charSet": col.get("charSet"),
        }
        column_metadata.append(col_meta)
    return column_metadata


def _multitable_key_names(export: _MultiTableExport, table_name: str) -> List[str]:
    """Key names of a chained table.

    Prefer the legacy per-table key array; if absent (2.2.6.0+), fall back
    to the isPK-derived list.
    """
    key_names: List[str] = []
    for key in export.key_groups.get(table_name, []):
        name = key.get("name")
        if name and name not in key_names:
            key_names.append(str(name))
    if not key_names and table_name in export.keys_by_table_from_is_pk:
        key_names = list(export.keys_by_table_from_is_pk[table_name])
    return key_names


def _export_multitable_table(export: _MultiTableExport, tbl: Dict[str, Any]) -> None:
    """Assign chain metadata to one table of the chain and restore its columns/keys."""
    table_name = tbl.get("name")
    if not table_name:
        return

    export.schema_cfg["tables"]["whitelist"].add(table_name)

    custom_tables: Dict[str, Any] = export.schema_cfg["tables"]["custom"]
    table_cfg = custom_tables.get(table_name)
    if not table_cfg:
        table_cfg = {}
        custom_tables[table_name] = table_cfg

    # Use the actual target table name if available, otherwise use source table name
    target_table_name = export.source_to_target_name.get(table_name, table_name)
    table_cfg.setdefault("name", target_table_name)
    # MultiTable entities share one entity name; persist it per table so import can reuse it
    table_cfg["entityName"] = export.entity_name
    table_cfg["chainId"] = export.chain_id
    if export.group_name:
        table_cfg["groupId"] = export.group_name

    _export_multitable_mapping_rows(export, table_cfg, tbl, table_name, target_table_name)

    # Restore column metadata with full details including id and ordinalPosition
    if table_name in export.column_groups and "columns" not in table_cfg:
        column_metadata = _multitable_column_metadata(export.column_groups[table_name])
        if column_metadata:
            table_cfg["columns"] = column_metadata

    # Restore key names if missing.
    if "keys" not in table_cfg:
        key_names = _multitable_key_names(export, table_name)
        if key_names:
            table_cfg["keys"] = key_names

    # Extract whereClause from source tablesProperties for this table
    _export_where_clause(table_cfg, export.source_ae, export.source_schema, table_name, f"MultiTable {table_name}")


def _process_multitable_entity(
    ent: Dict[str, Any],
    source_ae: Dict[str, Any],
    target_ae: Dict[str, Any],
    schemas: Dict[str, Dict[str, Any]],
    group_id_to_name: Dict[str, str],
) -> None:
    """Approximate reconstruction of MultiTable chains.

    We can't recover the original chainId string, so we generate a deterministic one
    based on the first table name.
    """
    entity_name = ent.get("entityName", "")

    # Source tables list
    tables = source_ae.get("tables", []) or []
    if not tables:
        logger.warning(f"MultiTable entity {entity_name} has no tables. Skipping.")
        return

    # Determine source schema from first source table
    first_table = tables[0]
    source_schema = first_table.get("schema")
    first_table_name = first_table.get("name")
    if not source_schema or not first_table_name:
        logger.warning(f"Unable to determine schema for MultiTable entity {entity_name}. Skipping.")
        return

    # Group info from entity
    group_id = ent.get("groupId")
    group_name = None
    if _is_custom_group(group_id):
        group_name = group_id_to_name.get(str(group_id), str(group_id))

    # Ensure schema config exists
    # Target schema for MultiTable is inferred from first target table
    target_tables = target_ae.get("tables", []) or []
    target_schema = (target_tables[0].get("schema") or source_schema) if target_tables else source_schema
    schema_cfg = _schema_cfg_for(schemas, source_schema, target_schema)

    column_groups = _group_multitable_entries(source_ae.get("columns"))
    key_groups = _group_multitable_entries(source_ae.get("keys"))
    target_column_groups = _group_multitable_entries(target_ae.get("columns"))
    target_type = target_ae.get("entityType", {}) or {}

    export = _MultiTableExport(
        entity_name=entity_name,
        # Generate deterministic chainId
        chain_id=f"multitable_{first_table_name}",
        group_name=group_name,
        source_schema=source_schema,
        schema_cfg=schema_cfg,
        source_ae=source_ae,
        target_tables=target_tables,
        column_groups=column_groups,
        key_groups=key_groups,
        target_column_groups=target_column_groups,
        matrix=target_type.get("columnsMappingMatrix", []) or [],
        field_functions=target_type.get("fieldFunctions") or [],
        keys_by_table_from_is_pk=_keys_by_table_from_pk_columns(key_groups, column_groups),
        source_to_target_name=_source_to_target_table_names(tables, target_tables),
    )

    # For each table in the chain, assign chain metadata and restore columns/keys
    for tbl in tables:
        _export_multitable_table(export, tbl)


def attach_schedules_from_jobs(
    jobs: List[Dict[str, Any]],
    entities_by_id: Dict[str, Dict[str, Any]],
    schemas: Dict[str, Dict[str, Any]],
    group_id_to_name: Dict[str, str],
) -> None:
    """Attach pipeline/group/entity schedules from Chronos jobs to schema configs."""
    if not jobs or not schemas:
        return

    # Determine primary schema for pipeline-level and group-level schedules
    primary_schema = sorted(schemas.keys())[0]
    primary_schema_cfg = schemas[primary_schema]

    for job in jobs:
        if not isinstance(job, dict):
            continue

        task_type = job.get("task_type")
        if not task_type:
            continue

        # Normalize basic schedule fields
        sched: Dict[str, Any] = {
            "name": job.get("name"),
            "description": job.get("description"),
            "task_type": task_type,
            "with_snapshot": job.get("with_snapshot", False),
            "enabled": job.get("enabled", True),
        }

        swm = job.get("snapshot_write_method")
        if swm:
            sched["snapshot_write_method"] = swm

        if "cron_expression" in job:
            sched["cron_expression"] = job.get("cron_expression")
        elif "schedule" in job:
            sched["schedule"] = job.get("schedule")

        # Entity-level schedules
        if task_type.startswith("entity_"):
            entity_ids = job.get("entity_ids") or []
            if not isinstance(entity_ids, list):
                entity_ids = [entity_ids]

            for eid in entity_ids:
                ent = entities_by_id.get(str(eid))
                if not ent:
                    logger.warning(f"Schedule job {job.get('id')} references unknown entity ID {eid}")
                    continue

                ename = ent.get("entityName", "")
                # Split at first dot only to preserve dots inside table names.
                parts = ename.split(".", 1)
                if len(parts) >= 2:
                    schema_name = parts[0]
                    table_name = parts[-1]
                else:
                    schema_name = primary_schema
                    table_name = ename or "unknown"

                schema_cfg = schemas.get(schema_name)
                if not schema_cfg:
                    logger.warning(
                        f"Entity {ename} schema {schema_name} not present in schemas map. Skipping schedule."
                    )
                    continue

                custom_tables: Dict[str, Any] = schema_cfg["tables"]["custom"]
                table_cfg = custom_tables.get(table_name)
                if not table_cfg:
                    # Create a minimal table config if missing
                    table_cfg = {"name": table_name}
                    custom_tables[table_name] = table_cfg
                    schema_cfg["tables"]["whitelist"].add(table_name)

                table_schedules = table_cfg.setdefault("schedules", [])
                table_schedules.append(sched.copy())

            continue

        # Group-level schedules
        if task_type.startswith("group_"):
            group_ids = job.get("group_ids") or []
            if isinstance(group_ids, str):
                group_ids = [group_ids]

            group_names: List[str] = []
            for gid in group_ids:
                if gid == "*":
                    group_names.append("*")
                else:
                    group_names.append(group_id_to_name.get(str(gid), str(gid)))

            if not group_names:
                logger.warning(f"Group-level job {job.get('id')} has no group_ids. Skipping.")
                continue

            sched_entry = sched.copy()
            sched_entry["group_ids"] = group_names
            primary_schema_cfg.setdefault("group_schedules", []).append(sched_entry)
            continue

        # Pipeline-level schedules
        if task_type.startswith("pipeline_"):
            primary_schema_cfg.setdefault("schedules", []).append(sched.copy())
            continue


def build_yaml_structure(
    schemas: Dict[str, Dict[str, Any]],
    groups_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Convert internal schema representation and group metadata to final YAML structure."""
    if not schemas:
        return {}

    # Convert whitelist sets to lists and drop empty customProperties when possible
    normalized: Dict[str, Dict[str, Any]] = {}
    for schema_name, cfg in schemas.items():
        tables_section = cfg.get("tables", {}) or {}
        whitelist = sorted(list(tables_section.get("whitelist", set())))
        custom_tables = tables_section.get("custom", {}) or {}

        schema_out: Dict[str, Any] = {
            "target": cfg.get("target", schema_name),
        }

        # Optional schema-level source/target type hints (e.g. SQL / NoSQL)
        # These are used when re-importing the YAML to derive source/target agent types.
        if cfg.get("sourceType"):
            schema_out["sourceType"] = cfg["sourceType"]
        if cfg.get("targetType"):
            schema_out["targetType"] = cfg["targetType"]

        if cfg.get("customProperties"):
            # Only include non-empty customProperties
            non_empty_cp = {
                k: v for k, v in cfg["customProperties"].items() if v
            }
            if non_empty_cp:
                schema_out["customProperties"] = non_empty_cp

        if cfg.get("schedules"):
            schema_out["schedules"] = cfg["schedules"]

        if cfg.get("group_schedules"):
            schema_out["group_schedules"] = cfg["group_schedules"]

        schema_out["tables"] = {
            "whitelist": whitelist,
            "custom": custom_tables,
        }

        normalized[schema_name] = schema_out

    # Base structure: single-schema vs multi-schema format
    if len(normalized) == 1:
        schema_name, cfg = next(iter(normalized.items()))
        root: Dict[str, Any] = {schema_name: cfg}
    else:
        root = {"schemas": normalized}

    # Attach group metadata at top-level when available
    if groups_by_name:
        groups_yaml: Dict[str, Dict[str, Any]] = {}
        for name, meta in groups_by_name.items():
            if not isinstance(meta, dict):
                continue
            g: Dict[str, Any] = {}
            if meta.get("id") is not None:
                g["id"] = meta["id"]
            if meta.get("description"):
                g["description"] = meta["description"]
            if meta.get("color"):
                g["color"] = meta["color"]
            groups_yaml[name] = g

        if groups_yaml:
            root["groups"] = groups_yaml

    return root


_HEADER_RULE = "# ---------------------------------------------------------------"


def build_export_header(
    token: str,
    pipeline_id: str,
    base_url: Optional[str] = None,
) -> str:
    """Return a YAML comment block that records the provenance of the export.

    The header is prepended verbatim to every exported backup YAML so that
    anyone receiving the file can immediately tell:
    - which CoreHub instance produced it (URL + version),
    - which pipeline was exported (ID + name),
    - and when the export was taken.

    All fields are best-effort: failures to fetch any piece of info are
    silently swallowed so that a metadata hiccup never blocks the export.
    """
    from datetime import datetime, timezone

    corehub_version = "unknown"
    pipeline_name = pipeline_id
    corehub_url = base_url or os.getenv("CORE_HUB_URL", "https://localhost:1717")

    try:
        version_resp = fetch_core_hub("/version", token=token)
        if isinstance(version_resp, str):
            corehub_version = version_resp.strip()
        elif isinstance(version_resp, dict):
            corehub_version = (
                version_resp.get("version")
                or version_resp.get("appVersion")
                or str(version_resp)
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("build_export_header: could not fetch CoreHub version: %s", exc)

    try:
        pipeline_resp = fetch_core_hub(f"/pipelines/{pipeline_id}", token=token)
        if isinstance(pipeline_resp, dict):
            pipeline_name = pipeline_resp.get("name") or pipeline_id
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("build_export_header: could not fetch pipeline name: %s", exc)

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        _HEADER_RULE,
        "# Gluesync Automator - Pipeline Configuration Export",
        _HEADER_RULE,
        f"# CoreHub URL     : {corehub_url}",
        f"# CoreHub version : {corehub_version}",
        f"# Pipeline ID     : {pipeline_id}",
        f"# Pipeline name   : {pipeline_name}",
        f"# Exported at     : {exported_at} (UTC)",
        _HEADER_RULE,
        "",
    ]
    return "\n".join(lines)


def enrich_null_column_types_from_discovery(
    schemas: Dict[str, Any],
    entities: List[Dict[str, Any]],
    token: str,
    pipeline_id: str,
) -> int:
    """Post-processing pass: for every table whose exported column types are all null
    (symptom of legacy entities stored when Column.dataType was an unregistered interface),
    call the CoreHub live column discovery endpoint and backfill the 'type' field.

    Returns the number of tables that were successfully enriched.
    """
    # Identify the source agent ID from the entity list
    source_agent_id: Optional[str] = None
    for ent in entities:
        for ae in ent.get("agentEntities", []) or []:
            if (ae.get("entityType") or {}).get("type") == "Source":
                source_agent_id = ae.get("agentId")
                break
        if source_agent_id:
            break

    if not source_agent_id:
        logger.warning("enrich_null_column_types_from_discovery: could not determine source agent ID – skipping enrichment")
        return 0

    enriched = 0
    for schema_name, schema_cfg in schemas.items():
        if not isinstance(schema_cfg, dict):
            continue
        custom = (schema_cfg.get("tables") or {}).get("custom") or {}
        for table_name, table_cfg in custom.items():
            if not isinstance(table_cfg, dict):
                continue
            columns = table_cfg.get("columns") or []
            if not columns:
                continue

            # Only attempt enrichment when ALL column type fields are null –
            # that is the clear fingerprint of the legacy serialisation bug.
            export_format_cols = [
                c for c in columns
                if isinstance(c, dict) and "source" in c and c.get("type") is None
            ]
            if not export_format_cols or len(export_format_cols) < len(columns):
                continue  # already has types, or mixed format – leave alone

            try:
                discovered = get_table_columns(token, pipeline_id, source_agent_id, schema_name, table_name)
                if not discovered or not isinstance(discovered.get("columns"), list):
                    logger.warning(
                        f"enrich_null_column_types_from_discovery: discovery returned no columns "
                        f"for {schema_name}.{table_name} – leaving type as null"
                    )
                    continue

                disc_by_name: Dict[str, Any] = {
                    c["name"]: c for c in discovered["columns"] if c.get("name")
                }

                filled = 0
                for col_mapping in columns:
                    if not (isinstance(col_mapping, dict) and col_mapping.get("type") is None and "source" in col_mapping):
                        continue
                    source_col_name = col_mapping["source"]
                    if not isinstance(source_col_name, str):
                        continue  # an N:1 row (a list of sources): its type is the target column's
                    disc_col = disc_by_name.get(source_col_name)
                    if disc_col:
                        resolved = disc_col.get("dataType") or disc_col.get("type")
                        if resolved:
                            col_mapping["type"] = resolved
                            filled += 1

                if filled:
                    logger.info(
                        f"enrich_null_column_types_from_discovery: filled {filled}/{len(columns)} "
                        f"null column types for {schema_name}.{table_name} via live discovery"
                    )
                    enriched += 1
                else:
                    logger.warning(
                        f"enrich_null_column_types_from_discovery: discovery found no matching columns "
                        f"for {schema_name}.{table_name}"
                    )
            except Exception as exc:
                logger.warning(
                    f"enrich_null_column_types_from_discovery: failed for {schema_name}.{table_name}: {exc}"
                )

    return enriched


def render_pipeline_export(token: str, pipeline_id: str) -> str:
    """Export one pipeline and return the YAML text, provenance header included.

    This is the single export path: the CLI, ``export_all_pipelines.py`` and the
    round-trip verifier all write exactly what this returns.
    """
    entities = fetch_pipeline_entities(token, pipeline_id)
    entities_by_id = build_entities_maps(entities)
    group_id_to_name, _, groups_by_name = fetch_groups_map(token, pipeline_id)
    schemas = build_schemas_from_entities(entities, group_id_to_name)

    # Override schema-level type hints using agents.json catalog when possible
    src_type, tgt_type = infer_pipeline_schema_types_from_agents(token, pipeline_id)
    if src_type and tgt_type:
        logger.info(
            "Using agents.json to set pipeline types: sourceType=%s, targetType=%s",
            src_type,
            tgt_type,
        )
        for schema_cfg in schemas.values():
            schema_cfg["sourceType"] = src_type
            schema_cfg["targetType"] = tgt_type

    jobs = fetch_pipeline_jobs(pipeline_id, token=token)
    attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

    # Backfill null column types via live discovery (legacy entity serialisation bug)
    enriched = enrich_null_column_types_from_discovery(schemas, entities, token, pipeline_id)
    if enriched:
        logger.info(f"Enriched null column types for {enriched} table(s) via live discovery")

    yaml_data = build_yaml_structure(schemas, groups_by_name)

    # Build provenance header and prepend it to the YAML
    header = build_export_header(token, pipeline_id)
    yaml_buffer = io.StringIO()
    yaml.safe_dump(yaml_data, yaml_buffer, sort_keys=False, allow_unicode=True)
    return header + yaml_buffer.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export an existing Gluesync pipeline configuration from CoreHub/Chronos "
            "into a table-list-style YAML template (backup)."
        )
    )
    parser.add_argument("--pipeline", required=True, help="Pipeline ID to export")
    parser.add_argument("--token", required=True, help="Authentication token for CoreHub")
    parser.add_argument(
        "--output",
        help="Path to output YAML file (one file per pipeline). "
        "Defaults to ./backup_<PIPELINE_ID>.yaml when not provided.",
    )

    args = parser.parse_args()

    pipeline_id = args.pipeline
    token = args.token
    output_path = args.output or f"./backup_{pipeline_id}.yaml"

    logger.info(f"Starting export for pipeline {pipeline_id} -> {output_path}")

    try:
        rendered = render_pipeline_export(token, pipeline_id)

        # Ensure destination directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(rendered)

        log_success(logger, f"Export completed for pipeline {pipeline_id}. Output: {output_path}")

    except Exception as exc:  # pylint: disable=broad-except
        log_failure(logger, f"Export failed for pipeline {pipeline_id}: {exc}")
        raise


if __name__ == "__main__":
    main()

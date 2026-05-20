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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from commons import fetch_core_hub, get_table_columns
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


def fetch_pipeline_jobs(pipeline_id: str) -> List[Dict[str, Any]]:
    """Fetch all Chronos jobs for the given pipeline."""
    chronos_client = ChronosClient(corehub_url=os.getenv("CORE_HUB_URL"))
    try:
        jobs_response = chronos_client._request(  # type: ignore[attr-defined]
            "api/jobs/", params={"pipeline_id": pipeline_id, "limit": 1000}
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Failed to fetch Chronos jobs for pipeline {pipeline_id}: {exc}")
        return []

    # Chronos can return either:
    # - a plain list of jobs (legacy shape)
    # - a paginated object {"items": [...], ...} (newer shape)
    jobs: List[Dict[str, Any]]
    if isinstance(jobs_response, list):
        jobs = [j for j in jobs_response if isinstance(j, dict)]
    elif isinstance(jobs_response, dict):
        items = jobs_response.get("items")
        if isinstance(items, list):
            jobs = [j for j in items if isinstance(j, dict)]
        else:
            logger.warning(
                "Unexpected Chronos jobs response format: missing 'items' list in %s",
                str(jobs_response)[:500],
            )
            return []
    else:
        logger.warning(f"Unexpected Chronos jobs response format: {str(jobs_response)[:500]}")
        return []

    logger.info(f"Fetched {len(jobs)} Chronos jobs for pipeline {pipeline_id}")
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

    source_schema = source_table.get("schema")
    source_table_name = source_table.get("name")

    # Fallbacks from entityName if table metadata missing
    if not source_schema or not source_table_name:
        parts = entity_name.split(".")
        if len(parts) >= 2:
            source_schema = source_schema or parts[0]
            source_table_name = source_table_name or parts[-1]

    if not source_schema or not source_table_name:
        logger.warning(f"Unable to determine source schema/table for entity {entity_name}. Skipping.")
        return

    # Extract target schema and table name, checking both table and entityObject
    target_schema = target_table.get("schema") or target_entity_object.get("scope") or source_schema
    # For NoSQL, collection name is in entityObject.collection, for SQL it's in table.name
    target_table_name = target_table.get("name") or target_entity_object.get("collection") or source_table_name

    schema_cfg = schemas.get(source_schema)
    if not schema_cfg:
        schema_cfg = init_schema_cfg(target_schema)
        schemas[source_schema] = schema_cfg

    # Track whitelist tables (all tables that currently have entities)
    schema_cfg["tables"]["whitelist"].add(source_table_name)

    custom_tables: Dict[str, Any] = schema_cfg["tables"]["custom"]
    
    # Check if this table already exists with different configuration
    # If so, create a unique key by appending a suffix
    table_key = source_table_name
    existing_table_cfg = custom_tables.get(table_key)
    
    if existing_table_cfg:
        # Table already exists - check if it's a duplicate with different config
        existing_entity_name = existing_table_cfg.get("entityName", "")
        if existing_entity_name and existing_entity_name != entity_name:
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
            if "has_duplicates" not in schema_cfg:
                schema_cfg["has_duplicates"] = []
            schema_cfg["has_duplicates"].append({
                "original_table": source_table_name,
                "unique_key": table_key,
                "entity_name": entity_name
            })
    
    table_cfg = {}
    custom_tables[table_key] = table_cfg

    # Table name (target name / collection)
    table_cfg["name"] = target_table_name
    table_cfg["entityName"] = entity_name
    
    # If the YAML key is different from the actual source table name (duplicate table case),
    # store the real source table name so import can find it in the database
    if table_key != source_table_name:
        table_cfg["sourceTableName"] = source_table_name

    # Group mapping
    group_id = ent.get("groupId")
    if group_id and group_id != "_default":
        group_name = group_id_to_name.get(str(group_id), str(group_id))
        table_cfg["groupId"] = group_name

    # Keys: prefer the legacy 'keys' array if present (older CoreHub versions),
    # otherwise derive from columns where isPK == true (2.2.6.0+ column model).
    keys: List[str] = []
    for key in source_ae.get("keys", []) or []:
        name = key.get("name")
        if name:
            keys.append(name)
    if not keys:
        keys = _keys_from_columns(source_ae.get("columns", []) or [])
    if keys:
        table_cfg["keys"] = keys

    # Extract whereClause from source tablesProperties
    source_tables_properties = source_ae.get("tablesProperties", {}) or {}
    source_table_key = f"{source_schema}.{source_table_name}"
    table_properties = source_tables_properties.get(source_table_key, {}) or {}
    if "whereClause" in table_properties:
        where_clause = table_properties["whereClause"]
        if where_clause:
            table_cfg["whereClause"] = str(where_clause)
            logger.debug(f"Exported whereClause for {source_table_name}: {where_clause}")

    # Columns: Export source->target column mappings
    source_columns = source_ae.get("columns", []) or []
    target_columns = target_ae.get("columns", []) or []
    
    if source_columns and target_columns:
        def _column_sort_key(col: Dict[str, Any], idx: int) -> int:
            raw_id = col.get("ordinalPosition") or col.get("id")
            try:
                return int(raw_id)
            except (TypeError, ValueError):
                return idx

        # Sort both source and target columns by ordinalPosition/id
        ordered_source = sorted(
            enumerate(source_columns, start=1),
            key=lambda pair: _column_sort_key(pair[1], pair[0]),
        )
        ordered_target = sorted(
            enumerate(target_columns, start=1),
            key=lambda pair: _column_sort_key(pair[1], pair[0]),
        )
        
        # Build mapping by matching column IDs from columnsMappingMatrix
        columns_mapping_matrix = ent.get("columnsMappingMatrix", []) or []
        if not columns_mapping_matrix:
            columns_mapping_matrix = (
                target_ae.get("entityType", {}).get("columnsMappingMatrix", []) or []
            )
        has_mapping_matrix = len(columns_mapping_matrix) > 0
        logger.info(
            f"Table {source_table_name}: columnsMappingMatrix has {len(columns_mapping_matrix)} entries"
        )
        
        # Create ID-based lookup for target columns
        target_by_id = {}
        for idx, (_, tcol) in enumerate(ordered_target):
            tid = tcol.get("id")
            tname = tcol.get("name")
            if tid is not None:
                target_by_id[tid] = tcol
                if idx < 3:  # Log only first 3 to avoid spam
                    logger.info(f"Target column: id={tid}, name={tname}")
        
        # Build column mappings with full metadata: [{source_name: target_name, type, dataLength, ...}]
        column_mappings = []
        
        # Use index in sorted array as ordinalPosition (1-based)
        for col_index, scol in ordered_source:
            source_name = scol.get("name")
            source_id = scol.get("id")
            if not source_name or source_id is None:
                continue
            
            # Find corresponding target column ID from mapping matrix
            target_col_id = None
            if has_mapping_matrix:
                for mapping in columns_mapping_matrix:
                    if mapping.get("sourceColumnId") == source_id:
                        target_col_id = mapping.get("targetColumnId")
                        break

            # Get target column and its metadata
            target_name = source_name  # Default to same name
            target_type = _column_data_type(scol)
            if target_col_id is not None and target_col_id in target_by_id:
                target_col = target_by_id[target_col_id]
                target_name = target_col.get("name", source_name)
                target_type = _column_data_type(target_col) or _column_data_type(scol)
                if target_name != source_name:
                    logger.info(f"Column mapping: {source_name} -> {target_name}")
            else:
                # Fallback: pair source/target columns by order when mapping matrix missing
                if not has_mapping_matrix:
                    target_idx = col_index - 1
                    if target_idx < len(ordered_target):
                        _, fallback_target_col = ordered_target[target_idx]
                        target_name = fallback_target_col.get("name", source_name)
                        target_type = _column_data_type(fallback_target_col) or _column_data_type(scol)
                        if target_name != source_name:
                            logger.info(
                                f"Fallback column mapping by position: {source_name} -> {target_name}"
                            )
                    if col_index == 1:
                        logger.info(
                            f"No columnsMappingMatrix for {source_table_name}; using positional fallback"
                        )
                elif col_index == 1:  # Log only once when matrix exists but id missing
                    logger.info(
                        f"No target mapping found for {source_name} (using same name)"
                    )
            
            # Build column mapping with metadata using explicit source/target keys
            # Use column index in sorted array as ordinalPosition (1-based)
            if target_type is None:
                logger.warning(
                    f"Column '{source_name}' in table '{source_table_name}' has no data type "
                    f"in stored entity data (legacy entity with unserialised DataTypeInterface). "
                    f"The type field will be null in the export; use a recent version of GlueSync "
                    f"or re-create the entity to populate the type."
                )
            col_mapping = {
                "source": source_name,
                "target": target_name,
                "type": target_type,
                "dataLength": _column_attr(scol, "charMaxLength", "dataLength", 0),
                "numericPrecision": _column_attr(scol, "numPrec", "numericPrecision", 0),
                "numericScale": _column_attr(scol, "numScale", "numericScale", 0),
                "isNullable": scol.get("isNullable", False),
                "isPK": bool(scol.get("isPK") or scol.get("isPrimaryKey")),
                "id": source_id,
                "ordinalPosition": col_index  # Use 1-based index from sorted array
            }
            column_mappings.append(col_mapping)
        
        # Always export column mappings with full metadata
        if column_mappings:
            table_cfg["columns"] = column_mappings
            logger.debug(f"Exported {len(column_mappings)} column mappings with metadata for {source_table_name}")

    # Filters on target entityType
    target_et = target_ae.get("entityType", {}) or {}
    if "filter" in target_et:
        inv = invert_filter(target_et.get("filter", {}))
        if inv:
            table_cfg["filter"] = inv

    if "snapshotDeleteFilter" in target_et:
        inv = invert_filter(target_et.get("snapshotDeleteFilter", {}))
        if inv:
            table_cfg["snapshotDeleteFilter"] = inv

    # unlockedSchema detection
    tables_with_unlocked = target_et.get("tablesWithUnlockedSchema") or []
    if tables_with_unlocked:
        table_cfg["unlockedSchema"] = True

    # Document key (keyMapping uses column IDs)
    doc_key = target_ae.get("keyMapping") or {}
    if doc_key and isinstance(doc_key, dict):
        key_ids = doc_key.get("keys", []) or []
        id_to_name: Dict[int, str] = {}
        for col in source_ae.get("columns", []) or []:
            cid = col.get("id")
            name = col.get("name")
            if cid is None or name is None:
                continue
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                continue
            id_to_name[cid_int] = name

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

        if key_names:
            table_cfg["documentKey"] = {
                "prefix": doc_key.get("prefix", ""),
                "suffix": doc_key.get("suffix", ""),
                "separator": doc_key.get("separator", "-"),
                "keys": key_names,
            }

    # Custom properties
    source_et = source_ae.get("entityType", {}) or {}

    # Exclude internal keys that were not user-configured
    source_excluded = {"type", "unchangedDataFilterType", "partitionSettings"}
    source_cp = {
        k: v
        for k, v in source_et.items()
        if k not in source_excluded
    }

    target_cp = (target_ae.get("customProperties") or {}).copy()

    # UDFs are stored on entityType, but YAML expects them in customProperties.target.udf.
    # Prefer the explicit "udf" array when present; otherwise, fall back to
    # mappingFunctionInfo (the single-UDT representation used by Core Hub).
    udf_cfg = target_et.get("udf")
    if not udf_cfg:
        mf_info = target_et.get("mappingFunctionInfo")
        if isinstance(mf_info, dict):
            udf_name = mf_info.get("name")
            if udf_name:
                udf_entry: Dict[str, Any] = {"name": udf_name}
                mf_type = mf_info.get("type")
                if mf_type is not None:
                    udf_entry["type"] = mf_type
                udf_cfg = [udf_entry]
    if udf_cfg:
        target_cp["udf"] = udf_cfg

    # Snapshot concurrency: expose only when different from default 1
    snapshot_conc = target_et.get("snapshotWritingConcurrency")
    if isinstance(snapshot_conc, int) and snapshot_conc != 1:
        target_cp["snapshotWritingConcurrency"] = snapshot_conc

    # Bulk operations flags (default False) - include only when enabled
    bulk_cdc = target_et.get("useBulkOperationsDuringCDC")
    if isinstance(bulk_cdc, bool) and bulk_cdc:
        target_cp["useBulkOperationsDuringCDC"] = bulk_cdc

    bulk_snapshot = target_et.get("useBulkOperationsWhileSnapshot")
    if isinstance(bulk_snapshot, bool) and bulk_snapshot:
        target_cp["useBulkOperationsWhileSnapshot"] = bulk_snapshot

    # allowedOperations: expose effective operations when different from default
    allowed_ops = target_et.get("allowedOperations")
    default_ops = ["INSERT", "DELETE", "UPDATE", "TRUNCATE"]
    if isinstance(allowed_ops, list) and allowed_ops and allowed_ops != default_ops:
        target_cp["allowedOperations"] = allowed_ops

    if source_cp or target_cp:
        cp = table_cfg.setdefault("customProperties", {})
        if source_cp:
            cp["source"] = source_cp
        if target_cp:
            cp["target"] = target_cp


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

    # Generate deterministic chainId
    chain_id = f"multitable_{first_table_name}"

    # Group info from entity
    group_id = ent.get("groupId")
    group_name = None
    if group_id and group_id != "_default":
        group_name = group_id_to_name.get(str(group_id), str(group_id))

    # Ensure schema config exists
    # Target schema for MultiTable is inferred from first target table
    target_tables = target_ae.get("tables", []) or []
    if target_tables:
        first_target = target_tables[0]
        target_schema = first_target.get("schema") or source_schema
    else:
        target_schema = source_schema

    schema_cfg = schemas.get(source_schema)
    if not schema_cfg:
        schema_cfg = init_schema_cfg(target_schema)
        schemas[source_schema] = schema_cfg

    custom_tables: Dict[str, Any] = schema_cfg["tables"]["custom"]

    # Helper to decode alternating [table, [items]] structure used by MultiTable payloads
    def _group_entries(entries: Any) -> Dict[str, List[Dict[str, Any]]]:
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

    column_groups = _group_entries(source_ae.get("columns"))
    key_groups = _group_entries(source_ae.get("keys"))

    # In 2.2.6.0 the 'keys' array is removed from MultiTable agent entities; the
    # primary-key information is encoded as isPK on each column. Derive a
    # fallback {table_name: [pk_col_name, ...]} mapping so that downstream code
    # can still emit a 'keys' list per chained table.
    keys_by_table_from_isPK: Dict[str, List[str]] = {}
    if not key_groups and column_groups:
        for tname, cols in column_groups.items():
            pk_names = _keys_from_columns(cols)
            if pk_names:
                keys_by_table_from_isPK[tname] = pk_names
    
    # Build a mapping from source table names to target table names
    source_to_target_name = {}
    if target_tables and len(target_tables) == len(tables):
        for src_tbl, tgt_tbl in zip(tables, target_tables):
            src_name = src_tbl.get("name")
            tgt_name = tgt_tbl.get("name")
            if src_name and tgt_name:
                source_to_target_name[src_name] = tgt_name

    # For each table in the chain, assign chain metadata and restore columns/keys
    for tbl in tables:
        table_name = tbl.get("name")
        if not table_name:
            continue

        schema_cfg["tables"]["whitelist"].add(table_name)

        table_cfg = custom_tables.get(table_name)
        if not table_cfg:
            table_cfg = {}
            custom_tables[table_name] = table_cfg

        # Use the actual target table name if available, otherwise use source table name
        target_table_name = source_to_target_name.get(table_name, table_name)
        table_cfg.setdefault("name", target_table_name)
        # MultiTable entities share one entity name; persist it per table so import can reuse it
        table_cfg["entityName"] = entity_name
        table_cfg["chainId"] = chain_id
        if group_name:
            table_cfg["groupId"] = group_name

        # Restore column metadata with full details including id and ordinalPosition
        if table_name in column_groups and "columns" not in table_cfg:
            column_metadata: List[Dict[str, Any]] = []
            for col in column_groups[table_name]:
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
                }
                column_metadata.append(col_meta)
            
            if column_metadata:
                table_cfg["columns"] = column_metadata

        # Restore key names if missing.
        # Prefer the legacy per-table key array; if absent (2.2.6.0+), fall back
        # to the isPK-derived list computed above.
        if "keys" not in table_cfg:
            key_names: List[str] = []
            if table_name in key_groups:
                for key in key_groups[table_name]:
                    name = key.get("name")
                    if name and name not in key_names:
                        key_names.append(str(name))
            if not key_names and table_name in keys_by_table_from_isPK:
                key_names = list(keys_by_table_from_isPK[table_name])
            if key_names:
                table_cfg["keys"] = key_names

        # Extract whereClause from source tablesProperties for this table
        source_tables_properties = source_ae.get("tablesProperties", {}) or {}
        source_table_key = f"{source_schema}.{table_name}"
        table_properties = source_tables_properties.get(source_table_key, {}) or {}
        if "whereClause" in table_properties:
            where_clause = table_properties["whereClause"]
            if where_clause:
                table_cfg["whereClause"] = str(where_clause)
                logger.debug(f"Exported whereClause for MultiTable {table_name}: {where_clause}")


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
                parts = ename.split(".")
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
    corehub_url = base_url or CORE_HUB_URL

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
        "# ---------------------------------------------------------------",
        "# Gluesync Automator – Pipeline Configuration Export",
        "# ---------------------------------------------------------------",
        f"# CoreHub URL     : {corehub_url}",
        f"# CoreHub version : {corehub_version}",
        f"# Pipeline ID     : {pipeline_id}",
        f"# Pipeline name   : {pipeline_name}",
        f"# Exported at     : {exported_at} (UTC)",
        "# ---------------------------------------------------------------",
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

        jobs = fetch_pipeline_jobs(pipeline_id)
        attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

        # Backfill null column types via live discovery (legacy entity serialisation bug)
        enriched = enrich_null_column_types_from_discovery(schemas, entities, token, pipeline_id)
        if enriched:
            logger.info(f"Enriched null column types for {enriched} table(s) via live discovery")

        yaml_data = build_yaml_structure(schemas, groups_by_name)

        # Ensure destination directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        # Build provenance header and prepend it to the YAML
        header = build_export_header(token, pipeline_id)
        yaml_buffer = io.StringIO()
        yaml.safe_dump(yaml_data, yaml_buffer, sort_keys=False, allow_unicode=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(yaml_buffer.getvalue())

        log_success(logger, f"Export completed for pipeline {pipeline_id}. Output: {output_path}")

    except Exception as exc:  # pylint: disable=broad-except
        log_failure(logger, f"Export failed for pipeline {pipeline_id}: {exc}")
        raise


if __name__ == "__main__":
    main()

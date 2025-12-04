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
import json
import os
from typing import Any, Dict, List, Tuple

import yaml

from commons import fetch_core_hub
from utils.chronos_client import ChronosClient
from utils.log import create_log_file, get_logger, log_failure, log_success


log_file = create_log_file()
logger = get_logger(log_file)


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
    chronos_client = ChronosClient(os.getenv("CHRONOS_URL"))
    try:
        jobs = chronos_client._request(  # type: ignore[attr-defined]
            "api/jobs/", params={"pipeline_id": pipeline_id, "limit": 1000}
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"Failed to fetch Chronos jobs for pipeline {pipeline_id}: {exc}")
        return []

    if not isinstance(jobs, list):
        logger.warning(f"Unexpected Chronos jobs response format: {str(jobs)[:500]}")
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

    for ent in entities:
        entity_name = ent.get("entityName", "")
        if not entity_name:
            continue

        source_ae, target_ae = extract_source_and_target(ent)
        if not source_ae or not target_ae:
            logger.warning(f"Entity {entity_name} missing source/target agent entities. Skipping.")
            continue

        # Detect MultiTable vs SingleTable
        entity_type = source_ae.get("type") or target_ae.get("type")
        if entity_type == "MultiTable":
            _process_multitable_entity(ent, source_ae, target_ae, schemas, group_id_to_name)
        else:
            _process_single_entity(ent, source_ae, target_ae, schemas, group_id_to_name)

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

    target_schema = target_table.get("schema") or source_schema
    target_table_name = target_table.get("name") or source_table_name

    schema_cfg = schemas.get(source_schema)
    if not schema_cfg:
        schema_cfg = init_schema_cfg(target_schema)
        schemas[source_schema] = schema_cfg

    # Track whitelist tables (all tables that currently have entities)
    schema_cfg["tables"]["whitelist"].add(source_table_name)

    custom_tables: Dict[str, Any] = schema_cfg["tables"]["custom"]
    table_cfg = custom_tables.get(source_table_name)
    if not table_cfg:
        table_cfg = {}
        custom_tables[source_table_name] = table_cfg

    # Table name (target name / collection)
    table_cfg["name"] = target_table_name

    # Group mapping
    group_id = ent.get("groupId")
    if group_id and group_id != "_default":
        group_name = group_id_to_name.get(str(group_id), str(group_id))
        table_cfg["groupId"] = group_name

    # Keys (use source keys by column name)
    keys = []
    for key in source_ae.get("keys", []) or []:
        name = key.get("name")
        if name:
            keys.append(name)
    if keys:
        table_cfg["keys"] = keys

    # Columns mapping (source -> alias)
    column_mappings: List[Dict[str, str]] = []
    for col in source_ae.get("columns", []) or []:
        name = col.get("name")
        alias = col.get("alias") or name
        if name and alias and alias != name:
            column_mappings.append({name: alias})
    if column_mappings:
        table_cfg["columns"] = column_mappings

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

    # UDFs are stored on entityType, but YAML expects them in customProperties.target.udf
    udf_cfg = target_et.get("udf")
    if udf_cfg:
        target_cp["udf"] = udf_cfg

    # Snapshot concurrency: expose only when different from default 1
    snapshot_conc = target_et.get("snapshotWritingConcurrency")
    if isinstance(snapshot_conc, int) and snapshot_conc != 1:
        target_cp["snapshotWritingConcurrency"] = snapshot_conc

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

    # For each table in the chain, assign chainId and groupId
    for tbl in tables:
        table_name = tbl.get("name")
        if not table_name:
            continue

        schema_cfg["tables"]["whitelist"].add(table_name)

        table_cfg = custom_tables.get(table_name)
        if not table_cfg:
            table_cfg = {}
            custom_tables[table_name] = table_cfg

        # Basic naming: keep same name for target
        table_cfg["name"] = table_name
        table_cfg["chainId"] = chain_id
        if group_name:
            table_cfg["groupId"] = group_name


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
    groups_by_name: Dict[str, Dict[str, Any]] | None = None,
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export an existing GlueSync pipeline configuration from CoreHub/Chronos "
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

        jobs = fetch_pipeline_jobs(pipeline_id)
        attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

        yaml_data = build_yaml_structure(schemas, groups_by_name)

        # Ensure destination directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                yaml_data,
                f,
                sort_keys=False,
                allow_unicode=True,
            )

        log_success(logger, f"Export completed for pipeline {pipeline_id}. Output: {output_path}")

    except Exception as exc:  # pylint: disable=broad-except
        log_failure(logger, f"Export failed for pipeline {pipeline_id}: {exc}")
        raise


if __name__ == "__main__":
    main()

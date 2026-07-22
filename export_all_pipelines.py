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
import os
from typing import Any, Dict, List, Optional

from commons import fetch_core_hub
from export_template_from_corehub import (
    fetch_pipeline_entities,
    fetch_groups_map,
    fetch_pipeline_jobs,
    build_entities_maps,
    build_schemas_from_entities,
    attach_schedules_from_jobs,
    build_yaml_structure,
    infer_pipeline_schema_types_from_agents,
    fetch_trigger_flows,
    filter_trigger_flows_by_pipeline,
    export_trigger_flows_to_yaml,
    logger,
)
from utils.log import create_log_file, log_failure, log_success
import yaml


# Ensure logger has a log file (reuse same logger instance)
if not any(hasattr(h, 'baseFilename') for h in logger.handlers if hasattr(h, 'baseFilename')):
    log_file = create_log_file()
    logger.info(f"Using log file: {log_file}")


def list_pipelines(token: str) -> List[Dict[str, Any]]:
    """Return a list of pipelines from CoreHub.

    The API may return either a plain list of pipeline objects or a dict
    containing a list under a specific key. We try to normalize to a list
    of dicts with at least a pipelineId field.
    """
    response = fetch_core_hub('/pipelines', token=token)

    pipelines: List[Dict[str, Any]] = []
    if isinstance(response, list):
        pipelines = [p for p in response if isinstance(p, dict)]
    elif isinstance(response, dict):
        # Try common container keys
        for key in ('pipelines', 'items', 'data'):
            value = response.get(key)
            if isinstance(value, list):
                pipelines = [p for p in value if isinstance(p, dict)]
                break

    if not pipelines:
        logger.warning(f"No pipelines found or unexpected response format: {str(response)[:500]}")

    return pipelines


def export_single_pipeline(token: str, pipeline_id: str, output_dir: Optional[str] = None) -> Optional[str]:
    """Export a single pipeline configuration to YAML.

    Returns the output file path on success, or None on failure.
    """
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

        # Fetch and attach trigger flows referencing this pipeline
        all_flows = fetch_trigger_flows()
        pipeline_flows = filter_trigger_flows_by_pipeline(all_flows, pipeline_id)
        if pipeline_flows:
            exported_flows = export_trigger_flows_to_yaml(pipeline_flows, pipeline_id)
            if exported_flows:
                primary_schema = sorted(schemas.keys())[0]
                schemas[primary_schema]["trigger_flows"] = exported_flows
                logger.info(f"Attached {len(exported_flows)} trigger flow(s) to export")

        yaml_data = build_yaml_structure(schemas, groups_by_name)

        # Determine output path
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"backup_{pipeline_id}.yaml")
        else:
            output_path = f"./backup_{pipeline_id}.yaml"

        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                yaml_data,
                f,
                sort_keys=False,
                allow_unicode=True,
            )

        log_success(logger, f"Export completed for pipeline {pipeline_id}. Output: {output_path}")
        return output_path
    except Exception as exc:  # pylint: disable=broad-except
        log_failure(logger, f"Export failed for pipeline {pipeline_id}: {exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export all Gluesync pipelines from CoreHub into per-pipeline "
            "table-list-style YAML templates (backups)."
        )
    )
    parser.add_argument("--token", required=True, help="Authentication token for CoreHub")
    parser.add_argument(
        "--output-dir",
        help=(
            "Directory where backup YAML files will be written. "
            "Defaults to current directory using backup_<PIPELINE_ID>.yaml when not provided."
        ),
    )

    args = parser.parse_args()
    token = args.token
    output_dir = args.output_dir

    logger.info("Listing pipelines from CoreHub for bulk export")
    pipelines = list_pipelines(token)

    if not pipelines:
        log_failure(logger, "No pipelines found to export")
        return

    total = len(pipelines)
    logger.info(f"Found {total} pipeline(s) to export")

    successful = 0
    failed = 0

    for idx, pipe in enumerate(pipelines, start=1):
        pipeline_id = pipe.get('pipelineId') or pipe.get('id') or pipe.get('pipeline_id')
        name = pipe.get('name', pipeline_id)

        if not pipeline_id:
            logger.warning(f"Skipping pipeline entry without ID: {pipe}")
            failed += 1
            continue

        logger.info(f"[{idx}/{total}] Exporting pipeline {pipeline_id} ({name})")
        out = export_single_pipeline(token, str(pipeline_id), output_dir)
        if out:
            successful += 1
        else:
            failed += 1

    logger.info("Bulk export completed")
    logger.info(f"Successful: {successful}, Failed: {failed}, Total: {total}")


if __name__ == "__main__":
    main()

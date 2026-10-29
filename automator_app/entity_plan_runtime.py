# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
# 2. MOLO17 Commercial License
#
# Copyright (C) 2025 MOLO17. All rights reserved.

"""Apply an entity-import plan to a YAML file and build ImportOptions for create_all_entities.

Used by Automator ``/api/run``, Bulk create, and ``create_all_entities.py --plan``. Reuses
:mod:`import_plan` (no forked selection/override logic)."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

import import_options
import import_plan


def load_plan(source: Any) -> import_plan.ImportPlan:
    """:func:`import_plan.load_entity`; empty source → empty plan."""
    return import_plan.load_entity(source)


def prepare_yaml(
    yaml_config: Any,
    plan: import_plan.ImportPlan,
    pipeline_id: str,
) -> Tuple[Any, List[str], import_plan.PipelinePlan, Dict[str, bool]]:
    """Planned YAML, notes, pipeline plan and part flags. Empty plan leaves YAML unchanged."""
    planned, notes, pipeline = import_plan.apply_entity(yaml_config, plan, pipeline_id)
    return planned, notes, pipeline, import_plan.entity_part_flags(plan, pipeline)


def write_planned_yaml(planned: Any, *, suffix: str = ".yaml") -> Path:
    """Write ``planned`` to a temp file (caller deletes). Mode 0600."""
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8")
    try:
        yaml.safe_dump(planned, handle, sort_keys=False, allow_unicode=True)
        path = Path(handle.name)
    finally:
        handle.close()
    path.chmod(0o600)
    return path


def options_for_run(
    plan: import_plan.ImportPlan,
    pipeline: import_plan.PipelinePlan,
    *,
    dry_run: bool = False,
    base: Optional[import_options.ImportOptions] = None,
) -> import_options.ImportOptions:
    """ImportOptions (remap, on-existing, validate, dry-run) for the entity run."""
    return import_plan.entity_import_options(plan, pipeline, dry_run=dry_run, base=base)


def filter_table_names(table_names: List[str], plan: import_plan.ImportPlan, pipeline_id: str,
                       source_schema: str) -> List[str]:
    """Apply plan include/exclude to a Bulk table-name list (SCHEMA.TABLE ids)."""
    pipeline = import_plan.entity_pipeline(plan, pipeline_id)
    if pipeline.include is None and not pipeline.exclude:
        return list(table_names)
    ids = [f"{source_schema}.{name}" for name in table_names]
    # Build a minimal YAML so selected_ids / _matches work.
    yaml_config = {
        source_schema: {
            "tables": {"whitelist": list(table_names), "custom": {}},
        }
    }
    selected = set(i.casefold() for i in import_plan.selected_ids(yaml_config, pipeline))
    return [name for name, eid in zip(table_names, ids) if eid.casefold() in selected]


def apply_overrides_to_bulk_yaml(yaml_config: Any, plan: import_plan.ImportPlan,
                                 pipeline_id: str) -> Any:
    """Apply entity overrides / parts to a Bulk-generated YAML (deep copy when changed)."""
    planned, _notes, _pipeline = import_plan.apply_entity(yaml_config, plan, pipeline_id)
    return planned


def preview_payload(
    *,
    yaml_config: Any,
    plan: import_plan.ImportPlan,
    pipeline_id: str,
    existing_sources: Optional[set] = None,
) -> Dict[str, Any]:
    """Read-only preview dict: selected entities, parts, conflicts, notes. No secrets."""
    planned, notes, pipeline = import_plan.apply_entity(copy.deepcopy(yaml_config), plan, pipeline_id)
    entities = import_plan.entity_ids(planned)
    parts = import_plan.entity_part_flags(plan, pipeline)
    conflicts: List[str] = []
    if existing_sources:
        for entity in entities:
            key = (str(entity["schema"]).casefold(), str(entity["table"]).casefold())
            if key in existing_sources:
                conflicts.append(entity["id"])
    return {
        "pipelineId": str(pipeline_id),
        "entityCount": len(entities),
        "entities": [
            {
                "id": e["id"],
                "source": f'{e["schema"]}.{e["table"]}',
                "targetSchema": _target_schema(planned, e),
                "targetTable": _target_table(planned, e),
                "entityName": _entity_name(planned, e),
            }
            for e in entities
        ],
        "parts": parts,
        "onExistingEntity": plan.on_existing_entity(pipeline) or "default",
        "conflicts": conflicts,
        "notes": notes,
        "plan": plan.to_dict(),
        "dryRunSafe": True,
    }


def _schema_entry(planned: Any, entity: Dict[str, Any]) -> Dict[str, Any]:
    root = import_plan._schemas_root(planned)  # pylint: disable=protected-access
    return root.get(entity["schema"]) or {}


def _custom_cfg(planned: Any, entity: Dict[str, Any]) -> Dict[str, Any]:
    entry = _schema_entry(planned, entity)
    tables = entry.get("tables") if isinstance(entry.get("tables"), dict) else {}
    custom = tables.get("custom") if isinstance(tables.get("custom"), dict) else {}
    cfg = custom.get(entity["key"]) if isinstance(custom, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def _target_schema(planned: Any, entity: Dict[str, Any]) -> str:
    cfg = _custom_cfg(planned, entity)
    entry = _schema_entry(planned, entity)
    return str(cfg.get("targetSchema") or entry.get("targetSchema") or entry.get("target") or entity["schema"])


def _target_table(planned: Any, entity: Dict[str, Any]) -> str:
    cfg = _custom_cfg(planned, entity)
    return str(cfg.get("name") or entity["table"])


def _entity_name(planned: Any, entity: Dict[str, Any]) -> Optional[str]:
    cfg = _custom_cfg(planned, entity)
    name = cfg.get("entityName")
    return str(name) if name else None

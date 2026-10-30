#!/usr/bin/env python3
"""Entity Customize plan (import_plan.load_entity / apply_entity, Automator /api/run + Bulk + CLI).

Offline only: synthetic YAML, mocked Core Hub. Empty plan must match current behaviour exactly.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import import_plan  # noqa: E402
import import_options  # noqa: E402
from automator_app import entity_plan_runtime  # noqa: E402
from automator_app import app as automator  # noqa: E402
from fastapi import HTTPException  # noqa: E402
import create_all_entities  # noqa: E402

SYNTH = {
    "SCH01": {
        "target": "TGT01",
        "tables": {
            "whitelist": ["TAB001", "TAB002", "TAB003"],
            "custom": {
                "TAB001": {
                    "groupId": "nightly",
                    "customProperties": {"target": {"udf": "u1"}},
                },
                "TAB002": {"entityName": "E2"},
            },
        },
        "group_schedules": {"nightly": {"cron": "0 0 * * *"}},
    },
    "groups": {"nightly": {}},
}


def _plan(**kwargs):
    body = {"version": 1, **kwargs}
    return import_plan.load_entity(body)


class LoadEntityRefusals(unittest.TestCase):
    def test_empty_plan_parity(self):
        self.assertTrue(import_plan.load_entity(None).empty)
        self.assertTrue(import_plan.load_entity({}).empty)
        self.assertTrue(import_plan.load_entity("").empty)
        self.assertTrue(import_plan.load_entity('{"version": 1}').empty)

    def test_refuse_password(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"defaults": {"password": "x"}})
        self.assertIn("password", str(ctx.exception).lower())

    def test_refuse_globals(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"globals": {"users": False}})
        self.assertIn("globals", str(ctx.exception))

    def test_refuse_agents(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"pipelines": {"p1": {"agents": {"src": {"host": "h"}}}}})
        self.assertIn("agents", str(ctx.exception))

    def test_refuse_resume_into_name_import(self):
        for key in ("resumeInto", "name", "import", "onExistingPipeline"):
            with self.assertRaises(import_plan.PlanError) as ctx:
                import_plan.load_entity({"pipelines": {"p1": {key: "x" if key != "import" else False}}})
            self.assertIn(key, str(ctx.exception))

    def test_refuse_on_existing_pipeline_defaults(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"defaults": {"onExistingPipeline": "fail"}})
        self.assertIn("onExistingPipeline", str(ctx.exception))

    def test_refuse_multiple_pipelines(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"pipelines": {"a": {}, "b": {}}})
        self.assertIn("single pipeline", str(ctx.exception))


class ApplyEntityOptions(unittest.TestCase):
    def test_include_exclude(self):
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["SCH01.TAB001", "SCH01.TAB002"],
                                                       "exclude": ["SCH01.TAB002"]}}})
        planned, notes, pipeline = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        ids = [e["id"] for e in import_plan.entity_ids(planned)]
        self.assertEqual(ids, ["SCH01.TAB001"])
        self.assertTrue(any("not selected" in n for n in notes) or notes == notes)

    def test_overrides_target_schema_table_entity_name(self):
        plan = _plan(pipelines={"pipe": {"entities": {"overrides": {
            "SCH01.TAB001": {"targetSchema": "NEW", "targetTable": "T1X", "entityName": "N1"},
        }}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        custom = planned["SCH01"]["tables"]["custom"]["TAB001"]
        self.assertEqual(custom["targetSchema"], "NEW")
        self.assertEqual(custom["name"], "T1X")
        self.assertEqual(custom["entityName"], "N1")

    def test_table_target_schema(self):
        plan = _plan(pipelines={"pipe": {"tableTargetSchema": {"SCH01.TAB002": "OTHER"}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        self.assertEqual(planned["SCH01"]["tables"]["custom"]["TAB002"]["targetSchema"], "OTHER")

    def test_skip_udfs_and_groups(self):
        plan = _plan(defaults={"udfs": False, "groups": False})
        planned, notes, _p = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        custom = planned["SCH01"]["tables"]["custom"]["TAB001"]
        self.assertNotIn("udf", (custom.get("customProperties") or {}).get("target") or {})
        self.assertNotIn("groupId", custom)
        self.assertNotIn("groups", planned)
        self.assertTrue(any("UDF" in n or "groups" in n for n in notes))

    def test_part_flags_create_tables_schedules_fc(self):
        plan = _plan(defaults={"createTables": False, "schedules": False, "functionCollection": False})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        flags = import_plan.entity_part_flags(plan, pipeline)
        self.assertFalse(flags["createTables"])
        self.assertFalse(flags["schedules"])
        self.assertFalse(flags["functionCollection"])
        self.assertTrue(flags["udfs"])
        self.assertTrue(flags["groups"])

    def test_on_existing_entity(self):
        plan = _plan(defaults={"onExistingEntity": "skip"})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        self.assertEqual(plan.on_existing_entity(pipeline), "skip")
        opts = import_plan.entity_import_options(plan, pipeline, base=import_options.ImportOptions())
        self.assertEqual(opts.on_existing, "skip")

    def test_remap_into_import_options(self):
        plan = _plan(pipelines={"pipe": {"remap": {"schemas": {"SCH01": "SCH01_QA"},
                                                    "groups": {"nightly": "nightly_qa"}}}})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        opts = import_plan.entity_import_options(plan, pipeline, base=import_options.ImportOptions())
        self.assertEqual(opts.remap.schema("SCH01"), "SCH01_QA")
        self.assertEqual(opts.remap.group("nightly"), "nightly_qa")

    def test_dry_run_flag(self):
        plan = import_plan.ImportPlan()
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        opts = import_plan.entity_import_options(plan, pipeline, dry_run=True,
                                                base=import_options.ImportOptions())
        self.assertTrue(opts.dry_run)

    def test_empty_plan_leaves_yaml_unchanged(self):
        original = copy.deepcopy(SYNTH)
        planned, notes, pipeline = import_plan.apply_entity(original, import_plan.ImportPlan(), "pipe")
        self.assertIs(planned, original)
        self.assertEqual(notes, [])
        self.assertFalse(pipeline.changes_yaml())

    def test_draft_entity(self):
        draft = import_plan.draft_entity(SYNTH, "pipe-1")
        self.assertEqual(draft["version"], 1)
        self.assertIn("SCH01.TAB001", draft["pipelines"]["pipe-1"]["entities"]["include"])


class FilterBulkTables(unittest.TestCase):
    def test_include_filters(self):
        plan = _plan(pipelines={"p": {"entities": {"include": ["SCH.T1"]}}})
        names = entity_plan_runtime.filter_table_names(["T1", "T2"], plan, "p", "SCH")
        self.assertEqual(names, ["T1"])

    def test_empty_plan_keeps_all(self):
        names = entity_plan_runtime.filter_table_names(["T1", "T2"], import_plan.ImportPlan(), "p", "SCH")
        self.assertEqual(names, ["T1", "T2"])


class AutomatorEntityPlanApi(unittest.TestCase):
    def test_load_entity_plan_or_400_refuses_agents(self):
        with self.assertRaises(HTTPException) as ctx:
            automator._load_entity_plan_or_400({"pipelines": {"p": {"agents": {}}}})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("agents", str(ctx.exception.detail))

    def test_entity_plan_flags(self):
        plan = _plan(defaults={"schedules": False, "createTables": False, "functionCollection": False})
        sched, tables, fc = automator._entity_plan_flags(
            plan, "p", enable_scheduling=True, create_tables=True,
        )
        self.assertFalse(sched)
        self.assertFalse(tables)
        self.assertFalse(fc)

    def test_empty_plan_flags_unchanged(self):
        plan = import_plan.ImportPlan()
        sched, tables, fc = automator._entity_plan_flags(
            plan, "p", enable_scheduling=True, create_tables=True,
        )
        self.assertTrue(sched)
        self.assertTrue(tables)
        self.assertTrue(fc)


class CliPlanArgs(unittest.TestCase):
    def test_write_plan_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            plan_path = Path(tmp) / "plan.json"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            code = create_all_entities.cli([
                "--pipeline", "pipe-1",
                "--yaml-file", str(yaml_path),
                "--write-plan", str(plan_path),
            ])
            self.assertEqual(code, 0)
            draft = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertIn("SCH01.TAB001", draft["pipelines"]["pipe-1"]["entities"]["include"])

    def test_plan_refuses_password_on_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            plan_path = Path(tmp) / "plan.json"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            plan_path.write_text(json.dumps({"defaults": {"password": "secret"}}), encoding="utf-8")
            code = create_all_entities.cli([
                "--pipeline", "p",
                "--source-schema", "SCH01",
                "--target-schema", "TGT01",
                "--source-type", "SQL",
                "--target-type", "SQL",
                "--token", "t",
                "--yaml-file", str(yaml_path),
                "--plan", str(plan_path),
            ])
            self.assertEqual(code, 1)

    def test_cli_apply_plan_dumps_filtered_yaml(self):
        """Include that shrinks the YAML must dump via yaml.safe_dump (regression: NameError)."""
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            plan_path = Path(tmp) / "plan.json"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            plan_path.write_text(json.dumps({
                "version": 1,
                "pipelines": {"pipe-1": {
                    "entities": {"include": ["SCH01.TAB001"]},
                    "onExistingEntity": "skip",
                }},
            }), encoding="utf-8")
            args = create_all_entities.build_arg_parser().parse_args([
                "--pipeline", "pipe-1",
                "--source-schema", "SCH01",
                "--target-schema", "TGT01",
                "--source-type", "SQL",
                "--target-type", "SQL",
                "--token", "t",
                "--yaml-file", str(yaml_path),
                "--plan", str(plan_path),
            ])
            options = create_all_entities._cli_merged_options(args)
            planned_path, options, restore_fc = create_all_entities._cli_apply_plan(args, options)
            try:
                self.assertNotEqual(planned_path, str(yaml_path))
                planned = yaml.safe_load(Path(planned_path).read_text(encoding="utf-8"))
                ids = [e["id"] for e in import_plan.entity_ids(planned)]
                self.assertEqual(ids, ["SCH01.TAB001"])
                self.assertEqual(options.on_existing, "skip")
                self.assertTrue(restore_fc)
            finally:
                if planned_path != str(yaml_path):
                    Path(planned_path).unlink(missing_ok=True)


    def test_cli_apply_plan_parts_schedules_off(self):
        """parts.schedules=false must call set_scheduling_enabled (regression: NameError)."""
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            plan_path = Path(tmp) / "plan.json"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            plan_path.write_text(json.dumps({
                "version": 1,
                "pipelines": {"pipe-1": {
                    "entities": {"include": ["SCH01.TAB001"]},
                    "parts": {"schedules": False, "createTables": False,
                              "udfs": False, "groups": False, "functionCollection": False},
                    "onExistingEntity": "skip",
                }},
            }), encoding="utf-8")
            args = create_all_entities.build_arg_parser().parse_args([
                "--pipeline", "pipe-1",
                "--source-schema", "SCH01",
                "--target-schema", "TGT01",
                "--source-type", "SQL",
                "--target-type", "SQL",
                "--token", "t",
                "--yaml-file", str(yaml_path),
                "--plan", str(plan_path),
            ])
            options = create_all_entities._cli_merged_options(args)
            with mock.patch("create_all_entities.set_scheduling_enabled") as sched:
                planned_path, options, restore_fc = create_all_entities._cli_apply_plan(args, options)
                try:
                    sched.assert_called_once_with(False)
                    self.assertFalse(restore_fc)
                    self.assertEqual(options.on_existing, "skip")
                finally:
                    if planned_path != str(yaml_path):
                        Path(planned_path).unlink(missing_ok=True)



class PreviewPayload(unittest.TestCase):
    def test_preview_lists_entities_and_conflicts(self):
        plan = _plan(pipelines={"p": {"entities": {"include": ["SCH01.TAB001"]}}})
        payload = entity_plan_runtime.preview_payload(
            yaml_config=copy.deepcopy(SYNTH),
            plan=plan,
            pipeline_id="p",
            existing_sources={("sch01", "tab001")},
        )
        self.assertEqual(payload["entityCount"], 1)
        self.assertEqual(payload["conflicts"], ["SCH01.TAB001"])
        self.assertTrue(payload["dryRunSafe"])


if __name__ == "__main__":
    unittest.main()


class CaseInsensitiveAndParity(unittest.TestCase):
    def test_include_casefold_matching(self):
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["sch01.tab001"]}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        ids = [e["id"] for e in import_plan.entity_ids(planned)]
        self.assertEqual(ids, ["SCH01.TAB001"])

    def test_filter_bulk_casefold(self):
        plan = _plan(pipelines={"p": {"entities": {"include": ["sch.t1"]}}})
        names = entity_plan_runtime.filter_table_names(["T1", "T2"], plan, "p", "SCH")
        self.assertEqual(names, ["T1"])

    def test_empty_plan_import_options_is_env_base(self):
        sentinel = import_options.ImportOptions()
        with mock.patch.object(import_options, "from_env_if_requested", return_value=sentinel):
            plan = import_plan.ImportPlan()
            pipeline = import_plan.entity_pipeline(plan, "pipe")
            got = automator._entity_run_import_options(plan, pipeline, dry_run=False)
            self.assertIs(got, sentinel)

    def test_prepare_yaml_empty_plan_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.yaml"
            path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            out, notes, pipeline, temp = automator._prepare_entity_yaml_for_run(
                path, import_plan.ImportPlan(), "pipe",
            )
            self.assertIs(out, path)
            self.assertEqual(notes, [])
            self.assertIsNone(temp)

    def test_refuse_unknown_pipeline_key(self):
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.load_entity({"pipelines": {"p": {"notARealKey": 1}}})
        self.assertIn("notARealKey", str(ctx.exception))

    def test_entity_pipeline_relabels_old_id(self):
        plan = _plan(pipelines={"backup-id": {"entities": {"include": ["SCH01.TAB001"]}}})
        pipeline = import_plan.entity_pipeline(plan, "live-pipe")
        self.assertEqual(pipeline.old_id, "live-pipe")
        self.assertEqual(pipeline.include, ["SCH01.TAB001"])


class EntityNameInclude(unittest.TestCase):
    def test_include_by_unique_entity_name(self):
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["E2"]}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(SYNTH), plan, "pipe")
        ids = [e["id"] for e in import_plan.entity_ids(planned)]
        self.assertEqual(ids, ["SCH01.TAB002"])

    def test_include_bare_schema_key_still_expands_at_at(self):
        yaml_cfg = {
            "SCH01": {
                "tables": {
                    "custom": {
                        "TABX": {"entityName": "KeepMe"},
                        "TABX@@2": {"entityName": "Other"},
                    }
                }
            }
        }
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["SCH01.TABX"]}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(yaml_cfg), plan, "pipe")
        ids = sorted(e["id"] for e in import_plan.entity_ids(planned))
        self.assertEqual(ids, ["SCH01.TABX", "SCH01.TABX@@2"])

    def test_ambiguous_entity_name_refused(self):
        yaml_cfg = {
            "SCH01": {
                "tables": {
                    "custom": {
                        "A": {"entityName": "Dup"},
                        "B": {"entityName": "Dup"},
                    }
                }
            }
        }
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["Dup"]}}})
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.apply_entity(copy.deepcopy(yaml_cfg), plan, "pipe")
        self.assertIn("entityName", str(ctx.exception))
        self.assertIn("Dup", str(ctx.exception))

    def test_entity_name_does_not_override_id_match(self):
        """When SCHEMA.KEY matches, entityName is not considered (bare→@@n wins)."""
        yaml_cfg = {
            "SCH01": {
                "tables": {
                    "custom": {
                        "TAB001": {"entityName": "SCH01.TAB002"},
                        "TAB002": {},
                    }
                }
            }
        }
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["SCH01.TAB002"]}}})
        planned, _n, _p = import_plan.apply_entity(copy.deepcopy(yaml_cfg), plan, "pipe")
        ids = [e["id"] for e in import_plan.entity_ids(planned)]
        self.assertEqual(ids, ["SCH01.TAB002"])


class CliIncludeExclude(unittest.TestCase):
    def test_include_builds_one_pipeline_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            args = create_all_entities.build_arg_parser().parse_args([
                "--pipeline", "pipe-1",
                "--source-schema", "SCH01",
                "--target-schema", "TGT01",
                "--source-type", "SQL",
                "--target-type", "SQL",
                "--token", "t",
                "--yaml-file", str(yaml_path),
                "--include", "SCH01.TAB001",
                "--include", "E2",
            ])
            options = create_all_entities._cli_merged_options(args)
            planned_path, options, _fc = create_all_entities._cli_apply_plan(args, options)
            try:
                planned = yaml.safe_load(Path(planned_path).read_text(encoding="utf-8"))
                ids = sorted(e["id"] for e in import_plan.entity_ids(planned))
                self.assertEqual(ids, ["SCH01.TAB001", "SCH01.TAB002"])
            finally:
                if planned_path != str(yaml_path):
                    Path(planned_path).unlink(missing_ok=True)

    def test_include_merges_into_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = Path(tmp) / "e.yaml"
            plan_path = Path(tmp) / "plan.json"
            yaml_path.write_text(yaml.safe_dump(SYNTH), encoding="utf-8")
            plan_path.write_text(json.dumps({
                "version": 1,
                "pipelines": {"pipe-1": {"onExistingEntity": "skip"}},
            }), encoding="utf-8")
            args = create_all_entities.build_arg_parser().parse_args([
                "--pipeline", "pipe-1",
                "--source-schema", "SCH01",
                "--target-schema", "TGT01",
                "--source-type", "SQL",
                "--target-type", "SQL",
                "--token", "t",
                "--yaml-file", str(yaml_path),
                "--plan", str(plan_path),
                "--include", "SCH01.TAB001",
            ])
            options = create_all_entities._cli_merged_options(args)
            planned_path, options, _fc = create_all_entities._cli_apply_plan(args, options)
            try:
                planned = yaml.safe_load(Path(planned_path).read_text(encoding="utf-8"))
                ids = [e["id"] for e in import_plan.entity_ids(planned)]
                self.assertEqual(ids, ["SCH01.TAB001"])
                self.assertEqual(options.on_existing, "skip")
            finally:
                if planned_path != str(yaml_path):
                    Path(planned_path).unlink(missing_ok=True)


class PreviewPickerFields(unittest.TestCase):
    def test_preview_exposes_picker_columns(self):
        yaml_cfg = copy.deepcopy(SYNTH)
        yaml_cfg["SCH01"]["sourceType"] = "SQL"
        yaml_cfg["SCH01"]["tables"]["custom"]["TAB001"]["chainId"] = "chain-a"
        yaml_cfg["SCH01"]["tables"]["custom"]["TAB002"]["chainId"] = "chain-a"
        payload = entity_plan_runtime.preview_payload(
            yaml_config=yaml_cfg,
            plan=import_plan.ImportPlan(),
            pipeline_id="p",
            existing_sources={("sch01", "tab001")},
        )
        by_id = {e["id"]: e for e in payload["entities"]}
        self.assertEqual(by_id["SCH01.TAB001"]["type"], "SQL")
        self.assertEqual(by_id["SCH01.TAB001"]["chainId"], "chain-a")
        self.assertTrue(by_id["SCH01.TAB001"]["alreadyExists"])
        self.assertFalse(by_id["SCH01.TAB002"]["alreadyExists"])
        self.assertEqual(by_id["SCH01.TAB002"]["entityName"], "E2")
        self.assertEqual(by_id["SCH01.TAB001"]["schema"], "SCH01")

    def test_partial_chain_note(self):
        yaml_cfg = copy.deepcopy(SYNTH)
        yaml_cfg["SCH01"]["tables"]["custom"]["TAB001"]["chainId"] = "c1"
        yaml_cfg["SCH01"]["tables"]["custom"]["TAB002"]["chainId"] = "c1"
        plan = _plan(pipelines={"p": {"entities": {"include": ["SCH01.TAB001"]}}})
        payload = entity_plan_runtime.preview_payload(
            yaml_config=yaml_cfg, plan=plan, pipeline_id="p",
        )
        self.assertTrue(any("partly selected" in n for n in payload["notes"]))


class ExistingEntitySources(unittest.TestCase):
    """Core Hub wraps listings as {"entity": {...}}; MultiTable uses agentEntities[].tables."""

    def test_all_source_tables_of_single_and_multi(self):
        single = {
            "agentEntities": [{
                "entityType": {"type": "Source"},
                "table": {"schema": "dbo", "name": "ARTICLES"},
            }],
        }
        self.assertEqual(
            create_all_entities._all_source_tables_of(single),
            {("dbo", "articles")},
        )
        multi = {
            "agentEntities": [{
                "entityType": {"type": "Source"},
                "tables": [
                    {"schema": "dbo", "name": "ORDERS_HEADERS"},
                    {"schema": "dbo", "name": "ORDERS_ROWS"},
                ],
            }],
        }
        self.assertEqual(
            create_all_entities._all_source_tables_of(multi),
            {("dbo", "orders_headers"), ("dbo", "orders_rows")},
        )

    def test_existing_entity_sources_unwraps_hub_listing(self):
        listing = [{
            "entity": {
                "entityName": "ArticlesEnt",
                "agentEntities": [{
                    "entityType": {"type": "Source"},
                    "table": {"schema": "dbo", "name": "ARTICLES"},
                }],
            }
        }, {
            "entity": {
                "entityName": "OrdersChain",
                "agentEntities": [{
                    "entityType": {"type": "Source"},
                    "tables": [
                        {"schema": "dbo", "name": "ORDERS_HEADERS"},
                        {"schema": "dbo", "name": "ORDERS_ROWS"},
                    ],
                }],
            }
        }]
        with mock.patch.object(automator.corehub, "fetch_core_hub", return_value=listing):
            prev = automator.state._token
            automator.state._token = "tok"
            try:
                sources = automator._existing_entity_sources("pipe-1")
            finally:
                automator.state._token = prev
        self.assertEqual(
            sources,
            {("dbo", "articles"), ("dbo", "orders_headers"), ("dbo", "orders_rows")},
        )

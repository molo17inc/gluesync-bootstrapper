#!/usr/bin/env python3
"""Regression tests for code-review F-ids on Customize / import plan (offline, synthetic names)."""

from __future__ import annotations

import copy
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

import create_all_tables  # noqa: E402
import import_options  # noqa: E402
import import_plan  # noqa: E402
from automator_app import app as automator  # noqa: E402
from automator_app import entity_plan_runtime  # noqa: E402


def _plan(**kwargs):
    return import_plan.load_entity({"version": 1, **kwargs})


MULTI = {
    "HR": {
        "target": "HR_TGT",
        "tables": {"whitelist": ["ORDERS", "ITEMS"], "custom": {}},
    },
    "FIN": {
        "target": "FIN_TGT",
        "tables": {"whitelist": ["ORDERS"], "custom": {}},
    },
}


class F1F4FinalTargetCollisions(unittest.TestCase):
    def test_schema_only_remap_collides(self):
        plan = _plan(pipelines={"p1": {"entities": {"overrides": {
            "HR.ORDERS": {"targetSchema": "APP"},
            "FIN.ORDERS": {"targetSchema": "APP"},
        }}}})
        pipeline = import_plan.entity_pipeline(plan, "p1")
        with self.assertRaisesRegex(import_plan.PlanError, r"both map to APP\.ORDERS"):
            import_plan.check_target_collisions(MULTI, pipeline, "p1")

    def test_advanced_include_still_collides(self):
        plan = _plan(pipelines={"p1": {"entities": {
            "include": ["HR.ORDERS", "FIN.ORDERS"],
            "overrides": {
                "HR.ORDERS": {"targetSchema": "APP"},
                "FIN.ORDERS": {"targetSchema": "APP"},
            },
        }}})
        pipeline = import_plan.entity_pipeline(plan, "p1")
        with self.assertRaisesRegex(import_plan.PlanError, "both map to"):
            import_plan.check_target_collisions(MULTI, pipeline, "p1")

    def test_exclude_avoids_collision(self):
        plan = _plan(pipelines={"p1": {"entities": {
            "exclude": ["FIN.ORDERS"],
            "overrides": {
                "HR.ORDERS": {"targetSchema": "APP"},
                "FIN.ORDERS": {"targetSchema": "APP"},
            },
        }}})
        pipeline = import_plan.entity_pipeline(plan, "p1")
        import_plan.check_target_collisions(MULTI, pipeline, "p1")

    def test_prepare_yaml_refuses_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.yaml"
            path.write_text(yaml.safe_dump(MULTI), encoding="utf-8")
            plan = _plan(pipelines={"pipe": {"entities": {"overrides": {
                "HR.ORDERS": {"targetSchema": "APP"},
                "FIN.ORDERS": {"targetSchema": "APP"},
            }}}})
            with self.assertRaises(import_plan.PlanError):
                automator._prepare_entity_yaml_for_run(path, plan, "pipe")

    def test_at_at_siblings_may_share_final_target(self):
        yaml_cfg = {
            "SCH01": {
                "target": "TGT",
                "tables": {"custom": {
                    "TABX": {"entityName": "KeepMe"},
                    "TABX@@2": {"entityName": "Other"},
                }},
            }
        }
        plan = _plan(pipelines={"pipe": {"entities": {
            "include": ["SCH01.TABX"],
            "overrides": {
                "SCH01.TABX": {"targetSchema": "APP"},
                "SCH01.TABX@@2": {"targetSchema": "APP"},
            },
        }}})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        import_plan.check_target_collisions(yaml_cfg, pipeline, "pipe")

    def test_preview_notes_multi_schema(self):
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["HR.ORDERS", "FIN.ORDERS"]}}})
        preview = entity_plan_runtime.preview_payload(
            yaml_config=MULTI, plan=plan, pipeline_id="pipe", existing_sources=set(),
        )
        self.assertTrue(any("multiple source schemas" in n for n in preview["notes"]))


class F2MultiSchemaSourceExpand(unittest.TestCase):
    def _yaml(self, tmp: Path) -> Path:
        path = tmp / "entities.yaml"
        path.write_text(yaml.safe_dump({
            "SRC_A": {"target": "TGT", "tables": {"whitelist": ["ORDERS"]}},
            "SRC_B": {"target": "TGT", "tables": {"whitelist": ["ORDERS"]}},
        }), encoding="utf-8")
        return path

    def _request(self, *, source: str, target: str):
        return automator.RunRequest.model_validate({
            "pipelineId": "pipe",
            "sourceSchema": source,
            "targetSchema": target,
            "sourceType": "SQL",
            "targetType": "SQL",
            "autoSchemas": False,
            "yamlFileId": "yaml-1",
            "skipErrors": False,
            "chunkSize": 50,
        })

    def _settings(self, yaml_path: Path):
        return automator._RunSettings(
            token="t",
            base_url="http://example",
            enable_scheduling=False,
            create_tables=False,
            use_ssl=False,
            skip_verify=True,
            yaml_file=str(yaml_path),
            source_type="SQL",
            target_type="SQL",
            log_callback=None,
        )

    def test_source_set_with_plan_expands_other_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = self._yaml(Path(tmp))
            request = self._request(source="SRC_A", target="GLUESYNC")
            settings = self._settings(yaml_path)
            calls = []

            def fake_create(req, sett, src, tgt):
                calls.append((src, tgt))
                return {"success": True, "logs": [], "error": None}

            plan = _plan(pipelines={"pipe": {"entities": {
                "include": ["SRC_A.ORDERS", "SRC_B.ORDERS"],
            }}})
            with mock.patch.object(automator, "_run_create_entities", side_effect=fake_create), \
                 mock.patch.object(automator, "_run_schema_types", return_value=("SQL", "SQL")):
                result = automator._run_entity_creation(
                    request, settings, yaml_path, plan=plan,
                )
            self.assertTrue(result["success"])
            self.assertEqual(sorted(calls), [("SRC_A", "GLUESYNC"), ("SRC_B", "GLUESYNC")])

    def test_source_set_without_plan_keeps_single_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_path = self._yaml(Path(tmp))
            request = self._request(source="SRC_A", target="")
            settings = self._settings(yaml_path)
            calls = []

            def fake_create(req, sett, src, tgt):
                calls.append((src, tgt))
                return {"success": True, "logs": [], "error": None}

            with mock.patch.object(automator, "_run_create_entities", side_effect=fake_create), \
                 mock.patch.object(automator, "_run_schema_types", return_value=("SQL", "SQL")):
                result = automator._run_entity_creation(
                    request, settings, yaml_path, plan=import_plan.ImportPlan(),
                )
            self.assertTrue(result["success"])
            self.assertEqual(calls, [("SRC_A", "TGT")])


class F6CaseFoldTableExists(unittest.TestCase):
    def test_alternates_dedupe(self):
        self.assertEqual(create_all_tables._case_fold_alternates("ORDERS"), ["ORDERS", "orders"])
        self.assertEqual(create_all_tables._case_fold_alternates("orders"), ["orders", "ORDERS"])
        self.assertEqual(create_all_tables._case_fold_alternates("Orders"), ["Orders", "orders", "ORDERS"])

    def test_schema_and_table_case_flipped(self):
        tried = []

        def fake_fetch(url, **_kwargs):
            tried.append(url)
            if url.endswith("/schemas/HR/tables/ORDERS"):
                return {"ok": True}
            raise RuntimeError("404")

        with mock.patch.object(create_all_tables, "fetch_core_hub", side_effect=fake_fetch):
            self.assertTrue(create_all_tables.table_exists("p", "hr", "Orders", "tok"))
        self.assertTrue(any("/schemas/HR/tables/ORDERS" in u for u in tried))
        self.assertTrue(any("/schemas/hr/" in u for u in tried))


class F10PlanPartsSourceOfTruth(unittest.TestCase):
    def test_nonempty_plan_ignores_global_create_tables_false(self):
        plan = _plan(defaults={"createTables": True, "schedules": True})
        sched, tables, fc = automator._entity_plan_flags(
            plan, "p", enable_scheduling=False, create_tables=False,
        )
        self.assertTrue(sched)
        self.assertTrue(tables)
        self.assertTrue(fc)

    def test_empty_plan_uses_global_flags(self):
        plan = import_plan.ImportPlan()
        sched, tables, fc = automator._entity_plan_flags(
            plan, "p", enable_scheduling=False, create_tables=True,
        )
        self.assertFalse(sched)
        self.assertTrue(tables)
        self.assertTrue(fc)


class F12EnvOnExistingCleared(unittest.TestCase):
    def test_nonempty_plan_without_on_existing_clears_env(self):
        plan = _plan(pipelines={"pipe": {"entities": {"include": ["HR.ORDERS"]}}})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        base = import_options.ImportOptions(on_existing="skip")
        opts = import_plan.entity_import_options(plan, pipeline, base=base)
        self.assertIsNone(opts.on_existing)

    def test_empty_plan_keeps_env_base(self):
        plan = import_plan.ImportPlan()
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        base = import_options.ImportOptions(on_existing="fail")
        opts = import_plan.entity_import_options(plan, pipeline, base=base)
        self.assertEqual(opts.on_existing, "fail")

    def test_explicit_plan_value_still_wins(self):
        plan = _plan(defaults={"onExistingEntity": "update"})
        pipeline = import_plan.entity_pipeline(plan, "pipe")
        base = import_options.ImportOptions(on_existing="fail")
        opts = import_plan.entity_import_options(plan, pipeline, base=base)
        self.assertEqual(opts.on_existing, "update")


class F13BulkSkipErrorsDefault(unittest.TestCase):
    def test_bulk_skip_errors_defaults_false(self):
        req = automator.BulkCreateRequest.model_validate({
            "pipelineId": "p",
            "sourceSchema": "S",
            "targetSchema": "T",
            "tableNames": ["X"],
        })
        self.assertFalse(req.skip_errors)


class F5UpdateWarning(unittest.TestCase):
    def test_update_logs_unverified_warning(self):
        # Smoke: the warning string is present in create_all_entities
        text = Path(ROOT / "create_all_entities.py").read_text(encoding="utf-8")
        self.assertIn("Core Hub update contract is unverified", text)


if __name__ == "__main__":
    unittest.main()

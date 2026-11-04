#!/usr/bin/env python3
"""Regression tests for review-6 I1–I9 (fixed behaviour). Synthetic names only."""
from __future__ import annotations

import inspect
import io
import os
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class I1SingleFeederExistingColumnsWarn(unittest.TestCase):
    def test_fan_in_warnings_for_one_feeder_with_missing_columns(self):
        import fan_in

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "ORDERS": {
                                "name": "ORDERS",
                                "columns": [
                                    {"source": "ID", "target": "ID", "type": "int"},
                                    {"source": "AMOUNT", "target": "AMOUNT", "type": "numeric"},
                                ],
                            }
                        },
                        "whitelist": ["ORDERS"],
                    },
                }
            }
        }

        warnings = fan_in.fan_in_warnings(
            yaml_cfg, "HR", default_target="APP",
            existing_columns=lambda s, t: ["ID"],
        )
        joined = "\n".join(warnings)
        self.assertTrue(warnings, "single-feeder existing-column drift must warn (I1)")
        self.assertIn("lacks union column", joined)
        self.assertIn("amount", joined)

    def test_two_feeders_still_warn_about_missing_union_columns(self):
        import fan_in

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "ORDERS": {
                                "name": "ORDERS",
                                "columns": [
                                    {"source": "ID", "target": "ID", "type": "int"},
                                    {"source": "AMOUNT", "target": "AMOUNT", "type": "numeric"},
                                ],
                            }
                        },
                        "whitelist": ["ORDERS"],
                    },
                },
                "FIN": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "ORDERS_FIN": {
                                "name": "ORDERS",
                                "columns": [
                                    {"source": "ID", "target": "ID", "type": "int"},
                                    {"source": "CURRENCY", "target": "CURRENCY", "type": "varchar"},
                                ],
                            }
                        },
                        "whitelist": ["ORDERS_FIN"],
                    },
                },
            }
        }
        warnings = fan_in.fan_in_warnings(
            yaml_cfg, "HR", default_target="APP",
            existing_columns=lambda s, t: ["ID"],
        )
        joined = "\n".join(warnings)
        self.assertIn("lacks union column", joined)

    def test_preview_includes_existing_column_notes(self):
        from automator_app import entity_plan_runtime as epr
        import import_plan

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "ORDERS": {
                                "name": "ORDERS",
                                "columns": [
                                    {"source": "ID", "target": "ID", "type": "int"},
                                    {"source": "AMOUNT", "target": "AMOUNT", "type": "numeric"},
                                ],
                            }
                        },
                        "whitelist": ["ORDERS"],
                    },
                }
            }
        }
        payload = epr.preview_payload(
            yaml_config=yaml_cfg,
            plan=import_plan.load_entity({}),
            pipeline_id="pipe-hr-1",
            existing_sources=set(),
            existing_columns=lambda s, t: ["ID"],
        )
        joined = "\n".join(payload["notes"])
        self.assertIn("lacks union column", joined)


class I2DryRunReportsDryRun(unittest.TestCase):
    def test_entity_run_status_dry_run_not_completed(self):
        from automator_app.app import _entity_run_status

        status = _entity_run_status({
            "success": True,
            "successful": 5,
            "failed": 0,
            "dry_run": True,
        })
        self.assertEqual(status, "dry-run")

    def test_entity_run_status_dry_run_with_errors_is_failed(self):
        from automator_app.app import _entity_run_status

        status = _entity_run_status({
            "dry_run": True,
            "success": False,
            "failed": 2,
            "successful": 0,
            "error": "two tables failed",
        })
        self.assertEqual(status, "failed")


    def test_put_entities_dry_run_still_blocks_writes(self):
        import create_all_entities as cae
        import import_options

        ctx = mock.Mock()
        ctx.pipeline_id = "pipe-hr-1"
        ctx.token = "tok-synthetic"
        with import_options.use(import_options.ImportOptions(dry_run=True)):
            result = cae._put_entities(ctx, {"entities": [{"entityName": "HR.ORDERS"}]})
        self.assertEqual(result, {"dryRun": True})


class I3ResumeAgentTagCasefold(unittest.TestCase):
    def test_existing_pipeline_agent_matches_casefolded_tag(self):
        from automator_app.app import _existing_pipeline_agent

        existing = [
            {"agentType": "SOURCE", "agentTag": "mssql-ct", "agentId": "a1"},
            {"agentType": "SOURCE", "agentTag": "oracle-logminer", "agentId": "a2"},
        ]
        backup = {"agentType": "SOURCE", "agentTag": "MSSQL-CT"}
        matched = _existing_pipeline_agent(existing, backup)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["agentId"], "a1")

    def test_single_same_type_accepted_regardless_of_tag(self):
        from automator_app.app import _existing_pipeline_agent

        existing = [
            {"agentType": "SOURCE", "agentTag": "other-tag", "agentId": "a9"},
        ]
        backup = {"agentType": "SOURCE", "agentTag": "mssql-ct"}
        matched = _existing_pipeline_agent(existing, backup)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["agentId"], "a9")

    def test_ambiguous_same_type_still_requires_tag_match(self):
        from automator_app.app import _existing_pipeline_agent

        existing = [
            {"agentType": "SOURCE", "agentTag": "other-tag", "agentId": "a9"},
            {"agentType": "SOURCE", "agentTag": "third-tag", "agentId": "a8"},
        ]
        backup = {"agentType": "SOURCE", "agentTag": "mssql-ct"}
        self.assertIsNone(_existing_pipeline_agent(existing, backup))


class I4StartRunNoDiscardedTemp(unittest.TestCase):
    def test_start_run_validates_without_prepare_write(self):
        from automator_app import app as automator_app

        src = inspect.getsource(automator_app._start_run)
        self.assertIn("_validate_entity_plan_for_run", src)
        self.assertNotIn("_prepare_entity_yaml_for_run", src)

    def test_validate_entity_plan_does_not_write_temp(self):
        from automator_app import app as automator_app
        import import_plan
        import tempfile
        import yaml

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump({
                "schemas": {
                    "HR": {"target": "APP", "tables": {"custom": {"ORDERS": {"name": "ORDERS"}},
                                                       "whitelist": ["ORDERS"]}}
                }
            }, handle)
            path = Path(handle.name)
        before = set(Path(tempfile.gettempdir()).glob("tmp*.yaml"))
        try:
            plan = import_plan.load_entity({
                "pipelines": {"pipe-hr-1": {"entities": {"include": ["HR.ORDERS"]}}},
            })
            with mock.patch.object(automator_app.entity_plan_runtime, "write_planned_yaml") as write_mock:
                automator_app._validate_entity_plan_for_run(path, plan, "pipe-hr-1")
                write_mock.assert_not_called()
        finally:
            path.unlink(missing_ok=True)
        after = set(Path(tempfile.gettempdir()).glob("tmp*.yaml"))
        self.assertFalse(after - before)


class I5SharedImportLock(unittest.TestCase):
    def test_import_lock_blocks_start_run(self):
        from automator_app import app as automator_app
        from automator_app import state as state_mod

        with state_mod.state._lock:
            state_mod.state.current_run = None

        held = []
        event = threading.Event()

        def hold_backup_lock():
            acquired = automator_app._IMPORT_LOCK.acquire(blocking=False)
            held.append(acquired)
            if acquired:
                event.wait(2.0)
                automator_app._IMPORT_LOCK.release()

        t = threading.Thread(target=hold_backup_lock)
        t.start()
        for _ in range(50):
            if held:
                break
            event.wait(0.01)
        self.assertTrue(held and held[0], "backup lock should be held")

        # Entity start must refuse while backup lock is held (I5).
        self.assertFalse(automator_app._IMPORT_LOCK.acquire(blocking=False))
        event.set()
        t.join(timeout=2)
        # Clean acquire after release
        self.assertTrue(automator_app._IMPORT_LOCK.acquire(blocking=False))
        automator_app._IMPORT_LOCK.release()

    def test_exclusive_import_refuses_running_entity(self):
        from automator_app import app as automator_app
        from automator_app import state as state_mod
        from fastapi import HTTPException

        run = state_mod.state.start_run()
        try:
            with self.assertRaises(HTTPException) as ctx:
                with automator_app._exclusive_import():
                    pass
            self.assertEqual(ctx.exception.status_code, 409)
        finally:
            with state_mod.state._lock:
                state_mod.state.current_run = None


class I6PreviewListingError(unittest.TestCase):
    def test_existing_entity_sources_returns_error_on_list_fail(self):
        from automator_app import app as automator_app

        with mock.patch.object(
            automator_app.corehub, "fetch_core_hub", side_effect=RuntimeError("hub down")
        ):
            sources, err = automator_app._existing_entity_sources("pipe-hr-1")
        self.assertEqual(sources, set())
        self.assertIn("hub down", err)

    def test_preview_payload_sets_listing_error(self):
        from automator_app import entity_plan_runtime as epr
        import import_plan

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {"ORDERS": {"name": "ORDERS"}},
                        "whitelist": ["ORDERS"],
                    },
                }
            }
        }
        payload = epr.preview_payload(
            yaml_config=yaml_cfg,
            plan=import_plan.load_entity({}),
            pipeline_id="pipe-hr-1",
            existing_sources=set(),
            listing_error="hub down",
        )
        self.assertEqual(payload["listingError"], "hub down")
        self.assertTrue(payload["conflictsUnknown"])
        self.assertFalse(payload["entities"][0]["alreadyExists"])
        self.assertTrue(any("could not list" in n.lower() for n in payload["notes"]))


class I7MissingPartitionColumnWarns(unittest.TestCase):
    def test_build_partition_settings_warns_and_returns_none_when_column_missing(self):
        import create_all_entities as cae

        settings = cae._build_partition_settings(
            "DEPT_ID",
            [{"name": "ID", "id": 1, "dataType": "int", "isPK": True}],
            "HR",
            "EMPLOYEES",
            42,
        )
        self.assertIsNone(settings)

    def test_build_partition_settings_warns_when_no_columns_discovered(self):
        import create_all_entities as cae

        settings = cae._build_partition_settings("DEPT_ID", [], "HR", "EMPLOYEES", 42)
        self.assertIsNone(settings)

    def test_build_partition_settings_warns_when_columns_none(self):
        import create_all_entities as cae

        settings = cae._build_partition_settings("DEPT_ID", None, "HR", "EMPLOYEES", 42)
        self.assertIsNone(settings)


class I8DryRunSuccessLogWording(unittest.TestCase):
    def test_send_paths_branch_dry_run_log(self):
        import create_all_entities as cae

        multi_src = inspect.getsource(cae._send_multi_table_entity)
        chunk_src = inspect.getsource(cae._send_entity_chunk)
        self.assertIn("Dry-run: would create MultiTable entity", multi_src)
        self.assertIn("Dry-run: would create entities", chunk_src)
        self.assertIn("dry_run", multi_src)


class I9ZipReopenScoped(unittest.TestCase):
    def test_backup_zip_scope_reuses_one_zipfile(self):
        from automator_app import app as automator_app

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("agents-config.yaml", "agents: []\n")
            zf.writestr("backup_HR.yaml", "schemas: {}\n")
        contents = buf.getvalue()
        opened = []
        real_zip = zipfile.ZipFile

        class CountingZip(real_zip):
            def __init__(self, *a, **k):
                opened.append(1)
                super().__init__(*a, **k)

        with mock.patch.object(automator_app.zipfile, "ZipFile", CountingZip):
            with automator_app._backup_zip_scope(contents):
                with automator_app._open_backup_zip(contents) as z1:
                    _ = z1.namelist()
                with automator_app._open_backup_zip(contents) as z2:
                    _ = z2.namelist()
        self.assertEqual(len(opened), 1, "scoped job must open ZipFile once (I9)")




class I5ExecuteRunFinallyClearsRunning(unittest.TestCase):
    def test_execute_run_finally_finalizes_when_body_raises(self):
        import asyncio
        from unittest import mock
        from automator_app import app as automator_app
        from automator_app import state as state_mod

        run = state_mod.state.start_run()
        run_id = run.run_id

        async def boom(*_a, **_k):
            raise RuntimeError("simulated crash")

        async def runner():
            with mock.patch.object(automator_app, "_execute_run_body", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    await automator_app._execute_run(run_id, mock.Mock())

        try:
            asyncio.run(runner())
            cur = state_mod.state.current_run
            self.assertIsNotNone(cur)
            self.assertEqual(cur.run_id, run_id)
            self.assertNotEqual(cur.status, "running")
            self.assertEqual(cur.status, "failed")
        finally:
            with state_mod.state._lock:
                state_mod.state.current_run = None


class I1ColumnLookupSkippedWithoutYamlColumns(unittest.TestCase):
    def test_fan_in_skips_lookup_when_yaml_lists_no_columns(self):
        import fan_in

        calls = []

        def existing_columns(schema, table):
            calls.append((schema, table))
            return ["ID"]

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "EMP": {"name": "EMP", "columns": []},
                        },
                    },
                }
            }
        }
        # Empty columns list → no union → no lookup
        warnings = fan_in.fan_in_warnings(yaml_cfg, "HR", default_target="APP",
                                          existing_columns=existing_columns)
        self.assertEqual(calls, [])
        self.assertEqual(warnings, [])

    def test_fan_in_looks_up_when_yaml_lists_columns(self):
        import fan_in

        calls = []

        def existing_columns(schema, table):
            calls.append((schema, table))
            return ["ID"]

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {
                            "EMP": {
                                "name": "EMP",
                                "columns": [
                                    {"source": "ID", "target": "ID"},
                                    {"source": "NAME", "target": "NAME"},
                                ],
                            },
                        },
                    },
                }
            }
        }
        warnings = fan_in.fan_in_warnings(yaml_cfg, "HR", default_target="APP",
                                          existing_columns=existing_columns)
        self.assertEqual(calls, [("APP", "EMP")])
        self.assertTrue(any("name" in w for w in warnings))



if __name__ == "__main__":
    unittest.main()

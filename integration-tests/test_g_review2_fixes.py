"""Regression tests for review-2 G1–G12 (fixed behaviour).

Synthetic names only (HR, FIN, ORDERS, …). Bug-demo assertions flipped.
"""
from __future__ import annotations

import copy
import inspect
import unittest
from pathlib import Path
from unittest import mock

import commons
import create_all_entities as cae
import export_template_from_corehub as exp
from import_plan import apply_to_yaml, load


class G1ScheduleParking(unittest.TestCase):
    def test_pipeline_schedules_returned_not_parked_on_alpha_first(self):
        schemas = {
            "HR": {"tables": {"custom": {}, "whitelist": set()}},
            "FIN": {"tables": {"custom": {}, "whitelist": set()}},
        }
        jobs = [{
            "id": "j1",
            "task_type": "pipeline_snapshot",
            "name": "nightly",
            "cron_expression": "0 2 * * *",
        }]
        pipe_sched, _group_sched = exp.attach_schedules_from_jobs(jobs, {}, schemas, {})
        # Fixed: returned at YAML root; not parked on FIN alone
        self.assertTrue(pipe_sched, pipe_sched)
        self.assertFalse(schemas["FIN"].get("schedules"))
        self.assertFalse(schemas["HR"].get("schedules"))

    def test_plan_include_preserves_or_notes_pipeline_schedules(self):
        yaml_cfg = {
            "schemas": {
                "FIN": {
                    "tables": {
                        "custom": {"BALANCES": {"name": "BALANCES"}},
                        "whitelist": ["BALANCES"],
                    },
                    "schedules": [{
                        "name": "nightly",
                        "task_type": "pipeline_snapshot",
                        "cron_expression": "0 2 * * *",
                    }],
                    "group_schedules": [{
                        "name": "g",
                        "task_type": "group_snapshot",
                        "group_ids": ["Sales"],
                        "cron_expression": "0 4 * * *",
                    }],
                },
                "HR": {
                    "tables": {
                        "custom": {"ORDERS": {"name": "ORDERS"}},
                        "whitelist": ["ORDERS"],
                    },
                },
            }
        }
        plan = load({
            "version": 1,
            "pipelines": {"pipe-1": {"entities": {"include": ["HR.ORDERS"]}}},
        })
        out, notes = apply_to_yaml(copy.deepcopy(yaml_cfg), plan, plan.pipeline("pipe-1"))
        root = out.get("schemas", out)
        self.assertNotIn("FIN", root)
        # Fixed: schedules retained on kept schema and/or noted
        hr_sched = (root.get("HR") or {}).get("schedules") or (out.get("schedules") if isinstance(out, dict) else None)
        note_hit = any("schedule" in n.lower() for n in (notes or []))
        self.assertTrue(hr_sched or out.get("schedules") or out.get("group_schedules") or note_hit, notes)


class G2G3EntityScheduleAttach(unittest.TestCase):
    def test_renamed_entity_keeps_schedules_on_real_at_at_key(self):
        schemas = {
            "HR": {
                "tables": {
                    "custom": {
                        "ORDERS@@2": {
                            "name": "ORDERS_COPY",
                            "entityName": "HR.ORDERS_COPY",
                            "keys": ["ID"],
                        },
                    },
                    "whitelist": set(["ORDERS"]),
                }
            },
        }
        entities = {"e9": {"entityName": "HR.ORDERS_COPY", "entityId": "e9"}}
        jobs = [{
            "id": "j9",
            "task_type": "entity_snapshot",
            "name": "copy-sched",
            "entity_ids": ["e9"],
            "cron_expression": "0 3 * * *",
        }]
        exp.attach_schedules_from_jobs(jobs, entities, schemas, {})
        custom = schemas["HR"]["tables"]["custom"]
        self.assertIn("ORDERS@@2", custom)
        self.assertIn("schedules", custom["ORDERS@@2"])
        self.assertNotIn("ORDERS_COPY", custom)

    def test_entity_schedule_attaches_when_entityname_schema_case_differs(self):
        schemas = {
            "HR": {
                "tables": {
                    "custom": {"ORDERS": {"name": "ORDERS"}},
                    "whitelist": set(["ORDERS"]),
                }
            },
        }
        entities = {"e1": {"entityName": "hr.ORDERS", "entityId": "e1"}}
        jobs = [{
            "id": "j1",
            "task_type": "entity_snapshot",
            "name": "s",
            "entity_ids": ["e1"],
            "cron_expression": "0 1 * * *",
        }]
        exp.attach_schedules_from_jobs(jobs, entities, schemas, {})
        self.assertTrue(schemas["HR"]["tables"]["custom"]["ORDERS"].get("schedules"))


class EntityScheduleWithoutSchemaInName(unittest.TestCase):
    """Entity names that are not SCHEMA.TABLE (e.g. an entity named after a DBMoto replication)."""

    @staticmethod
    def _schemas():
        # Two source schemas: min() is "APP", the entity's table lives in "APP_DAT".
        return {
            "APP": {
                "tables": {
                    "custom": {"CUSTOMERS": {"name": "DBM_CUSTOMERS", "entityName": "REPL_CUSTOMERS"}},
                    "whitelist": set(["CUSTOMERS"]),
                }
            },
            "APP_DAT": {
                "tables": {
                    "custom": {"ORDERS": {"name": "Company$ORDERS", "entityName": "REPL_ORDERS"}},
                    "whitelist": set(["ORDERS"]),
                }
            },
        }

    @staticmethod
    def _jobs(entity_id):
        return [{
            "id": "j5",
            "task_type": "entity_snapshot",
            "name": "refresh 07:30",
            "entity_ids": [entity_id],
            "cron_expression": "30 7 * * *",
        }]

    def test_schedule_lands_on_the_entity_entry_of_its_real_schema(self):
        schemas = self._schemas()
        entities = {"e1": {"entityName": "REPL_ORDERS", "entityId": "e1"}}
        exp.attach_schedules_from_jobs(self._jobs("e1"), entities, schemas, {})
        self.assertTrue(schemas["APP_DAT"]["tables"]["custom"]["ORDERS"].get("schedules"))
        # No phantom table named after the entity on the alphabetically-first schema
        self.assertNotIn("REPL_ORDERS", schemas["APP"]["tables"]["custom"])
        self.assertNotIn("REPL_ORDERS", schemas["APP"]["tables"]["whitelist"])

    def test_unknown_undotted_entity_is_skipped_not_invented(self):
        schemas = self._schemas()
        entities = {"x1": {"entityName": "NOT_EXPORTED", "entityId": "x1"}}
        with mock.patch.object(exp.logger, "warning") as warning:
            exp.attach_schedules_from_jobs(self._jobs("x1"), entities, schemas, {})
        self.assertIn("NOT_EXPORTED", warning.call_args.args[0])
        for cfg in schemas.values():
            self.assertNotIn("NOT_EXPORTED", cfg["tables"]["custom"])
            self.assertNotIn("NOT_EXPORTED", cfg["tables"]["whitelist"])

    def test_unknown_dotted_entity_is_skipped_not_invented(self):
        schemas = self._schemas()
        entities = {"x2": {"entityName": "APP.SKIPPED", "entityId": "x2"}}
        with mock.patch.object(exp.logger, "warning") as warning:
            exp.attach_schedules_from_jobs(self._jobs("x2"), entities, schemas, {})
        self.assertIn("APP.SKIPPED", warning.call_args.args[0])
        self.assertNotIn("SKIPPED", schemas["APP"]["tables"]["custom"])
        self.assertNotIn("SKIPPED", schemas["APP"]["tables"]["whitelist"])

    def test_multitable_schedule_lands_on_the_first_table_of_the_chain(self):
        schemas = self._schemas()
        chain = {"entityName": "REPL_CHAIN", "chainId": "multitable_HEAD"}
        schemas["APP_DAT"]["tables"]["custom"].update({
            "HEAD": {"name": "HEAD", **chain},
            "LINES": {"name": "LINES", **chain},
        })
        entities = {"m1": {"entityName": "REPL_CHAIN", "entityId": "m1"}}
        exp.attach_schedules_from_jobs(self._jobs("m1"), entities, schemas, {})
        custom = schemas["APP_DAT"]["tables"]["custom"]
        self.assertEqual(len(custom["HEAD"].get("schedules") or []), 1)
        self.assertNotIn("schedules", custom["LINES"])

    def test_name_shared_by_unrelated_tables_is_skipped_as_ambiguous(self):
        schemas = self._schemas()
        schemas["APP"]["tables"]["custom"]["CUSTOMERS"]["entityName"] = "REPL_ORDERS"
        entities = {"e1": {"entityName": "REPL_ORDERS", "entityId": "e1"}}
        with mock.patch.object(exp.logger, "warning") as warning:
            exp.attach_schedules_from_jobs(self._jobs("e1"), entities, schemas, {})
        self.assertIn("more than one exported table", warning.call_args.args[0])
        for cfg in schemas.values():
            for entry in cfg["tables"]["custom"].values():
                self.assertNotIn("schedules", entry)


class G4InvertFilter(unittest.TestCase):
    def test_invert_filter_preserves_datatype(self):
        filter_obj = {
            "clauses": [{
                "column": {"name": "AGE", "dataType": "integer", "id": 3},
                "operation": {"type": "GreaterThan", "filterValue": 18},
            }]
        }
        inv = exp.invert_filter(filter_obj)
        self.assertEqual(inv["clauses"][0]["type"], "integer")

    def test_invert_filter_legacy_type_field_preserved(self):
        filter_obj = {
            "clauses": [{
                "column": {"name": "AGE", "type": "integer", "id": 3},
                "operation": {"type": "GreaterThan", "filterValue": 18},
            }]
        }
        inv = exp.invert_filter(filter_obj)
        self.assertEqual(inv["clauses"][0]["type"], "integer")


class G5MissingFilterIds(unittest.TestCase):
    def test_missing_filter_columns_get_distinct_ids(self):
        filt = {
            "clauses": [
                {"column": "MISSING_A", "type": "string", "operation": "Equals", "value": "x"},
                {"column": "MISSING_B", "type": "string", "operation": "Equals", "value": "y"},
            ]
        }
        processed = commons.process_filter_clauses(filt, {"columns": []})
        ids = [c["column"]["id"] for c in processed["clauses"]]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        self.assertNotEqual(ids, [1, 1])


class G6CreateGroup(unittest.TestCase):
    def test_create_group_casefold_and_raises_on_failure(self):
        source = inspect.getsource(commons.create_group)
        self.assertIn("casefold", source)
        self.assertIn("RuntimeError", source)


class G7TokenFileMode(unittest.TestCase):
    def test_write_token_file_uses_0600(self):
        from automator_app.state import AutomatorState
        source = inspect.getsource(AutomatorState._write_token_file)
        self.assertIn("0o600", source)
        self.assertNotIn('open(token_file, "w")', source)


class G8ZipPathGuard(unittest.TestCase):
    def test_full_backup_restore_guards_extract_paths(self):
        import automator_app.corehub as corehub
        source = inspect.getsource(corehub)
        self.assertTrue(
            any(s in source for s in (
                "safe_extract", "zip_destination", "_extract_zip",
                "is_within_directory", "member_path", "ZipInfo",
            )) or ("extractall" in source and "resolve" in source),
            "expected a zip path guard around extractall",
        )


class G9ChronosPagination(unittest.TestCase):
    def test_chronos_jobs_paginate(self):
        source = inspect.getsource(exp._read_jobs_from_client)
        self.assertIn("offset", source)
        self.assertIn("page_limit", source)
        self.assertIn("raw_count", source)

    def test_mixed_pipeline_pages_keep_walking(self):
        """A full raw page of *other* pipelines must not stop pagination early."""
        pid = "pipe-ours"
        # page 0: 1000 other; page 1: 1 ours + padding others (short page ends)
        pages = {
            0: [{"id": f"o{i}", "pipeline_id": "other"} for i in range(1000)],
            1: [{"id": "ours-1", "pipeline_id": pid}] + [
                {"id": f"o{i}", "pipeline_id": "other"} for i in range(10)
            ],
        }
        calls = []

        class FakeClient:
            base_url = "http://chronos.test"

            def _request(self, path, params=None):
                calls.append(dict(params or {}))
                off = int((params or {}).get("offset") or 0)
                # page_limit in production is 1000; map offset→page index
                idx = off // 1000
                return pages.get(idx, [])

        prev = exp._chronos_clients
        exp._chronos_clients = lambda token=None: [FakeClient()]  # type: ignore
        try:
            jobs = exp.read_pipeline_jobs(pid, token="t")
        finally:
            exp._chronos_clients = prev
        self.assertEqual([c.get("offset") for c in calls], [0, 1000])
        self.assertEqual([j["id"] for j in jobs], ["ours-1"])

    def test_repeated_raw_page_stops(self):
        """Offset ignored returning the same other-pipeline page must not loop forever."""
        pid = "pipe-ours"
        same = [{"id": f"o{i}", "pipeline_id": "other"} for i in range(1000)]
        calls = []

        class FakeClient:
            base_url = "http://chronos.test"

            def _request(self, path, params=None):
                calls.append(dict(params or {}))
                if len(calls) > 5:
                    raise AssertionError("pagination did not stop")
                return same

        prev = exp._chronos_clients
        exp._chronos_clients = lambda token=None: [FakeClient()]  # type: ignore
        try:
            jobs = exp.read_pipeline_jobs(pid, token="t")
        finally:
            exp._chronos_clients = prev
        self.assertEqual(jobs, [])
        self.assertEqual(len(calls), 2)  # first page + repeated detect


class G10VersionFailClosed(unittest.TestCase):
    def test_unreadable_version_does_not_assume_23_plus(self):
        source = Path("commons.py").read_text(encoding="utf-8")
        self.assertNotIn("ASSUMING A GLUESYNC 2.3+", source.upper())


class G11TokenRefresh(unittest.TestCase):
    def test_fetch_core_hub_handles_401_refresh(self):
        source = inspect.getsource(commons.fetch_core_hub)
        self.assertIn("401", source)
        self.assertIn("refresh", source.lower())
        self.assertIn("SESSION_EXPIRED_HINT", inspect.getsource(commons))

    def test_session_expired_hint_without_refresh(self):
        commons.clear_token_refresh()

        class FakeResp:
            status_code = 401
            text = "Unauthorized"

            def json(self):
                return {"error": "Unauthorized"}

        class FakeErr(Exception):
            def __init__(self):
                self.response = FakeResp()
                super().__init__("401 Unauthorized")

        def boom(*_a, **_k):
            raise FakeErr()

        prev = commons.core_hub_client.request
        commons.core_hub_client.request = boom
        try:
            with self.assertRaises(RuntimeError) as ctx:
                commons.fetch_core_hub("/pipelines", token="OLD")
            msg = str(ctx.exception)
            self.assertIn("session expired", msg.lower())
            self.assertIn("onExisting=skip", msg)
        finally:
            commons.core_hub_client.request = prev
            commons.clear_token_refresh()


class G12NoInventedPrecision(unittest.TestCase):
    def test_export_omits_invented_numeric_precision_zero(self):
        fields = exp._source_column_fields({"name": "AMOUNT", "dataType": "NUMBER"}, 1)
        self.assertIsNone(fields.get("numericPrecision"))
        self.assertIsNone(fields.get("numericScale"))

    def test_offline_column_omits_invented_num_prec_zero(self):
        col = cae._offline_column({"type": "NUMBER"}, "AMOUNT", 1, set())
        self.assertIsNone(col.get("numPrec"))



class G6CreateTablesSoftFail(unittest.TestCase):
    """create_all_tables._create_groups must not abort the batch when create_group raises (G6)."""

    def test_create_all_tables_create_groups_soft_fails(self):
        import create_all_tables as cat

        calls = []

        def boom(token, pipeline_id, group_name, description=None, color=None):
            calls.append(group_name)
            if group_name == "Sales":
                raise RuntimeError("hub down")
            return "gid-ok"

        prev = cat.create_group
        cat.create_group = boom
        try:
            cat._create_groups("tok", "pid", ["Sales", "HR"])
        finally:
            cat.create_group = prev
        self.assertEqual(calls, ["Sales", "HR"])


if __name__ == "__main__":
    unittest.main()

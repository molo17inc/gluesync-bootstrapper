#!/usr/bin/env python3
"""Import report fixes: short reason + resume hint when a created pipeline fails (DB down), schedules that
could not be restored make the import PARTIAL, UDF type accepted in any case. Mocked (no live calls)."""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests

logging.disable(logging.CRITICAL)
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import commons  # noqa: E402
import create_user_defined_functions as udfs  # noqa: E402
from automator_app import app, corehub  # noqa: E402

CORE_HUB_500 = ('CoreHub request PUT /pipelines/p1/agents/a1/config/credentials failed: 500 Server Error | '
                'Response: {"notificationId": 9, "exceptionMessage": "Failed to initialize pool: Could not create '
                'connection to database server. Attempted reconnect 3 times. Giving up.", "stack": "x"}')


def http_500():
    response = requests.Response()
    response.status_code = 500
    try:
        raise requests.exceptions.HTTPError("500 Server Error", response=response)
    except requests.exceptions.HTTPError as cause:
        try:
            raise RuntimeError(CORE_HUB_500) from cause
        except RuntimeError as err:
            return err


class PartialImportErrorTests(unittest.TestCase):
    def test_db_down_gives_short_reason_and_resume_hint(self):
        err = corehub._partial_import_error("t", "p1", "demo", ["SOURCE s (id=a0)"], http_500(), cleanup=False)
        text = str(err)
        self.assertIn("can't connect to database", text)
        self.assertIn("Next: fix the cause, then resume with GS_IMPORT_RESUME_PIPELINE_ID=p1", text)
        self.assertIn("delete the pipeline", text)
        self.assertNotIn("notificationId", text)
        self.assertNotIn("Response:", text)
        self.assertIn("notificationId", err.detail)  # full answer kept for the debug log
        self.assertEqual(err.pipeline_id, "p1")

    def test_cleaned_up_has_no_resume_hint(self):
        with mock.patch.object(corehub, "fetch_core_hub", return_value=None):
            err = corehub._partial_import_error("t", "p1", "demo", [], http_500(), cleanup=True)
        self.assertIn("deleted (cleanup on failure)", str(err))
        self.assertNotIn("Next:", str(err))

    def test_non_connection_error_keeps_core_hub_message_only(self):
        err = corehub._partial_import_error("t", "p1", "demo", [], RuntimeError(
            'failed: 400 | Response: {"exceptionMessage": "Agent tag unknown"}'), cleanup=False)
        self.assertIn("failed after it was created: Agent tag unknown.", str(err))


class ScheduleFailureTests(unittest.TestCase):
    SCHEDULES = [{"task_type": "entity_snapshot", "name": "nightly", "cron_expression": "0 3 * * *"}]

    def setUp(self):
        commons.take_schedule_failures()

    def _chronos(self, available):
        client = mock.Mock()
        client.wait_for_chronos.return_value = available
        return mock.patch.object(commons, "ChronosClient", return_value=client)

    def test_unreachable_scheduler_is_recorded(self):
        with self._chronos(False), mock.patch.object(commons, "ENABLE_SCHEDULING", True):
            commons.create_entity_schedules("t", "p1", "e1", "CUSTOMERS", self.SCHEDULES)
            commons.create_pipeline_schedules("t", "p1", self.SCHEDULES)
        failures = commons.take_schedule_failures()
        self.assertEqual(len(failures), 2)
        self.assertIn("CUSTOMERS", failures[0])
        self.assertEqual(commons.take_schedule_failures(), [])

    def test_refused_schedule_is_recorded(self):
        with self._chronos(True) as chronos, mock.patch.object(commons, "ENABLE_SCHEDULING", True):
            chronos.return_value.create_entity_schedule.side_effect = RuntimeError("400")
            commons.create_entity_schedules("t", "p1", "e1", "CUSTOMERS", self.SCHEDULES)
        self.assertEqual(commons.take_schedule_failures(), ["schedule 'nightly' of entity CUSTOMERS (refused)"])

    def test_no_schedules_or_scheduling_off_records_nothing(self):
        with self._chronos(False) as chronos, mock.patch.object(commons, "ENABLE_SCHEDULING", True):
            commons.create_entity_schedules("t", "p1", "e1", "CUSTOMERS", [])
        with self._chronos(False), mock.patch.object(commons, "ENABLE_SCHEDULING", False):
            commons.create_entity_schedules("t", "p1", "e1", "CUSTOMERS", self.SCHEDULES)
        self.assertEqual(commons.take_schedule_failures(), [])
        chronos.assert_not_called()

    def _job_after_run(self, failures):
        job = app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None)
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "demo"})
        entities = mock.Mock(pipeline_id="p1", pipeline_name="demo", pending_agent_types=set(),
                             default_on_existing=None, plan_choices=None, compile_udfs=False)
        entities.name = "demo.yaml"

        def run(**_kwargs):
            for line in failures:
                commons.record_schedule_failure(line)
            return {"success": True, "result": {"successful": 3, "failed": 0, "errors": []}}
        with mock.patch.object(app.corehub, "run_create_entities", side_effect=run):
            app._create_schema_pair_entities(job, entities, "dbo", "dbo")
        return job

    def test_lost_schedule_makes_import_partial(self):
        job = self._job_after_run(["1 schedule(s) of entity CUSTOMERS (scheduler not reachable)"])
        self.assertEqual(app.import_status(job), "partial")
        self.assertEqual(job.outcomes["p1"]["schedulesNotRestored"], 1)
        self.assertIn("1 schedule(s) not restored", app._outcome_line(job.outcomes["p1"]))
        self.assertTrue(any("schedules not restored" in e and "recreate" in e for e in job.errors))

    def test_no_schedule_failure_stays_full(self):
        commons.record_schedule_failure("stale, from an earlier run")
        job = self._job_after_run([])
        self.assertEqual(app.import_status(job), "full")


class UdfTypeTests(unittest.TestCase):
    def test_any_case_maps_to_canonical_value(self):
        for raw in ("java", "Java", "JAVA", " java "):
            self.assertIs(udfs.UdfFunctionType(raw), udfs.UdfFunctionType.java)
        self.assertIs(udfs.UdfFunctionType("kotlin"), udfs.UdfFunctionType.kotlin)
        body = udfs.UdfFunctionCompileRequest(code="x", type="java", udfName="U").model_dump()
        self.assertEqual(body["type"], "Java")

    def test_unknown_type_still_refused(self):
        with self.assertRaises(ValueError):
            udfs.UdfFunctionType("python")


if __name__ == "__main__":
    unittest.main()




class EmptyWhitelistReportTests(unittest.TestCase):
    """Empty whitelist = allow-all discovery: YAML cannot name the total; report must not say N of M with M < N."""

    def test_empty_whitelist_yaml_total_is_unknown(self):
        schemas = {"demo": {"tables": {"whitelist": [], "custom": {"a": {}, "b": {}}}}}
        self.assertIsNone(app._schema_table_count(schemas, "demo"))

    def test_missing_whitelist_yaml_total_is_unknown(self):
        schemas = {"demo": {"tables": {"custom": {"only": {}}}}}
        self.assertIsNone(app._schema_table_count(schemas, "demo"))

    def test_filled_whitelist_counts_union_with_custom(self):
        schemas = {"demo": {"tables": {"whitelist": ["t1", "t2"], "custom": {"t2": {}, "t3": {}}}}}
        self.assertEqual(app._schema_table_count(schemas, "demo"), 3)

    def test_report_total_prefers_counted_when_larger_than_yaml(self):
        # Live bug: 9 created + 2 failed with yaml total 2 → "9 of 2". Counted sum wins.
        total = app._report_total({
            "entitiesRun": True, "total": 2, "created": 9, "failed": 2, "skipped": 0, "notAttempted": 0,
        })
        self.assertEqual(total, 11)

    def test_report_total_keeps_yaml_when_counted_is_smaller(self):
        total = app._report_total({
            "entitiesRun": True, "total": 58, "created": 0, "failed": 0, "skipped": 0, "notAttempted": 58,
        })
        self.assertEqual(total, 58)

    def test_pipeline_report_line_never_says_nine_of_two(self):
        line = app._pipeline_report_line({
            "pipelineName": "GS_IMP", "pipelineId": "x", "entitiesRun": True,
            "total": 2, "created": 9, "failed": 2, "skipped": 0, "notAttempted": 0,
            "errors": 2,
        })
        self.assertIn("9 of 11 entities created", line)
        self.assertNotIn("of 2 entities", line)

class DryRunOverrideTests(unittest.TestCase):
    """The dry run reports the passwords the agent overrides / GS_IMPORT_AGENT_PASSWORD_<TAG> would give."""

    SECRET = "Dry-Run-S3cret!"

    def _masked_archive(self):
        import test_custom_import as T  # pylint: disable=import-outside-toplevel
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(T.archive("synth1"))) as src, zipfile.ZipFile(buffer, "w") as dst:
            for item in src.namelist():
                data = src.read(item).decode()
                if item.endswith("agents-config.yaml"):
                    data = data.replace("<PASSWORD>", "'*******'")
                dst.writestr(item, data)
        return T, buffer.getvalue()

    def _preview(self, env):
        T, contents = self._masked_archive()
        hub = T.Hub()
        with mock.patch.dict(os.environ, env, clear=False), hub.patched():
            for name in [k for k in os.environ if k.startswith("GS_IMPORT_AGENT_")]:
                if name not in env:
                    os.environ.pop(name)
            preview = app._preview_import(contents, T.plan({}), plan_env={})  # pylint: disable=protected-access
        self.assertTrue(all(m == "GET" for m, _p in hub.calls), hub.calls)
        return preview

    def test_masked_without_override_is_a_problem(self):
        preview = self._preview({})
        self.assertFalse(preview["ok"])
        self.assertTrue(any("password missing (masked in the backup)" in e for e in preview["errors"]))

    def test_env_passwords_are_reported_as_given(self):
        preview = self._preview({"GS_IMPORT_AGENT_PASSWORD_MSSQL_CT": self.SECRET,
                                 "GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES": self.SECRET})
        self.assertNotIn(self.SECRET, json.dumps(preview))
        self.assertFalse([e for e in preview["errors"] if "password" in e], preview["errors"])
        states = {a["agentTag"]: a["password"] for p in preview["pipelines"] for a in p.get("agents", [])}
        self.assertEqual(set(states.values()), {"from agent overrides"}, states)

    def test_overrides_file_naming_no_agent_is_reported(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"no-such-agent": {"host": "h"}}, handle)
        try:
            preview = self._preview({"GS_IMPORT_AGENT_OVERRIDES_FILE": handle.name})
        finally:
            os.unlink(handle.name)
        self.assertTrue(any("Agent overrides:" in e for e in preview["errors"]), preview["errors"])

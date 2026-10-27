"""MR !69 review follow-ups: upload size cap, owner-only work dirs, target collisions, resume pre-check."""
import asyncio
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import import_plan  # noqa: E402
from automator_app import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Upload:
    def __init__(self, data):
        self._buf = io.BytesIO(data)

    async def read(self, size=-1):
        return self._buf.read(size)


class UploadCapTests(unittest.TestCase):
    def test_oversized_upload_is_413(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(app._read_capped_upload(_Upload(b"x" * (3 * 1024 * 1024)), 1024 * 1024))
        self.assertEqual(ctx.exception.status_code, 413)

    def test_empty_upload_is_400_and_small_one_read(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(app._read_capped_upload(_Upload(b""), 1024))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(asyncio.run(app._read_capped_upload(_Upload(b"abc"), 1024)), b"abc")

    def test_max_bytes_from_env(self):
        with mock.patch.dict(os.environ, {"GS_IMPORT_MAX_UPLOAD_MB": "2"}):
            self.assertEqual(app.import_upload_max_bytes(), 2 * 1024 * 1024)
        with mock.patch.dict(os.environ, {"GS_IMPORT_MAX_UPLOAD_MB": "junk"}):
            self.assertEqual(app.import_upload_max_bytes(), 512 * 1024 * 1024)

    def test_pruner_starts_once(self):
        with mock.patch.object(app, "_import_pruner_started", False), \
                mock.patch.object(app.threading, "Thread") as thread:
            app._start_import_pruner()
            app._start_import_pruner()
        self.assertEqual(thread.call_count, 1)
        self.assertTrue(thread.call_args.kwargs.get("daemon"))


@unittest.skipIf(os.name != "posix", "POSIX permissions")
class OwnerOnlyDirTests(unittest.TestCase):
    def test_session_and_root_dirs_are_0700(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GLUESYNC_AUTOMATOR_WORKDIR": str(Path(tmp) / "wd")}):
            session = app.new_session_dir("import")
            self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(app.get_workspace_root().stat().st_mode), 0o700)


YAML = {"schemas": {"SCH01": {"target": "TGT01", "tables": {"custom": {
    "TAB001": {"name": "TAB001"}, "TAB002": {"name": "TAB002"}, "TAB003@@2": {"name": "TAB003"}}}}}}


class TargetCollisionTests(unittest.TestCase):
    def plan(self, **kw):
        return import_plan.PipelinePlan(old_id="p1", **kw)

    def test_case_only_override_keys_refused(self):
        p = self.plan(overrides={"SCH01.TAB001": {"targetTable": "A"}, "sch01.tab001": {"targetTable": "B"}})
        with self.assertRaisesRegex(import_plan.PlanError, "differ only by case"):
            import_plan.check_target_collisions(YAML, p)

    def test_case_only_table_target_schema_refused(self):
        p = self.plan(table_target_schema={"SCH01.TAB001": "X", "SCH01.tab001": "Y"})
        with self.assertRaisesRegex(import_plan.PlanError, "differ only by case"):
            import_plan.check_target_collisions(YAML, p)

    def test_duplicate_rename_refused(self):
        p = self.plan(overrides={"SCH01.TAB001": {"targetTable": "NEW"}, "SCH01.TAB002": {"targetTable": "new"}})
        with self.assertRaisesRegex(import_plan.PlanError, "both renamed"):
            import_plan.check_target_collisions(YAML, p)

    def test_excluded_or_single_rename_ok(self):
        p = self.plan(overrides={"SCH01.TAB001": {"targetTable": "NEW"}, "SCH01.TAB002": {"targetTable": "NEW"}},
                      exclude=["SCH01.TAB002"])
        import_plan.check_target_collisions(YAML, p)
        # fan-in by YAML (no explicit rename on both) stays legitimate
        import_plan.check_target_collisions(YAML, self.plan(overrides={"SCH01.TAB001": {"targetTable": "TAB003"}}))


class ResumeTargetTests(unittest.TestCase):
    def test_unreadable_resume_target_refused_before_writes(self):
        with mock.patch.object(app.corehub, "fetch_core_hub", side_effect=RuntimeError("404")) as fetch:
            with self.assertRaisesRegex(import_plan.PlanError, "resumeInto"):
                app._check_resume_target("p1", "missing")
        self.assertEqual(fetch.call_args.args[0], "/pipelines/missing/config")
        self.assertNotIn("method", fetch.call_args.kwargs)

    def test_non_dict_answer_refused_and_dict_ok(self):
        with mock.patch.object(app.corehub, "fetch_core_hub", return_value=[]):
            with self.assertRaises(import_plan.PlanError):
                app._check_resume_target("p1", "x")
        with mock.patch.object(app.corehub, "fetch_core_hub", return_value={"pipelineName": "x"}):
            app._check_resume_target("p1", "x")


import agent_overrides  # noqa: E402


class EmptySelectionTests(unittest.TestCase):
    def test_plan_selecting_nothing_refused(self):
        p = import_plan.PipelinePlan(old_id="p1", include=[])
        self.assertEqual(import_plan.selected_ids(YAML, p), [])
        job = mock.Mock(resume_pipeline_id=None)
        with self.assertRaisesRegex(import_plan.PlanError, "no table selected"):
            app._check_pipeline_plan(job, "p1", p, "n", __import__("yaml").safe_dump(YAML))


class PasswordVarTests(unittest.TestCase):
    AGENTS = [{"agentId": "a1", "agentType": "SOURCE", "agentTag": "mssql-ct"},
              {"agentId": "a2", "agentType": "TARGET", "agentTag": "MSSQL_CT"}]

    def test_shared_set_variable_refused_without_printing_it(self):
        env = {"GS_IMPORT_AGENT_PASSWORD_MSSQL_CT": "s3cr3t-value"}
        with self.assertRaises(agent_overrides.AgentOverrideError) as ctx:
            agent_overrides.check_password_vars(self.AGENTS, {}, env)
        self.assertNotIn("s3cr3t-value", str(ctx.exception))

    def test_unset_shared_variable_ok(self):
        agent_overrides.check_password_vars(self.AGENTS, {}, {})

    def test_missing_tag_has_no_variable(self):
        for tag in (None, "", "  "):
            with self.assertRaises(agent_overrides.AgentOverrideError):
                agent_overrides.password_env_name(tag)


class SecretKeyTests(unittest.TestCase):
    def test_table_named_like_a_secret_is_allowed(self):
        import_plan._refuse_secrets({"pipelines": {"p1": {"entities": {"overrides": {
            "AUTH.PASSWORD_RESET": {"targetTable": "PWD_RESET"}}}, "tableTargetSchema": {"AUTH.TOKEN": "X"}}}})

    def test_secret_field_still_refused(self):
        with self.assertRaises(import_plan.PlanError):
            import_plan._refuse_secrets({"pipelines": {"p1": {"agents": {"a1": {"password": "x"}}}}})


if __name__ == "__main__":
    unittest.main()

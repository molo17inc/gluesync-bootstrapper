#!/usr/bin/env python3
"""Backup import: agent connection overrides, resume of an existing pipeline, full/partial summary and the
import_backup CLI. Mocked Core Hub (no live calls)."""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)
TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent_overrides  # noqa: E402
from automator_app import app, import_backup  # noqa: E402


def agents():
    return [
        {"agentId": "src-1", "agentTag": "ibm-iseries", "agentType": "SOURCE",
         "hostCredentials": {"host": "as400", "port": 446, "databaseName": "X", "username": "u", "password": "old1"}},
        {"agentId": "tgt-1", "agentTag": "mssql-ct", "agentType": "TARGET",
         "hostCredentials": {"host": "prod-sql", "port": 1433, "databaseName": "demo", "username": "sa",
                             "password": "old2"}},
    ]


class AgentOverridesTests(unittest.TestCase):
    def test_tag_override_and_password_from_env_never_logged(self):
        items = agents()
        overrides = agent_overrides.load('{"mssql-ct": {"host": "localhost", "port": "1433", "database": "demo2",'
                                         ' "username": "sa2"}}', env={})
        lines = agent_overrides.apply(items, overrides, env={"GS_IMPORT_AGENT_PASSWORD_MSSQL_CT": "s3cr3t"})
        creds = items[1]["hostCredentials"]
        self.assertEqual((creds["host"], creds["port"], creds["databaseName"], creds["username"], creds["password"]),
                         ("localhost", 1433, "demo2", "sa2", "s3cr3t"))
        self.assertEqual(items[0]["hostCredentials"]["host"], "as400")
        self.assertNotIn("s3cr3t", "\n".join(lines))
        self.assertIn("GS_IMPORT_AGENT_PASSWORD_MSSQL_CT", "\n".join(lines))

    def test_agent_id_and_password_env(self):
        items = agents()
        overrides = agent_overrides.load('{"src-1": {"host": "pub400", "passwordEnv": "AS400_PW"}}', env={})
        agent_overrides.apply(items, overrides, env={"AS400_PW": "pw"})
        self.assertEqual(items[0]["hostCredentials"]["host"], "pub400")
        self.assertEqual(items[0]["hostCredentials"]["password"], "pw")

    def test_password_in_json_is_refused(self):
        with self.assertRaises(agent_overrides.AgentOverrideError):
            agent_overrides.load('{"mssql-ct": {"password": "x"}}', env={})

    def test_unknown_agent_is_refused(self):
        overrides = agent_overrides.load('{"nope": {"host": "h"}}', env={})
        backup_agents = agents()
        with self.assertRaises(agent_overrides.AgentOverrideError) as ctx:
            agent_overrides.apply(backup_agents, overrides, env={})
        self.assertIn("mssql-ct", str(ctx.exception))

    def test_file_and_env_sources(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"mssql-ct": {"host": "from-file", "port": 1}}, handle)
        try:
            overrides = agent_overrides.load(env={"GS_IMPORT_AGENT_OVERRIDES_FILE": handle.name,
                                                  "GS_IMPORT_AGENT_OVERRIDES": '{"mssql-ct": {"port": 2}}'})
        finally:
            os.unlink(handle.name)
        items = agents()
        agent_overrides.apply(items, overrides, env={})
        self.assertEqual((items[1]["hostCredentials"]["host"], items[1]["hostCredentials"]["port"]), ("from-file", 2))

    def test_no_overrides_is_a_no_op(self):
        items = agents()
        self.assertEqual(agent_overrides.apply(items, agent_overrides.load(env={}), env={}), [])
        self.assertEqual(items, agents())

    def test_bad_mapping_is_a_400_before_anything_is_sent(self):
        job = app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None)
        backup_agents = agents()
        with self.assertRaises(app.HTTPException) as ctx:
            app._apply_agent_overrides(job, backup_agents, '{"nope": {"host": "h"}}')
        self.assertEqual(ctx.exception.status_code, 400)


def new_job(**kwargs):
    return app._ArchiveImport(contents=b"", agents_list=agents(), meta_by_old_id={}, udf_root=None, **kwargs)


class SummaryTests(unittest.TestCase):
    def test_zero_entities_is_partial_not_imported(self):
        job = new_job()
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "Synth1MSSQL"})
        app._outcome(job, "p1", "Synth1MSSQL")
        app._record_entity_counts(job, "p1", {"success": False, "result": {"successful": 0, "failed": 3,
                                                                            "total": 3, "errors": ["x"]}}, None)
        message = app._import_summary(job.created_pipelines, [], job.outcomes)
        self.assertNotIn("Imported 1 pipeline", message)
        self.assertIn("partially imported: 1", message)
        self.assertIn("0 entities created, 3 failed", message)
        self.assertEqual(app.import_status(job), "partial")

    def test_full(self):
        job = new_job()
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "A"})
        outcome = app._outcome(job, "p1", "A")
        outcome.update(entitiesRun=True, created=4)
        self.assertIn("fully imported: 1; partially imported: 0", app._import_summary(job.created_pipelines, [],
                                                                                       job.outcomes))
        self.assertEqual(app.import_status(job), "full")

    def test_entities_never_reached_is_partial_and_failed_when_nothing(self):
        job = new_job()
        self.assertEqual(app.import_status(job), "failed")
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "A"})
        self.assertEqual(app.import_status(job), "partial")
        self.assertIn("entity creation not reached", app._import_summary(job.created_pipelines, [], job.outcomes))

    def test_legacy_summary_unchanged_without_outcomes(self):
        self.assertEqual(app._import_summary([{"pipelineId": "p", "pipelineName": "A"}], []),
                         "Imported 1 pipeline(s): A (p).")

    def test_route_message_is_not_success_when_partial(self):
        job = new_job()
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "A"})
        with mock.patch.object(app, "_run_archive_import", return_value=job):
            result = app._import_archive(b"zip")
        self.assertFalse(result.success)
        self.assertIn("PARTIAL", result.message)


class ResumeTests(unittest.TestCase):
    META = {"old-1": {"pipelineName": "Synth1MSSQL"}}

    def _resume(self, function_collection_ok=True, only_source_bound=False, add_agent_error=None):
        job = new_job(resume_pipeline_id="p-existing")
        job.meta_by_old_id = self.META
        backup_agents = [dict(a, specificConfiguration={"k": a["agentTag"]}) for a in agents()]
        calls = []

        def fetch(path, method="GET", **kwargs):
            calls.append((method, path, kwargs.get("body")))
            if path == "/pipelines/p-existing/config":
                bound = [{"agentId": "new-src", "agentType": "SOURCE", "agentTag": "ibm-iseries"}]
                if not only_source_bound:
                    bound.append({"agentId": "new-tgt", "agentType": "TARGET", "agentTag": "mssql-ct"})
                return {"pipelineName": "Synth1MSSQL", "agents": bound}
            return {}

        def collection(job, name, *_a):
            if not function_collection_ok:
                job.errors.append("function collection refused: already exists")
                return False
            return True

        entity_calls = []
        with mock.patch.object(app, "_resolve_pipeline_identity", return_value=("old-1", "Synth1MSSQL")), \
                mock.patch.object(app, "_agents_for_pipeline", return_value=backup_agents), \
                mock.patch.object(app.corehub, "fetch_core_hub", side_effect=fetch), \
                mock.patch.object(app.corehub, "_add_agent_to_pipeline_via_corehub",
                                  side_effect=add_agent_error, return_value="added-tgt"), \
                mock.patch.object(app, "_materialize_import_yaml", return_value=Path("x.yaml")), \
                mock.patch.object(app, "_restore_imported_function_collection", side_effect=collection), \
                mock.patch.object(app, "_create_imported_entities",
                                  side_effect=lambda *a, **k: entity_calls.append((a, k))):
            app._resume_archive_pipeline(job, "Synth1MSSQL.yaml", "Synth1MSSQL", "yaml")
        return job, calls, entity_calls

    def test_no_pipeline_or_agent_or_credentials_created(self):
        job, calls, entity_calls = self._resume()
        methods_paths = [(m, p) for m, p, _b in calls]
        self.assertNotIn(("POST", "/pipelines"), methods_paths)
        self.assertFalse(any("credentials" in p for _m, p in methods_paths))
        self.assertIn(("PUT", "/pipelines/p-existing/agents/new-src/config/specific"), methods_paths)
        self.assertIn(("PUT", "/pipelines/p-existing/agents/new-tgt/config/specific"), methods_paths)
        body = next(b for m, p, b in calls if p.endswith("new-tgt/config/specific"))
        self.assertEqual(body, {"configuration": {"k": "mssql-ct"}})
        (args, kwargs), = entity_calls
        self.assertEqual(args[3], "p-existing")
        self.assertEqual(kwargs["default_on_existing"], "skip")
        self.assertEqual(job.created_pipelines, [{"pipelineId": "p-existing", "pipelineName": "Synth1MSSQL",
                                                  "resumed": True}])
        self.assertEqual(job.errors, [])

    def test_half_created_pipeline_gets_missing_target_bound(self):
        job, calls, entity_calls = self._resume(only_source_bound=True)
        methods_paths = [(m, p) for m, p, _b in calls]
        self.assertEqual(job.errors, [])
        self.assertNotIn(("POST", "/pipelines"), methods_paths)
        self.assertIn(("PUT", "/pipelines/p-existing/agents/added-tgt"), methods_paths)
        creds = [(m, p, b) for m, p, b in calls if "credentials" in p]
        self.assertEqual([p for _m, p, _b in creds], ["/pipelines/p-existing/agents/added-tgt/config/credentials"])
        self.assertFalse(any("new-src" in p and "credentials" in p for _m, p in methods_paths))
        specific = [p for _m, p in methods_paths if p.endswith("/config/specific")]
        self.assertEqual(sorted(specific), ["/pipelines/p-existing/agents/added-tgt/config/specific",
                                            "/pipelines/p-existing/agents/new-src/config/specific"])
        self.assertEqual(len(entity_calls), 1)
        self.assertNotIn("old2", "\n".join(job.notes + job.errors))

    def test_missing_agent_bind_failure_stops_before_entities(self):
        job, _calls, entity_calls = self._resume(only_source_bound=True, add_agent_error=RuntimeError("no agent"))
        self.assertEqual(entity_calls, [])
        self.assertTrue(any("failed to bind the missing agent" in e for e in job.errors))

    def test_credentials_refused_on_missing_agent_unassigns_it(self):
        def refuse(path, method="GET", **kwargs):
            if path.endswith("/config/credentials"):
                raise RuntimeError("500 Failed to initialize pool")
            return None
        with mock.patch.object(app.corehub, "_put_agent_credentials_or_record",
                               side_effect=lambda *a, **k: refuse("/config/credentials")):
            job, calls, entity_calls = self._resume(only_source_bound=True)
        self.assertEqual(entity_calls, [])
        self.assertIn(("DELETE", "/pipelines/p-existing/agents/added-tgt", None), calls)
        self.assertTrue(any("unassigned again: TARGET mssql-ct (id=added-tgt)" in e for e in job.errors))

    def test_function_collection_refusal_is_a_note(self):
        job, _calls, entity_calls = self._resume(function_collection_ok=False)
        self.assertEqual(job.errors, [])
        self.assertTrue(any("already exists" in n for n in job.notes))
        self.assertEqual(len(entity_calls), 1)

    def test_resume_default_on_existing_is_skip_but_env_wins(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            options = app._entity_import_options(frozenset(), default_on_existing="skip")
        self.assertEqual(options.on_existing, "skip")
        with mock.patch.dict(os.environ, {"GS_IMPORT_ON_EXISTING": "update"}, clear=True):
            options = app._entity_import_options(frozenset(), default_on_existing="skip")
        self.assertEqual(options.on_existing, "update")

    def test_several_pipelines_need_resume_from(self):
        job = new_job(resume_pipeline_id="p")
        members = [("a.yaml", "a", "A"), ("b.yaml", "b", "B")]
        with mock.patch.object(app, "_resolve_pipeline_identity", side_effect=lambda text, stem: (text, None)):
            self.assertEqual(app._resume_candidates(job, members), [])
            self.assertIn("GS_IMPORT_RESUME_FROM_PIPELINE", job.errors[0])
            job2 = new_job(resume_pipeline_id="p", resume_source_pipeline_id="B")
            self.assertEqual(app._resume_candidates(job2, members), [("b.yaml", "b", "B")])


class CliTests(unittest.TestCase):
    def _run(self, job, argv=None, env=None):
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(b"zip")
        try:
            with mock.patch.dict(os.environ, env if env is not None else {"GS_COREHUB_PASSWORD": "pw"}, clear=True), \
                    mock.patch.object(app.corehub, "authenticate", return_value="tok") as auth, \
                    mock.patch.object(app.state, "set_auth"), mock.patch.object(app.state, "set_preferences"), \
                    mock.patch.object(app, "_run_archive_import", return_value=job) as run:
                code = import_backup.main([handle.name, "--base-url", "https://hub:1717", "--username", "admin"]
                                          + (argv or []))
        finally:
            os.unlink(handle.name)
        return code, auth, run

    def test_exit_codes(self):
        job = new_job()
        self.assertEqual(self._run(job)[0], 1)
        job.created_pipelines.append({"pipelineId": "p1", "pipelineName": "A"})
        self.assertEqual(self._run(job)[0], 2)
        app._outcome(job, "p1", "A").update(entitiesRun=True, created=1)
        self.assertEqual(self._run(job)[0], 0)

    def test_password_only_from_env_and_resume_args_passed(self):
        job = new_job()
        code, auth, run = self._run(job, ["--resume-pipeline-id", "p9", "--resume-from-pipeline", "old"])
        self.assertEqual(auth.call_args.kwargs["password"], "pw")
        self.assertEqual(run.call_args.kwargs, {"resume_pipeline_id": "p9", "resume_source_pipeline_id": "old"})
        self.assertEqual(self._run(job, env={})[0], 1)


if __name__ == "__main__":
    unittest.main()

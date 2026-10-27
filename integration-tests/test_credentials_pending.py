#!/usr/bin/env python3
"""GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE: Core Hub refuses an agent's credentials when its database is
unreachable (VERIFIED on Core Hub 2.3.1: 500 "Failed to initialize pool ... Connection refused"). Mocked hub."""

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

logging.disable(logging.CRITICAL)
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import import_options  # noqa: E402
from automator_app import app, corehub  # noqa: E402
from utils import core_hub_client  # noqa: E402


def http_error(status):
    response = requests.Response()
    response.status_code = status
    error = requests.exceptions.HTTPError(f"{status} Server Error", response=response)
    wrapped = RuntimeError(f"PUT credentials failed: {status} Something went wrong dispatching credentials to agents")
    wrapped.__cause__ = error
    return wrapped


class ConfigOnlyImportTests(unittest.TestCase):
    CONFIG = {"pipelineName": "Synth1", "agents": [
        {"agentType": "SOURCE", "agentTag": "as400", "hostCredentials": {"host": "h"},
         "specificConfiguration": {"x": 1}},
        {"agentType": "TARGET", "agentTag": "mssql", "hostCredentials": {"host": "t"}}]}

    def _import(self, failure, **kwargs):
        calls = []

        def fetch(path, method="GET", **_kwargs):
            calls.append((method, path))
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines":
                return {"pipelineId": "p1"}
            if path == "/pipelines/p1/agents/id-as400/config/credentials":
                raise failure
            return {}
        with mock.patch.object(corehub, "configure_core_hub"), \
                mock.patch.object(corehub, "fetch_core_hub", side_effect=fetch), \
                mock.patch.object(corehub, "_add_agent_to_pipeline_via_corehub",
                                  side_effect=lambda **k: f"id-{k['agent_user_tag']}"):
            result = corehub.import_pipeline_config_only(token="t", base_url="https://hub", use_ssl=True,
                                                         skip_verify=True, config=dict(self.CONFIG), **kwargs)
        return result, calls

    def test_default_still_stops_on_a_500(self):
        error = http_error(500)
        with self.assertRaises(corehub.PartialPipelineImportError):
            self._import(error)

    def test_continue_records_the_agent_and_binds_the_others(self):
        result, calls = self._import(http_error(500), continue_on_credentials_failure=True)
        pending = result["credentialsPending"]
        self.assertEqual([(p["pipelineId"], p["agentId"], p["agentType"], p["agentTag"]) for p in pending],
                         [("p1", "id-as400", "SOURCE", "as400")])
        self.assertIn(("PUT", "/pipelines/p1/agents/id-as400/config/specific"), calls)
        self.assertIn(("PUT", "/pipelines/p1/agents/id-mssql/config/credentials"), calls)

    def test_connection_error_is_recoverable_too(self):
        failure = RuntimeError("down")
        failure.__cause__ = requests.exceptions.ConnectionError("refused")
        result, _calls = self._import(failure, continue_on_credentials_failure=True)
        self.assertEqual(len(result["credentialsPending"]), 1)

    def test_a_4xx_still_stops(self):
        error = http_error(400)
        with self.assertRaises(corehub.PartialPipelineImportError):
            self._import(error, continue_on_credentials_failure=True)

    def test_env_flag(self):
        with mock.patch.dict(os.environ, {"GS_IMPORT_CONTINUE_ON_CREDENTIALS_FAILURE": "true"}):
            self.assertTrue(corehub.continue_on_credentials_failure_requested())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(corehub.continue_on_credentials_failure_requested())


class CredentialsTimeoutTests(unittest.TestCase):
    def test_credentials_put_gets_at_least_120_s(self):
        with mock.patch.dict(os.environ, {"CORE_HUB_TIMEOUT": "30", "CORE_HUB_CONNECT_TIMEOUT": "5"}):
            self.assertEqual(core_hub_client.credentials_request_timeout(), (5.0, 120.0))
        with mock.patch.dict(os.environ, {"CORE_HUB_TIMEOUT": "30", "CORE_HUB_CREDENTIALS_TIMEOUT": "200"}):
            self.assertEqual(core_hub_client.credentials_request_timeout()[1], 200.0)

    def test_the_put_is_sent_with_it(self):
        client = object.__new__(core_hub_client.CoreHubClient) if hasattr(core_hub_client, "CoreHubClient") else None
        if client is None:
            self.skipTest("no CoreHubClient class")
        client.session = mock.Mock()
        with mock.patch.dict(os.environ, {"CORE_HUB_TIMEOUT": "30"}):
            client._send_with_body("PUT", "https://hub/pipelines/p/agents/a/config/credentials", {}, {}, False, None)
            self.assertEqual(client.session.put.call_args.kwargs["timeout"][1], 120.0)
            client._send_with_body("PUT", "https://hub/pipelines/p/agents/a/config/specific", {}, {}, False, None)
            self.assertEqual(client.session.put.call_args.kwargs["timeout"][1], 30.0)


class ArchiveImportTests(unittest.TestCase):
    def _job(self, pending):
        job = app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None,
                                 continue_on_credentials_failure=True)
        job.credentials_pending.extend(pending)
        return job

    PENDING = [{"pipelineId": "p1", "agentId": "a1", "agentType": "SOURCE", "agentTag": "as400",
                "error": "500 Failed to initialize pool"}]

    def test_source_pending_means_no_discovery(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(app._entity_import_options(frozenset()))
            options = app._entity_import_options(frozenset({"SOURCE"}))
        self.assertTrue(options.no_discovery and options.allow_offline_entities)

    def test_pipeline_with_pending_credentials_is_not_marked_complete_and_target_tables_follow(self):
        job = self._job(self.PENDING + [dict(self.PENDING[0], agentId="a2", agentType="TARGET")])
        seen = []
        with mock.patch.dict(os.environ, {"GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING": "true"}), \
                mock.patch.object(app, "extract_all_schemas_from_yaml", return_value=[("S", "T")]), \
                mock.patch.object(app, "extract_schema_types_from_yaml", return_value=(None, None)), \
                mock.patch.object(app.state, "preferences", return_value={"enableScheduling": False,
                                                                          "createTables": True}), \
                mock.patch.object(app, "_create_schema_pair_entities",
                                  side_effect=lambda job, entities, s, t: seen.append(entities) or True), \
                mock.patch.object(app, "_mark_configuration_completed") as completed:
            app._create_imported_entities(job, "x.yaml", Path("x.yaml"), "p1", "Synth1")
        completed.assert_not_called()
        self.assertFalse(seen[0].create_tables)
        self.assertEqual(seen[0].pending_agent_types, frozenset({"SOURCE", "TARGET"}))

    def test_report_lines(self):
        job = self._job(self.PENDING)
        job.offline_report.append("x: 2 entities saved without discovery: S.A, S.B")
        text = "\n".join(app._credentials_pending_lines(job))
        self.assertIn("CREDENTIALS PENDING: pipeline p1, SOURCE agent as400 (id a1)", text)
        self.assertIn("Re-enter them in Core Hub", text)
        self.assertIn("saved without discovery", text)
        self.assertNotIn("UNVERIFIED", text)
        with mock.patch.dict(os.environ, {"GS_IMPORT_ATTEMPT_ENTITIES_WHEN_PENDING": "true"}):
            self.assertIn("refuses entities", "\n".join(app._credentials_pending_lines(job)))
        self.assertEqual(app._credentials_pending_lines(self._job([])), [])

    def test_offline_outcome_reads_the_report(self):
        job = self._job(self.PENDING)
        report = import_options.ImportReport()
        report.offline_entities.extend(["S.A"])
        entities = app._PipelineEntities(name="x", yaml_path=Path("x"), pipeline_id="p1", pipeline_name="n",
                                         source_type="SQL", target_type="SQL", enable_scheduling=False,
                                         create_tables=False, pending_agent_types=frozenset({"SOURCE"}))
        app._record_offline_outcome(job, entities, "S", "T", {"success": True, "result": {"report": report}})
        self.assertEqual(job.offline_report, ["x S->T: 1 entities saved without discovery: S.A"])


if __name__ == "__main__":
    unittest.main()

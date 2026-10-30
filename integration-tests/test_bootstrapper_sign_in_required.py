#!/usr/bin/env python3
"""
Regression tests for the "sign-in required" warning at the end of a restore.

U2M OAuth refresh tokens never leave Core Hub, so a restored connection with ``authMode: U2M`` has no sign-in and
its pipelines cannot run until a user signs in again. The restore lists each such connection as a warning:
"Sign-in required: <name> (<provider>). Open the connection in Gluesync and sign in."

Covers:
- the rule table: Databricks ``authMode`` U2M warns, the other modes do not, a rule without an agent tag applies to
  any agent, and a new row is all another OAuth connector needs
- the config-only import returns the warning, still creates the pipeline and sends the credentials
- the import-all response carries the warning (``warnings`` and the message), the restore succeeds
- the bootstrapper command logs the warning
"""

import ast
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import commons  # noqa: E402
from test_bootstrapper_custom_credentials import (  # noqa: E402
    DATABRICKS_CUSTOM, MASKED, _agents_config, _backup_zip,
)

logging.disable(logging.CRITICAL)

HINT = "Open the connection in Gluesync and sign in."
AUTH_MODES = ("SERVICE_PRINCIPAL", "PAT", "TOKEN_FEDERATION")


def warning_for(name, provider="Databricks"):
    return f"Sign-in required: {name} ({provider}). {HINT}"


def databricks_agent(auth_mode="U2M", name="dbx", **extra):
    agent = {
        "agentType": "TARGET",
        "agentTag": "databricks",
        "hostCredentials": {"connectionName": name, "host": "dbc.example.com", "password": "pw"},
        "customHostCredentials": dict(DATABRICKS_CUSTOM, authMode=auth_mode),
    }
    agent.update(extra)
    return agent


class SignInRuleTests(unittest.TestCase):
    def test_a_databricks_u2m_agent_needs_a_sign_in(self):
        self.assertEqual(commons.sign_in_required_warnings([databricks_agent()]), [warning_for("dbx")])

    def test_the_other_databricks_auth_modes_need_none(self):
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                self.assertEqual(commons.sign_in_required_warnings([databricks_agent(mode)]), [])

    def test_an_agent_without_an_auth_mode_or_custom_credentials_needs_none(self):
        bare = databricks_agent()
        bare["customHostCredentials"] = {"warehouseId": "wh-1"}
        none = databricks_agent()
        del none["customHostCredentials"]
        self.assertEqual(commons.sign_in_required_warnings([bare, none, "junk", None]), [])
        self.assertEqual(commons.sign_in_required_warnings(None), [])

    def test_the_mode_is_matched_ignoring_case_and_spaces(self):
        for mode in ("u2m", " U2M "):
            self.assertEqual(commons.sign_in_required_warnings([databricks_agent(mode)]), [warning_for("dbx")])

    def test_another_connector_with_the_same_key_is_not_asked_to_sign_in(self):
        agent = databricks_agent(agentTag="snowflake")
        self.assertEqual(commons.sign_in_required_warnings([agent]), [])

    def test_the_catalog_tag_is_matched_by_prefix_whatever_the_user_named_the_agent(self):
        agent = databricks_agent(agentTag="my-warehouse", agentInternalName="databricks")
        self.assertEqual(commons.sign_in_required_warnings([agent]), [warning_for("dbx")])

    def test_the_backup_name_of_the_custom_credentials_is_read_too(self):
        agent = databricks_agent()
        agent["hostCredentialsCustomProperties"] = agent.pop("customHostCredentials")
        self.assertEqual(commons.sign_in_required_warnings([agent]), [warning_for("dbx")])

    def test_a_masked_secret_does_not_hide_the_warning(self):
        agent = databricks_agent()
        agent["customHostCredentials"]["u2mClientSecret"] = MASKED
        self.assertEqual(commons.sign_in_required_warnings([agent]), [warning_for("dbx")])

    def test_the_connection_name_is_used_then_the_user_tag_then_the_tag(self):
        named = databricks_agent(name="Lakehouse")
        unnamed = databricks_agent(agentUserTag="tgt-user")
        unnamed["hostCredentials"] = {"host": "h"}
        bare = databricks_agent()
        bare["hostCredentials"] = {}
        self.assertEqual(commons.sign_in_required_warnings([named]), [warning_for("Lakehouse")])
        self.assertEqual(commons.sign_in_required_warnings([unnamed]), [warning_for("tgt-user")])
        self.assertEqual(commons.sign_in_required_warnings([bare]), [warning_for("databricks")])

    def test_every_agent_is_listed_and_the_same_connection_once(self):
        agents = [databricks_agent(name="a"), databricks_agent("PAT", name="b"), databricks_agent(name="c"),
                  databricks_agent(name="a")]
        self.assertEqual(commons.sign_in_required_warnings(agents), [warning_for("a"), warning_for("c")])

    def test_a_rule_without_an_agent_tag_applies_to_any_agent(self):
        rules = (commons.SignInRule(None, "oauthMode", "browser", "Acme"),)
        agent = {"agentTag": "acme-cloud", "customHostCredentials": {"oauthMode": "BROWSER"}}
        other = {"agentTag": "mssql", "customHostCredentials": {"oauthMode": "password"}}
        self.assertEqual(
            commons.sign_in_required_warnings([agent, other], rules),
            ["Sign-in required: acme-cloud (Acme). " + HINT],
        )

    def test_the_table_lists_databricks(self):
        self.assertEqual(
            [(r.agent_tag_prefix, r.credential_key, r.value, r.provider) for r in commons.SIGN_IN_RULES],
            [("databricks", "authMode", "U2M", "Databricks")],
        )


class FakeCoreHubFetch:
    """Core Hub stand-in for the config-only import: records the calls and answers what the import reads."""

    def __init__(self):
        self.calls = []

    def __call__(self, path, method="GET", **kwargs):
        self.calls.append((path, method, kwargs.get("body")))
        if path == "/pipelines" and method == "POST":
            return {"pipelineId": "new-1"}
        if path == "/pipelines":
            return []
        return {}

    def credential_bodies(self):
        return [body for path, method, body in self.calls if path.endswith("/config/credentials") and method == "PUT"]


class ImportConfigOnlyTests(unittest.TestCase):
    def _import(self, agents):
        import automator_app.corehub as corehub

        fetch = FakeCoreHubFetch()
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fetch), \
             mock.patch.object(corehub, "_add_agent_to_pipeline_via_corehub", side_effect=lambda **kw: kw["agent_user_tag"]):
            result = corehub.import_pipeline_config_only(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                config={"pipelineName": "P", "agents": agents},
            )
        return result, fetch

    def test_a_u2m_agent_is_listed_and_the_import_succeeds(self):
        result, fetch = self._import([databricks_agent()])
        self.assertEqual(result["pipelineId"], "new-1")
        self.assertEqual(result["warnings"], [warning_for("dbx")])
        self.assertEqual(len(fetch.credential_bodies()), 1)
        self.assertEqual(fetch.credential_bodies()[0]["customHostCredentials"]["authMode"], "U2M")

    def test_the_other_auth_modes_have_no_warning(self):
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                result, _ = self._import([databricks_agent(mode)])
                self.assertEqual(result["warnings"], [])
                self.assertEqual(result["pipelineId"], "new-1")

    def test_the_warning_is_logged(self):
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs("automator_app.corehub", level="WARNING") as logs:
            self._import([databricks_agent()])
        self.assertTrue(any(warning_for("dbx") in line for line in logs.output), logs.output)


class ImportAllResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.state = state
        cls.client = TestClient(app_module.create_app())
        cls.app_module = app_module

    def setUp(self):
        import automator_app.corehub as corehub

        self.state.set_auth("tok", "http://test", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)
        self.fetch = FakeCoreHubFetch()
        entities = {"success": True}
        self.created_entities = mock.Mock(return_value=entities)
        for patcher in (
            mock.patch.object(corehub, "fetch_core_hub", side_effect=self.fetch),
            mock.patch.object(corehub, "_add_agent_to_pipeline_via_corehub", side_effect=lambda **kw: kw["agent_user_tag"]),
            mock.patch.object(corehub, "run_create_entities", self.created_entities),
            mock.patch.object(self.app_module.create_user_defined_functions, "main", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore(self, auth_mode):
        config = _agents_config(dict(DATABRICKS_CUSTOM, authMode=auth_mode))
        config["agents"][0]["hostCredentials"]["connectionName"] = "dbx"
        return self.client.post(
            "/api/import/all", files={"file": ("backup.zip", _backup_zip(config), "application/zip")}
        )

    def test_the_response_lists_the_connection_to_sign_in_to(self):
        response = self._restore("U2M")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["warnings"], [warning_for("dbx")])
        self.assertIn("Imported: First Pipeline (new-1)", body["message"])
        self.assertIn(warning_for("dbx"), body["message"])
        self.assertFalse(body["logs"])

    def test_the_restore_goes_on_with_the_credentials_and_the_entities(self):
        self._restore("U2M")
        self.assertEqual(len(self.fetch.credential_bodies()), 1)
        self.assertEqual(self.fetch.credential_bodies()[0]["customHostCredentials"]["authMode"], "U2M")
        self.assertTrue(self.created_entities.called)

    def test_the_other_auth_modes_have_no_warning(self):
        for mode in AUTH_MODES:
            with self.subTest(mode=mode):
                response = self._restore(mode)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertTrue(body["success"])
                self.assertIsNone(body["warnings"])
                self.assertNotIn("Sign-in required", body["message"])


class BootstrapperCommandTests(unittest.TestCase):
    """main.py runs its pipeline at import time, so only the function under test is loaded."""

    def _warn(self, agents):
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
        wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_warn_sign_in_required"]
        self.assertEqual(len(wanted), 1)
        logger = logging.getLogger("test-sign-in-required")
        namespace = {"logger": logger, "sign_in_required_warnings": commons.sign_in_required_warnings}
        exec(compile(ast.Module(body=wanted, type_ignores=[]), "main.py", "exec"), namespace)  # noqa: S102
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        with self.assertLogs(logger, level="WARNING") as logs:
            logger.warning("start")
            namespace["_warn_sign_in_required"](agents)
        return [line.split(":", 2)[2] for line in logs.output][1:]

    def test_the_u2m_connection_is_logged_as_a_warning(self):
        self.assertEqual(self._warn([databricks_agent()]), [warning_for("dbx")])

    def test_the_other_auth_modes_log_nothing(self):
        self.assertEqual(self._warn([databricks_agent("PAT")]), [])


if __name__ == "__main__":
    unittest.main()

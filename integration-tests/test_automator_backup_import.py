#!/usr/bin/env python3
"""
Regression tests for Automator backup/import flows.

Covers:
- import_pipeline_config_only (import agent config into a new pipeline)
- duplicate_pipeline (clone a pipeline with new name/schema)
- import_global_notifications (restore SMTP settings from backup)
- import_users (restore user list from backup)
- import_oidc_config (restore OIDC settings from backup)
- import-all skip-list for global-config.yaml

Mocks fetch_core_hub on automator_app.corehub since all CoreHub calls
in these functions go through the locally imported name.
For duplicate_pipeline, export_pipeline_yaml is also mocked to avoid
HTTP calls inside export_template_from_corehub.py helpers.
"""

import io
import logging
import sys
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SIMPLE_IMPORT_CONFIG = {
    "pipelineName": "ImportedPipe",
    "agents": [
        {
            "agentType": "SOURCE",
            "agentTag": "mssql",
            "agentInternalName": "mssql",
            "hostCredentials": {
                "connectionName": "src",
                "host": "1.2.3.4",
                "port": 1433,
                "password": "src-pass",
            },
            "specificConfiguration": {"bulkSize": 100},
        },
        {
            "agentType": "TARGET",
            "agentTag": "mysql",
            "agentInternalName": "mysql",
            "hostCredentials": {
                "connectionName": "tgt",
                "host": "5.6.7.8",
                "port": 3306,
                "password": "tgt-pass",
            },
        },
    ],
}


class ImportPipelineConfigOnlyTests(unittest.TestCase):
    """Verify import_pipeline_config_only creates a pipeline with agents."""

    def test_import_creates_pipeline_and_binds_agents(self):
        import automator_app.corehub as corehub

        captured_calls = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            captured_calls.append((path, method))
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "new-pipe-id", "name": body.get("name")}
            if "/agents/add" in path:
                return {"agentId": "added-agent-1"}
            if path.endswith("/agents/add"):
                return {"agentId": "added-agent-1"}
            if "/config/credentials" in path and method == "PUT":
                return {"ok": True}
            if "/config/specific" in path and method == "PUT":
                return {"ok": True}
            if "/agents/" in path and method == "PUT":
                return {"ok": True}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}):
            result = corehub.import_pipeline_config_only(
                token="tok",
                base_url="http://test",
                use_ssl=False,
                skip_verify=False,
                config=SIMPLE_IMPORT_CONFIG,
            )

        self.assertEqual(result["pipelineId"], "new-pipe-id")
        self.assertEqual(result["pipelineName"], "ImportedPipe")

        post_calls = [c for c in captured_calls if c == ("/pipelines", "POST")]
        self.assertTrue(post_calls, "Expected POST /pipelines")

    def test_duplicate_name_avoids_collision_with_suffix(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path == "/pipelines" and method == "GET":
                return [
                    {"pipelineId": "p1", "name": "ImportedPipe"},
                    {"pipelineId": "p2", "name": "ImportedPipe (restored)"},
                ]
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "new-id", "name": body.get("name")}
            if "/agents/add" in path:
                return {"agentId": "a1"}
            if path.endswith("/agents/add"):
                return {"agentId": "a1"}
            if "/config/credentials" in path and method == "PUT":
                return {"ok": True}
            if "/agents/" in path and method == "PUT":
                return {"ok": True}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}):
            result = corehub.import_pipeline_config_only(
                token="tok",
                base_url="http://test",
                use_ssl=False,
                skip_verify=False,
                config=SIMPLE_IMPORT_CONFIG,
            )

        self.assertEqual(result["pipelineName"], "ImportedPipe (restored 2)")


class DuplicatePipelineTests(unittest.TestCase):
    """Verify duplicate_pipeline clones with new agent tags."""

    def test_duplicate_creates_new_pipeline(self):
        import automator_app.corehub as corehub

        captured_calls = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            captured_calls.append((path, method))
            if path == f"/pipelines/original-pipe/config" and method == "GET":
                return {
                    "agents": [
                        {"agentType": "SOURCE", "agentTag": "mssql", "agentId": "src1"},
                        {"agentType": "TARGET", "agentTag": "mysql", "agentId": "tgt1"},
                    ]
                }
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "cloned-pipe", "name": body.get("name")}
            if "/agents/add" in path:
                return {"agentId": "new-agent-1"}
            if path.endswith("/agents/add"):
                return {"agentId": "new-agent-1"}
            if "/agents/" in path and method == "PUT":
                return {"ok": True}
            if "/config/credentials" in path and method == "PUT":
                return {"ok": True}
            if "/config/specific" in path and method == "PUT":
                return {"ok": True}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(corehub, "export_pipeline_yaml", return_value="schemas:\n  demo:\n    target: public\n"), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}):
            result = corehub.duplicate_pipeline(
                token="tok",
                base_url="http://test",
                pipeline_id="original-pipe",
                new_pipeline_name="ClonedPipe",
                source_agent_tag="mssql",
                target_agent_tag="mysql",
                source_agent_password="src-pass",
                target_agent_password="tgt-pass",
                clone_entities=False,
                use_ssl=False,
                skip_verify=False,
            )

        self.assertEqual(result["newPipelineId"], "cloned-pipe")
        self.assertEqual(result["newPipelineName"], "ClonedPipe")

        post_calls = [c for c in captured_calls if c == ("/pipelines", "POST")]
        self.assertTrue(post_calls)

    def test_duplicate_with_entity_clone(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path == f"/pipelines/original-pipe/config" and method == "GET":
                return {
                    "agents": [
                        {"agentType": "SOURCE", "agentTag": "mssql", "agentId": "src1"},
                        {"agentType": "TARGET", "agentTag": "mysql", "agentId": "tgt1"},
                    ]
                }
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "cloned-pipe", "name": body.get("name")}
            if "/agents/add" in path:
                return {"agentId": "new-agent-1"}
            if path.endswith("/agents/add"):
                return {"agentId": "new-agent-1"}
            if "/agents/" in path and method == "PUT":
                return {"ok": True}
            if "/config/credentials" in path and method == "PUT":
                return {"ok": True}
            if "/config/entities" in path and method == "PUT":
                return {"ok": True}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(corehub, "export_pipeline_yaml", return_value="schemas:\n  demo:\n    target: public\n"), \
             mock.patch.object(corehub, "run_create_entities", return_value={"success": True, "result": {"errors": []}}), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}):
            result = corehub.duplicate_pipeline(
                token="tok",
                base_url="http://test",
                pipeline_id="original-pipe",
                new_pipeline_name="ClonedPipe",
                source_agent_tag="mssql",
                target_agent_tag="mysql",
                source_agent_password="src-pass",
                target_agent_password="tgt-pass",
                clone_entities=True,
                use_ssl=False,
                skip_verify=False,
            )

        self.assertEqual(result["newPipelineId"], "cloned-pipe")


class GlobalConfigFileSkipTests(unittest.TestCase):
    """Verify global-config.yaml files are excluded from pipeline YAML scanning."""

    def test_global_config_in_skip_list(self):
        import automator_app.app as app_module

        # Stems skipped when scanning ZIP contents for pipeline YAMLs
        skip_stems = getattr(app_module, "GLOBAL_CONFIG_NAMES", set())
        self.assertIn("global-config", skip_stems)
        self.assertIn("global-configs", skip_stems)
        self.assertIn("smtp", skip_stems)


class ImportUsersTests(unittest.TestCase):
    """Verify import_users restores user list from backup."""

    def test_uses_bulk_import_endpoint_when_available(self):
        import automator_app.corehub as corehub

        calls: list[tuple[str, str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            body = kwargs.get("body")
            calls.append((method, path, body))
            if path == "/users/import" and method == "POST":
                return [{"id": "1", "username": "admin", "role": "SUPER_ADMIN"}]
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": [{"id": "1", "username": "admin", "role": "SUPER_ADMIN", "passwordHash": "abc123"}]},
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["note"], "passwords restored from backup hashes")

        import_calls = [c for c in calls if c[0] == "POST" and c[1] == "/users/import"]
        self.assertTrue(import_calls, "Expected POST /users/import")
        self.assertEqual(import_calls[0][2]["users"][0]["passwordHash"], "abc123")

    def test_fallback_creates_new_users_with_temp_password(self):
        import automator_app.corehub as corehub

        calls: list[tuple[str, str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            body = kwargs.get("body")
            calls.append((method, path, body))
            if path == "/users/import" and method == "POST":
                raise Exception("404 Client Error: Not Found")
            if path == "/users" and method == "GET":
                return []  # no existing users
            if path == "/users" and method == "POST":
                return {"id": "new-id", "username": body.get("username") if body else None}
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": [{"id": "1", "username": "admin", "role": "SUPER_ADMIN", "passwordHash": "abc123"}]},
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertIn("passwords NOT restored", result["note"])

        post_calls = [c for c in calls if c[0] == "POST" and c[1] == "/users"]
        self.assertTrue(post_calls, "Expected POST /users for new user")
        self.assertEqual(post_calls[0][2]["username"], "admin")
        # A random 16-char password should have been generated for fallback
        self.assertEqual(len(post_calls[0][2]["password"]), 16)

    def test_fallback_updates_existing_user_profile(self):
        import automator_app.corehub as corehub

        calls: list[tuple[str, str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            body = kwargs.get("body")
            calls.append((method, path, body))
            if path == "/users/import" and method == "POST":
                raise Exception("404 Client Error: Not Found")
            if path == "/users" and method == "GET":
                return [{"id": "existing-id", "username": "admin", "role": "MANAGER"}]
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": [{"id": "1", "username": "admin", "role": "SUPER_ADMIN", "name": "Admin User", "passwordHash": "abc123"}]},
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 1)

        put_calls = [c for c in calls if c[0] == "PUT" and "/users/" in c[1]]
        self.assertTrue(put_calls, "Expected PUT /users/{id} for existing user")
        self.assertEqual(put_calls[0][2]["role"], "SUPER_ADMIN")
        self.assertEqual(put_calls[0][2]["name"], "Admin User")

    def test_skips_oidc_users(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", **kwargs):
            if path == "/users/import" and method == "POST":
                return []
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": [{"id": "1", "username": "oidc-user", "role": "VIEWER", "isOidcUser": True, "passwordHash": "abc123"}]},
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no importable users")

    def test_fallback_restores_users_without_password_hash(self):
        import automator_app.corehub as corehub

        calls: list[tuple[str, str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            body = kwargs.get("body")
            calls.append((method, path, body))
            if path == "/users/import" and method == "POST":
                raise Exception("404 Client Error: Not Found")
            if path == "/users" and method == "GET":
                return []  # no existing users
            if path == "/users" and method == "POST":
                return {"id": "new-id", "username": body.get("username") if body else None}
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": [{"id": "1", "username": "admin", "role": "SUPER_ADMIN"}]},
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(result["created"], 1)
        self.assertIn("passwords NOT restored", result["note"])

    def test_skips_when_users_missing(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={},
            )

        self.assertEqual(result["status"], "skipped")

    def test_skips_when_users_exported_with_error(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                users_data={"users": {"error": "permission denied"}},
            )

        self.assertEqual(result["status"], "skipped")


class ImportOidcConfigTests(unittest.TestCase):
    """Verify import_oidc_config restores OIDC settings from backup."""

    def test_restores_oidc_configuration(self):
        import automator_app.corehub as corehub

        put_calls: list[tuple[str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_oidc_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                oidc_data={
                    "oidc_configuration": {"enabled": True, "providerName": "Auth0"},
                    "oidc_auth_url": {"authorizationUrl": "http://auth"},
                },
            )

        self.assertEqual(result["oidc_configuration"]["status"], "restored")
        self.assertEqual(result["oidc_auth_url"]["status"], "restored")
        paths = {p for p, _ in put_calls}
        self.assertIn("/oidc/configuration", paths)
        self.assertIn("/oidc/auth-url", paths)

    def test_skips_missing_oidc_keys(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_oidc_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                oidc_data={"oidc_configuration": {"enabled": True}},
            )

        self.assertEqual(result["oidc_configuration"]["status"], "restored")
        self.assertEqual(result["oidc_auth_url"]["status"], "skipped")

    def test_skips_errored_oidc_data(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_oidc_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                oidc_data={
                    "oidc_configuration": {"error": "not configured"},
                    "oidc_auth_url": {"authorizationUrl": "http://auth"},
                },
            )

        self.assertEqual(result["oidc_configuration"]["status"], "skipped")
        self.assertEqual(result["oidc_auth_url"]["status"], "restored")


if __name__ == "__main__":
    unittest.main()

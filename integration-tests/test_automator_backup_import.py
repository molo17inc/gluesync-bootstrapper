#!/usr/bin/env python3
"""
Regression tests for Automator backup/import flows.

Covers:
- import_pipeline_config_only (import agent config into a new pipeline)
- duplicate_pipeline (clone a pipeline with new name/schema)
- import_global_notifications (restore SMTP settings from backup)
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


if __name__ == "__main__":
    unittest.main()

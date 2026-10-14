#!/usr/bin/env python3
"""Regressions of the Automator web app found by the SonarQube sweep of main.

- The single-section exports (global configs, SMTP, webhooks, thresholds, schedules, users, OIDC)
  returned ``JSONResponse`` without importing it: every one answered 500.
- Bulk create with no source/target type looked the types up through a state method that does not
  exist, so a NoSQL pipeline was always treated as SQL (and its target tables created).
- Import all with an empty certificate found by its exact path crashed on an unbound
  ``pipeline_folder`` instead of reporting the certificate.
"""

import io
import logging
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _AppTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        import automator_app.corehub as corehub
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.app_module = app_module
        cls.corehub = corehub
        cls.state = state
        cls.client = TestClient(app_module.create_app())

    def setUp(self):
        self.state.set_auth("tok", "http://test", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


class SectionExportTests(_AppTestCase):
    ROUTES = {
        "/api/export/global-configs": "export_global_configs",
        "/api/export/smtp": "export_smtp_settings",
        "/api/export/webhooks": "export_webhooks_config",
        "/api/export/thresholds": "export_thresholds",
        "/api/export/schedules": "export_schedules",
        "/api/export/users": "export_users",
        "/api/export/oidc": "export_oidc_config",
    }

    def test_every_section_export_answers_its_json(self):
        for route, exporter in self.ROUTES.items():
            with self.subTest(route=route):
                payload = {"section": exporter, "items": [1, 2]}
                self.patch(self.corehub, exporter, return_value=payload)
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), payload)


class BulkCreateTypeTests(_AppTestCase):
    BODY = {"pipelineId": "p-1", "sourceSchema": "src", "targetSchema": "tgt", "tableNames": ["T1"]}

    def setUp(self):
        super().setUp()
        self.run = self.patch(self.corehub, "run_create_entities_for_tables", return_value={"success": True})

    def _post(self, **overrides):
        response = self.client.post("/api/bulk/create", json={**self.BODY, **overrides})
        self.assertEqual(response.status_code, 200, response.text)
        return self.run.call_args.kwargs

    def test_empty_types_come_from_the_pipeline_agents(self):
        infer = self.patch(self.corehub, "infer_agent_schema_types", return_value=("SQL", "NoSQL"))

        kwargs = self._post(createTables=True)

        infer.assert_called_once()
        self.assertEqual(infer.call_args.kwargs["pipeline_id"], "p-1")
        self.assertEqual((kwargs["source_type"], kwargs["target_type"]), ("SQL", "NoSQL"))
        self.assertFalse(kwargs["create_tables"], "a NoSQL target never gets tables created")

    def test_given_types_are_kept(self):
        infer = self.patch(self.corehub, "infer_agent_schema_types", return_value=("NoSQL", "NoSQL"))

        kwargs = self._post(sourceType="SQL", targetType="SQL")

        infer.assert_not_called()
        self.assertEqual((kwargs["source_type"], kwargs["target_type"]), ("SQL", "SQL"))

    def test_only_the_missing_type_is_inferred(self):
        self.patch(self.corehub, "infer_agent_schema_types", return_value=("NoSQL", "NoSQL"))

        kwargs = self._post(sourceType="SQL")

        self.assertEqual((kwargs["source_type"], kwargs["target_type"]), ("SQL", "NoSQL"))

    def test_types_fall_back_to_sql_when_core_hub_cannot_tell(self):
        self.patch(self.corehub, "infer_agent_schema_types", side_effect=RuntimeError("Core Hub down"))

        kwargs = self._post()

        self.assertEqual((kwargs["source_type"], kwargs["target_type"]), ("SQL", "SQL"))


def _pipeline_yaml():
    return yaml.safe_dump(
        {
            "exportMetadata": {"pipelineId": "aaaa1111", "pipelineName": "Certified"},
            "dbo": {"target": "public", "sourceType": "SQL", "targetType": "SQL",
                    "tables": {"whitelist": [], "custom": {}}},
        },
        sort_keys=False,
    )


AGENTS_CONFIG = {
    "agents": [
        {"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-1",
         "hostCredentials": {"host": "1.1.1.1"}, "customHostCredentials": {}, "specificConfiguration": {}},
        {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-1",
         "hostCredentials": {"host": "2.2.2.2"}, "customHostCredentials": {}, "specificConfiguration": {}},
    ],
    "pipelineAgents": {"aaaa1111": ["src-1", "tgt-1"]},
}


class ImportAllCertificateTests(_AppTestCase):
    CERT_PATH = "certs/ca.pem"

    def setUp(self):
        super().setUp()

        def fake_import_config_only(**kwargs):
            # Core Hub defers the credentials of an agent with a certificate, as import_pipeline_config_only does.
            for agent in kwargs["config"]["agents"]:
                if agent.get("agentType") == "SOURCE":
                    agent.update(_certificate_path=self.CERT_PATH, _certificate_type="CA", _agent_id="new-src")
            return {"pipelineId": "new-1", "pipelineName": kwargs["config"].get("pipelineName")}

        self.patch(self.corehub, "import_pipeline_config_only", side_effect=fake_import_config_only)
        self.patch(self.corehub, "run_create_entities", return_value={"success": True})
        self.patch(self.corehub, "fetch_core_hub", return_value={})
        self.patch(self.app_module.create_user_defined_functions, "main", return_value=None)
        self.upload = self.patch(self.corehub, "upload_agent_certificate", return_value=None)

    def _import(self, certificate):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("agents-config.yaml", yaml.safe_dump(AGENTS_CONFIG, sort_keys=False))
            zf.writestr("backup_Certified_aaaa1111_20260610_112407.yaml", _pipeline_yaml())
            if certificate is not None:
                zf.writestr(self.CERT_PATH, certificate)
        response = self.client.post("/api/import/all", files={"file": ("backup.zip", buf.getvalue(), "application/zip")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _text(self, result):
        return " ".join([result.get("message") or ""] + (result.get("logs") or []))

    def test_certificate_found_by_exact_path_is_uploaded(self):
        self._import(b"-----BEGIN CERTIFICATE-----\n")

        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["certificate_data"], b"-----BEGIN CERTIFICATE-----\n")
        self.assertEqual(self.upload.call_args.kwargs["certificate_ext"], "pem")

    def test_empty_certificate_is_reported_not_crashed_on(self):
        result = self._import(b"")

        self.upload.assert_not_called()
        text = self._text(result)
        self.assertIn("is empty", text)
        self.assertNotIn("pipeline_folder", text)

    def test_missing_certificate_is_reported(self):
        result = self._import(None)

        self.upload.assert_not_called()
        self.assertIn("not found in ZIP", self._text(result))


if __name__ == "__main__":
    unittest.main()

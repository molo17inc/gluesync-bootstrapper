#!/usr/bin/env python3
"""Lock CoreHub contract: credentials (connection) before certificate upload.

Hub 2.2.11.1+ returns 500
"Agent <id> has no connection: configure its credentials before uploading a certificate"
when PUT .../config/certificate/cert-store happens before credentials.

GCP cert-store kits (google-bigquery, google-cloud-storage) hit this path.
The same bootstrapper 2.7.1 was green on Hub 2.2.10.12.
"""

import ast
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


GCP_BQ_AGENT = {
    "agentId": "5528c919",
    "agentType": "TARGET",
    "agentTag": "google-bigquery",
    "hostCredentials": {
        "connectionName": "bigquery",
        "host": "gluesync-demo",
        "certificatePath": "/cert.json",
        "certificateType": "cert-store",
    },
    "customHostCredentials": {},
}

GCP_GCS_AGENT = {
    "agentId": "0ccfd1b2",
    "agentType": "TARGET",
    "agentTag": "google-cloud-storage",
    "hostCredentials": {
        "connectionName": "gcs",
        "host": "gluesync-demo",
        "certificatePath": "/gcs-sa.json",
        "certificateType": "cert-store",
    },
    "customHostCredentials": {},
}


class AgentCredentialThenCertOrderTests(unittest.TestCase):
    """apply_agent_host_credentials_and_certificate must PUT creds before cert-store."""

    def _run(self, agent):
        import commons

        calls = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            calls.append(("credentials", method, path, body))
            return {}

        def fake_upload(**kwargs):
            calls.append(
                (
                    "certificate",
                    kwargs["certificate_type"],
                    kwargs["certificate_path"],
                    kwargs["agent_id"],
                )
            )

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            commons.apply_agent_host_credentials_and_certificate(
                token="tok",
                pipeline_id="pipe-1",
                agent=agent,
                upload_certificate_fn=fake_upload,
            )
        return calls

    def test_gcp_bigquery_cert_store_uploads_credentials_before_certificate(self):
        calls = self._run(dict(GCP_BQ_AGENT, hostCredentials=dict(GCP_BQ_AGENT["hostCredentials"])))
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "credentials")
        self.assertEqual(calls[0][1], "PUT")
        self.assertIn("/agents/5528c919/config/credentials", calls[0][2])
        creds_body = calls[0][3]
        self.assertNotIn("certificatePath", creds_body["hostCredentials"])
        self.assertNotIn("certificateType", creds_body["hostCredentials"])
        self.assertEqual(creds_body["hostCredentials"]["connectionName"], "bigquery")
        self.assertEqual(calls[1][0], "certificate")
        self.assertEqual(calls[1][1], "cert-store")
        self.assertEqual(calls[1][2], "/cert.json")
        self.assertEqual(calls[1][3], "5528c919")

    def test_gcp_gcs_cert_store_uploads_credentials_before_certificate(self):
        calls = self._run(dict(GCP_GCS_AGENT, hostCredentials=dict(GCP_GCS_AGENT["hostCredentials"])))
        self.assertEqual(calls[0][0], "credentials")
        self.assertEqual(calls[1][0], "certificate")
        self.assertEqual(calls[1][1], "cert-store")
        self.assertEqual(calls[1][3], "0ccfd1b2")

    def test_no_certificate_only_puts_credentials(self):
        agent = {
            "agentId": "plain-1",
            "hostCredentials": {"connectionName": "src", "host": "1.2.3.4"},
            "customHostCredentials": {},
        }
        calls = self._run(agent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "credentials")

    def test_missing_agent_id_is_noop(self):
        calls = self._run({"hostCredentials": {"certificatePath": "/x", "certificateType": "cert-store"}})
        self.assertEqual(calls, [])

    def test_main_loop_uses_shared_helper(self):
        tree = ast.parse((PROJECT_ROOT / "main.py").read_text())
        helper_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "apply_agent_host_credentials_and_certificate"
        ]
        self.assertTrue(
            helper_calls,
            "main.py must apply credentials+cert via apply_agent_host_credentials_and_certificate",
        )

    def test_helper_uploads_credentials_before_calling_certificate_fn(self):
        """Source-order lock: fetch_core_hub (creds) appears before upload_certificate_fn."""
        tree = ast.parse((PROJECT_ROOT / "commons.py").read_text())
        helper = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "apply_agent_host_credentials_and_certificate":
                helper = node
                break
        self.assertIsNotNone(helper)
        names = []
        for node in ast.walk(helper):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.append(func.id)
        self.assertIn("fetch_core_hub", names)
        self.assertIn("upload_certificate_fn", names)
        self.assertLess(names.index("fetch_core_hub"), names.index("upload_certificate_fn"))


if __name__ == "__main__":
    unittest.main()

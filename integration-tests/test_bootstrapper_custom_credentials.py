#!/usr/bin/env python3
"""
Regression tests for the custom host credentials of an agent in a backup.

Core Hub reports them as ``hostCredentialsCustomProperties``; the backup used to read ``customHostCredentials``,
which a Core Hub response never has, and dropped them (Databricks authMode, warehouseId, catalog, ...).

Covers:
- the backup writes the custom host credentials under ``customHostCredentials`` (either source name)
- custom secrets are masked when secrets are excluded, and only those
- a ``<hidden>`` placeholder returned by Core Hub is written as the masked sentinel even with include_secrets
- the restore refuses a masked custom secret like a masked password, and never sends a placeholder to Core Hub
- a backup without custom host credentials restores as before
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

MASKED = "*******"
HIDDEN = "<hidden>"

# What Core Hub stores for a Databricks target: non-secret settings and a few secrets.
DATABRICKS_CUSTOM = {
    "authMode": "U2M",
    "warehouseId": "wh-123",
    "catalog": "main",
    "stagingVolume": "/Volumes/main/gluesync/staging",
    "federationJwtFilePath": "/etc/gluesync/jwt.txt",
    "u2mClientId": "client-id-1",
    "federationIdpClientSecret": "idp-secret",
    "u2mClientSecret": "u2m-secret",
    "openLineageApiSecret": "ol-secret",
}
SECRET_KEYS = ("federationIdpClientSecret", "u2mClientSecret", "openLineageApiSecret")
PLAIN_KEYS = tuple(key for key in DATABRICKS_CUSTOM if key not in SECRET_KEYS)


def _agent(custom_key="hostCredentialsCustomProperties", custom=None):
    agent = {
        "agentType": "TARGET",
        "agentTag": "databricks",
        "agentId": "tgt-1",
        "hostCredentials": {"connectionName": "dbx", "host": "dbc.example.com", "port": 443, "password": "pw"},
    }
    if custom is not None:
        agent[custom_key] = custom
    return agent


def _entry(agent, include_secrets):
    import automator_app.corehub as corehub

    identity = corehub._backup_agent_identity(agent)
    return corehub._backup_agent_entry(agent, identity, include_secrets)


class BackupCustomCredentialsTests(unittest.TestCase):
    def test_core_hub_name_is_written_under_the_restore_name(self):
        entry = _entry(_agent(custom=DATABRICKS_CUSTOM), include_secrets=True)
        self.assertEqual(entry["customHostCredentials"], DATABRICKS_CUSTOM)
        self.assertNotIn("hostCredentialsCustomProperties", entry)

    def test_config_name_is_still_read(self):
        agent = _agent(custom_key="customHostCredentials", custom=DATABRICKS_CUSTOM)
        self.assertEqual(_entry(agent, include_secrets=True)["customHostCredentials"], DATABRICKS_CUSTOM)

    def test_agent_without_custom_credentials_keeps_an_empty_map(self):
        self.assertEqual(_entry(_agent(), include_secrets=False)["customHostCredentials"], {})

    def test_secrets_are_masked_and_nothing_else_when_secrets_are_excluded(self):
        custom = _entry(_agent(custom=DATABRICKS_CUSTOM), include_secrets=False)["customHostCredentials"]
        for key in SECRET_KEYS:
            self.assertEqual(custom[key], MASKED, key)
        for key in PLAIN_KEYS:
            self.assertEqual(custom[key], DATABRICKS_CUSTOM[key], key)

    def test_every_secret_key_name_core_hub_obfuscates_is_masked(self):
        names = ["password", "logminerPassword", "clientSecret", "authToken", "private_key", "privateKey",
                 "credentials", "apiKey", "api_key", "accessKey", "ACCESS_KEY", "passphrase"]
        custom = _entry(_agent(custom={name: "v" for name in names}), include_secrets=False)["customHostCredentials"]
        self.assertEqual(custom, {name: MASKED for name in names})

    def test_nested_secrets_are_masked_and_empty_or_non_string_values_are_kept(self):
        custom = {"oauth": {"clientSecret": "s", "scope": "all"}, "apiKey": "", "tokenTtl": 30, "secretList": ["a"]}
        masked = _entry(_agent(custom=custom), include_secrets=False)["customHostCredentials"]
        self.assertEqual(masked, {"oauth": {"clientSecret": MASKED, "scope": "all"}, "apiKey": "", "tokenTtl": 30,
                                  "secretList": ["a"]})

    def test_source_credentials_are_not_modified(self):
        custom = dict(DATABRICKS_CUSTOM)
        _entry(_agent(custom=custom), include_secrets=False)
        self.assertEqual(custom, DATABRICKS_CUSTOM)

    def test_hidden_placeholder_of_core_hub_is_written_as_the_masked_sentinel(self):
        custom = {"authMode": "U2M", "u2mClientSecret": HIDDEN}
        for include_secrets in (True, False):
            masked = _entry(_agent(custom=custom), include_secrets)["customHostCredentials"]
            self.assertEqual(masked, {"authMode": "U2M", "u2mClientSecret": MASKED})

    def test_full_backup_zip_round_trips_custom_credentials(self):
        import automator_app.corehub as corehub

        for include_secrets in (True, False):
            with self.subTest(include_secrets=include_secrets):
                zip_bytes = self._full_backup([_agent(custom=DATABRICKS_CUSTOM)], include_secrets)
                agents = self._agents_config(zip_bytes)["agents"]
                custom = agents[0]["customHostCredentials"]
                for key in PLAIN_KEYS:
                    self.assertEqual(custom[key], DATABRICKS_CUSTOM[key])
                for key in SECRET_KEYS:
                    self.assertEqual(custom[key], DATABRICKS_CUSTOM[key] if include_secrets else MASKED)
                if not include_secrets:
                    text = self._agents_config_text(zip_bytes)
                    for secret in ("idp-secret", "u2m-secret", "ol-secret"):
                        self.assertNotIn(secret, text)
        self.assertTrue(corehub.has_masked_custom_secret(custom))

    @staticmethod
    def _full_backup(agents, include_secrets):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=[]), \
             mock.patch.object(corehub, "build_entities_maps", return_value={}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"databricks": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "fetch_core_hub", return_value={"pipelineId": "pipe", "name": "P"}):
            return corehub.export_pipeline_full_backup(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False, include_secrets=include_secrets,
            )

    @staticmethod
    def _agents_config_text(zip_bytes):
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            name = next(n for n in zf.namelist() if n.endswith("agents-config.yaml"))
            return zf.read(name).decode("utf-8")

    def _agents_config(self, zip_bytes):
        return yaml.safe_load(self._agents_config_text(zip_bytes))


class MaskedCustomSecretHelpersTests(unittest.TestCase):
    def test_detection_covers_both_placeholders_at_any_depth(self):
        import automator_app.corehub as corehub

        self.assertTrue(corehub.has_masked_custom_secret({"u2mClientSecret": MASKED}))
        self.assertTrue(corehub.has_masked_custom_secret({"u2mClientSecret": HIDDEN}))
        self.assertTrue(corehub.has_masked_custom_secret({"oauth": {"clientSecret": HIDDEN}}))
        self.assertFalse(corehub.has_masked_custom_secret(DATABRICKS_CUSTOM))
        self.assertFalse(corehub.has_masked_custom_secret({}))
        self.assertFalse(corehub.has_masked_custom_secret(None))

    def test_dropping_removes_the_placeholders_and_keeps_the_rest(self):
        import automator_app.corehub as corehub

        custom = dict(DATABRICKS_CUSTOM, u2mClientSecret=MASKED, openLineageApiSecret=HIDDEN, oauth={"s": HIDDEN, "a": 1})
        dropped = corehub.drop_masked_custom_secrets(custom)
        self.assertNotIn("u2mClientSecret", dropped)
        self.assertNotIn("openLineageApiSecret", dropped)
        self.assertEqual(dropped["federationIdpClientSecret"], "idp-secret")
        self.assertEqual(dropped["oauth"], {"a": 1})
        self.assertEqual(dropped["authMode"], "U2M")
        self.assertEqual(corehub.drop_masked_custom_secrets(None), {})


def _backup_zip(agents_config):
    pipeline = yaml.safe_dump(
        {
            "exportMetadata": {"pipelineId": "aaaa1111", "pipelineName": "First Pipeline"},
            "dbo": {"target": "public", "sourceType": "SQL", "targetType": "SQL",
                    "tables": {"whitelist": [], "custom": {}}},
        },
        sort_keys=False,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("backup_First Pipeline_aaaa1111_20260610_112407.yaml", pipeline)
        zf.writestr("agents-config.yaml", yaml.safe_dump(agents_config, sort_keys=False))
    return buf.getvalue()


def _agents_config(custom=None):
    target = {
        "agentType": "TARGET", "agentTag": "databricks", "agentId": "tgt-1",
        "hostCredentials": {"host": "dbc.example.com", "password": "pw"},
        "specificConfiguration": {},
    }
    if custom is not None:
        target["customHostCredentials"] = custom
    return {
        "agents": [target],
        "pipelines": [{
            "pipelineId": "aaaa1111", "pipelineName": "First Pipeline",
            "agents": [{"agentType": "TARGET", "agentTag": "databricks", "agentId": "tgt-1"}],
        }],
    }


class RestoreCustomCredentialsTests(unittest.TestCase):
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
        self.imported = []

        def fake_import(**kwargs):
            self.imported.append(kwargs["config"])
            return {"pipelineId": "new-1", "pipelineName": kwargs["config"].get("pipelineName")}

        for patcher in (
            mock.patch.object(corehub, "import_pipeline_config_only", side_effect=fake_import),
            mock.patch.object(corehub, "run_create_entities", return_value={"success": True}),
            mock.patch.object(corehub, "fetch_core_hub", return_value={}),
            mock.patch.object(self.app_module.create_user_defined_functions, "main", return_value=None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, path, agents_config):
        return self.client.post(path, files={"file": ("backup.zip", _backup_zip(agents_config), "application/zip")})

    def test_import_refuses_a_masked_custom_secret(self):
        for placeholder in (MASKED, HIDDEN):
            with self.subTest(placeholder=placeholder):
                custom = dict(DATABRICKS_CUSTOM, u2mClientSecret=placeholder)
                response = self._post("/api/import/all", _agents_config(custom))
                self.assertEqual(response.status_code, 400)
                self.assertIn("masked", response.json()["detail"])
                self.assertEqual(self.imported, [])

    def test_validation_refuses_a_masked_custom_secret(self):
        custom = dict(DATABRICKS_CUSTOM, openLineageApiSecret=MASKED)
        response = self._post("/api/import/validate-all", _agents_config(custom))
        self.assertEqual(response.status_code, 400)

    def test_import_accepts_custom_credentials_with_real_secrets(self):
        response = self._post("/api/import/all", _agents_config(DATABRICKS_CUSTOM))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.imported[0]["agents"][0]["customHostCredentials"], DATABRICKS_CUSTOM)

    def test_an_older_backup_without_custom_credentials_restores_as_before(self):
        response = self._post("/api/import/all", _agents_config())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.imported), 1)
        self.assertFalse(self.imported[0]["agents"][0].get("customHostCredentials"))


class ImportConfigNeverSendsAPlaceholderTests(unittest.TestCase):
    """import_pipeline_config_only is reachable without the archive check (config import)."""

    def _import(self, custom, certificate=False):
        import automator_app.corehub as corehub

        calls = []

        def fake_fetch(path, method="GET", **kwargs):
            calls.append((path, method, kwargs.get("body")))
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "new-1"}
            return {}

        host = {"host": "h", "password": "pw"}
        if certificate:
            host.update(certificatePath="c.jks", certificateType="TRUST_STORE")
        config = {"pipelineName": "P", "agents": [{
            "agentType": "TARGET", "agentTag": "databricks", "hostCredentials": host, "customHostCredentials": custom,
        }]}
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(corehub, "_add_agent_to_pipeline_via_corehub", return_value="agent-1"):
            corehub.import_pipeline_config_only(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False, config=config,
            )
        return calls, config["agents"][0]

    def test_masked_custom_secrets_are_left_out_of_the_credentials_sent(self):
        custom = dict(DATABRICKS_CUSTOM, u2mClientSecret=MASKED, openLineageApiSecret=HIDDEN)
        calls, _ = self._import(custom)
        bodies = [body for path, method, body in calls if path.endswith("/config/credentials") and method == "PUT"]
        self.assertEqual(len(bodies), 1)
        sent = bodies[0]["customHostCredentials"]
        self.assertNotIn("u2mClientSecret", sent)
        self.assertNotIn("openLineageApiSecret", sent)
        self.assertEqual(sent["federationIdpClientSecret"], "idp-secret")
        self.assertEqual(sent["authMode"], "U2M")
        self.assertNotIn(MASKED, repr(calls))
        self.assertNotIn(HIDDEN, repr(calls))

    def test_the_credentials_held_for_after_a_certificate_upload_are_clean_too(self):
        custom = dict(DATABRICKS_CUSTOM, u2mClientSecret=HIDDEN)
        _, agent = self._import(custom, certificate=True)
        self.assertNotIn("u2mClientSecret", agent["_custom_host_credentials"])


if __name__ == "__main__":
    unittest.main()

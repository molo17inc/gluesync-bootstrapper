"""Pipeline config short-TTL cache + idempotent configure_core_hub (no Core Hub)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "automator_app"))

import commons  # noqa: E402


class PipelineConfigCacheTests(unittest.TestCase):
    def setUp(self):
        commons.reset_pipeline_config_cache()
        self.addCleanup(commons.reset_pipeline_config_cache)
        commons._COREHUB_CONFIG_KEY = None  # pylint: disable=protected-access

    def test_get_pipeline_config_reuses_cache(self):
        calls = []

        def fake_fetch(path, method="GET", token=None, body=None, params=None, headers=None, **_kw):
            calls.append((path, dict(params or {})))
            return {"agents": [{"agentType": "TARGET", "agentId": "t1"}], "entities": ["x"]}

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            first = commons.get_pipeline_config("tok", "pipe-a")
            second = commons.get_pipeline_config("tok", "pipe-a")
            agents = commons.get_pipeline_agents("tok", "pipe-a")

        self.assertEqual(len(calls), 1)
        self.assertIs(first, second)
        self.assertEqual(agents, [{"agentType": "TARGET", "agentId": "t1"}])
        self.assertEqual(calls[0][1].get("include_secrets"), "true")

    def test_reset_pipeline_config_cache_forces_refetch(self):
        calls = []

        def fake_fetch(path, method="GET", token=None, body=None, params=None, headers=None, **_kw):
            calls.append(path)
            return {"agents": {}}

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            commons.get_pipeline_config("tok", "pipe-b")
            commons.reset_pipeline_config_cache()
            commons.get_pipeline_config("tok", "pipe-b")

        self.assertEqual(len(calls), 2)

    def test_configure_core_hub_skips_identical_settings(self):
        created = []

        class FakeClient:
            def __init__(self, base_url, use_ssl=None, skip_verify=None):
                self.base_url = base_url
                created.append((base_url, use_ssl, skip_verify))

        with mock.patch.object(commons, "CoreHubClient", FakeClient), mock.patch.object(commons, "reset_refreshed_token"):
            first = commons.configure_core_hub("http://hub:1717", use_ssl=False, skip_verify=True)
            second = commons.configure_core_hub("http://hub:1717", use_ssl=False, skip_verify=True)
            third = commons.configure_core_hub("http://hub:1717", use_ssl=True, skip_verify=True)

        self.assertIs(first, second)
        self.assertIsNot(first, third)
        self.assertEqual(len(created), 2)


class DiscoveryCoalesceMarkerTests(unittest.TestCase):
    def test_entity_import_has_inflight_coalesce_and_preview_busy(self):
        src = (ROOT / "automator_app" / "static" / "entity_import.js").read_text()
        self.assertIn("tablesInflight", src)
        self.assertIn("schemasInflight", src)
        self.assertIn("resetDiscoveryCaches", src)
        self.assertIn("Previewing…", src)
        self.assertIn("discoveryGen", src)

    def test_app_debounces_schema_default_input(self):
        src = (ROOT / "automator_app" / "static" / "app.js").read_text()
        self.assertIn("schemaDefaultsTimer", src)
        self.assertIn("onSchemaDefaultsChangeImmediate", src)


if __name__ == "__main__":
    unittest.main()

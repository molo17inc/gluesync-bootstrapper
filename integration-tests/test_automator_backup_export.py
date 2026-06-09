#!/usr/bin/env python3
"""
Regression tests for Automator backup/export flows.

Covers:
- export_pipeline_yaml (single pipeline YAML export)
- export_pipeline_full_backup (ZIP with YAML + agents-config + UDFs)
- export_all_pipelines_yaml (bulk export all pipelines)

Mocks the helper functions imported into automator_app.corehub so no
real HTTP requests are made.
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


# ---------------------------------------------------------------------------
# Fixtures — unwrapped entity dicts (as returned by fetch_pipeline_entities)
# ---------------------------------------------------------------------------

def _simple_entity(name: str, schema: str, group_id: str = "") -> dict:
    return {
        "entityId": f"ent-{name}",
        "entityName": f"{schema}.{name}",
        "groupId": group_id,
        "agentEntities": [
            {
                "type": "SingleTable",
                "agentId": "src-agent",
                "entityType": {"type": "Source", "unchangedDataFilterType": "ENTIRE_ROW"},
                "entityObject": {"id": "101", "schema": schema, "collection": name},
                "table": {"id": "101", "name": name, "schema": schema},
                "columns": [
                    {"id": 1, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 2, "name": "NAME", "dataType": "varchar", "isPK": False},
                ],
                "keys": [{"id": 1, "name": "ID"}],
            },
            {
                "type": "SingleTable",
                "agentId": "tgt-agent",
                "entityType": {
                    "type": "Target",
                    "unchangedDataFilterType": "ENTIRE_ROW",
                    "allowedOperations": ["INSERT", "UPDATE", "DELETE"],
                    "snapshotWritingConcurrency": 1,
                    "useBulkOperationsDuringCDC": False,
                    "useBulkOperationsWhileSnapshot": False,
                },
                "entityObject": {"id": "101", "schema": "public", "collection": name},
                "table": {"id": "101", "name": name, "schema": "public"},
                "columns": [
                    {"id": 1, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 2, "name": "NAME", "dataType": "varchar", "isPK": False},
                ],
                "keys": [{"id": 1, "name": "ID"}],
            },
        ],
    }


def _entity_with_udf(name: str, schema: str, udf_name: str) -> dict:
    ent = _simple_entity(name, schema)
    ent["agentEntities"][1]["entityType"]["mappingFunctionInfo"] = {
        "name": udf_name,
        "type": "Java",
    }
    return ent


class ExportPipelineYamlTests(unittest.TestCase):
    """Verify export_pipeline_yaml builds correct YAML from CoreHub responses."""

    def test_basic_export_contains_schema_tables_and_keys(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("DRIVERS", "demo")]

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}):
            text = corehub.export_pipeline_yaml(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
            )

        data = yaml.safe_load(text)
        # Single-schema export uses {schema_name: cfg} directly
        demo = data.get("demo", {})
        self.assertTrue(demo, "Expected 'demo' schema in YAML")
        tables = demo.get("tables", {})
        self.assertIn("whitelist", tables)
        self.assertIn("DRIVERS", tables.get("custom", {}))
        self.assertEqual(demo.get("target"), "public")

    def test_export_with_schedules_attaches_them_to_tables(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("ARTICLES", "demo", group_id="grp-1")]

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-ARTICLES": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({"grp-1": "articles_group"}, {}, {"articles_group": {"id": "grp-1"}})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[
                 {
                     "entity_ids": ["ent-ARTICLES"],
                     "name": "Daily snapshot",
                     "task_type": "entity_snapshot",
                     "with_snapshot": True,
                     "cron_expression": "0 2 * * *",
                 }
             ]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}):
            text = corehub.export_pipeline_yaml(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
            )

        data = yaml.safe_load(text)
        # Single-schema export uses {schema_name: cfg} directly
        demo = data.get("demo", {})
        articles_cfg = demo["tables"]["custom"]["ARTICLES"]
        self.assertIn("schedules", articles_cfg)
        self.assertEqual(articles_cfg["schedules"][0]["task_type"], "entity_snapshot")


class ExportPipelineFullBackupTests(unittest.TestCase):
    """Verify export_pipeline_full_backup produces a valid ZIP."""

    def test_zip_contains_yaml_agents_config_and_udfs(self):
        import automator_app.corehub as corehub

        entities = [_entity_with_udf("DRIVERS", "demo", "UDF_DRIVERS")]
        agents = [
            {
                "agentType": "SOURCE",
                "agentTag": "mssql",
                "agentId": "src-agent",
                "hostCredentials": {
                    "connectionName": "src",
                    "host": "1.2.3.4",
                    "port": 1433,
                    "password": "secret123",
                },
            },
            {
                "agentType": "TARGET",
                "agentTag": "mysql",
                "agentId": "tgt-agent",
                "hostCredentials": {
                    "connectionName": "tgt",
                    "host": "5.6.7.8",
                    "port": 3306,
                    "password": "alsosecret",
                },
            },
        ]

        def fake_fetch(path, **kwargs):
            routes = {
                "/pipelines/pipe": {"pipelineId": "pipe", "name": "TestPipe"},
                "/pipelines/pipe/config/entities/mapping-functions/UDF_DRIVERS": {
                    "code": "public class UDF_DRIVERS { }",
                    "type": "Java",
                },
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_pipeline_full_backup(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
            )

        self.assertTrue(zip_bytes)
        self.assertIsInstance(zip_bytes, bytes)

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()

        yaml_files = [n for n in namelist if n.endswith(".yaml")]
        self.assertTrue(yaml_files, "Expected at least one .yaml file in ZIP")

        agents_config = [n for n in namelist if "agents-config.yaml" in n]
        self.assertTrue(agents_config, "Expected agents-config.yaml in ZIP")

        udf_files = [n for n in namelist if "udf-" in n]
        self.assertTrue(udf_files, "Expected UDF folder in ZIP")

    def test_passwords_are_masked_in_agents_config(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("DRIVERS", "demo")]
        agents = [
            {
                "agentType": "SOURCE",
                "agentTag": "mssql",
                "agentId": "src-agent",
                "hostCredentials": {
                    "connectionName": "src",
                    "host": "1.2.3.4",
                    "port": 1433,
                    "password": "secret123",
                },
            },
        ]

        def fake_fetch(path, **kwargs):
            routes = {
                "/pipelines/pipe": {"pipelineId": "pipe", "name": "TestPipe"},
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_pipeline_full_backup(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
            )

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                for name in zf.namelist():
                    if "agents-config.yaml" in name:
                        content = zf.read(name).decode("utf-8")
                        self.assertIn("*******", content)
                        self.assertNotIn("secret123", content)
                        break
                else:
                    self.fail("agents-config.yaml not found in ZIP")


class ExportAllPipelinesYamlTests(unittest.TestCase):
    """Verify export_all_pipelines_yaml creates a multi-pipeline ZIP."""

    def test_bulk_export_contains_all_pipeline_yamls(self):
        import automator_app.corehub as corehub

        entities_p1 = [_simple_entity("A", "s1")]
        entities_p2 = [_simple_entity("B", "s2")]
        agents_p1 = [
            {"agentType": "SOURCE", "agentTag": "mssql", "agentId": "a1",
             "hostCredentials": {"host": "h1", "port": 1433, "connectionName": "c1", "password": "p1"}},
        ]
        agents_p2 = [
            {"agentType": "SOURCE", "agentTag": "mysql", "agentId": "a2",
             "hostCredentials": {"host": "h2", "port": 3306, "connectionName": "c2", "password": "p2"}},
        ]

        def fake_fetch(path, **kwargs):
            routes = {
                "/pipelines": [
                    {"pipelineId": "p1", "name": "PipeOne"},
                    {"pipelineId": "p2", "name": "PipeTwo"},
                ],
                "/pipelines/p1": {"pipelineId": "p1", "name": "PipeOne"},
                "/pipelines/p2": {"pipelineId": "p2", "name": "PipeTwo"},
            }
            return routes.get(path, {})

        call_count = {"p1": 0, "p2": 0}

        def fetch_entities(token, pipeline_id):
            call_count[pipeline_id] = call_count.get(pipeline_id, 0) + 1
            return entities_p1 if pipeline_id == "p1" else entities_p2

        def get_agents(token, pipeline_id):
            return agents_p1 if pipeline_id == "p1" else agents_p2

        with mock.patch.object(corehub, "fetch_pipeline_entities", side_effect=fetch_entities), \
             mock.patch.object(corehub, "build_entities_maps", side_effect=[
                 {"ent-A": entities_p1[0]},
                 {"ent-B": entities_p2[0]},
             ]), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", side_effect=get_agents), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_all_pipelines_yaml(
                token="tok", base_url="http://test",
                use_ssl=False, skip_verify=False,
            )

        self.assertTrue(zip_bytes)

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()

        # ZIP contains backup_<name>_<pipelineId>_<timestamp>.yaml files
        has_p1 = any("_p1_" in n for n in namelist)
        has_p2 = any("_p2_" in n for n in namelist)
        has_agents = any("agents-config.yaml" in n for n in namelist)
        self.assertTrue(has_p1, f"Expected p1 backup YAML in ZIP, got: {namelist}")
        self.assertTrue(has_p2, f"Expected p2 backup YAML in ZIP, got: {namelist}")
        self.assertTrue(has_agents, f"Expected agents-config.yaml in ZIP, got: {namelist}")


class ExportGlobalConfigsTests(unittest.TestCase):
    """Verify export_global_configs fetches all global config endpoints."""

    def test_exports_all_global_configs(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            routes = {
                "/global-config/grafana": {"enabled": True, "url": "http://graf"},
                "/global-config/logging": {"level": "INFO"},
                "/global-config/logging/level": "INFO",
                "/global-config/logging/telemetry": {"enabled": False},
                "/global-config/release-channel": {"channel": "stable"},
                "/global-config/license": {"key": "ABC-123"},
                "/global-config/session": {"timeout": 3600},
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_global_configs(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("grafana", result)
        self.assertIn("logging", result)
        self.assertIn("logging_level", result)
        self.assertIn("telemetry", result)
        self.assertIn("release_channel", result)
        self.assertIn("license", result)
        self.assertIn("session", result)
        self.assertEqual(result["grafana"]["enabled"], True)
        self.assertEqual(result["release_channel"]["channel"], "stable")

    def test_gracefully_handles_failed_endpoints(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            if path == "/global-config/grafana":
                raise RuntimeError("Grafana not configured")
            return {"ok": True}

        # This tests error handling indirectly via the generic catch
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_global_configs(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        # All endpoints should still be present, some may have errors
        self.assertIn("grafana", result)
        self.assertIn("error", result["grafana"])


class ExportSmtpSettingsTests(unittest.TestCase):
    """Verify export_smtp_settings fetches SMTP configuration with include_secrets."""

    def test_includes_password_with_include_secrets(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            return {
                "host": "smtp.example.com",
                "port": 587,
                "username": "admin",
                "password": "supersecret",
            }

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_smtp_settings(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        smtp = result["smtp"]
        self.assertEqual(smtp["host"], "smtp.example.com")
        self.assertEqual(smtp["password"], "supersecret")

    def test_handles_non_dict_response(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            return "smtp-enabled"

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_smtp_settings(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertEqual(result["smtp"]["value"], "smtp-enabled")


class ExportWebhooksConfigTests(unittest.TestCase):
    """Verify export_webhooks_config fetches webhooks, retention, and event types."""

    def test_exports_all_webhook_configs(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            routes = {
                "/global-config/webhooks": {"enabled": True, "urls": ["http://hook"]},
                "/global-config/webhooks/retention": {"days": 30},
                "/global-config/webhooks/event-types": ["pipeline.start", "pipeline.stop"],
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_webhooks_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("webhooks", result)
        self.assertIn("retention", result)
        self.assertIn("event_types", result)
        self.assertTrue(result["webhooks"]["enabled"])
        self.assertEqual(result["retention"]["days"], 30)


class ExportThresholdsTests(unittest.TestCase):
    """Verify export_thresholds fetches threshold configuration."""

    def test_exports_thresholds(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            return {
                "lag_threshold_ms": 5000,
                "retry_count": 3,
                "alert_enabled": True,
            }

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_thresholds(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("thresholds", result)
        self.assertEqual(result["thresholds"]["lag_threshold_ms"], 5000)
        self.assertTrue(result["thresholds"]["alert_enabled"])


class ExportSchedulesTests(unittest.TestCase):
    """Verify export_schedules fetches Chronos jobs and settings."""

    def test_exports_jobs_and_settings(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            routes = {
                "/chronos/api/jobs": [
                    {"id": "job-1", "name": "Daily snapshot", "schedule": "0 2 * * *"},
                ],
                "/chronos/api/settings": {"timezone": "UTC", "enabled": True},
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_schedules(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("jobs", result)
        self.assertIn("settings", result)
        self.assertEqual(result["jobs"][0]["name"], "Daily snapshot")
        self.assertEqual(result["settings"]["timezone"], "UTC")

    def test_gracefully_handles_missing_chronos(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            if path == "/chronos/api/jobs":
                raise RuntimeError("Chronos not available")
            return {"timezone": "UTC"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_schedules(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("error", result["jobs"])
        self.assertEqual(result["settings"]["timezone"], "UTC")


class ExportFullCorehubBackupTests(unittest.TestCase):
    """Verify export_full_corehub_backup produces a comprehensive ZIP."""

    def test_zip_contains_separate_yaml_files(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            routes = {
                "/global-config/grafana": {"enabled": True},
                "/global-config/logging": {"level": "INFO"},
                "/global-config/logging/level": "INFO",
                "/global-config/logging/telemetry": {"enabled": False},
                "/global-config/release-channel": {"channel": "stable"},
                "/global-config/license": {"key": "ABC"},
                "/global-config/session": {"timeout": 3600},
                "/global-config/smtp": {"host": "smtp", "password": "secret"},
                "/global-config/webhooks": {"enabled": True},
                "/global-config/webhooks/retention": {"days": 30},
                "/global-config/webhooks/event-types": ["event1"],
                "/global-config/webhooks/delivery-logs": [],
                "/global-config/webhooks/dead-letters": [],
                "/global-config/thresholds": {"lag": 1000},
                "/global-config/thresholds/pattern-status": {},
                "/global-config/thresholds/simulate": {},
                "/chronos/api/jobs": [],
                "/chronos/api/settings": {"timezone": "UTC"},
                "/users": [{"id": "1", "username": "admin"}],
                "/oidc/configuration": {"enabled": True},
                "/oidc/auth-url": {"url": "http://auth"},
                "/pipelines": [],
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_full_corehub_backup(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertTrue(zip_bytes)
        self.assertIsInstance(zip_bytes, bytes)

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()

        # Each domain stored in its own YAML file
        self.assertIn("global-configs.yaml", namelist)
        self.assertIn("smtp.yaml", namelist)
        self.assertIn("webhooks.yaml", namelist)
        self.assertIn("thresholds.yaml", namelist)
        self.assertIn("schedules.yaml", namelist)
        self.assertIn("users.yaml", namelist)
        self.assertIn("oidc.yaml", namelist)

        # Parse global-configs.yaml and verify content
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                global_cfg = yaml.safe_load(zf.read("global-configs.yaml").decode("utf-8"))
                self.assertEqual(global_cfg["release_channel"]["channel"], "stable")

                smtp_cfg = yaml.safe_load(zf.read("smtp.yaml").decode("utf-8"))
                self.assertEqual(smtp_cfg["smtp"]["password"], "secret")

                webhooks_cfg = yaml.safe_load(zf.read("webhooks.yaml").decode("utf-8"))
                self.assertTrue(webhooks_cfg["webhooks"]["enabled"])
                self.assertIn("delivery_logs", webhooks_cfg)
                self.assertIn("dead_letters", webhooks_cfg)

                users_cfg = yaml.safe_load(zf.read("users.yaml").decode("utf-8"))
                self.assertEqual(users_cfg["users"][0]["username"], "admin")

                oidc_cfg = yaml.safe_load(zf.read("oidc.yaml").decode("utf-8"))
                self.assertTrue(oidc_cfg["oidc_configuration"]["enabled"])


class ExportUsersTests(unittest.TestCase):
    """Verify export_users fetches user list from CoreHub."""

    def test_exports_users(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            if path == "/users":
                return [{"id": "1", "username": "admin", "role": "SUPER_ADMIN"}]
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_users(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIn("users", result)
        self.assertEqual(result["users"][0]["username"], "admin")


class ExportOidcConfigTests(unittest.TestCase):
    """Verify export_oidc_config fetches OIDC settings."""

    def test_exports_oidc_config(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            routes = {
                "/oidc/configuration": {"enabled": True, "providerName": "Auth0"},
                "/oidc/auth-url": {"authorizationUrl": "http://auth"},
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.export_oidc_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertTrue(result["oidc_configuration"]["enabled"])
        self.assertEqual(result["oidc_auth_url"]["authorizationUrl"], "http://auth")


class ImportGlobalConfigsTests(unittest.TestCase):
    """Verify import_global_configs PUTs each config to the correct endpoint."""

    def test_restores_all_present_configs(self):
        import automator_app.corehub as corehub

        put_calls: list[tuple[str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        configs = {
            "grafana": {"enabled": True, "url": "http://graf"},
            "logging": {"level": "INFO"},
            "logging_level": {"value": "INFO"},
            "telemetry": {"enabled": False},
            "release_channel": {"channel": "stable"},
            "license": {"key": "ABC-123"},
            "session": {"timeout": 3600},
        }

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_global_configs(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                configs=configs,
            )

        self.assertEqual(result["grafana"]["status"], "restored")
        self.assertEqual(result["logging_level"]["status"], "restored")
        self.assertEqual(result["session"]["status"], "restored")

        paths = {p for p, _ in put_calls}
        self.assertIn("/global-config/grafana", paths)
        self.assertIn("/global-config/session", paths)

        # logging_level should be unwrapped from {"value": "INFO"} to "INFO"
        logging_level_body = next(b for p, b in put_calls if p == "/global-config/logging/level")
        self.assertEqual(logging_level_body, "INFO")

    def test_skips_missing_and_errored_configs(self):
        import automator_app.corehub as corehub

        configs = {
            "grafana": {"error": "unavailable"},
            "license": {"key": "ABC"},
        }

        def fake_fetch(path, method="GET", **kwargs):
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_global_configs(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                configs=configs,
            )

        self.assertEqual(result["grafana"]["status"], "skipped")
        self.assertEqual(result["logging"]["status"], "skipped")  # missing
        self.assertEqual(result["license"]["status"], "restored")


class ImportSmtpSettingsTests(unittest.TestCase):
    """Verify import_smtp_settings restores SMTP config."""

    def test_restores_smtp(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", **kwargs):
            self.assertEqual(path, "/global-config/smtp")
            self.assertEqual(method, "PUT")
            self.assertEqual(kwargs["body"]["host"], "smtp.example.com")
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_smtp_settings(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                smtp_data={"smtp": {"host": "smtp.example.com", "port": 587}},
            )

        self.assertEqual(result["status"], "restored")

    def test_skips_missing_smtp(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_smtp_settings(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                smtp_data={},
            )

        self.assertEqual(result["status"], "skipped")


class ImportWebhooksConfigTests(unittest.TestCase):
    """Verify import_webhooks_config restores all webhook endpoints."""

    def test_restores_all_webhook_configs(self):
        import automator_app.corehub as corehub

        put_paths: list[str] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_paths.append(path)
            return {"status": "ok"}

        webhooks_data = {
            "webhooks": {"enabled": True},
            "retention": {"days": 30},
            "event_types": ["event1"],
            "delivery_logs": [],
            "dead_letters": [],
        }

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_webhooks_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                webhooks_data=webhooks_data,
            )

        for key in webhooks_data:
            self.assertEqual(result[key]["status"], "restored")

        self.assertIn("/global-config/webhooks", put_paths)
        self.assertIn("/global-config/webhooks/retention", put_paths)
        self.assertIn("/global-config/webhooks/event-types", put_paths)
        self.assertIn("/global-config/webhooks/delivery-logs", put_paths)
        self.assertIn("/global-config/webhooks/dead-letters", put_paths)


class ImportThresholdsTests(unittest.TestCase):
    """Verify import_thresholds restores thresholds."""

    def test_restores_thresholds(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", **kwargs):
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_thresholds(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                thresholds_data={"thresholds": {"lag": 1000}},
            )

        self.assertEqual(result["thresholds"]["status"], "restored")


class ImportSchedulesTests(unittest.TestCase):
    """Verify import_schedules restores jobs and settings."""

    def test_restores_settings_and_jobs(self):
        import automator_app.corehub as corehub

        post_calls: list[Any] = []
        put_calls: list[Any] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            elif method == "POST":
                post_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        schedules_data = {
            "settings": {"timezone": "UTC", "enabled": True},
            "jobs": [
                {"id": "job-1", "name": "Daily snapshot"},
                {"id": "job-2", "name": "Pipeline start"},
            ],
        }

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_schedules(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                schedules_data=schedules_data,
            )

        self.assertEqual(result["settings"]["status"], "restored")
        self.assertEqual(result["jobs"]["status"], "restored")
        self.assertEqual(result["jobs"]["restored"], 2)
        self.assertEqual(result["jobs"]["failed"], 0)

        # Settings should be PUT
        self.assertEqual(put_calls[0], ("/chronos/api/settings", {"timezone": "UTC", "enabled": True}))

        # Jobs should be POSTed
        self.assertEqual(len(post_calls), 2)
        self.assertEqual(post_calls[0][0], "/chronos/api/jobs")

    def test_skips_when_no_jobs(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_schedules(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                schedules_data={"settings": {"timezone": "UTC"}},
            )

        self.assertEqual(result["jobs"]["status"], "skipped")


class ImportFullCorehubBackupTests(unittest.TestCase):
    """Verify import_full_corehub_backup orchestrates restore from ZIP."""

    def test_restores_all_domains_from_zip(self):
        import automator_app.corehub as corehub

        # Build a ZIP with all the separate YAML files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("global-configs.yaml", yaml.safe_dump({
                "grafana": {"enabled": True},
                "logging": {"level": "INFO"},
                "logging_level": {"value": "INFO"},
                "telemetry": {"enabled": False},
                "release_channel": {"channel": "stable"},
                "license": {"key": "ABC"},
                "session": {"timeout": 3600},
            }))
            zf.writestr("smtp.yaml", yaml.safe_dump({"smtp": {"host": "smtp"}}))
            zf.writestr("webhooks.yaml", yaml.safe_dump({"webhooks": {"enabled": True}}))
            zf.writestr("thresholds.yaml", yaml.safe_dump({"thresholds": {"lag": 1000}}))
            zf.writestr("schedules.yaml", yaml.safe_dump({
                "settings": {"timezone": "UTC"},
                "jobs": [{"id": "job-1"}],
            }))
            zf.writestr("users.yaml", yaml.safe_dump({
                "users": [{"id": "1", "username": "admin"}],
            }))
            zf.writestr("oidc.yaml", yaml.safe_dump({
                "oidc_configuration": {"enabled": True},
                "oidc_auth_url": {"url": "http://auth"},
            }))

        zip_bytes = zip_buffer.getvalue()

        def fake_fetch(path, method="GET", **kwargs):
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_full_corehub_backup(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                zip_bytes=zip_bytes,
            )

        self.assertIn("global_configs", result)
        self.assertIn("smtp", result)
        self.assertIn("webhooks", result)
        self.assertIn("thresholds", result)
        self.assertIn("schedules", result)
        self.assertIn("users", result)
        self.assertIn("oidc", result)
        self.assertIn("pipelines", result)

        self.assertEqual(result["global_configs"]["grafana"]["status"], "restored")
        self.assertEqual(result["smtp"]["status"], "restored")
        self.assertEqual(result["webhooks"]["webhooks"]["status"], "restored")
        self.assertEqual(result["thresholds"]["thresholds"]["status"], "restored")
        self.assertEqual(result["schedules"]["settings"]["status"], "restored")
        self.assertEqual(result["schedules"]["jobs"]["status"], "restored")
        self.assertEqual(result["users"]["status"], "restored")
        self.assertEqual(result["oidc"]["oidc_configuration"]["status"], "restored")
        # pipelines.zip is not present in this test ZIP
        self.assertEqual(result["pipelines"]["status"], "skipped")


class FetchGlobalNotificationConfigTests(unittest.TestCase):
    """Verify fetch_global_notification_config fetches SMTP with include_secrets."""

    def test_fetches_smtp_without_secrets_masks_password(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, params=None, **kwargs):
            self.assertEqual(path, "/global-config/smtp")
            self.assertIsNone(params)
            return {"smtpServer": "smtp.example.com", "password": "<hidden>"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.fetch_global_notification_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertEqual(result["password"], "*******")
        self.assertEqual(result["smtpServer"], "smtp.example.com")

    def test_fetches_smtp_with_secrets_reveals_password(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, params=None, **kwargs):
            self.assertEqual(path, "/global-config/smtp")
            self.assertEqual(params, {"include_secrets": "true"})
            return {"smtpServer": "smtp.example.com", "password": "real-pass"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.fetch_global_notification_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                include_secrets=True,
            )

        self.assertEqual(result["password"], "real-pass")

    def test_returns_none_on_failure(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, **kwargs):
            raise RuntimeError("timeout")

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.fetch_global_notification_config(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
            )

        self.assertIsNone(result)


class ExportGlobalConfigYamlTests(unittest.TestCase):
    """Verify global-config.yaml appears as a separate file in ZIP exports."""

    def test_full_backup_contains_separate_global_config_yaml(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("DRIVERS", "demo")]
        agents = [
            {
                "agentType": "SOURCE",
                "agentTag": "mssql",
                "agentId": "src-agent",
                "hostCredentials": {
                    "connectionName": "src",
                    "host": "1.2.3.4",
                    "port": 1433,
                    "password": "secret123",
                },
            },
        ]

        def fake_fetch(path, **kwargs):
            routes = {
                "/pipelines/pipe": {"pipelineId": "pipe", "name": "TestPipe"},
                "/global-config/smtp": {
                    "smtpServer": "smtp.example.com",
                    "portNumber": 587,
                    "secureConnection": "TLS",
                    "email": "noreply@example.com",
                    "username": "noreply@example.com",
                    "password": "*******",
                    "alertEmail": "admin@example.com",
                    "enabledEvents": ["TEST"],
                    "severityFilter": ["INFO"],
                    "burstPrevention": {"enabled": True, "windowMinutes": 1, "maxEventsPerEmail": 20},
                    "pipelineFilter": ["pipe"],
                    "groupFilter": [],
                    "entityFilter": [],
                },
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_pipeline_full_backup(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
            )

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()

        self.assertTrue(
            any("global-config.yaml" in n for n in namelist),
            f"Expected global-config.yaml in ZIP, got: {namelist}",
        )

        # Pipeline YAML must NOT embed globalNotifications when it's in a ZIP
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                for name in zf.namelist():
                    if name.endswith(".yaml") and "agents-config" not in name and "global-config" not in name:
                        pipeline_yaml = yaml.safe_load(zf.read(name).decode("utf-8"))
                        self.assertNotIn(
                            "globalNotifications", pipeline_yaml,
                            "Pipeline YAML should not embed globalNotifications when ZIP has separate file",
                        )

    def test_export_all_contains_separate_global_config_yaml(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("A", "s1")]
        agents = [
            {"agentType": "SOURCE", "agentTag": "mssql", "agentId": "a1",
             "hostCredentials": {"host": "h1", "port": 1433, "connectionName": "c1", "password": "p1"}},
        ]

        def fake_fetch(path, **kwargs):
            routes = {
                "/pipelines": [{"pipelineId": "p1", "name": "PipeOne"}],
                "/pipelines/p1": {"pipelineId": "p1", "name": "PipeOne"},
                "/global-config/smtp": {
                    "smtpServer": "smtp.example.com",
                    "portNumber": 587,
                    "secureConnection": "TLS",
                    "email": "noreply@example.com",
                    "username": "noreply@example.com",
                    "password": "<hidden>",
                    "alertEmail": "admin@example.com",
                    "enabledEvents": ["TEST"],
                    "severityFilter": ["INFO"],
                    "burstPrevention": {"enabled": True, "windowMinutes": 1, "maxEventsPerEmail": 20},
                    "pipelineFilter": ["p1"],
                    "groupFilter": [],
                    "entityFilter": [],
                },
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-A": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            zip_bytes = corehub.export_all_pipelines_yaml(
                token="tok", base_url="http://test",
                use_ssl=False, skip_verify=False,
            )

        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                namelist = zf.namelist()

        self.assertIn("global-config.yaml", namelist)

        # Verify per-pipeline YAML does NOT embed globalNotifications
        with io.BytesIO(zip_bytes) as buf:
            with zipfile.ZipFile(buf, "r") as zf:
                for name in namelist:
                    if name.startswith("backup_") and name.endswith(".yaml"):
                        pipeline_yaml = yaml.safe_load(zf.read(name).decode("utf-8"))
                        self.assertNotIn("globalNotifications", pipeline_yaml)

    def test_single_yaml_export_embeds_global_notifications(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("DRIVERS", "demo")]

        def fake_fetch(path, **kwargs):
            routes = {
                "/global-config/smtp": {
                    "smtpServer": "smtp.example.com",
                    "portNumber": 587,
                    "secureConnection": "TLS",
                    "email": "noreply@example.com",
                    "username": "noreply@example.com",
                    "password": "real-pass",
                    "alertEmail": "admin@example.com",
                    "enabledEvents": ["TEST"],
                    "severityFilter": ["INFO"],
                    "burstPrevention": {"enabled": True, "windowMinutes": 1, "maxEventsPerEmail": 20},
                    "pipelineFilter": ["pipe"],
                    "groupFilter": [],
                    "entityFilter": [],
                },
            }
            return routes.get(path, {})

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            text = corehub.export_pipeline_yaml(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
                include_global_config=True,
                include_secrets=True,
            )

        data = yaml.safe_load(text)
        self.assertIn("globalNotifications", data)
        self.assertEqual(data["globalNotifications"]["smtpServer"], "smtp.example.com")
        self.assertEqual(data["globalNotifications"]["password"], "real-pass")

    def test_single_yaml_export_can_skip_global_notifications(self):
        import automator_app.corehub as corehub

        entities = [_simple_entity("DRIVERS", "demo")]

        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS"}):
            text = corehub.export_pipeline_yaml(
                token="tok", base_url="http://test", pipeline_id="pipe",
                use_ssl=False, skip_verify=False,
                include_global_config=False,
            )

        data = yaml.safe_load(text)
        self.assertNotIn("globalNotifications", data)


class ImportGlobalNotificationsTests(unittest.TestCase):
    """Verify import_global_notifications restores SMTP and drops masked passwords."""

    def test_restores_smtp_with_real_password(self):
        import automator_app.corehub as corehub

        put_calls: list[tuple[str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_global_notifications(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                notification_cfg={
                    "globalNotifications": {
                        "smtpServer": "smtp.example.com",
                        "password": "real-pass",
                    }
                },
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(put_calls[0][0], "/global-config/smtp")
        self.assertEqual(put_calls[0][1]["password"], "real-pass")

    def test_drops_masked_password_to_preserve_existing(self):
        import automator_app.corehub as corehub

        put_calls: list[tuple[str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        for sentinel in ("*******", "<hidden>"):
            put_calls.clear()
            with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
                result = corehub.import_global_notifications(
                    token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                    notification_cfg={
                        "globalNotifications": {
                            "smtpServer": "smtp.example.com",
                            "password": sentinel,
                        }
                    },
                )

            self.assertEqual(result["status"], "restored")
            self.assertNotIn("password", put_calls[0][1])

    def test_skips_when_no_global_notifications_key(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub"):
            result = corehub.import_global_notifications(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                notification_cfg={},
            )

        self.assertEqual(result["status"], "skipped")

    def test_accepts_flat_dict_without_wrapper(self):
        import automator_app.corehub as corehub

        put_calls: list[tuple[str, Any]] = []

        def fake_fetch(path, method="GET", **kwargs):
            if method == "PUT":
                put_calls.append((path, kwargs.get("body")))
            return {"status": "ok"}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            result = corehub.import_global_notifications(
                token="tok", base_url="http://test", use_ssl=False, skip_verify=False,
                notification_cfg={
                    "smtpServer": "smtp.example.com",
                    "password": "real-pass",
                },
            )

        self.assertEqual(result["status"], "restored")
        self.assertEqual(put_calls[0][0], "/global-config/smtp")


if __name__ == "__main__":
    unittest.main()

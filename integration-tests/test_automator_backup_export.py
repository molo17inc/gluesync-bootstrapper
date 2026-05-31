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


if __name__ == "__main__":
    unittest.main()

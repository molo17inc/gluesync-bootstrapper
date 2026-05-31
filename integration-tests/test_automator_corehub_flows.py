#!/usr/bin/env python3
"""
Regression tests for Automator CoreHub flows.

Covers:
- infer_agent_schema_types (SQL/NoSQL inference from pipeline agents)
- run_create_entities_for_tables (bulk table-list-driven entity creation)
- list_pipelines (pipeline list normalization)
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class InferAgentSchemaTypesTests(unittest.TestCase):
    """Verify SQL/NoSQL type inference from pipeline agents."""

    def test_rdbms_agents_return_sql(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "_load_agent_type_catalog", return_value={
            "mssql": "RDBMS",
            "mysql": "RDBMS",
        }), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=[
            {"agentType": "SOURCE", "agentTag": "mssql"},
            {"agentType": "TARGET", "agentTag": "mysql"},
        ]):
            src, tgt = corehub.infer_agent_schema_types(
                token="tok",
                base_url="http://test",
                pipeline_id="pipe",
                use_ssl=False,
                skip_verify=False,
            )
        self.assertEqual(src, "SQL")
        self.assertEqual(tgt, "SQL")

    def test_nosql_target_returns_nosql(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "_load_agent_type_catalog", return_value={
            "mssql": "RDBMS",
            "couchbase": "NOSQL",
        }), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=[
            {"agentType": "SOURCE", "agentTag": "mssql"},
            {"agentType": "TARGET", "agentTag": "couchbase"},
        ]):
            src, tgt = corehub.infer_agent_schema_types(
                token="tok",
                base_url="http://test",
                pipeline_id="pipe",
                use_ssl=False,
                skip_verify=False,
            )
        self.assertEqual(src, "SQL")
        self.assertEqual(tgt, "NoSQL")

    def test_object_store_target_returns_nosql(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "_load_agent_type_catalog", return_value={
            "mssql": "RDBMS",
            "awss3": "OBJECT STORE",
        }), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=[
            {"agentType": "SOURCE", "agentTag": "mssql"},
            {"agentType": "TARGET", "agentTag": "awss3"},
        ]):
            src, tgt = corehub.infer_agent_schema_types(
                token="tok",
                base_url="http://test",
                pipeline_id="pipe",
                use_ssl=False,
                skip_verify=False,
            )
        self.assertEqual(src, "SQL")
        self.assertEqual(tgt, "NoSQL")


class RunCreateEntitiesForTablesTests(unittest.TestCase):
    """Verify bulk table-list-driven entity creation in Automator."""

    def test_filters_tables_and_calls_create_entities(self):
        import automator_app.corehub as corehub

        captured_create_entities = []

        def fake_create_entities(token, pipeline_id, source_schema, target_schema, tables,
                                 source_agent_id, target_agent_id, source_type, target_type,
                                 yaml_config, skip_errors, chunk_size):
            captured_create_entities.append({
                "tables": tables,
                "source_type": source_type,
                "target_type": target_type,
            })
            return {"successful": 1, "failed": 0, "total": 1}

        with mock.patch.object(corehub, "get_pipeline_agents", return_value=[
            {"agentType": "SOURCE", "agentId": "src-agent", "category": "RDBMS"},
            {"agentType": "TARGET", "agentId": "tgt-agent", "category": "RDBMS"},
        ]), \
             mock.patch.object(corehub, "get_agent_tables", return_value=[
                 {"name": "A", "schema": "demo"},
                 {"name": "B", "schema": "demo"},
                 {"name": "C", "schema": "demo"},
             ]), \
             mock.patch.object(corehub, "create_entities", side_effect=fake_create_entities):
            result = corehub.run_create_entities_for_tables(
                token="tok",
                base_url="http://test",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                source_type="SQL",
                target_type="SQL",
                table_names=["A", "C"],
                skip_errors=False,
                chunk_size=50,
                enable_scheduling=False,
                create_tables=False,
                use_ssl=False,
                skip_verify=False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(captured_create_entities), 1)
        passed_tables = captured_create_entities[0]["tables"]
        self.assertEqual(len(passed_tables), 2)
        names = [t["name"] for t in passed_tables]
        self.assertEqual(sorted(names), ["A", "C"])

    def test_nosql_target_disables_table_creation(self):
        import automator_app.corehub as corehub

        captured_create_entities = []

        def fake_create_entities(*args, **kwargs):
            captured_create_entities.append(kwargs)
            return {"successful": 1, "failed": 0, "total": 1}

        with mock.patch.object(corehub, "get_pipeline_agents", return_value=[
            {"agentType": "SOURCE", "agentId": "src-agent", "category": "RDBMS"},
            {"agentType": "TARGET", "agentId": "tgt-agent", "category": "NoSQL"},
        ]), \
             mock.patch.object(corehub, "get_agent_tables", return_value=[
                 {"name": "A", "schema": "demo"},
             ]), \
             mock.patch.object(corehub, "create_entities", side_effect=fake_create_entities), \
             mock.patch.object(corehub, "set_create_table_if_not_exists"):
            result = corehub.run_create_entities_for_tables(
                token="tok",
                base_url="http://test",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                source_type="SQL",
                target_type="SQL",
                table_names=["A"],
                skip_errors=False,
                chunk_size=50,
                enable_scheduling=False,
                create_tables=True,  # User asked for table creation
                use_ssl=False,
                skip_verify=False,
            )

        self.assertTrue(result["success"])
        # create_tables should be overridden to False because target is NoSQL
        self.assertEqual(captured_create_entities[0].get("yaml_config"), None)


class ListPipelinesTests(unittest.TestCase):
    """Verify pipeline list normalization."""

    def test_normalizes_various_id_fields(self):
        import automator_app.corehub as corehub

        with mock.patch.object(corehub, "fetch_core_hub", return_value=[
            {"pipelineId": "p1", "name": "Pipe One"},
            {"id": "p2", "name": "Pipe Two"},
            {"pipeline_id": "p3", "name": "Pipe Three"},
            {"name": "Missing ID"},
        ]):
            pipelines = corehub.list_pipelines(
                token="tok",
                base_url="http://test",
                use_ssl=False,
                skip_verify=False,
            )

        ids = [p["id"] for p in pipelines]
        self.assertEqual(ids, ["p1", "p2", "p3"])
        self.assertEqual(pipelines[0]["name"], "Pipe One")


if __name__ == "__main__":
    unittest.main()

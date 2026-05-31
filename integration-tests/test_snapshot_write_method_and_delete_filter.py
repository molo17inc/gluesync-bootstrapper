#!/usr/bin/env python3
"""
Regression tests for snapshotWriteMethod and snapshotDeleteFilter.

Used by:
- mssql-ct-mssql-ct-conditional-deletion-integration-test (snapshotDeleteFilter)
- as400-pgsql-TRUNCATE-integration-test (snapshotWriteMethod)
- oracle-19c-logminer-postgres-integration-test (snapshotWriteMethod)
"""

import copy
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _node_info(default_type="varchar", gs_type="STRING"):
    return {
        "category": "RDBMS",
        "dataTypesMatrix": [
            {
                "gluesyncDataType": gs_type,
                "defaultType": default_type,
                "supportedTypes": [default_type],
            }
        ],
    }


def _source_col(name, col_id, data_type="varchar", is_pk=False, nullable=True):
    return {
        "id": col_id,
        "position": col_id,
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255,
        "numPrec": 0,
        "numScale": 0,
    }


SNAPSHOT_DELETE_FILTER_TEMPLATE = """
dbo:
  target: target_schema
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
        snapshotDeleteFilter:
          clauses:
            - column: "ID"
              type: "int"
              operation: "NotEqual"
              value: "2222"
        snapshotWriteMethod: "UPSERT"
"""


class SnapshotDeleteFilterTests(unittest.TestCase):
    """Verify snapshotDeleteFilter lands in target entityType."""

    def test_snapshot_delete_filter_clauses_in_target_entity_type(self):
        import create_all_entities

        yaml_config = yaml.safe_load(SNAPSHOT_DELETE_FILTER_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
            return {}

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=True),
                _source_col("NAME", 2, "varchar"),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="dbo",
                target_schema="target_schema",
                tables=[{"name": "DRIVERS", "schema": "dbo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        payload = captured_puts[0]
        entity = payload["entities"][0]
        target_ae = entity["agentEntities"][1]
        target_et = target_ae["entityType"]

        self.assertIn("snapshotDeleteFilter", target_et)
        clauses = target_et["snapshotDeleteFilter"]["clauses"]
        self.assertEqual(len(clauses), 1)
        self.assertEqual(clauses[0]["column"]["name"], "ID")
        self.assertEqual(clauses[0]["operation"]["type"], "NotEqual")
        self.assertEqual(clauses[0]["operation"]["filterValue"], "2222")

    def test_snapshot_write_method_not_in_custom_properties(self):
        """
        snapshotWriteMethod must be kept separate and NOT added to
        target customProperties (v2.1.17 regression fix).
        """
        import create_all_entities

        yaml_config = yaml.safe_load(SNAPSHOT_DELETE_FILTER_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
            return {}

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=True),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="dbo",
                target_schema="target_schema",
                tables=[{"name": "DRIVERS", "schema": "dbo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        payload = captured_puts[0]
        entity = payload["entities"][0]
        target_ae = entity["agentEntities"][1]

        # snapshotWriteMethod must NOT leak into customProperties
        self.assertNotIn("snapshotWriteMethod", target_ae.get("customProperties", {}))


if __name__ == "__main__":
    unittest.main()

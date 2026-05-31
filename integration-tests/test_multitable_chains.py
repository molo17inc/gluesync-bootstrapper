#!/usr/bin/env python3
"""
Regression tests for MultiTable (chain) entity creation.

Used by integration tests such as:
- mssql-ct-mysql-chains-integration-test
- mssql-cdc-mysql-chains-integration-test
- mysql8-mssql-ct-chains-integration-test
- mysql8-mssql-cdc-chains-integration-test
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


CHAIN_TEMPLATE = """
dbo:
  target: demo
  tables:
    whitelist:
      - ORDERS_HEADERS
      - ORDERS_ROWS
    blacklist: []
    custom:
      ORDERS_HEADERS:
        chainId: "orders_chain"
        orderIndex: 1
        keys: [ID]
        columns:
          - ID: "id"
          - ORDER_NUMBER: "orderNumber"
      ORDERS_ROWS:
        chainId: "orders_chain"
        orderIndex: 2
        keys: [ID]
        columns:
          - ID: "id"
          - ARTICLE_ID: "articleId"
"""


class MultiTablePayloadTests(unittest.TestCase):
    """Verify MultiTable entity payload shape."""

    def test_chain_creates_multitable_entity_with_table_ids_in_headers(self):
        """
        Tables sharing a chainId must be bundled into a single MultiTable
        entity.  Table headers inside the columns/keys arrays must contain
        the required 'id' field (TableWithId).
        """
        import create_all_entities

        yaml_config = yaml.safe_load(CHAIN_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
            return {}

        columns_by_table = {
            "ORDERS_HEADERS": [
                _source_col("ID", 1, "int", is_pk=True),
                _source_col("ORDER_NUMBER", 2, "varchar"),
            ],
            "ORDERS_ROWS": [
                _source_col("ID", 1, "int", is_pk=True),
                _source_col("ARTICLE_ID", 2, "int"),
            ],
        }

        def get_cols(token, pipeline_id, agent_id, schema, table):
            return {"columns": copy.deepcopy(columns_by_table.get(table, []))}

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=get_cols), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"):
            result = create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="dbo",
                target_schema="demo",
                tables=[
                    {"name": "ORDERS_HEADERS", "schema": "dbo", "id": 101},
                    {"name": "ORDERS_ROWS", "schema": "dbo", "id": 102},
                ],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        # Note: result counts only regular entities; multitable is tracked
        # separately in logs but not in the return dict.
        self.assertTrue(captured_puts, "Expected a MultiTable PUT payload")

        payload = captured_puts[0]
        entity = payload["entities"][0]
        self.assertEqual(entity["entityName"], "dbo.ORDERS_HEADERS")

        source_ae, target_ae = entity["agentEntities"]
        self.assertEqual(source_ae["type"], "MultiTable")
        self.assertEqual(target_ae["type"], "MultiTable")

        # Verify tables array has IDs
        src_tables = source_ae["tables"]
        self.assertEqual(len(src_tables), 2)
        for t in src_tables:
            self.assertIn("id", t)
            self.assertIn("name", t)
            self.assertIn("schema", t)

        # Verify table headers in columns have IDs
        src_columns = source_ae["columns"]
        # Even indices are table headers
        for i in range(0, len(src_columns), 2):
            header = src_columns[i]
            self.assertIn("id", header, f"Table header at index {i} missing 'id'")
            self.assertIn("name", header)
            self.assertIn("schema", header)

        # Note: MultiTable source agentEntity does not include a top-level
        # 'keys' array; keys are per-table in the target agentEntity.

        # Verify columnsMappingMatrix at entity level
        # One entry per column (2 tables x 2 columns each = 4)
        entity_matrix = entity.get("columnsMappingMatrix", [])
        self.assertTrue(entity_matrix, "Entity-level columnsMappingMatrix missing")
        self.assertEqual(len(entity_matrix), 4)

        # Verify columnsMappingMatrix in target entityType
        target_matrix = target_ae["entityType"].get("columnsMappingMatrix", [])
        self.assertTrue(target_matrix, "Target entityType columnsMappingMatrix missing")
        self.assertEqual(len(target_matrix), 4)

    def test_agent_entity_has_hardcoded_order_index_zero(self):
        """
        The orderIndex field in the MultiTable agentEntity is currently
        hardcoded to 0 regardless of the YAML orderIndex values.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(CHAIN_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            return {}

        columns_by_table = {
            "ORDERS_HEADERS": [
                _source_col("ID", 1, "int", is_pk=True),
            ],
            "ORDERS_ROWS": [
                _source_col("ID", 1, "int", is_pk=True),
            ],
        }

        def get_cols(token, pipeline_id, agent_id, schema, table):
            return {"columns": copy.deepcopy(columns_by_table.get(table, []))}

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=get_cols), \
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
                target_schema="demo",
                tables=[
                    {"name": "ORDERS_HEADERS", "schema": "dbo", "id": 101},
                    {"name": "ORDERS_ROWS", "schema": "dbo", "id": 102},
                ],
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
        source_ae = entity["agentEntities"][0]

        # orderIndex is hardcoded to 0 in both source and target agentEntities
        self.assertEqual(source_ae.get("orderIndex"), 0)
        self.assertEqual(entity["agentEntities"][1].get("orderIndex"), 0)

        # tablesProperties contains the tables but not the YAML orderIndex values
        tables_props = source_ae["tablesProperties"]
        self.assertIn("dbo.ORDERS_HEADERS", tables_props)
        self.assertIn("dbo.ORDERS_ROWS", tables_props)


if __name__ == "__main__":
    unittest.main()

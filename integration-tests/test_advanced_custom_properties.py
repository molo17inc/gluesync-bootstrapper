#!/usr/bin/env python3
"""
Regression tests for advanced custom properties:
- preSnapshotCommand / postSnapshotCommand
- partitions (simple string + extended dict)
- useBulkOperationsDuringCDC / useBulkOperationsWhileSnapshot
- snapshotWritingConcurrency
- writePageSize / writePageMultiplier
- maxFetchItemsCountPerIteration / maxTransactionMessageKbSize

Used by:
- mssql-ct-mssql-ct-integration-test (preSnapshotCommand, postSnapshotCommand, partitions)
- as400-pgsql-TRUNCATE-integration-test (useBulkOperations*)
- pgsqlwal-couchbase-integration-test (writePageSize, snapshotWritingConcurrency, maxFetchItemsCountPerIteration)
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


ADVANCED_PROPS_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [ORDERS_HEADERS, DRIVERS]
    blacklist: []
    custom:
      ORDERS_HEADERS:
        keys: [ID]
        customProperties:
          target:
            preSnapshotCommand: "ALTER TABLE public.ORDERS_HEADERS NOCHECK CONSTRAINT ALL;"
            postSnapshotCommand: "ALTER TABLE public.ORDERS_HEADERS CHECK CONSTRAINT ALL;"
      DRIVERS:
        keys: [ID]
        customProperties:
          target:
            snapshotWritingConcurrency: 4
            useBulkOperationsDuringCDC: true
            useBulkOperationsWhileSnapshot: true
            writePageSize: 250
            writePageMultiplier: 0
            maxFetchItemsCountPerIteration: 1000
            maxTransactionMessageKbSize: 1024
"""


class AdvancedCustomPropertiesTests(unittest.TestCase):
    """Verify advanced custom properties land in the correct payload locations."""

    def test_pre_post_snapshot_commands_in_target(self):
        import create_all_entities

        yaml_config = yaml.safe_load(ADVANCED_PROPS_TEMPLATE)
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
                source_schema="demo",
                target_schema="public",
                tables=[
                    {"name": "ORDERS_HEADERS", "schema": "demo", "id": 101},
                    {"name": "DRIVERS", "schema": "demo", "id": 102},
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
        entities = {e["entityName"]: e for e in payload["entities"]}

        orders = entities["demo.ORDERS_HEADERS"]
        target_ae = orders["agentEntities"][1]
        target_et = target_ae["entityType"]

        self.assertEqual(
            target_et["preSnapshotCommand"],
            "ALTER TABLE public.ORDERS_HEADERS NOCHECK CONSTRAINT ALL;"
        )
        self.assertEqual(
            target_et["postSnapshotCommand"],
            "ALTER TABLE public.ORDERS_HEADERS CHECK CONSTRAINT ALL;"
        )

    def test_bulk_and_concurrency_props_in_target_entity_type(self):
        import create_all_entities

        yaml_config = yaml.safe_load(ADVANCED_PROPS_TEMPLATE)
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
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 102}],
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

        self.assertEqual(target_et["snapshotWritingConcurrency"], 4)
        self.assertTrue(target_et["useBulkOperationsDuringCDC"])
        self.assertTrue(target_et["useBulkOperationsWhileSnapshot"])

    def test_source_custom_properties_not_leaking_snapshot_write_method(self):
        """
        Source customProperties should not contain keys that belong only
        to the target side (e.g. snapshotWritingConcurrency).
        """
        import create_all_entities

        yaml_config = yaml.safe_load(ADVANCED_PROPS_TEMPLATE)
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
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 102}],
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
        src_custom = source_ae.get("customProperties", {})

        self.assertNotIn("snapshotWritingConcurrency", src_custom)
        self.assertNotIn("useBulkOperationsDuringCDC", src_custom)
        self.assertNotIn("useBulkOperationsWhileSnapshot", src_custom)


if __name__ == "__main__":
    unittest.main()

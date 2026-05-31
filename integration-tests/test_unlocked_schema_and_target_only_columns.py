#!/usr/bin/env python3
"""
Regression tests for unlockedSchema and targetOnlyColumns features.

These features are used by many integration tests in gs-2 (e.g.
mysql8-vertica, pgsqlwal-couchbase, oracle-triggers-vertica).
They affect:

1. columnsMappingMatrix (single zero-entry for unlocked schema)
2. tablesWithUnlockedSchema / tablesWithUnlockedDataTypes arrays
3. targetOnlyColumns appended to target column definitions
4. UDF mandatory validation when unlockedSchema is enabled
"""

import copy
import io
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

UNLOCKED_SCHEMA_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [ARTICLES]
    blacklist: []
    custom:
      ARTICLES:
        keys: [ID]
        unlockedSchema: true
        targetOnlyColumns:
          - CURRENCY
          - name: CREATED_AT
            type: datetime
            dataLength: 19
            numericPrecision: 0
            numericScale: 0
            isNullable: false
        customProperties:
          target:
            udf:
              - name: UDF_ARTICLES_TRANSFORM
                type: Java
"""

UNLOCKED_SCHEMA_NO_UDF_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [ARTICLES]
    blacklist: []
    custom:
      ARTICLES:
        keys: [ID]
        unlockedSchema: true
"""

LOCKED_SCHEMA_WITH_TARGET_ONLY_COLUMNS_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
        targetOnlyColumns:
          - SYNC_TIMESTAMP
"""


class UnlockedSchemaPayloadTests(unittest.TestCase):
    """Verify entity payload shape when unlockedSchema is enabled."""

    def test_unlocked_schema_sets_single_zero_mapping_matrix(self):
        """
        For unlocked schema, columnsMappingMatrix must contain a single entry
        with sourceColumnId=0 and targetColumnId=0.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(UNLOCKED_SCHEMA_TEMPLATE)
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
            result = create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "ARTICLES", "schema": "demo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        self.assertEqual(result, {"successful": 1, "failed": 0, "total": 1})
        payload = captured_puts[0]
        entity = payload["entities"][0]
        target_ae = entity["agentEntities"][1]
        target_et = target_ae["entityType"]

        # Unlocked schema markers
        self.assertEqual(target_et["tablesWithUnlockedSchema"], [create_all_entities.get_table_id("public", "ARTICLES")])
        self.assertEqual(target_et["tablesWithUnlockedDataTypes"], [])

        # Single zero-entry mapping matrix
        matrix = target_et["columnsMappingMatrix"]
        self.assertEqual(len(matrix), 1)
        self.assertEqual(matrix[0]["sourceColumnId"], 0)
        self.assertEqual(matrix[0]["targetColumnId"], 0)

    def test_unlocked_schema_without_udf_raises_when_not_skipping_errors(self):
        """
        unlockedSchema=true without a UDF must raise ValueError when
        skip_errors=False.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(UNLOCKED_SCHEMA_NO_UDF_TEMPLATE)
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
             mock.patch.object(create_all_entities, "fetch_core_hub", return_value={}), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None):
            with self.assertRaises(ValueError) as cm:
                create_all_entities.create_entities(
                    token="tok",
                    pipeline_id="pipe",
                    source_schema="demo",
                    target_schema="public",
                    tables=[{"name": "ARTICLES", "schema": "demo", "id": 101}],
                    source_agent_id="src",
                    target_agent_id="tgt",
                    source_type="SQL",
                    target_type="SQL",
                    yaml_config=yaml_config,
                    skip_errors=False,
                    chunk_size=50,
                )
        self.assertIn("UDF is mandatory", str(cm.exception))

    def test_unlocked_schema_without_udf_skips_when_skip_errors_true(self):
        """
        When skip_errors=True, a missing UDF for unlocked schema should skip
        the table rather than crash.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(UNLOCKED_SCHEMA_NO_UDF_TEMPLATE)
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
             mock.patch.object(create_all_entities, "fetch_core_hub", return_value={}), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None):
            result = create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "ARTICLES", "schema": "demo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=True,
                chunk_size=50,
            )
        self.assertEqual(result["successful"], 0)
        # No entity was created for the skipped table
        self.assertEqual(result["total"], 0)


class TargetOnlyColumnsPayloadTests(unittest.TestCase):
    """Verify targetOnlyColumns behavior in entity payloads."""

    def test_target_only_columns_appended_to_target_entity(self):
        """
        Target-only columns must appear in the target agentEntity columns
        with sequential IDs after mapped source columns.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(UNLOCKED_SCHEMA_TEMPLATE)
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
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "ARTICLES", "schema": "demo", "id": 101}],
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
        target_cols = target_ae["columns"]

        # Two source columns + two target-only columns
        self.assertEqual(len(target_cols), 4)

        # Verify target-only columns are present
        col_names = [c["name"] for c in target_cols]
        self.assertIn("CURRENCY", col_names)
        self.assertIn("CREATED_AT", col_names)

        # Verify target-only columns are NOT primary keys
        currency_col = next(c for c in target_cols if c["name"] == "CURRENCY")
        self.assertFalse(currency_col.get("isPK", False))

        created_at_col = next(c for c in target_cols if c["name"] == "CREATED_AT")
        self.assertFalse(created_at_col.get("isPK", False))
        # Object format properties should be preserved
        self.assertEqual(created_at_col.get("dataType"), "datetime")

    def test_target_only_columns_without_unlocked_schema_are_skipped(self):
        """
        When targetOnlyColumns are present but unlockedSchema is not true,
        they must be ignored with a warning rather than added.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(LOCKED_SCHEMA_WITH_TARGET_ONLY_COLUMNS_TEMPLATE)
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
        target_cols = target_ae["columns"]

        # Only source columns should be present; target-only columns skipped
        col_names = [c["name"] for c in target_cols]
        self.assertNotIn("SYNC_TIMESTAMP", col_names)
        self.assertEqual(len(target_cols), 2)


class UdfInEntityTypeTests(unittest.TestCase):
    """Verify UDF configuration lands in entityType, not customProperties."""

    def test_udf_config_in_target_entity_type(self):
        """
        UDFs from YAML customProperties.target.udf must be moved to the
        target entityType (with mappingFunctionInfo pointing to the first UDF).
        """
        import create_all_entities

        yaml_config = yaml.safe_load(UNLOCKED_SCHEMA_TEMPLATE)
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
                tables=[{"name": "ARTICLES", "schema": "demo", "id": 101}],
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

        self.assertIn("udf", target_et)
        self.assertEqual(target_et["udf"][0]["name"], "UDF_ARTICLES_TRANSFORM")
        self.assertEqual(target_et["mappingFunctionInfo"]["name"], "UDF_ARTICLES_TRANSFORM")

        # UDF must NOT remain in customProperties
        self.assertNotIn("udf", target_ae.get("customProperties", {}))


if __name__ == "__main__":
    unittest.main()

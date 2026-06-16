#!/usr/bin/env python3
"""
Unit/Integration tests for fieldFunctions feature in Bootstrapper.
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


# YAML template representing payload 1
FIELD_FUNCTIONS_STANDARD_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [ARTICLES]
    blacklist: []
    custom:
      ARTICLES:
        keys: [ID]
        fieldFunctions:
          - column: ARTICLE_NAME
            expression:
              type: Dec2Sht
          - column: DESCRIPTION
            expression:
              type: DateTime2Str
              pattern: "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
              isTechnicalField: false
              requireUserInput: true
"""

# YAML template representing payload 2 with targetOnlyColumns and fieldFunctions
FIELD_FUNCTIONS_TARGET_ONLY_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
        unlockedSchema: true
        targetOnlyColumns:
          - name: additionalColumn
            type: string
            dataLength: 1000
            numericPrecision: 0
            numericScale: 0
            isNullable: false
        fieldFunctions:
          - column: additionalColumn
            expression:
              type: TsOff
              zoneName: Europe/Rome
              isTechnicalField: true
              requireUserInput: true
        customProperties:
          target:
            udf:
              - name: UDF_DRIVERS_TRANSFORM
                type: Java
"""

# YAML template representing payload 3 (MultiTable Chain)
FIELD_FUNCTIONS_CHAIN_TEMPLATE = """
demo:
  target: public
  tables:
    whitelist: [ARTICLES, CUSTOMERS]
    blacklist: []
    custom:
      ARTICLES:
        chainId: chain
        keys: [ID]
        fieldFunctions:
          - column: ID
            expression:
              type: Dbl2Dec
      CUSTOMERS:
        chainId: chain
        keys: [ID]
        fieldFunctions:
          - column: ID
            expression:
              type: Str2DateTime
              pattern: "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
              isTechnicalField: false
              requireUserInput: true
"""


class FieldFunctionsPayloadTests(unittest.TestCase):
    """Verify fieldFunctions behavior in entity payloads."""

    def test_standard_field_functions_payload(self):
        """
        Field functions configured on standard columns should be resolved
        and added to the target agentEntity entityType.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(FIELD_FUNCTIONS_STANDARD_TEMPLATE)
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
                _source_col("ID", 4054250243860915700, "int", is_pk=True),
                _source_col("ARTICLE_NAME", 5525177281213771000, "varchar"),
                _source_col("DESCRIPTION", -7150774750069270000, "varchar"),
            ]
        }

        target_table_id = create_all_entities.get_table_id("public", "ARTICLES")

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True):
            
            result = create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "ARTICLES", "schema": "demo", "id": -6596103237241045000}],
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

        self.assertIn("fieldFunctions", target_et)
        fns = target_et["fieldFunctions"]
        self.assertEqual(len(fns), 2)

        # Check resolution of column ARTICLE_NAME
        fn1 = next(f for f in fns if f["columnId"] == 5525177281213771000)
        self.assertEqual(fn1["tableOrObjectId"], target_table_id)
        self.assertEqual(fn1["expression"]["type"], "Dec2Sht")

        # Check resolution of column DESCRIPTION
        fn2 = next(f for f in fns if f["columnId"] == -7150774750069270000)
        self.assertEqual(fn2["tableOrObjectId"], target_table_id)
        self.assertEqual(fn2["expression"]["type"], "DateTime2Str")
        self.assertEqual(fn2["expression"]["pattern"], "yyyy-MM-dd'T'HH:mm:ss.SSSSSS")

    def test_target_only_field_functions_payload(self):
        """
        Field functions configured on target-only columns should be resolved
        even when the columns do not exist on the source side.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(FIELD_FUNCTIONS_TARGET_ONLY_TEMPLATE)
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
                _source_col("ID", 4054250243860915700, "int", is_pk=True),
            ]
        }

        target_table_id = create_all_entities.get_table_id("public", "DRIVERS")

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
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 8672292753311246000}],
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

        self.assertIn("fieldFunctions", target_et)
        fns = target_et["fieldFunctions"]
        self.assertEqual(len(fns), 1)

        # Check resolution of target-only column
        fn1 = fns[0]
        self.assertEqual(fn1["columnId"], 4054250243860915701) # Incremented from max target ID
        self.assertEqual(fn1["tableOrObjectId"], target_table_id)
        self.assertEqual(fn1["expression"]["type"], "TsOff")
        self.assertEqual(fn1["expression"]["zoneName"], "Europe/Rome")

    def test_multi_table_chain_field_functions_payload(self):
        """
        Field functions configured across multiple tables inside a chain should
        be successfully accumulated and added to the MultiTable target agentEntity.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(FIELD_FUNCTIONS_CHAIN_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
            return {}

        source_columns_articles = {
            "columns": [
                _source_col("ID", 4054250243860915700, "int", is_pk=True),
            ]
        }
        source_columns_customers = {
            "columns": [
                _source_col("ID", 4054250243860915700, "int", is_pk=True),
            ]
        }

        def fake_get_columns(token, pipeline_id, agent_id, schema, table_name):
            if table_name == "ARTICLES":
                return source_columns_articles
            elif table_name == "CUSTOMERS":
                return source_columns_customers
            return {"columns": []}

        target_articles_id = create_all_entities.get_table_id("public", "ARTICLES")
        target_customers_id = create_all_entities.get_table_id("public", "CUSTOMERS")

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=fake_get_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True):
            
            result = create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[
                    {"name": "ARTICLES", "schema": "demo", "id": -6596103237241045000},
                    {"name": "CUSTOMERS", "schema": "demo", "id": 3971609716785522000}
                ],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        self.assertEqual(result, {"successful": 0, "failed": 0, "total": 0})
        payload = captured_puts[0]
        entity = payload["entities"][0]
        target_ae = entity["agentEntities"][1]
        target_et = target_ae["entityType"]

        self.assertEqual(target_ae["type"], "MultiTable")
        self.assertIn("fieldFunctions", target_et)
        fns = target_et["fieldFunctions"]
        self.assertEqual(len(fns), 2)

        # Check resolution for ARTICLES table
        fn1 = next(f for f in fns if f["tableOrObjectId"] == target_articles_id)
        self.assertEqual(fn1["columnId"], 4054250243860915700)
        self.assertEqual(fn1["expression"]["type"], "Dbl2Dec")

        # Check resolution for CUSTOMERS table
        fn2 = next(f for f in fns if f["tableOrObjectId"] == target_customers_id)
        self.assertEqual(fn2["columnId"], 4054250243860915700)
        self.assertEqual(fn2["expression"]["type"], "Str2DateTime")
        self.assertEqual(fn2["expression"]["pattern"], "yyyy-MM-dd'T'HH:mm:ss.SSSSSS")


if __name__ == "__main__":
    unittest.main()

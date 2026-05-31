#!/usr/bin/env python3
"""
Regression tests for per-column targetDataType override.

Used by:
- pgsqlwal-cassandra-integration-test
- pgsqlwal-bigquery-integration-test
- pgsqlwal-vertica-integration-test
- pgsqlwal-couchbase-integration-test
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


TARGET_DATA_TYPE_EXPLICIT_TEMPLATE = """
dbo:
  target: public
  tables:
    whitelist: [VEHICLES]
    blacklist: []
    custom:
      VEHICLES:
        keys: [VEHICLE_ID]
        columns:
          - source: REGISTRATION_DATE
            target: registrationDate
            targetDataType: varchar(50)
"""

TARGET_DATA_TYPE_WHITELIST_TEMPLATE = """
dbo:
  target: public
  tables:
    whitelist: [VEHICLES]
    blacklist: []
    custom:
      VEHICLES:
        keys: [VEHICLE_ID]
        columns:
          - name: registrationDate
            sourceName: REGISTRATION_DATE
            type: timestamp
            targetDataType: text
"""


class TargetDataTypeOverrideTests(unittest.TestCase):
    """Verify targetDataType bypasses automatic mapping."""

    def test_explicit_mapping_target_data_type_override(self):
        """
        In explicit source:target mappings, targetDataType must be applied
        verbatim, bypassing map_data_type.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(TARGET_DATA_TYPE_EXPLICIT_TEMPLATE)
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
                _source_col("VEHICLE_ID", 1, "int", is_pk=True),
                _source_col("REGISTRATION_DATE", 2, "timestamp"),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", return_value="should_not_be_used"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="dbo",
                target_schema="public",
                tables=[{"name": "VEHICLES", "schema": "dbo", "id": 101}],
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

        reg_col = next(c for c in target_cols if c["name"] == "registrationDate")
        self.assertEqual(reg_col["dataType"], "varchar(50)")

    def test_whitelist_target_data_type_override(self):
        """
        In whitelist format, targetDataType must override map_data_type.
        """
        import create_all_entities

        yaml_config = yaml.safe_load(TARGET_DATA_TYPE_WHITELIST_TEMPLATE)
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
                _source_col("VEHICLE_ID", 1, "int", is_pk=True),
                _source_col("REGISTRATION_DATE", 2, "timestamp"),
            ]
        }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", return_value="should_not_be_used"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="dbo",
                target_schema="public",
                tables=[{"name": "VEHICLES", "schema": "dbo", "id": 101}],
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

        reg_col = next(c for c in target_cols if c["name"] == "registrationDate")
        self.assertEqual(reg_col["dataType"], "text")


if __name__ == "__main__":
    unittest.main()

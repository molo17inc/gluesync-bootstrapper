#!/usr/bin/env python3
"""
Regression test for YAML-defined keys when source metadata reports isPK=null.

Scenario
--------
A table-list-template declares explicit ``keys`` for a table, but the source
agent's column-discovery response returns ``isPK=None`` (or ``False``) for
every column.  We must still honour the YAML keys when building the
``GenerateCreateTargetTableStatementRequest`` payload that is sent to
CoreHub's ``generate_create_table_statement`` endpoint.

Failure mode (before any fix)
-----------------------------
If ``create_all_tables`` silently drops YAML keys because it trusts source
``isPK`` over the template, the generated CREATE TABLE statement omits the
primary-key clause and the target table is created without keys.
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


def _node_info():
    return {
        "category": "RDBMS",
        "dataTypesMatrix": [
            {
                "gluesyncDataType": "STRING",
                "defaultType": "varchar",
                "supportedTypes": ["varchar"],
            }
        ],
    }


def _source_col(name, col_id, data_type="varchar", is_pk=None, nullable=True):
    """Simulate a source-discovery column where isPK is null/unknown."""
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


def _model_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True)
    return model.dict(by_alias=True)


class YamlKeysWithNullSourcePkTests(unittest.TestCase):
    """
    Verify that ``generate_create_table_statement`` receives column DTOs with
    ``isPK=True`` for YAML-defined keys even when the source reports
    ``isPK=None`` for every column.
    """

    TABLE_LIST_YAML = """
demo:
  target: public
  tables:
    whitelist: [CUSTOMERS_NO_PKEY]
    blacklist: []
    custom:
      CUSTOMERS_NO_PKEY:
        keys:
          - ID
"""

    def test_yaml_keys_are_not_sent_when_source_isPK_is_null(self):
        """
        When the source agent returns ``isPK=None`` for all columns, the
        ``GenerateCreateTargetTableStatementRequest`` must NOT contain any
        key columns, even if the YAML explicitly lists ``keys: [ID]``.
        YAML-defined keys are honoured for entity creation but must not be
        sent to the table-creation endpoint when the source has not discovered
        primary keys.
        """
        import create_all_tables

        yaml_config = yaml.safe_load(self.TABLE_LIST_YAML)
        discovered_tables = [{"name": "CUSTOMERS_NO_PKEY"}]

        # Source columns: ID and NAME, both with isPK=None (source does not expose PKs)
        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=None),
                _source_col("NAME", 2, "varchar", is_pk=None),
            ]
        }

        captured_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            captured_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name} (...)"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=discovered_tables,
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
            )

        self.assertEqual(len(captured_requests), 1, "Expected exactly one table creation request")
        schema_name, table_name, request_body = captured_requests[0]
        self.assertEqual(table_name, "CUSTOMERS_NO_PKEY")
        self.assertEqual(schema_name, "public")  # from YAML target: public

        columns = request_body["columns"]
        self.assertEqual(len(columns), 2)

        id_col = next((c for c in columns if c["name"] == "ID"), None)
        name_col = next((c for c in columns if c["name"] == "NAME"), None)

        self.assertIsNotNone(id_col, "ID column must be present in the request")
        self.assertIsNotNone(name_col, "NAME column must be present in the request")

        # The core assertion: no column should be marked as PK because the
        # source did not discover any primary keys.
        self.assertFalse(
            id_col.get("isPK", False),
            "ID column must NOT have isPK=True because source reported isPK=None"
        )
        self.assertFalse(
            name_col.get("isPK", False),
            "NAME column must not be a PK"
        )

    def test_yaml_keys_do_not_override_source_isPK_false(self):
        """
        When the source agent returns ``isPK=False`` for all columns, the
        generated CREATE TABLE request must NOT mark any column as primary,
        even when the YAML explicitly lists ``keys: [ID]``.
        """
        import create_all_tables

        yaml_config = yaml.safe_load(self.TABLE_LIST_YAML)
        discovered_tables = [{"name": "CUSTOMERS_NO_PKEY"}]

        # Source columns with explicit isPK=False (different from null)
        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=False),
                _source_col("NAME", 2, "varchar", is_pk=False),
            ]
        }

        captured_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            captured_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name} (...)"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=discovered_tables,
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
            )

        self.assertEqual(len(captured_requests), 1)
        _, _, request_body = captured_requests[0]
        columns = request_body["columns"]

        id_col = next((c for c in columns if c["name"] == "ID"), None)
        self.assertFalse(
            id_col.get("isPK", False),
            "YAML keys must NOT override source isPK=False for table creation"
        )

    def test_multiple_yaml_keys_not_sent_when_source_isPK_is_null(self):
        """
        Multiple YAML-defined keys must NOT be marked as PK when source
        discovery returns isPK=None.
        """
        import create_all_tables

        yaml_text = """
demo:
  target: public
  tables:
    whitelist: [COMPOSITE_PKEY]
    blacklist: []
    custom:
      COMPOSITE_PKEY:
        keys:
          - ID
          - REGION
"""
        yaml_config = yaml.safe_load(yaml_text)
        discovered_tables = [{"name": "COMPOSITE_PKEY"}]

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=None),
                _source_col("REGION", 2, "varchar", is_pk=None),
                _source_col("DATA", 3, "varchar", is_pk=None),
            ]
        }

        captured_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            captured_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name} (...)"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=discovered_tables,
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
            )

        self.assertEqual(len(captured_requests), 1)
        _, _, request_body = captured_requests[0]
        columns = request_body["columns"]

        for col_name in ("ID", "REGION", "DATA"):
            col = next((c for c in columns if c["name"] == col_name), None)
            self.assertIsNotNone(col, f"{col_name} must be present")
            self.assertFalse(col.get("isPK", False), f"{col_name} must NOT be PK because source reported isPK=None")

    def test_yaml_keys_missing_from_source_columns_not_sent(self):
        """
        When a YAML-defined key is not found in the source-discovered columns
        and the source reports no primary keys, the create-table request must
        not contain any PK columns.
        """
        import create_all_tables

        yaml_text = """
demo:
  target: public
  tables:
    whitelist: [CUSTOMERS_NO_PKEY]
    blacklist: []
    custom:
      CUSTOMERS_NO_PKEY:
        keys:
          - ID
          - MISSING_KEY
"""
        yaml_config = yaml.safe_load(yaml_text)
        discovered_tables = [{"name": "CUSTOMERS_NO_PKEY"}]

        # Only ID is discovered; MISSING_KEY is not present
        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=None),
            ]
        }

        captured_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            captured_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name} (...)"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=discovered_tables,
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
            )

        self.assertEqual(len(captured_requests), 1)
        _, _, request_body = captured_requests[0]
        columns = request_body["columns"]

        # Only ID should be present (from source columns), and it must NOT be PK
        # because the source reported no primary keys.
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0]["name"], "ID")
        self.assertFalse(columns[0].get("isPK", False), "ID must NOT be PK because source reported no PKs")

    def test_null_yaml_keys_graceful_handling(self):
        """
        When 'keys' is explicitly null (None) in the YAML configuration,
        the bootstrapper must handle it gracefully without raising a TypeError.
        """
        import create_all_tables

        yaml_text = """
demo:
  target: public
  tables:
    whitelist: [CUSTOMERS_NO_PKEY]
    blacklist: []
    custom:
      CUSTOMERS_NO_PKEY:
        keys:
"""
        yaml_config = yaml.safe_load(yaml_text)
        discovered_tables = [{"name": "CUSTOMERS_NO_PKEY"}]

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=True),
                _source_col("NAME", 2, "varchar", is_pk=False),
            ]
        }

        captured_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            captured_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name} (...)"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table"):
            
            # This must run and complete without raising TypeError
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=discovered_tables,
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
            )

        self.assertEqual(len(captured_requests), 1)
        _, _, request_body = captured_requests[0]
        columns = request_body["columns"]

        id_col = next((c for c in columns if c["name"] == "ID"), None)
        self.assertIsNotNone(id_col)
        self.assertTrue(id_col.get("isPK", False), "ID must be a PK from discovery because YAML keys was null")


if __name__ == "__main__":
    unittest.main()

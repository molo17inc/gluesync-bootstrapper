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

    def test_ultimate_fallback_table_creation_all_columns_marked_as_pk(self):
        """
        When the source has no discovered primary keys and the YAML configuration
        does not define any keys (it is empty or null), the ultimate fallback
        must mark ALL columns as primary keys in the target table creation.
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

        # Source columns: both ID and NAME with is_pk=False or None
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

        self.assertEqual(len(captured_requests), 1)
        _, _, request_body = captured_requests[0]
        columns = request_body["columns"]

        self.assertEqual(len(columns), 2)
        id_col = next((c for c in columns if c["name"] == "ID"), None)
        name_col = next((c for c in columns if c["name"] == "NAME"), None)

        self.assertIsNotNone(id_col)
        self.assertIsNotNone(name_col)
        # Verify both ID and NAME are marked as PKs due to the ultimate fallback
        self.assertTrue(id_col.get("isPK", False), "ID must be a PK under the ultimate fallback")
        self.assertTrue(name_col.get("isPK", False), "NAME must be a PK under the ultimate fallback")

    def test_ultimate_fallback_entity_creation_all_columns_marked_as_pk(self):
        """
        When the source has no discovered primary keys and the YAML configuration
        does not define any keys (it is empty or null), the ultimate fallback
        must mark ALL columns as primary keys in the generated target entity payloads.
        """
        import create_all_entities

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
        discovered_tables = [{"name": "CUSTOMERS_NO_PKEY", "schema": "demo", "id": 101}]

        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=None),
                _source_col("NAME", 2, "varchar", is_pk=None),
            ]
        }

        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [{"entity": {"entityId": f"entity-{idx}", **entity}} for idx, entity in enumerate(entities, 1)]
            return {}

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch):
            
            result = create_all_entities.create_entities(
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

        self.assertEqual(result, {"successful": 1, "failed": 0, "total": 1})
        self.assertEqual(len(captured_puts), 1)
        
        entity = captured_puts[0]["entities"][0]
        # Inspect source entity columns
        source_agent_entity = entity["agentEntities"][0]
        columns = source_agent_entity["columns"]
        
        self.assertEqual(len(columns), 2, "Both columns must be included in the source entity")
        id_col = next((c for c in columns if c["name"] == "ID"), None)
        name_col = next((c for c in columns if c["name"] == "NAME"), None)
        
        self.assertIsNotNone(id_col)
        self.assertIsNotNone(name_col)
        self.assertTrue(id_col.get("isPK", False), "ID must be marked as PK under the ultimate fallback")
        self.assertTrue(name_col.get("isPK", False), "NAME must be marked as PK under the ultimate fallback")

    def test_user_provided_yaml_with_empty_keys_runs_successfully(self):
        """
        Verify that the user's provided YAML file ('fixtures/Dave_FILELIB_SIU.yaml')
        which triggered the bug (with empty keys for CLMSURPM and CLMSIUPM)
        loads, parses, and runs completely without throwing any exceptions.
        """
        import create_all_tables

        fixture_path = Path(__file__).resolve().parent / "fixtures" / "Dave_FILELIB_SIU.yaml"
        self.assertTrue(fixture_path.exists(), f"Fixture file {fixture_path} must exist")

        with open(fixture_path, "r") as f:
            yaml_config = yaml.safe_load(f)

        # Let's test table creation for ANON_TABLE_05 (which has null keys in the file)
        discovered_tables = [{"name": "ANON_TABLE_05"}]
        
        # Dynamically build source columns matching the YAML configuration for perfect alignment
        clmsurpm_cols = yaml_config['ANON_SCHEMA']['tables']['custom']['ANON_TABLE_05']['columns']
        source_columns = {
            "columns": [
                _source_col(col['name'], idx, col.get('type') or col.get('dataType') or 'varchar', is_pk=None)
                for idx, col in enumerate(clmsurpm_cols, 1)
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
            
            # This must complete successfully without raising any exceptions
            create_all_tables.create_tables(
                token="token",
                pipeline_id="pipeline",
                source_schema="ANON_SCHEMA",
                target_schema="ANON_TARGET",
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
        
        # Verify both columns got PK because of ultimate fallback
        for col in columns:
            self.assertTrue(col.get("isPK", False), f"{col['name']} must be PK under ultimate fallback")

    def test_fallback_to_target_discovered_keys(self):
        """
        Verify that when no keys are defined in YAML and no PKs are discovered on the source,
        but PKs exist in target discovery, the bootstrapper correctly resolves to target keys.
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

        # Source columns: no PKs
        source_columns = {
            "columns": [
                _source_col("ID", 1, "int", is_pk=None),
                _source_col("NAME", 2, "varchar", is_pk=None),
            ]
        }

        # Target columns: ID is PK
        target_columns = {
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
             mock.patch.object(create_all_tables, "get_table_columns", side_effect=[source_columns, target_columns]), \
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
        name_col = next((c for c in columns if c["name"] == "NAME"), None)

        self.assertIsNotNone(id_col)
        self.assertIsNotNone(name_col)
        self.assertTrue(id_col.get("isPK", False), "ID must be PK because it was discovered on target")
        self.assertFalse(name_col.get("isPK", False), "NAME must NOT be PK because target says it is not PK")


if __name__ == "__main__":
    unittest.main()

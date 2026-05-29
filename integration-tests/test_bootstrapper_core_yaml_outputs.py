#!/usr/bin/env python3
"""
Regression tests for core bootstrapper payload generation using realistic
``tables-list-template.yaml`` shapes from gluesync-demo-kits gs-2 integration tests.

The fixtures below are intentionally small, but keep the same YAML structures used by
these demo-kit templates:

- gs-2/integration_test/mysql8-mysql8-integration-test/tables-list-template.yaml
- gs-2/integration_test/mssql-couchbase-collections-CUSTOM-DOCUMENT-ID-integration-test/tables-list-template.yaml
- gs-2/integration_test/mysql8-vertica-chronos-integration-test/tables-list-template.yaml

The goal is to verify bootstrapper output contracts without needing a running
CoreHub/Chronos stack.
"""

import contextlib
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


MYSQL_MYSQL_TABLES_TEMPLATE = """
demo:
  target: demo
  tables:
    whitelist: ["CUSTOMERS_NO_PKEY", "DRIVERS", "ARTICLES", "SALES DATA"]
    blacklist: []
    custom:
      CUSTOMERS_NO_PKEY:
        keys:
          - ID
        customProperties:
          source:
            pollingIntervalMilliseconds: 1000
          target:
            ttlValue: 0
            useBulkOperationsDuringCDC: true
            useBulkOperationsWhileSnapshot: true
      DRIVERS:
        customProperties:
          source:
            pollingIntervalMilliseconds: 1000
          target:
            ttlValue: 0
            useBulkOperationsDuringCDC: true
            useBulkOperationsWhileSnapshot: true
      ARTICLES:
        customProperties:
          source:
            pollingIntervalMilliseconds: 1000
          target:
            ttlValue: 0
            useBulkOperationsDuringCDC: false
            useBulkOperationsWhileSnapshot: false
      "SALES DATA":
        keys:
          - ID
"""


MSSQL_COUCHBASE_DOCUMENT_KEY_TEMPLATE = """
dbo:
  target: dbo
  customProperties:
    source:
      maxItemsCountPerIteration: 1000
      maxMigrationItemsCountPerIteration: 1000
    target:
      ttlValue: 0
  tables:
    whitelist:
      - DRIVERS
      - DRIVERS_FILTER
    blacklist: []
    custom:
      DRIVERS:
        keys:
          - ID
        documentKey:
          prefix: "TESTPREFIX"
          suffix: "TESTSUFFIX"
          separator: "-"
          keys:
            - ID
            - LAST_NAME
        customProperties:
          source: {}
          target: {}
      DRIVERS_FILTER:
        keys:
          - ID
        columns: []
        customProperties:
          source: {}
          target: {}
        filter:
          clauses:
            - column: GENDER
              type: string
              operation: Equal
              value: "Female"
"""


MYSQL_VERTICA_CHRONOS_TEMPLATE = """
demo:
  target: public
  customProperties:
    source:
      maxItemsCountPerIteration: 1000
      pollingIntervalMilliseconds: 1000
      unchangedDataFilterType: "FIELDS"
  group_schedules:
      test:
        - name: "test group snapshot"
          description: "Create snapshot of ARTICLES and ADDRESSES every 5 minutes"
          task_type: "group_snapshot"
          cron_expression: "*/5 * * * *"
          enabled: true
  tables:
    whitelist: [DRIVERS, ARTICLES, ADDRESSES, ADDRESSES_REDO]
    blacklist: []
    custom:
      DRIVERS:
        schedules:
          - name: "Every 5 minutes driver data snapshot"
            description: "Create snapshot of driver data every 5 minutes"
            task_type: "entity_snapshot"
            cron_expression: "*/5 * * * *"
            enabled: true
      ARTICLES:
        groupId: "test"
      ADDRESSES:
        groupId: "test"
      ADDRESSES_REDO:
        schedules:
          - name: "Every 5 minutes driver data snapshot + REDO"
            description: "Redo driver entity to refresh CDC at quarter boundaries"
            task_type: "entity_redo"
            cron_expression: "*/5 * * * *"
            with_snapshot: true
            snapshot_write_method: "UPSERT"
            enabled: true
"""


def _load_yaml(text):
    return yaml.safe_load(text)


def _column(name, idx, data_type="varchar", is_pk=False, nullable=True):
    return {
        "id": idx,
        "position": idx,
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255 if data_type.lower() in {"varchar", "nvarchar"} else 0,
        "numPrec": 0,
        "numScale": 0,
    }


def _columns_for(table_name):
    columns_by_table = {
        "CUSTOMERS_NO_PKEY": [_column("ID", 1, "int"), _column("NAME", 2)],
        "DRIVERS": [_column("ID", 1, "int", is_pk=True), _column("LAST_NAME", 2), _column("GENDER", 3)],
        "ARTICLES": [_column("ID", 1, "int", is_pk=True), _column("ARTICLE_NAME", 2)],
        "SALES DATA": [_column("ID", 1, "int"), _column("TOTAL", 2, "decimal")],
        "DRIVERS_FILTER": [_column("ID", 1, "int", is_pk=True), _column("GENDER", 2)],
        "ADDRESSES": [_column("ID", 1, "int", is_pk=True), _column("CITY", 2)],
        "ADDRESSES_REDO": [_column("ID", 1, "int", is_pk=True), _column("CITY", 2)],
    }
    return {"columns": copy.deepcopy(columns_by_table.get(table_name, [_column("ID", 1, "int", is_pk=True)]))}


def _node_info(category="RDBMS"):
    return {"category": category, "dataTypesMatrix": []}


def _model_to_dict(model):
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True)
    return model.dict(by_alias=True)


class CreateTablesFromDemoKitYamlTests(unittest.TestCase):
    def test_create_tables_uses_yaml_target_schema_whitelist_custom_keys_and_quoted_names(self):
        import create_all_tables

        yaml_config = _load_yaml(MYSQL_MYSQL_TABLES_TEMPLATE)
        discovered_tables = [
            {"name": "CUSTOMERS_NO_PKEY"},
            {"name": "DRIVERS"},
            {"name": "ARTICLES"},
            {"name": "SALES DATA"},
            {"name": "NOT_WHITELISTED"},
        ]
        generated_requests = []
        create_requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            generated_requests.append((schema_name, table_name, _model_to_dict(table_data)))
            return f"CREATE TABLE {schema_name}.{table_name}"

        with mock.patch.object(create_all_tables, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_tables, "get_table_columns", side_effect=lambda token, pipeline_id, agent_id, schema, table: _columns_for(table)), \
             mock.patch.object(create_all_tables, "table_exists", return_value=False), \
             mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
             mock.patch.object(create_all_tables, "create_target_table", side_effect=lambda pipeline_id, create_table_request, token: create_requests.append(create_table_request.statement)):
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

        created_names = [table_name for _, table_name, _ in generated_requests]
        self.assertEqual(created_names, ["CUSTOMERS_NO_PKEY", "DRIVERS", "ARTICLES", "SALES DATA"])
        self.assertNotIn("NOT_WHITELISTED", created_names)
        self.assertTrue(all(schema_name == "demo" for schema_name, _, _ in generated_requests))
        self.assertIn("CREATE TABLE demo.SALES DATA", create_requests)

        # CUSTOMERS_NO_PKEY has YAML keys but no source-discovered PKs in the mock.
        # With the fix, YAML keys are NOT sent to generate_create_table_statement.
        customers_request = next(req for _, table, req in generated_requests if table == "CUSTOMERS_NO_PKEY")
        customers_columns = customers_request["columns"]
        self.assertFalse(next(col for col in customers_columns if col["name"] == "ID").get("isPK", False))

        # DRIVERS and ARTICLES have source-discovered PKs (isPK=True in mock)
        drivers_request = next(req for _, table, req in generated_requests if table == "DRIVERS")
        self.assertTrue(next(col for col in drivers_request["columns"] if col["name"] == "ID")["isPK"])

        articles_request = next(req for _, table, req in generated_requests if table == "ARTICLES")
        self.assertTrue(next(col for col in articles_request["columns"] if col["name"] == "ID")["isPK"])

        # SALES DATA also has YAML keys but no source-discovered PKs in the mock.
        sales_request = next(req for _, table, req in generated_requests if table == "SALES DATA")
        self.assertFalse(next(col for col in sales_request["columns"] if col["name"] == "ID").get("isPK", False))


class CreateEntitiesFromDemoKitYamlTests(unittest.TestCase):
    def test_create_entities_builds_document_keys_filters_and_custom_properties(self):
        import create_all_entities

        yaml_config = _load_yaml(MSSQL_COUCHBASE_DOCUMENT_KEY_TEMPLATE)
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
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info("NoSQL")]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=lambda token, pipeline_id, agent_id, schema, table: _columns_for(table)), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            result = create_all_entities.create_entities(
                token="token",
                pipeline_id="pipeline",
                source_schema="dbo",
                target_schema="fallback_target",
                tables=[{"name": "DRIVERS", "schema": "dbo", "id": 101}, {"name": "DRIVERS_FILTER", "schema": "dbo", "id": 102}],
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="NoSQL",
                yaml_config=yaml_config,
                chunk_size=50,
            )

        self.assertEqual(result, {"successful": 2, "failed": 0, "total": 2})
        payload = captured_puts[0]
        entities_by_name = {entity["entityName"]: entity for entity in payload["entities"]}

        drivers = entities_by_name["dbo.DRIVERS"]
        source_entity, target_entity = drivers["agentEntities"]
        self.assertEqual(source_entity["customProperties"]["maxItemsCountPerIteration"], 1000)
        self.assertEqual(source_entity["customProperties"]["maxMigrationItemsCountPerIteration"], 1000)
        self.assertEqual(target_entity["type"], "NoSqlEntity")
        self.assertEqual(target_entity["customProperties"]["ttlValue"], 0)
        self.assertEqual(target_entity["keyMapping"], {
            "prefix": "TESTPREFIX",
            "suffix": "TESTSUFFIX",
            "separator": "-",
            "keys": [1, 2],
        })

        filtered = entities_by_name["dbo.DRIVERS_FILTER"]
        target_entity_type = filtered["agentEntities"][1]["entityType"]
        self.assertIn("filter", target_entity_type)
        self.assertEqual(target_entity_type["filter"]["clauses"][0]["column"]["name"], "GENDER")
        self.assertEqual(target_entity_type["filter"]["clauses"][0]["operation"]["type"], "Equal")
        self.assertEqual(target_entity_type["filter"]["clauses"][0]["operation"]["filterValue"], "Female")


class CreateSchedulesFromDemoKitYamlTests(unittest.TestCase):
    def test_create_entities_wires_entity_and_group_schedules_from_chronos_template(self):
        import create_all_entities

        yaml_config = _load_yaml(MYSQL_VERTICA_CHRONOS_TEMPLATE)
        captured_puts = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured_puts.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                entities = captured_puts[-1]["entities"] if captured_puts else []
                return [
                    {"entity": {"entityId": f"entity-{entity['entityName'].split('.')[-1]}", **entity}}
                    for entity in entities
                ]
            return {}

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", side_effect=lambda token, pipeline_id, agent_id, schema, table: _columns_for(table)), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda source_type, *args, **kwargs: source_type), \
             mock.patch.object(create_all_entities, "create_group", return_value="group-test"), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch.object(create_all_entities, "create_entity_schedules") as create_entity_schedules, \
             mock.patch.object(create_all_entities, "create_group_schedules") as create_group_schedules, \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            result = create_all_entities.create_entities(
                token="token",
                pipeline_id="pipeline",
                source_schema="demo",
                target_schema="fallback_target",
                tables=[
                    {"name": "DRIVERS", "schema": "demo", "id": 201},
                    {"name": "ARTICLES", "schema": "demo", "id": 202},
                    {"name": "ADDRESSES", "schema": "demo", "id": 203},
                    {"name": "ADDRESSES_REDO", "schema": "demo", "id": 204},
                ],
                source_agent_id="source-agent",
                target_agent_id="target-agent",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                chunk_size=50,
            )

        self.assertEqual(result, {"successful": 4, "failed": 0, "total": 4})
        scheduled_table_names = [call.args[3] for call in create_entity_schedules.call_args_list]
        self.assertEqual(scheduled_table_names, ["DRIVERS", "ADDRESSES_REDO"])
        self.assertEqual(create_entity_schedules.call_args_list[1].args[4][0]["task_type"], "entity_redo")
        self.assertTrue(create_entity_schedules.call_args_list[1].args[4][0]["with_snapshot"])

        create_group_schedules.assert_called_once()
        prepared_group_schedules = create_group_schedules.call_args.args[2]
        self.assertEqual(prepared_group_schedules[0]["group_ids"], ["group-test"])
        self.assertEqual(prepared_group_schedules[0]["task_type"], "group_snapshot")
        self.assertEqual(prepared_group_schedules[0]["cron_expression"], "*/5 * * * *")


if __name__ == "__main__":
    unittest.main()

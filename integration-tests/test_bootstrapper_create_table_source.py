#!/usr/bin/env python3
"""
The create-table statement request names the source table the target table is created from.

CoreHub fills the element type of an array column that arrives without it from the source column of the same
name, so a bare ``array`` type is not generated on a PostgreSQL-family target. The request carries
``sourceSchema`` and ``sourceTable`` for it; they are left out (not null) when the source table is unknown.
"""

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import create_all_entities  # noqa: E402
import create_all_tables  # noqa: E402

_NODE_INFO = {
    "category": "RDBMS",
    "dataTypesMatrix": [{"gluesyncDataType": "STRING", "defaultType": "varchar", "supportedTypes": ["varchar"]}],
}
_SOURCE_COLUMNS = {
    "columns": [
        {"id": 1, "position": 1, "name": "ID", "dataType": "int", "isPK": True, "isNullable": False},
        {"id": 2, "position": 2, "name": "C_INT_ARRAY", "dataType": "_int4", "isPK": False, "isNullable": True},
    ]
}
_ENDPOINT = "/pipelines/p/config/entities/schemas/tgt/tables/orders_copy/statements/create-table"


def _request(**source):
    return create_all_tables.GenerateCreateTargetTableStatementRequest(columns=[], **source)


class GenerateCreateTableStatementBodyTests(unittest.TestCase):
    """What ``PUT .../statements/create-table`` is sent."""

    def _body(self, request):
        with mock.patch.object(create_all_tables, "fetch_core_hub", return_value="CREATE TABLE") as fetch:
            create_all_tables.generate_create_table_statement("p", "tgt", "orders_copy", "token", request)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], _ENDPOINT)
        self.assertEqual(fetch.call_args.kwargs["method"], "PUT")
        return fetch.call_args.kwargs["body"]

    def test_the_source_table_is_sent(self):
        body = self._body(_request(sourceSchema="public", sourceTable="orders"))
        self.assertEqual(body["sourceSchema"], "public")
        self.assertEqual(body["sourceTable"], "orders")
        self.assertEqual(body["columns"], [])

    def test_an_unknown_source_table_is_left_out_not_sent_as_null(self):
        body = self._body(_request())
        self.assertNotIn("sourceSchema", body)
        self.assertNotIn("sourceTable", body)
        self.assertEqual(body, {"columns": []})

    def test_a_source_schema_alone_is_sent_as_given(self):
        body = self._body(_request(sourceSchema="public"))
        self.assertEqual(body["sourceSchema"], "public")
        self.assertNotIn("sourceTable", body)


class TableCreationSourceTests(unittest.TestCase):
    """Where the source schema and table of the request come from."""

    def _requests(self, create):
        requests = []

        def fake_generate(pipeline_id, schema_name, table_name, token, table_data):
            requests.append((schema_name, table_name, table_data))
            return "CREATE TABLE"

        with mock.patch.object(create_all_tables, "table_exists", return_value=False), \
                mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", True), \
                mock.patch.object(create_all_tables, "map_data_type", side_effect=lambda source_type, *a, **k: source_type), \
                mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=fake_generate), \
                mock.patch.object(create_all_tables, "create_target_table"):
            create()
        return requests

    def test_create_tables_names_the_source_table_a_renamed_target_is_created_from(self):
        def create():
            with mock.patch.object(create_all_tables, "get_node_info", return_value=_NODE_INFO), \
                    mock.patch.object(create_all_tables, "get_table_columns", return_value=_SOURCE_COLUMNS):
                create_all_tables.create_tables(
                    token="token", pipeline_id="p", source_schema="public", target_schema="tgt",
                    tables=[{"name": "orders"}], source_agent_id="src", target_agent_id="tgt-agent",
                    yaml_config={"public": {"target": "tgt", "tables": {"custom": {"orders": {"name": "orders_copy"}}}}},
                )

        ((schema_name, table_name, table_data),) = self._requests(create)
        self.assertEqual((schema_name, table_name), ("tgt", "orders_copy"))
        self.assertEqual((table_data.sourceSchema, table_data.sourceTable), ("public", "orders"))

    def test_handle_table_creation_without_a_source_table_sends_none(self):
        def create():
            create_all_tables.handle_table_creation("p", "T", "tgt", [], "token", _SOURCE_COLUMNS, {}, _NODE_INFO,
                                                    _NODE_INFO)

        ((_, _, table_data),) = self._requests(create)
        self.assertIsNone(table_data.sourceSchema)
        self.assertIsNone(table_data.sourceTable)

    def test_discovered_table_entity_creation_names_the_source_table(self):
        run = SimpleNamespace(
            ctx=SimpleNamespace(pipeline_id="p", yaml_target_schema="tgt", token="token", source_node_info={},
                                target_node_info={}),
            target_type="sql", source_schema="public")
        pair = SimpleNamespace(table_name="orders", target_table_name="orders_copy", columns=_SOURCE_COLUMNS,
                               custom_config={})
        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", True), \
                mock.patch.object(create_all_entities, "handle_table_creation") as handle:
            create_all_entities._create_target_table(run, pair, [])

        handle.assert_called_once()
        self.assertEqual(handle.call_args.kwargs["source_schema"], "public")
        self.assertEqual(handle.call_args.kwargs["source_table"], "orders")
        self.assertEqual(handle.call_args.args[1], "orders_copy")


if __name__ == "__main__":
    unittest.main()

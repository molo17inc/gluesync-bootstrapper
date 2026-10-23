#!/usr/bin/env python3
"""An import creates the target table of an exported backup, and refuses a table it cannot create.

Found importing a backup the Automator exported onto a SQL target whose table did not exist yet: every row of the
export has a ``source`` key, which the table creation took for a "source->target mapping" it could not describe a table
with. It logged an INFO line, sent no create-table request, and the entity was still built and accepted, so the run
passed with an entity on a table that did not exist.

The fixture is synthetic (schema SALES, table ORDERS onto ORDER_RESULT) and has every row shape of an export: a source
column that is read and never written (``target: []``), a renamed key, a number written as text (a length of 0), a
character column with a length, a timestamp bound to a custom function, and columns no source feeds. Its target is an
Oracle-like one, which writes a VARCHAR2 from ``charMaxLength`` and a NUMBER from the precision and scale.
"""

import contextlib
import copy
import json
import logging
import re
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import yaml  # noqa: E402

import fake_corehub  # noqa: E402
from fake_corehub import FakeCoreHub, FakeFunctionCollections, column, patched  # noqa: E402

GOLDEN = TESTS_DIR / "fixtures" / "create_table_from_export_golden.json"
V23 = "2.3.0.2 - build test"
SOURCE_SCHEMA, TARGET_SCHEMA = "SALES", "TGT"
STATEMENT_PATH = "/pipelines/pipeline/config/entities/schemas/TGT/tables/{table}/statements/create-table"
CREATE_PATH = "/pipelines/pipeline/config/entities/statements/create-table"

SOURCE_MATRIX = [
    {"gluesyncDataType": "STRING", "defaultType": "VARCHAR", "supportedTypes": ["VARCHAR", "CHAR", "CHARACTER"]},
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "DECIMAL", "supportedTypes": ["DECIMAL", "NUMERIC"]},
    {"gluesyncDataType": "TIMESTAMP", "defaultType": "TIMESTAMP", "supportedTypes": ["TIMESTAMP", "DATE"]},
]
ORACLE_MATRIX = [
    {"gluesyncDataType": "STRING", "defaultType": "VARCHAR2", "supportedTypes": ["VARCHAR2", "CHAR", "NVARCHAR2"]},
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "NUMBER", "supportedTypes": ["NUMBER", "FLOAT"]},
    {"gluesyncDataType": "TIMESTAMP", "defaultType": "TIMESTAMP", "supportedTypes": ["TIMESTAMP", "DATE"]},
]


def _order_rows():
    """The rows the export writes for ORDERS: the shapes of a real export, with neutral names."""
    return [
        {"source": "ORD_SEQ", "target": [], "type": "DECIMAL", "dataLength": 0, "numericPrecision": 9,
         "numericScale": 0, "isNullable": False, "isPK": False, "id": 101, "ordinalPosition": 1, "charSet": None},
        {"source": "ORD_NO", "target": "ORDER_NUMBER", "type": "NUMBER", "dataLength": 0, "numericPrecision": 10,
         "numericScale": 0, "isNullable": False, "isPK": True, "id": 102, "ordinalPosition": 2, "charSet": None,
         "targetPosition": 1, "targetIsNullable": False},
        {"source": "CUST_TAX", "target": "TAX_ID", "type": "VARCHAR2", "dataLength": 0, "numericPrecision": 9,
         "numericScale": 0, "isNullable": False, "isPK": False, "id": 103, "ordinalPosition": 5, "charSet": None,
         "targetPosition": 2, "targetIsNullable": True, "expression": {"type": "Dec2Str"}},
        {"source": "STATUS", "target": "STATUS_CODE", "type": "VARCHAR2", "dataLength": 1, "numericPrecision": 0,
         "numericScale": 0, "isNullable": False, "isPK": False, "id": 104, "ordinalPosition": 17, "charSet": "37",
         "targetPosition": 13, "targetIsNullable": True},
        {"source": "PLACED_ON", "target": "RECEIVED_DATE", "type": "TIMESTAMP", "dataLength": 0,
         "numericPrecision": 8, "numericScale": 0, "isNullable": False, "isPK": False, "id": 105,
         "ordinalPosition": 18, "targetPosition": 15, "targetIsNullable": True,
         "expression": {"type": "CustomFn", "functionName": "convertDate",
                        "arguments": [{"type": "SourceColumnArg", "name": "PLACED_ON"}]}},
        {"source": "CREATED_BY", "target": "CREATED_BY", "type": "VARCHAR2", "dataLength": 10,
         "numericPrecision": 0, "numericScale": 0, "isNullable": False, "isPK": False, "id": 106,
         "ordinalPosition": 19, "charSet": "37", "targetPosition": 16, "targetIsNullable": True},
        {"target": "CHANGED_FLAG", "type": "VARCHAR2", "expression": {"type": "StaticStr", "staticValue": "Y"}},
        {"target": "LAST_MODIFIED_DATE", "type": "TIMESTAMP", "expression": {"type": "LocalTs"}},
    ]


def _item_rows():
    return [
        {"source": "ITEM_ID", "target": "ID", "type": "NUMBER", "dataLength": 0, "numericPrecision": 12,
         "numericScale": 0, "isNullable": False, "isPK": True, "id": 201, "ordinalPosition": 1,
         "targetPosition": 1, "targetIsNullable": False},
        {"source": "DESCR", "target": "DESCRIPTION", "type": "VARCHAR2", "dataLength": 30, "numericPrecision": 0,
         "numericScale": 0, "isNullable": True, "isPK": False, "id": 202, "ordinalPosition": 2,
         "targetPosition": 2, "targetIsNullable": True},
    ]


def source_tables():
    return {
        (SOURCE_SCHEMA, "ORDERS"): [
            column("ORD_SEQ", 101, "DECIMAL", nullable=False, position=1, numPrec=9),
            column("ORD_NO", 102, "DECIMAL", is_pk=True, nullable=False, position=2, numPrec=10),
            column("CUST_TAX", 103, "DECIMAL", nullable=False, position=5, numPrec=9),
            column("STATUS", 104, "CHAR", nullable=False, position=17, charMaxLength=1),
            column("PLACED_ON", 105, "DECIMAL", nullable=False, position=18, numPrec=8),
            column("CREATED_BY", 106, "CHAR", nullable=False, position=19, charMaxLength=10),
        ],
        (SOURCE_SCHEMA, "ITEMS"): [
            column("ITEM_ID", 201, "DECIMAL", is_pk=True, nullable=False, position=1, numPrec=12),
            column("DESCR", 202, "VARCHAR", position=2, charMaxLength=30),
        ],
        (SOURCE_SCHEMA, "ACCOUNTS"): [  # no primary key
            column("ACC_NO", 301, "DECIMAL", nullable=False, position=1, numPrec=10),
            column("OWNER_ID", 302, "DECIMAL", nullable=False, position=2, numPrec=9),
            column("NOTE", 303, "CHAR", position=3, charMaxLength=20),
        ],
    }


SOURCE_LIST = [{"name": "ORDERS", "schema": SOURCE_SCHEMA, "id": 500}, {"name": "ITEMS", "schema": SOURCE_SCHEMA, "id": 501},
               {"name": "ACCOUNTS", "schema": SOURCE_SCHEMA, "id": 502}]


def yaml_of(**tables):
    """The YAML of schema SALES: ``tables`` maps a source table to its YAML entry (whitelist: the tables named)."""
    return yaml.safe_dump({SOURCE_SCHEMA: {
        "target": TARGET_SCHEMA, "sourceType": "SQL", "targetType": "SQL",
        "tables": {"whitelist": [t.split("@@")[0] for t in tables], "custom": tables},
    }}, sort_keys=False)


def orders_yaml(rows=None, **extra):
    entry = {"name": "ORDER_RESULT", "entityName": "SALES.ORDERS", "keys": ["ORD_NO"],
             "columns": rows if rows is not None else _order_rows(), **extra}
    return yaml_of(ORDERS=entry)


def items_entry(**extra):
    return {"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"], "columns": _item_rows(), **extra}


class CreatingHub(FakeCoreHub):
    """A CoreHub that answers the table existence check and the two create-table routes.

    ``target_tables`` are the tables the target discovers and (unless ``existence_route`` is false) the ones the
    existence check finds, spelt exactly so. ``fail_statement`` and ``fail_request`` name the tables whose create-table
    statement, or create request, CoreHub answers with a 500. With ``reflect_created`` a created table is discovered
    afterwards with the columns it was created with, primary key flags included, as on a real target.
    """

    def __init__(self, source=None, target=None, *, existence_route=True, fail_statement=(), fail_request=(),
                 reflect_created=False, target_category="RDBMS", **kwargs):
        kwargs.setdefault("function_collection", FakeFunctionCollections.exporting("convertDate"))
        super().__init__(source if source is not None else source_tables(), target or {}, version=V23,
                         target_category=target_category, **kwargs)
        self.existence_route = existence_route
        self.fail_statement = set(fail_statement)
        self.fail_request = set(fail_request)
        self.reflect_created = reflect_created
        self.statements = {}  # table -> the body of its statement request
        self.created = []  # the tables whose create request went through, in order

    def node_info(self, token, pipeline_id, agent_id):
        info = super().node_info(token, pipeline_id, agent_id)
        info["dataTypesMatrix"] = copy.deepcopy(SOURCE_MATRIX if agent_id == fake_corehub.SOURCE_AGENT else ORACLE_MATRIX)
        return info

    def fetch(self, path, method="GET", token=None, body=None, **kwargs):
        statement = re.search(r"/schemas/([^/]+)/tables/([^/]+)/statements/create-table$", path)
        if statement and method == "PUT":
            table = urllib.parse.unquote(statement.group(2))
            self.events.append((method, path))
            self.statements[table] = copy.deepcopy(body)
            if table in self.fail_statement:
                raise fake_corehub._refusal(path, "cannot generate the statement", 500)
            return f"CREATE TABLE {table}"
        if path.endswith("/config/entities/statements/create-table") and method == "PUT":
            self.events.append((method, path))
            table = body["statement"].removeprefix("CREATE TABLE ")
            if table in self.fail_request:
                raise fake_corehub._refusal(path, "cannot create the table", 500)
            self.created.append(table)
            if self.reflect_created:
                self.target_tables[(TARGET_SCHEMA, table)] = [
                    {key: c[key] for key in ("id", "name", "dataType", "position", "isPK", "isNullable", "charMaxLength",
                                             "numPrec", "numScale")}
                    for c in self.statements[table]["columns"]]
            return {}
        exists = re.search(r"/schemas/([^/]+)/tables/([^/]+)$", path)
        if exists and method == "GET":
            key = (exists.group(1), urllib.parse.unquote(exists.group(2)))
            if self.existence_route and key in self.target_tables:
                return {"name": key[1]}
            raise fake_corehub._refusal(path, "table not found", 404, method="GET")
        return super().fetch(path, method, token, body, **kwargs)


def run_import(hub, yaml_text, *, create=True, tables_flag=None, target_type="SQL"):
    """Run ``create_entities`` on ``yaml_text`` against ``hub``, creating tables (``create``); returns its result.

    ``tables_flag`` is what create_all_tables sees of CREATE_TABLE_IF_NOT_EXISTS, ``create`` when None.
    """
    import create_all_entities
    import create_all_tables

    with contextlib.ExitStack() as stack:
        stack.enter_context(patched(hub))
        stack.enter_context(mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", create))
        stack.enter_context(mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS",
                                              create if tables_flag is None else tables_flag))
        stack.enter_context(mock.patch.object(create_all_tables, "fetch_core_hub", side_effect=hub.fetch))
        return create_all_entities.create_entities(
            token="token", pipeline_id="pipeline", source_schema=SOURCE_SCHEMA, target_schema=TARGET_SCHEMA,
            tables=SOURCE_LIST, source_agent_id=fake_corehub.SOURCE_AGENT, target_agent_id=fake_corehub.TARGET_AGENT,
            source_type="SQL", target_type=target_type, yaml_config=yaml.safe_load(yaml_text), skip_errors=True,
        )


def created(hub, table):
    """What the create-table statement request of ``table`` asked for: ``[(name, type, length, precision, scale,
    isPK, isNullable)]`` in the order sent, positions 1..n."""
    columns = hub.statements[table]["columns"]
    assert [c["position"] for c in columns] == list(range(1, len(columns) + 1)), columns
    return [(c["name"], c["dataType"], c["charMaxLength"], c["numPrec"], c["numScale"], c["isPK"], c["isNullable"])
            for c in columns]


def entity_names(hub):
    return [entity["entityName"] for entity in hub.saved]


EXPECTED_ORDER_RESULT = [
    ("ORDER_NUMBER", "NUMBER", 0, 10, 0, True, False),
    ("TAX_ID", "VARCHAR2", 1024, 9, 0, False, True),
    ("STATUS_CODE", "VARCHAR2", 1, 0, 0, False, True),
    ("RECEIVED_DATE", "TIMESTAMP", 0, 8, 0, False, True),
    ("CREATED_BY", "VARCHAR2", 10, 0, 0, False, True),
    ("CHANGED_FLAG", "VARCHAR2", 1024, 0, 0, False, True),
    ("LAST_MODIFIED_DATE", "TIMESTAMP", 0, 0, 0, False, True),
]


class ExportedBackupCreatesItsTableTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.hub = CreatingHub()
        cls.result = run_import(cls.hub, orders_yaml())

    def test_the_table_is_created_with_the_columns_of_the_entity(self):
        self.assertEqual(created(self.hub, "ORDER_RESULT"), EXPECTED_ORDER_RESULT)

    def test_a_column_that_is_read_and_never_written_is_not_a_column_of_the_table(self):
        self.assertNotIn("ORD_SEQ", [c[0] for c in created(self.hub, "ORDER_RESULT")])

    def test_only_the_renamed_key_is_a_primary_key(self):
        self.assertEqual([c[0] for c in created(self.hub, "ORDER_RESULT") if c[5]], ["ORDER_NUMBER"])

    def test_the_statement_goes_to_the_target_table_then_the_create_request_then_the_entity(self):
        paths = [path for _, path in self.hub.events if "function-collection" not in path]
        self.assertEqual(paths, [STATEMENT_PATH.format(table="ORDER_RESULT"), CREATE_PATH,
                                 "/pipelines/pipeline/config/entities"])
        self.assertEqual(self.hub.created, ["ORDER_RESULT"])

    def test_the_run_passes_with_the_entity_of_the_table(self):
        self.assertEqual(self.result["failed"], 0, self.result)
        self.assertEqual(entity_names(self.hub), ["SALES.ORDERS"])

    def test_the_entity_is_the_one_the_import_built_before_tables_were_created_from_exports(self):
        # recorded from origin/main, which skipped the table and built the entity
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(self.hub.saved, golden["entities"])


class OlderExportsTests(unittest.TestCase):
    """Rows exported before the target of a row was stated carry the type of the source column in ``type``."""

    def _old_rows(self):
        rows = _order_rows()
        for row in rows:
            for key in ("targetPosition", "targetIsNullable"):
                row.pop(key, None)
        by_source = {row.get("source"): row for row in rows}
        by_source["ORD_NO"]["type"] = "DECIMAL"
        by_source["CUST_TAX"]["type"] = "DECIMAL"
        by_source["STATUS"]["type"] = "CHARACTER"
        by_source["CREATED_BY"]["type"] = "CHARACTER"
        by_source["PLACED_ON"]["type"] = "TIMESTAMP"
        return rows

    def test_the_types_are_mapped_through_the_matrix_of_the_target(self):
        hub = CreatingHub()
        run_import(hub, orders_yaml(self._old_rows()))
        types = {name: data_type for name, data_type, *_ in created(hub, "ORDER_RESULT")}
        self.assertEqual(types, {
            "ORDER_NUMBER": "NUMBER", "TAX_ID": "NUMBER", "STATUS_CODE": "VARCHAR2", "RECEIVED_DATE": "TIMESTAMP",
            "CREATED_BY": "VARCHAR2", "CHANGED_FLAG": "VARCHAR2", "LAST_MODIFIED_DATE": "TIMESTAMP",
        })

    def test_without_positions_the_columns_keep_the_order_of_their_source_columns(self):
        hub = CreatingHub()
        run_import(hub, orders_yaml(self._old_rows()))
        self.assertEqual([c[0] for c in created(hub, "ORDER_RESULT")],
                         ["ORDER_NUMBER", "TAX_ID", "STATUS_CODE", "RECEIVED_DATE", "CREATED_BY", "CHANGED_FLAG",
                          "LAST_MODIFIED_DATE"])

    def test_nullability_is_the_one_of_the_source_column_without_targetIsNullable(self):
        hub = CreatingHub()
        run_import(hub, orders_yaml(self._old_rows()))
        by_name = {c[0]: c for c in created(hub, "ORDER_RESULT")}
        self.assertFalse(by_name["TAX_ID"][6])
        self.assertTrue(by_name["CHANGED_FLAG"][6])

    def test_a_declared_target_data_type_is_sent_as_declared(self):
        rows = _order_rows()
        rows[3]["targetDataType"] = "NVARCHAR2"
        hub = CreatingHub()
        run_import(hub, orders_yaml(rows))
        self.assertEqual({c[0]: c[1] for c in created(hub, "ORDER_RESULT")}["STATUS_CODE"], "NVARCHAR2")


class TargetDimensionsTests(unittest.TestCase):

    def test_the_length_precision_and_scale_of_the_target_win_over_the_source_ones(self):
        rows = _order_rows()
        rows[3].update({"targetDataLength": 40})
        rows[1].update({"targetNumericPrecision": 18, "targetNumericScale": 2})
        hub = CreatingHub()
        run_import(hub, orders_yaml(rows))
        by_name = {c[0]: c for c in created(hub, "ORDER_RESULT")}
        self.assertEqual(by_name["STATUS_CODE"][2], 40)
        self.assertEqual(by_name["ORDER_NUMBER"][3:5], (18, 2))

    def test_a_target_length_of_zero_is_a_length_of_1024_for_a_character_type(self):
        rows = _order_rows()
        rows[3]["targetDataLength"] = 0
        hub = CreatingHub()
        run_import(hub, orders_yaml(rows))
        self.assertEqual({c[0]: c[2] for c in created(hub, "ORDER_RESULT")}["STATUS_CODE"], 1024)

    def test_the_new_keys_are_export_metadata_and_no_column_names(self):
        import create_all_entities

        for key in ("targetDataLength", "targetNumericPrecision", "targetNumericScale"):
            self.assertIn(key, create_all_entities._EXPORT_METADATA_FIELDS)


class TableCreationSwitchTests(unittest.TestCase):

    def test_a_table_the_target_has_is_not_created(self):
        hub = CreatingHub(target={(TARGET_SCHEMA, "ORDER_RESULT"): [column("ORDER_NUMBER", 1, "NUMBER")]})
        result = run_import(hub, orders_yaml())
        self.assertEqual(hub.statements, {})
        self.assertEqual((result["failed"], entity_names(hub)), (0, ["SALES.ORDERS"]))

    def test_a_table_the_target_has_under_the_other_casing_is_not_created(self):
        hub = CreatingHub(target={(TARGET_SCHEMA, "order_result"): [column("ORDER_NUMBER", 1, "NUMBER")]})
        result = run_import(hub, orders_yaml())
        self.assertEqual(hub.statements, {})
        self.assertEqual((result["failed"], entity_names(hub)), (0, ["SALES.ORDERS"]))

    def test_with_table_creation_off_no_table_is_created_and_nothing_is_refused(self):
        hub = CreatingHub()
        result = run_import(hub, orders_yaml(), create=False)
        self.assertEqual((hub.statements, hub.created), ({}, []))
        self.assertEqual((result["failed"], result["errors"], entity_names(hub)), (0, [], ["SALES.ORDERS"]))

    def test_with_table_creation_off_in_create_all_tables_alone_no_table_is_created_and_nothing_is_refused(self):
        hub = CreatingHub()
        result = run_import(hub, orders_yaml(), create=True, tables_flag=False)
        self.assertEqual((hub.statements, hub.created), ({}, []))
        self.assertEqual((result["failed"], entity_names(hub)), (0, ["SALES.ORDERS"]))

    def test_a_nosql_target_gets_no_table(self):
        hub = CreatingHub(target_category="NoSQL")
        run_import(hub, orders_yaml(), target_type="NoSQL")
        self.assertEqual((hub.statements, hub.created), ({}, []))
        self.assertEqual(entity_names(hub), ["SALES.ORDERS"])

    def test_an_object_store_target_gets_no_table(self):
        hub = CreatingHub(target_category="Object Store")
        run_import(hub, orders_yaml(), target_type="Object Store")
        self.assertEqual((hub.statements, hub.created), ({}, []))

    def test_the_toggle_of_the_automator_reaches_create_all_tables(self):
        import create_all_entities
        import create_all_tables

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
                mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", False):
            create_all_entities.set_create_table_if_not_exists(True)
            self.assertTrue(create_all_entities.CREATE_TABLE_IF_NOT_EXISTS)
            self.assertTrue(create_all_tables.CREATE_TABLE_IF_NOT_EXISTS)
            create_all_entities.set_create_table_if_not_exists(False)
            self.assertFalse(create_all_entities.CREATE_TABLE_IF_NOT_EXISTS)
            self.assertFalse(create_all_tables.CREATE_TABLE_IF_NOT_EXISTS)

    def test_a_process_started_with_creation_off_creates_tables_once_the_ui_enables_it(self):
        import create_all_entities
        import create_all_tables

        hub = CreatingHub()
        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
                mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", False):
            create_all_entities.set_create_table_if_not_exists(True)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patched(hub))
                stack.enter_context(mock.patch.object(create_all_tables, "fetch_core_hub", side_effect=hub.fetch))
                # patched() switches the flag of create_all_entities off, as the other payload tests need
                stack.enter_context(mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", True))
                create_all_entities.create_entities(
                    token="token", pipeline_id="pipeline", source_schema=SOURCE_SCHEMA,
                    target_schema=TARGET_SCHEMA, tables=SOURCE_LIST, source_agent_id=fake_corehub.SOURCE_AGENT,
                    target_agent_id=fake_corehub.TARGET_AGENT, source_type="SQL", target_type="SQL",
                    yaml_config=yaml.safe_load(orders_yaml()), skip_errors=True)
        self.assertEqual(hub.created, ["ORDER_RESULT"])


class RefusedTableTests(unittest.TestCase):
    """A table that has to be created and cannot be is refused: no entity for it, and the run fails."""

    def _yaml(self):
        return yaml_of(ORDERS={"name": "ORDER_RESULT", "entityName": "SALES.ORDERS", "keys": ["ORD_NO"],
                               "columns": _order_rows()}, ITEMS=items_entry())

    def _assert_refused(self, hub, result):
        self.assertEqual(entity_names(hub), ["SALES.ITEMS"])
        self.assertEqual(result["failed"], 1, result)
        self.assertEqual(result["successful"], 1, result)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(result["errors"][0].startswith("ORDERS: "), result["errors"])
        self.assertEqual(hub.created, ["ITEM_RESULT"])

    def test_a_failing_statement_refuses_the_table_and_the_other_tables_are_still_created(self):
        hub = CreatingHub(fail_statement={"ORDER_RESULT"})
        result = run_import(hub, self._yaml())
        self._assert_refused(hub, result)
        self.assertIn("cannot generate the statement", result["errors"][0])

    def test_a_failing_create_request_refuses_the_table_and_the_other_tables_are_still_created(self):
        hub = CreatingHub(fail_request={"ORDER_RESULT"})
        result = run_import(hub, self._yaml())
        self._assert_refused(hub, result)
        self.assertIn("cannot create the table", result["errors"][0])

    def test_rows_that_name_no_source_column_refuse_the_table(self):
        rows = [{"source": "NO_SUCH_COLUMN", "target": "X", "type": "NUMBER"}]
        hub = CreatingHub()
        result = run_import(hub, yaml_of(ORDERS={"name": "ORDER_RESULT", "keys": ["ORD_NO"], "columns": rows}))
        self.assertEqual((entity_names(hub), result["failed"], hub.statements), ([], 1, {}))

    def test_a_table_the_target_reports_columns_for_is_used_when_its_creation_fails(self):
        hub = CreatingHub(target={(TARGET_SCHEMA, "ORDER_RESULT"): [column("ORDER_NUMBER", 1, "NUMBER", is_pk=True)]},
                          existence_route=False, fail_statement={"ORDER_RESULT"})
        result = run_import(hub, self._yaml())
        self.assertEqual(sorted(entity_names(hub)), ["SALES.ITEMS", "SALES.ORDERS"])
        self.assertEqual((result["failed"], result["errors"]), (0, []))

    def test_without_skip_errors_the_run_stops_on_the_refusal(self):
        import create_all_entities
        import create_all_tables

        hub = CreatingHub(fail_statement={"ORDER_RESULT"})
        yaml_config = yaml.safe_load(self._yaml())
        with contextlib.ExitStack() as stack:
            stack.enter_context(patched(hub))
            stack.enter_context(mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", True))
            stack.enter_context(mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", True))
            stack.enter_context(mock.patch.object(create_all_tables, "fetch_core_hub", side_effect=hub.fetch))
            with self.assertRaises(create_all_tables.TargetTableCreationError):
                create_all_entities.create_entities(
                    token="token", pipeline_id="pipeline", source_schema=SOURCE_SCHEMA,
                    target_schema=TARGET_SCHEMA, tables=SOURCE_LIST, source_agent_id=fake_corehub.SOURCE_AGENT,
                    target_agent_id=fake_corehub.TARGET_AGENT, source_type="SQL", target_type="SQL",
                    yaml_config=yaml_config, skip_errors=False)


class RowShapesTests(unittest.TestCase):
    """The columns of the table follow the rows the way the entity reads them."""

    def _columns(self, rows, keys=("ORD_NO",), **extra):
        hub = CreatingHub()
        entry = {"name": "ORDER_RESULT", "keys": list(keys), "columns": rows, **extra}
        run_import(hub, yaml_of(ORDERS=entry))
        return [c[0] for c in created(hub, "ORDER_RESULT")], hub

    def test_a_row_without_target_writes_the_name_of_its_source(self):
        names, _ = self._columns([{"source": "ORD_NO", "type": "NUMBER"}, {"source": "STATUS", "type": "VARCHAR2"}])
        self.assertEqual(names, ["ORD_NO", "STATUS"])

    def test_a_list_source_is_one_column(self):
        names, _ = self._columns([{"source": "ORD_NO", "target": "ORD_NO", "type": "NUMBER"},
                                  {"source": ["STATUS", "CREATED_BY"], "target": "BOTH", "type": "VARCHAR2"}])
        self.assertEqual(names, ["ORD_NO", "BOTH"])

    def test_the_shorthand_rows_name_source_and_target(self):
        names, hub = self._columns([{"ORD_NO": "ORDER_NUMBER"}, {"STATUS": "STATUS_CODE"}])
        self.assertEqual(names, ["ORDER_NUMBER", "STATUS_CODE"])
        by_name = {c[0]: c for c in created(hub, "ORDER_RESULT")}
        self.assertEqual(by_name["STATUS_CODE"][1:3], ("VARCHAR2", 1))
        self.assertTrue(by_name["ORDER_NUMBER"][5])

    def test_a_source_fanned_out_onto_several_targets_gives_a_column_each(self):
        names, _ = self._columns([{"source": "ORD_NO", "target": "A", "type": "NUMBER"},
                                  {"source": "ORD_NO", "target": "B", "type": "NUMBER"}])
        self.assertEqual(names, ["A", "B"])

    def test_a_target_named_by_two_rows_is_one_column_the_first_row_wins(self):
        rows = [{"source": "STATUS", "target": "CODE", "type": "VARCHAR2", "targetDataLength": 7},
                {"source": "ORD_NO", "target": "CODE", "type": "NUMBER"}]
        names, hub = self._columns(rows, keys=("STATUS",))
        self.assertEqual((names, created(hub, "ORDER_RESULT")[0][2]), (["CODE"], 7))

    def test_a_key_no_row_names_is_a_column_of_the_table(self):
        names, _ = self._columns([{"source": "STATUS", "target": "STATUS_CODE", "type": "VARCHAR2"}])
        self.assertEqual(names, ["STATUS_CODE", "ORD_NO"])

    def test_rows_that_write_no_source_column_fall_back_to_the_source_columns_as_the_entity_does(self):
        names, _ = self._columns([{"target": "CHANGED_FLAG", "type": "VARCHAR2",
                                   "expression": {"type": "StaticStr", "staticValue": "Y"}}])
        self.assertEqual(names, ["ORD_SEQ", "ORD_NO", "CUST_TAX", "STATUS", "PLACED_ON", "CREATED_BY", "CHANGED_FLAG"])

    def test_the_target_only_columns_no_row_writes_come_last(self):
        rows = [{"source": "ORD_NO", "target": "ORDER_NUMBER", "type": "NUMBER", "targetPosition": 1},
                {"target": "FLAG", "type": "VARCHAR2"}]
        names, _ = self._columns(rows, targetOnlyColumns=["FLAG", "EPOCH_FIELD"])
        self.assertEqual(names, ["ORDER_NUMBER", "FLAG", "EPOCH_FIELD"])


def metadata_yaml():
    """ITEMS with column metadata rows (``name:``), the form the creation read before exports were supported."""
    return yaml_of(ITEMS={"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"], "columns": [
        {"name": "ID", "dataType": "DECIMAL", "numericPrecision": 12, "isNullable": False, "ordinalPosition": 1},
        {"name": "DESCRIPTION", "dataType": "VARCHAR", "dataLength": 30, "ordinalPosition": 2},
    ]})


def discovery_yaml():
    """ITEMS with no ``columns:``: the table is made of the discovered source columns."""
    return yaml_of(ITEMS={"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"]})


def whitelist_yaml():
    """ITEMS with a column whitelist (``name``, ``sourceName``, ``type``)."""
    return yaml_of(ITEMS={"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"], "columns": [
        {"name": "ID", "sourceName": "ITEM_ID", "type": "NUMBER"},
        {"name": "DESCRIPTION", "sourceName": "DESCR", "type": "VARCHAR2"}]})


def statements_of(hub):
    """``{table: created(hub, table)}`` for every table a statement was requested for."""
    return {table: [list(column) for column in created(hub, table)] for table in hub.statements}


class OtherYamlFormsTests(unittest.TestCase):
    """The forms that created a table before exports did keep creating the table they created (recorded from
    origin/main)."""

    maxDiff = None

    def _check(self, yaml_text, form):
        hub = CreatingHub()
        run_import(hub, yaml_text)
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))["statements"][form]
        self.assertEqual(statements_of(hub), golden)

    def test_column_metadata_rows_create_the_table_they_created(self):
        self._check(metadata_yaml(), "metadata")

    def test_a_table_without_columns_in_the_yaml_is_created_from_the_discovered_source_columns(self):
        self._check(discovery_yaml(), "discovery")

    def test_a_whitelist_creates_the_table_it_created(self):
        self._check(whitelist_yaml(), "whitelist")


def account_rows():
    """ACCOUNTS has no primary key: the entity is keyed on the targets of ``keys`` plus ``targetKeys``, which names a
    column a field function writes."""
    return [
        {"source": "OWNER_ID", "target": "OWNER_SIN", "type": "NUMBER", "dataLength": 0, "numericPrecision": 9,
         "numericScale": 0, "isNullable": False, "isPK": False, "id": 302, "ordinalPosition": 2, "targetPosition": 1,
         "targetIsNullable": False},
        {"source": "ACC_NO", "target": "ACCOUNT_NUMBER", "type": "NUMBER", "dataLength": 0, "numericPrecision": 10,
         "numericScale": 0, "isNullable": False, "isPK": False, "id": 301, "ordinalPosition": 1, "targetPosition": 2,
         "targetIsNullable": False},
        {"source": "NOTE", "target": "NOTE", "type": "VARCHAR2", "dataLength": 20, "numericPrecision": 0,
         "numericScale": 0, "isNullable": True, "isPK": False, "id": 303, "ordinalPosition": 3, "targetPosition": 3,
         "targetIsNullable": True},
        {"target": "INSTITUTION_CODE", "type": "VARCHAR2", "expression": {"type": "StaticStr", "staticValue": "X"}},
    ]


def accounts_yaml(target_keys=("OWNER_SIN", "INSTITUTION_CODE", "ACCOUNT_NUMBER")):
    return yaml_of(ACCOUNTS={"name": "ACCOUNT_RESULT", "entityName": "SALES.ACCOUNTS", "keys": ["ACC_NO", "OWNER_ID"],
                             "targetKeys": list(target_keys), "columns": account_rows()})


class TargetKeysTests(unittest.TestCase):
    """The primary key of the created table is the keys of the entity: the targets of the source keys and
    ``targetKeys``, since a target with a primary key refuses an entity keyed on anything else (R12.2)."""

    def _primary_key(self, hub, table):
        return [c[0] for c in created(hub, table) if c[5]]

    def test_a_target_key_a_field_function_writes_is_part_of_the_primary_key(self):
        hub = CreatingHub(reflect_created=True)
        result = run_import(hub, accounts_yaml())
        self.assertEqual(sorted(self._primary_key(hub, "ACCOUNT_RESULT")),
                         ["ACCOUNT_NUMBER", "INSTITUTION_CODE", "OWNER_SIN"])
        self.assertEqual((result["failed"], result["errors"]), (0, []))

    def test_the_entity_is_accepted_with_those_target_keys(self):
        hub = CreatingHub(reflect_created=True)
        run_import(hub, accounts_yaml())
        target = hub.saved[0]["agentEntities"][1]
        self.assertEqual(sorted(c["name"] for c in target["columns"] if c["isPK"]),
                         ["ACCOUNT_NUMBER", "INSTITUTION_CODE", "OWNER_SIN"])

    def test_target_keys_are_matched_without_regard_to_case(self):
        hub = CreatingHub()
        run_import(hub, accounts_yaml(target_keys=("owner_sin", "institution_code", "Account_Number")))
        self.assertEqual(sorted(self._primary_key(hub, "ACCOUNT_RESULT")),
                         ["ACCOUNT_NUMBER", "INSTITUTION_CODE", "OWNER_SIN"])

    def test_a_target_key_that_is_no_column_of_the_table_refuses_it(self):
        hub = CreatingHub(reflect_created=True)
        result = run_import(hub, accounts_yaml(target_keys=("OWNER_SIN", "NO_SUCH_COLUMN")))
        self.assertEqual((entity_names(hub), hub.statements, result["failed"]), ([], {}, 1))
        self.assertIn("NO_SUCH_COLUMN", result["errors"][0])

    def test_column_metadata_rows_mark_the_target_keys_too(self):
        entry = {"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"], "targetKeys": ["description"],
                 "columns": [
                     {"name": "ID", "dataType": "DECIMAL", "numericPrecision": 12, "isNullable": False},
                     {"name": "DESCRIPTION", "dataType": "VARCHAR", "dataLength": 30}]}
        hub = CreatingHub()
        run_import(hub, yaml_of(ITEMS=entry))
        self.assertEqual(self._primary_key(hub, "ITEM_RESULT"), ["DESCRIPTION"])

    def test_the_discovered_source_columns_mark_the_target_keys_too(self):
        hub = CreatingHub()
        run_import(hub, yaml_of(ITEMS={"name": "ITEM_RESULT", "keys": ["ITEM_ID"], "targetKeys": ["DESCR"]}))
        self.assertEqual(self._primary_key(hub, "ITEM_RESULT"), ["ITEM_ID", "DESCR"])

    def test_the_target_only_columns_mark_the_target_keys_too(self):
        import create_all_tables

        captured = {}

        def statement(**kwargs):
            captured["columns"] = {c.name: c.isPK for c in kwargs["table_data"].columns}
            return "CREATE TABLE"

        custom_config = {"targetOnlyColumns": ["EPOCH_FIELD", "OTHER"], "targetKeys": ["epoch_field"]}
        with mock.patch.object(create_all_tables, "table_exists", return_value=False), \
                mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", True), \
                mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=statement), \
                mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.handle_table_creation("p", "T", "tgt", [], "token", {"columns": []}, custom_config, {}, {})
        self.assertEqual(captured["columns"], {"EPOCH_FIELD": True, "OTHER": False})


class RunSummaryTests(unittest.TestCase):
    """``main`` does not call a run with a refused table a success."""

    def _main(self, result):
        import create_all_entities

        with mock.patch.object(create_all_entities, "load_yaml_config", return_value=None), \
                mock.patch.object(create_all_entities, "_resolve_udf_path"), \
                mock.patch.object(create_all_entities, "_restore_function_collection"), \
                mock.patch.object(create_all_entities, "get_pipeline_config"), \
                mock.patch.object(create_all_entities, "_pipeline_agents",
                                  return_value=({}, {}, "source-agent", "target-agent")), \
                mock.patch.object(create_all_entities, "get_agent_tables", return_value=[{"name": "T"}]), \
                mock.patch.object(create_all_entities, "create_entities", return_value=dict(result)), \
                mock.patch.object(create_all_entities, "log_success") as log_success, \
                mock.patch.object(create_all_entities, "log_failure") as log_failure, \
                mock.patch.object(create_all_entities, "lockfile_complete") as lockfile_complete:
            returned = create_all_entities.main("pipeline-1", "src", "tgt", "SQL", "SQL", None, "token")
        return returned, log_success, log_failure, lockfile_complete

    def test_a_run_with_failed_entities_logs_the_failure_not_a_pass(self):
        result = {"successful": 1, "failed": 2, "total": 3, "errors": ["A: x", "B: y"]}
        returned, log_success, log_failure, lockfile_complete = self._main(result)
        log_success.assert_not_called()
        log_failure.assert_called_once()
        self.assertEqual(log_failure.call_args.args[1],
                         "Entity creation completed with 2 of 3 entities failed for pipeline pipeline-1")
        lockfile_complete.assert_called_once()
        self.assertEqual(returned, result)

    def test_a_run_without_failed_entities_still_logs_the_pass(self):
        result = {"successful": 3, "failed": 0, "total": 3, "errors": []}
        returned, log_success, log_failure, lockfile_complete = self._main(result)
        log_failure.assert_not_called()
        log_success.assert_called_once()
        lockfile_complete.assert_called_once()
        self.assertEqual(returned, result)


class DuplicateTableTests(unittest.TestCase):

    def test_a_duplicate_table_gets_its_target_table_created(self):
        yaml_text = yaml_of(ITEMS=items_entry(),
                            **{"ITEMS@@2": items_entry(name="ITEM_COPY", entityName="SALES.ITEMS copy",
                                                       sourceTableName="ITEMS")})
        hub = CreatingHub()
        result = run_import(hub, yaml_text)
        self.assertEqual(sorted(hub.created), ["ITEM_COPY", "ITEM_RESULT"])
        self.assertEqual(created(hub, "ITEM_COPY"), created(hub, "ITEM_RESULT"))
        self.assertEqual((result["failed"], sorted(entity_names(hub))), (0, ["SALES.ITEMS", "SALES.ITEMS copy"]))

    def test_a_duplicate_table_that_cannot_be_created_is_refused(self):
        yaml_text = yaml_of(ITEMS=items_entry(),
                            **{"ITEMS@@2": items_entry(name="ITEM_COPY", entityName="SALES.ITEMS copy",
                                                       sourceTableName="ITEMS")})
        hub = CreatingHub(fail_statement={"ITEM_COPY"})
        result = run_import(hub, yaml_text)
        self.assertEqual(entity_names(hub), ["SALES.ITEMS"])
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["errors"][0].startswith("ITEMS@@2: "), result["errors"])


def _backup_entity():
    """The entity a CoreHub stores for ORDERS onto ORDER_RESULT, whose target text is 40 long where the source is 255."""
    source_columns = [
        {"id": 1, "name": "ORD_NO", "dataType": "DECIMAL", "position": 1, "isPK": True, "isNullable": False,
         "charMaxLength": 0, "numPrec": 10, "numScale": 0, "tableId": 1},
        {"id": 2, "name": "CUSTOMER", "dataType": "VARCHAR", "position": 2, "isPK": False, "isNullable": True,
         "charMaxLength": 255, "numPrec": 0, "numScale": 0, "tableId": 1},
    ]
    target_columns = [
        {"id": 11, "name": "ORDER_NUMBER", "dataType": "NUMBER", "position": 1, "isPK": True, "isNullable": False,
         "charMaxLength": 0, "numPrec": 10, "numScale": 0, "tableId": 2},
        {"id": 12, "name": "CUSTOMER_NAME", "dataType": "VARCHAR2", "position": 2, "isPK": False, "isNullable": True,
         "charMaxLength": 40, "numPrec": 0, "numScale": 0, "tableId": 2},
        {"id": 13, "name": "NOTE", "dataType": "VARCHAR2", "position": 3, "isPK": False, "isNullable": True,
         "charMaxLength": 20, "numPrec": 0, "numScale": 0, "tableId": 2},
    ]
    matrix = [
        {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [1], "targetColumnIds": [11]},
        {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [2], "targetColumnIds": [12]},
        {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [], "targetColumnIds": [13]},
    ]
    return {
        "entityId": "e1", "entityName": "SALES.ORDERS", "groupId": "_default", "orderIndex": 0,
        "agentEntities": [
            {"agentEntityId": "a1", "agentId": "s", "entityId": "e1", "entityName": "SALES.ORDERS", "type": "SingleTable",
             "columns": source_columns, "customProperties": {}, "entityType": {"type": "Source"},
             "table": {"id": 1, "name": "ORDERS", "schema": "SALES", "type": "TABLE"},
             "tablesProperties": {"SALES.ORDERS": {}}},
            {"agentEntityId": "a2", "agentId": "t", "entityId": "e1", "entityName": "SALES.ORDERS", "type": "SingleTable",
             "columns": target_columns, "customProperties": {},
             "entityType": {"type": "Target", "columnsMappingMatrix": matrix, "fieldFunctions": [],
                            "tablesWithUnlockedSchema": [], "tablesWithUnlockedDataTypes": []},
             "table": {"id": 2, "name": "ORDER_RESULT", "schema": "TGT", "type": "TABLE"},
             "tablesProperties": {"TGT.ORDER_RESULT": {}}},
        ],
    }


def _export_rows(entity):
    import export_template_from_corehub as export_mod

    schemas = export_mod.build_schemas_from_entities(copy.deepcopy([entity]), {})
    exported = export_mod.build_yaml_structure(schemas, {})
    return exported, yaml.safe_dump(exported, sort_keys=False)


class ExportTargetDimensionsTests(unittest.TestCase):

    def test_a_row_states_the_target_length_only_when_it_differs_from_the_source_one(self):
        import export_template_from_corehub as export_mod

        source = {"name": "CUSTOMER", "dataType": "VARCHAR", "charMaxLength": 255, "numPrec": 0, "numScale": 0}
        same = {"name": "CUSTOMER", "dataType": "VARCHAR2", "charMaxLength": 255, "numPrec": 0, "numScale": 0}
        shorter = {"name": "CUSTOMER", "dataType": "VARCHAR2", "charMaxLength": 40, "numPrec": 0, "numScale": 0}
        self.assertFalse({"targetDataLength", "targetNumericPrecision", "targetNumericScale"}
                         & set(export_mod._mapping_row(source, same, 1)))
        row = export_mod._mapping_row(source, shorter, 1)
        self.assertEqual(row["targetDataLength"], 40)
        self.assertNotIn("targetNumericPrecision", row)
        self.assertEqual(row["dataLength"], 255)

    def test_a_row_states_the_target_precision_and_scale_when_they_differ(self):
        import export_template_from_corehub as export_mod

        source = {"name": "N", "dataType": "DECIMAL", "numPrec": 10, "numScale": 0}
        target = {"name": "N", "dataType": "NUMBER", "numPrec": 18, "numScale": 2}
        row = export_mod._mapping_row(source, target, 1)
        self.assertEqual((row["targetNumericPrecision"], row["targetNumericScale"]), (18, 2))

    def test_a_target_that_states_nothing_adds_nothing(self):
        import export_template_from_corehub as export_mod

        row = export_mod._mapping_row({"name": "N", "dataType": "int"}, {"name": "N", "dataType": "int"}, 1)
        self.assertFalse([key for key in row if key.startswith("targetNumeric") or key == "targetDataLength"])

    def test_a_column_no_source_feeds_states_the_length_its_table_has(self):
        import export_template_from_corehub as export_mod

        row = export_mod._mapping_row(None, {"name": "NOTE", "dataType": "VARCHAR2", "charMaxLength": 20,
                                             "numPrec": 0, "numScale": 0}, None)
        self.assertEqual(row, {"source": None, "target": "NOTE", "type": "VARCHAR2", "dataLength": 20})

    def test_a_varchar_longer_than_its_source_states_the_target_length(self):
        import export_template_from_corehub as export_mod

        source = {"name": "N", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 9, "numScale": 0}
        target = {"name": "N", "dataType": "VARCHAR2", "charMaxLength": 1024, "numPrec": 0, "numScale": 0}
        row = export_mod._mapping_row(source, target, 1)
        self.assertEqual(row["targetDataLength"], 1024)
        self.assertNotIn("targetNumericPrecision", row)

    def test_a_number_with_another_precision_and_scale_than_its_source_states_them(self):
        import export_template_from_corehub as export_mod

        source = {"name": "N", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 9, "numScale": 0}
        target = {"name": "N", "dataType": "NUMBER", "charMaxLength": 22, "numPrec": 10, "numScale": 2}
        row = export_mod._mapping_row(source, target, 1)
        self.assertEqual((row["targetNumericPrecision"], row["targetNumericScale"]), (10, 2))
        self.assertNotIn("targetDataLength", row)

    def test_the_byte_length_oracle_reports_for_a_number_is_not_a_length_to_state(self):
        import export_template_from_corehub as export_mod

        source = {"name": "N", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 10, "numScale": 0}
        target = {"name": "N", "dataType": "NUMBER", "charMaxLength": 22, "numPrec": 10, "numScale": 0}
        row = export_mod._mapping_row(source, target, 1)
        self.assertFalse([key for key in row if key.startswith("targetNumeric") or key == "targetDataLength"], row)

    def test_the_length_and_scale_oracle_reports_for_a_timestamp_are_not_dimensions_to_state(self):
        import export_template_from_corehub as export_mod

        source = {"name": "T", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 8, "numScale": 0}
        for temporal in ("TIMESTAMP(6)", "TIMESTAMP", "DATE", "INTERVAL DAY TO SECOND", "datetime"):
            target = {"name": "T", "dataType": temporal, "charMaxLength": 11, "numPrec": 0, "numScale": 6}
            row = export_mod._mapping_row(source, target, 1)
            self.assertFalse([key for key in row if key.startswith("targetNumeric") or key == "targetDataLength"],
                             (temporal, row))

    def test_a_column_no_source_feeds_states_only_the_dimensions_its_type_has(self):
        import export_template_from_corehub as export_mod

        number = export_mod._mapping_row(None, {"name": "N", "dataType": "NUMBER", "charMaxLength": 22, "numPrec": 10,
                                                "numScale": 2}, None)
        self.assertEqual(number, {"source": None, "target": "N", "type": "NUMBER", "numericPrecision": 10,
                                  "numericScale": 2})
        timestamp = export_mod._mapping_row(None, {"name": "T", "dataType": "TIMESTAMP(6)", "charMaxLength": 11,
                                                   "numPrec": 0, "numScale": 6}, None)
        self.assertEqual(timestamp, {"source": None, "target": "T", "type": "TIMESTAMP(6)"})

    def test_a_column_no_source_feeds_states_its_position(self):
        import export_template_from_corehub as export_mod

        row = export_mod._mapping_row(None, {"name": "F", "dataType": "VARCHAR2", "position": 18, "charMaxLength": 1},
                                      None)
        self.assertEqual(row["targetPosition"], 18)
        zero = export_mod._mapping_row(None, {"name": "F", "dataType": "VARCHAR2", "position": 0}, None)
        self.assertEqual(zero["targetPosition"], 0)
        self.assertNotIn("targetPosition", export_mod._mapping_row(None, {"name": "F", "dataType": "VARCHAR2"}, None))

    def test_the_length_types_are_the_ones_the_table_creation_sizes(self):
        import commons

        for data_type in ("VARCHAR2(10)", "varchar", "NCHAR", "raw", "VarBinary"):
            self.assertTrue(commons.is_length_type(data_type), data_type)
        for data_type in ("NUMBER", "TIMESTAMP(6)", "text", None):
            self.assertFalse(commons.is_length_type(data_type), data_type)
        for data_type in ("DATE", "time", "TIMESTAMP(6)", "timestamp with time zone", "DATETIME", "INTERVAL YEAR"):
            self.assertTrue(commons.is_temporal_type(data_type), data_type)
        for data_type in ("NUMBER", "VARCHAR2", "dec", None):
            self.assertFalse(commons.is_temporal_type(data_type), data_type)

    def test_the_export_of_an_entity_writes_the_target_length_and_the_length_of_a_column_no_source_feeds(self):
        exported, _ = _export_rows(_backup_entity())
        rows = exported[SOURCE_SCHEMA]["tables"]["custom"]["ORDERS"]["columns"]
        by_target = {row["target"]: row for row in rows}
        self.assertEqual(by_target["CUSTOMER_NAME"]["targetDataLength"], 40)
        self.assertEqual(by_target["CUSTOMER_NAME"]["dataLength"], 255)
        self.assertNotIn("targetDataLength", by_target["ORDER_NUMBER"])
        self.assertEqual(by_target["NOTE"]["dataLength"], 20)
        self.assertNotIn("numericPrecision", by_target["NOTE"])


class RoundTripTests(unittest.TestCase):

    def test_an_exported_backup_creates_its_table_with_the_target_length(self):
        _, exported = _export_rows(_backup_entity())
        source = {(SOURCE_SCHEMA, "ORDERS"): [
            column("ORD_NO", 1, "DECIMAL", is_pk=True, nullable=False, numPrec=10),
            column("CUSTOMER", 2, "VARCHAR", charMaxLength=255),
        ]}
        hub = CreatingHub(source)
        result = run_import(hub, exported)
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(created(hub, "ORDER_RESULT"), [
            ("ORDER_NUMBER", "NUMBER", 0, 10, 0, True, False),
            ("CUSTOMER_NAME", "VARCHAR2", 40, 0, 0, False, True),
            ("NOTE", "VARCHAR2", 20, 0, 0, False, True),
        ])

    def test_the_columns_no_source_feeds_come_back_in_the_order_they_were_exported_in(self):
        entity = _backup_entity()
        target = entity["agentEntities"][1]
        target["columns"] += [
            {"id": 14, "name": "LAST_MODIFIED_DATE", "dataType": "TIMESTAMP", "position": 5, "isPK": False,
             "isNullable": True, "charMaxLength": 11, "numPrec": 0, "numScale": 6, "tableId": 2},
            {"id": 15, "name": "CHANGED_FLAG", "dataType": "VARCHAR2", "position": 4, "isPK": False,
             "isNullable": True, "charMaxLength": 1, "numPrec": 0, "numScale": 0, "tableId": 2},
        ]
        target["entityType"]["columnsMappingMatrix"] += [
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [], "targetColumnIds": [14]},
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [], "targetColumnIds": [15]},
        ]
        exported, _ = _export_rows(entity)
        table = exported[SOURCE_SCHEMA]["tables"]["custom"]["ORDERS"]
        by_target = {row["target"]: row for row in table["columns"]}
        self.assertEqual((by_target["CHANGED_FLAG"]["targetPosition"], by_target["LAST_MODIFIED_DATE"]["targetPosition"]),
                         (4, 5))
        self.assertNotIn("dataLength", by_target["LAST_MODIFIED_DATE"])
        self.assertNotIn("numericScale", by_target["LAST_MODIFIED_DATE"])
        # the rows of the columns no source feeds come in the order the entity stored them, the reverse of the table's
        mapped = [row for row in table["columns"] if "source" in row]
        table["columns"] = mapped + sorted((row for row in table["columns"] if "source" not in row),
                                           key=lambda row: -row["targetPosition"])
        source = {(SOURCE_SCHEMA, "ORDERS"): [
            column("ORD_NO", 1, "DECIMAL", is_pk=True, nullable=False, numPrec=10),
            column("CUSTOMER", 2, "VARCHAR", charMaxLength=255),
        ]}
        hub = CreatingHub(source)
        result = run_import(hub, yaml.safe_dump(exported, sort_keys=False))
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual([c[0] for c in created(hub, "ORDER_RESULT")],
                         ["ORDER_NUMBER", "CUSTOMER_NAME", "NOTE", "CHANGED_FLAG", "LAST_MODIFIED_DATE"])
        positions = {c["name"]: c["position"] for c in hub.saved[0]["agentEntities"][1]["columns"]}
        self.assertEqual((positions["CHANGED_FLAG"], positions["LAST_MODIFIED_DATE"]), (4, 5))

    def test_an_older_export_gives_the_columns_no_source_feeds_the_places_of_the_table_it_created(self):
        rows = _order_rows()
        for row in rows:
            row.pop("targetPosition", None)
        rows.insert(-1, {"target": "LAST_MODIFIED_BY", "type": "VARCHAR2",
                         "expression": {"type": "StaticStr", "staticValue": "Z"}})
        first = CreatingHub(reflect_created=True)
        result = run_import(first, orders_yaml(rows))
        self.assertEqual(result["failed"], 0, result)
        table = [c[0] for c in created(first, "ORDER_RESULT")]
        self.assertEqual(table[-3:], ["CHANGED_FLAG", "LAST_MODIFIED_BY", "LAST_MODIFIED_DATE"])
        entity = copy.deepcopy(first.saved[0])
        target_columns = entity["agentEntities"][1]["columns"]
        self.assertEqual({c["name"]: c["position"] for c in target_columns},
                         {name: place for place, name in enumerate(table, 1)})
        # CoreHub stores them in no order of the table's: the export and the restore must not follow it
        target_columns.reverse()
        exported, text = _export_rows(entity)
        by_target = {row["target"]: row for row in exported[SOURCE_SCHEMA]["tables"]["custom"]["ORDERS"]["columns"]
                     if isinstance(row["target"], str)}
        self.assertEqual([by_target[name]["targetPosition"] for name in table[-3:]],
                         [len(table) - 2, len(table) - 1, len(table)])
        second = CreatingHub(reflect_created=True)
        self.assertEqual(run_import(second, text)["failed"], 0)
        self.assertEqual([c[0] for c in created(second, "ORDER_RESULT")], table)

    def test_a_target_only_column_the_target_does_not_report_keeps_its_exported_position(self):
        rows = _order_rows()
        rows[-1]["targetPosition"] = 0
        hub = CreatingHub()
        run_import(hub, orders_yaml(rows))
        positions = {c["name"]: c["position"] for c in hub.saved[0]["agentEntities"][1]["columns"]}
        self.assertEqual(positions["LAST_MODIFIED_DATE"], 0)
        self.assertEqual(positions["CHANGED_FLAG"], 0)

    def _positions(self, hub):
        return {c["name"]: c["position"] for c in hub.saved[0]["agentEntities"][1]["columns"]}

    def test_the_entity_takes_the_positions_of_the_table_the_run_created_and_the_next_export_is_stable(self):
        rows = _order_rows()  # mapped targetPositions 1, 2, 13, 15, 16: gaps the created table closes
        first = CreatingHub(reflect_created=True)
        self.assertEqual(run_import(first, orders_yaml(rows))["failed"], 0)
        table = {name: place for place, (name, *_) in enumerate(created(first, "ORDER_RESULT"), 1)}
        self.assertEqual(table["STATUS_CODE"], 3)
        self.assertEqual(self._positions(first), table)

        def export(hub):
            entity = copy.deepcopy(hub.saved[0])
            entity["agentEntities"][1]["columns"].reverse()
            return _export_rows(entity)

        exported, text = export(first)
        second = CreatingHub(reflect_created=True)
        self.assertEqual(run_import(second, text)["failed"], 0)
        self.assertEqual(created(second, "ORDER_RESULT"), created(first, "ORDER_RESULT"))
        self.assertEqual(self._positions(second), table)
        self.assertEqual(export(second)[0], exported)

    def test_an_older_style_export_with_whitelist_rows_takes_the_positions_of_the_created_table_too(self):
        entry = {"name": "ITEM_RESULT", "entityName": "SALES.ITEMS", "keys": ["ITEM_ID"], "columns": [
            {"name": "DESCRIPTION", "sourceName": "DESCR", "type": "VARCHAR2"},
            {"name": "ID", "sourceName": "ITEM_ID", "type": "NUMBER"}]}
        hub = CreatingHub(reflect_created=True)
        run_import(hub, yaml_of(ITEMS=entry))
        table = {name: place for place, (name, *_) in enumerate(created(hub, "ITEM_RESULT"), 1)}
        positions = {c["name"]: c["position"] for c in hub.saved[0]["agentEntities"][1]["columns"]}
        self.assertEqual(positions, table)

    def test_with_a_table_that_exists_the_exported_positions_still_win(self):
        existing = [column("ORDER_NUMBER", 11, "NUMBER", is_pk=True, nullable=False, position=1),
                    column("TAX_ID", 12, "VARCHAR2", position=2), column("STATUS_CODE", 13, "VARCHAR2", position=3),
                    column("RECEIVED_DATE", 14, "TIMESTAMP", position=4), column("CREATED_BY", 15, "VARCHAR2", position=5)]
        hub = CreatingHub(target={(TARGET_SCHEMA, "ORDER_RESULT"): existing}, reflect_created=True)
        self.assertEqual(run_import(hub, orders_yaml())["failed"], 0)
        self.assertEqual(hub.statements, {})
        positions = self._positions(hub)
        self.assertEqual((positions["STATUS_CODE"], positions["RECEIVED_DATE"], positions["CREATED_BY"]), (13, 15, 16))



if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A column only a UDF writes must come back with the restore of an unlocked-schema table.

Found by the Automator round trip (mysql8-vertica-3pipelines): UDF_TEST_DATE and UDF_TEST_TF are unlocked-schema
tables whose Java UDF writes one target column no source column feeds and no field function produces
(EPOCH_FIELD, TECHNICAL_FIELD). The export wrote the rows of the source columns and the UDF, and nothing about those
columns, so the restored entity had no such target column.

The fixture holds the two entities of that run, as Core Hub returned them. The target-only column is the last of the
target columns and sits at position 0, as the bootstrapper creates it.
"""

import copy
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import yaml  # noqa: E402

import export_template_from_corehub as export_mod  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "roundtrip" / "mysql_vertica_udf_target_only_entities.json"
SOURCE_SCHEMA, TARGET_SCHEMA = "demo_default", "public_default"
V23 = "2.3.0.2 - build test"
TARGET_ONLY = {"UDF_TEST_DATE": "EPOCH_FIELD", "UDF_TEST_TF": "TECHNICAL_FIELD"}


def _entities():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_name(entities):
    return {e["entityName"].split(".", 1)[1]: e for e in entities}


def export_yaml(entities):
    """The YAML the Automator exports for ``entities``."""
    schemas = export_mod.build_schemas_from_entities(copy.deepcopy(entities), {})
    return yaml.safe_dump(export_mod.build_yaml_structure(schemas, {}), sort_keys=False)


def _tables(entities, target_holds_columns):
    """``(source_tables, target_tables)`` the fake CoreHub discovers; the target already holds its columns, or no table."""
    source_tables, target_tables = {}, {}
    for name, entity in _by_name(entities).items():
        source_ae, target_ae = entity["agentEntities"]
        source_tables[(SOURCE_SCHEMA, name)] = [{k: v for k, v in col.items() if k != "tableId"}
                                                for col in source_ae["columns"]]
        if target_holds_columns:
            # a SQL target reports the position of every column, the one of the UDF's among them
            target_tables[(TARGET_SCHEMA, name)] = [
                {k: v for k, v in col.items() if k != "tableId"} | {"position": position}
                for position, col in enumerate(target_ae["columns"], start=1)
            ]
    return source_tables, target_tables


def restore(entities, exported, target_holds_columns=True):
    """Restore ``exported`` against the tables of ``entities``; returns the entities CoreHub saved."""
    source_tables, target_tables = _tables(entities, target_holds_columns)
    hub = FakeCoreHub(source_tables, target_tables, version=V23)
    source_list = [{"name": name, "schema": SOURCE_SCHEMA, "id": 500 + idx}
                   for idx, name in enumerate(_by_name(entities))]
    result = run_create_entities(hub, exported, source_list, source_schema=SOURCE_SCHEMA,
                                 target_schema=TARGET_SCHEMA, target_type="SQL")
    assert result["failed"] == 0, result
    return hub.saved


def target_columns(entity):
    return [{k: v for k, v in c.items() if k != "tableId"} for c in entity["agentEntities"][1]["columns"]]


class UdfTargetOnlyExportTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entities()
        cls.exported = yaml.safe_load(export_yaml(cls.original))[SOURCE_SCHEMA]["tables"]["custom"]

    def test_the_export_writes_the_column_only_the_udf_writes(self):
        for table, column in TARGET_ONLY.items():
            cfg = self.exported[table]
            self.assertTrue(cfg["unlockedSchema"])
            self.assertEqual([entry["name"] for entry in cfg["targetOnlyColumns"]], [column])
            self.assertEqual(cfg["customProperties"]["target"]["udf"][0]["type"], "Java")

    def test_the_entry_carries_what_the_import_rebuilds_the_column_from(self):
        self.assertEqual(self.exported["UDF_TEST_DATE"]["targetOnlyColumns"], [
            {"name": "EPOCH_FIELD", "type": "varchar", "dataLength": 0, "numericPrecision": 0, "numericScale": 0,
             "isNullable": False, "position": 0}])

    def test_a_column_a_source_feeds_is_not_a_target_only_column(self):
        names = {entry["name"] for entry in self.exported["UDF_TEST_DATE"]["targetOnlyColumns"]}
        fed = {row["target"] for row in self.exported["UDF_TEST_DATE"]["columns"]}
        self.assertEqual(fed, {"ID", "DAY", "MONTH", "YEAR"})
        self.assertFalse(names & fed)

    def test_a_locked_table_exports_no_target_only_columns(self):
        entities = _entities()
        for entity in entities:
            entity["agentEntities"][1]["entityType"]["tablesWithUnlockedSchema"] = []
        for cfg in yaml.safe_load(export_yaml(entities))[SOURCE_SCHEMA]["tables"]["custom"].values():
            self.assertNotIn("targetOnlyColumns", cfg)

    def test_a_column_without_a_type_is_exported_as_its_name(self):
        entities = _entities()
        for col in entities[0]["agentEntities"][1]["columns"]:
            if col["name"] == "EPOCH_FIELD":
                col.pop("dataType")
        cfg = yaml.safe_load(export_yaml(entities))[SOURCE_SCHEMA]["tables"]["custom"]["UDF_TEST_DATE"]
        self.assertEqual(cfg["targetOnlyColumns"], ["EPOCH_FIELD"])


class FieldFunctionTargetOnlyTests(unittest.TestCase):
    """A target-only column of an unlocked table can be produced by a field function instead of the UDF."""

    @staticmethod
    def _with_field_function():
        entities = _entities()
        target_ae = entities[0]["agentEntities"][1]
        column_id = next(c["id"] for c in target_ae["columns"] if c["name"] == "EPOCH_FIELD")
        target_ae["entityType"]["fieldFunctions"] = [
            {"columnId": column_id, "tableOrObjectId": target_ae["table"]["id"],
             "expression": {"type": "StaticStr", "value": "x"}}]
        return entities

    def test_the_column_is_a_target_only_column_and_keeps_its_expression_row(self):
        cfg = yaml.safe_load(export_yaml(self._with_field_function()))[SOURCE_SCHEMA]["tables"]["custom"]["UDF_TEST_DATE"]
        self.assertEqual([entry["name"] for entry in cfg["targetOnlyColumns"]], ["EPOCH_FIELD"])
        self.assertEqual(cfg["columns"][-1]["target"], "EPOCH_FIELD")
        self.assertEqual(cfg["columns"][-1]["expression"], {"type": "StaticStr", "value": "x"})

    def test_the_restore_gives_the_column_and_its_field_function_back(self):
        entities = self._with_field_function()
        restored = _by_name(restore(entities, export_yaml(entities)))["UDF_TEST_DATE"]
        target_ae = restored["agentEntities"][1]
        self.assertEqual([c["name"] for c in target_ae["columns"]], ["ID", "DAY", "MONTH", "YEAR", "EPOCH_FIELD"])
        self.assertEqual(target_ae["entityType"]["fieldFunctions"][0]["expression"], {"type": "StaticStr", "value": "x"})


class UdfTargetOnlyRestoreTests(unittest.TestCase):
    maxDiff = None

    # What a column keeps when no target table was discovered to copy lengths, character sets and the like from.
    CORE_FIELDS = ("id", "name", "dataType", "isNullable", "isPK", "position")

    def _assert_round_trip(self, target_holds_columns):
        original = _entities()
        exported = export_yaml(original)
        restored = restore(original, exported, target_holds_columns)
        self.assertEqual(len(restored), len(original))
        for entity, saved in zip(original, _by_name(restored).values()):
            expected, actual = target_columns(entity), target_columns(saved)
            if not target_holds_columns:
                expected = [{k: c[k] for k in self.CORE_FIELDS} for c in expected]
                actual = [{k: c[k] for k in self.CORE_FIELDS} for c in actual]
            self.assertEqual(actual, expected, entity["entityName"])
        if target_holds_columns:
            # exporting the restore gives the export it was restored from
            self.assertEqual(yaml.safe_load(export_yaml(restored)), yaml.safe_load(exported))

    def test_the_restored_target_has_the_column_the_udf_writes_as_it_was(self):
        self._assert_round_trip(target_holds_columns=True)

    def test_the_column_is_restored_from_the_export_when_the_target_does_not_hold_it(self):
        self._assert_round_trip(target_holds_columns=False)

    def test_the_declared_position_type_and_nullability_are_restored(self):
        original = _entities()
        schemas = yaml.safe_load(export_yaml(original))
        cfg = schemas[SOURCE_SCHEMA]["tables"]["custom"]["UDF_TEST_DATE"]
        cfg["targetOnlyColumns"] = [{"name": "EPOCH_FIELD", "type": "bigint", "dataLength": 0, "numericPrecision": 0,
                                     "numericScale": 0, "isNullable": True, "position": 9}]
        restored = _by_name(restore(original, yaml.safe_dump(schemas, sort_keys=False), target_holds_columns=False))
        column = {c["name"]: c for c in target_columns(restored["UDF_TEST_DATE"])}["EPOCH_FIELD"]
        self.assertEqual((column["dataType"], column["isNullable"], column["position"]), ("bigint", True, 9))

    def test_an_entry_that_declares_no_position_is_at_position_zero(self):
        original = _entities()
        schemas = yaml.safe_load(export_yaml(original))
        for cfg in schemas[SOURCE_SCHEMA]["tables"]["custom"].values():
            for entry in cfg["targetOnlyColumns"]:
                entry.pop("position")
        restored = restore(original, yaml.safe_dump(schemas, sort_keys=False), target_holds_columns=False)
        for saved in restored:
            self.assertEqual(target_columns(saved)[-1]["position"], 0)


class TableCreationTests(unittest.TestCase):
    """The target table of a restore is not made of the target-only columns alone."""

    def _created_columns(self, custom_config, source_columns=()):
        import create_all_tables

        captured = {}

        def statement(**kwargs):
            captured["columns"] = [c.name for c in kwargs["table_data"].columns]
            return "CREATE TABLE"

        with mock.patch.object(create_all_tables, "table_exists", return_value=False), \
                mock.patch.object(create_all_tables, "CREATE_TABLE_IF_NOT_EXISTS", True), \
                mock.patch.object(create_all_tables, "generate_create_table_statement", side_effect=statement), \
                mock.patch.object(create_all_tables, "create_target_table"):
            create_all_tables.handle_table_creation("p", "T", "tgt", [], "token",
                                                    {"columns": list(source_columns)}, custom_config, {}, {})
        return captured.get("columns")

    def test_target_only_columns_alone_describe_a_table_without_rows(self):
        self.assertEqual(self._created_columns({"targetOnlyColumns": ["EPOCH_FIELD"]}), ["EPOCH_FIELD"])

    def test_a_table_the_rows_write_is_not_created_from_the_target_only_columns(self):
        import create_all_tables

        rows = [{"source": "ID", "target": "ID", "type": "int"}]
        with self.assertRaises(create_all_tables.TargetTableCreationError):
            self._created_columns({"columns": rows, "targetOnlyColumns": ["EPOCH_FIELD"]})

    def test_the_target_only_columns_come_after_the_columns_the_rows_write(self):
        rows = [{"source": "ID", "target": "ID", "type": "int"}]
        source = [{"id": 1, "name": "ID", "dataType": "int", "position": 1}]
        self.assertEqual(self._created_columns({"columns": rows, "targetOnlyColumns": ["EPOCH_FIELD"]}, source),
                         ["ID", "EPOCH_FIELD"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The export of the column mapping (T13 of the alignment plan).

A Gluesync 2.3 CoreHub returns every matrix entry as two lists of column ids; the export read
only the single pair, so every renamed target was exported under its source name (BS-1) and the
grouped relations were lost. The export now reads both shapes: a 2.3 entity is written with the
YAML forms of each relation, and an entity of an older CoreHub exports exactly as before.
"""

import copy
import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from export_template_from_corehub import _process_multitable_entity, _process_single_entity  # noqa: E402


def scol(name, col_id, data_type="varchar", is_pk=False):
    return {"id": col_id, "name": name, "dataType": data_type, "isPK": is_pk, "ordinalPosition": col_id,
            "charMaxLength": 255, "isNullable": not is_pk}


def tcol(name, col_id, data_type="varchar", is_pk=False):
    return {"id": col_id, "name": name, "dataType": data_type, "isPK": is_pk, "ordinalPosition": col_id - 10}


def entry(sources, targets):
    return {"sourceTableObjectId": 100, "targetTableObjectId": 200, "sourceColumnIds": sources, "targetColumnIds": targets}


def pair(source, target):
    return {"sourceTableObjectId": 100, "targetTableObjectId": 200, "sourceColumnId": source, "targetColumnId": target}


SOURCE_COLUMNS = [
    scol("ID", 1, "int", is_pk=True),
    scol("FIRST_NAME", 2),
    scol("LAST_NAME", 3),
    scol("PHONE", 4),
    scol("NOTE", 5),
]
TARGET_COLUMNS = [
    tcol("id", 11, "int", is_pk=True),
    tcol("full_name", 12),
    tcol("phone_home", 13),
    tcol("phone_work", 14),
    tcol("ingest_ts", 15, "timestamp"),
    tcol("row_hash", 16),
]


def export(matrix, target_columns=TARGET_COLUMNS, field_functions=None, source_columns=SOURCE_COLUMNS):
    source_ae = {
        "type": "SingleTable",
        "entityType": {"type": "Source"},
        "table": {"id": "100", "name": "PEOPLE", "schema": "src"},
        "columns": copy.deepcopy(source_columns),
    }
    target_type = {"type": "Target", "columnsMappingMatrix": matrix}
    if field_functions:
        target_type["fieldFunctions"] = field_functions
    target_ae = {
        "type": "SingleTable",
        "entityType": target_type,
        "table": {"id": "200", "name": "people", "schema": "tgt"},
        "columns": copy.deepcopy(target_columns),
    }
    schemas = {}
    _process_single_entity({"entityName": "src.PEOPLE"}, source_ae, target_ae, schemas, {})
    return schemas["src"]["tables"]["custom"]["PEOPLE"]


def shape(rows):
    return [(row.get("source"), row.get("target")) for row in rows]


class GroupedExportTests(unittest.TestCase):
    maxDiff = None

    def test_every_relation_shape(self):
        cfg = export(
            [
                entry([1], [11]),
                entry([2, 3], [12]),
                entry([4], [13, 14]),
                entry([5], []),
                entry([], [15]),
                entry([], [16]),
            ],
            field_functions=[{"columnId": 15, "tableOrObjectId": 200, "expression": {"type": "LocalTs"}},
                             {"columnId": 12, "tableOrObjectId": 200, "expression": {"type": "CustomFn", "functionName": "concat"}}],
        )
        self.assertEqual(shape(cfg["columns"]), [
            ("ID", "id"),
            (["FIRST_NAME", "LAST_NAME"], "full_name"),
            ("PHONE", "phone_home"),
            ("PHONE", "phone_work"),
            ("NOTE", []),
            (None, "ingest_ts"),
            (None, "row_hash"),
        ])
        rows = {str(row.get("target")): row for row in cfg["columns"]}
        self.assertEqual(rows["full_name"]["expression"], {"type": "CustomFn", "functionName": "concat"})
        self.assertEqual(rows["ingest_ts"], {"target": "ingest_ts", "type": "timestamp", "targetPosition": 5,
                                          "expression": {"type": "LocalTs"}})
        self.assertEqual(rows["row_hash"], {"target": "row_hash", "type": "varchar", "targetPosition": 6})
        self.assertEqual(rows["id"]["type"], "int")
        self.assertTrue(rows["id"]["isPK"])
        self.assertNotIn("targetKeys", cfg)

    def test_a_renamed_target_keeps_its_name(self):
        # BS-1: the pair reader exported every target of a lists-only matrix under the source name.
        cfg = export([entry([1], [11]), entry([2], [12])],
                     source_columns=SOURCE_COLUMNS[:2], target_columns=TARGET_COLUMNS[:2])
        self.assertEqual(shape(cfg["columns"]), [("ID", "id"), ("FIRST_NAME", "full_name")])

    def test_target_keys_are_written_when_they_are_not_inherited(self):
        targets = [tcol("row_id", 10, "bigint", is_pk=True)] + [dict(c, isPK=False) for c in TARGET_COLUMNS[:2]]
        cfg = export([entry([1], []), entry([2], [12]), entry([], [10])],
                     source_columns=SOURCE_COLUMNS[:2], target_columns=targets)
        self.assertEqual(cfg["targetKeys"], ["row_id"])

    def test_the_source_keys_stay_in_keys(self):
        cfg = export([entry([1], [11])], source_columns=SOURCE_COLUMNS[:1], target_columns=TARGET_COLUMNS[:1])
        self.assertEqual(cfg["keys"], ["ID"])


class LegacyExportTests(unittest.TestCase):
    """An entity of a CoreHub older than 2.3 (single pairs) exports as it always has."""

    def test_pairs_and_a_source_without_entry(self):
        cfg = export(
            [pair(1, 11), pair(2, 12), pair(3, 12)],
            source_columns=SOURCE_COLUMNS[:4], target_columns=TARGET_COLUMNS[:2],
        )
        self.assertEqual(shape(cfg["columns"]), [
            ("ID", "id"), ("FIRST_NAME", "full_name"), ("LAST_NAME", "full_name"), ("PHONE", "PHONE"),
        ])
        self.assertNotIn("targetKeys", cfg)


class MultiTableExportTests(unittest.TestCase):
    """BOP-12 A: MultiTable tables export the mapping rows of SingleTable, so renames survive."""

    def export(self, matrix, field_functions=None):
        source_ae = {
            "type": "MultiTable",
            "tables": [{"id": 100, "name": "ORDERS", "schema": "src"}],
            "columns": [{"id": 100, "name": "ORDERS", "schema": "src"},
                        [scol("ORDER_ID", 1, "int", is_pk=True), scol("TOTAL", 2), scol("NOTE", 3)]],
        }
        target_type = {"type": "Target", "columnsMappingMatrix": matrix}
        if field_functions:
            target_type["fieldFunctions"] = field_functions
        target_ae = {
            "type": "MultiTable",
            "entityType": target_type,
            "tables": [{"id": 200, "name": "ORDERS_T", "schema": "tgt"}],
            "columns": [{"id": 200, "name": "ORDERS_T", "schema": "tgt"},
                        [tcol("ORDER_ID", 1, "int", is_pk=True), tcol("AMOUNT", 2), tcol("SYNCED_AT", 4, "timestamp")]],
        }
        schemas = {}
        _process_multitable_entity({"entityName": "src.ORDERS chain"}, source_ae, target_ae, schemas, {})
        return schemas["src"]["tables"]["custom"]["ORDERS"]

    def test_grouped_matrix_exports_the_rows_of_a_single_table(self):
        # BOP-16 A: a column fed by no source is a {target, expression} row, which the import checks with the key
        # rules. A non-key source column read and never written needs no row: the MultiTable source holds it anyway.
        cfg = self.export(
            [entry([1], [1]), entry([2], [2]), entry([3], []), entry([], [4])],
            field_functions=[{"columnId": 4, "tableOrObjectId": 200, "expression": {"type": "LocalTs"}}],
        )
        self.assertEqual(shape(cfg["columns"]), [("ORDER_ID", "ORDER_ID"), ("TOTAL", "AMOUNT"), (None, "SYNCED_AT")])
        self.assertEqual(cfg["columns"][2]["expression"], {"type": "LocalTs"})
        self.assertNotIn("customProperties", cfg)
        self.assertEqual(cfg["keys"], ["ORDER_ID"])

    def test_a_source_key_read_and_never_written_keeps_its_row(self):
        cfg = self.export([entry([1], []), entry([2], [2]), entry([3], []), entry([], [1])])
        self.assertEqual(shape(cfg["columns"])[:2], [("ORDER_ID", []), ("TOTAL", "AMOUNT")])

    def test_field_functions_of_another_table_of_the_chain_are_left_out(self):
        cfg = self.export(
            [entry([1], [1]), entry([2], [2]), entry([3], [])],
            field_functions=[{"columnId": 2, "tableOrObjectId": 999, "expression": {"type": "LocalTs"}}],
        )
        self.assertNotIn("expression", cfg["columns"][1])

    def test_legacy_pairs_export_mapping_rows_with_the_target_names(self):
        cfg = self.export([pair(1, 1), pair(2, 2), pair(3, 3)])
        self.assertEqual(shape(cfg["columns"]), [("ORDER_ID", "ORDER_ID"), ("TOTAL", "AMOUNT"), ("NOTE", "NOTE")])


if __name__ == "__main__":
    unittest.main()

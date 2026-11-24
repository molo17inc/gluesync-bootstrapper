#!/usr/bin/env python3
"""The fractional-seconds precision of a date/time target column survives an export and a restore (GS-1488).

A BIGINT epoch mapped by a custom function onto a SQL Server ``datetime`` column lost the column's precision: the export
wrote none for a temporal type, and the restore sent a plain ``numPrec`` (0 dropped) and never ``datetimePrec``.
"""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import create_all_tables as tables  # noqa: E402
import export_template_from_corehub as export_mod  # noqa: E402

SOURCE_INFO = {"dataTypesMatrix": [
    {"gluesyncDataType": "LONG", "defaultType": "BIGINT", "supportedTypes": ["BIGINT"]},
    {"gluesyncDataType": "TIMESTAMP", "defaultType": "TIMESTAMP", "supportedTypes": ["TIMESTAMP"]},
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "DECIMAL", "supportedTypes": ["DECIMAL"]},
]}
TARGET_INFO = {"dataTypesMatrix": [
    {"gluesyncDataType": "LONG", "defaultType": "bigint", "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "TIMESTAMP", "defaultType": "datetime2",
     "supportedTypes": ["datetime", "datetime2", "time", "timestamp"]},
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "numeric", "supportedTypes": ["numeric"]},
]}
BIGINT = {"name": "TS", "dataType": "BIGINT", "charMaxLength": 0, "numPrec": None, "numScale": None}


def target(data_type="datetime", **fields):
    return {"name": "TS", "dataType": data_type, "charMaxLength": 0, **fields}


def restored(row, source_col=None):
    relation = {"row": row, "sources": [row["source"]] if row.get("source") else [], "targets": [row["target"]]}
    return tables._exported_column_dto(row["target"], relation, source_col, tables._KeyLookup([]), SOURCE_INFO,
                                       TARGET_INFO)


class ExportTests(unittest.TestCase):

    def test_a_datetime_target_fed_by_a_bigint_states_its_precision_only(self):
        row = export_mod._mapping_row(BIGINT, target(datetimePrec=3, numPrec=23, numScale=3, charMaxLength=8), 1)
        self.assertEqual(row["targetNumericPrecision"], 3)
        self.assertNotIn("targetDataLength", row)
        self.assertNotIn("targetNumericScale", row)

    def test_a_precision_of_zero_is_exported(self):
        row = export_mod._mapping_row(BIGINT, target(datetimePrec=0), 1)
        self.assertEqual(row["targetNumericPrecision"], 0)

    def test_a_target_that_states_no_precision_exports_none(self):
        row = export_mod._mapping_row(BIGINT, target(), 1)
        self.assertNotIn("targetNumericPrecision", row)

    def test_the_legacy_column_name_is_read(self):
        row = export_mod._mapping_row(BIGINT, target(datetimePrecision=4), 1)
        self.assertEqual(row["targetNumericPrecision"], 4)

    def test_the_same_precision_as_the_source_column_exports_nothing(self):
        source = {"name": "TS", "dataType": "TIMESTAMP", "datetimePrec": 3}
        row = export_mod._mapping_row(source, target("timestamp", datetimePrec=3), 1)
        self.assertNotIn("targetNumericPrecision", row)
        other = export_mod._mapping_row(source, target("timestamp", datetimePrec=6), 1)
        self.assertEqual(other["targetNumericPrecision"], 6)

    def test_what_oracle_reports_besides_the_precision_stays_unexported(self):
        source = {"name": "TS", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 8, "numScale": 0}
        tcol = target("TIMESTAMP(6)", charMaxLength=11, numPrec=0, numScale=6, datetimePrec=6)
        row = export_mod._mapping_row(source, tcol, 1)
        self.assertEqual(row["targetNumericPrecision"], 6)
        self.assertNotIn("targetNumericScale", row)
        self.assertNotIn("targetDataLength", row)

    def test_a_number_is_unchanged(self):
        source = {"name": "N", "dataType": "DECIMAL", "charMaxLength": 0, "numPrec": 10, "numScale": 0}
        row = export_mod._mapping_row(source, {"name": "N", "dataType": "NUMBER", "numPrec": 12, "numScale": 2,
                                               "datetimePrec": 0}, 1)
        self.assertEqual((row["targetNumericPrecision"], row["targetNumericScale"]), (12, 2))

    def test_a_temporal_column_no_source_feeds_states_its_precision(self):
        row = export_mod._mapping_row(None, target("datetime2", datetimePrec=3, numPrec=0, numScale=3), None)
        self.assertEqual(row, {"source": None, "target": "TS", "type": "datetime2", "numericPrecision": 3})
        zero = export_mod._mapping_row(None, target("datetime2", datetimePrec=0), None)
        self.assertEqual(zero["numericPrecision"], 0)
        none = export_mod._mapping_row(None, target("datetime2"), None)
        self.assertNotIn("numericPrecision", none)


class RestoreTests(unittest.TestCase):

    def row(self, **fields):
        return {"source": "TS", "target": "TS", "type": "datetime", "dataLength": 0, "numericPrecision": None,
                "numericScale": None, **fields}

    def test_the_target_precision_is_the_datetime_precision(self):
        for precision in (0, 3):
            column = restored(self.row(targetNumericPrecision=precision), BIGINT)
            self.assertEqual(column.datetimePrec, precision)
            self.assertIsNone(column.numPrec)
            self.assertIsNone(column.numScale)

    def test_a_size_in_the_target_type_wins(self):
        column = restored(self.row(targetDataType="datetime2(3)", targetNumericPrecision=0), BIGINT)
        self.assertEqual((column.dataType, column.datetimePrec, column.numPrec), ("datetime2(3)", 3, None))

    def test_a_row_without_a_precision_keeps_the_default(self):
        self.assertIsNone(restored(self.row(), BIGINT).datetimePrec)

    def test_a_date_time_source_gives_its_precision_to_a_row_without_one(self):
        source = {"name": "TS", "dataType": "TIMESTAMP", "datetimePrec": 3}
        self.assertEqual(restored(self.row(type="timestamp"), source).datetimePrec, 3)

    def test_the_source_digits_of_a_row_are_not_a_precision(self):
        column = restored(self.row(numericPrecision=19), BIGINT)
        self.assertIsNone(column.datetimePrec)

    def test_a_numeric_column_is_unchanged(self):
        column = restored(self.row(type="numeric", targetNumericPrecision=12, targetNumericScale=2), BIGINT)
        self.assertEqual((column.numPrec, column.numScale, column.datetimePrec), (12, 2, None))

    def test_a_metadata_column_and_a_target_only_column(self):
        lookup = tables._KeyLookup([])
        meta = tables._metadata_column_dto(
            1, {"name": "TS", "type": "datetime", "numericPrecision": 0}, lookup, SOURCE_INFO, TARGET_INFO)
        self.assertEqual((meta.datetimePrec, meta.numPrec), (0, None))
        meta = tables._metadata_column_dto(
            1, {"name": "TS", "type": "datetime", "targetDataType": "datetime2(5)", "numericPrecision": 0}, lookup,
            SOURCE_INFO, TARGET_INFO)
        self.assertEqual(meta.datetimePrec, 5)
        only = tables._target_only_column_dto(
            1, {"name": "TS", "type": "datetime", "numericPrecision": 3}, lookup, SOURCE_INFO, TARGET_INFO)
        self.assertEqual((only.datetimePrec, only.numPrec), (3, None))
        plain = tables._target_only_column_dto(1, {"name": "TS", "type": "datetime"}, lookup, SOURCE_INFO,
                                               TARGET_INFO)
        self.assertIsNone(plain.datetimePrec)

    def test_a_target_only_column_round_trips(self):
        tcol = target("datetime2", datetimePrec=3, numPrec=0, numScale=3)
        row = export_mod._mapping_row(None, tcol, None)
        self.assertEqual(restored(row).datetimePrec, 3)
        zero = export_mod._mapping_row(None, target("datetime2", datetimePrec=0), None)
        self.assertEqual(restored(zero).datetimePrec, 0)

    def test_a_bigint_to_datetime_column_round_trips(self):
        row = export_mod._mapping_row(BIGINT, target(datetimePrec=3), 1)
        self.assertEqual(restored(row, BIGINT).datetimePrec, 3)


if __name__ == "__main__":
    unittest.main()

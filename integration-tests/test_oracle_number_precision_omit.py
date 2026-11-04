#!/usr/bin/env python3
"""Oracle NUMBER without discovery precision must not become NUMBER(0,0).

Core Hub turns numPrec=0/numScale=0 into NUMBER(0,0), which Oracle rejects with
ORA-01727. Discovery for unconstrained NUMBER often omits numPrec entirely and
only reports charMaxLength=22 (internal storage). The create-table request must
omit those fields so Core Hub emits bare NUMBER.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import create_all_tables as cat
from create_all_tables import (
    Column,
    GenerateCreateTargetTableStatementRequest,
    _KeyLookup,
    _source_column_dtos,
    generate_create_table_statement,
)


def _node(matrix):
    return {"dataTypesMatrix": matrix}


ORACLE_MATRIX = [
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "NUMBER",
     "supportedTypes": ["NUMBER"]},
    {"gluesyncDataType": "STRING", "defaultType": "VARCHAR2",
     "supportedTypes": ["VARCHAR2"]},
]


class OracleNumberPrecisionOmitTests(unittest.TestCase):
    def test_source_dtos_omit_missing_precision(self):
        discovery = {
            "columns": [
                {
                    "name": "ID",
                    "position": 1,
                    "dataType": "NUMBER",
                    "charMaxLength": 22,
                    "isPK": True,
                    "isNullable": False,
                },
                {
                    "name": "LABEL",
                    "position": 2,
                    "dataType": "VARCHAR2",
                    "charMaxLength": 100,
                    "isPK": False,
                    "isNullable": True,
                },
            ]
        }
        node = _node(ORACLE_MATRIX)
        dtos = _source_column_dtos(discovery, _KeyLookup([]), node, node)
        by_name = {c.name: c for c in dtos}
        self.assertIsNone(by_name["ID"].numPrec)
        self.assertIsNone(by_name["ID"].numScale)
        payload = GenerateCreateTargetTableStatementRequest(columns=dtos).model_dump()
        id_col = next(c for c in payload["columns"] if c["name"] == "ID")
        self.assertIsNone(id_col["numPrec"])
        self.assertIsNone(id_col["numScale"])

    def test_generate_create_table_excludes_none_fields(self):
        captured = {}

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            captured["body"] = body
            return 'CREATE TABLE "APP"."SENSOR_READINGS" ("ID" NUMBER)'

        dto = Column(
            id=1,
            tableId=0,
            name="ID",
            position=1,
            dataType="NUMBER",
            isPK=True,
            isNullable=False,
            numPrec=None,
            numScale=None,
            charMaxLength=22,
        )
        with mock.patch.object(cat, "fetch_core_hub", side_effect=fake_fetch):
            stmt = generate_create_table_statement(
                "pipe-1",
                "APP",
                "SENSOR_READINGS",
                "tok",
                GenerateCreateTargetTableStatementRequest(columns=[dto]),
            )
        self.assertIn("NUMBER", stmt)
        self.assertIsNone(captured["body"]["columns"][0]["numPrec"])
        self.assertIsNone(captured["body"]["columns"][0]["numScale"])

    def test_explicit_precision_zero_scale_preserved(self):
        """NUMBER(10,0) must keep scale 0 — only missing fields are omitted."""
        discovery = {
            "columns": [
                {
                    "name": "AMOUNT",
                    "position": 1,
                    "dataType": "NUMBER",
                    "numPrec": 10,
                    "numScale": 0,
                    "isPK": False,
                    "isNullable": True,
                }
            ]
        }
        node = _node(ORACLE_MATRIX)
        dtos = _source_column_dtos(discovery, _KeyLookup([]), node, node)
        self.assertEqual(dtos[0].numPrec, 10)
        self.assertEqual(dtos[0].numScale, 0)
        payload = GenerateCreateTargetTableStatementRequest(columns=dtos).model_dump()
        self.assertEqual(payload["columns"][0]["numPrec"], 10)
        self.assertEqual(payload["columns"][0]["numScale"], 0)


if __name__ == "__main__":
    unittest.main()

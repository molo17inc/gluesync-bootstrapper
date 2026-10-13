#!/usr/bin/env python3
"""The Automator's table templates read discovery under the names discovery uses.

CoreHub discovery serializes a column as ``isPK``, ``dataType``, ``position``, ``charMaxLength``,
``numPrec`` and ``numScale`` (gluesync-kotlin ``commons-model/.../entity/Column.kt``). The template
generator read ``isPrimaryKey``, ``type``, ``ordinalPosition``, ``dataLength``, ``numericPrecision``
and ``numericScale``, so every template had ``keys: []`` and null types, and the target tables
created from it had every column as ``varchar``.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DISCOVERY = {
    "columns": [
        {"id": 7, "tableId": 3, "name": "ID", "position": 1, "dataType": "NUMBER", "charMaxLength": None,
         "numPrec": 10, "numScale": 0, "isPK": True, "isIdentity": False, "isNullable": False},
        {"id": 8, "tableId": 3, "name": "NAME", "position": 2, "dataType": "VARCHAR2", "charMaxLength": 120,
         "numPrec": None, "numScale": None, "isPK": False, "isIdentity": False, "isNullable": True, "charSet": "AL32UTF8"},
    ]
}


def call(function_name, discovery=DISCOVERY):
    import automator_app.corehub as corehub

    with mock.patch.object(corehub, "configure_core_hub"), \
         mock.patch.object(corehub, "_get_source_agent", return_value={"agentId": "src-1"}), \
         mock.patch.object(corehub, "get_table_columns", return_value=discovery):
        return getattr(corehub, function_name)(
            token="tok", base_url="http://test", pipeline_id="pipe", schema="SALES", table_name="CUSTOMERS",
            use_ssl=False, skip_verify=False,
        )


class AutomatorTemplateTests(unittest.TestCase):
    maxDiff = None

    def test_primary_key_names_read_is_pk(self):
        self.assertEqual(call("discover_primary_key_names"), ["ID"])

    def test_column_metadata_reads_the_discovery_names(self):
        self.assertEqual(call("discover_column_metadata_for_table"), [
            {"name": "ID", "type": "NUMBER", "dataLength": 0, "numericPrecision": 10, "numericScale": 0,
             "isNullable": False, "id": 7, "ordinalPosition": 1},
            {"name": "NAME", "type": "VARCHAR2", "dataLength": 120, "numericPrecision": 0, "numericScale": 0,
             "isNullable": True, "id": 8, "ordinalPosition": 2, "charSet": "AL32UTF8"},
        ])

    def test_the_names_the_generator_read_before_are_still_accepted(self):
        legacy = {"columns": [{"id": 1, "name": "ID", "type": "int", "dataLength": 4, "numericPrecision": 9,
                               "numericScale": 0, "ordinalPosition": 1, "isPrimaryKey": True, "isNullable": False}]}
        self.assertEqual(call("discover_primary_key_names", legacy), ["ID"])
        metadata = call("discover_column_metadata_for_table", legacy)[0]
        self.assertEqual((metadata["type"], metadata["dataLength"], metadata["numericPrecision"], metadata["ordinalPosition"]),
                         ("int", 4, 9, 1))


if __name__ == "__main__":
    unittest.main()

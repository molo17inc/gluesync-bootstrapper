#!/usr/bin/env python3
"""Oracle target: MSSQL int must map to NUMBER via compatibility category fallback."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import commons


class OracleIntCompatibilityTests(unittest.TestCase):
    def test_mssql_int_to_oracle_number_via_category(self):
        # Shape mirrors Core Hub 2.3.1 oracle-triggers agent nodeInfo (no INT/SHORT/LONG
        # in dataTypesMatrix; NUMBER only under BIG_DECIMAL; compatibility NUMBER=[SHORT,INT,LONG]).
        source_node_info = {
            "dataTypesMatrix": [
                {"gluesyncDataType": "INT", "defaultType": "int", "supportedTypes": ["int", "INT"]},
                {"gluesyncDataType": "STRING", "defaultType": "varchar", "supportedTypes": ["varchar", "nvarchar"]},
            ]
        }
        target_node_info = {
            "dataTypesMatrix": [
                {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "NUMBER", "supportedTypes": ["NUMBER"]},
                {"gluesyncDataType": "STRING", "defaultType": "VARCHAR2",
                 "supportedTypes": ["CHAR", "VARCHAR2", "NVARCHAR2"]},
                {"gluesyncDataType": "FLOAT", "defaultType": "FLOAT", "supportedTypes": ["FLOAT"]},
            ],
            "dataTypeCompatibilityMatrix": {
                "NUMBER": ["SHORT", "INT", "LONG"],
                "DECIMAL": ["BIG_DECIMAL"],
                "STRING": ["STRING"],
                "FLOAT": ["FLOAT", "DOUBLE"],
            },
        }
        self.assertEqual(
            commons.map_data_type("int", source_node_info, target_node_info),
            "NUMBER",
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""GSSD-1355: Field Functions (entityType.fieldFunctions) export/import round-trip."""

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from field_function_utils import (
    apply_field_functions_to_column_mappings,
    build_field_functions_for_entity_type,
    normalize_expression_for_corehub,
    normalize_expression_for_yaml,
)


class ExpressionNormalizeTests(unittest.TestCase):
    def test_charset_renamed_to_docs_charset_name_on_export(self):
        raw = {
            "type": "Bytes2Str",
            "charset": "UTF-8",
            "isTechnicalField": False,
            "requireUserInput": True,
            "options": ["UTF-8"],
            "validationRegex": None,
        }
        out = normalize_expression_for_yaml(raw)
        self.assertEqual(out["type"], "Bytes2Str")
        self.assertEqual(out["charsetName"], "UTF-8")
        self.assertNotIn("charset", out)
        self.assertNotIn("isTechnicalField", out)
        self.assertNotIn("requireUserInput", out)
        self.assertNotIn("options", out)

    def test_charset_name_mapped_back_on_import(self):
        out = normalize_expression_for_corehub(
            {"type": "Bytes2Str", "charsetName": "ISO-8859-1"}
        )
        self.assertEqual(out, {"type": "Bytes2Str", "charset": "ISO-8859-1"})


class ApplyFieldFunctionsExportTests(unittest.TestCase):
    def test_attaches_expression_to_mapped_column_and_appends_technical(self):
        mappings = [
            {"source": "BIRTH_DATE", "target": "birth_date", "type": "DATE"},
            {"source": "EMAIL", "target": "email", "type": "VARCHAR"},
        ]
        target_cols = [
            {"id": 1, "name": "birth_date", "dataType": "DATE"},
            {"id": 2, "name": "email", "dataType": "VARCHAR"},
            {"id": 3, "name": "ingest_ts", "dataType": "TIMESTAMP"},
        ]
        field_functions = [
            {
                "columnId": 1,
                "tableOrObjectId": 99,
                "expression": {"type": "Date2Str", "pattern": "yyyy-MM-dd"},
            },
            {
                "columnId": 2,
                "tableOrObjectId": 99,
                "expression": {"type": "MaskEmail", "char": "*"},
            },
            {
                "columnId": 3,
                "tableOrObjectId": 99,
                "expression": {"type": "LocalTs"},
            },
        ]
        out = apply_field_functions_to_column_mappings(
            mappings, target_cols, field_functions
        )
        by_target = {c["target"]: c for c in out}
        self.assertEqual(by_target["birth_date"]["expression"]["type"], "Date2Str")
        self.assertEqual(by_target["birth_date"]["expression"]["pattern"], "yyyy-MM-dd")
        self.assertEqual(by_target["email"]["expression"]["type"], "MaskEmail")
        self.assertEqual(by_target["ingest_ts"]["expression"]["type"], "LocalTs")
        self.assertNotIn("source", by_target["ingest_ts"])


class BuildFieldFunctionsImportTests(unittest.TestCase):
    def test_rebuilds_corehub_field_functions_from_yaml_columns(self):
        yaml_columns = [
            {
                "source": "BIRTH_DATE",
                "target": "birth_date",
                "type": "DATE",
                "expression": {"type": "Date2Str", "pattern": "yyyy-MM-dd"},
            },
            {
                "target": "ingest_ts",
                "type": "TIMESTAMP",
                "expression": {"type": "LocalTs"},
            },
            {"source": "ID", "target": "id", "type": "INT"},
        ]
        target_columns_def = [
            {"id": 10, "name": "birth_date"},
            {"id": 11, "name": "ingest_ts"},
            {"id": 12, "name": "id"},
        ]
        ffs = build_field_functions_for_entity_type(
            yaml_columns, target_columns_def, table_or_object_id=55
        )
        self.assertEqual(len(ffs), 2)
        by_col = {ff["columnId"]: ff for ff in ffs}
        self.assertEqual(by_col[10]["tableOrObjectId"], 55)
        self.assertEqual(by_col[10]["expression"]["type"], "Date2Str")
        self.assertEqual(by_col[11]["expression"]["type"], "LocalTs")


class ExportTemplateIntegrationTests(unittest.TestCase):
    def test_process_single_entity_emits_column_expression(self):
        from export_template_from_corehub import _process_single_entity

        entity = {
            "entityName": "drivers_entity",
            "groupId": "_default",
            "columnsMappingMatrix": [
                {"sourceColumnId": 1, "targetColumnId": 11},
                {"sourceColumnId": 2, "targetColumnId": 12},
            ],
        }
        source_ae = {
            "type": "SingleTable",
            "entityType": {"type": "Source"},
            "table": {"id": "100", "name": "DRIVERS", "schema": "dbo"},
            "entityObject": {"id": "100", "schema": "dbo", "collection": "DRIVERS"},
            "columns": [
                {"id": 1, "name": "ID", "dataType": "int", "isPK": True, "ordinalPosition": 1},
                {"id": 2, "name": "BIRTH_DATE", "dataType": "date", "isPK": False, "ordinalPosition": 2},
            ],
            "keys": [{"id": 1, "name": "ID"}],
        }
        target_ae = {
            "type": "SingleTable",
            "entityType": {
                "type": "Target",
                "fieldFunctions": [
                    {
                        "columnId": 12,
                        "tableOrObjectId": 200,
                        "expression": {
                            "type": "Date2Str",
                            "pattern": "yyyy-MM-dd",
                            "isTechnicalField": False,
                            "requireUserInput": True,
                            "options": ["yyyy-MM-dd"],
                            "validationRegex": None,
                        },
                    }
                ],
            },
            "table": {"id": "200", "name": "drivers", "schema": "public"},
            "entityObject": {"id": "200", "schema": "public", "collection": "drivers"},
            "columns": [
                {"id": 11, "name": "id", "dataType": "int", "isPK": True, "ordinalPosition": 1},
                {"id": 12, "name": "birth_date", "dataType": "varchar", "isPK": False, "ordinalPosition": 2},
            ],
            "keys": [{"id": 11, "name": "id"}],
        }
        schemas = {}
        _process_single_entity(entity, source_ae, target_ae, schemas, {})
        table_cfg = schemas["dbo"]["tables"]["custom"]["DRIVERS"]
        birth = next(c for c in table_cfg["columns"] if c.get("target") == "birth_date")
        self.assertEqual(birth["expression"]["type"], "Date2Str")
        self.assertEqual(birth["expression"]["pattern"], "yyyy-MM-dd")
        self.assertNotIn("isTechnicalField", birth["expression"])
        self.assertNotIn("requireUserInput", birth["expression"])
        self.assertNotIn("options", birth["expression"])



if __name__ == "__main__":
    unittest.main()

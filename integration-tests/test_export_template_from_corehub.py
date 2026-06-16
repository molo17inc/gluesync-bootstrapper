#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FetchPipelineJobsTests(unittest.TestCase):
    def test_fetch_pipeline_jobs_accepts_paginated_items_shape(self):
        import export_template_from_corehub as export_mod

        jobs_payload = [
            {
                "id": 1,
                "name": "test scheduler",
                "task_type": "group_snapshot",
                "pipeline_id": "7ab21909",
                "group_ids": ["f67947cb"],
                "cron_expression": "10 10 * * 1",
                "enabled": True,
            }
        ]

        with mock.patch.object(
            export_mod.ChronosClient,
            "_request",
            return_value={"items": jobs_payload, "count": 1},
        ) as req_mock:
            jobs = export_mod.fetch_pipeline_jobs("7ab21909")

        self.assertEqual(jobs, jobs_payload)
        req_mock.assert_called_once_with(
            "api/jobs/",
            params={"pipeline_id": "7ab21909", "limit": 1000},
        )

    def test_fetch_pipeline_jobs_accepts_legacy_list_shape(self):
        import export_template_from_corehub as export_mod

        jobs_payload = [
            {
                "id": 2,
                "name": "legacy scheduler",
                "task_type": "entity_snapshot",
                "pipeline_id": "7ab21909",
                "entity_ids": ["abc123"],
                "cron_expression": "*/5 * * * *",
                "enabled": True,
            }
        ]

        with mock.patch.object(
            export_mod.ChronosClient,
            "_request",
            return_value=jobs_payload,
        ):
            jobs = export_mod.fetch_pipeline_jobs("7ab21909")

        self.assertEqual(jobs, jobs_payload)


class ExportFieldFunctionsTests(unittest.TestCase):
    def test_export_single_entity_with_field_functions(self):
        import export_template_from_corehub as export_mod

        ent = {
            "entityId": "ent-1",
            "entityName": "dbo.DRIVERS",
            "columnsMappingMatrix": [
                {"sourceColumnId": 1001, "targetColumnId": 1001},
                {"sourceColumnId": 1002, "targetColumnId": 1002}
            ],
            "groupId": "_default"
        }

        source_ae = {
            "type": "SingleTable",
            "table": {"schema": "dbo", "name": "DRIVERS"},
            "columns": [
                {"id": 1001, "name": "ID", "dataType": "int", "isPK": True},
                {"id": 1002, "name": "FIRST_NAME", "dataType": "varchar"}
            ]
        }

        target_ae = {
            "type": "SingleTable",
            "table": {"schema": "dbo", "name": "DRIVERS"},
            "columns": [
                {"id": 1001, "name": "ID", "dataType": "int", "isPK": True},
                {"id": 1002, "name": "FIRST_NAME", "dataType": "varchar"}
            ],
            "entityType": {
                "type": "Target",
                "fieldFunctions": [
                    {
                        "columnId": 1002,
                        "tableOrObjectId": 55555,
                        "expression": {"type": "Str2Sht"}
                    }
                ]
            }
        }

        schemas = {}
        group_id_to_name = {}

        export_mod._process_single_entity(ent, source_ae, target_ae, schemas, group_id_to_name)

        self.assertIn("dbo", schemas)
        drivers_cfg = schemas["dbo"]["tables"]["custom"]["DRIVERS"]
        self.assertIn("fieldFunctions", drivers_cfg)
        self.assertEqual(len(drivers_cfg["fieldFunctions"]), 1)
        self.assertEqual(drivers_cfg["fieldFunctions"][0]["column"], "FIRST_NAME")
        self.assertEqual(drivers_cfg["fieldFunctions"][0]["expression"]["type"], "Str2Sht")

    def test_export_multitable_entity_with_field_functions(self):
        import export_template_from_corehub as export_mod

        ent = {
            "entityId": "ent-2",
            "entityName": "dbo.ORDERS_HEADERS",
            "groupId": "_default"
        }

        source_ae = {
            "type": "MultiTable",
            "tables": [
                {"schema": "dbo", "name": "ORDERS_HEADERS"},
                {"schema": "dbo", "name": "ORDERS_ROWS"}
            ],
            "columns": [
                {"name": "ORDERS_HEADERS"},
                [
                    {"id": 2001, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 2002, "name": "ORDER_DATE", "dataType": "timestamp"}
                ],
                {"name": "ORDERS_ROWS"},
                [
                    {"id": 3001, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 3002, "name": "QUANTITY", "dataType": "int"}
                ]
            ]
        }

        target_ae = {
            "type": "MultiTable",
            "tables": [
                {"schema": "dbo", "name": "ORDERS_HEADERS", "id": 50001},
                {"schema": "dbo", "name": "ORDERS_ROWS", "id": 50002}
            ],
            "columns": [
                {"name": "ORDERS_HEADERS"},
                [
                    {"id": 2001, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 2002, "name": "ORDER_DATE", "dataType": "timestamp"}
                ],
                {"name": "ORDERS_ROWS"},
                [
                    {"id": 3001, "name": "ID", "dataType": "int", "isPK": True},
                    {"id": 3002, "name": "QUANTITY", "dataType": "int"}
                ]
            ],
            "entityType": {
                "type": "Target",
                "fieldFunctions": [
                    {
                        "columnId": 2002,
                        "tableOrObjectId": 50001,
                        "expression": {"type": "Str2DateTime", "pattern": "yyyy-MM-dd"}
                    }
                ]
            }
        }

        schemas = {}
        group_id_to_name = {}

        export_mod._process_multitable_entity(ent, source_ae, target_ae, schemas, group_id_to_name)

        self.assertIn("dbo", schemas)
        headers_cfg = schemas["dbo"]["tables"]["custom"]["ORDERS_HEADERS"]
        rows_cfg = schemas["dbo"]["tables"]["custom"]["ORDERS_ROWS"]

        # ORDERS_HEADERS should have the field function
        self.assertIn("fieldFunctions", headers_cfg)
        self.assertEqual(len(headers_cfg["fieldFunctions"]), 1)
        self.assertEqual(headers_cfg["fieldFunctions"][0]["column"], "ORDER_DATE")
        self.assertEqual(headers_cfg["fieldFunctions"][0]["expression"]["type"], "Str2DateTime")

        # ORDERS_ROWS should not have any field functions
        self.assertNotIn("fieldFunctions", rows_cfg)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

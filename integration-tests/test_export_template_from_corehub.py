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

    def test_chronos_url_answers_when_the_corehub_path_does_not(self):
        import export_template_from_corehub as export_mod

        jobs_payload = [{"id": 3, "name": "snap", "task_type": "entity_snapshot", "pipeline_id": "p1"}]
        env = {"CORE_HUB_URL": "http://gluesync-core-hub:1717", "CHRONOS_URL": "http://gluesync-chronos:1717"}
        with mock.patch.dict("os.environ", env), mock.patch.object(
            export_mod.ChronosClient, "_request", side_effect=[RuntimeError("404"), {"items": jobs_payload}],
        ) as req_mock:
            self.assertEqual(export_mod.read_pipeline_jobs("p1"), jobs_payload)
        self.assertEqual(req_mock.call_count, 2)

    def test_jobs_of_other_pipelines_are_dropped(self):
        import export_template_from_corehub as export_mod

        jobs_payload = [
            {"id": 1, "name": "snap", "pipeline_id": "other"},
            {"id": 2, "name": "snap", "pipeline_id": "p1"},
            {"id": 3, "name": "legacy row without pipeline_id"},
        ]
        with mock.patch.object(export_mod.ChronosClient, "_request", return_value={"items": jobs_payload}):
            self.assertEqual([j["id"] for j in export_mod.read_pipeline_jobs("p1")], [2, 3])

    def test_no_chronos_answering_is_none_and_exports_no_schedules(self):
        import export_template_from_corehub as export_mod

        env = {"CORE_HUB_URL": "http://gluesync-core-hub:1717", "CHRONOS_URL": "http://gluesync-chronos:1717"}
        with mock.patch.dict("os.environ", env), mock.patch.object(
            export_mod.ChronosClient, "_request", side_effect=RuntimeError("404"),
        ):
            self.assertIsNone(export_mod.read_pipeline_jobs("p1"))
            self.assertEqual(export_mod.fetch_pipeline_jobs("p1"), [])


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "roundtrip"


def _drivers_entity():
    """public.drivers as CoreHub 2.3 returned it in the pgsqlwal-mysql8 truncate kit."""
    import json

    return json.loads((FIXTURES / "pgsqlwal_mysql8_drivers_entity.json").read_text(encoding="utf-8"))


def _drivers_table_cfg(entity):
    import export_template_from_corehub as export_mod

    schemas = export_mod.build_schemas_from_entities([entity], {})
    return schemas["public"]["tables"]["custom"]["drivers"]


class ColumnOrderTests(unittest.TestCase):
    """Columns are exported in table order, so a restore keeps their positions."""

    def test_columns_follow_the_discovered_position_not_the_column_id(self):
        cfg = _drivers_table_cfg(_drivers_entity())
        self.assertEqual(
            [c["source"] for c in cfg["columns"]],
            ["id", "first_name", "last_name", "email", "gender", "driving_license_id",
             "driving_license_expire_date"],
        )
        self.assertEqual([c["ordinalPosition"] for c in cfg["columns"]], [1, 2, 3, 4, 5, 6, 7])

    def test_stored_order_is_the_fallback_when_columns_have_no_position(self):
        entity = _drivers_entity()
        for ae in entity["agentEntities"]:
            for col in ae["columns"]:
                col.pop("position", None)
        cfg = _drivers_table_cfg(entity)
        self.assertEqual([c["source"] for c in cfg["columns"]][:3], ["id", "first_name", "last_name"])


class SourceCustomPropertiesTests(unittest.TestCase):
    """The source settings CoreHub runs with are exported, user-only keys included."""

    def test_unchanged_data_filter_and_user_keys_are_exported(self):
        source = _drivers_table_cfg(_drivers_entity())["customProperties"]["source"]
        self.assertEqual(source["unchangedDataFilterType"], "FIELDS")
        self.assertEqual(source["maxItemsCountPerIteration"], 1000)
        self.assertEqual(source["maxFetchItemsCountPerIteration"], 10000)
        self.assertNotIn("type", source)

    def test_the_running_setting_wins_over_the_stored_custom_property(self):
        entity = _drivers_entity()
        source_ae = entity["agentEntities"][0]
        source_ae["entityType"]["unchangedDataFilterType"] = "ENTIRE_ROW"
        source = _drivers_table_cfg(entity)["customProperties"]["source"]
        self.assertEqual(source["unchangedDataFilterType"], "ENTIRE_ROW")

    def test_partition_settings_stay_out(self):
        entity = _drivers_entity()
        entity["agentEntities"][0]["entityType"]["partitionSettings"] = {"partitions": 4}
        source = _drivers_table_cfg(entity)["customProperties"]["source"]
        self.assertNotIn("partitionSettings", source)


if __name__ == "__main__":
    unittest.main()

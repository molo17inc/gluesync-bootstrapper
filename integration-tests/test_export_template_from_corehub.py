#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml


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


class TargetPositionTests(unittest.TestCase):
    """A row also carries the position of its target column, which the import restores."""

    def test_it_is_the_position_of_the_target_column_not_the_one_of_the_source_column(self):
        entity = _drivers_entity()
        target_ae = entity["agentEntities"][1]
        for position, col in enumerate(reversed(target_ae["columns"]), start=10):
            col["position"] = position
        cfg = _drivers_table_cfg(entity)
        self.assertEqual([c["targetPosition"] for c in cfg["columns"]], [16, 15, 14, 13, 12, 11, 10])
        self.assertEqual([c["ordinalPosition"] for c in cfg["columns"]], [1, 2, 3, 4, 5, 6, 7])

    def test_zero_is_exported_as_a_position_of_its_own(self):
        cfg = _drivers_table_cfg(_drivers_entity())  # the MySQL target of the kit reports 0 for every column
        self.assertEqual([c["targetPosition"] for c in cfg["columns"]], [0] * 7)

    def test_a_target_column_without_a_position_has_no_target_position(self):
        entity = _drivers_entity()
        for col in entity["agentEntities"][1]["columns"]:
            col.pop("position")
        cfg = _drivers_table_cfg(entity)
        self.assertTrue(all("targetPosition" not in c for c in cfg["columns"]))

    def test_target_position_comes_after_the_keys_a_row_has_always_had(self):
        row = _drivers_table_cfg(_drivers_entity())["columns"][0]
        self.assertEqual(
            list(row),
            ["source", "target", "type", "dataLength", "numericPrecision", "numericScale", "isNullable", "isPK",
             "id", "ordinalPosition", "charSet", "targetPosition", "targetIsNullable"],
        )

    def test_a_column_that_feeds_nothing_has_no_target_position(self):
        entity = _drivers_entity()
        target_ae = entity["agentEntities"][1]
        matrix = target_ae["entityType"]["columnsMappingMatrix"]
        entity["agentEntities"][1]["entityType"]["columnsMappingMatrix"] = matrix[1:]
        row = _drivers_table_cfg(entity)["columns"][0]
        self.assertEqual((row["source"], row["target"]), ("id", []))
        self.assertNotIn("targetPosition", row)


class TargetIsNullableTests(unittest.TestCase):
    """A row also carries the nullability of its target column, which the import restores."""

    def test_it_is_the_nullability_of_the_target_column_not_the_one_of_the_source_column(self):
        entity = _drivers_entity()
        source_ae, target_ae = entity["agentEntities"]
        for index, (scol, tcol) in enumerate(zip(source_ae["columns"], target_ae["columns"])):
            scol["isNullable"] = bool(index % 2)
            tcol["isNullable"] = not index % 2
        cfg = _drivers_table_cfg(entity)
        self.assertEqual([c["isNullable"] for c in cfg["columns"]], [False, True] * 3 + [False])
        self.assertEqual([c["targetIsNullable"] for c in cfg["columns"]], [True, False] * 3 + [True])

    def test_a_target_column_without_nullability_has_no_target_is_nullable(self):
        entity = _drivers_entity()
        for col in entity["agentEntities"][1]["columns"]:
            col.pop("isNullable", None)
        cfg = _drivers_table_cfg(entity)
        self.assertTrue(all("targetIsNullable" not in c for c in cfg["columns"]))

    def test_a_value_that_is_not_a_boolean_is_not_exported(self):
        entity = _drivers_entity()
        for col in entity["agentEntities"][1]["columns"]:
            col["isNullable"] = None
        cfg = _drivers_table_cfg(entity)
        self.assertTrue(all("targetIsNullable" not in c for c in cfg["columns"]))

    def test_a_column_that_feeds_nothing_has_no_target_is_nullable(self):
        entity = _drivers_entity()
        matrix = entity["agentEntities"][1]["entityType"]["columnsMappingMatrix"]
        entity["agentEntities"][1]["entityType"]["columnsMappingMatrix"] = matrix[1:]
        row = _drivers_table_cfg(entity)["columns"][0]
        self.assertEqual((row["source"], row["target"]), ("id", []))
        self.assertNotIn("targetIsNullable", row)


class OrdinalPositionTests(unittest.TestCase):
    """ordinalPosition is the place of the column in the table, whatever order the entity stores its columns in."""

    def test_an_entity_that_stores_its_columns_in_another_order_exports_the_same_ordinals(self):
        entity = _drivers_entity()
        expected = _drivers_table_cfg(entity)["columns"]
        shuffled = _drivers_entity()
        for ae in shuffled["agentEntities"]:
            ae["columns"] = ae["columns"][3:] + ae["columns"][:3]  # an entity built from a whitelist
        cfg = _drivers_table_cfg(shuffled)
        self.assertEqual(cfg["columns"], expected)
        self.assertEqual([c["ordinalPosition"] for c in cfg["columns"]], [1, 2, 3, 4, 5, 6, 7])


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


def _fixture_entity(name):
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _table_cfg(entity, table):
    import export_template_from_corehub as export_mod

    schemas = export_mod.build_schemas_from_entities([entity], {})
    (schema_cfg,) = schemas.values()
    return schema_cfg["tables"]["custom"][table]


class PartitionColumnTests(unittest.TestCase):
    """The logical partition column survives an export (mssql-ct TEST_LOGICAL_PARTITIONING, column ID, 10 ranges)."""

    TABLE = "TEST_LOGICAL_PARTITIONING"

    def _entity(self):
        return _fixture_entity("mssql_ct_logical_partitioning_entity.json")

    def _source(self, entity):
        return _table_cfg(entity, self.TABLE)["customProperties"]["source"]

    def test_the_column_is_exported_the_way_the_import_reads_it(self):
        import create_all_entities

        source = self._source(self._entity())
        self.assertEqual(source["partitions"], "ID")
        self.assertEqual(create_all_entities._parse_partition_config(source["partitions"]),
                         ("ID", create_all_entities.DEFAULT_MAX_LOGICAL_PARTITIONS))

    def test_the_computed_ranges_and_the_settings_are_not_exported(self):
        import yaml

        source = self._source(self._entity())
        self.assertNotIn("partitionSettings", source)
        text = yaml.safe_dump(_table_cfg(self._entity(), self.TABLE))
        for computed in ("startValue", "endValue", "100001"):
            self.assertNotIn(computed, text)

    def test_a_count_other_than_the_default_is_exported_as_the_extended_form(self):
        import create_all_entities

        entity = self._entity()
        settings = entity["agentEntities"][0]["entityType"]["partitionSettings"]
        settings["partitions"] = settings["partitions"][:4]
        partitions = self._source(entity)["partitions"]
        self.assertEqual(partitions, {"column": "ID", "maxPartitionsNumber": 4})
        self.assertEqual(create_all_entities._parse_partition_config(partitions), ("ID", 4))

    def test_a_column_with_no_ranges_yet_is_exported_as_the_column_name(self):
        entity = self._entity()
        entity["agentEntities"][0]["entityType"]["partitionSettings"]["partitions"] = []
        self.assertEqual(self._source(entity)["partitions"], "ID")

    def test_no_column_no_partitions(self):
        entity = self._entity()
        del entity["agentEntities"][0]["entityType"]["partitionSettings"]
        self.assertNotIn("partitions", self._source(entity))

    def test_a_table_without_partitions_exports_none(self):
        self.assertNotIn("partitions", _drivers_table_cfg(_drivers_entity())["customProperties"]["source"])


class CouchbaseDocumentKeyTests(unittest.TestCase):
    """The Couchbase document key is exported as column names (mssql-couchbase CUSTOM-DOCUMENT-ID, dbo.ARTICLES)."""

    def test_document_key_names_the_columns_of_its_ids(self):
        cfg = _table_cfg(_fixture_entity("mssql_couchbase_document_key_entity.json"), "ARTICLES")
        self.assertEqual(cfg["documentKey"],
                         {"prefix": "TESTPREFIX", "suffix": "TESTSUFFIX", "separator": "**",
                          "keys": ["ID", "ARTICLE_NAME"]})

    def test_a_table_without_a_key_mapping_exports_no_document_key(self):
        entity = _fixture_entity("mssql_couchbase_document_key_entity.json")
        del entity["agentEntities"][1]["keyMapping"]
        self.assertNotIn("documentKey", _table_cfg(entity, "ARTICLES"))


class NoSqlTargetExportTests(unittest.TestCase):
    """A NoSQL target entity exports the type of its columns and the settings that restore them."""

    def test_target_types_of_a_sql_source_are_exported(self):
        cfg = _table_cfg(_fixture_entity("mssql_cdc_couchbase_addresses_entity.json"), "ADDRESSES")
        types = {row["source"]: row["type"] for row in cfg["columns"]}
        self.assertEqual((types["ID"], types["STREET"], types["STREET_NUMBER"]), ("int", "string", "int"))
        self.assertNotIn("target", cfg["customProperties"])  # a Couchbase target with no TTL set has nothing to export

    def test_a_mongo_source_keeps_its_own_column_types_on_the_source_side(self):
        cfg = _table_cfg(_fixture_entity("mongo_couchbase_democollection_entity.json"), "democollection")
        types = {row["source"]: row["type"] for row in cfg["columns"]}
        self.assertEqual(types, {"_id": "string", "name": "string", "age": "int"})


class PipelineTypesTests(unittest.TestCase):
    """agents.json decides a side only when it lists that side's agentTag."""

    CATALOG = {"mssql-cdc": "SQL", "couchbase": "NoSQL", "mongodb": "NoSQL"}

    def _export_types(self, entity_file, source_tag, target_tag, config_error=None):
        import export_template_from_corehub as export_mod

        entity = _fixture_entity(entity_file)
        config = {
            "agents": [
                {"agentId": "a1", "agentTag": source_tag, "agentType": "SOURCE"},
                {"agentId": "a2", "agentTag": target_tag, "agentType": "TARGET"},
            ]
        }

        def fake_core_hub(path, token=None, **_kwargs):
            if path.endswith("/config") and config_error:
                raise config_error
            if path.endswith("/config"):
                return config
            if path.endswith("/entities"):
                return [entity]
            return []

        with mock.patch.object(export_mod, "fetch_core_hub", side_effect=fake_core_hub), \
                mock.patch.object(export_mod, "_load_agent_type_catalog", return_value=self.CATALOG), \
                mock.patch.object(export_mod, "fetch_pipeline_jobs", return_value=[]), \
                mock.patch.object(export_mod, "enrich_null_column_types_from_discovery", return_value=0), \
                mock.patch.object(export_mod, "build_export_header", return_value=""):
            data = yaml.safe_load(export_mod.render_pipeline_export("t", "p1"))
        schema = next(v for v in data.values() if isinstance(v, dict) and "targetType" in v)
        return schema["sourceType"], schema["targetType"]

    def test_an_unlisted_target_tag_keeps_the_type_of_the_entities(self):
        types = self._export_types("mssql_cdc_couchbase_addresses_entity.json", "mssql-cdc", "couchbase-dcp")
        self.assertEqual(types, ("SQL", "NoSQL"))

    def test_a_listed_tag_overrides_the_entities(self):
        # the entities say NoSQL on the target, the catalog says the agent is relational
        catalog = dict(self.CATALOG, **{"couchbase-dcp": "SQL"})
        with mock.patch.object(type(self), "CATALOG", catalog):
            types = self._export_types("mssql_cdc_couchbase_addresses_entity.json", "mssql-cdc", "couchbase-dcp")
        self.assertEqual(types, ("SQL", "SQL"))

    def test_an_unlisted_source_tag_keeps_the_type_of_the_entities(self):
        types = self._export_types("mongo_couchbase_democollection_entity.json", "mongodb-cdc", "couchbase")
        self.assertEqual(types, ("NoSQL", "NoSQL"))

    def test_a_failed_config_fetch_keeps_the_type_of_the_entities(self):
        types = self._export_types(
            "mssql_cdc_couchbase_addresses_entity.json", "mssql-cdc", "couchbase", config_error=RuntimeError("boom")
        )
        self.assertEqual(types, ("SQL", "NoSQL"))

    def test_the_catalog_answer_is_none_per_side_when_it_does_not_know_the_tag(self):
        import export_template_from_corehub as export_mod

        config = {"agents": [
            {"agentTag": "mssql-cdc", "agentType": "SOURCE"},
            {"agentTag": "couchbase-dcp", "agentType": "TARGET"},
        ]}
        with mock.patch.object(export_mod, "fetch_core_hub", return_value=config), \
                mock.patch.object(export_mod, "_load_agent_type_catalog", return_value=self.CATALOG):
            self.assertEqual(export_mod.infer_pipeline_schema_types_from_agents("t", "p1"), ("SQL", None))


if __name__ == "__main__":
    unittest.main()

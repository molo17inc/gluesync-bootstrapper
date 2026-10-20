#!/usr/bin/env python3
"""One source column fanned out onto several target columns (1:N) must give each target column its own id.

A target column discovery does not report took the id of its source column, so the two targets of a fan-out
(``{source: AFUSER, target: CREATED_BY}`` and ``{source: AFUSER, target: LAST_MODIFIED_BY}``) got the same id on a
target table that does not exist yet, and the restore failed on the column mapping of the entity. Every target column
takes its id from one place (``_TargetBuild.target_column_id``): a column keeps the id it got, a discovered column
keeps its own, and a new one takes the id of its source column unless another target column holds it.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import create_all_entities  # noqa: E402
import entity_mapping  # noqa: E402
from fake_corehub import column  # noqa: E402
from test_bootstrapper_entity_payloads_23 import matrix, run, sides, table  # noqa: E402

AFUSER_ID = 5528

SOURCE = {("src", "SAAFTR"): [
    column("ID", 1, "integer", is_pk=True, nullable=False),
    column("AFUSER", AFUSER_ID, "varchar"),
    column("NOTE", 3, "varchar"),
]}
# The same table with the fanned-out column as its primary key.
SOURCE_KEYED_ON_AFUSER = {("src", "SAAFTR"): [
    column("ID", 1, "integer"),
    column("AFUSER", AFUSER_ID, "varchar", is_pk=True, nullable=False),
    column("NOTE", 3, "varchar"),
]}

FAN_OUT = table("""        keys: [ID]
        name: SAAFTR
        columns:
          - {source: ID, target: ID}
          - {source: AFUSER, target: CREATED_BY}
          - {source: AFUSER, target: LAST_MODIFIED_BY}
          - {source: NOTE, target: NOTE}""", "SAAFTR")


def run_capturing(yaml_text, **kwargs):
    """``run``, plus what ``_assemble_target_side`` returned: ``(target_columns_def, target_keys)`` of each table."""
    captured = []
    real = create_all_entities._assemble_target_side

    def spy(*args, **kw):
        result = real(*args, **kw)
        captured.append(result)
        return result

    with mock.patch.object(create_all_entities, "_assemble_target_side", side_effect=spy):
        hub, result, entities = run(yaml_text, "SAAFTR", **kwargs)
    return hub, result, entities, captured


def ids_by_name(columns):
    return {c["name"]: c["id"] for c in columns}


def raw_matrix(entity):
    _, target = sides(entity)
    return target["entityType"]["columnsMappingMatrix"]


class FanOutTargetIdsTests(unittest.TestCase):
    def assert_fan_out_ids_are_distinct(self, entity):
        source, target = sides(entity)
        target_ids = [c["id"] for c in target["columns"]]
        self.assertEqual(len(target_ids), len(set(target_ids)), f"a target id is shared: {ids_by_name(target['columns'])}")
        by_name = ids_by_name(target["columns"])
        self.assertNotEqual(by_name["CREATED_BY"], by_name["LAST_MODIFIED_BY"])
        fanned = [e for e in raw_matrix(entity) if e["sourceColumnIds"] == [AFUSER_ID]]
        self.assertEqual(len(fanned), 1)
        self.assertCountEqual(fanned[0]["targetColumnIds"], [by_name["CREATED_BY"], by_name["LAST_MODIFIED_BY"]])
        self.assertEqual(len(fanned[0]["targetColumnIds"]), 2)

    def test_target_table_that_does_not_exist_yet(self):
        # The customer restore: discovery of the target returns no column at all.
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables={})
        self.assertEqual((result["failed"], result["successful"]), (0, 1), result.get("errors"))
        entity = entities["src.SAAFTR"]
        self.assert_fan_out_ids_are_distinct(entity)
        self.assertEqual(matrix(entity), [
            (["ID"], ["ID"]), (["NOTE"], ["NOTE"]), (["AFUSER"], ["CREATED_BY", "LAST_MODIFIED_BY"]),
        ])
        # the first target keeps the id of its source column, the second one takes a fresh one
        _, target = sides(entity)
        by_name = ids_by_name(target["columns"])
        self.assertEqual(by_name["CREATED_BY"], AFUSER_ID)
        self.assertEqual(by_name["LAST_MODIFIED_BY"], AFUSER_ID + 1)

    def test_discovered_target_columns_keep_their_ids(self):
        target_tables = {("tgt", "SAAFTR"): [
            column("ID", 201, "integer", is_pk=True, nullable=False),
            column("CREATED_BY", 202, "varchar"),
            column("LAST_MODIFIED_BY", 203, "varchar"),
            column("NOTE", 204, "varchar"),
        ]}
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables=target_tables)
        self.assertEqual(result["failed"], 0, result.get("errors"))
        entity = entities["src.SAAFTR"]
        self.assert_fan_out_ids_are_distinct(entity)
        _, target = sides(entity)
        self.assertEqual(ids_by_name(target["columns"]),
                         {"ID": 201, "CREATED_BY": 202, "LAST_MODIFIED_BY": 203, "NOTE": 204})

    def test_one_fanned_target_discovered_the_other_not(self):
        # The discovered CREATED_BY holds the id of the source column AFUSER, which the new column would take.
        target_tables = {("tgt", "SAAFTR"): [column("CREATED_BY", AFUSER_ID, "varchar")]}
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables=target_tables)
        self.assertEqual(result["failed"], 0, result.get("errors"))
        entity = entities["src.SAAFTR"]
        self.assert_fan_out_ids_are_distinct(entity)
        _, target = sides(entity)
        by_name = ids_by_name(target["columns"])
        self.assertEqual(by_name["CREATED_BY"], AFUSER_ID)
        self.assertNotIn(by_name["LAST_MODIFIED_BY"], {AFUSER_ID, by_name["ID"], by_name["NOTE"]})
        self.assertEqual(by_name["LAST_MODIFIED_BY"], max(v for k, v in by_name.items() if k != "LAST_MODIFIED_BY") + 1)

    def test_the_other_fanned_target_discovered(self):
        target_tables = {("tgt", "SAAFTR"): [column("LAST_MODIFIED_BY", AFUSER_ID, "varchar")]}
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables=target_tables)
        self.assertEqual(result["failed"], 0, result.get("errors"))
        self.assert_fan_out_ids_are_distinct(entities["src.SAAFTR"])
        _, target = sides(entities["src.SAAFTR"])
        self.assertEqual(ids_by_name(target["columns"])["LAST_MODIFIED_BY"], AFUSER_ID)

    def test_source_id_held_by_a_discovered_column_of_another_name_is_not_taken(self):
        # OTHER (a column of the target table no mapping names) holds 7, the id of the source column ID.
        source = {("src", "SAAFTR"): [column("ID", 7, "integer", is_pk=True, nullable=False),
                                      column("NOTE", 3, "varchar")]}
        target_tables = {("tgt", "SAAFTR"): [column("OTHER", 7, "varchar"), column("NOTE", 3, "varchar")]}
        yaml_text = table("""        keys: [ID]
        name: SAAFTR
        columns:
          - {source: ID, target: ID_COPY}
          - {source: NOTE, target: NOTE}""", "SAAFTR")
        _, result, entities = run(yaml_text, "SAAFTR", source_tables=source, target_tables=target_tables)
        self.assertEqual(result["failed"], 0, result.get("errors"))
        entity = entities["src.SAAFTR"]
        _, target = sides(entity)
        by_name = ids_by_name(target["columns"])
        self.assertNotEqual(by_name["ID_COPY"], 7)
        self.assertEqual(by_name["ID_COPY"], 8)  # above every id the target table holds
        self.assertEqual(by_name["NOTE"], 3)
        self.assertCountEqual(matrix(entity), [(["ID"], ["ID_COPY"]), (["NOTE"], ["NOTE"])])


class FanOutTargetKeyIdsTests(unittest.TestCase):
    YAML = table("""        keys: [AFUSER]
        targetKeys: [CREATED_BY]
        name: SAAFTR
        columns:
          - {source: ID, target: ID}
          - {source: AFUSER, target: CREATED_BY}
          - {source: AFUSER, target: LAST_MODIFIED_BY}
          - {source: NOTE, target: NOTE}""", "SAAFTR")

    def assert_keys_share_the_id_of_their_column(self, target_columns, target_keys):
        by_name = ids_by_name(target_columns)
        self.assertTrue(target_keys)
        for key in target_keys:
            self.assertEqual(key["id"], by_name[key["name"]], f"the key {key['name']} has an id of its own")
        self.assertEqual(len(by_name.values()), len(set(by_name.values())))
        self.assertIn("CREATED_BY", [k["name"] for k in target_keys])

    def test_target_key_of_a_fanned_out_source_key_on_a_sql_target(self):
        _, result, entities, captured = run_capturing(self.YAML, source_tables=SOURCE_KEYED_ON_AFUSER, target_tables={})
        self.assertEqual(result["failed"], 0, result.get("errors"))
        target_columns, target_keys = captured[0]
        self.assert_keys_share_the_id_of_their_column(target_columns, target_keys)
        _, target = sides(entities["src.SAAFTR"])
        self.assertEqual(ids_by_name(target["columns"]), ids_by_name(target_columns))

    def test_target_key_of_a_fanned_out_source_key_on_a_target_without_key_rules(self):
        # A NoSQL target builds its keys from the YAML pairs (_mapped_target_key), not from the key rules.
        _, result, entities, captured = run_capturing(self.YAML, source_tables=SOURCE_KEYED_ON_AFUSER,
                                                      target_tables={}, target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        target_columns, target_keys = captured[0]
        self.assertEqual({k["name"] for k in target_keys}, {"CREATED_BY", "LAST_MODIFIED_BY"})
        self.assert_keys_share_the_id_of_their_column(target_columns, target_keys)

    def test_target_key_ids_do_not_depend_on_the_discovery_of_the_target(self):
        target_tables = {("tgt", "SAAFTR"): [column("CREATED_BY", 202, "varchar"), column("ID", 201, "integer")]}
        _, result, entities, captured = run_capturing(self.YAML, source_tables=SOURCE_KEYED_ON_AFUSER,
                                                      target_tables=target_tables, target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        target_columns, target_keys = captured[0]
        self.assert_keys_share_the_id_of_their_column(target_columns, target_keys)
        self.assertEqual(ids_by_name(target_columns)["CREATED_BY"], 202)


class NoSqlTargetFanOutTests(unittest.TestCase):
    def test_fan_out_onto_a_nosql_target_that_does_not_exist_yet(self):
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables={}, target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        entity = entities["src.SAAFTR"]
        _, target = sides(entity)
        by_name = ids_by_name(target["columns"])
        self.assertEqual(len(set(by_name.values())), len(by_name), by_name)
        fanned = [e for e in raw_matrix(entity) if e["sourceColumnIds"] == [AFUSER_ID]]
        self.assertCountEqual(fanned[0]["targetColumnIds"], [by_name["CREATED_BY"], by_name["LAST_MODIFIED_BY"]])

    def test_fan_out_onto_a_nosql_target_that_exists(self):
        target_tables = {("tgt", "SAAFTR"): [column("CREATED_BY", 302, "string"), column("LAST_MODIFIED_BY", 303, "string")]}
        _, result, entities = run(FAN_OUT, "SAAFTR", source_tables=SOURCE, target_tables=target_tables,
                                  target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        _, target = sides(entities["src.SAAFTR"])
        by_name = ids_by_name(target["columns"])
        self.assertEqual((by_name["CREATED_BY"], by_name["LAST_MODIFIED_BY"]), (302, 303))
        self.assertEqual(len(set(by_name.values())), len(by_name), by_name)


class FreshTargetColumnIdTests(unittest.TestCase):
    fresh = staticmethod(create_all_entities._fresh_target_column_id)

    def test_above_the_highest_id_held(self):
        self.assertEqual(self.fresh({5, 9, -3}, "X", "T"), 10)
        self.assertEqual(self.fresh({-5, -3}, "X", "T"), 1)
        self.assertEqual(self.fresh(set(), "X", "T"), 1)

    def test_below_the_lowest_id_held_when_a_java_long_would_overflow(self):
        long_max, long_min = 2 ** 63 - 1, -2 ** 63
        self.assertEqual(self.fresh({long_max, 5}, "X", "T"), 4)
        self.assertEqual(self.fresh({long_max - 1}, "X", "T"), long_max)
        self.assertEqual(self.fresh({long_max, long_min + 1}, "X", "T"), long_min)

    def test_refused_when_no_id_is_left(self):
        with self.assertRaises(entity_mapping.EntityMappingError):
            self.fresh({2 ** 63 - 1, -2 ** 63}, "X", "T")


class TargetColumnsSharingAnIdTests(unittest.TestCase):
    SOURCE_COLUMNS = [{"id": 1, "name": "ID", "position": 1}, {"id": 2, "name": "AFUSER", "position": 2}]

    def test_two_target_columns_of_different_names_with_one_id_are_refused_naming_both(self):
        targets = [{"id": 10, "name": "ID", "position": 1}, {"id": 11, "name": "CREATED_BY", "position": 2},
                   {"id": 11, "name": "LAST_MODIFIED_BY", "position": 3}]
        relations = [{"sources": [1], "targets": [10]}, {"sources": [2], "targets": [11]}]
        with self.assertRaises(entity_mapping.EntityMappingError) as raised:
            entity_mapping.build_entries(100, 200, relations, self.SOURCE_COLUMNS, targets, grouped=True,
                                         label="SAAFTR")
        message = str(raised.exception)
        for part in ("CREATED_BY", "LAST_MODIFIED_BY", "SAAFTR", "11", "its own id"):
            self.assertIn(part, message)
        self.assertNotIn("more than once", message)

    def test_columns_that_differ_by_case_only_are_two_columns_and_need_two_ids(self):
        targets = [{"id": 10, "name": "id", "position": 1}, {"id": 10, "name": "ID", "position": 2}]
        with self.assertRaises(entity_mapping.EntityMappingError) as raised:
            entity_mapping.build_entries(100, 200, [], self.SOURCE_COLUMNS, targets, grouped=True, label="T")
        self.assertIn("share the id 10", str(raised.exception))

    def test_distinct_ids_are_accepted(self):
        targets = [{"id": 10, "name": "ID", "position": 1}, {"id": 11, "name": "CREATED_BY", "position": 2},
                   {"id": 12, "name": "LAST_MODIFIED_BY", "position": 3}]
        relations = [{"sources": [1], "targets": [10]}, {"sources": [2], "targets": [11, 12]}]
        entries = entity_mapping.build_entries(100, 200, relations, self.SOURCE_COLUMNS, targets, grouped=True)
        self.assertEqual([(s, t) for _, _, s, t in entries], [([1], [10]), ([2], [11, 12])])

    def test_a_pre_gluesync_23_payload_is_not_checked(self):
        targets = [{"id": 11, "name": "CREATED_BY"}, {"id": 11, "name": "LAST_MODIFIED_BY"}]
        entries = entity_mapping.build_entries(100, 200, [{"sources": [2], "targets": [11]}], self.SOURCE_COLUMNS,
                                               targets, grouped=False)
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()

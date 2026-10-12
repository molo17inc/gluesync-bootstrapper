#!/usr/bin/env python3
"""Demo-kit shapes 2.8.0-alpha.1 refused on a Gluesync 2.3 CoreHub, and their payload on older CoreHubs."""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_corehub import column  # noqa: E402
from test_bootstrapper_entity_payloads_23 import V23, keyed, matrix, run, sides, table  # noqa: E402

V22 = "2.2.11.5 - build test"

# Cosmos DB: the document id `id` next to a document field `ID` (cosmosdb-mongodb-integration-test).
COSMOS = {("src", "drivers"): [column("FIRST_NAME", 1, "string"), column("ID", 2, "string"),
                               column("_ts", 3, "long"), column("id", 4, "string")]}
COSMOS_TARGET_WITH_BOTH = {("tgt", "drivers"): [column("_id", 100, "string"), column("ID", 101, "string"),
                                                 column("FIRST_NAME", 102, "string"), column("id", 103, "string")]}
# A whitelist row the source table lacks (pgsqlwal-pgsqlwal, yugabytedb-cockroachdb test_create_table).
PG_SOURCE = {("src", "test_create_table"): [column("id", 1, "integer", is_pk=True, nullable=False),
                                            column("tn", 2, "smallint"), column("image", 3, "bytea")]}
PG_TARGET = {("tgt", "test_create_table"): [column("id", 101, "integer", is_pk=True, nullable=False),
                                            column("tn", 102, "smallint"), column("json", 103, "varchar"),
                                            column("image", 104, "bytea")]}
PG_YAML = table("""        keys: [id]
        name: test_create_table
        columns:
          - {name: id, type: int, isNullable: false}
          - {name: tn, type: smallint, isNullable: false}
          - {name: json, type: varchar, dataLength: 65000, isNullable: false}
          - {name: image, type: bytea, isNullable: false}""", "test_create_table")


class CaseCollidingColumnsTests(unittest.TestCase):
    def test_columns_that_differ_by_case_only_stay_two_relations(self):
        _, result, entities = run(table("        keys: [ID]", "drivers"), "drivers", source_tables=COSMOS,
                                  target_tables={}, source_type="NoSQL", target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        self.assertEqual(matrix(entities["src.drivers"]),
                         [(["FIRST_NAME"], ["FIRST_NAME"]), (["ID"], ["ID"]), (["_ts"], ["_ts"]), (["id"], ["id"])])

    def test_discovered_target_columns_are_matched_by_exact_spelling_first(self):
        _, result, entities = run(table("        keys: [ID]", "drivers"), "drivers", source_tables=COSMOS,
                                  target_tables=COSMOS_TARGET_WITH_BOTH, source_type="NoSQL", target_type="NoSQL")
        self.assertEqual(result["failed"], 0, result.get("errors"))
        _, target = sides(entities["src.drivers"])
        by_name = {c["name"]: c["id"] for c in target["columns"]}
        self.assertEqual((by_name["ID"], by_name["id"]), (101, 103))
        self.assertIn((["ID"], ["ID"]), matrix(entities["src.drivers"]))
        self.assertIn((["id"], ["id"]), matrix(entities["src.drivers"]))

    def test_older_corehub_payload_keeps_two_pairs(self):
        hub, result, _ = run(table("        keys: [ID]", "drivers"), "drivers", version=V22, source_tables=COSMOS,
                             target_tables={}, source_type="NoSQL", target_type="NoSQL")
        self.assertEqual(result["failed"], 0)
        pairs = [(e["sourceColumnId"], e["targetColumnId"])
                 for put in hub.puts for ent in put["entities"]
                 for e in ent["agentEntities"][1]["entityType"]["columnsMappingMatrix"]]
        self.assertEqual(pairs, [(1, 1), (2, 2), (3, 3), (4, 4)])


class WhitelistRowTheSourceLacksTests(unittest.TestCase):
    def test_row_is_left_out_with_a_warning_as_before_2_3(self):
        _, result, entities = run(PG_YAML, "test_create_table", source_tables=PG_SOURCE, target_tables=PG_TARGET)
        self.assertEqual(result["failed"], 0, result.get("errors"))
        source, target = sides(entities["src.test_create_table"])
        self.assertEqual(matrix(entities["src.test_create_table"]),
                         [(["id"], ["id"]), (["tn"], ["tn"]), (["image"], ["image"])])
        self.assertNotIn("json", [c["name"] for c in source["columns"] + target["columns"]])
        self.assertEqual((keyed(source["columns"]), keyed(target["columns"])), (["id"], ["id"]))

    def test_target_table_not_discovered_yet(self):
        _, result, entities = run(PG_YAML, "test_create_table", source_tables=PG_SOURCE, target_tables={})
        self.assertEqual(result["failed"], 0, result.get("errors"))
        self.assertEqual(len(matrix(entities["src.test_create_table"])), 3)

    def test_mapping_row_the_source_lacks_is_still_refused(self):
        yaml_text = table("""        keys: [id]
        columns:
          - id: id
          - json: json""", "test_create_table")
        _, result, entities = run(yaml_text, "test_create_table", source_tables=PG_SOURCE, target_tables=PG_TARGET)
        self.assertEqual((result["failed"], entities), (1, {}))
        self.assertRegex(result["errors"][0], "json")


if __name__ == "__main__":
    unittest.main()

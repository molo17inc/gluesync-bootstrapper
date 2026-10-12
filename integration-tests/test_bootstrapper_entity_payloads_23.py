#!/usr/bin/env python3
"""SingleTable entity payloads for a Gluesync 2.3 CoreHub (T8 of the alignment plan).

A 2.3 CoreHub treats every entity the bootstrapper saves as new, so its whole mapping stage runs
on it: the matrix rules M1-M7, the producer rows and the key rules B1-B7. These tests check that
the payload is the one that stage accepts (grouped lists, every checked column in one entry, keys
derived by the R12 rules) and that what it would refuse is refused before any request, naming the
table. The older payloads are pinned by test_bootstrapper_payload_golden.
"""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

V23 = "2.3.0.0 - build test"


def run(yaml_text, *tables, version=V23, source_tables=SOURCE_TABLES, target_tables=TARGET_TABLES,
        source_type="RDBMS", target_type="RDBMS", skip_errors=True):
    hub = FakeCoreHub(
        source_tables, target_tables, version=version,
        source_category="NoSQL" if source_type == "NoSQL" else "RDBMS",
        target_category="NoSQL" if target_type == "NoSQL" else "RDBMS",
    )
    result = run_create_entities(hub, yaml_text, _source_list(*tables), source_type=source_type,
                                 target_type=target_type, skip_errors=skip_errors)
    entities = {entity["entityName"]: entity for put in hub.puts for entity in put["entities"]}
    return hub, result, entities


def sides(entity):
    source, target = entity["agentEntities"]
    return source, target


def matrix(entity):
    source, target = sides(entity)
    src_names = {c["id"]: c["name"] for c in source["columns"]}
    tgt_names = {c["id"]: c["name"] for c in target["columns"]}
    return [
        ([src_names[i] for i in e["sourceColumnIds"]], [tgt_names[i] for i in e["targetColumnIds"]])
        for e in target["entityType"]["columnsMappingMatrix"]
    ]


def keyed(columns):
    return [c["name"] for c in columns if c.get("isPK")]


def table(yaml_body, name):
    return f"""
src:
  target: tgt
  tables:
    whitelist: [{name}]
    custom:
      {name}:
{yaml_body}
"""


class GroupedPayloadTests(unittest.TestCase):
    maxDiff = None

    def test_discovery_only_table_without_target_writes_lists_and_inherits_the_key(self):
        _, result, entities = run(table("        {}", "PARTS"), "PARTS")
        entity = entities["src.PARTS"]
        source, target = sides(entity)
        for entry in target["entityType"]["columnsMappingMatrix"]:
            self.assertNotIn("sourceColumnId", entry)
            self.assertNotIn("targetColumnId", entry)
        self.assertEqual(matrix(entity), [(["PART_ID"], ["PART_ID"]), (["LABEL"], ["LABEL"])])
        self.assertEqual((keyed(source["columns"]), keyed(target["columns"])), (["PART_ID"], ["PART_ID"]))
        self.assertEqual(result["successful"], 1)

    def test_renames_and_a_produced_column(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - NAME: name
          - EMAIL: email_address
          - target: ingest_ts
            type: timestamp
            expression:
              type: LocalTs""", "CUSTOMERS")
        _, _, entities = run(yaml_text, "CUSTOMERS")
        entity = entities["src.CUSTOMERS"]
        source, target = sides(entity)
        self.assertEqual(matrix(entity), [
            (["ID"], ["id"]), (["NAME"], ["name"]), (["EMAIL"], ["email_address"]), ([], ["ingest_ts"]),
        ])
        ingest = next(c for c in target["columns"] if c["name"] == "ingest_ts")
        self.assertEqual(ingest["id"], 105)
        self.assertEqual(target["entityType"]["fieldFunctions"],
                         [{"columnId": 105, "tableOrObjectId": int(target["table"]["id"]), "expression": {"type": "LocalTs"}}])
        self.assertEqual((keyed(source["columns"]), keyed(target["columns"])), (["ID"], ["id"]))

    def test_yaml_target_name_takes_the_discovered_spelling(self):
        # BOP-15 A: the key rules compare names case-sensitively, so ID: ID against a physical "id".
        _, _, entities = run(table("""        name: customers
        columns:
          - ID: ID
          - NAME: NAME""", "CUSTOMERS"), "CUSTOMERS")
        _, target = sides(entities["src.CUSTOMERS"])
        self.assertEqual([c["name"] for c in target["columns"]], ["id", "name"])
        self.assertEqual(keyed(target["columns"]), ["id"])

    def test_n_to_one_and_one_to_n(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - source: [NAME, EMAIL]
            target: name
            expression:
              type: CustomFn
              functionName: concat
              arguments:
                - {type: SourceColumnArg, name: NAME}
                - {type: SourceColumnArg, name: EMAIL}
          - source: CREATED
            target: created
          - source: CREATED
            target: email_address""", "CUSTOMERS")
        _, _, entities = run(yaml_text, "CUSTOMERS")
        self.assertEqual(matrix(entities["src.CUSTOMERS"]), [
            (["ID"], ["id"]), (["NAME", "EMAIL"], ["name"]), (["CREATED"], ["email_address", "created"]),
        ])

    def test_one_to_null_source_is_read_and_never_written(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - source: NAME
            target: []""", "CUSTOMERS")
        _, _, entities = run(yaml_text, "CUSTOMERS")
        source, target = sides(entities["src.CUSTOMERS"])
        self.assertEqual(matrix(entities["src.CUSTOMERS"]), [(["ID"], ["id"]), (["NAME"], [])])
        self.assertNotIn("NAME", [c["name"] for c in target["columns"]])

    def test_whitelist_rows_with_yaml_keys_on_tables_without_physical_key(self):
        yaml_text = table("""        keys: [CODE]
        columns:
          - {name: code, sourceName: CODE, type: varchar}
          - {name: descr, sourceName: DESCR, type: varchar}""", "NOPK")
        _, _, entities = run(yaml_text, "NOPK")
        source, target = sides(entities["src.NOPK"])
        self.assertEqual(matrix(entities["src.NOPK"]), [(["CODE"], ["code"]), (["DESCR"], ["descr"])])
        self.assertEqual((keyed(source["columns"]), keyed(target["columns"])), (["CODE"], ["code"]))

    def test_target_key_of_a_udf_entity_is_produced_by_the_udf(self):
        yaml_text = table("""        name: ORDERS_T
        columns:
          - {source: ORDER_ID, target: []}
          - {source: CUSTOMER_ID, target: CUSTOMER_ID}
        customProperties:
          target:
            udf:
              - {name: UDF_ORDERS, type: Java}""", "ORDERS")
        _, _, entities = run(yaml_text, "ORDERS")
        source, target = sides(entities["src.ORDERS"])
        self.assertIn("ROW_ID", [c["name"] for c in target["columns"]])
        self.assertEqual(keyed(target["columns"]), ["ROW_ID"])
        self.assertEqual(keyed(source["columns"]), ["ORDER_ID"])
        self.assertIn(([], ["ROW_ID"]), matrix(entities["src.ORDERS"]))


class RefusedBeforeTheRequestTests(unittest.TestCase):
    def assert_refused(self, yaml_text, table_name, pattern, **kwargs):
        _, result, entities = run(yaml_text, table_name, **kwargs)
        self.assertEqual(entities, {}, "the refused entity must not be sent")
        self.assertEqual(result["failed"], 1)
        with self.assertRaisesRegex(Exception, pattern):
            run(yaml_text, table_name, skip_errors=False, **kwargs)

    def test_explicit_source_key_onto_a_non_key_target_column(self):
        yaml_text = table("""        name: ORDERS_T
        columns:
          - {source: ORDER_ID, target: ORDER_ID}
          - {source: CUSTOMER_ID, target: CUSTOMER_ID}
          - {target: ROW_ID, type: bigint, expression: {type: RecordID}}""", "ORDERS")
        self.assert_refused(yaml_text, "ORDERS", r"R12\.8.*ORDER_ID")

    def test_target_primary_key_no_source_feeds(self):
        yaml_text = table("""        name: ORDERS_T
        columns:
          - {source: ORDER_ID, target: []}
          - {source: CUSTOMER_ID, target: CUSTOMER_ID}""", "ORDERS")
        self.assert_refused(yaml_text, "ORDERS", r"ROW_ID.*expression")

    def test_fixed_value_on_a_fed_target(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - source: NAME
            target: name
            expression: {type: StaticStr, value: "x"}""", "CUSTOMERS")
        self.assert_refused(yaml_text, "CUSTOMERS", r"StaticStr.*name.*source")

    def test_yaml_keys_that_are_not_the_primary_key(self):
        yaml_text = table("""        name: customers
        keys: [ID, NAME]
        columns:
          - ID: id
          - NAME: name""", "CUSTOMERS")
        self.assert_refused(yaml_text, "CUSTOMERS", r"R12\.1")

    def test_two_rows_on_one_target(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - NAME: name
          - EMAIL: name""", "CUSTOMERS")
        self.assert_refused(yaml_text, "CUSTOMERS", r"name.*several rows")

    def test_yaml_source_discovery_does_not_find(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - MISSING_COL: name""", "CUSTOMERS")
        self.assert_refused(yaml_text, "CUSTOMERS", r"MISSING_COL")

    def test_mongodb_source_without_keys(self):
        mongo = {("src", "people"): [column("_id", 1, "objectId"), column("name", 2)]}
        yaml_text = table("        {}", "people")
        self.assert_refused(yaml_text, "people", r"_id", source_tables=mongo, target_tables={}, source_type="NoSQL")

    def test_grouped_forms_on_an_older_corehub(self):
        yaml_text = table("""        name: customers
        columns:
          - ID: id
          - source: [NAME, EMAIL]
            target: name""", "CUSTOMERS")
        self.assert_refused(yaml_text, "CUSTOMERS", r"older than Gluesync 2\.3.*NAME, EMAIL",
                            version="2.2.11.5 - build test")


if __name__ == "__main__":
    unittest.main()

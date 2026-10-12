#!/usr/bin/env python3
"""MultiTable entity payloads for a Gluesync 2.3 CoreHub (T10 of the alignment plan).

Each table pair of a chain gets grouped entries naming every column of the entity: the filtered
source columns are read and never written (1:null), and a ``targetOnlyColumns`` entry is a column
fed by no source, which 2.3 accepts only with a field function that produces it (P12, BOP-12 A).
"""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from entity_mapping import read_entry  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

CHAIN = """
src:
  target: tgt
  tables:
    whitelist: [ORDERS, PARTS]
    custom:
      ORDERS:
        chainId: chain1
        columns:
          - {{source: ORDER_ID, target: ORDER_ID}}
          - {{source: TOTAL, target: AMOUNT}}
        customProperties:
          target:
            targetOnlyColumns:
              - {target_only}
      PARTS:
        chainId: chain1
"""

WITH_EXPRESSION = "{name: SYNCED_AT, type: timestamp, dataLength: 0, numericPrecision: 0, numericScale: 0, isNullable: true, expression: {type: LocalTs}}"


def run(target_only, version="2.3.0.0 - build test"):
    hub = FakeCoreHub(SOURCE_TABLES, {}, version=version)
    result = run_create_entities(hub, CHAIN.format(target_only=target_only), _source_list("ORDERS", "PARTS"))
    multi = [e for put in hub.puts for e in put["entities"] if e["agentEntities"][0]["type"] == "MultiTable"]
    return result, multi


def entries_by_table(entity):
    source, target = entity["agentEntities"]

    def names(columns):
        by_table, current = {}, None
        for item in columns:
            if isinstance(item, dict):
                current = item["id"]
            else:
                by_table[current] = {c["id"]: c["name"] for c in item}
        return by_table

    src, tgt = names(source["columns"]), names(target["columns"])
    out = {}
    for entry in target["entityType"]["columnsMappingMatrix"]:
        s_table, t_table = entry["sourceTableObjectId"], entry["targetTableObjectId"]
        out.setdefault(s_table, []).append((
            [src[s_table][i] for i in entry["sourceColumnIds"]],
            [tgt[t_table][i] for i in entry["targetColumnIds"]],
        ))
    return out


class MultiTableGroupedTests(unittest.TestCase):
    maxDiff = None

    def test_filtered_chain_with_a_produced_target_only_column(self):
        _, multi = run(WITH_EXPRESSION)
        self.assertEqual(len(multi), 1)
        entity = multi[0]
        self.assertNotIn("columnsMappingMatrix", entity)
        by_table = entries_by_table(entity)
        self.assertEqual(by_table[500], [
            (["ORDER_ID"], ["ORDER_ID"]),
            (["TOTAL"], ["AMOUNT"]),
            (["CUSTOMER_ID"], []),
            (["NOTE"], []),
            ([], ["SYNCED_AT"]),
        ])
        self.assertEqual(by_table[501], [(["PART_ID"], ["PART_ID"]), (["LABEL"], ["LABEL"])])
        target = entity["agentEntities"][1]
        synced = [c for item in target["columns"] if isinstance(item, list) for c in item if c["name"] == "SYNCED_AT"][0]
        self.assertEqual(
            [(ff["columnId"], ff["expression"]) for ff in target["entityType"]["fieldFunctions"]],
            [(synced["id"], {"type": "LocalTs"})],
        )

    def test_target_only_column_without_expression_is_refused(self):
        result, multi = run("SYNCED_AT")
        self.assertEqual(multi, [])
        self.assertGreaterEqual(result["failed"], 1)

    def test_a_malformed_row_refuses_the_chain_instead_of_stopping_the_run(self):
        hub = FakeCoreHub(SOURCE_TABLES, {}, version="2.3.0.0 - build test")
        yaml_text = CHAIN.format(target_only=WITH_EXPRESSION).replace(
            "          - {source: TOTAL, target: AMOUNT}", "          - {source: [TOTAL, NOTE]}")
        result = run_create_entities(hub, yaml_text, _source_list("ORDERS", "PARTS"))
        self.assertEqual([e for put in hub.puts for e in put["entities"] if e["agentEntities"][0]["type"] == "MultiTable"], [])
        self.assertGreaterEqual(result["failed"], 1)

    def test_older_corehub_keeps_the_pairs_and_takes_no_field_function(self):
        _, multi = run("SYNCED_AT", version="2.2.11.5")
        target = multi[0]["agentEntities"][1]
        self.assertTrue(all("sourceColumnId" in e for e in target["entityType"]["columnsMappingMatrix"]))
        self.assertNotIn("fieldFunctions", target["entityType"])



class ChainedTablesAreNotSentAsSingleTableTests(unittest.TestCase):
    """A table of a chain goes into its MultiTable entity only, whatever its entityName.

    The chained tables were filtered out of the SingleTable entities by parsing the entity name
    (``schema.TABLE``), so a chained table with another entityName (the export writes the MultiTable
    entity name on every table of the chain) was also sent as a SingleTable entity.
    """

    def run_yaml(self, yaml_text, *tables):
        hub = FakeCoreHub(SOURCE_TABLES, {}, version="2.2.11.5")
        run_create_entities(hub, yaml_text, _source_list(*tables))
        return [(e["agentEntities"][0]["type"], e["entityName"]) for put in hub.puts for e in put["entities"]]

    def test_chained_table_with_a_custom_entity_name_is_sent_once(self):
        yaml_text = """
src:
  target: tgt
  tables:
    whitelist: [ORDERS, PARTS, CUSTOMERS]
    custom:
      ORDERS:
        chainId: chain1
        entityName: src.ORDERS chain
      PARTS:
        chainId: chain1
        entityName: src.ORDERS chain
      CUSTOMERS: {}
"""
        self.assertEqual(sorted(self.run_yaml(yaml_text, "ORDERS", "PARTS", "CUSTOMERS")), [
            ("MultiTable", "src.ORDERS chain"),
            ("SingleTable", "src.CUSTOMERS"),
        ])


class ChainsBelongToTheirSchemaTests(unittest.TestCase):
    """A chain declared under one schema is not built while another schema is processed.

    create_entities runs once per source schema but collected the chains of every schema of the YAML,
    so a chain of another schema was built from the current schema's tables whenever their names
    passed the current whitelist.
    """

    def test_a_table_named_like_a_chain_table_of_another_schema_stays_a_single_table(self):
        yaml_text = """
other:
  target: tgt
  tables:
    custom:
      CUSTOMERS:
        chainId: chain_other
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS: {}
"""
        hub = FakeCoreHub(SOURCE_TABLES, {}, version="2.2.11.5")
        run_create_entities(hub, yaml_text, _source_list("CUSTOMERS"))
        sent = [(e["agentEntities"][0]["type"], e["entityName"]) for put in hub.puts for e in put["entities"]]
        self.assertEqual(sent, [("SingleTable", "src.CUSTOMERS")])

    def test_the_chain_of_the_current_schema_is_still_built(self):
        yaml_text = """
src:
  target: tgt
  tables:
    whitelist: [ORDERS, PARTS]
    custom:
      ORDERS:
        chainId: chain1
      PARTS:
        chainId: chain1
other:
  target: tgt
  tables:
    custom:
      ORDERS:
        chainId: chain1
"""
        hub = FakeCoreHub(SOURCE_TABLES, {}, version="2.2.11.5")
        run_create_entities(hub, yaml_text, _source_list("ORDERS", "PARTS"))
        multi = [e for put in hub.puts for e in put["entities"] if e["agentEntities"][0]["type"] == "MultiTable"]
        self.assertEqual(len(multi), 1)
        self.assertEqual([t["name"] for t in multi[0]["agentEntities"][0]["tables"]], ["ORDERS", "PARTS"])


V23 = "2.3.0.0 - build test"

# The relation shapes of the SingleTable tests (T8, T14), each on its own table pair.
SHAPES = {
    "n_to_one_one_to_n_and_a_produced_column": ("CUSTOMERS", """name: customers
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
    target: email_address
  - target: ingest_ts
    type: timestamp
    expression:
      type: LocalTs"""),
    "one_to_null_and_renames": ("CUSTOMERS", """name: customers
columns:
  - ID: id
  - NAME: email_address
  - source: EMAIL
    target: []"""),
    "target_with_its_own_key_produced_by_a_function": ("ORDERS", """name: ORDERS_T
columns:
  - {source: ORDER_ID, target: []}
  - {source: CUSTOMER_ID, target: CUSTOMER_ID}
  - {source: TOTAL, target: TOTAL}
  - {target: ROW_ID, type: bigint, expression: {type: RecordID}}"""),
    "discovery_only": ("CUSTOMERS", "name: customers"),
}


def yaml_of(table_name, body, chained):
    """A YAML with ``table_name`` (and PARTS, in the same chain, when ``chained``)."""
    lines = ["src:", "  target: tgt", "  tables:",
             f"    whitelist: [{table_name}{', PARTS' if chained else ''}]", "    custom:", f"      {table_name}:"]
    if chained:
        lines.append("        chainId: c1")
    lines += [f"        {line}" for line in body.splitlines()]
    if chained:
        lines += ["      PARTS:", "        chainId: c1"]
    return "\n".join(lines) + "\n"


def run_23(table_name, body, chained, version=V23):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=version)
    tables = (table_name, "PARTS") if chained else (table_name,)
    result = run_create_entities(hub, yaml_of(table_name, body, chained), _source_list(*tables))
    return result, [e for put in hub.puts for e in put["entities"]]


def single_view(table_name, body):
    """The relations, keys and field functions of the SingleTable entity of ``table_name``, by name."""
    result, entities = run_23(table_name, body, chained=False)
    assert result["failed"] == 0, result
    source, target = entities[0]["agentEntities"]
    src = {c["id"]: c["name"] for c in source["columns"]}
    tgt = {c["id"]: c["name"] for c in target["columns"]}
    return {
        "entries": [([src[i] for i in e["sourceColumnIds"]], [tgt[i] for i in e["targetColumnIds"]])
                    for e in target["entityType"]["columnsMappingMatrix"]],
        "source_keys": [c["name"] for c in source["columns"] if c.get("isPK")],
        "target_columns": [(c["name"], bool(c.get("isPK"))) for c in target["columns"]],
        "field_functions": sorted((tgt[ff["columnId"]], repr(ff["expression"]))
                                  for ff in target["entityType"].get("fieldFunctions", [])),
    }


def chain_view(table_name, body, version=V23):
    """The same view of the pair of ``table_name`` in its MultiTable entity, with the target table it names."""
    result, entities = run_23(table_name, body, chained=True, version=version)
    assert result["failed"] == 0, result
    source, target = entities[0]["agentEntities"]
    source_table = next(t for t in source["tables"] if t["name"] == table_name)
    index = source["tables"].index(source_table)
    target_table = target["tables"][index]

    def pair_columns(columns, table_id):
        for position, item in enumerate(columns):
            if isinstance(item, dict) and int(item["id"]) == int(table_id):
                return columns[position + 1]
        raise AssertionError(f"no columns for table {table_id}")

    src_cols = pair_columns(source["columns"], source_table["id"])
    tgt_cols = pair_columns(target["columns"], target_table["id"])
    src = {c["id"]: c["name"] for c in src_cols}
    tgt = {c["id"]: c["name"] for c in tgt_cols}
    pair_entries = [read_entry(e) for e in target["entityType"]["columnsMappingMatrix"]]
    return target_table["name"], {
        "entries": [([src[i] for i in sources], [tgt[i] for i in targets])
                    for source_id, _, sources, targets in pair_entries if source_id == source_table["id"]],
        "source_keys": [c["name"] for c in src_cols if c.get("isPK")],
        "target_columns": [(c["name"], bool(c.get("isPK"))) for c in tgt_cols],
        "field_functions": sorted((tgt[ff["columnId"]], repr(ff["expression"]))
                                  for ff in target["entityType"].get("fieldFunctions", [])
                                  if int(ff["tableOrObjectId"]) == int(target_table["id"])),
    }


class ChainPairsFollowTheSingleTableRulesTests(unittest.TestCase):
    """BOP-16 A: on 2.3 a table pair of a chain is written as the same table's SingleTable entity.

    The 2.3 key rules run per table pair on a MultiTable target too, and a chain table takes every
    2.3 form. The source side of a MultiTable entity keeps every discovered column (BOP-17.4), so a
    source column the SingleTable entity leaves out is in a 1:null entry of the pair.
    """

    maxDiff = None

    def test_every_shape_is_written_as_for_a_single_table(self):
        for name, (table_name, body) in SHAPES.items():
            with self.subTest(shape=name):
                single = single_view(table_name, body)
                target_table, chained = chain_view(table_name, body)
                single_sources = {s for entry in single["entries"] for s in entry[0]}
                left_out = [entry for entry in chained["entries"] if not entry[1] and entry[0][0] not in single_sources]
                self.assertEqual([entry for entry in chained["entries"] if entry not in left_out], single["entries"])
                self.assertEqual({k: v for k, v in chained.items() if k != "entries"},
                                 {k: v for k, v in single.items() if k != "entries"})

    def test_the_target_table_is_the_one_the_yaml_name_gives(self):
        # BOP-19 B: the table the first loop creates and the export writes as ``name``.
        target_table, _ = chain_view("CUSTOMERS", "name: customers")
        self.assertEqual(target_table, "customers")

    def test_an_older_corehub_keeps_the_table_key_as_target_table(self):
        target_table, _ = chain_view("CUSTOMERS", "name: customers", version="2.2.11.5")
        self.assertEqual(target_table, "CUSTOMERS")


class ChainRefusalsTests(unittest.TestCase):
    """What the 2.3 mapping stage would refuse in a chain is refused before any request, naming the chain."""

    def assert_refused(self, table_name, body, pattern, version=V23):
        result, entities = run_23(table_name, body, chained=True, version=version)
        self.assertEqual([e for e in entities if e["agentEntities"][0]["type"] == "MultiTable"], [])
        self.assertEqual(len(result["errors"]), 1, result["errors"])
        self.assertRegex(result["errors"][0], r"^MultiTable chain c1: ")
        self.assertRegex(result["errors"][0], pattern)

    def test_yaml_keys_that_are_not_the_source_primary_key(self):
        self.assert_refused("CUSTOMERS", "name: customers\nkeys: [ID, NAME]\ncolumns:\n  - ID: id\n  - NAME: name",
                            r"R12\.1")

    def test_a_target_primary_key_nothing_feeds(self):
        self.assert_refused("ORDERS", "name: ORDERS_T\ncolumns:\n  - {source: ORDER_ID, target: []}\n  - TOTAL: TOTAL",
                            r"ROW_ID.*expression")

    def test_an_older_corehub_refuses_the_forms_it_cannot_store(self):
        self.assert_refused("CUSTOMERS", SHAPES["one_to_null_and_renames"][1], r"older than Gluesync 2\.3",
                            version="2.2.11.5")


if __name__ == "__main__":
    unittest.main()

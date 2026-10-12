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

from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, _source_list  # noqa: E402

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

    def test_older_corehub_keeps_the_pairs_and_takes_no_field_function(self):
        _, multi = run("SYNCED_AT", version="2.2.11.5")
        target = multi[0]["agentEntities"][1]
        self.assertTrue(all("sourceColumnId" in e for e in target["entityType"]["columnsMappingMatrix"]))
        self.assertNotIn("fieldFunctions", target["entityType"])


if __name__ == "__main__":
    unittest.main()

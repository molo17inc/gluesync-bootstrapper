#!/usr/bin/env python3
"""Export a Gluesync 2.3 entity and import it again (T14 of the alignment plan).

The Automator backup and duplicate flows go through the export and create_entities, so an entity
of each relation shape has to come back with the same matrix, keys and field functions.
"""

import json
import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from export_template_from_corehub import _process_single_entity  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402
from test_bootstrapper_entity_payloads_23 import keyed, matrix, table  # noqa: E402
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

V23 = "2.3.0.0 - build test"

SHAPES = {
    "n_to_one_one_to_n_and_a_produced_column": """        name: customers
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
              type: LocalTs""",
    "one_to_null_and_renames": """        name: customers
        columns:
          - ID: id
          - NAME: email_address
          - source: EMAIL
            target: []""",
    "target_with_its_own_key_produced_by_a_function": """        name: ORDERS_T
        columns:
          - {source: ORDER_ID, target: []}
          - {source: CUSTOMER_ID, target: CUSTOMER_ID}
          - {source: TOTAL, target: TOTAL}
          - {target: ROW_ID, type: bigint, expression: {type: RecordID}}""",
}
TABLE_OF = {"target_with_its_own_key_produced_by_a_function": "ORDERS"}


def create(yaml_text, table_name):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V23)
    result = run_create_entities(hub, yaml_text, _source_list(table_name))
    assert result["failed"] == 0, result
    return hub.puts[0]["entities"][0]


def export(entity):
    source_ae, target_ae = json.loads(json.dumps(entity["agentEntities"]))
    schemas = {}
    _process_single_entity(entity, source_ae, target_ae, schemas, {})
    custom = schemas["src"]["tables"]["custom"]
    return {"src": {"target": "tgt", "tables": {"whitelist": list(custom), "custom": custom}}}


def view(entity):
    source, target = entity["agentEntities"]
    names = {c["id"]: c["name"] for c in target["columns"]}
    return {
        "matrix": matrix(entity),
        "source_keys": keyed(source["columns"]),
        "target_keys": keyed(target["columns"]),
        "field_functions": sorted(
            (names[ff["columnId"]], json.dumps(ff["expression"], sort_keys=True))
            for ff in target["entityType"].get("fieldFunctions", [])
        ),
    }


class RoundTripTests(unittest.TestCase):
    maxDiff = None

    def test_every_shape_comes_back(self):
        for name, body in SHAPES.items():
            with self.subTest(shape=name):
                table_name = TABLE_OF.get(name, "CUSTOMERS")
                original = create(table(body, table_name), table_name)
                exported = export(original)
                import yaml

                restored = create(yaml.safe_dump(exported, sort_keys=False), table_name)
                self.assertEqual(view(restored), view(original))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""An ``@@`` duplicate table builds its columns, keys and matrix as the first loop does (T11).

Before the per-table assembly was shared, the duplicate loop mapped every discovered column under
its source name, whatever the YAML ``columns:`` filtered or renamed (BS-3 of the alignment plan).
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

SOURCE_TABLES = {
    ("src", "CUSTOMERS"): [
        column("ID", 1, "int", is_pk=True, nullable=False),
        column("NAME", 2),
        column("EMAIL", 3),
    ],
}

COLUMNS = """
        columns:
          - ID: ID
          - NAME: FULL_NAME
          - target: SYNCED_AT
            type: timestamp
            expression:
              type: LocalTs
"""

YAML = f"""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: CUSTOMERS_A
{COLUMNS}
      CUSTOMERS@@2:
        sourceTableName: CUSTOMERS
        name: CUSTOMERS_B
        entityName: src.CUSTOMERS copy
{COLUMNS}
"""

TARGET_TABLES = {
    ("tgt", name): [
        column("ID", 11, "int", is_pk=True, nullable=False),
        column("FULL_NAME", 12),
        column("SYNCED_AT", 13, "timestamp"),
    ]
    for name in ("CUSTOMERS_A", "CUSTOMERS_B")
}


def _mapping_view(entity):
    source, target = entity["agentEntities"]
    target_type = target["entityType"]
    return {
        "source_columns": source["columns"],
        "target_columns": [{k: v for k, v in col.items() if k != "tableId"} for col in target["columns"]],
        "matrix": [
            {k: v for k, v in entry.items() if k not in ("sourceTableObjectId", "targetTableObjectId")}
            for entry in target_type["columnsMappingMatrix"]
        ],
        "field_functions": [
            {k: v for k, v in ff.items() if k != "tableOrObjectId"} for ff in target_type.get("fieldFunctions", [])
        ],
    }


class DuplicateTableAssemblyTests(unittest.TestCase):
    maxDiff = None

    def test_duplicate_table_maps_the_columns_the_yaml_names_as_the_first_loop_does(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES)
        run_create_entities(hub, YAML, [{"name": "CUSTOMERS", "schema": "src", "id": 500}])

        entities = {entity["entityName"]: entity for put in hub.puts for entity in put["entities"]}
        first = _mapping_view(entities["src.CUSTOMERS"])
        duplicate = _mapping_view(entities["src.CUSTOMERS copy"])

        self.assertEqual(duplicate, first)
        self.assertEqual([col["name"] for col in duplicate["source_columns"]], ["ID", "NAME"])
        self.assertNotIn("EMAIL", [col["name"] for col in duplicate["target_columns"]])
        self.assertIn("FULL_NAME", [col["name"] for col in duplicate["target_columns"]])


if __name__ == "__main__":
    unittest.main()

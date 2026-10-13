#!/usr/bin/env python3
"""One entity CoreHub refuses does not take its chunk down (T15 of the alignment plan, C-6).

The 2.3 mapping stage refuses the whole request at the first entity it refuses, so a chunk of 50
entities used to fail for one of them. On a 400 the chunk's entities are sent again one by one.
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
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

YAML = """
src:
  target: tgt
  tables:
    whitelist: [PARTS, NOPK, CUSTOMERS]
    custom:
      NOPK:
        keys: [CODE]
      CUSTOMERS:
        name: customers
"""


def refuse_nopk(body):
    names = [entity["entityName"] for entity in body["entities"]]
    if "src.NOPK" in names:
        return "Entity src.NOPK (): R12.6: ... (the stage's line)"
    return None


def run(skip_errors=True):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version="2.3.0.0 - build test", refuse=refuse_nopk)
    result = run_create_entities(hub, YAML, _source_list("PARTS", "NOPK", "CUSTOMERS"), skip_errors=skip_errors)
    return hub, result


class ChunkRefusalTests(unittest.TestCase):
    def test_a_refused_chunk_is_sent_again_entity_by_entity(self):
        hub, result = run()
        self.assertEqual([len(put["entities"]) for put in hub.puts], [3, 1, 1, 1])
        self.assertEqual(sorted(e["entityName"] for e in hub.saved), ["src.CUSTOMERS", "src.PARTS"])
        self.assertEqual({k: result[k] for k in ("successful", "failed", "total")}, {"successful": 2, "failed": 1, "total": 3})
        self.assertEqual(len(result["errors"]), 1)
        self.assertRegex(result["errors"][0], r"^src\.NOPK: .*R12\.6")

    def test_without_skip_errors_the_refusal_is_raised(self):
        with self.assertRaisesRegex(RuntimeError, "src.NOPK"):
            run(skip_errors=False)


if __name__ == "__main__":
    unittest.main()

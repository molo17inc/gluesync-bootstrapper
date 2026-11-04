#!/usr/bin/env python3
"""F3 best-effort: injected failures leave successes in Core Hub; retry with skip completes them."""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for path in (str(TESTS_DIR), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import import_options  # noqa: E402
from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402

TABLES = [f"T{i:02d}" for i in range(1, 11)]
FAIL = {"T03", "T08"}


def _tables():
    cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
    source = {("src", name): [dict(c) for c in cols] for name in TABLES}
    target = {("tgt", name): [dict(c) for c in cols] for name in TABLES}
    return source, target


def _yaml():
    lines = [
        "src:",
        "  target: tgt",
        "  tables:",
        "    whitelist: [" + ", ".join(TABLES) + "]",
        "    custom:",
    ]
    for name in TABLES:
        lines.append(f"      {name}: {{}}")
    return "\n".join(lines) + "\n"


def _source_list():
    return [{"name": name, "schema": "src", "id": 600 + i} for i, name in enumerate(TABLES)]


def _refuse_fail_set(body, fail_set):
    for entity in body.get("entities") or []:
        name = entity.get("entityName") or ""
        table = name.rsplit(".", 1)[-1]
        if table in fail_set:
            return f"injected failure for {name}"
    return None


class BestEffortPartialImportTests(unittest.TestCase):
    def test_eight_of_ten_persist_and_retry_completes(self):
        source, target = _tables()
        fail_set = set(FAIL)
        hub = FakeCoreHub(
            source,
            target,
            version="2.3.0.0 - build test",
            refuse=lambda body: _refuse_fail_set(body, fail_set),
        )
        result = run_create_entities(
            hub, _yaml(), _source_list(), skip_errors=True, chunk_size=50,
        )

        expected_ok = [f"src.{name}" for name in TABLES if name not in FAIL]
        expected_fail = [f"src.{name}" for name in TABLES if name in FAIL]
        self.assertEqual(sorted(e["entityName"] for e in hub.saved), expected_ok)
        self.assertEqual(
            {k: result[k] for k in ("successful", "failed", "total")},
            {"successful": 8, "failed": 2, "total": 10},
        )
        self.assertTrue(result.get("partial"))
        self.assertTrue(result.get("success"))  # skip_errors=True → overall OK
        self.assertEqual(sorted(result.get("failedEntityIds") or []), expected_fail)
        self.assertEqual(len(result.get("failures") or []), 2)
        for item in result["failures"]:
            self.assertIn(item["entity"], expected_fail)
            self.assertIn("injected failure", item["reason"])

        # Retry: clear injected failures; skip entities already in the pipeline.
        hub.refuse = None
        hub.puts.clear()
        options = import_options.ImportOptions(on_existing="skip")
        with import_options.use(options):
            retry = run_create_entities(
                hub, _yaml(), _source_list(), skip_errors=True, chunk_size=50,
            )

        self.assertEqual(
            sorted(e["entityName"] for e in hub.saved),
            [f"src.{name}" for name in TABLES],
        )
        self.assertEqual(retry["successful"], 2)
        self.assertEqual(retry["failed"], 0)
        self.assertEqual(sorted(retry.get("skippedEntities") or []), expected_ok)
        self.assertEqual(retry.get("skipped"), 8)


    def test_skip_errors_false_still_persists_successes(self):
        source, target = _tables()
        fail_set = set(FAIL)
        hub = FakeCoreHub(
            source,
            target,
            version="2.3.0.0 - build test",
            refuse=lambda body: _refuse_fail_set(body, fail_set),
        )
        result = run_create_entities(
            hub, _yaml(), _source_list(), skip_errors=False, chunk_size=50,
        )
        self.assertEqual(len(hub.saved), 8)
        self.assertFalse(result.get("success"))
        self.assertTrue(result.get("partial"))
        self.assertEqual(result["failed"], 2)


if __name__ == "__main__":
    unittest.main()

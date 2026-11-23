#!/usr/bin/env python3
"""Starting the entities of a pipeline reads the TABLE_LIST YAML once, not once per entity.

``start_entity_syncs`` looked up ``snapshotWriteMethod`` by parsing the whole YAML again for every entity. With the
1000 tables of as400-mssql-TRUNCATE (an 8000-line YAML) that cost about a second per entity, and the entity starts of
that pipeline took 20 to 70 minutes of the kit (Nov 2026). ``main.py`` runs its pipeline at import time, so the
functions are taken from its source, as test_bootstrapper_schema_failures does.
"""

import ast
import logging
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FUNCTIONS = ("load_table_list_yaml", "get_snapshot_write_method_from_yaml", "start_entity_syncs")

TABLE_LIST = textwrap.dedent("""
    INTTEST:
      target: dbo
      tables:
        custom:
          TRUNCATED:
            snapshotWriteMethod: TRUNCATE_INSERT
          PLAIN: {}
""")


def _function_nodes(tree):
    """The wanted functions wherever main.py defines them (module level or inside its ``if True:`` block)."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]


def load_main_functions(namespace):
    tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
    wanted = _function_nodes(tree)
    assert sorted(n.name for n in wanted) == sorted(FUNCTIONS)
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "main.py", "exec"), namespace)  # noqa: S102
    return namespace


class StartEntitySyncsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.yaml_path = os.path.join(tmp.name, "tables-list.yaml")
        with open(self.yaml_path, "w") as f:
            f.write(TABLE_LIST)

        self.parses = 0
        real_safe_load = yaml.safe_load

        def counting_safe_load(stream):
            self.parses += 1
            return real_safe_load(stream)

        counting_yaml = type("counting_yaml", (), {"safe_load": staticmethod(counting_safe_load)})
        self.started = []

        def fetch_core_hub(path, method="GET", token=None, body=None, params=None, headers=None):
            if path.endswith("/entities"):
                return [{"entity": {"entityId": f"id-{i}", "entityName": name}}
                        for i, name in enumerate(["INTTEST.TRUNCATED", "INTTEST.PLAIN", "OTHER.X", "NODOT"] * 50)]
            self.started.append(path)
            return {}

        def get_entities(token, pipeline_id):
            return [item["entity"] for item in fetch_core_hub(f"/pipelines/{pipeline_id}/entities")]

        self.main = load_main_functions({
            "os": os, "time": type("t", (), {"sleep": staticmethod(lambda s: None)}),
            "yaml": counting_yaml, "logger": logging.getLogger("test"),
            "fetch_core_hub": fetch_core_hub, "get_entities": get_entities,
            "safe_encode": lambda s: s, "log_failure": lambda *a, **k: None,
            "START_SYNC": True, "ENTITY_START_TIMEOUT": 0,
        })
        os.environ["TABLE_LIST_YAML"] = self.yaml_path
        self.addCleanup(os.environ.pop, "TABLE_LIST_YAML", None)

    def test_the_yaml_is_parsed_once_for_every_entity_of_the_pipeline(self):
        self.main["start_entity_syncs"]("tok", "pipe")
        self.assertEqual(len(self.started), 200)
        self.assertEqual(self.parses, 1)

    def test_each_entity_still_gets_its_own_snapshot_write_method(self):
        self.main["start_entity_syncs"]("tok", "pipe")
        by_entity = {p.split("entity=")[1].split("&")[0]: p.rsplit("snapshotWriteMethod=", 1)[1] for p in self.started}
        self.assertEqual(by_entity["id-0"], "TRUNCATE_INSERT")
        self.assertEqual(by_entity["id-1"], "UPSERT")
        self.assertEqual(by_entity["id-2"], "UPSERT")
        self.assertEqual(by_entity["id-3"], "UPSERT")

    def test_an_unreadable_yaml_starts_every_entity_with_upsert(self):
        os.environ["TABLE_LIST_YAML"] = self.yaml_path + ".missing"
        self.main["start_entity_syncs"]("tok", "pipe")
        self.assertEqual(len(self.started), 200)
        self.assertTrue(all(p.endswith("snapshotWriteMethod=UPSERT") for p in self.started))

    def test_the_single_entity_lookup_still_reads_the_file_itself(self):
        lookup = self.main["get_snapshot_write_method_from_yaml"]
        self.assertEqual(lookup("INTTEST.TRUNCATED", self.yaml_path), "TRUNCATE_INSERT")
        self.assertEqual(lookup("INTTEST.PLAIN", self.yaml_path), "UPSERT")


if __name__ == "__main__":
    unittest.main()

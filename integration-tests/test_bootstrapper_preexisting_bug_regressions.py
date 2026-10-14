#!/usr/bin/env python3
"""Regressions of create_entities found by the SonarQube sweep of main.

- An ``@@`` duplicate table looked its group up in ``groups_by_name``, a name never defined: the
  group was dropped (a NameError once the YAML named one under ``group``).
- An ``@@`` duplicate table read its UDFs after popping them from its target custom properties, so
  they were never compiled into the pipeline.
- A YAML target key with no column mappings did not reset ``found``: a missing key passed silently
  after a found one, and an UnboundLocalError was raised when the first key was missing.
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

from fake_corehub import TARGET_AGENT, FakeCoreHub, column, node_info, run_create_entities  # noqa: E402

import create_all_entities  # noqa: E402

SOURCE_TABLES = {
    ("src", "CUSTOMERS"): [
        column("ID", 1, "int", is_pk=True, nullable=False),
        column("NAME", 2),
    ],
}

TARGET_TABLES = {
    ("tgt", name): [
        column("ID", 11, "int", is_pk=True, nullable=False),
        column("NAME", 12),
    ]
    for name in ("CUSTOMERS_A", "CUSTOMERS_B")
}

UDF = [{"name": "upper_name", "language": "kotlin", "code": "fun upper_name() {}"}]


def _yaml(duplicate_extra):
    return f"""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: CUSTOMERS_A
        groupId: Billing
      CUSTOMERS@@2:
        sourceTableName: CUSTOMERS
        name: CUSTOMERS_B
        entityName: src.CUSTOMERS copy
{duplicate_extra}
"""


def _entities(hub):
    return {entity["entityName"]: entity for put in hub.puts for entity in put["entities"]}


class DuplicateTableGroupTests(unittest.TestCase):
    def test_duplicate_table_joins_the_group_named_under_group_id(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES)
        run_create_entities(hub, _yaml("        groupId: Billing"), [{"name": "CUSTOMERS", "schema": "src", "id": 500}])

        entities = _entities(hub)
        self.assertEqual(entities["src.CUSTOMERS"].get("groupId"), "group-Billing")
        self.assertEqual(entities["src.CUSTOMERS copy"].get("groupId"), "group-Billing")

    def test_duplicate_table_still_reads_the_legacy_group_key(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES)
        run_create_entities(hub, _yaml("        group: Archive"), [{"name": "CUSTOMERS", "schema": "src", "id": 500}])

        self.assertEqual(_entities(hub)["src.CUSTOMERS copy"].get("groupId"), "group-Archive")

    def test_duplicate_table_without_a_group_stays_in_the_default_group(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES)
        run_create_entities(hub, _yaml(""), [{"name": "CUSTOMERS", "schema": "src", "id": 500}])

        self.assertNotIn("groupId", _entities(hub)["src.CUSTOMERS copy"])


class DuplicateTableUdfTests(unittest.TestCase):
    def test_duplicate_table_udfs_are_compiled_into_the_pipeline(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES)
        duplicate_udf = """        customProperties:
          target:
            udf:
              - name: upper_name
                language: kotlin
                code: "fun upper_name() {}"
"""
        compiled = []
        run_create_entities(hub, _yaml(duplicate_udf), [{"name": "CUSTOMERS", "schema": "src", "id": 500}],
                            udf_handler=lambda table, pipeline, udfs, token: compiled.append((table, udfs)))

        self.assertEqual(compiled, [("CUSTOMERS", UDF)])


class ResolveGroupIdTests(unittest.TestCase):
    def test_cached_group_skips_the_api(self):
        with mock.patch.object(create_all_entities, "create_group") as create_group:
            group_id = create_all_entities.resolve_group_id({"groupId": "Billing"}, {"Billing": "g-1"}, "t", "p")
        self.assertEqual(group_id, "g-1")
        create_group.assert_not_called()

    def test_uncached_group_is_created_and_cached(self):
        cache = {}
        with mock.patch.object(create_all_entities, "create_group", return_value="g-2") as create_group:
            self.assertEqual(create_all_entities.resolve_group_id({"groupId": " Sales "}, cache, "t", "p"), "g-2")
            self.assertEqual(create_all_entities.resolve_group_id({"groupId": "Sales"}, cache, "t", "p"), "g-2")
        create_group.assert_called_once_with("t", "p", "Sales")
        self.assertEqual(cache, {"Sales": "g-2"})

    def test_group_core_hub_refuses_falls_back_to_default(self):
        cache = {}
        with mock.patch.object(create_all_entities, "create_group", return_value="_default"):
            self.assertEqual(create_all_entities.resolve_group_id({"groupId": "Nope"}, cache, "t", "p"), "_default")
        self.assertEqual(cache, {})

    def test_no_group_is_the_default_group(self):
        with mock.patch.object(create_all_entities, "create_group") as create_group:
            self.assertEqual(create_all_entities.resolve_group_id({}, {}, "t", "p"), "_default")
            self.assertEqual(create_all_entities.resolve_group_id({"groupId": "_default"}, {}, "t", "p"), "_default")
        create_group.assert_not_called()


class TargetKeysWithoutColumnMappingsTests(unittest.TestCase):
    """The pre-2.3 path of _assemble_target_side, with YAML keys and no column mappings."""

    SOURCE_COLUMNS = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]

    def _assemble(self, keys):
        target_discovery = {"columns": [column("ID", 11, "int", is_pk=True, nullable=False), column("NAME", 12)]}
        columns_def = [dict(col, alias=col["name"]) for col in self.SOURCE_COLUMNS]
        with mock.patch.object(create_all_entities, "get_table_columns", return_value=target_discovery), \
                mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda t, *a, **k: t):
            return create_all_entities._assemble_target_side(
                "token", "pipeline", TARGET_AGENT, "tgt", "CUSTOMERS", "CUSTOMERS",
                {"columns": self.SOURCE_COLUMNS}, columns_def, {"keys": keys}, False, False, {}, False,
                500, 600, {}, node_info(), node_info(), None, None, True,
            )

    def test_every_key_is_resolved(self):
        _, target_keys = self._assemble(["ID", "name"])
        self.assertEqual([key["name"] for key in target_keys], ["ID", "NAME"])

    def test_a_missing_key_after_a_found_one_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Target key 'GHOST' not found"):
            self._assemble(["ID", "GHOST"])

    def test_a_missing_first_key_is_refused_by_name(self):
        with self.assertRaisesRegex(ValueError, "Target key 'GHOST' not found"):
            self._assemble(["GHOST", "ID"])


if __name__ == "__main__":
    unittest.main()

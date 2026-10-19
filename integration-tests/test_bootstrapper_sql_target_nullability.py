#!/usr/bin/env python3
"""A restore onto a SQL target that reports every column not nullable must give the entity back as it was exported.

Found by the Automator round trip (dynamodb-mongodb, demo.drivers_seed -> DRIVERS_integration_test, demo-kits job
17296585474): the six target columns were nullable in the original entity, which had been created from the whitelist
of the kit, and not nullable after the restore. DynamoDB has no nullability: its discovery reports ``isNullable``
false for every column, and the import let the discovered value win over the one of the source column the export
wrote. The export now also writes ``targetIsNullable``, the nullability of the target column, and the import
restores it, as it does ``targetPosition`` and the row's ``type``.

The fixture is the entity of that run, as Core Hub returned it (MongoDB source, DynamoDB target).
"""

import copy
import json
import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import yaml  # noqa: E402

import export_template_from_corehub as export_mod  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "roundtrip" / "mongo_dynamodb_drivers_entity.json"
SCHEMA, TABLE, TARGET_TABLE = "demo", "drivers_seed", "DRIVERS_integration_test"
V23 = "2.3.0.2 - build test"
COLUMNS = ["ID", "FIRST_NAME", "LAST_NAME", "EMAIL", "GENDER", "DRIVING_LICENSE_ID"]


def _entity():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def export_yaml(entity):
    """The YAML the Automator exports for ``entity``."""
    schemas = export_mod.build_schemas_from_entities([copy.deepcopy(entity)], {})
    return yaml.safe_dump(export_mod.build_yaml_structure(schemas, {}), sort_keys=False)


def _source_tables(entity):
    """What the MongoDB agent discovers: the source columns of the entity."""
    return {(SCHEMA, TABLE): [{k: v for k, v in col.items() if k != "tableId"}
                              for col in entity["agentEntities"][0]["columns"]]}


def _target_tables(entity):
    """What DynamoDB reports for the table the original pipeline created: no order, every column not nullable."""
    return {(SCHEMA, TARGET_TABLE): [
        {"id": col["id"], "name": col["name"], "dataType": col["dataType"], "position": 0, "isPK": col["isPK"],
         "isNullable": False} for col in entity["agentEntities"][1]["columns"]]}


def restore(entity, exported, target_tables=None, target_type="SQL"):
    """Restore ``exported`` against the tables of ``entity``; returns the entity CoreHub saved."""
    hub = FakeCoreHub(_source_tables(entity), target_tables or _target_tables(entity), version=V23,
                      source_category="NoSQL", target_category=target_type)
    result = run_create_entities(hub, exported, [{"name": TABLE, "schema": SCHEMA, "id": 500}],
                                 source_schema=SCHEMA, target_schema=SCHEMA, source_type="NoSQL",
                                 target_type=target_type)
    assert result["failed"] == 0, result
    assert len(hub.saved) == 1, result
    return hub.saved[0]


def target_columns(entity):
    return {c["name"]: c for c in entity["agentEntities"][1]["columns"]}


def exported_rows(exported):
    return yaml.safe_load(exported)[SCHEMA]["tables"]["custom"][TABLE]["columns"]


def _dump_without(exported, key):
    """``exported`` with ``key`` removed from every mapping row (an export written before the key existed)."""
    schemas = yaml.safe_load(exported)
    for row in schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]:
        row.pop(key)
    return yaml.safe_dump(schemas, sort_keys=False)


class SqlTargetNullabilityRestoreTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entity()
        cls.exported = export_yaml(cls.original)
        cls.restored = restore(cls.original, cls.exported)

    def test_the_target_reports_every_column_not_nullable_where_the_original_had_them_nullable(self):
        discovered = _target_tables(self.original)[(SCHEMA, TARGET_TABLE)]
        self.assertEqual({c["isNullable"] for c in discovered}, {False})
        self.assertEqual({c["isNullable"] for c in target_columns(self.original).values()}, {True})

    def test_the_export_writes_the_nullability_of_the_target_column(self):
        self.assertEqual({row["source"]: row["targetIsNullable"] for row in exported_rows(self.exported)},
                         dict.fromkeys(COLUMNS, True))

    def test_the_restored_target_columns_are_as_nullable_as_they_were(self):
        self.assertEqual({n: c["isNullable"] for n, c in target_columns(self.restored).items()},
                         {n: c["isNullable"] for n, c in target_columns(self.original).items()})

    def test_a_target_column_exported_not_nullable_is_restored_not_nullable(self):
        entity = _entity()
        target_columns(entity)["EMAIL"]["isNullable"] = False
        restored = restore(entity, export_yaml(entity), _target_tables(_entity()))
        self.assertEqual({n: c["isNullable"] for n, c in target_columns(restored).items()},
                         {n: n != "EMAIL" for n in COLUMNS})

    def test_the_export_of_the_restored_entity_is_the_export_of_the_original(self):
        self.assertEqual(yaml.safe_load(export_yaml(self.restored)), yaml.safe_load(self.exported))

    def test_the_restored_target_columns_are_the_original_ones(self):
        strip = lambda e: sorted(({k: v for k, v in c.items() if k != "tableId"}  # noqa: E731
                                  for c in e["agentEntities"][1]["columns"]), key=lambda c: c["name"])
        self.assertEqual(strip(self.restored), strip(self.original))

    def test_the_target_position_and_type_are_restored_as_before(self):
        # the target reports position 0 for every column: the exported targetPosition still wins
        self.assertEqual({n: c["position"] for n, c in target_columns(self.restored).items()},
                         {n: c["position"] for n, c in target_columns(self.original).items()})
        self.assertEqual({n: c["dataType"] for n, c in target_columns(self.restored).items()},
                         {n: c["dataType"] for n, c in target_columns(self.original).items()})
        self.assertEqual({row["source"]: (row["type"], row["targetPosition"]) for row in exported_rows(self.exported)},
                         {n: (c["dataType"], c["position"]) for n, c in target_columns(self.original).items()})


class ExportedRowsTests(unittest.TestCase):
    """Rows of an export written before ``targetIsNullable`` existed, and rows edited by hand, still import."""

    maxDiff = None

    def test_an_older_export_takes_the_nullability_the_target_reports(self):
        entity = _entity()
        restored = restore(entity, _dump_without(export_yaml(entity), "targetIsNullable"))
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {False})

    def test_an_older_export_keeps_the_position_and_type_it_always_did(self):
        entity = _entity()
        with_key = restore(entity, export_yaml(entity))
        without = restore(entity, _dump_without(export_yaml(entity), "targetIsNullable"))
        for key in ("position", "dataType"):
            self.assertEqual({n: c[key] for n, c in target_columns(without).items()},
                             {n: c[key] for n, c in target_columns(with_key).items()})

    def test_without_a_discovered_nullability_an_older_export_takes_the_one_of_the_source_column(self):
        entity = _entity()
        target_tables = _target_tables(entity)
        for col in target_tables[(SCHEMA, TARGET_TABLE)]:
            del col["isNullable"]
        restored = restore(entity, _dump_without(export_yaml(entity), "targetIsNullable"), target_tables)
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {True})

    def test_a_value_that_is_not_a_boolean_is_ignored(self):
        entity = _entity()
        schemas = yaml.safe_load(export_yaml(entity))
        for row in schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]:
            row["targetIsNullable"] = "yes"
        restored = restore(entity, yaml.safe_dump(schemas, sort_keys=False))
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {False})

    def test_the_nullability_of_the_target_does_not_change_the_one_of_the_source_columns(self):
        entity = _entity()
        schemas = yaml.safe_load(export_yaml(entity))
        for row in schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]:
            row["targetIsNullable"] = False
        restored = restore(entity, yaml.safe_dump(schemas, sort_keys=False))
        self.assertEqual({c["isNullable"] for c in restored["agentEntities"][0]["columns"]}, {True})
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {False})


class NoSqlTargetTests(unittest.TestCase):
    """A NoSQL target keeps reporting no nullability: its columns take the one of the source column, as before."""

    def test_the_exported_target_nullability_does_not_change_a_nosql_target_column(self):
        entity = _entity()
        schemas = yaml.safe_load(export_yaml(entity))
        for row in schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]:
            row["targetIsNullable"] = False
        restored = restore(entity, yaml.safe_dump(schemas, sort_keys=False), target_type="NoSQL")
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {True})


class WhitelistAndDiscoveryTests(unittest.TestCase):
    """The columns of a table whose YAML states no mapping rows are built as they always were."""

    def test_a_discovery_only_restore_takes_the_nullability_the_target_reports(self):
        entity = _entity()
        discovery_only = yaml.safe_dump({SCHEMA: {"target": SCHEMA, "tables": {"whitelist": [TABLE], "custom": {
            TABLE: {"name": TARGET_TABLE}}}}})
        restored = restore(entity, discovery_only)
        self.assertEqual({c["isNullable"] for c in target_columns(restored).values()}, {False})


if __name__ == "__main__":
    unittest.main()

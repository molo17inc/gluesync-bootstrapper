#!/usr/bin/env python3
"""A restore onto a SQL target that does not keep what it was given must give the entity back as it was exported.

Found by the Automator round trip (pgsqlwal-cassandra): the restore ran against Cassandra tables the original
pipeline had already created. Cassandra is a SQL target for the bootstrapper, but its catalog

- reports no position: every column comes back at position 0;
- reads varchar back as text (``cassandra_missing_types_test.varchar_value``),

so the restored target columns were all at position 0 where the originals were at 1..N, and ``varchar_value`` was
``text`` where it had been pinned to ``varchar``. The export wrote the target type of each row but not the position
of the target column, and the import let the type the target reports win over the exported one.

The fixture holds the two entities of that run (cassandra_missing_types_test, test_create_table: the source columns
stored in a different order than their positions), as Core Hub returned them.
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
import fake_corehub  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "roundtrip" / "pgsqlwal_cassandra_entities.json"
SOURCE_SCHEMA, TARGET_SCHEMA = "public", "demo"
V23 = "2.3.0.2 - build test"

# What a Cassandra agent advertises for the types of the entities: text is its string type, varchar a supported one.
CASSANDRA_MATRIX = [
    {"gluesyncDataType": "STRING", "defaultType": "text", "supportedTypes": ["varchar", "ascii"]},
    {"gluesyncDataType": "INT", "defaultType": "int", "supportedTypes": ["tinyint", "smallint"]},
    {"gluesyncDataType": "LONG", "defaultType": "bigint", "supportedTypes": []},
    {"gluesyncDataType": "TIME", "defaultType": "time", "supportedTypes": []},
    {"gluesyncDataType": "TIMESTAMP", "defaultType": "timestamp", "supportedTypes": []},
    {"gluesyncDataType": "DATE", "defaultType": "date", "supportedTypes": []},
    {"gluesyncDataType": "BLOB", "defaultType": "blob", "supportedTypes": []},
    {"gluesyncDataType": "UUID", "defaultType": "timeuuid", "supportedTypes": ["uuid"]},
]

# What the Cassandra catalog calls a type the entity pinned to another name.
CATALOG_NAMES = {"varchar": "text"}


def _entities():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _by_name(entities):
    return {e["entityName"].split(".", 1)[1]: e for e in entities}


class CassandraLikeHub(FakeCoreHub):
    """A target that reports every column at position 0 and varchar as text, and advertises its types."""

    def node_info(self, token, pipeline_id, agent_id):
        info = super().node_info(token, pipeline_id, agent_id)
        if agent_id == fake_corehub.TARGET_AGENT:
            info["dataTypesMatrix"] = copy.deepcopy(CASSANDRA_MATRIX)
        return info


def _discovery(entities):
    """``(source_tables, target_tables)`` the fake CoreHub discovers: the tables of the original entities."""
    source_tables, target_tables = {}, {}
    for name, entity in _by_name(entities).items():
        source_ae, target_ae = entity["agentEntities"]
        source_tables[(SOURCE_SCHEMA, name)] = sorted(
            ({k: v for k, v in col.items() if k != "tableId"} for col in source_ae["columns"]),
            key=lambda col: col["position"],
        )
        target_tables[(TARGET_SCHEMA, name)] = [
            {
                "id": col["id"],
                "name": col["name"],
                "dataType": CATALOG_NAMES.get(col["dataType"], col["dataType"]),
                "position": 0,
                "isPK": col["isPK"],
                "isNullable": col["isNullable"],
            }
            for col in target_ae["columns"]
        ]
    return source_tables, target_tables


def export_yaml(entities):
    """The YAML the Automator exports for ``entities``."""
    schemas = export_mod.build_schemas_from_entities(copy.deepcopy(entities), {})
    return yaml.safe_dump(export_mod.build_yaml_structure(schemas, {}), sort_keys=False)


def restore(entities, exported, target_tables=None):
    """Restore ``exported`` against the tables of ``entities``; returns the entities CoreHub saved.

    ``target_tables`` replaces what the target discovers (see :func:`_discovery`).
    """
    source_tables, discovered_target_tables = _discovery(entities)
    hub = CassandraLikeHub(source_tables, target_tables or discovered_target_tables, version=V23)
    source_list = [{"name": name, "schema": SOURCE_SCHEMA, "id": 500 + idx}
                   for idx, name in enumerate(_by_name(entities))]
    result = run_create_entities(hub, exported, source_list, source_schema=SOURCE_SCHEMA,
                                 target_schema=TARGET_SCHEMA, target_type="SQL")
    assert result["failed"] == 0, result
    return hub.saved


def target_columns(entity):
    return {c["name"]: c for c in entity["agentEntities"][1]["columns"]}


class SqlTargetRestoreTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entities()
        cls.exported = export_yaml(cls.original)
        cls.restored = restore(cls.original, cls.exported)

    def test_the_target_catalog_is_the_one_the_run_found(self):
        _, target_tables = _discovery(self.original)
        columns = target_tables[(TARGET_SCHEMA, "cassandra_missing_types_test")]
        self.assertEqual({c["position"] for c in columns}, {0})
        self.assertEqual({c["name"]: c["dataType"] for c in columns}["varchar_value"], "text")

    def test_the_export_writes_the_position_of_the_target_column(self):
        tables = yaml.safe_load(self.exported)[SOURCE_SCHEMA]["tables"]["custom"]
        by_source = {name: {row["source"]: row["targetPosition"] for row in table["columns"]}
                     for name, table in tables.items()}
        self.assertEqual(by_source["cassandra_missing_types_test"],
                         {"id": 1, "tinyint_value": 2, "varchar_value": 3, "time_value": 4, "timeuuid_value": 5})
        self.assertEqual(by_source["test_create_table"],
                         {"id": 1, "image": 2, "ts": 3, "date": 4, "dt": 5, "tn": 6, "bint": 7, "nil": 8})

    def test_the_restored_target_columns_have_the_positions_they_had(self):
        for original, restored in zip(self.original, _by_name(self.restored).values()):
            self.assertEqual({n: c["position"] for n, c in target_columns(restored).items()},
                             {n: c["position"] for n, c in target_columns(original).items()},
                             original["entityName"])

    def test_no_restored_target_column_is_at_position_zero(self):
        for restored in self.restored:
            self.assertNotIn(0, [c["position"] for c in target_columns(restored).values()])

    def test_a_type_pinned_to_varchar_comes_back_as_varchar_not_as_the_catalog_text(self):
        restored = _by_name(self.restored)["cassandra_missing_types_test"]
        self.assertEqual(target_columns(restored)["varchar_value"]["dataType"], "varchar")

    def test_every_target_type_is_the_exported_one(self):
        for original, restored in zip(self.original, _by_name(self.restored).values()):
            self.assertEqual({n: c["dataType"] for n, c in target_columns(restored).items()},
                             {n: c["dataType"] for n, c in target_columns(original).items()},
                             original["entityName"])

    def test_the_export_of_the_restored_entities_is_the_export_of_the_original(self):
        self.assertEqual(yaml.safe_load(export_yaml(self.restored)), yaml.safe_load(self.exported))


class ExportedRowsTests(unittest.TestCase):
    """Rows of an export written before ``targetPosition`` existed, and rows edited by hand, still import."""

    def _without_target_position(self):
        schemas = yaml.safe_load(export_yaml(_entities()))
        for table in schemas[SOURCE_SCHEMA]["tables"]["custom"].values():
            for row in table["columns"]:
                row.pop("targetPosition")
        return yaml.safe_dump(schemas, sort_keys=False)

    def test_the_position_falls_back_to_the_one_of_the_source_column(self):
        entities = _entities()
        restored = restore(entities, self._without_target_position())
        for original, entity in zip(entities, _by_name(restored).values()):
            source_positions = {c["name"]: c["position"] for c in original["agentEntities"][0]["columns"]}
            self.assertEqual({n: c["position"] for n, c in target_columns(entity).items()}, source_positions)

    def test_a_type_the_target_does_not_advertise_falls_back_to_what_the_target_reports(self):
        schemas = yaml.safe_load(export_yaml(_entities()))
        for row in schemas[SOURCE_SCHEMA]["tables"]["custom"]["cassandra_missing_types_test"]["columns"]:
            if row["source"] == "varchar_value":
                row["type"] = "character varying"  # a source type: Cassandra advertises no such type
        restored = restore(_entities(), yaml.safe_dump(schemas, sort_keys=False))
        entity = _by_name(restored)["cassandra_missing_types_test"]
        self.assertEqual(target_columns(entity)["varchar_value"]["dataType"], "text")

    def test_a_target_data_type_override_wins_over_the_exported_type(self):
        schemas = yaml.safe_load(export_yaml(_entities()))
        for row in schemas[SOURCE_SCHEMA]["tables"]["custom"]["cassandra_missing_types_test"]["columns"]:
            if row["source"] == "varchar_value":
                row["targetDataType"] = "ascii"
        restored = restore(_entities(), yaml.safe_dump(schemas, sort_keys=False))
        entity = _by_name(restored)["cassandra_missing_types_test"]
        self.assertEqual(target_columns(entity)["varchar_value"]["dataType"], "ascii")

    def test_a_position_the_row_states_wins_over_the_one_the_target_reports(self):
        schemas = yaml.safe_load(export_yaml(_entities()))
        for row in schemas[SOURCE_SCHEMA]["tables"]["custom"]["cassandra_missing_types_test"]["columns"]:
            row["targetPosition"] = 10 + row["ordinalPosition"]
        restored = restore(_entities(), yaml.safe_dump(schemas, sort_keys=False))
        entity = _by_name(restored)["cassandra_missing_types_test"]
        self.assertEqual([c["position"] for c in entity["agentEntities"][1]["columns"]], [11, 12, 13, 14, 15])

    def test_a_target_column_exported_at_position_zero_is_restored_at_position_zero(self):
        entities = _entities()
        for entity in entities:
            for col in entity["agentEntities"][1]["columns"]:
                col["position"] = 0  # an entity built before the bootstrapper gave target columns a position
        exported = export_yaml(entities)
        for table in yaml.safe_load(exported)[SOURCE_SCHEMA]["tables"]["custom"].values():
            self.assertEqual({row["targetPosition"] for row in table["columns"]}, {0})
        _, target_tables = _discovery(_entities())
        for columns in target_tables.values():
            for position, col in enumerate(columns, start=1):
                col["position"] = position  # the target now reports a real position
        for entity in restore(entities, exported, target_tables):
            self.assertEqual({c["position"] for c in target_columns(entity).values()}, {0})


class TargetWithoutRowsTests(unittest.TestCase):
    """The columns of a table whose YAML states no mapping are built from discovery."""

    def test_the_position_the_target_reports_is_kept_and_the_source_position_stands_in_for_none(self):
        entities = _entities()
        _, target_tables = _discovery(entities)
        for col in target_tables[(TARGET_SCHEMA, "test_create_table")]:
            col["position"] = {"id": 1, "image": 2, "ts": 3, "date": 4, "dt": 5, "tn": 6, "bint": 7, "nil": 8}[col["name"]]
        discovery_only = yaml.safe_dump({SOURCE_SCHEMA: {"target": TARGET_SCHEMA, "tables": {
            "whitelist": ["test_create_table", "cassandra_missing_types_test"]}}})
        restored = _by_name(restore(entities, discovery_only, target_tables))
        self.assertEqual({n: c["position"] for n, c in target_columns(restored["test_create_table"]).items()},
                         {"id": 1, "image": 2, "ts": 3, "date": 4, "dt": 5, "tn": 6, "bint": 7, "nil": 8})
        # the target reports 0 for the other table: its columns are at the position of their source column
        self.assertEqual([c["position"] for c in restored["cassandra_missing_types_test"]["agentEntities"][1]["columns"]],
                         [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()

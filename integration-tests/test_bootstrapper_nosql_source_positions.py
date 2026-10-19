#!/usr/bin/env python3
"""A restore onto a schemaless source must give the source columns the order the export lists them in.

Found by the Automator round trip (mongo8.0.3-couchbase-two-ways, demo.type_test_GSSD_179): a MongoDB collection has
no schema, its agent infers the columns by sampling documents, so the positions it reports depend on the data at
restore time. The original entity had mixed_number_2 at 3, description at 4 and mixed_number_1 at 5; the restore
discovered mixed_number_1 at 3, mixed_number_2 at 4 and description at 5, and the columnsMappingMatrix, which is
written in the order of the positions, followed. The export carries the right ``ordinalPosition`` of every row.

The fixture is the entity of that run, as Core Hub returned it (MongoDB source, Couchbase target).
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

import create_all_entities  # noqa: E402
import fake_corehub  # noqa: E402
import export_template_from_corehub as export_mod  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "roundtrip" / "mongo_couchbase_type_test_entity.json"
SCHEMA, TABLE = "demo", "type_test_GSSD_179"
V23 = "2.3.0.2 - build test"

# The positions the agent found when the restore ran: the same fields, sampled in another order.
RESTORE_DISCOVERY = {"_id": 0, "int32_field": 1, "int64_field": 2, "mixed_number_1": 3, "mixed_number_2": 4,
                     "description": 5}
ORIGINAL_POSITIONS = {"_id": 0, "int32_field": 1, "int64_field": 2, "mixed_number_2": 3, "description": 4,
                      "mixed_number_1": 5}


def _entity():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def export_yaml(entity):
    """The YAML the Automator exports for ``entity``."""
    schemas = export_mod.build_schemas_from_entities([copy.deepcopy(entity)], {})
    return yaml.safe_dump(export_mod.build_yaml_structure(schemas, {}), sort_keys=False)


def _source_tables(entity, positions):
    """What the source agent discovers: the columns of the entity at ``positions``, in the order of the entity."""
    columns = [{k: v for k, v in col.items() if k != "tableId"} | {"position": positions[col["name"]]}
               for col in entity["agentEntities"][0]["columns"]]
    return {(SCHEMA, TABLE): columns}


def _target_tables(entity):
    """What a Couchbase collection the original pipeline filled reports: the columns inferred from its documents."""
    return {(SCHEMA, TABLE): [
        {"id": col["id"], "name": col["name"], "dataType": "number", "position": 0, "isPK": col["isPK"],
         "isNullable": False} for col in entity["agentEntities"][1]["columns"]]}


class CouchbaseLikeHub(FakeCoreHub):
    """A target that advertises its types, as the Couchbase agent does."""

    def node_info(self, token, pipeline_id, agent_id):
        info = super().node_info(token, pipeline_id, agent_id)
        if agent_id == fake_corehub.TARGET_AGENT:
            info["dataTypesMatrix"] = [
                {"gluesyncDataType": "STRING", "defaultType": "string", "supportedTypes": []},
                {"gluesyncDataType": "INT", "defaultType": "int", "supportedTypes": []},
                {"gluesyncDataType": "LONG", "defaultType": "long", "supportedTypes": []},
            ]
        return info


def restore_all(entity, exported, positions=None, source_tables=None, source_type="NoSQL", version=V23):
    """Restore ``exported`` against a source that reports ``positions``; returns ``(result, entities CoreHub saved)``."""
    source_tables = source_tables or _source_tables(entity, positions or RESTORE_DISCOVERY)
    hub = CouchbaseLikeHub(source_tables, _target_tables(entity), version=version, source_category=source_type,
                           target_category="NoSQL")
    source_list = [{"name": TABLE, "schema": SCHEMA, "id": 500}]
    result = run_create_entities(hub, exported, source_list, source_schema=SCHEMA, target_schema=SCHEMA,
                                 source_type=source_type, target_type="NoSQL")
    return result, hub.saved


def restore(entity, exported, **kwargs):
    """Like :func:`restore_all`, for a run that creates one entity (returned)."""
    result, saved = restore_all(entity, exported, **kwargs)
    assert result["failed"] == 0, result
    assert len(saved) == 1, result
    return saved[0]


def source_positions(entity):
    return {c["name"]: c["position"] for c in entity["agentEntities"][0]["columns"]}


def matrix_order(entity):
    """The source column names of the columnsMappingMatrix, in the order it is written."""
    source_ae, target_ae = entity["agentEntities"]
    names = {c["id"]: c["name"] for c in source_ae["columns"]}
    return [names[entry["sourceColumnIds"][0]] for entry in target_ae["entityType"]["columnsMappingMatrix"]]


class NoSqlSourcePositionsTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entity()
        cls.exported = export_yaml(cls.original)
        cls.restored = restore(cls.original, cls.exported)

    def test_the_run_found_the_columns_in_another_order(self):
        self.assertEqual(source_positions(self.original), ORIGINAL_POSITIONS)
        self.assertNotEqual(RESTORE_DISCOVERY, ORIGINAL_POSITIONS)

    def test_the_export_lists_the_columns_in_their_order(self):
        rows = yaml.safe_load(self.exported)[SCHEMA]["tables"]["custom"][TABLE]["columns"]
        self.assertEqual({row["source"]: row["ordinalPosition"] for row in rows},
                         {name: position + 1 for name, position in ORIGINAL_POSITIONS.items()})

    def test_the_restored_source_columns_have_the_positions_they_had(self):
        self.assertEqual(source_positions(self.restored), ORIGINAL_POSITIONS)

    def test_the_matrix_is_written_in_the_order_of_the_original(self):
        self.assertEqual(matrix_order(self.restored), matrix_order(self.original))
        self.assertEqual(matrix_order(self.restored),
                         ["int32_field", "int64_field", "mixed_number_2", "description", "mixed_number_1", "_id"])

    def test_the_restored_source_columns_are_the_original_ones(self):
        strip = lambda e: sorted(({k: v for k, v in c.items() if k != "tableId"}  # noqa: E731
                                  for c in e["agentEntities"][0]["columns"]), key=lambda c: c["name"])
        self.assertEqual(strip(self.restored), strip(self.original))

    def test_the_export_of_the_restored_entity_is_the_export_of_the_original(self):
        self.assertEqual(yaml.safe_load(export_yaml(self.restored)), yaml.safe_load(self.exported))

    def test_a_table_read_under_a_duplicate_key_gets_the_positions_too(self):
        schemas = yaml.safe_load(self.exported)
        custom = schemas[SCHEMA]["tables"]["custom"]
        custom[f"{TABLE}@@2"] = dict(custom[TABLE], sourceTableName=TABLE)
        schemas[SCHEMA]["tables"]["whitelist"] = [TABLE, f"{TABLE}@@2"]
        result, saved = restore_all(self.original, yaml.safe_dump(schemas, sort_keys=False))
        self.assertEqual((result["failed"], len(saved)), (0, 2))
        self.assertEqual([source_positions(entity) for entity in saved], [ORIGINAL_POSITIONS] * 2)

    def test_an_export_without_ordinal_positions_keeps_what_discovery_reports(self):
        schemas = yaml.safe_load(self.exported)
        for row in schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]:
            row.pop("ordinalPosition")
        restored = restore(self.original, yaml.safe_dump(schemas, sort_keys=False))
        self.assertEqual(source_positions(restored), RESTORE_DISCOVERY)


class SqlSourceTests(unittest.TestCase):
    """The positions of a SQL source are the ordinals of the database: the export does not change them."""

    def test_the_discovered_positions_are_kept(self):
        entity = _entity()
        restored = restore(entity, export_yaml(entity), source_type="RDBMS")
        self.assertEqual(source_positions(restored), RESTORE_DISCOVERY)


class ColumnsOutOfStepTests(unittest.TestCase):
    """What the YAML lists and what discovery returns may differ: the restore warns and carries on."""

    maxDiff = None

    def _restore_with(self, edit_rows, edit_discovery=lambda columns: columns, version=V23):
        entity = _entity()
        schemas = yaml.safe_load(export_yaml(entity))
        rows = schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]
        schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"] = edit_rows(rows)
        tables = _source_tables(entity, RESTORE_DISCOVERY)
        tables[(SCHEMA, TABLE)] = edit_discovery(tables[(SCHEMA, TABLE)])
        with self.assertLogs(create_all_entities.logger, level="WARNING") as logs:
            logging.disable(logging.NOTSET)
            try:
                restored = restore(entity, yaml.safe_dump(schemas, sort_keys=False), source_tables=tables,
                                   version=version)
            finally:
                logging.disable(logging.CRITICAL)
        return restored, "\n".join(logs.output)

    def test_a_discovered_column_the_yaml_does_not_list_keeps_its_discovered_position(self):
        restored, log = self._restore_with(lambda rows: [r for r in rows if r["source"] != "description"])
        positions = source_positions(restored)
        self.assertNotIn("description", positions)  # the rows are the columns of the entity
        self.assertEqual({n: positions[n] for n in ("mixed_number_2", "mixed_number_1")},
                         {"mixed_number_2": 3, "mixed_number_1": 4})  # the slots of the two listed columns
        self.assertIn("description", log)

    def test_a_listed_column_discovery_does_not_return_is_reported_and_the_rest_keep_their_order(self):
        def without_description(columns):
            return [c for c in columns if c["name"] != "description"]

        # a CoreHub before 2.3 keeps the column from the metadata of the row; 2.3 refuses it, as it always did
        restored, log = self._restore_with(lambda rows: rows, without_description, version="2.2.11.5 - build test")
        self.assertEqual({n: p for n, p in source_positions(restored).items() if n != "description"},
                         {"_id": 0, "int32_field": 1, "int64_field": 2, "mixed_number_2": 3, "mixed_number_1": 4})
        self.assertIn("not found in discovery", log)

    def test_a_column_discovery_reports_without_a_position_leaves_the_discovered_order(self):
        def without_position(columns):
            return [{k: v for k, v in c.items() if k != "position"} if c["name"] == "description" else c
                    for c in columns]

        restored, log = self._restore_with(lambda rows: rows, without_position)
        self.assertEqual(source_positions(restored)["mixed_number_1"], 3)
        self.assertIn("no position", log)


class HelperTests(unittest.TestCase):
    """:func:`create_all_entities._with_exported_source_positions` on its own."""

    @staticmethod
    def _columns(*named_positions):
        return {"columns": [{"id": i, "name": n, "position": p} for i, (n, p) in enumerate(named_positions, 1)]}

    @staticmethod
    def _config(*named_ordinals):
        return {"columns": [{"source": n, "target": n, "ordinalPosition": o} for n, o in named_ordinals]}

    def _positions(self, columns, config):
        result = create_all_entities._with_exported_source_positions(columns, config, "T")
        return {c["name"]: c["position"] for c in result["columns"]}

    def test_the_numbering_of_discovery_is_kept(self):
        self.assertEqual(self._positions(self._columns(("b", 1), ("a", 2)), self._config(("a", 1), ("b", 2))),
                         {"a": 1, "b": 2})

    def test_the_discovery_is_not_modified(self):
        columns = self._columns(("b", 0), ("a", 1))
        self._positions(columns, self._config(("a", 1), ("b", 2)))
        self.assertEqual([c["position"] for c in columns["columns"]], [0, 1])

    def test_rows_without_a_source_or_an_ordinal_position_change_nothing(self):
        columns = self._columns(("b", 0), ("a", 1))
        for config in ({}, None, {"columns": [{"target": "a", "expression": {}}, {"source": ["a", "b"], "target": "c"},
                                              {"source": "a", "ordinalPosition": None}, "a"]}):
            self.assertIs(create_all_entities._with_exported_source_positions(columns, config, "T"), columns)

    def test_names_are_matched_as_spelt_then_case_insensitively(self):
        columns = self._columns(("id", 0), ("ID", 1), ("Name", 2))
        self.assertEqual(self._positions(columns, self._config(("ID", 1), ("id", 2), ("name", 3))),
                         {"ID": 0, "id": 1, "Name": 2})


if __name__ == "__main__":
    unittest.main()

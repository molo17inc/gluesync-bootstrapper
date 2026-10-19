#!/usr/bin/env python3
"""Names that differ by case only are distinct columns: a restore must keep both.

Found by the Automator round trip (cosmosdb-mongodb, integration_tests.drivers): a Cosmos DB container holds the
document field ``ID`` and the system key ``id``, two columns. The export is right (keys ``[ID, id]``, one row
each, both primary keys); the restore read every name case-insensitively, wrote ``id -> id`` twice and aborted
with "the column row id -> id of drivers is listed twice".

The fixture is the entity of that run, as Core Hub returned it, cut down to four columns (Cosmos source, MongoDB
target).
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
import export_template_from_corehub as export_mod  # noqa: E402
import entity_mapping  # noqa: E402
from entity_mapping import EntityMappingError, group_relations, grouped_only_forms  # noqa: E402
import fake_corehub  # noqa: E402
from fake_corehub import FakeCoreHub, run_create_entities  # noqa: E402

FIXTURE = TESTS_DIR / "fixtures" / "roundtrip" / "cosmos_mongo_drivers_entity.json"
SCHEMA, TABLE = "integration_tests", "drivers"
V23 = "2.3.0.2 - build test"
V22 = "2.2.11.5 - build test"


def _entity():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def export_yaml(entity):
    """The YAML the Automator exports for ``entity``."""
    schemas = export_mod.build_schemas_from_entities([copy.deepcopy(entity)], {})
    return yaml.safe_dump(export_mod.build_yaml_structure(schemas, {}), sort_keys=False)


def _discovered(entity, agent_index, only_names=None):
    """The columns an agent discovers: the ones of the entity, without the table id."""
    return [{k: v for k, v in col.items() if k != "tableId"} for col in entity["agentEntities"][agent_index]["columns"]
            if only_names is None or col["name"] in only_names]


class MongoLikeHub(FakeCoreHub):
    """A target that advertises its types, as the MongoDB agent does."""

    def node_info(self, token, pipeline_id, agent_id):
        info = super().node_info(token, pipeline_id, agent_id)
        if agent_id == fake_corehub.TARGET_AGENT:
            info["dataTypesMatrix"] = [{"gluesyncDataType": "STRING", "defaultType": "STRING", "supportedTypes": []}]
        return info


def restore_all(entity, exported, source_columns=None, target_columns=None, version=V23):
    """Restore ``exported`` against agents that discover ``source_columns`` / ``target_columns`` (the entity's own
    by default); returns ``(result, entities Core Hub saved)``."""
    source_columns = _discovered(entity, 0) if source_columns is None else source_columns
    target_columns = _discovered(entity, 1) if target_columns is None else target_columns
    hub = MongoLikeHub({(SCHEMA, TABLE): source_columns}, {("demo", TABLE): target_columns}, version=version,
                      source_category="NoSQL", target_category="NoSQL")
    result = run_create_entities(hub, exported, [{"name": TABLE, "schema": SCHEMA, "id": 500}],
                                 source_schema=SCHEMA, target_schema="demo", source_type="NoSQL", target_type="NoSQL")
    return result, hub.saved


def restore(entity, exported, **kwargs):
    """Like :func:`restore_all`, for a run that creates one entity (returned)."""
    result, saved = restore_all(entity, exported, **kwargs)
    assert result["failed"] == 0, result
    assert len(saved) == 1, result
    return saved[0]


def columns_of(entity, agent_index):
    """The columns of one side: a MultiTable entity lists its table header, then the columns."""
    columns = entity["agentEntities"][agent_index]["columns"]
    return columns[1] if entity["agentEntities"][agent_index]["type"] == "MultiTable" else columns


def names(entity, agent_index):
    return {c["id"]: c["name"] for c in columns_of(entity, agent_index)}


def matrix(entity):
    """``[(source name, target name)]`` of the columnsMappingMatrix, in the order it is written."""
    source_names, target_names = names(entity, 0), names(entity, 1)
    return [(source_names[(entry.get("sourceColumnIds") or [entry.get("sourceColumnId")])[0]],
             target_names[(entry.get("targetColumnIds") or [entry.get("targetColumnId")])[0]])
            for entry in entity["agentEntities"][1]["entityType"]["columnsMappingMatrix"]]


def summary(entity, agent_index):
    """``{name: (id, position, isPK)}`` of the columns of one side."""
    return {c["name"]: (c["id"], c["position"], c["isPK"]) for c in columns_of(entity, agent_index)}


class CaseOnlyColumnsRestoreTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entity()
        cls.exported = export_yaml(cls.original)
        cls.restored = restore(cls.original, cls.exported)

    def test_the_container_holds_two_columns_that_differ_by_case_only(self):
        self.assertEqual(sorted(names(self.original, 0).values()), ["FIRST_NAME", "ID", "_etag", "id"])

    def test_the_export_lists_both_keys_and_both_columns(self):
        table = yaml.safe_load(self.exported)[SCHEMA]["tables"]["custom"][TABLE]
        self.assertEqual(table["keys"], ["ID", "id"])
        rows = [(row["source"], row["target"], row["isPK"]) for row in table["columns"]]
        self.assertEqual(rows,
                         [("FIRST_NAME", "FIRST_NAME", False), ("ID", "ID", True), ("_etag", "_etag", False),
                          ("id", "id", True)])

    def test_the_restore_keeps_both_source_columns_as_they_were(self):
        self.assertEqual(summary(self.restored, 0), summary(self.original, 0))

    def test_the_restore_keeps_both_target_columns_as_they_were(self):
        self.assertEqual(summary(self.restored, 1), summary(self.original, 1))

    def test_each_column_is_mapped_onto_the_column_of_its_spelling(self):
        self.assertEqual(matrix(self.restored), matrix(self.original))
        self.assertIn(("ID", "ID"), matrix(self.restored))
        self.assertIn(("id", "id"), matrix(self.restored))

    def test_the_restored_entity_exports_as_the_original(self):
        self.assertEqual(yaml.safe_load(export_yaml(self.restored)), yaml.safe_load(self.exported))


class CaseFallbackTests(unittest.TestCase):
    """A name no column is spelt as still finds the one column of that name, ignoring case."""

    maxDiff = None

    def test_a_name_with_no_exact_match_finds_the_only_column_of_that_name(self):
        entity = _entity()
        schemas = yaml.safe_load(export_yaml(entity))
        table = schemas[SCHEMA]["tables"]["custom"][TABLE]
        table["keys"] = ["first_name"]
        table["columns"] = [row for row in table["columns"] if row["source"] in ("FIRST_NAME", "_etag")]
        for row in table["columns"]:
            row["source"] = row["source"].lower()
            row["target"] = row["target"].lower()
        discovered = _discovered(entity, 0, {"FIRST_NAME", "_etag"})
        restored = restore(entity, yaml.safe_dump(schemas, sort_keys=False), source_columns=discovered,
                           target_columns=_discovered(entity, 1, {"FIRST_NAME", "_etag"}))
        self.assertEqual(sorted(names(restored, 0).values()), ["FIRST_NAME", "_etag"])
        self.assertEqual(sorted(names(restored, 1).values()), ["FIRST_NAME", "_etag"])
        self.assertEqual(matrix(restored), [("FIRST_NAME", "FIRST_NAME"), ("_etag", "_etag")])


def _flagged(columns, is_pk=False):
    """``columns`` as an agent that flags no primary key reports them (a schemaless source declares none)."""
    return [dict(c, isPK=is_pk) for c in columns]


def _with_keys(exported, keys=None, chain=False):
    schemas = yaml.safe_load(exported)
    table = schemas[SCHEMA]["tables"]["custom"][TABLE]
    if keys is not None:
        table["keys"] = keys
    if chain:
        table.update(chainId="drivers_chain", orderIndex=1)
    return yaml.safe_dump(schemas, sort_keys=False)


def primary_keys(entity, agent_index):
    return sorted(c["name"] for c in columns_of(entity, agent_index) if c["isPK"])


class CaseOnlyKeysTests(unittest.TestCase):
    """A key names the column spelt so; the other column of the same name, case aside, is no key."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.original = _entity()
        cls.exported = export_yaml(cls.original)

    def _restore(self, keys, version, chain=False):
        source, target = (_flagged(_discovered(self.original, i)) for i in (0, 1))
        return restore(self.original, _with_keys(self.exported, keys, chain), source_columns=source,
                       target_columns=target, version=version)

    def test_each_key_is_the_column_of_its_spelling_on_every_core_hub(self):
        for version in (V23, V22):
            for chain in (False, True):
                for keys, expected in ((["id"], ["id"]), (["ID"], ["ID"]), (["ID", "id"], ["ID", "id"])):
                    with self.subTest(version=version, chain=chain, keys=keys):
                        restored = self._restore(keys, version, chain)
                        restored_keys = [primary_keys(restored, 0), primary_keys(restored, 1)]
                        self.assertEqual(restored_keys, [expected] * 2)
                        self.assertEqual(sorted(names(restored, 0).values()), ["FIRST_NAME", "ID", "_etag", "id"])
                        self.assertEqual(sorted(matrix(restored)),
                                         [("FIRST_NAME", "FIRST_NAME"), ("ID", "ID"), ("_etag", "_etag"),
                                          ("id", "id")])

    def test_a_restore_without_column_rows_keeps_both_columns(self):
        schemas = yaml.safe_load(self.exported)
        del schemas[SCHEMA]["tables"]["custom"][TABLE]["columns"]
        restored = restore(self.original, yaml.safe_dump(schemas, sort_keys=False))
        restored_keys = [primary_keys(restored, 0), primary_keys(restored, 1)]
        self.assertEqual(restored_keys, [["ID", "id"]] * 2)
        self.assertEqual(summary(restored, 0), summary(self.original, 0))

    def test_the_target_holds_only_one_of_the_two_columns_yet(self):
        """A collection written to before the restore may not hold both fields: the YAML says there are two."""
        target = [c for c in _discovered(self.original, 1) if c["name"] != "id"]
        restored = restore(self.original, self.exported, target_columns=target)
        self.assertEqual(summary(restored, 1)["ID"], summary(self.original, 1)["ID"])
        self.assertEqual(sorted(summary(restored, 1)), ["FIRST_NAME", "ID", "_etag", "id"])
        self.assertIn(("id", "id"), matrix(restored))

    def test_the_target_holds_none_of_them_yet(self):
        restored = restore(self.original, self.exported, target_columns=[])
        self.assertEqual(sorted(names(restored, 1).values()), ["FIRST_NAME", "ID", "_etag", "id"])
        self.assertEqual(sorted(matrix(restored)), sorted(matrix(self.original)))


class ExportedSourcePositionsTests(unittest.TestCase):
    """:func:`create_all_entities._with_exported_source_positions` with columns that differ by case only."""

    @staticmethod
    def _positions(discovered, exported):
        columns = {"columns": [{"id": i, "name": n, "position": p} for i, (n, p) in enumerate(discovered, 1)]}
        config = {"columns": [{"source": n, "target": n, "ordinalPosition": o} for n, o in exported]}
        result = create_all_entities._with_exported_source_positions(columns, config, "T")
        return {c["name"]: c["position"] for c in result["columns"]}

    def test_a_row_never_gives_its_position_to_two_columns(self):
        # only ``id`` is listed: ``ID`` has an exact row of no one, it keeps the position discovery gave it
        self.assertEqual(self._positions([("ID", 0), ("a", 1), ("id", 2)], [("id", 3), ("a", 1)]),
                         {"ID": 0, "a": 1, "id": 2})
        self.assertEqual(self._positions([("id", 0), ("a", 1), ("ID", 2)], [("a", 1), ("id", 2)]),
                         {"id": 1, "a": 0, "ID": 2})

    def test_a_name_that_is_ambiguous_without_an_exact_match_is_no_column(self):
        self.assertEqual(self._positions([("ID", 0), ("id", 1), ("a", 2)], [("a", 1), ("iD", 2)]),
                         {"ID": 0, "id": 1, "a": 2})

    def test_both_spellings_keep_their_own_rows(self):
        self.assertEqual(self._positions([("id", 0), ("ID", 1), ("a", 2)], [("a", 1), ("ID", 2), ("id", 3)]),
                         {"a": 0, "ID": 1, "id": 2})

    def test_a_row_falls_back_to_the_only_column_of_that_name_case_aside(self):
        self.assertEqual(self._positions([("Id", 0), ("a", 1)], [("a", 1), ("ID", 2)]), {"Id": 1, "a": 0})


class NameMatchingTests(unittest.TestCase):
    """The helpers every lookup of the import goes through."""

    def test_a_name_is_the_column_spelt_so_else_the_only_one_spelt_so_case_aside(self):
        names = entity_mapping.ColumnNames(["ID", "id", "Name", "A", "a", "a "])
        resolved = [names.resolve(n) for n in ("ID", "id", "iD", "name", "NAME", "x", "a ")]
        self.assertEqual(resolved,
                         ["ID", "id", None, "Name", "Name", None, "a "])
        keys = [names.key(n) for n in ("id", "iD", "x", "X")]
        self.assertEqual(keys, ["id", "id", "x", "x"])

    def test_find_by_name_prefers_the_exact_spelling(self):
        columns = [{"name": "ID", "n": 1}, {"name": "id", "n": 2}, {"name": "Name", "n": 3}]
        found = [entity_mapping.find_by_name(columns, n)["n"] for n in ("ID", "id", "NAME")]
        self.assertEqual(found, [1, 2, 3])
        self.assertIsNone(entity_mapping.find_by_name(columns, "iD"))
        self.assertIsNone(entity_mapping.find_by_name(columns, "other"))
        self.assertEqual(entity_mapping.find_by_name([{"name": "ID"}, {"name": "ID"}], "id"), {"name": "ID"})

    @staticmethod
    def _relations(*pairs):
        return [{"sources": [s], "targets": [t], "explicit": True} for s, t in pairs]

    def test_rows_on_columns_that_differ_by_case_are_not_the_same_row(self):
        names = entity_mapping.ColumnNames(["ID", "id"])
        grouped = group_relations(self._relations(("ID", "ID"), ("id", "id")), "T", names, names)
        pairs = [(r["sources"], r["targets"]) for r in grouped]
        self.assertEqual(pairs, [(["ID"], ["ID"]), (["id"], ["id"])])

    def test_without_the_columns_names_are_compared_case_insensitively_as_before(self):
        relations = self._relations(("ID", "ID"), ("id", "id"))
        with self.assertRaisesRegex(EntityMappingError, "listed twice"):
            group_relations(relations, "T")

    def test_the_same_row_listed_twice_is_still_refused(self):
        names = entity_mapping.ColumnNames(["ID", "id"])
        relations = self._relations(("id", "id"), ("id", "id"))
        with self.assertRaisesRegex(EntityMappingError, "listed twice"):
            group_relations(relations, "T", names, names)

    def test_a_name_no_column_is_spelt_as_is_the_column_of_that_name(self):
        names = entity_mapping.ColumnNames(["Name"])
        relations = self._relations(("Name", "Name"), ("name", "name"))
        with self.assertRaisesRegex(EntityMappingError, "listed twice"):
            group_relations(relations, "T", names, names)

    def test_a_source_repeated_over_rows_is_one_source_only_when_spelt_alike(self):
        rows = [{"sources": [s], "targets": [t]} for s, t in (("ID", "a"), ("id", "b"))]
        names = entity_mapping.ColumnNames(["ID", "id"])
        self.assertEqual(grouped_only_forms(rows, None, names), [])
        self.assertEqual(len(grouped_only_forms(rows, None)), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The YAML ``columns:`` grammar of the column mapping (T5 of the alignment plan).

Every form the bootstrapper read before Gluesync 2.3 keeps its meaning (an absent or ``null``
target is the source name, ``{SRC: tgt}`` is 1:1, whitelist rows are 1:1); the explicit form gains
a list-valued ``source`` (N:1), a repeated ``source`` (1:N) and ``target: []`` (1:null), and the
``{target, expression}`` row is a column fed by no source (null:1).
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entity_mapping import EntityMappingError, group_relations, grouped_only_forms, yaml_relations  # noqa: E402


def shapes(relations):
    return [(r["sources"], r["targets"]) for r in relations]


class YamlRelationsTests(unittest.TestCase):
    def test_existing_forms_keep_their_meaning(self):
        rows = [
            {"ID": "id", "NAME": "name"},                 # shorthand, two pairs in one row
            {"source": "EMAIL", "target": "email"},       # explicit
            {"source": "NOTE"},                           # absent target: the source name
            {"source": "CITY", "target": None, "type": "varchar"},
            {"source": None, "target": "x"},              # no source: nothing, as before
        ]
        self.assertEqual(shapes(yaml_relations(rows)), [
            (["ID"], ["id"]),
            (["NAME"], ["name"]),
            (["EMAIL"], ["email"]),
            (["NOTE"], ["NOTE"]),
            (["CITY"], ["CITY"]),
        ])

    def test_whitelist_rows_are_decided_by_the_first_row_as_before(self):
        rows = [
            {"name": "zip", "sourceName": "ZIP", "type": "varchar"},
            {"name": "phone", "type": "varchar"},
        ]
        self.assertEqual(shapes(yaml_relations(rows)), [(["ZIP"], ["zip"]), (["phone"], ["phone"])])

    def test_a_list_that_is_neither_mapping_nor_whitelist_filters_nothing(self):
        self.assertIsNone(yaml_relations([{"name": "zip"}]))
        self.assertIsNone(yaml_relations([]))
        self.assertIsNone(yaml_relations(None))

    def test_shorthand_keeps_metadata_named_columns(self):
        # GSSD-1355: ``id`` is a column name in the shorthand form, not export metadata.
        self.assertEqual(shapes(yaml_relations([{"id": "ID"}])), [(["id"], ["ID"])])

    def test_new_forms(self):
        rows = [
            {"source": ["FIRST_NAME", "LAST_NAME"], "target": "full_name", "expression": {"type": "CustomFn"}},
            {"source": "PHONE", "target": "phone_home"},
            {"source": "PHONE", "target": "phone_work"},
            {"source": "INTERNAL_NOTE", "target": []},
            {"target": "ingest_ts", "type": "timestamp", "expression": {"type": "LocalTs"}},
            {"target": "row_hash", "type": "varchar"},
            {"source": ["ONLY"], "target": "only"},
        ]
        self.assertEqual(shapes(yaml_relations(rows)), [
            (["FIRST_NAME", "LAST_NAME"], ["full_name"]),
            (["PHONE"], ["phone_home"]),
            (["PHONE"], ["phone_work"]),
            (["INTERNAL_NOTE"], []),
            ([], ["ingest_ts"]),
            ([], ["row_hash"]),
            (["ONLY"], ["only"]),
        ])
        self.assertTrue(all(r["explicit"] for r in yaml_relations(rows)))

    def test_n_to_one_needs_a_target(self):
        with self.assertRaisesRegex(EntityMappingError, "FIRST_NAME"):
            yaml_relations([{"source": ["FIRST_NAME", "LAST_NAME"]}], label="src.T")

    def test_a_list_of_targets_is_one_row_per_target(self):
        with self.assertRaisesRegex(EntityMappingError, "one row per target"):
            yaml_relations([{"source": "PHONE", "target": ["a", "b"]}], label="src.T")


class GroupRelationsTests(unittest.TestCase):
    def test_rows_sharing_a_source_are_one_one_to_n(self):
        grouped = group_relations(yaml_relations([
            {"source": "PHONE", "target": "phone_home"},
            {"ID": "id"},
            {"source": "PHONE", "target": "phone_work"},
        ]), label="src.T")
        self.assertEqual(shapes(grouped), [(["PHONE"], ["phone_home", "phone_work"]), (["ID"], ["id"])])

    def test_two_rows_on_one_target_are_refused_with_the_n_to_one_form(self):
        with self.assertRaisesRegex(EntityMappingError, r"full_name of src\.T.*source: \[FIRST_NAME, LAST_NAME\]"):
            group_relations(yaml_relations([{"FIRST_NAME": "full_name"}, {"LAST_NAME": "full_name"}]), label="src.T")

    def test_a_row_listed_twice_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "twice"):
            group_relations(yaml_relations([{"A": "a"}, {"A": "a"}]), label="src.T")

    def test_n_to_one_source_in_another_row_is_m_to_n(self):
        with self.assertRaisesRegex(EntityMappingError, "M:N"):
            group_relations(yaml_relations([
                {"source": ["A", "B"], "target": "ab"},
                {"source": "A", "target": "a"},
            ]), label="src.T")

    def test_one_to_null_and_another_row_on_the_same_source_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "INTERNAL_NOTE"):
            group_relations(yaml_relations([
                {"source": "INTERNAL_NOTE", "target": []},
                {"source": "INTERNAL_NOTE", "target": "note"},
            ]), label="src.T")

    def test_names_compare_case_insensitively(self):
        with self.assertRaisesRegex(EntityMappingError, "several rows"):
            group_relations(yaml_relations([{"A": "x"}, {"B": "X"}]), label="src.T")


class GroupedOnlyFormsTests(unittest.TestCase):
    def test_forms_an_older_corehub_cannot_take(self):
        rows = [
            {"source": ["A", "B"], "target": "ab"},
            {"source": "C", "target": "c1"},
            {"source": "C", "target": "c2"},
            {"source": "D", "target": []},
        ]
        forms = grouped_only_forms(yaml_relations(rows), {"targetKeys": ["ab"]})
        self.assertEqual(len(forms), 4)
        self.assertTrue(any("A, B" in form for form in forms))
        self.assertTrue(any("C" in form for form in forms))
        self.assertTrue(any("D" in form for form in forms))
        self.assertTrue(any("targetKeys" in form for form in forms))

    def test_existing_forms_need_nothing(self):
        rows = [{"ID": "id"}, {"source": "X", "target": "x"}, {"target": "ts", "expression": {"type": "LocalTs"}}]
        self.assertEqual(grouped_only_forms(yaml_relations(rows), {"keys": ["ID"]}), [])


if __name__ == "__main__":
    unittest.main()

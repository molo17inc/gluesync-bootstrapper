#!/usr/bin/env python3
"""The column mapping matrix rules of entity_mapping.py (T1-T4 of the alignment plan).

Gluesync 2.3 stores each entry of ``columnsMappingMatrix`` as two lists of column ids
(``sourceColumnIds``, ``targetColumnIds``); every earlier release stores one pair of ids
(``sourceColumnId``, ``targetColumnId``, ``0`` for "no column"). The rules mirror CoreHub's decoder
and mapping stage (gluesync-kotlin ``EntityType.kt`` ``ColumnMappingMatrixSerializer`` and
``ColumnMappingMatrix``, ``GluesyncMainRoutesOrchestrator.kt`` M1-M7).
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from entity_mapping import (  # noqa: E402
    EntityMappingError,
    build_entries,
    encode_entries,
    read_entry,
    relations_of,
)


def col(name, col_id, position=None):
    return {"name": name, "id": col_id, "position": col_id if position is None else position}


SOURCE = [col("ID", 1), col("FIRST_NAME", 2), col("LAST_NAME", 3), col("NOTE", 4)]
TARGET = [col("id", 11), col("full_name", 12), col("first", 13), col("last", 14), col("ingest_ts", 15)]


def rel(sources, targets, explicit=True):
    return {"sources": list(sources), "targets": list(targets), "explicit": explicit}


class ReadEntryTests(unittest.TestCase):
    """T1: both wire shapes, as CoreHub decodes them."""

    def test_single_pair(self):
        self.assertEqual(read_entry({"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 5, "targetColumnId": 7}),
                         (7, 8, [5], [7]))

    def test_zero_is_no_column(self):
        self.assertEqual(read_entry({"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 5, "targetColumnId": 0})[2:],
                         ([5], []))
        self.assertEqual(read_entry({"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 0, "targetColumnId": 7})[2:],
                         ([], [7]))
        self.assertEqual(read_entry({"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 0, "targetColumnId": 0})[2:],
                         ([], []))

    def test_lists_only(self):
        entry = {"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnIds": [2, 3], "targetColumnIds": [12]}
        self.assertEqual(read_entry(entry), (7, 8, [2, 3], [12]))

    def test_list_next_to_an_agreeing_scalar(self):
        entry = {"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 5, "sourceColumnIds": [5],
                 "targetColumnId": 0, "targetColumnIds": []}
        self.assertEqual(read_entry(entry)[2:], ([5], []))

    def test_disagreeing_scalar_names_the_side(self):
        entry = {"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnId": 5, "targetColumnId": 9,
                 "targetColumnIds": [7]}
        with self.assertRaisesRegex(EntityMappingError, "targetColumnId"):
            read_entry(entry)

    def test_null_list_reads_as_absent(self):
        entry = {"sourceTableObjectId": 7, "targetTableObjectId": 8, "sourceColumnIds": None, "sourceColumnId": 5,
                 "targetColumnIds": [7]}
        self.assertEqual(read_entry(entry)[2:], ([5], [7]))

    def test_missing_side_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "source"):
            read_entry({"sourceTableObjectId": 7, "targetTableObjectId": 8, "targetColumnIds": [7]})


class BuildEntriesTests(unittest.TestCase):
    """T2: grouped entries, R7.4-complete, ordered, and the shapes CoreHub refuses."""

    def build(self, relations, source=SOURCE, target=TARGET, **kwargs):
        return build_entries(100, 200, relations, source, target, grouped=True, label="src.T", **kwargs)

    def test_every_relation_shape_and_completeness(self):
        entries = self.build([
            rel([1], [11]),            # 1:1
            rel([3, 2], [12]),         # N:1, sources in ordinal order
            rel([], [15]),             # null:1, ordered with the targets that have no source
        ])
        self.assertEqual(entries, [
            (100, 200, [1], [11]),
            (100, 200, [2, 3], [12]),
            (100, 200, [4], []),        # unlinked source NOTE: 1:null
            (100, 200, [], [13]),       # unlinked targets, in ordinal order
            (100, 200, [], [14]),
            (100, 200, [], [15]),
        ])

    def test_one_to_n_orders_targets_and_groups_by_first_source(self):
        entries = self.build([rel([3], [14, 13]), rel([1], [11])], target=TARGET[:4])
        self.assertEqual(entries[:2], [(100, 200, [1], [11]), (100, 200, [3], [13, 14])])

    def test_one_to_null_is_one_entry_per_source(self):
        entries = self.build([rel([1, 2], [])], source=SOURCE[:2], target=[])
        self.assertEqual(entries, [(100, 200, [1], []), (100, 200, [2], [])])

    def test_unlocked_table_keeps_only_the_pairing(self):
        self.assertEqual(self.build([rel([1], [11])], unlocked=True), [(100, 200, [], [])])

    def test_m_to_n_is_refused_with_the_names(self):
        with self.assertRaisesRegex(EntityMappingError, r"src\.T.*FIRST_NAME.*LAST_NAME.*first.*last"):
            self.build([rel([2, 3], [13, 14])])

    def test_a_column_in_two_relations_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "full_name"):
            self.build([rel([2], [12]), rel([3], [12])])
        with self.assertRaisesRegex(EntityMappingError, "FIRST_NAME"):
            self.build([rel([2], [13]), rel([2, 3], [12])])

    def test_ids_that_are_no_column_of_the_entity_are_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "99"):
            self.build([rel([99], [11])])
        with self.assertRaisesRegex(EntityMappingError, "0"):
            self.build([rel([1], [0])])

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaisesRegex(EntityMappingError, "more than once"):
            self.build([rel([2, 2], [12])])

    def test_not_grouped_keeps_the_relations_as_given(self):
        relations = [rel([3], [13]), rel([1], [11])]
        self.assertEqual(build_entries(100, 200, relations, SOURCE, TARGET, grouped=False),
                         [(100, 200, [3], [13]), (100, 200, [1], [11])])

    def test_not_grouped_refuses_what_a_pair_cannot_hold(self):
        with self.assertRaisesRegex(EntityMappingError, "older than Gluesync 2.3"):
            build_entries(100, 200, [rel([2, 3], [12])], SOURCE, TARGET, grouped=False, label="src.T")


class EncodeEntriesTests(unittest.TestCase):
    """T3: lists for a 2.3 CoreHub, today's pairs for an older one."""

    def test_grouped_writes_lists_and_no_scalar(self):
        self.assertEqual(encode_entries([(1, 2, [3], []), (1, 2, [], [])], grouped=True), [
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [3], "targetColumnIds": []},
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnIds": [], "targetColumnIds": []},
        ])

    def test_not_grouped_writes_the_pair(self):
        self.assertEqual(encode_entries([(1, 2, [3], [4]), (1, 2, [], [])], grouped=False), [
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnId": 3, "targetColumnId": 4},
            {"sourceTableObjectId": 1, "targetTableObjectId": 2, "sourceColumnId": 0, "targetColumnId": 0},
        ])

    def test_not_grouped_leaves_out_one_sided_entries(self):
        self.assertEqual(encode_entries([(1, 2, [3], []), (1, 2, [], [4])], grouped=False), [])

    def test_not_grouped_refuses_a_grouped_entry(self):
        with self.assertRaises(EntityMappingError):
            encode_entries([(1, 2, [3, 5], [4])], grouped=False)


class RelationsOfTests(unittest.TestCase):
    """T4: the relations an export reads from a stored matrix, whatever its shape."""

    def test_legacy_rows_on_one_target_read_as_one_n_to_one(self):
        entries = [(100, 200, [2], [12]), (100, 200, [3], [12]), (100, 200, [1], [11])]
        relations = relations_of(entries, SOURCE, TARGET)
        self.assertEqual(relations["relations"], [
            {"sources": ["ID"], "targets": ["id"]},
            {"sources": ["FIRST_NAME", "LAST_NAME"], "targets": ["full_name"]},
        ])
        self.assertEqual(relations["unlinked_sources"], ["NOTE"])
        self.assertEqual(relations["unlinked_targets"], ["first", "last", "ingest_ts"])

    def test_grouped_entries_and_one_sided_entries(self):
        entries = [(100, 200, [1], [13, 14]), (100, 200, [4], []), (100, 200, [], [15])]
        relations = relations_of(entries, SOURCE, TARGET)
        self.assertEqual(relations["relations"], [{"sources": ["ID"], "targets": ["first", "last"]}])
        self.assertIn("NOTE", relations["unlinked_sources"])
        self.assertIn("ingest_ts", relations["unlinked_targets"])

    def test_unlocked_table_gives_no_relation(self):
        relations = relations_of([(100, 200, [], [])], SOURCE, TARGET, unlocked=True)
        self.assertEqual(relations, {"relations": [], "unlinked_sources": [], "unlinked_targets": []})


if __name__ == "__main__":
    unittest.main()

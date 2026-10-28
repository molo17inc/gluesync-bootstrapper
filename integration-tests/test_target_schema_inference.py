"""Review #7/#8: snapshot-command schema inference and casefolded target schema comparison."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import table_target_schema as tts  # noqa: E402


def _cfg(*commands):
    return {"preSnapshotCommand": commands[0], **({"postSnapshotCommand": commands[1]} if len(commands) > 1 else {})}


class InferenceTests(unittest.TestCase):
    def test_two_part(self):
        self.assertEqual(tts.inferred_target_schema(_cfg("DELETE FROM [TGT02].[TAB050]"), "TGT01"), "TGT02")

    def test_three_part_names_take_the_schema(self):
        self.assertEqual(tts.inferred_target_schema(_cfg("DELETE FROM DB1.TGT02.TAB050"), "TGT01"), "TGT02")
        self.assertEqual(tts.inferred_target_schema(_cfg("TRUNCATE TABLE [DB 1].[TGT02].[TAB050]"), "TGT01"),
                         "TGT02")

    def test_bracketed_and_quoted_with_spaces(self):
        self.assertEqual(tts.inferred_target_schema(_cfg('DELETE FROM [Tgt Two].[Tab 50]'), "TGT01"), "Tgt Two")
        self.assertEqual(tts.inferred_target_schema(_cfg('DELETE FROM "Tgt Two"."Tab 50"'), "TGT01"), "Tgt Two")

    def test_comments_and_strings_ignored(self):
        command = "-- copied from OTHER.TAB1\nDELETE FROM TGT02.TAB050 /* not SCH9.X */ WHERE C = 'A.B'"
        self.assertEqual(tts.inferred_target_schema(_cfg(command), "TGT01"), "TGT02")

    def test_column_and_alias_references_are_not_schemas(self):
        command = "UPDATE TGT02.TAB050 SET COL1 = 0 WHERE t.COL2 = 1"
        self.assertEqual(tts.inferred_target_schema(_cfg(command), "TGT01"), "TGT02")

    def test_ambiguous_is_skipped(self):
        self.assertIsNone(tts.inferred_target_schema(_cfg("DELETE FROM DB1..TAB050"), "TGT01"))
        self.assertIsNone(tts.inferred_target_schema(_cfg("DELETE FROM SRV.DB1.TGT02.TAB050"), "TGT01"))
        self.assertIsNone(tts.inferred_target_schema(_cfg("DELETE FROM TGT02.A; DELETE FROM TGT03.B"), "TGT01"))
        self.assertIsNone(tts.inferred_target_schema(_cfg("DELETE FROM TGT02.A", "DELETE FROM TGT03.B"), "TGT01"))

    def test_same_schema_other_case_is_not_a_change(self):
        self.assertIsNone(tts.inferred_target_schema(_cfg("DELETE FROM tgt01.TAB050"), "TGT01"))


class CasefoldTests(unittest.TestCase):
    def test_split_casefolds_the_target_schema(self):
        config = {"schemas": {"SCH01": {"target": "TGT01", "tables": {"custom": {
            "TAB001": {"targetSchema": "tgt01"}, "TAB002": {}, "TAB003": {"targetSchema": "TGT02"},
            "TAB004": {"targetSchema": "tgt02"}}}}}}
        passes = tts.split_by_target_schema(config, "SCH01", "TGT01")
        self.assertEqual([t for t, _ in passes], ["TGT01", "TGT02"])
        first = passes[0][1]["schemas"]["SCH01"]["tables"]["custom"]
        second = passes[1][1]["schemas"]["SCH01"]["tables"]["custom"]
        self.assertEqual(sorted(first), ["TAB001", "TAB002"])
        self.assertEqual(sorted(second), ["TAB003", "TAB004"])

    def test_only_case_differs_is_one_pass(self):
        config = {"schemas": {"SCH01": {"target": "TGT01", "tables": {"custom": {
            "TAB001": {"targetSchema": "tgt01"}}}}}}
        self.assertEqual(len(tts.split_by_target_schema(config, "SCH01", "TGT01")), 1)


if __name__ == "__main__":
    unittest.main()

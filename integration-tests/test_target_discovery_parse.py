"""Unit tests for TARGET discovery payload parsers (no Core Hub)."""
import unittest
from unittest import mock

# Import after path setup like other automator tests
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "automator_app"))

# Stub heavy deps before importing corehub
sys.modules.setdefault("requests", mock.MagicMock())

import corehub  # noqa: E402


class TargetDiscoveryParseTests(unittest.TestCase):
    def test_parse_schema_names_dict_and_list(self):
        self.assertEqual(
            corehub._parse_schema_names({"schemas": ["a", {"name": "b"}]}, "p"),
            ["a", "b"],
        )
        self.assertEqual(corehub._parse_schema_names(["x", {"schemaName": "y"}], "p"), ["x", "y"])

    def test_parse_table_names_sorted(self):
        self.assertEqual(
            corehub._parse_table_names([{"tableName": "t2"}, "t1"], "p", "s"),
            ["t1", "t2"],
        )

    def test_parse_column_names(self):
        cols = {"columns": [{"name": "id"}, {"name": "name"}, "raw"]}
        self.assertEqual(corehub._parse_column_names(cols, "p", "s", "t"), ["id", "name", "raw"])
        self.assertEqual(corehub._parse_column_names("bad", "p", "s", "t"), [])


if __name__ == "__main__":
    unittest.main()

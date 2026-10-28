#!/usr/bin/env python3
"""Offline tests for agents.json tag prefix match and documentKey string coercion."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from commons import catalog_type_for_agent_tag, coerce_document_key_config


class CatalogTypeForAgentTagTests(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(catalog_type_for_agent_tag({"mongodb": "NoSQL"}, "mongodb"), "NoSQL")

    def test_hyphen_prefix(self):
        cat = {"couchbase": "NoSQL", "mssql-cdc": "SQL"}
        self.assertEqual(catalog_type_for_agent_tag(cat, "couchbase-dcp"), "NoSQL")
        self.assertEqual(catalog_type_for_agent_tag(cat, "mssql-cdc"), "SQL")

    def test_longest_prefix_wins(self):
        cat = {"redis": "NoSQL", "redis-streams": "NoSQL"}
        self.assertEqual(catalog_type_for_agent_tag(cat, "redis-streams"), "NoSQL")
        # exact key preferred over shorter prefix
        self.assertIs(catalog_type_for_agent_tag(cat, "redis-streams"), cat["redis-streams"])

    def test_unknown(self):
        self.assertIsNone(catalog_type_for_agent_tag({"couchbase": "NoSQL"}, "totally-unknown"))


class CoerceDocumentKeyConfigTests(unittest.TestCase):
    def test_string_shorthand(self):
        self.assertEqual(
            coerce_document_key_config("id"),
            {"keys": ["id"], "prefix": "", "suffix": "", "separator": "-"},
        )

    def test_object_passthrough(self):
        obj = {"keys": ["_id"], "prefix": "p"}
        self.assertIs(coerce_document_key_config(obj), obj)

    def test_empty(self):
        self.assertIsNone(coerce_document_key_config(""))
        self.assertIsNone(coerce_document_key_config(None))


if __name__ == "__main__":
    unittest.main()

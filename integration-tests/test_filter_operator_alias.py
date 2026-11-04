#!/usr/bin/env python3
"""Filter YAML ``operator`` / SQL-ish symbols and top-level ``partitions`` must not be silently dropped."""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import commons  # noqa: E402
import create_all_entities as cae  # noqa: E402


class FilterOperatorAliasTests(unittest.TestCase):
    def test_operation_equal_unchanged(self):
        self.assertEqual(commons._normalize_filter_operation("Equal"), "Equal")

    def test_equals_alias_maps_to_equal(self):
        self.assertEqual(commons._normalize_filter_operation("Equals"), "Equal")

    def test_sql_equals_symbol(self):
        self.assertEqual(commons._normalize_filter_operation("="), "Equal")

    def test_operator_key_accepted(self):
        clause = {"column": "STATUS", "operator": "=", "value": "A", "type": "string"}
        built = commons._build_filter_clause(clause, {"columns": []}, table_id=1,
                                            source_node_info=None, target_node_info=None)
        self.assertIsNotNone(built)
        self.assertEqual(built["operation"]["type"], "Equal")
        self.assertEqual(built["operation"]["filterValue"], "A")

    def test_process_filter_clauses_with_operator(self):
        cfg = {
            "type": "AND",
            "clauses": [
                {"column": "STATUS", "operator": "=", "value": "A", "type": "string"},
                {"column": "AMOUNT", "operation": "GreaterEqual", "value": "10", "type": "int"},
            ],
        }
        out = commons.process_filter_clauses(cfg, {"columns": []}, table_id=7)
        self.assertIsNotNone(out)
        self.assertEqual(len(out["clauses"]), 2)
        self.assertEqual(out["clauses"][0]["operation"]["type"], "Equal")
        self.assertEqual(out["clauses"][1]["operation"]["type"], "GreaterEqual")

    def test_missing_both_operation_and_operator_skipped(self):
        built = commons._build_filter_clause(
            {"column": "STATUS", "value": "A"}, {"columns": []}, 1, None, None
        )
        self.assertIsNone(built)


class TopLevelPartitionsFallbackTests(unittest.TestCase):
    def test_top_level_partitions_used_when_source_omits_them(self):
        run = mock.Mock()
        run.schema.global_source_custom_properties = {}
        run.schema.global_target_custom_properties = {}
        custom = {
            "customProperties": {"source": {}, "target": {}},
            "partitions": {"column": "ID", "maxPartitionsNumber": 4},
        }
        props = cae._table_properties(run, custom, "T", duplicate=False)
        self.assertIsNotNone(props)
        self.assertEqual(props.partition_column, "ID")
        self.assertEqual(props.partition_max, 4)

    def test_source_partitions_win_over_top_level(self):
        run = mock.Mock()
        run.schema.global_source_custom_properties = {}
        run.schema.global_target_custom_properties = {}
        custom = {
            "customProperties": {
                "source": {"partitions": "ORDER_ID"},
                "target": {},
            },
            "partitions": {"column": "ID", "maxPartitionsNumber": 4},
        }
        props = cae._table_properties(run, custom, "T", duplicate=False)
        self.assertEqual(props.partition_column, "ORDER_ID")


if __name__ == "__main__":
    unittest.main()

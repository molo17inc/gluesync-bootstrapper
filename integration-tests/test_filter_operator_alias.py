#!/usr/bin/env python3
"""Filter YAML ``operator`` / SQL-ish symbols and top-level ``partitions`` must not be silently dropped.

Core Hub @SerialName values (gluesync-kotlin EntityType.kt Filter.Operation):
Equal, Greater, Lower, GreaterEqual, LowerEqual, NotEqual, In, NotIn, IsNull, IsNotNull, Regex.
"""

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

    def test_less_than_symbol_maps_to_lower(self):
        self.assertEqual(commons._normalize_filter_operation("<"), "Lower")

    def test_lt_alias_maps_to_lower(self):
        self.assertEqual(commons._normalize_filter_operation("lt"), "Lower")

    def test_lessthan_alias_maps_to_lower(self):
        self.assertEqual(commons._normalize_filter_operation("lessthan"), "Lower")

    def test_LessThan_canonical_maps_to_lower(self):
        self.assertEqual(commons._normalize_filter_operation("LessThan"), "Lower")

    def test_less_equal_symbol_maps_to_lower_equal(self):
        self.assertEqual(commons._normalize_filter_operation("<="), "LowerEqual")

    def test_lte_alias_maps_to_lower_equal(self):
        self.assertEqual(commons._normalize_filter_operation("lte"), "LowerEqual")

    def test_lessequals_alias_maps_to_lower_equal(self):
        self.assertEqual(commons._normalize_filter_operation("lessequals"), "LowerEqual")

    def test_LessThanOrEqual_maps_to_lower_equal(self):
        self.assertEqual(commons._normalize_filter_operation("LessThanOrEqual"), "LowerEqual")

    def test_greater_than_symbol_maps_to_greater(self):
        self.assertEqual(commons._normalize_filter_operation(">"), "Greater")

    def test_GreaterThan_maps_to_greater(self):
        self.assertEqual(commons._normalize_filter_operation("GreaterThan"), "Greater")

    def test_greater_equal_symbol_maps_to_greater_equal(self):
        self.assertEqual(commons._normalize_filter_operation(">="), "GreaterEqual")

    def test_GreaterThanOrEqual_maps_to_greater_equal(self):
        self.assertEqual(commons._normalize_filter_operation("GreaterThanOrEqual"), "GreaterEqual")

    def test_corehub_lower_unchanged(self):
        self.assertEqual(commons._normalize_filter_operation("Lower"), "Lower")

    def test_corehub_lower_equal_unchanged(self):
        self.assertEqual(commons._normalize_filter_operation("LowerEqual"), "LowerEqual")

    def test_operator_key_accepted_with_less_than_symbol(self):
        clause = {"column": "AMOUNT", "operator": "<", "value": "10", "type": "int"}
        built = commons._build_filter_clause(clause, {"columns": []}, table_id=1,
                                            source_node_info=None, target_node_info=None)
        self.assertIsNotNone(built)
        self.assertEqual(built["operation"]["type"], "Lower")
        self.assertEqual(built["operation"]["filterValue"], "10")

    def test_operator_key_accepted_equals(self):
        clause = {"column": "STATUS", "operator": "=", "value": "A", "type": "string"}
        built = commons._build_filter_clause(clause, {"columns": []}, table_id=1,
                                            source_node_info=None, target_node_info=None)
        self.assertIsNotNone(built)
        self.assertEqual(built["operation"]["type"], "Equal")
        self.assertEqual(built["operation"]["filterValue"], "A")

    def test_process_filter_clauses_with_operator_and_greater_equal(self):
        cfg = {
            "type": "AND",
            "clauses": [
                {"column": "STATUS", "operator": "=", "value": "A", "type": "string"},
                {"column": "AMOUNT", "operation": "GreaterThanOrEqual", "value": "10", "type": "int"},
                {"column": "SCORE", "operator": "lte", "value": "100", "type": "int"},
            ],
        }
        out = commons.process_filter_clauses(cfg, {"columns": []}, table_id=7)
        self.assertIsNotNone(out)
        self.assertEqual(len(out["clauses"]), 3)
        self.assertEqual(out["clauses"][0]["operation"]["type"], "Equal")
        self.assertEqual(out["clauses"][1]["operation"]["type"], "GreaterEqual")
        self.assertEqual(out["clauses"][2]["operation"]["type"], "LowerEqual")

    def test_missing_both_operation_and_operator_skipped(self):
        built = commons._build_filter_clause(
            {"column": "STATUS", "value": "A"}, {"columns": []}, 1, None, None
        )
        self.assertIsNone(built)

    def test_every_documented_alias(self):
        cases = {
            "=": "Equal", "==": "Equal", "eq": "Equal", "equals": "Equal",
            "!=": "NotEqual", "<>": "NotEqual", "ne": "NotEqual",
            ">": "Greater", "gt": "Greater", "greaterthan": "Greater", "GreaterThan": "Greater",
            ">=": "GreaterEqual", "gte": "GreaterEqual", "greaterequals": "GreaterEqual",
            "GreaterThanOrEqual": "GreaterEqual",
            "<": "Lower", "lt": "Lower", "lessthan": "Lower", "LessThan": "Lower",
            "<=": "LowerEqual", "lte": "LowerEqual", "lessequals": "LowerEqual",
            "LessThanOrEqual": "LowerEqual",
            "in": "In", "notin": "NotIn", "isnull": "IsNull", "isnotnull": "IsNotNull",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(commons._normalize_filter_operation(raw), expected)


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

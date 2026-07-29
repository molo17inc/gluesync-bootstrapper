#!/usr/bin/env python3
"""
Regression tests for per-column charSet override on source columns.

The character set of a source column is what the data is read with: source agents
publish there the one the column declares — its CCSID as a decimal string on AS400
(Db2 for i) — and declaring a different value in the YAML is the equivalent of
editing it from the UI.

Used by:
- as400-mssql-TRUNCATE-integration-test (AS400 per-column CCSID override)
"""

import copy
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _node_info(default_type="varchar", gs_type="STRING"):
    return {
        "category": "RDBMS",
        "dataTypesMatrix": [
            {
                "gluesyncDataType": gs_type,
                "defaultType": default_type,
                "supportedTypes": [default_type],
            }
        ],
    }


def _source_col(name, col_id, data_type="varchar", is_pk=False, nullable=True, char_set=None):
    col = {
        "id": col_id,
        "position": col_id,
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255,
        "numPrec": 0,
        "numScale": 0,
    }
    if char_set is not None:
        col["charSet"] = char_set
    return col


def _fake_fetch_factory(captured_puts):
    def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
        if path.endswith("/config/entities") and method == "PUT":
            captured_puts.append(copy.deepcopy(body))
            return {"ok": True}
        if path.endswith("/entities"):
            entities = captured_puts[-1]["entities"] if captured_puts else []
            return [{"entity": {"entityId": f"entity-{e['entityName']}", **e}} for e in entities]
        return {}

    return fake_fetch


# ID carries an override too, to prove the keys array is patched like the columns one.
# VAL_KEEP declares the CCSID its file already declares, VAL_UNTOUCHED declares nothing
# and VAL_NULL clears it.
CHAR_SET_EXPLICIT_TEMPLATE = """
INTTEST:
  target: target_schema
  tables:
    whitelist: [CCSIDCHR]
    blacklist: []
    custom:
      CCSIDCHR:
        keys: [ID]
        columns:
          - source: ID
            target: ID
            charSet: 1208
          - source: VAL
            target: VAL
            charSet: "280"
          - source: VAL_KEEP
            target: VAL_KEEP
            charSet: "37"
          - VAL_UNTOUCHED: "VAL_UNTOUCHED"
          - source: VAL_NULL
            target: VAL_NULL
            charSet: null
"""

CHAR_SET_WHITELIST_TEMPLATE = """
INTTEST:
  target: target_schema
  tables:
    whitelist: [CCSIDCHR]
    blacklist: []
    custom:
      CCSIDCHR:
        keys: [ID]
        columns:
          - name: ID
            type: int
          - name: VAL
            sourceName: VAL
            type: char
            charSet: "280"
"""

CHAR_SET_CHAIN_TEMPLATE = """
INTTEST:
  target: target_schema
  tables:
    whitelist:
      - CCSIDCHR
      - CCSIDVAR
    blacklist: []
    custom:
      CCSIDCHR:
        chainId: "ccsid_chain"
        orderIndex: 1
        keys: [ID]
        columns:
          - source: VAL
            target: VAL
            charSet: "280"
      CCSIDVAR:
        chainId: "ccsid_chain"
        orderIndex: 2
        keys: [ID]
        columns:
          - ID: "ID"
          - VAL: "VAL"
"""


def _source_columns():
    return {
        "columns": [
            _source_col("ID", 1, "int", is_pk=True, char_set="1208"),
            _source_col("VAL", 2, "char", char_set="37"),
            _source_col("VAL_KEEP", 3, "char", char_set="37"),
            _source_col("VAL_UNTOUCHED", 4, "char", char_set="37"),
            _source_col("VAL_NULL", 5, "char", char_set="37"),
        ]
    }


def _run_create_entities(yaml_config, captured_puts, get_columns, tables):
    import create_all_entities

    with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
         mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
         mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
         mock.patch.object(create_all_entities, "get_table_columns", side_effect=get_columns), \
         mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
         mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=_fake_fetch_factory(captured_puts)), \
         mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
         mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
         mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
         mock.patch("create_all_entities.handle_udf_function_definition"):
        create_all_entities.create_entities(
            token="tok",
            pipeline_id="pipe",
            source_schema="INTTEST",
            target_schema="target_schema",
            tables=tables,
            source_agent_id="src",
            target_agent_id="tgt",
            source_type="SQL",
            target_type="SQL",
            yaml_config=yaml_config,
            skip_errors=False,
            chunk_size=50,
        )


class CharSetOverrideTests(unittest.TestCase):
    """Verify charSet declared in the YAML lands on the source columns."""

    def test_explicit_mapping_char_set_override(self):
        """
        In explicit source:target mappings the declared charSet must replace the one
        published by discovery, an omitted key must leave it as discovered and an
        explicit null must remove it from the payload.
        """
        yaml_config = yaml.safe_load(CHAR_SET_EXPLICIT_TEMPLATE)
        captured_puts = []

        _run_create_entities(
            yaml_config,
            captured_puts,
            lambda *a, **k: copy.deepcopy(_source_columns()),
            [{"name": "CCSIDCHR", "schema": "INTTEST", "id": 101}],
        )

        entity = captured_puts[0]["entities"][0]
        source_ae = entity["agentEntities"][0]
        by_name = {c["name"]: c for c in source_ae["columns"]}

        self.assertEqual(by_name["VAL"]["charSet"], "280")
        self.assertEqual(by_name["VAL_KEEP"]["charSet"], "37")
        self.assertEqual(by_name["VAL_UNTOUCHED"]["charSet"], "37")
        self.assertNotIn("charSet", by_name["VAL_NULL"])
        # A number in the YAML is a legitimate way to write a CCSID: it must reach the
        # payload as the string the Column model declares.
        self.assertEqual(by_name["ID"]["charSet"], "1208")

    def test_target_columns_keep_their_own_char_set(self):
        """
        The override says how to *read* the source column, so it must not be copied onto
        the target column, whose character set is the target's own business.
        """
        yaml_config = yaml.safe_load(CHAR_SET_EXPLICIT_TEMPLATE)
        captured_puts = []

        _run_create_entities(
            yaml_config,
            captured_puts,
            lambda *a, **k: copy.deepcopy(_source_columns()),
            [{"name": "CCSIDCHR", "schema": "INTTEST", "id": 101}],
        )

        entity = captured_puts[0]["entities"][0]
        target_ae = entity["agentEntities"][1]
        target_val = next(c for c in target_ae["columns"] if c["name"] == "VAL")

        self.assertNotEqual(target_val.get("charSet"), "280")

    def test_whitelist_char_set_override(self):
        """In whitelist format the declared charSet must be applied as well."""
        yaml_config = yaml.safe_load(CHAR_SET_WHITELIST_TEMPLATE)
        captured_puts = []

        _run_create_entities(
            yaml_config,
            captured_puts,
            lambda *a, **k: copy.deepcopy(_source_columns()),
            [{"name": "CCSIDCHR", "schema": "INTTEST", "id": 101}],
        )

        entity = captured_puts[0]["entities"][0]
        source_ae = entity["agentEntities"][0]
        by_name = {c["name"]: c for c in source_ae["columns"]}

        self.assertEqual(by_name["VAL"]["charSet"], "280")
        self.assertEqual(by_name["ID"]["charSet"], "1208")

    def test_multitable_component_char_set_override(self):
        """
        A MultiTable entity builds its columns straight from discovery: the charSet
        declared on a component table must still reach that component's columns, and
        only those.
        """
        yaml_config = yaml.safe_load(CHAR_SET_CHAIN_TEMPLATE)
        captured_puts = []

        columns_by_table = {
            "CCSIDCHR": [
                _source_col("ID", 1, "int", is_pk=True, char_set="1208"),
                _source_col("VAL", 2, "char", char_set="37"),
            ],
            "CCSIDVAR": [
                _source_col("ID", 1, "int", is_pk=True, char_set="1208"),
                _source_col("VAL", 2, "varchar", char_set="37"),
            ],
        }

        def get_cols(token, pipeline_id, agent_id, schema, table):
            return {"columns": copy.deepcopy(columns_by_table.get(table, []))}

        _run_create_entities(
            yaml_config,
            captured_puts,
            get_cols,
            [
                {"name": "CCSIDCHR", "schema": "INTTEST", "id": 101},
                {"name": "CCSIDVAR", "schema": "INTTEST", "id": 102},
            ],
        )

        multi_entity = next(
            e for e in captured_puts[0]["entities"]
            if e["agentEntities"][0].get("type") == "MultiTable"
        )
        # columns is a flat list alternating table header, column list, table header, ...
        columns_by_header = {}
        current_header = None
        for item in multi_entity["agentEntities"][0]["columns"]:
            if isinstance(item, dict):
                current_header = item.get("name")
            elif isinstance(item, list):
                columns_by_header[current_header] = {c["name"]: c for c in item}

        self.assertEqual(columns_by_header["CCSIDCHR"]["VAL"]["charSet"], "280")
        self.assertEqual(columns_by_header["CCSIDVAR"]["VAL"]["charSet"], "37")


if __name__ == "__main__":
    unittest.main()

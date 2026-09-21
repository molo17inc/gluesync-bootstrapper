#!/usr/bin/env python3
"""Regression: plain SQL entity CREATE must keep primary keys after GSSD-1355.

Bootstrapper 2.7.7 (MR !37 Field Functions) broke pgsql agent ITs with:
  Missing primary keys in entity public.drivers for public.drivers

CoreHub derives SingleTable keys from columns where isPK=true. These tests lock
the CREATE path so normal YAML (no field functions) and FF target-only columns
cannot wipe discovery / YAML primary keys.
"""

import copy
import io
import contextlib
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


def _node_info():
    return {
        "category": "RDBMS",
        "dataTypesMatrix": [
            {
                "gluesyncDataType": "STRING",
                "defaultType": "varchar",
                "supportedTypes": ["varchar"],
            }
        ],
    }


def _pgsql_drivers_columns():
    # PostgreSQL folds unquoted identifiers to lowercase.
    return {
        "columns": [
            {
                "id": 1,
                "position": 1,
                "name": "id",
                "dataType": "int4",
                "isPK": True,
                "isNullable": False,
                "isIdentity": False,
            },
            {
                "id": 2,
                "position": 2,
                "name": "first_name",
                "dataType": "varchar",
                "isPK": False,
                "isNullable": True,
                "isIdentity": False,
            },
        ]
    }


class SqlEntityPrimaryKeysRegressionTests(unittest.TestCase):
    def _run_create(self, yaml_text, tables=None, columns_by_table=None):
        import create_all_entities as cae

        yaml_config = yaml.safe_load(yaml_text)
        tables = tables or [{"name": "drivers", "schema": "public", "id": 10}]
        columns_by_table = columns_by_table or {"drivers": _pgsql_drivers_columns()}
        captured = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/config/entities") and method == "PUT":
                captured.append(copy.deepcopy(body))
                return {"ok": True}
            if path.endswith("/entities"):
                ents = captured[-1]["entities"] if captured else []
                return [{"entity": {"entityId": f"e-{i}", **e}} for i, e in enumerate(ents)]
            return {}

        def fake_columns(token, pipeline_id, agent_id, schema, table):
            return copy.deepcopy(columns_by_table.get(table, {"columns": []}))

        with mock.patch.object(cae, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(cae, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(cae, "get_agent_tables", return_value=[]), \
             mock.patch.object(cae, "get_table_columns", side_effect=fake_columns), \
             mock.patch.object(cae, "map_data_type", side_effect=lambda source_type, *a, **k: source_type), \
             mock.patch.object(cae, "fetch_core_hub", side_effect=fake_fetch), \
             contextlib.redirect_stdout(io.StringIO()):
            result = cae.create_entities(
                token="t",
                pipeline_id="p",
                source_schema="public",
                target_schema="public",
                tables=tables,
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                chunk_size=50,
            )
        self.assertEqual(result.get("failed", 0), 0)
        self.assertTrue(captured)
        return captured[0]

    def test_pgsql_drivers_discovery_pk_without_yaml_keys(self):
        """IT tables-list: drivers whitelisted, no custom keys — rely on discovery isPK."""
        yaml_text = """
public:
  target: public
  tables:
    whitelist: ["drivers"]
    blacklist: []
    custom: {}
"""
        payload = self._run_create(yaml_text)
        entity = payload["entities"][0]
        self.assertEqual(entity["entityName"], "public.drivers")
        source_ae, target_ae = entity["agentEntities"]
        src_pks = [c["name"] for c in source_ae["columns"] if c.get("isPK")]
        tgt_pks = [c["name"] for c in target_ae["columns"] if c.get("isPK")]
        self.assertEqual(src_pks, ["id"])
        self.assertEqual(tgt_pks, ["id"])
        self.assertTrue(all(isinstance(c.get("isPK"), bool) for c in source_ae["columns"]))

    def test_pgsql_uppercase_yaml_keys_with_lowercase_discovery(self):
        """YAML keys: [ID] must paint isPK on discovered lowercase id (pgsql)."""
        yaml_text = """
public:
  target: public
  tables:
    whitelist: ["drivers"]
    blacklist: []
    custom:
      drivers:
        keys:
          - ID
"""
        cols = _pgsql_drivers_columns()
        # Simulate source that does not advertise PK (common edge case)
        for c in cols["columns"]:
            c["isPK"] = False
        payload = self._run_create(yaml_text, columns_by_table={"drivers": cols})
        source_ae, target_ae = payload["entities"][0]["agentEntities"]
        self.assertTrue(next(c for c in source_ae["columns"] if c["name"] == "id")["isPK"])
        self.assertTrue(next(c for c in target_ae["columns"] if c["name"] == "id")["isPK"])

    def test_target_only_field_function_does_not_wipe_discovery_pks(self):
        """FF target-only column entries must not empty columns_def / strip PKs."""
        yaml_text = """
public:
  target: public
  tables:
    whitelist: ["drivers"]
    blacklist: []
    custom:
      drivers:
        columns:
          - target: ingested_at
            type: varchar
            expression:
              type: LocalTs
              pattern: "yyyy-MM-dd"
"""
        payload = self._run_create(yaml_text)
        source_ae, target_ae = payload["entities"][0]["agentEntities"]
        src_pks = [c["name"] for c in source_ae["columns"] if c.get("isPK")]
        tgt_pks = [c["name"] for c in target_ae["columns"] if c.get("isPK")]
        self.assertEqual(src_pks, ["id"], f"source columns={source_ae['columns']}")
        self.assertEqual(tgt_pks, ["id"], f"target columns={target_ae['columns']}")
        # Field function should be attached when target column exists after fallback
        # (ingested_at may not be in discovery — FF attach requires target column id)
        self.assertTrue(any(c.get("isPK") for c in source_ae["columns"]))

    def test_extract_mapping_pairs_skips_target_only_ff(self):
        import create_all_entities as cae

        self.assertEqual(
            cae._extract_mapping_pairs(
                {"target": "ingested_at", "type": "varchar", "expression": {"type": "LocalTs"}}
            ),
            [],
        )
        self.assertEqual(
            cae._extract_mapping_pairs({"ID": "id"}),
            [("ID", "id")],
        )
        self.assertEqual(
            cae._extract_mapping_pairs({"source": "id", "target": "id", "type": "int4"}),
            [("id", "id")],
        )


if __name__ == "__main__":
    unittest.main()

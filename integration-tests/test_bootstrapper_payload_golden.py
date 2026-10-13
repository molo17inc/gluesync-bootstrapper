#!/usr/bin/env python3
"""Golden entity payloads for a CoreHub older than Gluesync 2.3.

Every scenario runs ``create_entities`` on a fake CoreHub that reports 2.2.11 and compares the
``PUT /pipelines/{id}/config/entities`` bodies with the ones recorded in
``fixtures/payload_golden/<scenario>.json``. The recordings were taken from the bootstrapper
before the Gluesync 2.3 alignment, so a difference is a change of what a 2.2.x CoreHub receives
(RK-2 of the alignment plan). Bodies are compared as parsed JSON: list order is checked, dict key
order is not.

Re-record after an intended change with ``UPDATE_PAYLOAD_GOLDEN=1``, and say in the commit why
the payload changed.
"""

import json
import logging
import os
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import yaml  # noqa: E402

from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402

GOLDEN_DIR = TESTS_DIR / "fixtures" / "payload_golden"
UPDATE = os.getenv("UPDATE_PAYLOAD_GOLDEN") == "1"

SOURCE_TABLES = {
    ("src", "CUSTOMERS"): [
        column("ID", 1, "int", is_pk=True, nullable=False),
        column("NAME", 2),
        column("EMAIL", 3),
        column("CREATED", 4, "datetime"),
    ],
    ("src", "NOPK"): [column("CODE", 1), column("DESCR", 2)],
    ("src", "ORDERS"): [
        column("ORDER_ID", 1, "int", is_pk=True, nullable=False),
        column("CUSTOMER_ID", 2, "int"),
        column("TOTAL", 3, "decimal"),
        column("NOTE", 4),
    ],
    ("src", "PARTS"): [column("PART_ID", 1, "int", is_pk=True, nullable=False), column("LABEL", 2)],
}

TARGET_TABLES = {
    ("tgt", "customers"): [
        column("id", 101, "int", is_pk=True, nullable=False),
        column("name", 102),
        column("email_address", 103),
        column("created", 104, "timestamp"),
        column("ingest_ts", 105, "timestamp"),
    ],
    ("tgt", "ORDERS_T"): [
        column("ROW_ID", 201, "bigint", is_pk=True, nullable=False, isIdentity=True),
        column("ORDER_ID", 202, "int"),
        column("CUSTOMER_ID", 203, "int"),
        column("TOTAL", 204, "decimal"),
    ],
}


def _source_list(*names):
    return [{"name": name, "schema": "src", "id": 500 + idx} for idx, name in enumerate(names)]


SCENARIOS = {
    "discovery_only": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [PARTS]
    custom:
      PARTS: {}
""",
        tables=("PARTS",),
    ),
    "shorthand_renames_and_target_only_row": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
        columns:
          - ID: id
          - NAME: name
          - EMAIL: email_address
          - target: ingest_ts
            type: timestamp
            expression:
              type: LocalTs
""",
        tables=("CUSTOMERS",),
    ),
    "explicit_rows_target_with_own_key": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [ORDERS]
    custom:
      ORDERS:
        name: ORDERS_T
        keys: [ORDER_ID]
        columns:
          - {source: ORDER_ID, target: ORDER_ID, type: int}
          - {source: CUSTOMER_ID, target: CUSTOMER_ID, type: int}
          - {source: TOTAL, target: TOTAL, type: decimal, expression: {type: StaticBD, value: "0"}}
""",
        tables=("ORDERS",),
    ),
    "whitelist_rows_yaml_keys_without_physical_key": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [NOPK]
    custom:
      NOPK:
        keys: [CODE]
        columns:
          - {name: code, sourceName: CODE, type: varchar}
          - {name: descr, sourceName: DESCR, type: varchar}
""",
        tables=("NOPK",),
    ),
    "unknown_source": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
        columns:
          - ID: id
          - EMAIL: email_address
          - MISSING_COL: created
""",
        tables=("CUSTOMERS",),
    ),
    # A source repeated over rows (1:N) is a Gluesync 2.3 form: refused before any request on an older CoreHub,
    # where it used to send two source columns with one id.
    "repeated_source_refused": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
        columns:
          - ID: id
          - EMAIL: email_address
          - EMAIL: name
""",
        tables=("CUSTOMERS",),
    ),
    "unlocked_schema_with_target_only_columns": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [PARTS]
    custom:
      PARTS:
        keys: [PART_ID]
        unlockedSchema: true
        targetOnlyColumns:
          - CURRENCY
          - {name: CREATED_AT, type: datetime, dataLength: 19, numericPrecision: 0, numericScale: 0, isNullable: false}
        customProperties:
          target:
            udf:
              - {name: UDF_PARTS, type: Java}
""",
        tables=("PARTS",),
    ),
    "duplicate_table_with_filter_and_rename": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        name: customers
      CUSTOMERS@@2:
        sourceTableName: CUSTOMERS
        name: CUSTOMERS_COPY
        entityName: src.CUSTOMERS copy
        columns:
          - ID: ID
          - NAME: FULL_NAME
""",
        tables=("CUSTOMERS",),
    ),
    "multitable_chain_with_filter_and_target_only": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [ORDERS, PARTS]
    custom:
      ORDERS:
        chainId: chain1
        entityName: src.ORDERS chain
        columns:
          - {source: ORDER_ID, target: ORDER_ID}
          - {source: TOTAL, target: AMOUNT}
        customProperties:
          target:
            targetOnlyColumns:
              - SYNCED_AT
      PARTS:
        chainId: chain1
""",
        tables=("ORDERS", "PARTS"),
    ),
    "nosql_target": dict(
        yaml="""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS]
    custom:
      CUSTOMERS:
        keys: [ID]
        documentKey:
          keys: [ID]
""",
        tables=("CUSTOMERS",),
        target_type="NoSQL",
    ),
}


def _demo_kit_scenario(template, source_schema, target_schema, target_type="RDBMS"):
    """A gs-2 demo-kit template of test_bootstrapper_core_yaml_outputs, with its discovery on both sides."""
    from test_bootstrapper_core_yaml_outputs import _columns_for

    config = yaml.safe_load(template)
    tables = config[source_schema]["tables"]["whitelist"]
    return dict(
        yaml=template,
        tables=tables,
        source_schema=source_schema,
        target_schema=target_schema,
        source_tables={(source_schema, t): _columns_for(t)["columns"] for t in tables},
        target_tables={(target_schema, t): _columns_for(t)["columns"] for t in tables},
        source_list=[{"name": t, "schema": source_schema, "id": 100 + idx} for idx, t in enumerate(tables)],
        target_type=target_type,
    )


def _demo_kit_scenarios():
    from test_bootstrapper_core_yaml_outputs import (
        MSSQL_COUCHBASE_DOCUMENT_KEY_TEMPLATE,
        MYSQL_MYSQL_TABLES_TEMPLATE,
        MYSQL_VERTICA_CHRONOS_TEMPLATE,
    )

    return {
        "demo_kit_mysql8_mysql8": _demo_kit_scenario(MYSQL_MYSQL_TABLES_TEMPLATE, "demo", "demo"),
        "demo_kit_mssql_couchbase_document_key": _demo_kit_scenario(
            MSSQL_COUCHBASE_DOCUMENT_KEY_TEMPLATE, "dbo", "dbo", target_type="NoSQL"),
        "demo_kit_mysql8_vertica_chronos": _demo_kit_scenario(MYSQL_VERTICA_CHRONOS_TEMPLATE, "demo", "public"),
    }


SCENARIOS.update(_demo_kit_scenarios())


def run_scenario(name):
    scenario = SCENARIOS[name]
    target_type = scenario.get("target_type", "RDBMS")
    hub = FakeCoreHub(
        scenario.get("source_tables", SOURCE_TABLES),
        scenario.get("target_tables", TARGET_TABLES),
        version="2.2.11.5 - build golden",
        target_category="NoSQL" if target_type == "NoSQL" else "RDBMS",
    )
    run_create_entities(
        hub, scenario["yaml"], scenario.get("source_list") or _source_list(*scenario["tables"]),
        source_schema=scenario.get("source_schema", "src"), target_schema=scenario.get("target_schema", "tgt"),
        target_type=target_type,
    )
    return hub.puts


class PayloadGoldenTests(unittest.TestCase):
    maxDiff = None

    def _check(self, name):
        puts = json.loads(json.dumps(run_scenario(name)))
        path = GOLDEN_DIR / f"{name}.json"
        if UPDATE:
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(puts, indent=2, sort_keys=True) + "\n")
        self.assertTrue(path.exists(), f"missing golden payload {path}; record it with UPDATE_PAYLOAD_GOLDEN=1")
        self.assertEqual(puts, json.loads(path.read_text()))


def _make_test(name):
    return lambda self: self._check(name)


for _name in SCENARIOS:
    setattr(PayloadGoldenTests, f"test_{_name}", _make_test(_name))


if __name__ == "__main__":
    unittest.main()

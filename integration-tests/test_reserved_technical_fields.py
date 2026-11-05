#!/usr/bin/env python3
"""Reserved Core Hub technical fields require agent technicalFields (discovery), else clear refuse.

Synthetic fixtures only. Portal: BaseAgentService appends getTechnicalFields only for names listed
in specificConfiguration.technicalFields. Automator must not synthesise missing tech columns —
that would map fields the agent does not produce. When discovery returns them (fields enabled),
mapping works; when not, refuse with an enable-technicalFields message. Doc alias ``_RRN_COLUMN``
normalises to ``_RRN``.
"""

import logging
import sys
import unittest
from pathlib import Path

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for path in (TESTS_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402
from technical_fields import (  # noqa: E402
    TRANSACTION_ID,
    TRANSACTION_OPERATION,
    TRANSACTION_TIMESTAMP,
    TRANSACTION_USER,
    AS400_RRN,
    canonical_technical_field_name,
    is_reserved_technical_field,
    missing_technical_field_message,
    reserved_technical_column,
    reserved_technical_field_id,
    to64_bit_id,
)
from entity_mapping import EntityMappingError  # noqa: E402
from create_all_entities import _assemble_source_columns  # noqa: E402

V23 = "2.3.0.0 - build test"
V22 = "2.2.9.0 - build test"

# Commons Constants.TRANSACTION_OPERATION_ID (verified against exported backups).
EXPECTED_OPERATION_ID = -5166680671020418130

PHYSICAL = [
    column("ROW_ID", 1, "int", is_pk=True, nullable=False),
    column("PAYLOAD", 2, "varchar"),
]

# Discovery shape when the source agent has technicalFields enabled for the op flag.
TECH_OPERATION = column(
    TRANSACTION_OPERATION, EXPECTED_OPERATION_ID, "CHAR", position=3, nullable=True,
)
TECH_RRN = column(AS400_RRN, to64_bit_id(AS400_RRN), "BIG_INT", position=4, nullable=True)

SOURCE_WITHOUT_TECH = {("src", "AUDIT"): list(PHYSICAL)}
SOURCE_WITH_TECH = {("src", "AUDIT"): list(PHYSICAL) + [dict(TECH_OPERATION)]}
SOURCE_WITH_RRN = {("src", "AUDIT"): list(PHYSICAL) + [dict(TECH_RRN)]}

TARGET_TABLES = {
    ("tgt", "AUDIT"): [
        column("ROW_ID", 11, "int", is_pk=True, nullable=False),
        column("PAYLOAD", 12, "varchar"),
        column("OP_FLAG", 13, "char"),
        column("CREATED_BY", 14, "varchar"),
        column("RRN_OUT", 15, "bigint"),
    ],
}


def _source_list(*names):
    return [{"name": name, "schema": "src", "id": 500 + idx} for idx, name in enumerate(names)]


def _run(yaml_text, *, source_tables, version=V23, skip_errors=True):
    hub = FakeCoreHub(source_tables, TARGET_TABLES, version=version)
    result = run_create_entities(
        hub, yaml_text, _source_list("AUDIT"), source_type="RDBMS", target_type="RDBMS",
        skip_errors=skip_errors,
    )
    entities = {entity["entityName"]: entity for put in hub.puts for entity in put["entities"]}
    return hub, result, entities


def _table(body: str) -> str:
    return f"""
src:
  target: tgt
  tables:
    whitelist: [AUDIT]
    custom:
      AUDIT:
{body}
"""


class To64BitIdTests(unittest.TestCase):
    def test_transaction_operation_id_matches_commons_constants(self):
        self.assertEqual(to64_bit_id(TRANSACTION_OPERATION), EXPECTED_OPERATION_ID)
        self.assertEqual(reserved_technical_field_id(TRANSACTION_OPERATION), EXPECTED_OPERATION_ID)

    def test_other_commons_names_are_stable(self):
        for name in (TRANSACTION_ID, TRANSACTION_TIMESTAMP, TRANSACTION_USER, AS400_RRN):
            self.assertTrue(is_reserved_technical_field(name))
            self.assertEqual(to64_bit_id(name), reserved_technical_field_id(name))

    def test_unknown_name_is_not_reserved(self):
        self.assertFalse(is_reserved_technical_field("MISSING_COL"))
        self.assertIsNone(reserved_technical_column("MISSING_COL"))
        self.assertIsNone(canonical_technical_field_name("MISSING_COL"))

    def test_rrn_column_doc_alias_normalises_to_rrn(self):
        self.assertEqual(canonical_technical_field_name("_RRN_COLUMN"), AS400_RRN)
        self.assertTrue(is_reserved_technical_field("_RRN_COLUMN"))
        self.assertEqual(reserved_technical_field_id("_RRN_COLUMN"), to64_bit_id(AS400_RRN))

    def test_yaml_type_preferred_over_default(self):
        col = reserved_technical_column(
            TRANSACTION_OPERATION,
            position=9,
            yaml_row={"type": "CHARACTER", "isNullable": False, "ordinalPosition": 34},
        )
        self.assertEqual(col["dataType"], "CHARACTER")
        self.assertEqual(col["id"], EXPECTED_OPERATION_ID)
        self.assertEqual(col["position"], 34)
        self.assertFalse(col["isNullable"])


class AssembleReservedTechFieldTests(unittest.TestCase):
    def test_grouped_accepts_operation_when_discovery_has_it(self):
        discovery = {"columns": list(SOURCE_WITH_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"source": "ROW_ID", "target": "ROW_ID", "type": "int", "isPK": True},
                {"source": "PAYLOAD", "target": "PAYLOAD", "type": "varchar"},
                {
                    "source": TRANSACTION_OPERATION,
                    "target": [],
                    "type": "CHARACTER",
                    "id": EXPECTED_OPERATION_ID,
                    "ordinalPosition": 3,
                },
            ],
            "keys": ["ROW_ID"],
        }
        columns_def, keys, *_rest = _assemble_source_columns(
            discovery, custom, "AUDIT", "src", grouped=True,
        )
        names = [c["name"] for c in columns_def]
        self.assertEqual(names, ["ROW_ID", "PAYLOAD", TRANSACTION_OPERATION])
        op = next(c for c in columns_def if c["name"] == TRANSACTION_OPERATION)
        self.assertEqual(op["id"], EXPECTED_OPERATION_ID)
        self.assertEqual([k["name"] for k in keys], ["ROW_ID"])

    def test_grouped_refuses_missing_tech_field_with_technicalfields_message(self):
        discovery = {"columns": list(SOURCE_WITHOUT_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"source": "ROW_ID", "target": "ROW_ID"},
                {"source": TRANSACTION_OPERATION, "target": "OP_FLAG"},
            ],
            "keys": ["ROW_ID"],
        }
        with self.assertRaises(EntityMappingError) as ctx:
            _assemble_source_columns(discovery, custom, "AUDIT", "src", grouped=True)
        self.assertIn("technicalFields", str(ctx.exception))
        self.assertIn(TRANSACTION_OPERATION, str(ctx.exception))
        self.assertEqual(str(ctx.exception), missing_technical_field_message("AUDIT", TRANSACTION_OPERATION))

    def test_grouped_refuses_truly_unknown_source(self):
        discovery = {"columns": list(SOURCE_WITHOUT_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"source": "ROW_ID", "target": "ROW_ID"},
                {"source": "NO_SUCH_COLUMN", "target": "X"},
            ],
            "keys": ["ROW_ID"],
        }
        with self.assertRaisesRegex(EntityMappingError, r"NO_SUCH_COLUMN.*discovery"):
            _assemble_source_columns(discovery, custom, "AUDIT", "src", grouped=True)

    def test_whitelist_resolves_tech_field_from_discovery(self):
        discovery = {"columns": list(SOURCE_WITH_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"name": "ROW_ID", "type": "int", "sourceName": "ROW_ID"},
                {"name": "OP", "type": "CHAR", "sourceName": TRANSACTION_OPERATION},
            ],
            "keys": ["ROW_ID"],
        }
        columns_def, keys, *_rest = _assemble_source_columns(
            discovery, custom, "AUDIT", "src", grouped=True,
        )
        op = next(c for c in columns_def if c["name"] == TRANSACTION_OPERATION)
        self.assertEqual(op["id"], EXPECTED_OPERATION_ID)
        self.assertEqual(op.get("alias"), "OP")

    def test_whitelist_refuses_missing_tech_field(self):
        discovery = {"columns": list(SOURCE_WITHOUT_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"name": "ROW_ID", "type": "int", "sourceName": "ROW_ID"},
                {"name": "OP", "type": "CHAR", "sourceName": TRANSACTION_OPERATION},
            ],
            "keys": ["ROW_ID"],
        }
        with self.assertRaisesRegex(EntityMappingError, r"technicalFields"):
            _assemble_source_columns(discovery, custom, "AUDIT", "src", grouped=True)

    def test_whitelist_unknown_physical_still_warns_and_skips(self):
        discovery = {"columns": list(SOURCE_WITHOUT_TECH[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"name": "ROW_ID", "type": "int", "sourceName": "ROW_ID"},
                {"name": "GHOST", "type": "varchar", "sourceName": "NO_SUCH_COLUMN"},
            ],
            "keys": ["ROW_ID"],
        }
        columns_def, keys, *_rest = _assemble_source_columns(
            discovery, custom, "AUDIT", "src", grouped=True,
        )
        self.assertEqual([c["name"] for c in columns_def], ["ROW_ID"])

    def test_rrn_column_alias_matches_discovered_rrn(self):
        discovery = {"columns": list(SOURCE_WITH_RRN[("src", "AUDIT")])}
        custom = {
            "columns": [
                {"source": "ROW_ID", "target": "ROW_ID"},
                {"source": "_RRN_COLUMN", "target": "RRN_OUT"},
            ],
            "keys": ["ROW_ID"],
        }
        columns_def, *_rest = _assemble_source_columns(
            discovery, custom, "AUDIT", "src", grouped=True,
        )
        self.assertIn(AS400_RRN, [c["name"] for c in columns_def])
        self.assertNotIn("_RRN_COLUMN", [c["name"] for c in columns_def])


class EntityPayloadReservedTechFieldTests(unittest.TestCase):
    def test_insert_only_1null_when_discovery_has_operation(self):
        yaml_text = _table(f"""        columns:
          - source: ROW_ID
            target: ROW_ID
            type: int
            isPK: true
          - source: PAYLOAD
            target: PAYLOAD
            type: varchar
          - source: {TRANSACTION_OPERATION}
            target: []
            type: CHARACTER
            id: {EXPECTED_OPERATION_ID}
        keys: [ROW_ID]
""")
        _, result, entities = _run(yaml_text, source_tables=SOURCE_WITH_TECH)
        self.assertEqual(result["successful"], 1, result)
        entity = entities["src.AUDIT"]
        source, target = entity["agentEntities"]
        src_names = [c["name"] for c in source["columns"]]
        tgt_names = [c["name"] for c in target["columns"]]
        self.assertIn(TRANSACTION_OPERATION, src_names)
        self.assertNotIn(TRANSACTION_OPERATION, tgt_names)
        op = next(c for c in source["columns"] if c["name"] == TRANSACTION_OPERATION)
        self.assertEqual(op["id"], EXPECTED_OPERATION_ID)

    def test_mapped_operation_1n_when_discovery_has_it(self):
        # 1:N onto two targets from the same reserved tech source (no CustomFn — Hub expression
        # types with isTechnicalField are separate; CDC op comes only through _TRANSACTION_OPERATION).
        yaml_text = _table(f"""        columns:
          - source: ROW_ID
            target: ROW_ID
            type: int
            isPK: true
          - source: PAYLOAD
            target: PAYLOAD
            type: varchar
          - source: {TRANSACTION_OPERATION}
            target: CREATED_BY
            type: CHARACTER
            id: {EXPECTED_OPERATION_ID}
          - source: {TRANSACTION_OPERATION}
            target: OP_FLAG
            type: CHARACTER
            id: {EXPECTED_OPERATION_ID}
        keys: [ROW_ID]
""")
        _, result, entities = _run(yaml_text, source_tables=SOURCE_WITH_TECH)
        self.assertEqual(result["successful"], 1, result)
        entity = entities["src.AUDIT"]
        source, target = entity["agentEntities"]
        op_cols = [c for c in source["columns"] if c["name"] == TRANSACTION_OPERATION]
        self.assertEqual(len(op_cols), 1)
        self.assertEqual(op_cols[0]["id"], EXPECTED_OPERATION_ID)
        tgt_names = {c["name"] for c in target["columns"]}
        self.assertTrue({"CREATED_BY", "OP_FLAG"} <= tgt_names)

    def test_missing_tech_field_refuses_entity_on_23(self):
        yaml_text = _table(f"""        columns:
          - source: ROW_ID
            target: ROW_ID
          - source: {TRANSACTION_OPERATION}
            target: OP_FLAG
            type: CHAR
        keys: [ROW_ID]
""")
        _, result, entities = _run(yaml_text, source_tables=SOURCE_WITHOUT_TECH)
        self.assertEqual(result["successful"], 0, result)
        self.assertEqual(entities, {})
        self.assertTrue(any("technicalFields" in e for e in result["errors"]), result["errors"])

    def test_older_corehub_also_refuses_missing_tech_field(self):
        yaml_text = _table(f"""        columns:
          - source: ROW_ID
            target: ROW_ID
          - source: {TRANSACTION_OPERATION}
            target: OP_FLAG
            type: CHAR
        keys: [ROW_ID]
""")
        _, result, entities = _run(yaml_text, source_tables=SOURCE_WITHOUT_TECH, version=V22)
        self.assertEqual(result["successful"], 0, result)
        self.assertEqual(entities, {})
        self.assertTrue(any("technicalFields" in e for e in result["errors"]), result["errors"])

    def test_older_corehub_maps_when_discovery_has_operation(self):
        yaml_text = _table(f"""        columns:
          - source: ROW_ID
            target: ROW_ID
          - source: {TRANSACTION_OPERATION}
            target: OP_FLAG
            type: CHAR
        keys: [ROW_ID]
""")
        _, result, entities = _run(yaml_text, source_tables=SOURCE_WITH_TECH, version=V22)
        self.assertEqual(result["successful"], 1, result)
        source = entities["src.AUDIT"]["agentEntities"][0]
        op = next(c for c in source["columns"] if c["name"] == TRANSACTION_OPERATION)
        self.assertEqual(op["id"], EXPECTED_OPERATION_ID)


if __name__ == "__main__":
    unittest.main()

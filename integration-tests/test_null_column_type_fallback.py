#!/usr/bin/env python3
"""
Tests for GSSD-956 – null column-type fallback during export and import.

Background
----------
Old GlueSync instances stored ``Column`` with a ``DataTypeInterface`` field
that was not registered for kotlinx.serialization.  The field was silently
omitted from the JSON blob persisted in SQLite, so the GET /entities endpoint
returned columns with no ``dataType`` key at all.  The Automator exporter
wrote ``type: null`` for every column mapping, and the importer subsequently
crashed with ``KeyError: 'type'`` while building entity payloads.

The fix adds two layers of protection:

1. **Export** – ``enrich_null_column_types_from_discovery`` detects tables
   where every exported column type is null and calls the CoreHub live
   discovery endpoint to backfill the types before the YAML is written.

2. **Import** – ``create_entities`` detects all-null YAML columns early,
   builds a ``_discovery_type_by_source`` lookup from the already-fetched
   source columns, and uses it as a fallback in the mapping branch so that
   ``resolved_target_type`` is never silently null.

Additionally, ``col.get('type', 'varchar')`` was replaced with
``col.get('type') or 'varchar'`` in both ``create_all_entities.py`` and
``create_all_tables.py`` so that an *explicit* ``null`` value (Python
``None``) is treated the same as a missing key and falls through to the
``varchar`` default instead of propagating ``None`` downstream.
"""

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _make_oracle_col(name: str, col_id: int, data_type: str = "varchar2") -> dict:
    return {
        "name": name,
        "id": col_id,
        "dataType": data_type,
        "isPK": False,
        "isNullable": False,
        "isIdentity": False,
        "position": col_id,
    }


def _make_pg_col(name: str, col_id: int, data_type: str = "varchar") -> dict:
    return {
        "name": name,
        "id": col_id,
        "dataType": data_type,
        "isPK": False,
        "isNullable": False,
        "isIdentity": False,
        "position": col_id,
    }


def _null_type_yaml_col(source: str, target: str, col_id: int) -> dict:
    """Mimics a column mapping entry exported from a legacy entity (type=null)."""
    return {
        "source": source,
        "target": target,
        "type": None,          # ← the legacy bug: DataTypeInterface not serialised
        "dataLength": 0,
        "numericPrecision": 0,
        "numericScale": 0,
        "isNullable": False,
        "isPK": False,
        "id": col_id,
        "ordinalPosition": 1,
    }


def _minimal_node_info(default_type: str = "varchar2", gs_type: str = "STRING") -> dict:
    return {
        "dataTypesMatrix": [
            {
                "gluesyncDataType": gs_type,
                "defaultType": default_type,
                "supportedTypes": [default_type],
            }
        ],
        "implementedEntityTypes": ["SingleTable"],
        "maxAliasLength": 2147483647,
        "allowedBulkMode": "BOTH",
        "databaseStructureDefinition": [
            {"key": "schema"},
            {"key": "table"},
        ],
    }


# ---------------------------------------------------------------------------
# 1.  Export-side: enrich_null_column_types_from_discovery
# ---------------------------------------------------------------------------

class EnrichNullColumnTypesTests(unittest.TestCase):
    """Unit tests for the export enrichment helper."""

    @classmethod
    def setUpClass(cls):
        from export_template_from_corehub import enrich_null_column_types_from_discovery
        cls.enrich = staticmethod(enrich_null_column_types_from_discovery)

    # ------------------------------------------------------------------
    def _make_schemas_with_null_types(self, table: str = "MY_TABLE",
                                      schema: str = "SYSADM",
                                      cols: list | None = None) -> dict:
        cols = cols or [
            _null_type_yaml_col("COL_A", "col_a", 1),
            _null_type_yaml_col("COL_B", "col_b", 2),
        ]
        return {
            schema: {
                "target": "target_schema",
                "tables": {
                    "whitelist": [table],
                    "custom": {
                        table: {
                            "name": table.lower(),
                            "entityName": f"{schema}.{table}",
                            "columns": cols,
                            "customProperties": {},
                        }
                    },
                },
            }
        }

    def _make_entity_with_source_agent(self, agent_id: str = "src-agent-001") -> list:
        return [
            {
                "entityId": "ent-1",
                "entityName": "SYSADM.MY_TABLE",
                "agentEntities": [
                    {
                        "agentId": agent_id,
                        "entityType": {"type": "Source"},
                        "columns": [],
                    }
                ],
            }
        ]

    # ------------------------------------------------------------------
    def test_backfills_null_types_from_discovery(self):
        """When all column types are null, discovery is called and types are filled."""
        schemas = self._make_schemas_with_null_types()
        entities = self._make_entity_with_source_agent()
        discovery_resp = {
            "columns": [
                _make_oracle_col("COL_A", 1, "VARCHAR2"),
                _make_oracle_col("COL_B", 2, "NUMBER"),
            ]
        }

        with mock.patch("export_template_from_corehub.get_table_columns",
                        return_value=discovery_resp) as mock_disc:
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        self.assertEqual(enriched, 1, "Expected exactly one table to be enriched")
        mock_disc.assert_called_once_with(
            "token", "pipeline-1", "src-agent-001", "SYSADM", "MY_TABLE"
        )
        cols = schemas["SYSADM"]["tables"]["custom"]["MY_TABLE"]["columns"]
        self.assertEqual(cols[0]["type"], "VARCHAR2")
        self.assertEqual(cols[1]["type"], "NUMBER")

    def test_skips_tables_with_partial_types(self):
        """If only *some* types are null the table is left unchanged (mixed format)."""
        cols = [
            _null_type_yaml_col("COL_A", "col_a", 1),
            {**_null_type_yaml_col("COL_B", "col_b", 2), "type": "VARCHAR2"},  # already set
        ]
        schemas = self._make_schemas_with_null_types(cols=cols)
        entities = self._make_entity_with_source_agent()

        with mock.patch("export_template_from_corehub.get_table_columns") as mock_disc:
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        mock_disc.assert_not_called()
        self.assertEqual(enriched, 0)

    def test_skips_tables_with_no_null_types(self):
        """Tables whose columns already have types are not touched."""
        cols = [
            {**_null_type_yaml_col("COL_A", "col_a", 1), "type": "VARCHAR2"},
        ]
        schemas = self._make_schemas_with_null_types(cols=cols)
        entities = self._make_entity_with_source_agent()

        with mock.patch("export_template_from_corehub.get_table_columns") as mock_disc:
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        mock_disc.assert_not_called()
        self.assertEqual(enriched, 0)

    def test_returns_zero_when_no_source_agent(self):
        """No source agent in entity list → enrichment is safely skipped."""
        schemas = self._make_schemas_with_null_types()
        entities = [
            {
                "entityId": "ent-1",
                "entityName": "SYSADM.MY_TABLE",
                "agentEntities": [
                    {"agentId": "tgt-001", "entityType": {"type": "Target"}, "columns": []}
                ],
            }
        ]
        with mock.patch("export_template_from_corehub.get_table_columns") as mock_disc:
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        mock_disc.assert_not_called()
        self.assertEqual(enriched, 0)

    def test_discovery_exception_does_not_propagate(self):
        """A discovery failure for one table must not crash the entire enrichment."""
        schemas = self._make_schemas_with_null_types()
        entities = self._make_entity_with_source_agent()

        with mock.patch("export_template_from_corehub.get_table_columns",
                        side_effect=RuntimeError("agent unreachable")):
            # Must not raise
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        self.assertEqual(enriched, 0)
        # Types remain null (no crash, no silent data corruption)
        col = schemas["SYSADM"]["tables"]["custom"]["MY_TABLE"]["columns"][0]
        self.assertIsNone(col["type"])

    def test_multiple_tables_all_enriched(self):
        """All tables with null types in a schema are enriched in one pass."""
        schemas = {
            "SYSADM": {
                "target": "tgt",
                "tables": {
                    "whitelist": ["TABLE_A", "TABLE_B"],
                    "custom": {
                        "TABLE_A": {
                            "name": "table_a",
                            "entityName": "SYSADM.TABLE_A",
                            "columns": [_null_type_yaml_col("ID", "id", 1)],
                            "customProperties": {},
                        },
                        "TABLE_B": {
                            "name": "table_b",
                            "entityName": "SYSADM.TABLE_B",
                            "columns": [_null_type_yaml_col("NAME", "name", 1)],
                            "customProperties": {},
                        },
                    },
                },
            }
        }
        entities = self._make_entity_with_source_agent()
        disc_resp = {"columns": [_make_oracle_col("ID", 1, "NUMBER"),
                                  _make_oracle_col("NAME", 1, "VARCHAR2")]}

        with mock.patch("export_template_from_corehub.get_table_columns",
                        return_value=disc_resp):
            enriched = self.enrich(schemas, entities, "token", "pipeline-1")

        self.assertEqual(enriched, 2)


# ---------------------------------------------------------------------------
# 2.  Import-side: null-type columns handled in create_entities mapping branch
# ---------------------------------------------------------------------------

class NullTypeImportFallbackTests(unittest.TestCase):
    """
    Verify that create_entities processes backup YAMLs with all-null column
    types without raising and produces entity payloads with non-null dataType.
    """

    SOURCE_COLS = ["COL_A", "COL_B", "COL_C"]

    def _make_yaml_config(self, table: str = "MY_TABLE", schema: str = "SYSADM",
                          target_schema: str = "public") -> dict:
        cols = [
            _null_type_yaml_col(c, c.lower(), i + 1)
            for i, c in enumerate(self.SOURCE_COLS)
        ]
        return {
            schema: {
                "target": target_schema,
                "sourceType": "SQL",
                "targetType": "SQL",
                "tables": {
                    "whitelist": [table],
                    "custom": {
                        table: {
                            "name": table.lower(),
                            "entityName": f"{schema}.{table}",
                            "columns": cols,
                            "customProperties": {
                                "source": {},
                                "target": {
                                    "allowedOperations": ["INSERT", "UPDATE", "DELETE", "TRUNCATE"],
                                },
                            },
                        }
                    },
                },
            }
        }

    def _run_create_entities(self, oracle_data_type: str = "varchar2",
                             pg_data_type: str = "varchar") -> dict:
        """
        Execute create_entities with all-null YAML column types and return
        the result dict.  All external I/O is mocked.
        """
        from create_all_entities import create_entities

        oracle_cols = [_make_oracle_col(c, i + 1, oracle_data_type)
                       for i, c in enumerate(self.SOURCE_COLS)]
        pg_cols = [_make_pg_col(c.lower(), i + 100, pg_data_type)
                   for i, c in enumerate(self.SOURCE_COLS)]

        oracle_resp = {"columns": oracle_cols}
        pg_resp = {"columns": pg_cols}

        oracle_node = _minimal_node_info(oracle_data_type)
        pg_node = _minimal_node_info(pg_data_type)
        yaml_cfg = self._make_yaml_config()
        tables = [{"name": "MY_TABLE", "schema": "SYSADM", "id": 1}]

        captured_payloads = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if body and "entities" in body:
                captured_payloads.append(copy.deepcopy(body))
            return {}

        with mock.patch("create_all_entities.get_node_info",
                        side_effect=lambda t, p, a: oracle_node if a == "src" else pg_node), \
             mock.patch("create_all_entities.get_table_columns",
                        side_effect=lambda t, p, a, s, tbl:
                            oracle_resp if a == "src" else pg_resp), \
             mock.patch("create_all_entities.get_agent_tables",
                        return_value=[{"name": "my_table", "schema": "public", "id": 99}]), \
             mock.patch("create_all_entities.fetch_core_hub", side_effect=fake_fetch), \
             mock.patch("create_all_entities.create_group", return_value="grp-id"), \
             mock.patch("create_all_entities.handle_table_creation", return_value=None):

            result = create_entities(
                "tok", "pipeline-1", "SYSADM", "public",
                tables, "src", "tgt",
                "SQL", "SQL",
                yaml_cfg,
                skip_errors=False,
                chunk_size=50,
            )

        return result, captured_payloads

    # ------------------------------------------------------------------
    def test_does_not_raise_with_all_null_types(self):
        """create_entities must complete successfully despite all-null YAML types."""
        result, _ = self._run_create_entities()
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["successful"], 1)

    def test_entity_payload_has_non_null_source_datatype(self):
        """Source agentEntity columns in the PUT payload must have a non-null dataType."""
        _, payloads = self._run_create_entities(oracle_data_type="VARCHAR2",
                                                pg_data_type="varchar")
        self.assertTrue(payloads, "No entity PUT payload was captured")
        entities = payloads[0]["entities"]
        self.assertTrue(entities, "Payload contains no entities")

        for entity in entities:
            for ae in entity.get("agentEntities", []):
                entity_type = (ae.get("entityType") or {}).get("type", "")
                if entity_type != "Source":
                    continue
                for col in ae.get("columns", []):
                    self.assertIsNotNone(
                        col.get("dataType"),
                        f"Source column '{col.get('name')}' has null dataType in payload",
                    )
                    self.assertNotEqual(
                        col.get("dataType"), "",
                        f"Source column '{col.get('name')}' has empty dataType in payload",
                    )

    def test_entity_payload_has_non_null_target_datatype(self):
        """Target agentEntity columns in the PUT payload must have a non-null dataType."""
        _, payloads = self._run_create_entities(oracle_data_type="VARCHAR2",
                                                pg_data_type="varchar")
        self.assertTrue(payloads, "No entity PUT payload was captured")
        entities = payloads[0]["entities"]

        for entity in entities:
            for ae in entity.get("agentEntities", []):
                entity_type = (ae.get("entityType") or {}).get("type", "")
                if entity_type != "Target":
                    continue
                for col in ae.get("columns", []):
                    self.assertIsNotNone(
                        col.get("dataType"),
                        f"Target column '{col.get('name')}' has null dataType in payload",
                    )


# ---------------------------------------------------------------------------
# 3.  Null-safe col_type resolution (the .get() / or-fallback fix)
# ---------------------------------------------------------------------------

class NullSafeColTypeResolutionTests(unittest.TestCase):
    """
    dict.get(key, default) returns None when the key is present but the value
    is None.  Verify that the 'or' fallback pattern used throughout the
    codebase correctly converts None → 'varchar'.
    """

    def test_none_value_falls_through_to_default(self):
        col = {"type": None, "dataLength": 0}
        # Old (broken) pattern:
        old = col.get("dataType", col.get("type", "varchar"))
        # New (fixed) pattern:
        new = col.get("dataType") or col.get("type") or "varchar"
        self.assertIsNone(old,  "Old pattern should return None (demonstrating the bug)")
        self.assertEqual(new, "varchar", "New pattern should fall through to 'varchar'")

    def test_missing_key_uses_default_in_both_patterns(self):
        col = {"dataLength": 0}  # no 'type' or 'dataType' key at all
        old = col.get("dataType", col.get("type", "varchar"))
        new = col.get("dataType") or col.get("type") or "varchar"
        self.assertEqual(old, "varchar")
        self.assertEqual(new, "varchar")

    def test_present_value_is_preserved(self):
        col = {"type": "NUMBER", "dataLength": 0}
        new = col.get("dataType") or col.get("type") or "varchar"
        self.assertEqual(new, "NUMBER")

    def test_datatype_takes_priority_over_type(self):
        col = {"dataType": "numeric", "type": "NUMBER"}
        new = col.get("dataType") or col.get("type") or "varchar"
        self.assertEqual(new, "numeric")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()

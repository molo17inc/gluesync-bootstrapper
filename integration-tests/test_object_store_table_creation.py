#!/usr/bin/env python3
"""
Tests for the "do not attempt CREATE TABLE on non-RDBMS targets" guarantee.

This covers the bug where the Automator attempted to issue a CREATE TABLE
statement against an Azure Data Lake Storage agent (whose category in
agents.json is "Object Store"), causing CoreHub to fail with
"Generation of create target table statement error: This function must not
be called.".

Two layers are exercised:

1. ``create_all_entities.is_no_create_table_target`` - the predicate used at
   the entity-creation site to decide whether to call ``handle_table_creation``.
2. ``automator_app.corehub._normalize_agent_category`` and
   ``automator_app.corehub.run_create_entities`` - the upstream normalization
   that turns agents.json categories ("RDBMS", "NoSQL", "Object Store", ...)
   into the SQL/NoSQL labels consumed downstream, and the inference + override
   that ``run_create_entities`` performs before invoking
   ``create_entities_main``.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Make the project root importable so we can import create_all_entities and
# automator_app without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class IsNoCreateTableTargetTests(unittest.TestCase):
    """Direct tests for the create_all_entities helper predicate."""

    @classmethod
    def setUpClass(cls):
        from create_all_entities import is_no_create_table_target
        cls.is_no_create_table_target = staticmethod(is_no_create_table_target)

    def test_object_store_is_skipped(self):
        """Azure Data Lake Storage et al. report 'Object Store' in agents.json."""
        self.assertTrue(self.is_no_create_table_target("Object Store"))

    def test_object_store_case_insensitive(self):
        for value in ("OBJECT STORE", "object store", "Object store", "  Object Store  "):
            with self.subTest(value=value):
                self.assertTrue(self.is_no_create_table_target(value))

    def test_nosql_is_skipped(self):
        for value in ("NoSQL", "NOSQL", "nosql"):
            with self.subTest(value=value):
                self.assertTrue(self.is_no_create_table_target(value))

    def test_sql_targets_are_not_skipped(self):
        """RDBMS / SQL targets must continue to receive CREATE TABLE calls."""
        for value in ("SQL", "sql", "RDBMS", "rdbms"):
            with self.subTest(value=value):
                # RDBMS is normalized to SQL upstream; the predicate sees only
                # SQL/NoSQL/Object Store at the call site, but be defensive and
                # also accept RDBMS as creatable.
                self.assertFalse(self.is_no_create_table_target(value))

    def test_empty_and_none_are_not_skipped(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertFalse(self.is_no_create_table_target(value))


class NormalizeAgentCategoryTests(unittest.TestCase):
    """Test that agents.json categories are normalized correctly upstream."""

    @classmethod
    def setUpClass(cls):
        from automator_app.corehub import _normalize_agent_category
        cls._normalize = staticmethod(_normalize_agent_category)

    def test_rdbms_becomes_sql(self):
        self.assertEqual(self._normalize("RDBMS"), "SQL")
        self.assertEqual(self._normalize("rdbms"), "SQL")

    def test_sql_stays_sql(self):
        self.assertEqual(self._normalize("SQL"), "SQL")

    def test_nosql_stays_nosql(self):
        self.assertEqual(self._normalize("NoSQL"), "NoSQL")

    def test_object_store_normalizes_to_nosql(self):
        """The bug fix relies on this collapse: Object Store -> NoSQL.

        Without it, downstream code that only checks for 'NoSQL' would
        attempt CREATE TABLE against object stores like Azure Data Lake.
        """
        self.assertEqual(self._normalize("Object Store"), "NoSQL")
        self.assertEqual(self._normalize("OBJECT STORE"), "NoSQL")

    def test_unknown_category_falls_back_to_nosql(self):
        """Conservative default: anything we don't recognize is treated as
        non-RDBMS so we don't accidentally issue CREATE TABLE."""
        self.assertEqual(self._normalize("Search"), "NoSQL")
        self.assertEqual(self._normalize("Streaming"), "NoSQL")

    def test_none_or_empty_returns_none(self):
        self.assertIsNone(self._normalize(None))
        self.assertIsNone(self._normalize(""))


class RunCreateEntitiesInferenceTests(unittest.TestCase):
    """run_create_entities must infer & override target_type from agents.json.

    This is the regression test for the case where the Automator UI passed
    target_type='SQL' (from a YAML hint or fallback) for a pipeline whose
    target was actually an Object Store. The previous behavior trusted the
    caller and forwarded 'SQL' to create_entities_main, causing it to call
    handle_table_creation. The fix mirrors the inference logic from
    run_create_entities_for_tables.
    """

    def _invoke(self, *, inferred_target, supplied_target="SQL", supplied_create_tables=True):
        """Drive run_create_entities with mocks and capture the args forwarded
        to create_entities_main plus the resulting CREATE_TABLE_IF_NOT_EXISTS
        flag observed during the call."""

        from automator_app import corehub
        import create_all_entities

        observed = {}

        def fake_create_entities_main(pipeline_id, source_schema, target_schema,
                                      source_type, target_type, yaml_file,
                                      token, skip_errors, chunk_size):
            # Capture the values the downstream call actually received.
            # Read CREATE_TABLE_IF_NOT_EXISTS from create_all_entities (the
            # module that owns the flag) - corehub's binding is a stale
            # import-time snapshot that does not get updated by
            # set_create_table_if_not_exists.
            observed["target_type"] = target_type
            observed["source_type"] = source_type
            observed["create_table_flag"] = create_all_entities.CREATE_TABLE_IF_NOT_EXISTS
            observed["env_flag"] = os.environ.get("CREATE_TABLE_IF_NOT_EXISTS")
            return {"ok": True}

        with mock.patch.object(corehub, "infer_agent_schema_types",
                               return_value=("SQL", inferred_target)), \
             mock.patch.object(corehub, "create_entities_main",
                               side_effect=fake_create_entities_main), \
             mock.patch.object(corehub, "configure_core_hub"), \
             mock.patch.object(corehub, "set_scheduling_enabled"):
            corehub.run_create_entities(
                token="tok",
                base_url="https://example.invalid/",
                pipeline_id="pid",
                source_schema="src",
                target_schema="tgt",
                source_type="SQL",
                target_type=supplied_target,
                yaml_file="/tmp/does-not-matter.yaml",
                skip_errors=True,
                chunk_size=50,
                enable_scheduling=False,
                create_tables=supplied_create_tables,
                use_ssl=False,
                skip_verify=True,
                log_callback=None,
            )

        return observed

    def test_object_store_target_disables_table_creation(self):
        """When agents.json says target is Object Store (-> NoSQL), the
        bootstrapper must run with table creation disabled, regardless of
        the caller-supplied flag."""
        observed = self._invoke(
            inferred_target="NoSQL",     # what _normalize_agent_category produces
            supplied_target="SQL",        # caller's stale hint from YAML/fallback
            supplied_create_tables=True,
        )
        self.assertEqual(observed["target_type"], "NoSQL",
                         "target_type forwarded to create_entities_main must be the inferred value")
        self.assertFalse(observed["create_table_flag"],
                         "CREATE_TABLE_IF_NOT_EXISTS must be False during the inner call")
        self.assertEqual(observed["env_flag"], "false",
                         "CREATE_TABLE_IF_NOT_EXISTS env var must be 'false' for downstream subprocesses")

    def test_sql_target_keeps_table_creation_enabled(self):
        observed = self._invoke(
            inferred_target="SQL",
            supplied_target="SQL",
            supplied_create_tables=True,
        )
        self.assertEqual(observed["target_type"], "SQL")
        self.assertTrue(observed["create_table_flag"])

    def test_inferred_value_overrides_caller(self):
        """Even if the caller passed 'SQL', a NoSQL inference must win."""
        observed = self._invoke(
            inferred_target="NoSQL",
            supplied_target="SQL",
            supplied_create_tables=True,
        )
        self.assertEqual(observed["target_type"], "NoSQL")


if __name__ == "__main__":
    unittest.main()

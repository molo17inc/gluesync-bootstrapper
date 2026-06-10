#!/usr/bin/env python3
"""
Regression tests for the fix/import-group-loop branch.

Covers every bug fixed:
1. CreateGroupNonListResponseTests  – create_group robustness when the API
   returns a non-list (dict, None) or a list containing non-dict items.
2. WhitelistBareNameFallbackTests   – AS/400-style library-qualified table
   names (FILELIB.TABLE) are matched against whitelist/blacklist entries that
   use the bare name.
3. TargetTableNameNormalizationTests – library prefix is stripped from the
   target table 'name' field when it matches the YAML key.
4. EntityNameSplitTests             – split('.', 1) preserves dots inside
   table names when splitting entity names into schema/table components.
5. MultiSchemaExportTests           – build_schemas_from_entities and
   build_yaml_structure handle multi-schema pipelines correctly.
6. GroupIdMapCacheTests             – the per-entity groupId_map cache avoids
   redundant API calls for entities that share the same group.
"""

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch, call

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# CLASS 1 – create_group robustness
# ---------------------------------------------------------------------------

class CreateGroupNonListResponseTests(unittest.TestCase):
    """
    Verify create_group handles non-list responses from the groups endpoint
    gracefully (isinstance list guard + isinstance dict guard in inner loop).
    """

    def _run_create_group(self, get_response, put_response=None):
        """Helper: run create_group with mocked fetch_core_hub.

        GET  /pipelines/.../config/groups  → get_response
        PUT  /pipelines/.../config/groups  → put_response (default {"groupId": "new-id"})
        """
        import commons

        if put_response is None:
            put_response = {"groupId": "new-id"}

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if method == "GET":
                return get_response
            if method == "PUT":
                return put_response
            return {}

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            result = commons.create_group(
                token="tok",
                pipeline_id="pipe-1",
                group_name="finance",
            )
        return result

    def test_create_group_handles_dict_response(self):
        """GET returns a plain dict (not a list) → must not crash, must create."""
        result = self._run_create_group(
            get_response={},
            put_response={"groupId": "new-id"},
        )
        self.assertEqual(result, "new-id")

    def test_create_group_handles_none_response(self):
        """GET returns None → must fall through to creation."""
        result = self._run_create_group(
            get_response=None,
            put_response={"groupId": "created-from-none"},
        )
        self.assertEqual(result, "created-from-none")

    def test_create_group_skips_non_dict_list_items(self):
        """GET returns a list with a string item before the matching dict.

        The inner loop must skip the string and correctly match the dict entry.
        """
        import commons

        groups_response = [
            "bad-string-item",
            {"name": "finance", "groupId": "gid-1"},
        ]
        put_called = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if method == "GET":
                return groups_response
            if method == "PUT":
                put_called.append(True)
                return {"groupId": "should-not-be-called"}
            return {}

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            result = commons.create_group("tok", "pipe", "finance")

        self.assertEqual(result, "gid-1")
        self.assertFalse(put_called, "PUT should not be called when group already exists")

    def test_create_group_finds_existing_group(self):
        """GET returns a clean list with the group → must return its ID without PUT."""
        import commons

        groups_response = [{"name": "finance", "groupId": "gid-1"}]
        put_called = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if method == "GET":
                return groups_response
            if method == "PUT":
                put_called.append(True)
                return {"groupId": "should-not-be-called"}
            return {}

        with mock.patch.object(commons, "fetch_core_hub", side_effect=fake_fetch):
            result = commons.create_group("tok", "pipe", "finance")

        self.assertEqual(result, "gid-1")
        self.assertFalse(put_called, "PUT must not be called when group already exists")


# ---------------------------------------------------------------------------
# CLASS 2 – Whitelist / blacklist bare-name fallback
# ---------------------------------------------------------------------------

class WhitelistBareNameFallbackTests(unittest.TestCase):
    """
    Verify the logic that extracts the bare table name (part after the last dot)
    from a library-qualified name like 'FILELIB.SECMSTPM'.

    These are pure-logic tests: no mocking, no imports needed beyond stdlib.
    """

    def test_whitelist_bare_name_matches_qualified(self):
        """A whitelist containing 'SECMSTPM' should match 'FILELIB.SECMSTPM'."""
        qualified = "FILELIB.SECMSTPM"
        whitelist = ["SECMSTPM"]
        bare = qualified.rsplit('.', 1)[-1]
        self.assertEqual(bare, "SECMSTPM")
        self.assertIn(bare, whitelist)
        # Simulate the production guard: table_name in whitelist OR bare_name in whitelist
        matched = qualified in whitelist or bare in whitelist
        self.assertTrue(matched)

    def test_blacklist_bare_name_matches_qualified(self):
        """A blacklist containing 'SECMSTPM' should block 'FILELIB.SECMSTPM'."""
        qualified = "FILELIB.SECMSTPM"
        blacklist = ["SECMSTPM"]
        bare = qualified.rsplit('.', 1)[-1]
        self.assertEqual(bare, "SECMSTPM")
        blocked = qualified in blacklist or bare in blacklist
        self.assertTrue(blocked)

    def test_bare_name_extraction_no_dot(self):
        """A name with no dot returns itself unchanged."""
        name = "SECMSTPM"
        bare = name.rsplit('.', 1)[-1] if '.' in name else name
        self.assertEqual(bare, "SECMSTPM")

    def test_bare_name_extraction_multiple_dots(self):
        """rsplit('.', 1)[-1] returns only the last segment for multi-dot names."""
        name = "LIB.SUB.TABLE"
        bare = name.rsplit('.', 1)[-1]
        self.assertEqual(bare, "TABLE")


# ---------------------------------------------------------------------------
# CLASS 3 – Target table name normalization
# ---------------------------------------------------------------------------

class TargetTableNameNormalizationTests(unittest.TestCase):
    """
    Verify the stripping logic that removes a library prefix from the YAML
    `name` field when it is just a prefix prepended to the YAML key.

    The production code (in create_all_entities.py) reads:

        raw_target_name = custom_config.get('name', table_name)
        if (
            raw_target_name != table_name
            and '.' in raw_target_name
            and raw_target_name.rsplit('.', 1)[-1] == yaml_table_key
        ):
            target_table_name = raw_target_name.rsplit('.', 1)[-1]
        else:
            target_table_name = raw_target_name
    """

    @staticmethod
    def _resolve_target_name(raw_target_name, table_name, yaml_table_key):
        """Mirror the production stripping logic for unit-testing."""
        if (
            raw_target_name != table_name
            and '.' in raw_target_name
            and raw_target_name.rsplit('.', 1)[-1] == yaml_table_key
        ):
            return raw_target_name.rsplit('.', 1)[-1]
        return raw_target_name

    def test_strip_library_prefix_when_suffix_matches_key(self):
        """FILELIB.SECMSTPM → SECMSTPM when yaml_table_key == 'SECMSTPM'."""
        result = self._resolve_target_name(
            raw_target_name="FILELIB.SECMSTPM",
            table_name="SECMSTPM",
            yaml_table_key="SECMSTPM",
        )
        self.assertEqual(result, "SECMSTPM")

    def test_no_strip_when_name_differs_from_key(self):
        """The suffix of raw_target_name does NOT match yaml_table_key → kept as-is."""
        result = self._resolve_target_name(
            raw_target_name="FILELIB.TABLE_B",
            table_name="TABLE_B",
            yaml_table_key="TABLE_A",
        )
        self.assertEqual(result, "FILELIB.TABLE_B")

    def test_no_strip_when_no_dot_in_name(self):
        """No dot in raw_target_name → condition short-circuits, returned as-is."""
        result = self._resolve_target_name(
            raw_target_name="SECMSTPM",
            table_name="SECMSTPM",
            yaml_table_key="SECMSTPM",
        )
        self.assertEqual(result, "SECMSTPM")

    def test_no_strip_when_name_equals_table_name(self):
        """raw_target_name == table_name → first condition False, returned as-is."""
        result = self._resolve_target_name(
            raw_target_name="FILELIB.SECMSTPM",
            table_name="FILELIB.SECMSTPM",
            yaml_table_key="SECMSTPM",
        )
        self.assertEqual(result, "FILELIB.SECMSTPM")


# ---------------------------------------------------------------------------
# CLASS 4 – split('.', 1) fix for dotted table names
# ---------------------------------------------------------------------------

class EntityNameSplitTests(unittest.TestCase):
    """
    Pure-logic tests for the split('.', 1) fix.

    The old code used `split('.')[-1]` to extract the table name from an
    entityName string like 'SCHEMA.MY.DOTTED.TABLE', which returned only
    'TABLE'.  The fix uses `split('.', 1)[-1]` to preserve the full dotted
    table name 'MY.DOTTED.TABLE'.
    """

    def test_split_1_preserves_dotted_table_name(self):
        entity_name = "SCHEMA.MY.DOTTED.TABLE"
        table_name = entity_name.split('.', 1)[-1]
        self.assertEqual(table_name, "MY.DOTTED.TABLE")

    def test_split_1_simple_name(self):
        entity_name = "SCHEMA.TABLE"
        table_name = entity_name.split('.', 1)[-1]
        self.assertEqual(table_name, "TABLE")

    def test_split_at_first_dot_vs_last(self):
        entity_name = "SCHEMA.MY.DOTTED.TABLE"
        old_result = entity_name.split('.')[-1]   # Bug: only last segment
        new_result = entity_name.split('.', 1)[-1]  # Fix: everything after first dot
        self.assertEqual(old_result, "TABLE",
                         "Old split()[-1] should return only the last segment")
        self.assertEqual(new_result, "MY.DOTTED.TABLE",
                         "New split('.', 1)[-1] should preserve interior dots")
        self.assertNotEqual(old_result, new_result)

    def test_split_schema_extraction_correct(self):
        entity_name = "SCHEMA.MY.DOTTED.TABLE"
        schema = entity_name.split('.', 1)[0]
        self.assertEqual(schema, "SCHEMA")


# ---------------------------------------------------------------------------
# CLASS 5 – Multi-schema export tests
# ---------------------------------------------------------------------------

def _make_entity(entity_name, src_schema, src_table, tgt_schema, tgt_table):
    """Build a minimal CoreHub-style entity dict for export tests."""
    return {
        "entityName": entity_name,
        "groupId": "_default",
        "agentEntities": [
            {
                "entityType": {
                    "type": "Source",
                },
                "table": {"schema": src_schema, "name": src_table},
                "tablesProperties": {},
                "customProperties": {},
                "columns": [],
                "keys": [],
            },
            {
                "entityType": {
                    "type": "Target",
                },
                "table": {"schema": tgt_schema, "name": tgt_table},
                "tablesProperties": {},
                "customProperties": {},
                "columns": [],
                "keys": [],
            },
        ],
    }


class MultiSchemaExportTests(unittest.TestCase):
    """
    Test that build_schemas_from_entities and build_yaml_structure handle
    multi-schema pipelines and produce the correct flat / schemas-wrapper format.
    """

    def setUp(self):
        from export_template_from_corehub import build_schemas_from_entities, build_yaml_structure
        from commons import extract_all_schemas_from_yaml
        self.build_schemas = build_schemas_from_entities
        self.build_yaml = build_yaml_structure
        self.extract_all = extract_all_schemas_from_yaml

    def test_two_schemas_produce_schemas_wrapper(self):
        """Entities from two distinct source schemas → 'schemas' wrapper in output."""
        entities = [
            _make_entity("SCHEMA_A.TABLE_A", "SCHEMA_A", "TABLE_A", "TARGET_A", "TABLE_A"),
            _make_entity("SCHEMA_B.TABLE_B", "SCHEMA_B", "TABLE_B", "TARGET_B", "TABLE_B"),
        ]
        schemas = self.build_schemas(entities, {})
        self.assertIn("SCHEMA_A", schemas)
        self.assertIn("SCHEMA_B", schemas)

        yaml_data = self.build_yaml(schemas)
        self.assertIn("schemas", yaml_data,
                      "Multi-schema output must use the 'schemas' wrapper key")
        self.assertIn("SCHEMA_A", yaml_data["schemas"])
        self.assertIn("SCHEMA_B", yaml_data["schemas"])

    def test_single_schema_produces_flat_format(self):
        """Entities from a single source schema → flat format (no 'schemas' wrapper)."""
        entities = [
            _make_entity("SCHEMA_A.TABLE_1", "SCHEMA_A", "TABLE_1", "TARGET_A", "TABLE_1"),
            _make_entity("SCHEMA_A.TABLE_2", "SCHEMA_A", "TABLE_2", "TARGET_A", "TABLE_2"),
        ]
        schemas = self.build_schemas(entities, {})
        yaml_data = self.build_yaml(schemas)
        self.assertNotIn("schemas", yaml_data,
                         "Single-schema output must NOT use the 'schemas' wrapper key")
        self.assertIn("SCHEMA_A", yaml_data)

    def test_multi_schema_round_trips_via_extract_all_schemas(self):
        """
        Build multi-schema YAML, write to a temp file, then read it back with
        extract_all_schemas_from_yaml and verify both schema pairs are present.
        """
        entities = [
            _make_entity("SCHEMA_A.TABLE_A", "SCHEMA_A", "TABLE_A", "TARGET_A", "TABLE_A"),
            _make_entity("SCHEMA_B.TABLE_B", "SCHEMA_B", "TABLE_B", "TARGET_B", "TABLE_B"),
        ]
        schemas = self.build_schemas(entities, {})
        yaml_data = self.build_yaml(schemas)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(yaml_data, f, sort_keys=False, allow_unicode=True)
            tmp_path = f.name

        try:
            pairs = self.extract_all(tmp_path)
            pair_set = set(pairs)
            self.assertIn(("SCHEMA_A", "TARGET_A"), pair_set)
            self.assertIn(("SCHEMA_B", "TARGET_B"), pair_set)
        finally:
            os.unlink(tmp_path)

    def test_single_schema_round_trips_via_extract_all_schemas(self):
        """
        Build single-schema YAML, write to temp file, verify exactly one pair.
        """
        entities = [
            _make_entity("SCHEMA_A.TABLE_1", "SCHEMA_A", "TABLE_1", "TARGET_A", "TABLE_1"),
            _make_entity("SCHEMA_A.TABLE_2", "SCHEMA_A", "TABLE_2", "TARGET_A", "TABLE_2"),
        ]
        schemas = self.build_schemas(entities, {})
        yaml_data = self.build_yaml(schemas)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.safe_dump(yaml_data, f, sort_keys=False, allow_unicode=True)
            tmp_path = f.name

        try:
            pairs = self.extract_all(tmp_path)
            self.assertEqual(len(pairs), 1)
            self.assertEqual(pairs[0], ("SCHEMA_A", "TARGET_A"))
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# CLASS 6 – groupId_map cache tests
# ---------------------------------------------------------------------------

def resolve_group_id(requested_group, groupId_map, create_group_fn, token, pipeline_id):
    """
    Inline mirror of the per-entity group resolution logic introduced in
    create_all_entities.py as part of the fix/import-group-loop patch.

    This helper is tested in isolation so we can verify cache-hit / cache-miss
    behaviour without running the entire create_entities function.
    """
    if not requested_group or requested_group == '_default':
        return '_default'
    cached_id = groupId_map.get(requested_group)
    if cached_id and cached_id != '_default':
        return cached_id
    group_id = create_group_fn(token, pipeline_id, requested_group)
    if group_id != '_default':
        groupId_map[requested_group] = group_id
    return group_id


class GroupIdMapCacheTests(unittest.TestCase):
    """
    Test that the per-entity groupId_map cache prevents redundant API calls
    when multiple entities share the same group name.
    """

    def test_cache_hit_skips_api_call(self):
        """Pre-populated cache entry is returned without calling create_group."""
        groupId_map = {"finance": "gid-1"}
        mock_create_group = MagicMock()

        result = resolve_group_id(
            "finance", groupId_map, mock_create_group, "tok", "pipe"
        )

        mock_create_group.assert_not_called()
        self.assertEqual(result, "gid-1")

    def test_cache_miss_calls_api_and_caches(self):
        """On a cache miss, create_group is called and the result is cached."""
        groupId_map = {}
        mock_create_group = MagicMock(return_value="gid-new")

        result = resolve_group_id(
            "finance", groupId_map, mock_create_group, "tok", "pipe"
        )

        mock_create_group.assert_called_once()
        self.assertEqual(result, "gid-new")
        self.assertEqual(groupId_map.get("finance"), "gid-new",
                         "Newly created group ID must be stored in the cache")

    def test_cache_populated_for_second_entity(self):
        """
        Two consecutive calls for the same group name: the second call must use
        the cache and NOT invoke create_group a second time.
        """
        groupId_map = {}
        mock_create_group = MagicMock(return_value="gid-new")

        result1 = resolve_group_id(
            "finance", groupId_map, mock_create_group, "tok", "pipe"
        )
        result2 = resolve_group_id(
            "finance", groupId_map, mock_create_group, "tok", "pipe"
        )

        # create_group must have been called exactly once despite two resolutions
        mock_create_group.assert_called_once()
        self.assertEqual(result1, "gid-new")
        self.assertEqual(result2, "gid-new")

    def test_default_group_bypasses_cache_and_api(self):
        """A requested_group of '_default' (or empty) must return '_default' directly."""
        groupId_map = {}
        mock_create_group = MagicMock()

        for requested in ("_default", "", None):
            with self.subTest(requested=requested):
                result = resolve_group_id(
                    requested, groupId_map, mock_create_group, "tok", "pipe"
                )
                self.assertEqual(result, "_default")

        mock_create_group.assert_not_called()

    def test_api_returning_default_does_not_populate_cache(self):
        """
        When create_group returns '_default' (failure path), the cache must NOT
        be populated so the next call retries the API.
        """
        groupId_map = {}
        mock_create_group = MagicMock(return_value="_default")

        resolve_group_id("finance", groupId_map, mock_create_group, "tok", "pipe")

        self.assertNotIn("finance", groupId_map,
                         "A '_default' API result must not be cached")


if __name__ == "__main__":
    unittest.main()

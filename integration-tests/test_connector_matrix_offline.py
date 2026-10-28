#!/usr/bin/env python3
"""Offline coverage for every data connector the backup import supports.

For each agents.json data tag (RDBMS / NoSQL / Object Store / Event Streaming) we
run create_entities against FakeCoreHub in both directions that matter:

* RDBMS → connector (create-table skipped iff target is non-SQL)
* connector → RDBMS (source discovery + documentKey when source is NoSQL)
* NoSQL → NoSQL (couchbase/mongodb style documentKey)

Also covers Core Hub <2.3 (version 2.2.x) request path via the same FakeCoreHub.
Live DB labs are listed in e2e/COVERAGE_MATRIX.md — this file never needs a hub.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fake_corehub import FakeCoreHub, SOURCE_AGENT, TARGET_AGENT, column, patched, run_create_entities  # noqa: E402
import create_all_entities as cae  # noqa: E402
from automator_app.corehub import _normalize_agent_category  # noqa: E402

AGENTS_JSON = ROOT / "agents.json"

_DATA_TYPES = {"RDBMS", "NoSQL", "Object Store", "Event Streaming"}


def data_connectors():
    data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))["data"]
    out = []
    for entry in data:
        typ = entry.get("type")
        tag = entry.get("internalName")
        if typ in _DATA_TYPES and tag:
            out.append((tag, typ, _normalize_agent_category(typ)))
    return out



def _yaml_dict(src="src", tgt="tgt", table="ITEMS", *, document_key=None,
               source_type="SQL", target_type="SQL", id_yaml="ID"):
    custom = {
        "name": table,
        "keys": [id_yaml],
        "columns": [
            {"source": id_yaml, "destination": "ID", "type": "INTEGER", "isPK": True},
            {"source": "NAME", "destination": "NAME", "type": "VARCHAR"},
        ],
    }
    if document_key is not None:
        custom["documentKey"] = document_key
    return {
        src: {
            "target": tgt,
            "sourceType": source_type,
            "targetType": target_type,
            "tables": {"whitelist": [table], "custom": {table: custom}},
        }
    }


class ConnectorCatalogTests(unittest.TestCase):
    def test_every_data_connector_is_enumerated(self):
        cons = data_connectors()
        self.assertGreaterEqual(len(cons), 30, cons)
        types = {t for _, t, _ in cons}
        self.assertLessEqual({"RDBMS", "NoSQL", "Object Store", "Event Streaming"}, types, types)


class CreateTableSkipByTargetCategoryTests(unittest.TestCase):
    """CREATE TABLE must run only for SQL/RDBMS targets — never NoSQL/Object Store/Event Streaming."""

    def test_skip_predicate_matches_agents_json_categories(self):
        for tag, typ, normalized in data_connectors():
            with self.subTest(tag=tag, typ=typ, normalized=normalized):
                if normalized == "SQL":
                    self.assertFalse(cae.is_no_create_table_target(typ))
                    self.assertFalse(cae.is_no_create_table_target(normalized))
                else:
                    self.assertTrue(cae.is_no_create_table_target(typ))
                    self.assertTrue(cae.is_no_create_table_target(normalized))


class ConnectorImportOfflineTests(unittest.TestCase):
    """Full create_entities dry path per connector category (mocked Core Hub)."""

    def _run(self, yaml_cfg, source_cols, target_cols=None, *,
             source_type="SQL", target_type="SQL",
             source_category="RDBMS", target_category="RDBMS",
             version="2.3.0.1", create_tables=True):
        source_tables = {("src", "ITEMS"): source_cols}
        target_tables = {("tgt", "ITEMS"): (target_cols or source_cols)}
        hub = FakeCoreHub(
            source_tables, target_tables,
            version=version,
            source_category=source_category,
            target_category=target_category,
        )
        create_calls = []

        def _capture_create(*a, **k):
            create_calls.append((a, k))
            return True

        with patched(hub):
            # patched forces CREATE_TABLE_IF_NOT_EXISTS=False; re-enable for this assertion.
            with mock.patch.object(cae, "CREATE_TABLE_IF_NOT_EXISTS", create_tables):
                with mock.patch.object(cae, "handle_table_creation", side_effect=_capture_create):
                    # create_entities expects yaml as dict under schemas? Looking at API —
                    # yaml_config is the full file; schemas keyed by source schema name.
                    result = cae.create_entities(
                        token="token",
                        pipeline_id="pipeline",
                        source_schema="src",
                        target_schema="tgt",
                        tables=[{"name": "ITEMS", "schema": "src", "id": 1}],
                        source_agent_id=SOURCE_AGENT,
                        target_agent_id=TARGET_AGENT,
                        source_type=source_type,
                        target_type=target_type,
                        yaml_config=yaml_cfg,
                        skip_errors=True,
                        chunk_size=50,
                    )
        return hub, create_calls, result

    def test_rdbms_target_attempts_create_table(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        hub, creates, _ = self._run(
            _yaml_dict(target_type="SQL"), cols,
            source_type="SQL", target_type="SQL",
            source_category="RDBMS", target_category="RDBMS",
        )
        self.assertTrue(creates, "SQL target must call handle_table_creation")
        self.assertTrue(hub.puts, "entity PUT expected")

    def test_nosql_object_store_streaming_targets_skip_create_table(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        for typ, normalized in (
            ("NoSQL", "NoSQL"),
            ("Object Store", "NoSQL"),
            ("Event Streaming", "NoSQL"),
        ):
            with self.subTest(typ=typ):
                hub, creates, _ = self._run(
                    _yaml_dict(target_type=typ, document_key={"keys": ["ID"], "name": "doc"}),
                    cols,
                    source_type="SQL", target_type=typ,
                    source_category="RDBMS", target_category=typ,
                )
                self.assertFalse(creates, f"{typ} must not CREATE TABLE")
                self.assertTrue(hub.puts, f"{typ}: entity PUT expected")

    def test_every_connector_as_target_completes_entity_put(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        # Folded discovery spelling (MySQL-style lower) still matches YAML ID via find_by_name
        folded = [column("id", 1, "int", is_pk=True, nullable=False), column("name", 2)]
        for tag, typ, normalized in data_connectors():
            with self.subTest(tag=tag, as_="target"):
                use_cols = folded if typ == "RDBMS" else cols
                doc_key = {"keys": ["ID"], "name": "doc"} if normalized != "SQL" else None
                hub, creates, result = self._run(
                    _yaml_dict(target_type=typ, document_key=doc_key, id_yaml="ID"),
                    use_cols,
                    source_type="SQL", target_type=typ,
                    source_category="RDBMS", target_category=typ,
                )
                self.assertTrue(hub.puts, f"{tag}: expected entity PUT")
                if normalized == "SQL":
                    self.assertTrue(creates, f"{tag} RDBMS target should create-table")
                else:
                    self.assertFalse(creates, f"{tag} non-SQL must skip create-table")

    def test_every_connector_as_source_completes_entity_put(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        for tag, typ, normalized in data_connectors():
            with self.subTest(tag=tag, as_="source"):
                hub, creates, _ = self._run(
                    _yaml_dict(source_type=typ, target_type="SQL"),
                    cols,
                    source_type=typ, target_type="SQL",
                    source_category=typ, target_category="RDBMS",
                )
                self.assertTrue(hub.puts, f"{tag} as source: entity PUT expected")
                self.assertTrue(creates, "SQL target still creates tables")

    def test_nosql_to_nosql_document_key(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        hub, creates, _ = self._run(
            _yaml_dict(source_type="NoSQL", target_type="NoSQL",
                       document_key={"keys": ["ID"], "name": "meta.docId"}),
            cols,
            source_type="NoSQL", target_type="NoSQL",
            source_category="NoSQL", target_category="NoSQL",
        )
        self.assertFalse(creates)
        self.assertTrue(hub.puts)
        body = hub.puts[0]
        entities = body.get("entities") or body
        # Gluesync 2.3 puts the YAML documentKey on the target agent as keyMapping
        # (pre-2.3 duplicate path still uses tablesProperties.documentKey).
        blob = json.dumps(entities)
        self.assertTrue(
            "keyMapping" in blob or "documentKey" in blob,
            blob[:500],
        )
        self.assertIn('"keys":[1]', blob.replace(" ", ""))

    def test_corehub_pre_23_version_still_puts_entities(self):
        cols = [column("ID", 1, "int", is_pk=True, nullable=False), column("NAME", 2)]
        hub, creates, _ = self._run(
            _yaml_dict(), cols,
            version="2.2.11.5 - build test",
        )
        self.assertTrue(hub.puts)
        self.assertTrue(creates)


class YamlTypeNormalizeTests(unittest.TestCase):
    def test_commons_collapses_non_rdbms_yaml_types(self):
        import commons
        import os
        import tempfile
        import yaml as y

        for raw in ("Object Store", "Event Streaming", "NoSQL", "Search"):
            doc = {"src": {"target": "tgt", "sourceType": "SQL", "targetType": raw, "tables": {}}}
            fd, path = tempfile.mkstemp(suffix=".yaml")
            os.close(fd)
            Path(path).write_text(y.safe_dump(doc), encoding="utf-8")
            try:
                st, tt = commons.extract_schema_types_from_yaml(path)
                self.assertEqual(st, "SQL")
                self.assertEqual(tt, "NoSQL", f"raw={raw!r} -> {tt!r}")
            finally:
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()

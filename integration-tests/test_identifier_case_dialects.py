#!/usr/bin/env python3
"""YAML ↔ discovery identifier matching is dialect-agnostic: exact spelling first, else unique
case-insensitive (the same rule ColumnNames/find_by_name use).

RDBMS agents fold unquoted identifiers differently (MySQL/MSSQL CI, Postgres→lower,
Oracle/DB2/IBM i→upper). The import does not branch per dialect: discovery returns the folded
form and this fallback still hits the YAML name. Case-only collisions (ID + id) stay exact-only,
as Cosmos/NoSQL needs.

Offline coverage: every RDBMS agent tag in agents.json, plus whitelist/custom/filter/documentKey
sites that previously compared names with ``==``.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import commons  # noqa: E402
import create_all_entities as cae  # noqa: E402
from entity_mapping import ColumnNames, find_by_name, yaml_relations  # noqa: E402

AGENTS_JSON = ROOT / "agents.json"

# Agent tags that are not RDBMS identifier-folders (NoSQL, brokers, infra).
_NON_RDBMS = {
    "couchbase", "aerospike", "dynamodb", "mongodb", "solace", "kafka", "awss3",
    "google-cloud-storage", "hbase", "ravendb", "cosmosdb", "redis", "google-pubsub",
    "google-bigquery", "core-hub", "conductor", "chronos", "bootstrapper",
    "grafana-bookmark", "whisperer", "scylladb", "gridgain", "redis-streams",
    "azure-data-lake", "cassandra", "traefik", "grafana", "prometheus", "automator",
    "traefik-win-2019", "traefik-win-2022", "grafana-win-2019", "grafana-win-2022",
    "prometheus-win-2019", "prometheus-win-2022", "docker-helper", "node",
    "portainer", "portainer-win-2019", "portainer-win-2022", "automator-docker",
    "gluesync-ui",
}


def _fold_category(tag: str) -> str:
    t = tag.lower()
    if any(x in t for x in ("mysql", "mariadb", "mssql", "sybase", "singlestore")):
        return "case_insensitive"
    if any(x in t for x in ("postgres", "cockroach", "yugabyte", "vertica", "redshift", "clickhouse", "snowflake")):
        return "fold_lower"
    if any(x in t for x in ("oracle", "db2", "iseries", "hana", "informix")):
        return "fold_upper"
    return "case_insensitive"


def rdbms_agent_tags():
    data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))["data"]
    tags = []
    for entry in data:
        tag = entry.get("internalName")
        if tag and tag not in _NON_RDBMS:
            tags.append((tag, _fold_category(tag)))
    return tags


# Discovery spelling the agent would return after the dialect folds an unquoted YAML name "IdCol".
_DISCOVERY_SPELLING = {
    "case_insensitive": "IdCol",  # engine may preserve or fold; either works with CI fallback
    "fold_lower": "idcol",
    "fold_upper": "IDCOL",
}


class FindByNameDialectTests(unittest.TestCase):
    """Parametrized over every RDBMS agent tag the repo ships."""

    def test_yaml_name_matches_folded_discovery_for_every_rdbms_tag(self):
        tags = rdbms_agent_tags()
        self.assertGreaterEqual(len(tags), 15, tags)
        yaml_name = "IdCol"
        for tag, category in tags:
            with self.subTest(tag=tag, category=category):
                discovered = _DISCOVERY_SPELLING[category]
                cols = [{"name": discovered, "id": 1}]
                found = find_by_name(cols, yaml_name)
                self.assertIsNotNone(found, f"{tag}: {yaml_name!r} vs discovery {discovered!r}")
                self.assertEqual(found["name"], discovered)
                # Exact still preferred when both spellings exist
                both = [{"name": yaml_name, "id": 2}, {"name": discovered, "id": 3}]
                if yaml_name != discovered:
                    self.assertEqual(find_by_name(both, yaml_name)["id"], 2)

    def test_ambiguous_case_only_pair_stays_unmatched(self):
        cols = [{"name": "ID"}, {"name": "id"}]
        self.assertIsNone(find_by_name(cols, "iD"))
        self.assertEqual(find_by_name(cols, "ID")["name"], "ID")
        self.assertEqual(find_by_name(cols, "id")["name"], "id")


class ResidualIdentifierSitesTests(unittest.TestCase):
    """Sites that used to compare YAML names with ``==`` against discovery / listings."""

    def test_find_discovered_column_uses_case_insensitive_fallback(self):
        info = {"columns": [{"name": "status", "id": 9}]}
        self.assertEqual(commons._find_discovered_column(info, "STATUS")["id"], 9)
        self.assertIsNone(commons._find_discovered_column({"columns": []}, "STATUS"))

    def test_normalize_columns_payload_accepts_bare_list(self):
        bare = [{"name": "ID"}]
        self.assertEqual(commons.normalize_columns_payload(bare), {"columns": bare})
        self.assertEqual(commons.normalize_columns_payload(None), {"columns": []})
        self.assertEqual(commons.normalize_columns_payload({"columns": bare})["columns"], bare)

    def test_custom_config_of_matches_table_key_case_insensitively(self):
        custom = {"GS_IMP_REV_ITEMS": {"keys": ["ID"]}}
        cfg, key = cae._custom_config_of(custom, "gs_imp_rev_items")
        self.assertEqual(key, "GS_IMP_REV_ITEMS")
        self.assertEqual(cfg["keys"], ["ID"])

    def test_whitelist_and_blacklist_are_case_insensitive(self):
        self.assertTrue(cae._name_in_listing("Customers", ["CUSTOMERS", "ORDERS"]))
        self.assertTrue(cae._name_in_listing("orders", [{"name": "ORDERS"}]))
        run = SimpleNamespace(schema=SimpleNamespace(whitelist=["GS_IMP_CUSTOMERS"], blacklist=["sysx"]),
                              source_schema="dbo")
        self.assertTrue(cae._in_schema_whitelist(run, "gs_imp_customers", "gs_imp_customers"))
        self.assertTrue(cae._name_in_listing("skipme", ["SkipMe"]))

    def test_document_key_column_id_is_case_insensitive(self):
        run = SimpleNamespace(last_col=None)
        columns = {"columns": [{"name": "id", "id": 42}]}
        self.assertEqual(cae._document_key_column_id(run, columns, "ID", "T", False), 42)

    def test_destination_alias_renames_like_target(self):
        rels = yaml_relations([{"source": "NAME", "destination": "PRODUCT_NAME", "type": "VARCHAR"}])
        self.assertEqual(rels[0]["sources"], ["NAME"])
        self.assertEqual(rels[0]["targets"], ["PRODUCT_NAME"])

    def test_validation_column_lookup_is_case_insensitive(self):
        columns = {"columns": [{"name": "id"}, {"name": "status"}]}
        run = SimpleNamespace(
            source_schema="demo",
            offline_tables=set(),
            table_columns=lambda source, config: columns,
        )
        issues = cae._source_column_issues(
            run, "gs_imp_rev_items",
            {"columns": [{"source": "ID", "target": "ID"}, {"source": "STATUS", "target": "STATUS"}]},
        )
        self.assertEqual(issues, [])


class ColumnNamesResolveTests(unittest.TestCase):
    def test_resolve_prefers_exact_then_unique_ci(self):
        names = ColumnNames(["ID", "Name", "status"])
        self.assertEqual(names.resolve("ID"), "ID")
        self.assertEqual(names.resolve("name"), "Name")
        self.assertEqual(names.resolve("STATUS"), "status")
        self.assertIsNone(names.resolve("missing"))


if __name__ == "__main__":
    unittest.main()

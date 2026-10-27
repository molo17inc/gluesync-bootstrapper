#!/usr/bin/env python3
"""Items 1-11 of feat/simple-backup-import: remap, dry run, validation, on-existing, exit code, GET retries,
Core Hub version override, deterministic entity lookup and the DBMoto converter fixes. Synthetic data only."""

import argparse
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
for path in (TESTS_DIR, TESTS_DIR.parent / "dbmoto-converter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import import_options  # noqa: E402
import create_all_entities  # noqa: E402
from fake_corehub import FakeCoreHub, column, run_create_entities  # noqa: E402

V23 = "2.3.0.0 - build test"
SOURCE = {("src", "ORDERS"): [column("ID", 1, "int", is_pk=True, nullable=False), column("NOTE", 2)]}
TARGET = {("tgt", "orders"): [column("id", 101, "int", is_pk=True), column("note", 102)]}
YAML = """
src:
  target: tgt
  tables:
    whitelist: [ORDERS]
    custom:
      ORDERS:
        name: orders
        keys: [ID]
"""
TABLES = [{"name": "ORDERS", "schema": "src", "id": 500}]


def _run(options, hub=None, yaml_text=YAML):
    hub = hub or FakeCoreHub(SOURCE, TARGET, version=V23)
    with import_options.use(options):
        result = run_create_entities(hub, yaml_text, TABLES)
    return hub, result


class RemapTests(unittest.TestCase):
    def test_remap_file_and_flags(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump({"schemas": {"src": "SRC2"}, "targetSchemas": {"tgt": "TGT2"},
                            "groups": {"g1": "g2"}, "entityPrefix": "new_"}, handle)
        self.addCleanup(os.unlink, handle.name)
        parser = argparse.ArgumentParser()
        import_options.add_cli_arguments(parser)
        args = parser.parse_args(["--remap", handle.name, "--table-map", "src.ORDERS=ORDERS_V2"])
        remap = import_options.remap_from_args(args)
        out = remap.apply(yaml.safe_load(YAML))
        self.assertEqual(list(out), ["SRC2"])
        self.assertEqual(out["SRC2"]["target"], "TGT2")
        self.assertIn("ORDERS_V2", out["SRC2"]["tables"]["custom"])
        # the source table moves, its target table stays
        self.assertEqual(out["SRC2"]["tables"]["custom"]["ORDERS_V2"]["name"], "orders")
        self.assertEqual(remap.entity_name("src.ORDERS"), "new_src.ORDERS")

    def test_unknown_remap_section_is_refused(self):
        with self.assertRaises(ValueError):
            import_options.Remap.from_mapping({"schemaz": {}})

    def test_table_target_schema_is_remapped(self):
        remap = import_options.Remap(target_schemas={"TGT02": "TGT02_NEW"})
        out = remap.apply({"S": {"target": "T", "tables": {"custom": {"A": {"targetSchema": "TGT02"}}}}})
        self.assertEqual(out["S"]["tables"]["custom"]["A"]["targetSchema"], "TGT02_NEW")

    def test_no_remap_returns_the_yaml_unchanged(self):
        config = yaml.safe_load(YAML)
        self.assertIs(import_options.Remap().apply(config), config)


class DryRunTests(unittest.TestCase):
    def test_guard_write_blocks_writes_only(self):
        options = import_options.ImportOptions(dry_run=True)
        with import_options.use(options):
            import_options.guard_write("GET", "/pipelines")
            with self.assertRaises(import_options.DryRunWriteBlocked):
                import_options.guard_write("PUT", "/pipelines/p/config/entities")
        self.assertEqual(options.report.blocked_writes, ["PUT /pipelines/p/config/entities"])
        import_options.guard_write("PUT", "/x")  # no dry run: nothing blocked

    def test_dry_run_saves_no_entity(self):
        options = import_options.ImportOptions(dry_run=True)
        hub, result = _run(options)
        self.assertEqual(hub.puts, [])
        self.assertTrue(result.get("dry_run"))


class OnExistingTests(unittest.TestCase):
    def _hub_with_existing(self):
        hub = FakeCoreHub(SOURCE, TARGET, version=V23)
        _run(import_options.ImportOptions(), hub)
        self.assertEqual(len(hub.saved), 1)
        hub.puts.clear()
        return hub

    def test_skip(self):
        hub = self._hub_with_existing()
        options = import_options.ImportOptions(on_existing="skip")
        _run(options, hub)
        self.assertEqual(options.report.skipped_existing, ["src.ORDERS"])
        self.assertEqual(hub.puts, [])

    def test_fail(self):
        hub = self._hub_with_existing()
        options = import_options.ImportOptions(on_existing="fail")
        _hub, result = _run(options, hub)
        self.assertTrue(any("already in the pipeline" in e for e in result["errors"]))
        # A per-entity refusal: exit 0 as on main, the report carries it.
        self.assertEqual(create_all_entities._exit_code(result, options), 0)

    def test_invalid_choice(self):
        with self.assertRaises(ValueError):
            import_options.ImportOptions(on_existing="replace")


class ValidationTests(unittest.TestCase):
    BAD = YAML.replace("keys: [ID]", "keys: [ID]\n        columns:\n          - {source: NO_SUCH, target: x}")

    def test_issues_are_reported_and_the_run_goes_on(self):
        options = import_options.ImportOptions()
        _hub, result = _run(options, yaml_text=self.BAD)
        self.assertTrue(any("NO_SUCH" in i for i in options.report.validation_issues), options.report)
        self.assertFalse(result.get("validation_failed"))

    def test_strict_validate_stops_before_writing(self):
        options = import_options.ImportOptions(strict_validate=True)
        hub, result = _run(options, yaml_text=self.BAD)
        self.assertTrue(result.get("validation_failed"), result)
        self.assertEqual(hub.puts, [])
        self.assertEqual(create_all_entities._exit_code(result, options), 1)

    def test_no_validate_skips_the_check(self):
        options = import_options.ImportOptions(validate=False)
        _run(options, yaml_text=self.BAD)
        self.assertEqual(options.report.validation_issues, [])


class ExitCodeTests(unittest.TestCase):
    def test_codes(self):
        options = import_options.ImportOptions()
        self.assertEqual(create_all_entities._exit_code({"successful": 1, "failed": 0, "errors": []}, options), 0)
        # Per-entity failures: 0, as on main (main.py reads non-zero as "the schema could not be processed").
        self.assertEqual(create_all_entities._exit_code({"successful": 1, "failed": 1, "errors": ["x"]}, options), 0)
        self.assertEqual(create_all_entities._exit_code({"successful": 0, "failed": 2, "errors": ["x"]}, options), 0)
        self.assertEqual(create_all_entities._exit_code({"failed": 1, "aborted": True}, options), 1)
        self.assertEqual(create_all_entities._exit_code(None, options), 1)
        dry = import_options.ImportOptions(dry_run=True)
        dry.report.plan.append({"status": "fail"})
        self.assertEqual(create_all_entities._exit_code({"successful": 0, "failed": 0}, dry), 1)


class RetryTests(unittest.TestCase):
    def test_get_is_retried_on_connection_errors_then_succeeds(self):
        from utils import core_hub_client
        answers = [requests.exceptions.ConnectionError("down"), mock.Mock(status_code=503), mock.Mock(status_code=200)]

        def send():
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        sleeps = []
        with mock.patch.dict(os.environ, {"CORE_HUB_GET_RETRIES": "3", "CORE_HUB_RETRY_BACKOFF": "0.5"}):
            response = core_hub_client.retry_get(send, "GET /x", sleep=sleeps.append)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_4xx_is_not_retried(self):
        from utils import core_hub_client
        calls = []

        def send():
            calls.append(1)
            return mock.Mock(status_code=404)
        with mock.patch.dict(os.environ, {"CORE_HUB_GET_RETRIES": "3"}):
            core_hub_client.retry_get(send, "GET /x", sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_timeouts_come_from_the_environment(self):
        from utils import core_hub_client
        with mock.patch.dict(os.environ, {"CORE_HUB_CONNECT_TIMEOUT": "3", "CORE_HUB_TIMEOUT": "7"}):
            self.assertEqual(core_hub_client.request_timeout(), (3.0, 7.0))


class CoreHubVersionOverrideTests(unittest.TestCase):
    def test_flag_wins_over_env(self):
        import commons
        with mock.patch.dict(os.environ, {"COREHUB_VERSION": "2.2.0"}):
            self.assertEqual(commons.corehub_version_override(), "2.2.0")
            with import_options.use(import_options.ImportOptions(corehub_version="2.3.1")):
                self.assertEqual(commons.corehub_version_override(), "2.3.1")


class EntityLookupTests(unittest.TestCase):
    def _item(self, entity_id, name, schema, table):
        return {"entity": {"entityId": entity_id, "entityName": name,
                           "agentEntities": [{"table": {"schema": schema, "name": table}}]}}

    def test_same_table_in_two_schemas_is_deterministic_and_prefers_the_current_schema(self):
        listing = [self._item("e2", "B.ORDERS", "B", "ORDERS"), self._item("e1", "A.ORDERS", "A", "ORDERS")]
        self.assertEqual(create_all_entities._entity_ids_by_table(listing)["ORDERS"], "e1")
        self.assertEqual(create_all_entities._entity_ids_by_table(list(reversed(listing)))["ORDERS"], "e1")
        self.assertEqual(create_all_entities._entity_ids_by_table(listing, source_schema="B")["ORDERS"], "e2")


class DbmotoTests(unittest.TestCase):
    def test_template_match_is_exact_by_default(self):
        import parse_dbmoto_metadata_xml as dbm
        templates = {"sales_archive": ("SALES_ARCHIVE", {"t": 1})}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEMPLATE_SUBSTRING_MATCH", None)
            self.assertIsNone(dbm.find_matching_template_schema("SALES", templates))
        with mock.patch.dict(os.environ, {"TEMPLATE_SUBSTRING_MATCH": "true"}):
            self.assertEqual(dbm.find_matching_template_schema("SALES", templates), {"t": 1})

    def test_output_file_is_never_overwritten(self):
        import parse_dbmoto_metadata_xml as dbm
        written = set()
        first = dbm._unique_output_path("/out", "x.yaml", "S1", written)
        second = dbm._unique_output_path("/out", "x.yaml", "S2", written)
        third = dbm._unique_output_path("/out", "x.yaml", "S2", written)
        self.assertEqual(len({first, second, third}), 3)


class ExistingListingTests(unittest.TestCase):
    """Review #4/#5: a failed listing under --on-existing/resume aborts; the source-table fallback only when
    unambiguous."""

    def test_failed_listing_aborts_with_on_existing(self):
        hub = FakeCoreHub(SOURCE, TARGET, version=V23)
        options = import_options.ImportOptions(on_existing="skip")
        with mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=RuntimeError("503")):
            run = mock.Mock()
            with self.assertRaises(create_all_entities.ExistingEntitiesUnavailable):
                create_all_entities._existing_entities(run, required=True)
            self.assertEqual(create_all_entities._existing_entities(run), [])
        with mock.patch.object(create_all_entities, "_existing_entities",
                               side_effect=create_all_entities.ExistingEntitiesUnavailable("could not list")):
            hub, result = _run(options, hub)
        self.assertTrue(result.get("aborted"), result)
        self.assertEqual(hub.puts, [])
        self.assertEqual(hub.saved, [])
        self.assertEqual(create_all_entities._exit_code(result, options), 1)

    @staticmethod
    def _entity(name, table, entity_id=None, schema="S"):
        out = {"entityName": name, "agentEntities": [{"table": {"schema": schema, "name": table}}, {}]}
        if entity_id:
            out["entityId"] = entity_id
        return out

    def test_fallback_by_source_only_when_unambiguous(self):
        existing = [self._entity("old_T1", "SRC", "e1")]
        # entityPrefix remap: the only payload and the only entity reading SRC -> matched.
        payloads = [self._entity("new_T1", "SRC")]
        index = create_all_entities._ExistingIndex(existing, payloads)
        self.assertEqual(index.match(payloads[0])["entityId"], "e1")

    def test_fallback_refused_when_one_source_feeds_two_tables(self):
        for order in ([0, 1], [1, 0]):
            existing = [self._entity("old_T1", "SRC", "e1")]
            payloads = [self._entity("new_T1", "SRC"), self._entity("new_T2", "SRC")]
            payloads = [payloads[i] for i in order]
            index = create_all_entities._ExistingIndex(existing, payloads)
            self.assertEqual([index.match(p) for p in payloads], [None, None])

    def test_fallback_refused_when_two_existing_read_the_source(self):
        existing = [self._entity("T1", "SRC", "e1"), self._entity("T2", "SRC", "e2")]
        payloads = [self._entity("new_T1", "SRC")]
        index = create_all_entities._ExistingIndex(existing, payloads)
        self.assertIsNone(index.match(payloads[0]))

    def test_name_match_wins_and_is_not_stolen(self):
        existing = [self._entity("T1", "SRC", "e1")]
        payloads = [self._entity("T1", "SRC"), self._entity("T2", "OTHER")]
        index = create_all_entities._ExistingIndex(existing, payloads)
        index.payload_names = {"t1", "t2"}
        self.assertEqual(index.match(payloads[0])["entityId"], "e1")
        self.assertIsNone(index.match(payloads[1]))


if __name__ == "__main__":
    unittest.main()

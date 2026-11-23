#!/usr/bin/env python3
"""
Backup and restore of the transactions audit configuration of a pipeline.

Core Hub is faked by a small in-memory router: two Core Hubs with different ids, so the round trip proves that the
section names things (store pipeline and agent, entities) and the restore resolves them on the other side.
"""

import copy
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_roundtrip  # noqa: E402
import transactions_audit as ta  # noqa: E402


def http_error(status):
    response = requests.Response()
    response.status_code = status
    cause = requests.HTTPError(f"{status}", response=response)
    try:
        raise RuntimeError(f"CoreHub request failed: {cause}") from cause
    except RuntimeError as exc:
        return exc


class FakeHub:
    """pipelines: {id: {"name", "target": (agentId, tag), "entities": {entityId: name}, "audit": config or None, "state"}}"""

    def __init__(self, pipelines, supports_audit=True, refuse_put=None):
        self.pipelines = pipelines
        self.supports_audit = supports_audit
        self.refuse_put = refuse_put
        self.calls = []

    def __call__(self, path, method="GET", token=None, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path == "/pipelines":
            return [{"id": pid, "name": p["name"]} for pid, p in self.pipelines.items()]
        parts = path.strip("/").split("/")
        pipeline = self.pipelines[parts[1]]
        if len(parts) == 2:
            agent_id, tag = pipeline["target"]
            return {"id": parts[1], "name": pipeline["name"], "targetAgentsInfo": [{"agentId": agent_id, "agentTag": tag}]}
        assert parts[2] == "transactions-audit", path
        if not self.supports_audit:
            raise http_error(404)
        if method == "PUT":
            if self.refuse_put:
                raise http_error(self.refuse_put)
            pipeline["audit"] = body
            return {}
        return {
            "pipelineId": parts[1],
            "state": pipeline.get("state", "OFF"),
            **({"config": pipeline["audit"]} if pipeline.get("audit") else {}),
            "entities": [{"entityId": i, "entityName": n} for i, n in pipeline["entities"].items()],
        }

    def puts(self):
        return [c for c in self.calls if c[0] == "PUT"]

    def starts(self):
        return [c for c in self.calls if c[0] == "POST"]


def source_hub(state="RECORDING"):
    audit = {
        "store": {"pipelineId": "p-store", "agentId": "a-store"},
        "level": "TRACE",
        "scope": {"mode": "SELECTED", "entityIds": ["e2"]},
        "schema": "gluesync_audit",
        "layout": "TABLE_PER_ENTITY",
        "onStoreFailure": "HOLD",
    }
    return FakeHub({
        "p-main": {"name": "Orders", "target": ("a-main", "mssql"), "entities": {"e1": "orders", "e2": "items"},
                   "audit": audit, "state": state},
        "p-store": {"name": "Ledger host", "target": ("a-store", "postgres"), "entities": {}, "audit": None},
    })


def target_hub():
    return FakeHub({
        "n-main": {"name": "Orders", "target": ("b-main", "mssql"), "entities": {"f1": "orders", "f2": "items"}, "audit": None},
        "n-store": {"name": "Ledger host", "target": ("b-store", "postgres"), "entities": {}, "audit": None},
    })


class ExportTests(unittest.TestCase):
    def test_section_names_the_store_and_the_entities(self):
        hub = source_hub()
        section = ta.export_section("t", "p-main", hub)
        self.assertEqual(section["state"], "RECORDING")
        self.assertEqual(section["store"], {"pipelineId": "p-store", "pipelineName": "Ledger host",
                                            "agentTag": "postgres", "agentId": "a-store"})
        self.assertEqual(section["scope"], {"mode": "SELECTED", "entities": ["items"]})
        self.assertEqual((section["level"], section["onStoreFailure"], section["layout"]),
                         ("TRACE", "HOLD", "TABLE_PER_ENTITY"))
        self.assertNotIn("tableName", section)

    def test_the_own_store_is_not_tied_to_an_id(self):
        hub = source_hub()
        hub.pipelines["p-main"]["audit"]["store"] = {"pipelineId": "p-main", "agentId": "a-main"}
        self.assertEqual(ta.export_section("t", "p-main", hub)["store"],
                         {"own": True, "agentTag": "mssql", "agentId": "a-main"})

    def test_nothing_for_a_pipeline_without_audit_or_an_older_core_hub(self):
        self.assertIsNone(ta.export_section("t", "p-store", source_hub()))
        self.assertIsNone(ta.export_section("t", "p-main", FakeHub(source_hub().pipelines, supports_audit=False)))

    def test_an_html_answer_is_an_older_core_hub(self):
        self.assertIsNone(ta.read_status("t", "p", lambda *a, **k: "<!doctype html><html></html>"))

    def test_a_failed_read_leaves_an_error_instead_of_nothing(self):
        def broken(*args, **kwargs):
            raise http_error(500)
        yaml_data = {"dbo": {"target": "dbo", "tables": {}}}
        ta.export_into(yaml_data, "t", "p-main", broken)
        self.assertNotIn(ta.SECTION_KEY, yaml_data["dbo"])
        self.assertIn("transactionsAuditError", yaml_data["dbo"])

    def test_placement_follows_the_schedules(self):
        single = ta.export_into({"dbo": {"target": "dbo"}, "groups": {"g": {}}}, "t", "p-main", source_hub())
        self.assertIn(ta.SECTION_KEY, single["dbo"])
        self.assertNotIn(ta.SECTION_KEY, single)
        multi = ta.export_into({"schemas": {"a": {}, "b": {}}}, "t", "p-main", source_hub())
        self.assertIn(ta.SECTION_KEY, multi)
        self.assertNotIn(ta.SECTION_KEY, multi["schemas"]["a"])
        none = ta.export_into({}, "t", "p-main", source_hub())
        self.assertEqual(none, {})

    def test_find_section_in_every_place(self):
        section = {"store": {"own": True}}
        self.assertEqual(ta.find_section({ta.SECTION_KEY: section}), (section, None))
        self.assertEqual(ta.find_section({"schemas": {"a": {ta.SECTION_KEY: section}}}), (section, None))
        self.assertEqual(ta.find_section({"dbo": {ta.SECTION_KEY: section}}), (section, None))
        self.assertEqual(ta.find_section({"dbo": {ta.ERROR_KEY: "boom"}}), (None, "boom"))
        self.assertEqual(ta.find_section(None), (None, None))
        self.assertEqual(ta.find_section({"dbo": {}}), (None, None))


class RestoreTests(unittest.TestCase):
    def round_trip(self, mutate=None, id_map=None, target=None):
        source = source_hub()
        section = ta.export_section("t", "p-main", source)
        if mutate:
            mutate(section)
        target = target or target_hub()
        yaml_config = yaml.safe_load(yaml.safe_dump(ta.export_into({"dbo": {"target": "dbo"}}, "t", "p-main", source)))
        yaml_config["dbo"][ta.SECTION_KEY] = section
        result = ta.restore_from_yaml("t", "n-main", yaml_config, id_map if id_map is not None else {"p-main": "n-main", "p-store": "n-store"}, target)
        return result, target

    def test_round_trip_resolves_every_id_on_the_other_hub(self):
        result, target = self.round_trip()
        self.assertEqual(result["status"], ta.STATUS_RESTORED)
        self.assertEqual(target.pipelines["n-main"]["audit"], {
            "store": {"pipelineId": "n-store", "agentId": "b-store"},
            "scope": {"mode": "SELECTED", "entityIds": ["f2"]},
            "level": "TRACE", "schema": "gluesync_audit", "layout": "TABLE_PER_ENTITY", "onStoreFailure": "HOLD",
        })

    def test_a_restore_never_starts_and_says_it_was_recording(self):
        result, target = self.round_trip()
        self.assertEqual(target.starts(), [])
        self.assertEqual(len(target.puts()), 1)
        self.assertEqual(result["wasState"], "RECORDING")
        self.assertTrue(any("not started" in w for w in result["warnings"]))

    def test_a_stopped_audit_restores_without_that_warning(self):
        result, _ = self.round_trip(lambda s: s.update(state="OFF"))
        self.assertEqual(result["warnings"], [])

    def test_the_store_pipeline_is_found_by_name_when_it_is_not_in_the_backup(self):
        result, target = self.round_trip(id_map={})
        self.assertEqual(result["status"], ta.STATUS_RESTORED)
        self.assertEqual(target.pipelines["n-main"]["audit"]["store"]["pipelineId"], "n-store")

    def test_a_missing_store_pipeline_needs_attention_and_changes_nothing(self):
        target = target_hub()
        del target.pipelines["n-store"]
        result, _ = self.round_trip(id_map={}, target=target)
        self.assertEqual(result["status"], ta.STATUS_NEEDS_ATTENTION)
        self.assertIn("Ledger host", result["warnings"][0])
        self.assertEqual(target.puts(), [])

    def test_the_own_store_is_the_restored_pipeline(self):
        def own(section):
            section["store"] = {"own": True, "agentTag": "mssql"}
        result, target = self.round_trip(own)
        self.assertEqual(target.pipelines["n-main"]["audit"]["store"], {"pipelineId": "n-main", "agentId": "b-main"})
        self.assertEqual(result["status"], ta.STATUS_RESTORED)

    def test_a_renamed_store_agent_is_a_warning(self):
        target = target_hub()
        target.pipelines["n-store"]["target"] = ("b-store", "mysql")
        result, _ = self.round_trip(target=target)
        self.assertEqual(result["status"], ta.STATUS_RESTORED)
        self.assertTrue(any("'mysql'" in w for w in result["warnings"]))

    def test_an_entity_missing_after_the_restore_needs_attention(self):
        target = target_hub()
        del target.pipelines["n-main"]["entities"]["f2"]
        result, _ = self.round_trip(target=target)
        self.assertEqual(result["status"], ta.STATUS_NEEDS_ATTENTION)
        self.assertIn("items", result["warnings"][0])
        self.assertEqual(target.puts(), [])

    def test_scope_all_and_table_name_travel(self):
        def plain(section):
            section["scope"] = {"mode": "ALL"}
            section["layout"] = "SINGLE_TABLE"
            section["tableName"] = "LEDGER"
        _, target = self.round_trip(plain)
        audit = target.pipelines["n-main"]["audit"]
        self.assertEqual((audit["scope"], audit["tableName"]), ({"mode": "ALL", "entityIds": []}, "LEDGER"))

    def test_core_hub_refusing_the_configuration_is_reported_not_raised(self):
        target = target_hub()
        target.refuse_put = 400
        result, _ = self.round_trip(target=target)
        self.assertEqual(result["status"], ta.STATUS_FAILED)

    def test_an_older_core_hub_skips(self):
        target = target_hub()
        target.supports_audit = False
        result, _ = self.round_trip(target=target)
        self.assertEqual(result["status"], ta.STATUS_SKIPPED)
        self.assertIn("2.3.4.0", result["warnings"][0])

    def test_the_error_of_the_export_is_reported(self):
        result = ta.restore_from_yaml("n-main", "n-main", {"dbo": {ta.ERROR_KEY: "boom"}}, {}, target_hub())
        self.assertEqual(result["status"], ta.STATUS_NEEDS_ATTENTION)
        self.assertIn("boom", result["warnings"][0])

    def test_nothing_to_restore_when_the_yaml_has_no_audit(self):
        self.assertIsNone(ta.restore_from_yaml("t", "n-main", {"dbo": {"target": "dbo"}}, {}, target_hub()))


class LegacyShapeTests(unittest.TestCase):
    def restore(self, legacy, hub=None):
        hub = hub or target_hub()
        return ta.restore_section("t", "n-main", legacy, {}, hub), hub

    def test_the_agent_named_by_tag_becomes_an_own_store_at_trace(self):
        result, hub = self.restore({"enabled": True, "targetAgentId": "mssql", "deepTrace": True})
        self.assertEqual(result["status"], ta.STATUS_RESTORED)
        audit = hub.pipelines["n-main"]["audit"]
        self.assertEqual((audit["store"], audit["level"]), ({"pipelineId": "n-main", "agentId": "b-main"}, "TRACE"))
        self.assertEqual(hub.starts(), [])

    def test_the_agent_named_by_id_and_the_lone_target_fallback(self):
        self.assertEqual(self.restore({"enabled": True, "targetAgentId": "b-main"})[0]["status"], ta.STATUS_RESTORED)
        self.assertEqual(self.restore({"enabled": True})[0]["status"], ta.STATUS_RESTORED)

    def test_disabled_asks_for_nothing(self):
        result, hub = self.restore({"enabled": False})
        self.assertEqual(result["status"], ta.STATUS_SKIPPED)
        self.assertEqual(hub.puts(), [])

    def test_an_unknown_agent_with_no_target_to_fall_back_on_needs_attention(self):
        self.assertIsNone(ta.legacy_to_section({"enabled": True, "targetAgentId": "nope"}, []))
        two = [{"agentId": "1", "agentTag": "a"}, {"agentId": "2", "agentTag": "b"}]
        self.assertIsNone(ta.legacy_to_section({"enabled": True, "targetAgentId": "nope"}, two))


class RoundTripCheckTests(unittest.TestCase):
    def side(self, section):
        return {"transactionsAudit": section}

    def test_state_and_ids_do_not_count(self):
        original = ta.export_section("t", "p-main", source_hub())
        restored = copy.deepcopy(original)
        restored["state"] = "OFF"
        restored["store"]["agentId"] = "other"
        check = automator_roundtrip.check_transactions_audit(self.side(original), self.side(restored))
        self.assertTrue(check.passed)

    def test_a_changed_level_fails(self):
        original = ta.export_section("t", "p-main", source_hub())
        restored = dict(original, level="PULSE")
        check = automator_roundtrip.check_transactions_audit(self.side(original), self.side(restored))
        self.assertFalse(check.passed)
        self.assertTrue(check.diff)

    def test_a_lost_audit_fails_and_none_on_both_sides_passes(self):
        original = ta.export_section("t", "p-main", source_hub())
        self.assertFalse(automator_roundtrip.check_transactions_audit(self.side(original), self.side(None)).passed)
        self.assertTrue(automator_roundtrip.check_transactions_audit(self.side(None), self.side(None)).passed)

    def test_the_check_runs_only_when_a_side_has_an_audit(self):
        bare = {"entities": [], "groups": [], "jobs": [], "export": "", "idMap": {}, "pipeline": {}}
        with mock.patch.object(automator_roundtrip, "check_entities"), \
                mock.patch.object(automator_roundtrip, "check_groups"), \
                mock.patch.object(automator_roundtrip, "check_schedules"), \
                mock.patch.object(automator_roundtrip, "check_export_stable"):
            names = [c for c in automator_roundtrip.run_checks(bare, bare)]
            with_audit = automator_roundtrip.run_checks(dict(bare, transactionsAudit={"level": "TRACE"}), bare)
        self.assertEqual(len(with_audit), len(names) + 1)

    def test_export_stability_ignores_the_state_of_the_audit(self):
        section = ta.export_section("t", "p-main", source_hub())
        doc = {"dbo": {"target": "dbo", ta.SECTION_KEY: copy.deepcopy(section)}}
        automator_roundtrip._drop_audit_volatile(doc)
        self.assertNotIn("state", doc["dbo"][ta.SECTION_KEY])
        self.assertEqual(doc["dbo"][ta.SECTION_KEY]["store"]["agentTag"], "postgres")


class AutomatorRestoreTests(unittest.TestCase):
    def test_every_restored_pipeline_gets_its_audit_after_all_exist(self):
        from automator_app import app
        source = source_hub()
        yaml_text = yaml.safe_dump(ta.export_into({"dbo": {"target": "dbo"}}, "t", "p-main", source))
        target = target_hub()
        job = app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None)
        created = {"pipelineId": "n-main", "pipelineName": "Orders"}
        store_created = {"pipelineId": "n-store", "pipelineName": "Ledger host"}
        sources = [("a.yaml", "p-main", created, yaml_text), ("b.yaml", "p-store", store_created, "dbo: {target: dbo}\n")]
        with mock.patch.object(ta, "fetch_core_hub", target), mock.patch.object(type(app.state), "token", new_callable=mock.PropertyMock, return_value="t"):
            app._restore_transactions_audits(job, sources)
        self.assertEqual(target.pipelines["n-main"]["audit"]["store"], {"pipelineId": "n-store", "agentId": "b-store"})
        self.assertEqual(target.starts(), [])
        self.assertTrue(any("restored (not started)" in n for n in job.notes))
        self.assertTrue(any("not started" in w for w in job.warnings))

    def test_a_failure_is_a_warning_never_an_error(self):
        from automator_app import app
        yaml_text = yaml.safe_dump({"dbo": {ta.SECTION_KEY: {"store": {"own": True}, "level": "TRACE"}}})
        target = target_hub()
        target.refuse_put = 500
        job = app._ArchiveImport(contents=b"", agents_list=[], meta_by_old_id={}, udf_root=None)
        with mock.patch.object(ta, "fetch_core_hub", target), mock.patch.object(type(app.state), "token", new_callable=mock.PropertyMock, return_value="t"):
            app._restore_transactions_audits(job, [("a.yaml", "p-main", {"pipelineId": "n-main", "pipelineName": "Orders"}, yaml_text)])
        self.assertEqual(job.errors, [])
        self.assertTrue(job.warnings)


if __name__ == "__main__":
    unittest.main()

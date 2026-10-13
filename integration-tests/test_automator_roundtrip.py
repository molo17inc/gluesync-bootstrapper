#!/usr/bin/env python3
"""Offline tests for the comparison logic of automator_roundtrip.py."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_roundtrip as rt  # noqa: E402


def _entity(entity_id, name, source_agent, target_agent, group_id=None, status="RUNNING"):
    entity = {
        "entityId": entity_id,
        "entityName": name,
        "status": status,
        "agentEntities": [
            {"agentId": source_agent, "type": "SOURCE", "table": {"schema": "public", "name": name.lower()},
             "keys": ["id"], "customProperties": {"pollingIntervalMilliseconds": 100}},
            {"agentId": target_agent, "type": "TARGET", "table": {"schema": "demo", "name": name},
             "keys": ["ID"], "customProperties": {"ttlValue": 0}},
        ],
    }
    if group_id:
        entity["groupId"] = group_id
    return entity


def _side(pipeline_id, suffix, entities=None, groups=None, jobs=None, export=None):
    src, tgt = f"src{suffix}", f"tgt{suffix}"
    gid = f"grp{suffix}"
    if entities is None:
        entities = [
            _entity(f"ent-drivers{suffix}", "DRIVERS", src, tgt, gid),
            _entity(f"ent-vehicles{suffix}", "VEHICLES", src, tgt),
        ]
    if groups is None:
        groups = [{"groupId": gid, "name": "nightly", "description": "", "color": "#fff"}]
    if jobs is None:
        jobs = [{"id": 7 if suffix == "a" else 99, "pipeline_id": pipeline_id, "name": "snap",
                 "task_type": "group_snapshot", "group_ids": [gid], "cron_expression": "0 2 * * *",
                 "enabled": True, "next_run_at": f"2026-10-1{len(suffix)}T02:00:00Z"}]
    agents = [{"agentId": src, "agentType": "SOURCE"}, {"agentId": tgt, "agentType": "TARGET"}]
    if export is None:
        export = (
            f"# Pipeline ID     : {pipeline_id}\n# Exported at     : now\n\n"
            "public:\n  target: demo\n  tables:\n    whitelist: [drivers, vehicles]\n"
            f"groups:\n  nightly:\n    id: {gid}\n"
        )
    return {
        "pipelineId": pipeline_id,
        "entities": entities,
        "groups": groups,
        "jobs": jobs,
        "export": export,
        "idMap": rt.build_id_map(pipeline_id, agents, entities, groups),
    }


class NormalizeTests(unittest.TestCase):
    def test_ids_become_names_and_runtime_keys_go(self):
        id_map = {"a5d110b3": "<pipeline>", "ent-1": "<entity:DRIVERS>"}
        value = {"entityId": "ent-1", "status": "RUNNING", "chainId": "chain_a5d110b3",
                 "nested": [{"metrics": {"rows": 3}, "ref": "ent-1"}]}
        self.assertEqual(
            rt.normalize(value, id_map),
            {"entityId": "<entity:DRIVERS>", "chainId": "chain_<pipeline>", "nested": [{"ref": "<entity:DRIVERS>"}]},
        )

    def test_short_ids_are_only_replaced_whole(self):
        self.assertEqual(rt.normalize("id-12345", {"id": "<x>"}), "id-12345")
        self.assertEqual(rt.normalize("id", {"id": "<x>"}), "<x>")

    def test_named_lists_are_compared_as_sets_plain_lists_keep_order(self):
        cols_a = [{"name": "email", "position": 4}, {"name": "id", "position": 1}]
        cols_b = list(reversed(cols_a))
        self.assertEqual(rt.normalize(cols_a, {}), rt.normalize(cols_b, {}))
        self.assertNotEqual(rt.normalize(["b", "a"], {}), rt.normalize(["a", "b"], {}))
        self.assertNotEqual(
            rt.normalize([{"name": "email", "position": 4}], {}),
            rt.normalize([{"name": "email", "position": 1}], {}),
        )

    def test_agent_entity_ids_are_mapped_per_side(self):
        entities = [{"entityId": "e1", "entityName": "T", "agentEntities": [
            {"agentEntityId": "76e5ee55", "agentId": "src1"}, {"agentEntityId": "aaaa1111", "agentId": "tgt1"}]}]
        id_map = rt.build_id_map("p", [{"agentId": "src1", "agentType": "SOURCE"},
                                       {"agentId": "tgt1", "agentType": "TARGET"}], entities, [])
        self.assertEqual(id_map["76e5ee55"], "<agentEntity:T:SOURCE>")
        self.assertEqual(id_map["aaaa1111"], "<agentEntity:T:TARGET>")

    def test_key_by_name_keeps_duplicates(self):
        keyed = rt.key_by_name([{"n": "a"}, {"n": "a"}, {"n": "b"}], "n")
        self.assertEqual(sorted(keyed), ["a", "a#2", "b"])

    def test_strip_export_header(self):
        self.assertEqual(rt.strip_export_header("# x\n# y\n\nroot:\n  k: 1\n"), "root:\n  k: 1\n")


class RunChecksTests(unittest.TestCase):
    def test_identical_configuration_passes(self):
        checks = rt.run_checks(_side("a5d110b3", "a"), _side("c0ffee00", "b"))
        self.assertTrue(all(c.passed for c in checks), [c.to_dict() for c in checks])

    def test_missing_entity_fails_with_name(self):
        restored = _side("c0ffee00", "b")
        restored["entities"] = restored["entities"][:1]
        check = rt.check_entities(_side("a5d110b3", "a"), restored)
        self.assertFalse(check.passed)
        self.assertIn("missing after restore: VEHICLES", check.message)

    def test_changed_custom_property_fails_with_diff(self):
        restored = _side("c0ffee00", "b")
        restored["entities"][0]["agentEntities"][1]["customProperties"]["ttlValue"] = 10
        check = rt.check_entities(_side("a5d110b3", "a"), restored)
        self.assertFalse(check.passed)
        self.assertIn("configured differently: DRIVERS", check.message)
        self.assertTrue(any('"ttlValue": 10' in line for line in check.diff))

    def test_effective_source_settings_written_out_on_restore_pass(self):
        original = _side("a5d110b3", "a")
        restored = _side("c0ffee00", "b")
        for side in (original, restored):
            side["entities"][0]["agentEntities"][0]["entityType"] = {"type": "Source", "snapshotOnly": False}
        restored["entities"][0]["agentEntities"][0]["customProperties"]["snapshotOnly"] = False
        self.assertTrue(rt.check_entities(original, restored).passed)

    def test_extra_source_property_that_is_not_effective_fails(self):
        original = _side("a5d110b3", "a")
        restored = _side("c0ffee00", "b")
        for side in (original, restored):
            side["entities"][0]["agentEntities"][0]["entityType"] = {"type": "Source", "snapshotOnly": False}
        restored["entities"][0]["agentEntities"][0]["customProperties"]["snapshotOnly"] = True
        self.assertFalse(rt.check_entities(original, restored).passed)

    def test_lost_source_property_fails(self):
        restored = _side("c0ffee00", "b")
        del restored["entities"][0]["agentEntities"][0]["customProperties"]["pollingIntervalMilliseconds"]
        self.assertFalse(rt.check_entities(_side("a5d110b3", "a"), restored).passed)

    def test_extra_target_property_fails(self):
        restored = _side("c0ffee00", "b")
        restored["entities"][0]["agentEntities"][1]["customProperties"]["preSnapshotCommand"] = "TRUNCATE x"
        self.assertFalse(rt.check_entities(_side("a5d110b3", "a"), restored).passed)

    def test_lost_group_membership_fails(self):
        restored = _side("c0ffee00", "b")
        del restored["entities"][0]["groupId"]
        self.assertFalse(rt.check_entities(_side("a5d110b3", "a"), restored).passed)

    def test_dropped_schedule_fails(self):
        restored = _side("c0ffee00", "b", jobs=[])
        check = rt.check_schedules(_side("a5d110b3", "a"), restored)
        self.assertFalse(check.passed)
        self.assertIn("missing after restore: snap", check.message)

    def test_export_drift_fails(self):
        restored = _side("c0ffee00", "b")
        restored["export"] = restored["export"].replace("[drivers, vehicles]", "[drivers]")
        check = rt.check_export_stable(_side("a5d110b3", "a"), restored)
        self.assertFalse(check.passed)
        self.assertTrue(any(line.startswith("-") and "vehicles" in line for line in check.diff))

    def test_empty_pipeline_is_a_failure(self):
        check = rt.check_entities(_side("a5d110b3", "a", entities=[]), _side("c0ffee00", "b", entities=[]))
        self.assertFalse(check.passed)


class JunitTests(unittest.TestCase):
    def test_junit_is_valid_and_counts_failures(self):
        ok = rt.Check("one", "d")
        ok.message = "fine"
        bad = rt.Check("two", "d")
        bad.fail('broke & "quoted" <tag>', ["-a", "+b"])
        root = ElementTree.fromstring(rt.junit_xml("kit", [ok, bad], 1.5))
        self.assertEqual(root.get("tests"), "2")
        self.assertEqual(root.get("failures"), "1")
        failure = root.find("./testcase[@name='two']/failure")
        self.assertEqual(failure.get("message"), 'broke & "quoted" <tag>')
        self.assertIn("+b", failure.text)


class RunTests(unittest.TestCase):
    """The orchestration, with CoreHub and the restore subprocess stubbed."""

    def _run(self, restored_side, restore_result=("c0ffee00", "")):
        original_side = _side("a5d110b3", "a")
        sides = {"a5d110b3": original_side, "c0ffee00": restored_side}

        def snapshot(_token, pid):
            side = copy.deepcopy(sides[pid])
            side["pipeline"] = {"name": "kit pipeline"}
            return {k: v for k, v in side.items() if k != "export"}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(rt, "authenticate", return_value="tok"), \
                mock.patch.object(rt, "snapshot_pipeline", side_effect=snapshot), \
                mock.patch.object(rt, "render_pipeline_export", side_effect=lambda _t, pid: sides[pid]["export"]), \
                mock.patch.object(rt, "restore_export", return_value=restore_result) as restore:
            code = rt.run("a5d110b3", Path(tmp), "/nonexistent", 60, "kit")
            report = json.loads((Path(tmp) / "report.json").read_text())
            files = sorted(p.name for p in Path(tmp).iterdir())
        return code, report, files, restore

    def test_green_round_trip(self):
        code, report, files, restore = self._run(_side("c0ffee00", "b"))
        self.assertEqual(code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 5)
        self.assertEqual(restore.call_args.args[1], "kit pipeline [automator round-trip]")
        self.assertIn("original-export.yaml", files)
        self.assertIn("restored-export.yaml", files)
        self.assertIn("junit.xml", files)

    def test_failed_restore_stops_and_fails(self):
        code, report, files, _ = self._run(_side("c0ffee00", "b"), restore_result=(None, "restore exploded"))
        self.assertEqual(code, 1)
        self.assertEqual([c["name"] for c in report["checks"]], ["export-and-restore"])
        self.assertEqual(report["checks"][0]["message"], "restore exploded")

    def test_mismatch_writes_diff_file(self):
        restored = _side("c0ffee00", "b")
        restored["entities"] = restored["entities"][:1]
        code, report, files, _ = self._run(restored)
        self.assertEqual(code, 1)
        self.assertIn("restored-entities-match.diff", files)


class RestoreExportTests(unittest.TestCase):
    def test_restore_runs_main_without_sync_and_reads_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def fake_run(cmd, cwd, env, stdout, stderr, timeout, check):
                self.assertEqual(env["START_SYNC"], "false")
                self.assertEqual(env["CREATE_TABLE_IF_NOT_EXISTS"], "false")
                self.assertEqual(env["TABLE_LIST_YAML"], str(out / "x.yaml"))
                self.assertEqual(cmd[-2:], ["--pipeline-name", "p [automator round-trip]"])
                Path(env["BOOTSTRAPPER_RESULT_FILE"]).write_text(
                    json.dumps({"pipelineId": "c0ffee00", "status": "complete"}))
                return mock.Mock(returncode=0)

            with mock.patch.object(rt.subprocess, "run", side_effect=fake_run):
                self.assertEqual(
                    rt.restore_export(out / "x.yaml", "p [automator round-trip]", out, 60), ("c0ffee00", ""))

    def test_restore_without_result_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(returncode=3)):
            pid, error = rt.restore_export(Path(tmp) / "x.yaml", "p", Path(tmp), 60)
        self.assertIsNone(pid)
        self.assertIn("code 3", error)


if __name__ == "__main__":
    unittest.main()

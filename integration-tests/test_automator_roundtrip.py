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
        "agents": agents,
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

    def test_missing_ids_never_become_placeholders(self):
        id_map = rt.build_id_map("", [{"agentType": "SOURCE"}, {"agentId": "", "agentType": "TARGET"}],
                                 [{"entityName": "T"}], [{"name": "g"}])
        self.assertEqual(id_map, {})
        self.assertEqual(rt.normalize({"note": ""}, id_map), {"note": ""})

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

    def test_chronos_run_state_is_ignored(self):
        original = _side("a5d110b3", "a")
        restored = _side("c0ffee00", "b")
        original["jobs"][0].update({"is_cron_expression": True, "start_time": "08:55", "command": "Scheduled job ID: job_1",
                                    "cron_job_identifier": "gluesync_job_1", "last_successful_run": "08:55"})
        restored["jobs"][0].update({"is_cron_expression": True, "start_time": "09:05", "command": "pending",
                                    "cron_job_identifier": "gluesync_job_2", "last_successful_run": None})
        self.assertTrue(rt.check_schedules(original, restored).passed)

    def test_start_time_of_a_one_off_schedule_is_compared(self):
        original = _side("a5d110b3", "a")
        restored = _side("c0ffee00", "b")
        original["jobs"][0].update({"is_cron_expression": False, "start_time": "08:55"})
        restored["jobs"][0].update({"is_cron_expression": False, "start_time": "09:05"})
        self.assertFalse(rt.check_schedules(original, restored).passed)

    def test_unreadable_chronos_passes_only_without_declared_schedules(self):
        original = _side("a5d110b3", "a", jobs=None)
        restored = _side("c0ffee00", "b", jobs=None)
        original["jobs"] = restored["jobs"] = None
        self.assertTrue(rt.check_schedules(original, restored).passed)
        check = rt.check_schedules(original, restored, expects_schedules=True)
        self.assertFalse(check.passed)
        self.assertIn("could not be read", check.message)

    def test_declared_schedules_missing_from_chronos_fail(self):
        original = _side("a5d110b3", "a", jobs=[])
        restored = _side("c0ffee00", "b", jobs=[])
        original["jobs"] = restored["jobs"] = []
        self.assertTrue(rt.check_schedules(original, restored).passed)
        self.assertFalse(rt.check_schedules(original, restored, expects_schedules=True).passed)

    def test_declares_schedules_finds_any_level(self):
        self.assertTrue(rt.declares_schedules(
            {"public": {"tables": {"custom": {"DRIVERS": {"schedules": [{"name": "s"}]}}}}}))
        self.assertTrue(rt.declares_schedules({"public": {"group_schedules": [{"name": "g"}]}}))
        self.assertFalse(rt.declares_schedules({"public": {"schedules": [], "tables": {"whitelist": ["a"]}}}))
        self.assertFalse(rt.declares_schedules(None))

    def test_export_drift_fails(self):
        restored = _side("c0ffee00", "b")
        restored["export"] = restored["export"].replace("[drivers, vehicles]", "[drivers]")
        check = rt.check_export_stable(_side("a5d110b3", "a"), restored)
        self.assertFalse(check.passed)
        self.assertTrue(any(line.startswith("-") and "vehicles" in line for line in check.diff))

    def test_empty_pipeline_is_a_failure(self):
        check = rt.check_entities(_side("a5d110b3", "a", entities=[]), _side("c0ffee00", "b", entities=[]))
        self.assertFalse(check.passed)


SRC_TABLE_ID = 3401175334927524529


def _table_entity(target_table_id, target_name="TEST_CREATE_TABLE", target_schema="demo", partitions=None,
                  partition_settings="keep", source_agent="srcX", target_agent="tgtX"):
    """An entity shaped like the informix -> mysql export: ids on the table, columns and mapping matrix."""
    def column(col_id, name, table_id):
        return {"id": col_id, "name": name, "dataType": "int", "isPK": name == "ID", "tableId": table_id}

    source_type = {
        "type": "Source", "snapshotOnly": False,
        "columnsMappingMatrix": [],
    }
    if partition_settings == "keep":
        source_type["partitionSettings"] = {
            "column": column(11, "ID", SRC_TABLE_ID),
            "partitions": partitions if partitions is not None else [],
        }
    elif partition_settings is not None:
        source_type["partitionSettings"] = partition_settings
    return {
        "entityId": "ab6d213a",
        "entityName": "informix.test_create_table",
        "agentEntities": [
            {"agentId": source_agent, "agentEntityId": "c1437eab", "type": "SingleTable", "customProperties": {},
             "table": {"id": SRC_TABLE_ID, "name": "test_create_table", "schema": "informix", "type": "TABLE"},
             "columns": [column(11, "ID", SRC_TABLE_ID), column(12, "Tn", SRC_TABLE_ID)],
             "entityType": source_type},
            {"agentId": target_agent, "agentEntityId": "60cc50a8", "type": "SingleTable",
             "table": {"id": target_table_id, "name": target_name, "schema": target_schema, "type": "TABLE"},
             "columns": [column(21, "ID", target_table_id), column(22, "Tn", target_table_id)],
             "entityType": {"type": "Target", "columnsMappingMatrix": [
                 {"sourceColumnIds": [11], "targetColumnIds": [21],
                  "sourceTableObjectId": SRC_TABLE_ID, "targetTableObjectId": target_table_id},
                 {"sourceColumnIds": [12], "targetColumnIds": [22],
                  "sourceTableObjectId": SRC_TABLE_ID, "targetTableObjectId": target_table_id},
             ]}},
        ],
    }


def _table_side(pipeline_id, suffix, entity):
    side = _side(pipeline_id, suffix, entities=[entity], jobs=[])
    agents = [{"agentId": "srcX", "agentType": "SOURCE"}, {"agentId": "tgtX", "agentType": "TARGET"}]
    side["agents"] = agents
    side["idMap"] = rt.build_id_map(pipeline_id, agents, [entity], side["groups"])
    return side


class TableIdTests(unittest.TestCase):
    """Table ids are generated by the bootstrapper before a target table exists, CoreHub's once it does."""

    def test_informix_shape_generated_vs_corehub_target_table_id_passes(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387))
        restored = _table_side("c0ffee00", "b", _table_entity(1917908521409706403))
        check = rt.check_entities(original, restored)
        self.assertTrue(check.passed, check.message + "\n".join(check.diff[:20]))

    def test_other_table_name_still_fails(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387))
        restored = _table_side("c0ffee00", "b", _table_entity(1917908521409706403, target_name="OTHER_TABLE"))
        check = rt.check_entities(original, restored)
        self.assertFalse(check.passed)
        self.assertTrue(any("OTHER_TABLE" in line for line in check.diff))

    def test_other_schema_still_fails(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387))
        restored = _table_side("c0ffee00", "b", _table_entity(1917908521409706403, target_schema="other"))
        self.assertFalse(rt.check_entities(original, restored).passed)

    def test_a_reference_to_the_wrong_table_still_fails(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387))
        entity = _table_entity(1917908521409706403)
        entity["agentEntities"][1]["entityType"]["columnsMappingMatrix"][0]["targetTableObjectId"] = SRC_TABLE_ID
        self.assertFalse(rt.check_entities(original, _table_side("c0ffee00", "b", entity)).passed)

    def test_unknown_table_id_is_kept_and_compared(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387))
        entity = _table_entity(1917908521409706403)
        entity["agentEntities"][1]["columns"][0]["tableId"] = 42
        self.assertFalse(rt.check_entities(original, _table_side("c0ffee00", "b", entity)).passed)

    def test_ids_as_strings_and_multitable_headers_are_normalised(self):
        def multi(table_id):
            return {"entityId": "e1", "entityName": "chain", "agentEntities": [
                {"agentId": "tgtX", "agentEntityId": "t1", "type": "MultiTable",
                 "tables": [{"id": table_id, "name": "T1", "schema": "demo"}],
                 "columns": [{"id": str(table_id), "name": "T1", "schema": "demo"},
                             [{"id": 5, "name": "ID", "tableId": table_id}]],
                 "keys": [{"id": str(table_id), "name": "T1", "schema": "demo"}, ["ID"]]}]}
        normalised = rt.normalize_table_ids(multi(1162263133), {"tgtX": "TARGET"})
        again = rt.normalize_table_ids(multi(-7666519659296244926), {"tgtX": "TARGET"})
        self.assertEqual(normalised, again)
        self.assertEqual(normalised["agentEntities"][0]["tables"][0]["id"], "<table:TARGET:demo.T1>")
        self.assertEqual(normalised["agentEntities"][0]["columns"][1][0]["tableId"], "<table:TARGET:demo.T1>")
        self.assertEqual(normalised["agentEntities"][0]["columns"][1][0]["id"], 5)

    def test_nosql_target_sharing_the_source_table_id_after_restore_passes(self):
        """CoreHub gives a source and a target table of the same name the same id."""
        def entity(target_id):
            return {"entityId": "e1", "entityName": "demo.docs", "agentEntities": [
                {"agentId": "srcX", "type": "NoSqlEntity", "customProperties": {}, "entityObject": {"collection": "docs", "scope": "demo", "id": 77},
                 "columns": [{"id": 1, "name": "_id", "tableId": 77}], "entityType": {"type": "Source"}},
                {"agentId": "tgtX", "type": "NoSqlEntity", "entityObject": {"collection": "docs", "scope": "demo", "id": target_id},
                 "columns": [{"id": 2, "name": "_id", "tableId": target_id}],
                 "entityType": {"type": "Target", "columnsMappingMatrix": [
                     {"sourceTableObjectId": 77, "targetTableObjectId": target_id}]}}]}
        original = _table_side("a5d110b3", "a", entity(135281843))
        restored = _table_side("c0ffee00", "b", entity(77))
        check = rt.check_entities(original, restored)
        self.assertTrue(check.passed, check.message + "\n".join(check.diff[:20]))

    def test_table_ids_match_by_exact_value_only(self):
        entity = {"agentEntities": [{"agentId": "tgtX", "table": {"id": 123456789, "name": "T", "schema": "s"},
                                     "columns": [{"tableId": 1234567890}]}]}
        out = rt.normalize_table_ids(entity, {"tgtX": "TARGET"})
        self.assertEqual(out["agentEntities"][0]["columns"][0]["tableId"], 1234567890)

    def test_input_entity_is_not_modified(self):
        entity = _table_entity(-639482387)
        before = copy.deepcopy(entity)
        rt.normalize_table_ids(entity, {"srcX": "SOURCE", "tgtX": "TARGET"})
        self.assertEqual(entity, before)


class PartitionSettingsTests(unittest.TestCase):
    RANGES = [{"id": 1, "startValue": "1", "endValue": "100001"}, {"id": 2, "startValue": "100001", "endValue": "200001"}]

    def test_computed_partition_ranges_are_ignored(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387, partitions=self.RANGES))
        restored = _table_side("c0ffee00", "b", _table_entity(1917908521409706403, partitions=[]))
        self.assertTrue(rt.check_entities(original, restored).passed)

    def test_partition_column_is_still_compared(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387, partitions=self.RANGES))
        entity = _table_entity(1917908521409706403, partitions=[])
        entity["agentEntities"][0]["entityType"]["partitionSettings"]["column"]["name"] = "OTHER"
        self.assertFalse(rt.check_entities(original, _table_side("c0ffee00", "b", entity)).passed)

    def test_missing_partition_settings_after_restore_fails(self):
        original = _table_side("a5d110b3", "a", _table_entity(-639482387, partitions=self.RANGES))
        restored = _table_side("c0ffee00", "b", _table_entity(1917908521409706403, partition_settings=None))
        check = rt.check_entities(original, restored)
        self.assertFalse(check.passed)
        self.assertTrue(any("partitionSettings" in line for line in check.diff))

    def test_other_partition_settings_fields_are_still_compared(self):
        original = _table_entity(-639482387, partition_settings={"column": None, "partitions": self.RANGES, "mode": "a"})
        restored = _table_entity(-639482387, partition_settings={"column": None, "partitions": [], "mode": "b"})
        self.assertFalse(rt.check_entities(_table_side("a5d110b3", "a", original),
                                           _table_side("c0ffee00", "b", restored)).passed)


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

    def test_failed_restore_message_reaches_the_report(self):
        message = "restore of pipeline c0ffee00 failed (see restore.log): ERROR - Error running entity creation script"
        code, report, files, _ = self._run(_side("c0ffee00", "b"), restore_result=("c0ffee00", message))
        self.assertEqual(code, 1)
        self.assertFalse(report["checks"][0]["passed"])
        self.assertEqual(report["checks"][0]["name"], "export-and-restore")
        self.assertEqual(report["checks"][0]["message"], message)
        self.assertNotIn("restored-export.yaml", files)

    def test_mismatch_writes_diff_file(self):
        restored = _side("c0ffee00", "b")
        restored["entities"] = restored["entities"][:1]
        code, report, files, _ = self._run(restored)
        self.assertEqual(code, 1)
        self.assertIn("restored-entities-match.diff", files)


class AuthenticateTests(unittest.TestCase):
    def test_sdk_token_comes_first_when_use_sdk_is_on(self):
        with mock.patch.dict("os.environ", {"USE_SDK": "true"}), \
                mock.patch("utils.gluesync_sdk_client.initialize_gluesync_sdk"), \
                mock.patch("utils.gluesync_sdk_client.get_token", return_value="sdk"), \
                mock.patch.object(rt, "fetch_core_hub") as fetch:
            self.assertEqual(rt.authenticate("/nonexistent"), "sdk")
        fetch.assert_not_called()

    def test_sdk_is_skipped_when_use_sdk_is_off(self):
        with mock.patch.dict("os.environ", {"USE_SDK": "false"}), \
                mock.patch("utils.gluesync_sdk_client.initialize_gluesync_sdk") as init, \
                mock.patch.object(rt, "fetch_core_hub", return_value={"apiToken": "fresh"}):
            self.assertEqual(rt.authenticate("/nonexistent"), "fresh")
        init.assert_not_called()

    def test_fresh_login_comes_first(self):
        with mock.patch.dict("os.environ", {"USE_SDK": "false"}), \
                mock.patch.object(rt, "fetch_core_hub", return_value={"apiToken": "fresh"}) as fetch:
            self.assertEqual(rt.authenticate("/nonexistent"), "fresh")
        self.assertEqual(fetch.call_args.args[0], "/authentication/login")

    def test_saved_token_when_login_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "auth_token.json"
            token_file.write_text(json.dumps({"token": "saved"}))

            def fetch(path, **_kwargs):
                if path == "/authentication/login":
                    return {"changeRequired": True}
                return []

            with mock.patch.object(rt, "fetch_core_hub", side_effect=fetch):
                self.assertEqual(rt.authenticate(str(token_file)), "saved")

    def test_no_way_in_raises(self):
        with mock.patch.object(rt, "fetch_core_hub", side_effect=RuntimeError("401")):
            with self.assertRaises(RuntimeError):
                rt.authenticate("/nonexistent")


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

    def _restore_with(self, returncode, result, log_text):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def fake_run(cmd, cwd, env, stdout, stderr, timeout, check):
                stdout.write(log_text)
                stdout.flush()
                if result is not None:
                    Path(env["BOOTSTRAPPER_RESULT_FILE"]).write_text(json.dumps(result))
                return mock.Mock(returncode=returncode)

            with mock.patch.object(rt.subprocess, "run", side_effect=fake_run):
                return rt.restore_export(out / "x.yaml", "p", out, 60)

    def test_non_zero_exit_fails_the_restore_even_with_a_complete_result(self):
        pid, error = self._restore_with(1, {"pipelineId": "c0ffee00", "status": "complete"}, "INFO fine\n")
        self.assertEqual(pid, "c0ffee00")
        self.assertIn("exited with code 1", error)

    def test_restore_error_from_the_log_is_in_the_message(self):
        log = ("INFO start\n\x1b[43mERROR\x1b[49m - [Schema 1/1] Error running entity creation script: exit code 1\n"
               "INFO next\n")
        pid, error = self._restore_with(1, {"pipelineId": "c0ffee00", "status": "failure"}, log)
        self.assertEqual(pid, "c0ffee00")
        self.assertIn("failed", error)
        self.assertIn("Error running entity creation script: exit code 1", error)
        self.assertNotIn("\x1b", error)

    def test_log_error_is_added_when_no_pipeline_was_created(self):
        pid, error = self._restore_with(1, None, "Traceback (most recent call last):\nBootstrapperError: Failed to authenticate\n")
        self.assertIsNone(pid)
        self.assertIn("code 1", error)
        self.assertIn("Traceback", error)

    def test_result_of_an_earlier_run_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "restore-result.json").write_text(json.dumps({"pipelineId": "old", "status": "complete"}))
            with mock.patch.object(rt.subprocess, "run", return_value=mock.Mock(returncode=1)):
                pid, error = rt.restore_export(out / "x.yaml", "p", out, 60)
        self.assertIsNone(pid)
        self.assertIn("before creating a pipeline", error)

    def test_clean_run_still_passes(self):
        self.assertEqual(
            self._restore_with(0, {"pipelineId": "c0ffee00", "status": "complete"}, "INFO ok\n"),
            ("c0ffee00", ""))


if __name__ == "__main__":
    unittest.main()

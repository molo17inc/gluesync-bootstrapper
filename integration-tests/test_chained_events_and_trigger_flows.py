#!/usr/bin/env python3
"""
Unit tests for chained events, webhooks, and platform events (trigger flows)
support in the bootstrapper.

Covers:
- ChronosClient.create_trigger_flow / get_trigger_flows / update / delete
- commons._create_chained_events_for_job normalization and update_job call
- commons.create_trigger_flows_from_yaml YAML-driven creation
- commons.create_*_schedules passing chained_events through
- export_template_from_corehub.attach_schedules_from_jobs exporting chained_events
- export_template_from_corehub.fetch_trigger_flows / filter / export_to_yaml
- export_template_from_corehub.build_yaml_structure including trigger_flows
- create_all_entities.create_entities wiring trigger_flows
"""

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# ChronosClient trigger flow method tests
# ---------------------------------------------------------------------------

class ChronosClientTriggerFlowTests(unittest.TestCase):
    """Verify ChronosClient trigger flow methods call the correct endpoints."""

    def setUp(self):
        from utils.chronos_client import ChronosClient
        self.ChronosClient = ChronosClient

    def _make_client(self, request_mock):
        client = self.ChronosClient(
            base_url="http://chronos:1717",
            corehub_url="https://corehub:1717",
            token="test-token",
        )
        client._request = request_mock
        return client

    def test_create_trigger_flow_calls_post(self):
        flow_data = {
            "name": "post-deploy",
            "events": [{"task_type": "pipeline_start", "pipeline_id": "p1"}],
        }
        mock_req = mock.Mock(return_value={"id": 1, "name": "post-deploy"})
        client = self._make_client(mock_req)

        result = client.create_trigger_flow(flow_data)

        mock_req.assert_called_once_with("api/triggers/", method="POST", data=flow_data)
        self.assertEqual(result["id"], 1)

    def test_get_trigger_flows_calls_get(self):
        mock_req = mock.Mock(return_value={"items": [], "total": 0})
        client = self._make_client(mock_req)

        client.get_trigger_flows(limit=50)

        mock_req.assert_called_once_with("api/triggers/", params={"limit": 50})

    def test_get_trigger_flow_by_id(self):
        mock_req = mock.Mock(return_value={"id": 42})
        client = self._make_client(mock_req)

        client.get_trigger_flow(42)

        mock_req.assert_called_once_with("api/triggers/42")

    def test_update_trigger_flow_calls_put(self):
        update_data = {"name": "updated-flow"}
        mock_req = mock.Mock(return_value={"id": 42, "name": "updated-flow"})
        client = self._make_client(mock_req)

        client.update_trigger_flow(42, update_data)

        mock_req.assert_called_once_with("api/triggers/42", method="PUT", data=update_data)

    def test_delete_trigger_flow_calls_delete(self):
        mock_req = mock.Mock(return_value=None)
        client = self._make_client(mock_req)

        client.delete_trigger_flow(42)

        mock_req.assert_called_once_with("api/triggers/42", method="DELETE")


# ---------------------------------------------------------------------------
# _create_chained_events_for_job tests
# ---------------------------------------------------------------------------

class CreateChainedEventsForJobTests(unittest.TestCase):
    """Verify _create_chained_events_for_job normalizes and calls update_job."""

    def test_attaches_chained_events_via_update_job(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        job_result = {"id": 99, "name": "test-job"}
        chained_events = [
            {
                "task_type": "pipeline_stop",
                "execution_mode": "sync",
                "webhook_timeout_seconds": 1800,
            },
            {
                "task_type": "pipeline_start",
                "execution_mode": "async",
            },
        ]

        _create_chained_events_for_job(chronos_client, "pipe-1", job_result, chained_events)

        chronos_client.update_job.assert_called_once()
        call_args = chronos_client.update_job.call_args
        job_id = call_args[0][0]
        payload = call_args[0][1]
        self.assertEqual(job_id, 99)
        self.assertIn("chained_events", payload)
        events = payload["chained_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["task_type"], "pipeline_stop")
        self.assertEqual(events[0]["execution_mode"], "sync")
        self.assertEqual(events[0]["pipeline_id"], "pipe-1")
        self.assertEqual(events[0]["webhook_timeout_seconds"], 1800)
        self.assertEqual(events[1]["task_type"], "pipeline_start")
        self.assertEqual(events[1]["execution_mode"], "async")

    def test_skips_when_job_result_has_no_id(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        _create_chained_events_for_job(
            chronos_client, "pipe-1", {"name": "no-id"}, [{"task_type": "pipeline_start"}]
        )
        chronos_client.update_job.assert_not_called()

    def test_skips_when_chained_events_empty(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        _create_chained_events_for_job(
            chronos_client, "pipe-1", {"id": 1}, []
        )
        chronos_client.update_job.assert_not_called()

    def test_skips_when_chained_events_not_list(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        _create_chained_events_for_job(
            chronos_client, "pipe-1", {"id": 1}, "not-a-list"
        )
        chronos_client.update_job.assert_not_called()

    def test_normalizes_optional_fields(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        chained_events = [
            {
                "task_type": "entity_snapshot",
                "entity_ids": ["ent-1", "ent-2"],
                "group_ids": ["grp-1"],
                "with_snapshot": True,
                "snapshot_write_method": "INSERT",
            },
        ]

        _create_chained_events_for_job(
            chronos_client, "pipe-1", {"id": 5}, chained_events
        )

        payload = chronos_client.update_job.call_args[0][1]
        event = payload["chained_events"][0]
        self.assertEqual(event["entity_ids"], ["ent-1", "ent-2"])
        self.assertEqual(event["group_ids"], ["grp-1"])
        self.assertTrue(event["with_snapshot"])
        self.assertEqual(event["snapshot_write_method"], "INSERT")
        self.assertEqual(event["execution_mode"], "async")  # default
        self.assertEqual(event["pipeline_id"], "pipe-1")  # defaults to parent

    def test_swallows_update_job_exception(self):
        from commons import _create_chained_events_for_job

        chronos_client = mock.Mock()
        chronos_client.update_job.side_effect = Exception("API error")
        # Should not raise
        _create_chained_events_for_job(
            chronos_client, "pipe-1", {"id": 1}, [{"task_type": "pipeline_start"}]
        )


# ---------------------------------------------------------------------------
# create_trigger_flows_from_yaml tests
# ---------------------------------------------------------------------------

class CreateTriggerFlowsFromYamlTests(unittest.TestCase):
    """Verify create_trigger_flows_from_yaml creates flows correctly."""

    def test_creates_trigger_flows(self):
        import commons

        flows_config = [
            {
                "name": "post-deploy-resync",
                "description": "Post deploy flow",
                "enabled": True,
                "platform_event": "PIPELINE_CDC_STARTED",
                "events": [
                    {
                        "task_type": "pipeline_stop",
                        "execution_mode": "sync",
                    },
                    {
                        "task_type": "pipeline_start",
                        "execution_mode": "async",
                    },
                ],
            },
        ]

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_trigger_flow.return_value = {"id": 1, "name": "post-deploy-resync"}

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml(
                "tok", "pipe-1", flows_config, chronos_token="chronos-tok"
            )

        mock_client.create_trigger_flow.assert_called_once()
        call_data = mock_client.create_trigger_flow.call_args[0][0]
        self.assertEqual(call_data["name"], "post-deploy-resync")
        self.assertEqual(call_data["platform_event"], "PIPELINE_CDC_STARTED")
        self.assertEqual(len(call_data["events"]), 2)
        self.assertEqual(call_data["events"][0]["task_type"], "pipeline_stop")
        self.assertEqual(call_data["events"][0]["pipeline_id"], "pipe-1")
        self.assertEqual(call_data["events"][0]["execution_mode"], "sync")
        self.assertEqual(call_data["events"][1]["task_type"], "pipeline_start")

    def test_skips_when_scheduling_disabled(self):
        import commons

        mock_client = mock.Mock()
        with mock.patch.object(commons, "ENABLE_SCHEDULING", False), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml(
                "tok", "pipe-1", [{"name": "flow", "events": [{"task_type": "pipeline_start"}]}]
            )
        mock_client.create_trigger_flow.assert_not_called()

    def test_skips_when_chronos_unavailable(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = False

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml(
                "tok", "pipe-1", [{"name": "flow", "events": [{"task_type": "pipeline_start"}]}]
            )
        mock_client.create_trigger_flow.assert_not_called()

    def test_skips_flow_with_no_events(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml(
                "tok", "pipe-1", [{"name": "empty-flow", "events": []}]
            )
        mock_client.create_trigger_flow.assert_not_called()

    def test_skips_when_config_not_list(self):
        import commons

        mock_client = mock.Mock()
        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml(
                "tok", "pipe-1", {"not": "a list"}
            )
        mock_client.create_trigger_flow.assert_not_called()

    def test_continues_on_single_flow_error(self):
        import commons

        flows_config = [
            {"name": "flow-1", "events": [{"task_type": "pipeline_start"}]},
            {"name": "flow-2", "events": [{"task_type": "pipeline_stop"}]},
        ]

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_trigger_flow.side_effect = [
            Exception("API error"),
            {"id": 2, "name": "flow-2"},
        ]

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml("tok", "pipe-1", flows_config)

        self.assertEqual(mock_client.create_trigger_flow.call_count, 2)

    def test_omits_platform_event_when_not_set(self):
        import commons

        flows_config = [
            {
                "name": "http-only-flow",
                "events": [{"task_type": "pipeline_start"}],
            },
        ]

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_trigger_flow.return_value = {"id": 1}

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client):
            commons.create_trigger_flows_from_yaml("tok", "pipe-1", flows_config)

        call_data = mock_client.create_trigger_flow.call_args[0][0]
        self.assertNotIn("platform_event", call_data)


# ---------------------------------------------------------------------------
# create_*_schedules chained_events passthrough tests
# ---------------------------------------------------------------------------

class ScheduleChainedEventsPassthroughTests(unittest.TestCase):
    """Verify create_entity_schedules / create_group_schedules / create_pipeline_schedules
    pass chained_events through to _create_chained_events_for_job."""

    def test_create_entity_schedules_passes_chained_events(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_entity_schedule.return_value = {"id": 10, "name": "ent-sched"}

        schedules = [
            {
                "name": "entity snapshot with chain",
                "task_type": "entity_snapshot",
                "cron_expression": "0 0 * * *",
                "chained_events": [
                    {"task_type": "entity_start", "execution_mode": "async"},
                ],
            },
        ]

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client), \
             mock.patch.object(commons, "_create_chained_events_for_job") as mock_chain:
            commons.create_entity_schedules(
                "tok", "pipe-1", "ent-1", "demo.DRIVERS", schedules, chronos_token="ct"
            )

        mock_chain.assert_called_once()
        args = mock_chain.call_args[0]
        self.assertEqual(args[1], "pipe-1")
        self.assertEqual(args[2], {"id": 10, "name": "ent-sched"})
        self.assertEqual(args[3], schedules[0]["chained_events"])

    def test_create_group_schedules_passes_chained_events(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_group_schedule.return_value = {"id": 20, "name": "grp-sched"}

        schedules = [
            {
                "name": "group snapshot with chain",
                "task_type": "group_snapshot",
                "group_ids": ["grp-1"],
                "cron_expression": "0 2 * * *",
                "chained_events": [
                    {"task_type": "group_start", "execution_mode": "async"},
                ],
            },
        ]

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client), \
             mock.patch.object(commons, "_create_chained_events_for_job") as mock_chain:
            commons.create_group_schedules("tok", "pipe-1", schedules, chronos_token="ct")

        mock_chain.assert_called_once()

    def test_create_pipeline_schedules_passes_chained_events(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_pipeline_schedule.return_value = {"id": 30, "name": "pipe-sched"}

        schedules = [
            {
                "name": "pipeline redo with chain",
                "task_type": "pipeline_redo",
                "cron_expression": "0 1 * * 0",
                "chained_events": [
                    {"task_type": "pipeline_stop", "execution_mode": "sync"},
                    {"task_type": "pipeline_start", "execution_mode": "async"},
                ],
            },
        ]

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client), \
             mock.patch.object(commons, "_create_chained_events_for_job") as mock_chain:
            commons.create_pipeline_schedules("tok", "pipe-1", schedules, chronos_token="ct")

        mock_chain.assert_called_once()
        passed_events = mock_chain.call_args[0][3]
        self.assertEqual(len(passed_events), 2)

    def test_create_pipeline_schedules_no_chain_no_call(self):
        import commons

        mock_client = mock.Mock()
        mock_client.wait_for_chronos.return_value = True
        mock_client.create_pipeline_schedule.return_value = {"id": 30}

        schedules = [
            {
                "name": "simple schedule",
                "task_type": "pipeline_start",
                "cron_expression": "0 7 * * *",
            },
        ]

        with mock.patch.object(commons, "ENABLE_SCHEDULING", True), \
             mock.patch.object(commons, "ChronosClient", return_value=mock_client), \
             mock.patch.object(commons, "_create_chained_events_for_job") as mock_chain:
            commons.create_pipeline_schedules("tok", "pipe-1", schedules)

        mock_chain.assert_not_called()


# ---------------------------------------------------------------------------
# Export: attach_schedules_from_jobs with chained_events
# ---------------------------------------------------------------------------

class ExportChainedEventsTests(unittest.TestCase):
    """Verify attach_schedules_from_jobs exports chained_events."""

    def test_chained_events_exported_from_job(self):
        from export_template_from_corehub import attach_schedules_from_jobs

        jobs = [
            {
                "id": 1,
                "name": "pipeline redo",
                "task_type": "pipeline_redo",
                "cron_expression": "0 1 * * 0",
                "with_snapshot": True,
                "enabled": True,
                "snapshot_write_method": "UPSERT",
                "chained_events": [
                    {
                        "task_type": "pipeline_stop",
                        "execution_mode": "sync",
                        "pipeline_id": "pipe-1",
                        "webhook_timeout_seconds": 3600,
                    },
                    {
                        "task_type": "pipeline_start",
                        "execution_mode": "async",
                        "pipeline_id": "pipe-1",
                    },
                ],
            },
        ]
        schemas = {"demo": {"schedules": [], "group_schedules": [], "tables": {"whitelist": set(), "custom": {}}}}
        entities_by_id = {}
        group_id_to_name = {}

        attach_schedules_from_jobs(jobs, entities_by_id, schemas, group_id_to_name)

        schedules = schemas["demo"]["schedules"]
        self.assertEqual(len(schedules), 1)
        sched = schedules[0]
        self.assertIn("chained_events", sched)
        ce = sched["chained_events"]
        self.assertEqual(len(ce), 2)
        self.assertEqual(ce[0]["task_type"], "pipeline_stop")
        self.assertEqual(ce[0]["execution_mode"], "sync")
        self.assertEqual(ce[0]["webhook_timeout_seconds"], 3600)
        self.assertEqual(ce[1]["task_type"], "pipeline_start")
        self.assertEqual(ce[1]["execution_mode"], "async")

    def test_no_chained_events_key_when_absent(self):
        from export_template_from_corehub import attach_schedules_from_jobs

        jobs = [
            {
                "id": 2,
                "name": "simple schedule",
                "task_type": "pipeline_start",
                "cron_expression": "0 7 * * *",
                "enabled": True,
            },
        ]
        schemas = {"demo": {"schedules": [], "group_schedules": [], "tables": {"whitelist": set(), "custom": {}}}}

        attach_schedules_from_jobs(jobs, {}, schemas, {})

        sched = schemas["demo"]["schedules"][0]
        self.assertNotIn("chained_events", sched)

    def test_chained_events_exported_for_entity_schedule(self):
        from export_template_from_corehub import attach_schedules_from_jobs

        jobs = [
            {
                "id": 3,
                "name": "entity snapshot with chain",
                "task_type": "entity_snapshot",
                "entity_ids": ["ent-1"],
                "cron_expression": "0 0 * * *",
                "enabled": True,
                "chained_events": [
                    {"task_type": "entity_start", "execution_mode": "async"},
                ],
            },
        ]
        entities_by_id = {
            "ent-1": {"entityName": "demo.DRIVERS"},
        }
        schemas = {
            "demo": {
                "schedules": [],
                "group_schedules": [],
                "tables": {"whitelist": set(), "custom": {}},
            }
        }

        attach_schedules_from_jobs(jobs, entities_by_id, schemas, {})

        table_cfg = schemas["demo"]["tables"]["custom"]["DRIVERS"]
        self.assertIn("schedules", table_cfg)
        sched = table_cfg["schedules"][0]
        self.assertIn("chained_events", sched)
        self.assertEqual(sched["chained_events"][0]["task_type"], "entity_start")


# ---------------------------------------------------------------------------
# Export: trigger flow fetch / filter / serialize
# ---------------------------------------------------------------------------

class ExportTriggerFlowsTests(unittest.TestCase):
    """Verify trigger flow export functions."""

    def test_fetch_trigger_flows_list_shape(self):
        import export_template_from_corehub as export_mod

        flows_payload = [
            {"id": 1, "name": "flow-1", "events": [{"task_type": "pipeline_start", "pipeline_id": "p1"}]},
        ]

        with mock.patch.object(export_mod.ChronosClient, "_request", return_value=flows_payload):
            flows = export_mod.fetch_trigger_flows(token="tok")

        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]["name"], "flow-1")

    def test_fetch_trigger_flows_paginated_shape(self):
        import export_template_from_corehub as export_mod

        flows_payload = {
            "items": [
                {"id": 1, "name": "flow-1"},
                {"id": 2, "name": "flow-2"},
            ],
            "total": 2,
        }

        with mock.patch.object(export_mod.ChronosClient, "_request", return_value=flows_payload):
            flows = export_mod.fetch_trigger_flows(token="tok")

        self.assertEqual(len(flows), 2)

    def test_fetch_trigger_flows_error_returns_empty(self):
        import export_template_from_corehub as export_mod

        with mock.patch.object(export_mod.ChronosClient, "_request", side_effect=Exception("API error")):
            flows = export_mod.fetch_trigger_flows(token="tok")

        self.assertEqual(flows, [])

    def test_filter_trigger_flows_by_pipeline(self):
        from export_template_from_corehub import filter_trigger_flows_by_pipeline

        flows = [
            {
                "id": 1,
                "name": "flow-p1",
                "events": [{"task_type": "pipeline_start", "pipeline_id": "p1"}],
            },
            {
                "id": 2,
                "name": "flow-p2",
                "events": [{"task_type": "pipeline_start", "pipeline_id": "p2"}],
            },
            {
                "id": 3,
                "name": "flow-multi",
                "events": [
                    {"task_type": "pipeline_start", "pipeline_id": "p2"},
                    {"task_type": "pipeline_stop", "pipeline_id": "p1"},
                ],
            },
        ]

        matching = filter_trigger_flows_by_pipeline(flows, "p1")
        self.assertEqual(len(matching), 2)
        self.assertEqual(matching[0]["name"], "flow-p1")
        self.assertEqual(matching[1]["name"], "flow-multi")

    def test_export_trigger_flows_to_yaml(self):
        from export_template_from_corehub import export_trigger_flows_to_yaml

        flows = [
            {
                "id": 1,
                "name": "post-deploy",
                "description": "Post deploy flow",
                "enabled": True,
                "platform_event": "PIPELINE_CDC_STARTED",
                "events": [
                    {
                        "task_type": "pipeline_stop",
                        "pipeline_id": "p1",
                        "execution_mode": "sync",
                        "with_snapshot": True,
                        "snapshot_write_method": "UPSERT",
                        "webhook_timeout_seconds": 3600,
                        "entity_ids": ["ent-1"],
                        "group_ids": ["grp-1"],
                    },
                    {
                        "task_type": "pipeline_start",
                        "pipeline_id": "p1",
                        "execution_mode": "async",
                    },
                ],
            },
        ]

        exported = export_trigger_flows_to_yaml(flows, "p1")

        self.assertEqual(len(exported), 1)
        flow = exported[0]
        self.assertEqual(flow["name"], "post-deploy")
        self.assertEqual(flow["platform_event"], "PIPELINE_CDC_STARTED")
        self.assertTrue(flow["enabled"])
        self.assertEqual(len(flow["events"]), 2)
        ev0 = flow["events"][0]
        self.assertEqual(ev0["task_type"], "pipeline_stop")
        self.assertEqual(ev0["execution_mode"], "sync")
        self.assertEqual(ev0["with_snapshot"], True)
        self.assertEqual(ev0["entity_ids"], ["ent-1"])
        self.assertEqual(ev0["group_ids"], ["grp-1"])
        ev1 = flow["events"][1]
        self.assertEqual(ev1["task_type"], "pipeline_start")
        self.assertEqual(ev1["execution_mode"], "async")

    def test_export_trigger_flows_omits_platform_event_when_absent(self):
        from export_template_from_corehub import export_trigger_flows_to_yaml

        flows = [
            {
                "id": 2,
                "name": "http-only",
                "enabled": True,
                "events": [{"task_type": "pipeline_start", "pipeline_id": "p1"}],
            },
        ]

        exported = export_trigger_flows_to_yaml(flows, "p1")
        self.assertNotIn("platform_event", exported[0])

    def test_export_trigger_flows_skips_flows_without_events(self):
        from export_template_from_corehub import export_trigger_flows_to_yaml

        flows = [
            {"id": 3, "name": "empty", "enabled": True, "events": []},
            {"id": 4, "name": "no-events-key", "enabled": True},
        ]

        exported = export_trigger_flows_to_yaml(flows, "p1")
        self.assertEqual(len(exported), 0)


# ---------------------------------------------------------------------------
# build_yaml_structure with trigger_flows
# ---------------------------------------------------------------------------

class BuildYamlStructureTriggerFlowsTests(unittest.TestCase):
    """Verify build_yaml_structure includes trigger_flows in schema output."""

    def test_trigger_flows_included_in_single_schema_output(self):
        from export_template_from_corehub import build_yaml_structure

        schemas = {
            "demo": {
                "target": "public",
                "tables": {"whitelist": set(), "custom": {}},
                "trigger_flows": [
                    {
                        "name": "post-deploy",
                        "events": [{"task_type": "pipeline_start"}],
                    },
                ],
            }
        }

        yaml_data = build_yaml_structure(schemas)

        self.assertIn("demo", yaml_data)
        self.assertIn("trigger_flows", yaml_data["demo"])
        self.assertEqual(yaml_data["demo"]["trigger_flows"][0]["name"], "post-deploy")

    def test_trigger_flows_omitted_when_not_present(self):
        from export_template_from_corehub import build_yaml_structure

        schemas = {
            "demo": {
                "target": "public",
                "tables": {"whitelist": set(), "custom": {}},
            }
        }

        yaml_data = build_yaml_structure(schemas)

        self.assertNotIn("trigger_flows", yaml_data["demo"])


# ---------------------------------------------------------------------------
# create_entities wiring of trigger_flows
# ---------------------------------------------------------------------------

class CreateEntitiesTriggerFlowsWiringTests(unittest.TestCase):
    """Verify create_entities calls create_trigger_flows_from_yaml when trigger_flows present."""

    def test_trigger_flows_are_created(self):
        import create_all_entities

        TEMPLATE = """
demo:
  target: public
  schedules:
    - name: "Daily start"
      task_type: "pipeline_start"
      cron_expression: "0 7 * * *"
      enabled: true
  trigger_flows:
    - name: "post-deploy"
      events:
        - task_type: "pipeline_stop"
          execution_mode: "sync"
        - task_type: "pipeline_start"
          execution_mode: "async"
  tables:
    whitelist: [DRIVERS]
    blacklist: []
    custom:
      DRIVERS:
        keys: [ID]
"""
        yaml_config = yaml.safe_load(TEMPLATE)
        captured_trigger_flows = []

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path.endswith("/entities"):
                return [{"entity": {"entityId": "ent-1", "entityName": "demo.DRIVERS"}}]
            return {}

        source_columns = {
            "columns": [
                {"id": 1, "position": 1, "name": "ID", "dataType": "int", "isPK": True, "isNullable": False},
            ]
        }

        def _node_info(default_type="varchar", gs_type="STRING"):
            return {
                "category": "RDBMS",
                "dataTypesMatrix": [{"gluesyncDataType": gs_type, "defaultType": default_type, "supportedTypes": [default_type]}],
            }

        with mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False), \
             mock.patch.object(create_all_entities, "get_node_info", side_effect=[_node_info(), _node_info()]), \
             mock.patch.object(create_all_entities, "get_agent_tables", return_value=[]), \
             mock.patch.object(create_all_entities, "get_table_columns", return_value=source_columns), \
             mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda src, *a, **k: src or "varchar"), \
             mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=fake_fetch), \
             mock.patch.object(create_all_entities, "create_group", return_value="grp-id"), \
             mock.patch.object(create_all_entities, "handle_table_creation", return_value=None), \
             mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True), \
             mock.patch("create_all_entities.handle_udf_function_definition"), \
             mock.patch("create_all_entities.create_pipeline_schedules"), \
             mock.patch("create_all_entities.create_trigger_flows_from_yaml",
                        side_effect=lambda *a, **k: captured_trigger_flows.append(a)):
            create_all_entities.create_entities(
                token="tok",
                pipeline_id="pipe",
                source_schema="demo",
                target_schema="public",
                tables=[{"name": "DRIVERS", "schema": "demo", "id": 101}],
                source_agent_id="src",
                target_agent_id="tgt",
                source_type="SQL",
                target_type="SQL",
                yaml_config=yaml_config,
                skip_errors=False,
                chunk_size=50,
            )

        self.assertTrue(captured_trigger_flows, "create_trigger_flows_from_yaml was not called")
        _token, _pipeline_id, flows_config = captured_trigger_flows[0]
        self.assertEqual(len(flows_config), 1)
        self.assertEqual(flows_config[0]["name"], "post-deploy")
        self.assertEqual(len(flows_config[0]["events"]), 2)


# ---------------------------------------------------------------------------
# YAML template validation
# ---------------------------------------------------------------------------

class YamlTemplateValidationTests(unittest.TestCase):
    """Verify the YAML template includes chained_events and trigger_flows sections."""

    def test_template_contains_chained_events(self):
        template_path = PROJECT_ROOT / "table-list-template.yaml"
        with open(template_path, "r") as f:
            content = f.read()

        self.assertIn("chained_events", content)
        self.assertIn("execution_mode", content)
        self.assertIn("webhook_timeout_seconds", content)

    def test_template_contains_trigger_flows(self):
        template_path = PROJECT_ROOT / "table-list-template.yaml"
        with open(template_path, "r") as f:
            content = f.read()

        self.assertIn("trigger_flows", content)
        self.assertIn("platform_event", content)

    def test_template_yaml_is_valid(self):
        template_path = PROJECT_ROOT / "table-list-template.yaml"
        with open(template_path, "r") as f:
            data = yaml.safe_load(f)

        self.assertIsInstance(data, dict)
        # At least one schema should have trigger_flows
        found_trigger_flows = False
        found_chained_events = False
        for _schema_name, schema_cfg in data.items():
            if not isinstance(schema_cfg, dict):
                continue
            if "trigger_flows" in schema_cfg:
                found_trigger_flows = True
            schedules = schema_cfg.get("schedules", [])
            for sched in schedules:
                if isinstance(sched, dict) and "chained_events" in sched:
                    found_chained_events = True
            tables = schema_cfg.get("tables", {})
            custom = tables.get("custom", {}) if isinstance(tables, dict) else {}
            for table_cfg in custom.values():
                if not isinstance(table_cfg, dict):
                    continue
                for sched in table_cfg.get("schedules", []):
                    if isinstance(sched, dict) and "chained_events" in sched:
                        found_chained_events = True

        self.assertTrue(found_trigger_flows, "Template should contain trigger_flows section")
        self.assertTrue(found_chained_events, "Template should contain chained_events in schedules")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Coverage-matrix gap tests: residual I2/I3 paths + duplicated UI/CLI/backup cells."""
from __future__ import annotations

import unittest
from unittest import mock


class MappedAgentTagCasefold(unittest.TestCase):
    """I3 residual: mapped-agent fallback and dedupe must casefold tags like resume."""

    def test_find_mapped_agent_fallback_casefolds_tag(self):
        from automator_app import app as automator_app

        agents = [
            {"agentType": "SOURCE", "agentTag": "MsSql-Cdc", "agentId": "other"},
            {"agentType": "SOURCE", "agentTag": "MsSql-Cdc", "host": "ok"},
        ]
        # Id miss forces the tag/type fallback path.
        match = automator_app._find_mapped_agent(agents, "SOURCE", "mssql-cdc", "missing-id")
        self.assertIs(match, agents[0])

    def test_dedupe_agents_casefolds_tag(self):
        from automator_app import app as automator_app

        selected = [
            {"agentType": "SOURCE", "agentTag": "Oracle-Triggers"},
            {"agentType": "SOURCE", "agentTag": "oracle-triggers"},
            {"agentType": "TARGET", "agentTag": "oracle-triggers"},
        ]
        out = automator_app._dedupe_agents(selected)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["agentTag"], "Oracle-Triggers")
        self.assertEqual(out[1]["agentType"], "TARGET")


class BulkDryRunStatus(unittest.TestCase):
    """I2 residual: Bulk /api/bulk/create dry-run must not claim 'completed successfully'."""

    def test_bulk_create_dry_run_message_and_flag(self):
        from automator_app import app as automator_app
        from automator_app import state as state_mod

        req = automator_app.BulkCreateRequest(
            pipelineId="pipe-hr-1",
            sourceSchema="HR",
            targetSchema="APP",
            tableNames=["ORDERS"],
            dryRun=True,
        )
        plan = mock.Mock()
        plan.empty = True
        plan.to_dict.return_value = {}

        prev_tok = state_mod.state._token
        prev_url = state_mod.state._base_url
        with mock.patch.object(automator_app, "_require_session"), \
             mock.patch.object(state_mod.state, "preferences", return_value={
                 "enableScheduling": True, "createTables": True,
             }), \
             mock.patch.object(automator_app, "_load_entity_plan_or_400", return_value=plan), \
             mock.patch.object(automator_app, "_entity_plan_flags", return_value=(True, True, False)), \
             mock.patch.object(automator_app.entity_plan_runtime, "filter_table_names",
                               return_value=["ORDERS"]), \
             mock.patch.object(automator_app, "_bulk_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(automator_app.import_plan, "entity_pipeline", return_value=mock.Mock()), \
             mock.patch.object(automator_app, "_entity_run_import_options", return_value=None), \
             mock.patch.object(automator_app, "_bulk_yaml_for_plan", return_value={}), \
             mock.patch.object(
                 automator_app.corehub, "run_create_entities_for_tables",
                 return_value={"success": True, "dry_run": True, "successful": 1, "failed": 0, "logs": []},
             ):
            state_mod.state._token = "tok"
            state_mod.state._base_url = "https://localhost"
            try:
                msg = automator_app._bulk_create_entities(req)
            finally:
                state_mod.state._token = prev_tok
                state_mod.state._base_url = prev_url

        self.assertTrue(msg.success)
        self.assertTrue(msg.dry_run)
        self.assertIn("dry-run", (msg.message or "").lower())
        self.assertNotIn("completed successfully", (msg.message or "").lower())


class EntityRunStatusDryRun(unittest.TestCase):
    def test_entity_run_status_prefers_dry_run(self):
        from automator_app import app as automator_app

        self.assertEqual(
            automator_app._entity_run_status({"dry_run": True, "success": True, "successful": 3, "failed": 0}),
            "dry-run",
        )
        self.assertEqual(
            automator_app._entity_run_status({"success": True, "successful": 3, "failed": 0}),
            "completed",
        )


if __name__ == "__main__":
    unittest.main()

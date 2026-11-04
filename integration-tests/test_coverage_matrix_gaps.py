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


class LargeInputUploadLimit(unittest.TestCase):
    """C9: backup / GS1 upload must 413 when over GS_IMPORT_MAX_UPLOAD_MB (chunked read)."""

    def test_read_upload_enforces_max_bytes(self):
        import asyncio
        from automator_app import app as automator_app
        from fastapi import HTTPException

        class _FakeUpload:
            def __init__(self, total: int, chunk: int = 1024 * 1024):
                self._remaining = total
                self._chunk = chunk

            async def read(self, n: int = -1):
                if self._remaining <= 0:
                    return b""
                take = min(self._chunk if n < 0 else min(n, self._chunk), self._remaining)
                self._remaining -= take
                return b"x" * take

        # 3 MiB payload vs 2 MiB cap → 413 before finishing the read.
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(automator_app._read_capped_upload(
                _FakeUpload(3 * 1024 * 1024), max_bytes=2 * 1024 * 1024,
            ))
        self.assertEqual(ctx.exception.status_code, 413)
        self.assertIn("larger than", (ctx.exception.detail or "").lower())

    def test_import_upload_max_bytes_from_env(self):
        from automator_app import app as automator_app
        with mock.patch.dict("os.environ", {"GS_IMPORT_MAX_UPLOAD_MB": "3"}):
            self.assertEqual(automator_app.import_upload_max_bytes(), 3 * 1024 * 1024)


class LargeInputEntityChunking(unittest.TestCase):
    """C9: entity create walks chunks; a refused chunk falls back to one-by-one."""

    def test_send_entity_chunks_splits(self):
        import create_all_entities as cae

        sent = []

        class _Ctx:
            dry_run = False
            skip_errors = True
            pipeline_id = "p1"

        class _Run:
            ctx = _Ctx()
            errors = []
            logs = []
            entities = [{"entityName": f"T{i}", "columns": []} for i in range(5)]

        def fake_put(ctx, chunk_data):
            sent.append(list(chunk_data.get("entities") or []))

        with mock.patch.object(cae, "_put_entities", side_effect=fake_put), \
             mock.patch.object(cae, "_strip_single_table_columns"), \
             mock.patch.object(cae.import_options, "current", return_value=type("O", (), {"dry_run": False})()):
            total, ok, fail = cae._send_entity_chunks(_Run(), chunk_size=2)
        self.assertEqual(total, 5)
        self.assertEqual(ok, 5)
        self.assertEqual(fail, 0)
        self.assertEqual([len(c) for c in sent], [2, 2, 1])


class CrossDialectCredentialFallback(unittest.TestCase):
    """E2/E5 × S3 × C6: role-agnostic credential fallback preserves trustServerCertificate."""

    def test_credentials_for_duplicate_falls_back_across_agent_type(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path == "/pipelines":
                return [{"pipelineId": "kit"}]
            if path == "/pipelines/kit/config":
                return {"agents": [
                    {"agentTag": "mssql-cdc", "agentType": "SOURCE", "agentId": "ms",
                     "hostCredentials": {"host": "ms-host", "port": 1433, "username": "sa"}},
                    {"agentTag": "mysql-cdc", "agentType": "TARGET", "agentId": "my",
                     "hostCredentials": {
                         "host": "my-host", "port": 3306, "username": "u",
                         "trustServerCertificate": True,
                     }},
                ]}
            if path.endswith("/config/credentials"):
                return {}
            return {}

        tmpl = {"agentTag": "mssql-cdc", "agentType": "SOURCE", "agentId": "ms",
                "hostCredentials": {"host": "ms-host", "port": 1433, "username": "sa"}}
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            host, _ = corehub._credentials_for_duplicate_agent(
                token="t", pipeline_id="kit", agent_template=tmpl, new_tag="mysql-cdc",
            )
        self.assertEqual(host.get("host"), "my-host")
        self.assertIs(host.get("trustServerCertificate"), True)


if __name__ == "__main__":
    unittest.main()

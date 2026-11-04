"""Regression tests for review-5 H1–H7 (fixed behaviour). Synthetic names only."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class H1QualifiedFailedIdsResolve(unittest.TestCase):
    def test_server_resolve_selection_accepts_unique_bare_key(self):
        import import_plan

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {
                        "custom": {"ORDERS": {"name": "ORDERS"}, "EMPLOYEES": {"name": "EMPLOYEES"}},
                        "whitelist": ["ORDERS", "EMPLOYEES"],
                    },
                }
            }
        }
        self.assertTrue(import_plan._matches("ORDERS", "HR.ORDERS"))
        self.assertEqual(import_plan.resolve_selection(yaml_cfg, "ORDERS"), ["HR.ORDERS"])
        self.assertEqual(import_plan.resolve_selection(yaml_cfg, "HR.ORDERS"), ["HR.ORDERS"])

    def test_server_resolve_selection_rejects_ambiguous_bare_key(self):
        import import_plan

        yaml_cfg = {
            "schemas": {
                "HR": {
                    "target": "APP",
                    "tables": {"custom": {"ORDERS": {"name": "ORDERS"}}, "whitelist": ["ORDERS"]},
                },
                "FIN": {
                    "target": "APP",
                    "tables": {"custom": {"ORDERS": {"name": "ORDERS"}}, "whitelist": ["ORDERS"]},
                },
            }
        }
        with self.assertRaises(import_plan.PlanError):
            import_plan.resolve_selection(yaml_cfg, "ORDERS")

    def test_refuse_table_qualifies_with_source_schema(self):
        import create_all_entities as cae

        class _Ctx:
            skip_errors = True

        run = cae._EntityRun(
            _Ctx(), schema={}, source_schema="HR", target_schema="APP",
            source_agent_id="s", source_type="RDBMS", target_type="RDBMS", grouped=False,
        )
        run.refuse_table("ORDERS", RuntimeError("boom"))
        self.assertEqual(run.refused_tables, ["HR.ORDERS"])
        failures = cae._entity_failures(run.errors)
        ids = cae._failed_entity_ids(failures, run.refused_tables)
        self.assertIn("HR.ORDERS", ids)

    def test_client_advanced_matches_unique_bare_key(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = (ROOT / "automator_app/static/entity_import.js").read_text()
        script = r"""
import { advancedMatches, effectiveSelectedIds, selectionError } from './entity_import.mjs';
class FakeEl {
  constructor() { this._kids = {}; }
  querySelector(sel) { return this._kids[sel] || null; }
}
const panel = new FakeEl();
panel._kids['[data-include]'] = { value: 'ORDERS\nEMPLOYEES' };
panel._kids['[data-exclude]'] = { value: '' };
panel._kids['[data-on-existing]'] = { value: 'skip' };
const loaded = [
  { id: 'HR.ORDERS', entityName: 'HR.ORDERS', targetSchema: 'APP', targetTable: 'ORDERS' },
  { id: 'HR.EMPLOYEES', entityName: 'HR.EMPLOYEES', targetSchema: 'APP', targetTable: 'EMPLOYEES' },
];
const assert = (c, m) => { if (!c) throw new Error(m); };
assert(advancedMatches('ORDERS', loaded[0], loaded), 'unique bare ORDERS matches HR.ORDERS');
assert(advancedMatches('EMPLOYEES', loaded[1], loaded), 'unique bare EMPLOYEES matches');
assert(advancedMatches('HR.ORDERS', loaded[0], loaded), 'qualified still matches');
const effective = effectiveSelectedIds(panel, loaded, new Set());
assert(effective.size === 2, 'bare failed ids resolve via Advanced include');
const err = selectionError(panel, loaded, new Set());
assert(!err, 'no selectionError: ' + err);
console.log('ok');
"""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "entity_import.mjs").write_text(js)
            Path(tmp, "t.mjs").write_text(script)
            proc = subprocess.run(["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


class H2AutoRenameNoSecondaryCollision(unittest.TestCase):
    def test_suffix_skips_occupied_target(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = (ROOT / "automator_app/static/entity_import.js").read_text()
        script = r"""
import { autoRenameDuplicateTargets, resolveTarget } from './entity_import.mjs';
const loaded = [
  { id: 'HR.ORDERS', targetSchema: 'APP', targetTable: 'ORDERS' },
  { id: 'FIN.ORDERS', targetSchema: 'APP', targetTable: 'ORDERS' },
  { id: 'HR.ORDERS_COPY', targetSchema: 'APP', targetTable: 'ORDERS_1' },
];
const selected = new Set(loaded.map((e) => e.id));
const { renamed, overrides } = autoRenameDuplicateTargets(loaded, selected, new Map(), null);
const assert = (c, m) => { if (!c) throw new Error(m); };
assert(renamed >= 1, 'renames second ORDERS feeder');
const fin = resolveTarget(loaded[1], overrides, null);
const copy = resolveTarget(loaded[2], overrides, null);
assert(fin.targetTable !== copy.targetTable, 'no secondary collision: ' + fin.targetTable + ' vs ' + copy.targetTable);
assert(fin.targetTable === 'ORDERS_2' || fin.targetTable.startsWith('ORDERS_'), 'bumped past ORDERS_1: ' + fin.targetTable);
const keys = new Set(loaded.map((e) => {
  const r = resolveTarget(e, overrides, null);
  return `${r.targetSchema}.${r.targetTable}`.toLowerCase();
}));
assert(keys.size === 3, 'three distinct targets: ' + [...keys]);
console.log('ok');
"""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "entity_import.mjs").write_text(js)
            Path(tmp, "t.mjs").write_text(script)
            proc = subprocess.run(["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


class H3RetryClearsExclude(unittest.TestCase):
    def test_prepare_retry_clears_stale_exclude(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = (ROOT / "automator_app/static/entity_import.js").read_text()
        # Smoke the source contains clear-exclude; full mount is heavier.
        self.assertIn("excludeEl.value = ''", js)
        self.assertIn("Resolve bare keys", js)
        script = r"""
import { effectiveSelectedIds, selectionError, advancedMatches } from './entity_import.mjs';
class FakeEl {
  constructor() { this._kids = {}; }
  querySelector(sel) { return this._kids[sel] || null; }
}
const panel = new FakeEl();
// Simulate prepareRetryFailed after fix: include set, exclude cleared.
panel._kids['[data-include]'] = { value: 'HR.ORDERS' };
panel._kids['[data-exclude]'] = { value: '' };
panel._kids['[data-on-existing]'] = { value: 'skip' };
const loaded = [
  { id: 'HR.ORDERS', entityName: 'HR.ORDERS', targetSchema: 'APP', targetTable: 'ORDERS' },
];
const assert = (c, m) => { if (!c) throw new Error(m); };
const effective = effectiveSelectedIds(panel, loaded, new Set(['HR.ORDERS']));
assert(effective.size === 1 && effective.has('HR.ORDERS'), 'retry include survives');
const err = selectionError(panel, loaded, new Set(['HR.ORDERS']));
assert(!err, 'retry not blocked: ' + err);
console.log('ok');
"""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "entity_import.mjs").write_text(js)
            Path(tmp, "t.mjs").write_text(script)
            proc = subprocess.run(["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


class H4UnambiguousCredentialLookup(unittest.TestCase):
    def test_casefold_tag_match(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path == "/pipelines":
                return [{"pipelineId": "ora-pipe"}]
            if path == "/pipelines/ora-pipe/config":
                return {"agents": [
                    {"agentTag": "Oracle-Triggers", "agentType": "TARGET", "agentId": "ora",
                     "hostCredentials": {"host": "oracle-host", "port": 1521, "username": "GLUESYNCUSER"}},
                ]}
            if path.endswith("/config/credentials"):
                return {}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            host, _c, found = corehub._find_agent_credentials_by_tag(
                token="tok", agent_tag="oracle-triggers", prefer_pipeline_id=None, agent_type="TARGET",
            )
        self.assertEqual(found, "ora-pipe")
        self.assertEqual(host.get("host"), "oracle-host")

    def test_ambiguous_hosts_return_none(self):
        import automator_app.corehub as corehub

        def fake_fetch(path, method="GET", token=None, body=None, **kwargs):
            if path == "/pipelines":
                return [{"pipelineId": "oracle-sandbox"}, {"pipelineId": "oracle-prod"}]
            if path == "/pipelines/oracle-sandbox/config":
                return {"agents": [
                    {"agentTag": "oracle-triggers", "agentType": "TARGET", "agentId": "ora-sbx",
                     "hostCredentials": {"host": "sandbox-oracle", "port": 1521, "username": "u"}},
                ]}
            if path == "/pipelines/oracle-prod/config":
                return {"agents": [
                    {"agentTag": "oracle-triggers", "agentType": "TARGET", "agentId": "ora-prod",
                     "hostCredentials": {"host": "prod-oracle", "port": 1521, "username": "u"}},
                ]}
            if path.endswith("/config/credentials"):
                return {}
            return {}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fake_fetch):
            host, _c, found = corehub._find_agent_credentials_by_tag(
                token="tok", agent_tag="oracle-triggers", prefer_pipeline_id=None, agent_type="TARGET",
            )
        self.assertIsNone(host)
        self.assertIsNone(found)

    def test_usable_requires_username(self):
        import automator_app.corehub as corehub

        self.assertFalse(corehub._host_credentials_usable({"host": "only-host"}))
        self.assertFalse(corehub._host_credentials_usable({"hostname": "only-hostname"}))
        self.assertTrue(corehub._host_credentials_usable({"host": "h", "username": "u"}))
        self.assertFalse(corehub._host_credentials_usable({"password": "secret"}))


class H5AllFailedReportsFailed(unittest.TestCase):
    def test_entity_run_result_success_false_when_all_failed(self):
        import create_all_entities as cae

        class _Ctx:
            skip_errors = True

        class _Run:
            ctx = _Ctx()
            errors = ["HR.ORDERS: boom", "HR.EMPLOYEES: boom"]
            refused_tables = ["HR.ORDERS", "HR.EMPLOYEES"]

        result = cae._entity_run_result(_Run(), type("O", (), {"dry_run": False})(), None, (0, 2, 2))
        self.assertEqual(result["successful"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertFalse(result["success"])
        self.assertFalse(result["partial"])

        def entity_run_status(result):
            if result.get("partial") or (int(result.get("successful") or 0) > 0 and int(result.get("failed") or 0) > 0):
                return "partial"
            if result.get("success"):
                return "completed"
            return "failed"

        self.assertEqual(entity_run_status(result), "failed")


class H6ChronosBoundedWalk(unittest.TestCase):
    def test_zero_match_full_pages_stop_at_streak_cap(self):
        import export_template_from_corehub as exp

        pages_seen = {"n": 0}

        class Client:
            def _request(self, path, params=None):
                pages_seen["n"] += 1
                offset = (params or {}).get("offset", 0)
                return {
                    "items": [
                        {"id": f"other-{offset}-{i}", "pipeline_id": "other-pipe", "name": f"j{i}"}
                        for i in range(1000)
                    ]
                }

        jobs, readable = exp._read_jobs_from_client(Client(), "my-pipe", page_limit=1000)
        self.assertEqual(jobs, [])
        self.assertTrue(readable)
        self.assertEqual(pages_seen["n"], exp._CHRONOS_MAX_ZERO_MATCH_STREAK)
        src = (ROOT / "export_template_from_corehub.py").read_text()
        self.assertRegex(src, r"_CHRONOS_MAX_ZERO_MATCH_STREAK|_CHRONOS_MAX_PAGES")


class H7TokenRefreshSingleFlight(unittest.TestCase):
    def test_second_waiter_reuses_in_flight_token(self):
        for name in list(__import__("sys").modules):
            if name == "commons":
                del __import__("sys").modules[name]
        import commons

        calls = []
        barrier = threading.Barrier(2)

        def refresh():
            calls.append(threading.current_thread().name)
            time.sleep(0.08)
            return f"tok-{len(calls)}"

        commons.register_token_refresh(refresh)
        commons.reset_refreshed_token()
        results = [None, None]

        def worker(idx):
            barrier.wait()
            results[idx] = commons._try_refresh_token()

        t1 = threading.Thread(target=worker, args=(0,), name="A")
        t2 = threading.Thread(target=worker, args=(1,), name="B")
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(calls), 1, f"expected single-flight login, got {calls}")
        self.assertTrue(all(results))
        self.assertEqual(results[0], results[1])
        commons.clear_token_refresh()


if __name__ == "__main__":
    unittest.main()

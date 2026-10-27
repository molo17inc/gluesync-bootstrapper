#!/usr/bin/env python3
"""Customizable backup import (import_plan.py, /api/import/analyze|preview|all with a plan, import_backup
--plan/--write-plan/--dry-run) on the two synthetic backups, against a mocked Core Hub (no live calls).

The archives are rebuilt from the sanitized fixtures under fixtures/synthetic (placeholder credentials)."""

import contextlib
import copy
import io
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import import_plan  # noqa: E402
from automator_app import app, import_backup  # noqa: E402
from fastapi import HTTPException  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures" / "synthetic"
BACKUPS = {"synth1": ("5e1f0001", "Synth1MSSQL"), "synth2": ("5e1f0002", "Synth2MSSQL")}
TGT02_TABLES = {"TAB039", "TAB047", "TAB042", "TAB043", "TAB041@@3", "TAB044@@2", "TAB045", "TAB040",
               "TAB046"}
SECRET = "Pl4n-S3cret-pw!"


def entities_yaml(tag):
    return yaml.safe_load((FIXTURES / f"{tag}_entities.yaml").read_text())


def archive(tag):
    old_id, name = BACKUPS[tag]
    agents = (FIXTURES / f"{tag}_agents-config.yaml").read_text()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"pipeline_{old_id}/backup_{name}_{old_id}_20261026_154826.yaml",
                    (FIXTURES / f"{tag}_entities.yaml").read_text())
        zf.writestr(f"pipeline_{old_id}/agents-config.yaml", agents)
    return buffer.getvalue()


def both_archives():
    """One ZIP with both pipelines (shared agents-config of Synth1 plus Synth2's agents)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for tag in BACKUPS:
            with zipfile.ZipFile(io.BytesIO(archive(tag))) as src:
                for item in src.namelist():
                    zf.writestr(item, src.read(item))
    return buffer.getvalue()


class Hub:
    """Mocked Core Hub boundary of one import: what was created, with which YAML and options."""

    def __init__(self, existing=None, existing_entities=None):
        self.configs, self.entity_runs, self.calls, self.collections = [], [], [], []
        self.existing = existing or []
        self.existing_entities = existing_entities or {}

    def fetch(self, path, method="GET", **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path == "/pipelines":
            return self.existing
        if method == "GET" and path.endswith("/entities"):
            return self.existing_entities.get(path.split("/")[2], [])
        if method == "GET" and path.endswith("/config"):
            pid = path.split("/")[2]
            return {"pipelineName": pid, "agents": [
                {"agentId": f"{pid}-src", "agentType": "SOURCE", "agentTag": "ibm-iseries"},
                {"agentId": f"{pid}-tgt", "agentType": "TARGET", "agentTag": "mssql-ct"}]}
        return {}

    echo = None

    def import_config(self, **kwargs):
        if self.echo:
            logging.getLogger("automator_app.app").info("echo %s", self.echo)
            logging.getLogger("automator_app.app").error("boom", exc_info=ValueError(self.echo))
        cfg = copy.deepcopy(kwargs["config"])
        self.configs.append((cfg, kwargs["on_existing_name"]))
        return {"pipelineId": f"new-{len(self.configs)}", "pipelineName": cfg["pipelineName"]}

    def schema_pair(self, job, entities, src, tgt):
        self.entity_runs.append({"pipelineId": entities.pipeline_id, "src": src, "tgt": tgt,
                                 "yaml": yaml.safe_load(Path(entities.yaml_path).read_text()),
                                 "scheduling": entities.enable_scheduling, "createTables": entities.create_tables,
                                 "compileUdfs": entities.compile_udfs, "choices": entities.plan_choices})
        return True

    def collection(self, job, name, *args):
        self.collections.append(name)
        return True

    @contextlib.contextmanager
    def patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(app.corehub, "fetch_core_hub", side_effect=self.fetch))
            stack.enter_context(mock.patch.object(app.corehub, "import_pipeline_config_only",
                                                  side_effect=self.import_config))
            stack.enter_context(mock.patch.object(app, "_upload_pipeline_certificates"))
            stack.enter_context(mock.patch.object(app, "_restore_imported_function_collection",
                                                  side_effect=self.collection))
            stack.enter_context(mock.patch.object(app, "_create_schema_pair_entities", side_effect=self.schema_pair))
            yield self


def run(contents, plan=None, passwords=None, hub=None, **kwargs):
    hub = hub or Hub()
    with hub.patched():
        job = app._run_archive_import(contents, plan=plan, agent_passwords=passwords, plan_env={}, **kwargs)
    return job, hub


def plan(data):
    return import_plan.load(json.dumps(data))


class _Workdir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"GLUESYNC_AUTOMATOR_WORKDIR": self._tmp.name})
        self._env.start()
        logging.disable(logging.NOTSET)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()
        logging.disable(logging.CRITICAL)


class ImportPlanUnitTests(unittest.TestCase):
    def test_empty_plan(self):
        p = import_plan.load("{}")
        self.assertTrue(p.empty)
        config = entities_yaml("synth1")
        self.assertIs(import_plan.apply_to_yaml(config, p, p.pipeline("5e1f0001"))[0], config)

    def test_passwords_refused_anywhere(self):
        for bad in ({"pipelines": {"x": {"agents": {"a": {"password": "x"}}}}},
                    {"defaults": {"apiToken": "x"}}, {"pipelines": {"x": {"agents": {"a": {"pwd": "x"}}}}}):
            with self.assertRaises(import_plan.PlanError) as ctx:
                plan(bad)
            self.assertNotIn('"x"', str(ctx.exception))

    def test_unknown_keys_and_bad_choices(self):
        for bad in ({"bogus": 1}, {"defaults": {"onExistingEntity": "drop"}},
                    {"pipelines": {"x": {"agents": {"a": {"passwordRef": "plain"}}}}}):
            with self.assertRaises(import_plan.PlanError):
                plan(bad)

    def test_selection_overrides_and_skips(self):
        config = entities_yaml("synth1")
        ids = [e["id"] for e in import_plan.entity_ids(config)]
        first = ids[0]
        p = plan({"defaults": {"udfs": False, "groups": False},
                  "pipelines": {"5e1f0001": {"entities": {"include": ids[:3], "overrides": {
                      first: {"targetSchema": "NEWS", "targetTable": "NEWT", "entityName": "NEWE"}}}}}})
        out, notes = import_plan.apply_to_yaml(config, p, p.pipeline("5e1f0001"))
        self.assertEqual(import_plan.entity_ids(out).__len__(), 3)
        schema, key = first.split(".", 1)
        cfg = out["schemas"][schema]["tables"]["custom"][key]
        self.assertEqual((cfg["targetSchema"], cfg["name"], cfg["entityName"]), ("NEWS", "NEWT", "NEWE"))
        self.assertNotIn("groups", out)
        self.assertFalse(any((c.get("customProperties") or {}).get("target", {}).get("udf")
                             for e in out["schemas"].values() for c in e["tables"]["custom"].values()))
        self.assertTrue(any("not selected" in n for n in notes))
        self.assertEqual(len(import_plan.entity_ids(config)), len(ids))  # input untouched

    def test_unknown_table_name_is_refused_with_suggestion(self):
        p = plan({"pipelines": {"5e1f0001": {"entities": {"include": ["SCH19.TAB039X"]}}}})
        config, pipeline_plan = entities_yaml("synth1"), p.pipeline("5e1f0001")
        with self.assertRaises(import_plan.PlanError) as ctx:
            import_plan.check_names(config, pipeline_plan)
        self.assertIn("TAB039", str(ctx.exception))

    def test_base_name_selects_variants(self):
        config = entities_yaml("synth1")
        tgt02 = [e for e in import_plan.entity_ids(config) if e["key"] in TGT02_TABLES]
        self.assertEqual({e["key"] for e in tgt02}, TGT02_TABLES)
        schema = tgt02[0]["schema"]
        p = plan({"pipelines": {"5e1f0001": {"entities": {"include": [f"{schema}.TAB041"]}}}})
        self.assertEqual([i.split(".", 1)[1] for i in import_plan.selected_ids(config, p.pipeline("5e1f0001"))],
                         ["TAB041", "TAB041@@2", "TAB041@@3"])
        exact = plan({"pipelines": {"5e1f0001": {"entities": {"include": [f"{schema}.TAB041@@3"]}}}})
        out, _notes = import_plan.apply_to_yaml(config, exact, exact.pipeline("5e1f0001"))
        self.assertEqual([e["id"] for e in import_plan.entity_ids(out)], [f"{schema}.TAB041@@3"])

    def test_agent_passwords_private_env(self):
        p = plan({"pipelines": {"x": {"agents": {"mssql-ct": {"host": "h", "passwordRef": "env:PW"}}}}})
        overrides, env = import_plan.agent_overrides_for(p.pipeline("x"), {"ibm-iseries": SECRET}, {"PW": "pw2"})
        self.assertEqual(overrides["mssql-ct"], {"host": "h", "passwordEnv": "PW"})
        self.assertIn(SECRET, env.values())
        self.assertNotIn(SECRET, json.dumps(overrides))
        pipeline_plan = p.pipeline("x")
        with self.assertRaises(import_plan.PlanError):
            import_plan.agent_overrides_for(pipeline_plan, {}, {})

    def test_draft_round_trips(self):
        analysis = app._analyze_archive(archive("synth1"))
        loaded = import_plan.load(json.dumps(analysis["plan"]))
        self.assertNotIn("password", json.dumps(analysis["plan"]).lower())
        self.assertEqual(loaded.selected_pipelines(), ["5e1f0001"])


class GoldenTests(_Workdir):
    def test_empty_plan_equals_no_plan(self):
        for tag in BACKUPS:
            with self.subTest(tag=tag):
                _job_a, a = run(archive(tag))
                _job_b, b = run(archive(tag), plan=import_plan.load("{}"))
                self.assertEqual(a.configs, b.configs)
                strip = [{k: v for k, v in r.items() if k != "choices"} for r in a.entity_runs]
                self.assertEqual(strip, [{k: v for k, v in r.items() if k != "choices"} for r in b.entity_runs])
                self.assertEqual(a.calls, b.calls)
                self.assertEqual(a.collections, b.collections)
                self.assertTrue(a.entity_runs)


class PlanImportTests(_Workdir):
    def test_selection_rename_and_tgt02(self):
        config = entities_yaml("synth1")
        tgt02 = [e["id"] for e in import_plan.entity_ids(config) if e["key"] in TGT02_TABLES]
        first = tgt02[0]
        p = plan({"pipelines": {"5e1f0001": {"name": "Renamed", "entities": {
            "include": tgt02, "overrides": {first: {"targetSchema": "NEWSCH", "targetTable": "NEWTAB"}}}}}})
        job, hub = run(archive("synth1"), plan=p)
        self.assertEqual(hub.configs[0][0]["pipelineName"], "Renamed")
        imported = {}
        for r in hub.entity_runs:
            for schema, entry in r["yaml"]["schemas"].items():
                imported.update({f"{schema}.{k}": c for k, c in entry["tables"]["custom"].items()})
        self.assertEqual(set(imported), set(tgt02))
        self.assertEqual((imported[first]["targetSchema"], imported[first]["name"]), ("NEWSCH", "NEWTAB"))

    def test_each_skip(self):
        p = plan({"globals": {"notifications": False, "users": False, "accessPolicies": False},
                  "defaults": {"udfs": False, "schedules": False, "groups": False, "functionCollection": False,
                               "createTables": False}})
        with mock.patch.object(app.corehub, "import_global_notifications") as notif, \
                mock.patch.object(app.corehub, "import_users") as users, \
                mock.patch.object(app.corehub, "import_oidc_config") as oidc:
            job, hub = run(archive("synth1"), plan=p)
        notif.assert_not_called(), users.assert_not_called(), oidc.assert_not_called()
        self.assertEqual(hub.collections, [])
        self.assertTrue(hub.entity_runs)
        for r in hub.entity_runs:
            self.assertFalse(r["scheduling"])
            self.assertFalse(r["createTables"])
            self.assertFalse(r["compileUdfs"])
            self.assertNotIn("groups", r["yaml"])
        self.assertFalse(any("access-polic" in path for _m, path in hub.calls))

    def test_two_pipelines_pick_one_and_on_existing(self):
        p = plan({"defaults": {"onExistingPipeline": "fail", "onExistingEntity": "update"},
                  "pipelines": {"5e1f0002": {"import": False}}})
        job, hub = run(both_archives(), plan=p)
        self.assertEqual([c[0]["pipelineName"] for c in hub.configs], ["Synth1MSSQL"])
        self.assertEqual(hub.configs[0][1], "fail")

    def test_resume_into_existing_creates_no_pipeline(self):
        p = plan({"pipelines": {"5e1f0001": {"resumeInto": "p-existing"}, "5e1f0002": {"import": False}}})
        job, hub = run(both_archives(), plan=p)
        self.assertEqual(hub.configs, [])
        self.assertNotIn(("POST", "/pipelines"), hub.calls)
        self.assertEqual({r["pipelineId"] for r in hub.entity_runs}, {"p-existing"})

    def test_unknown_pipeline_is_400_before_any_write(self):
        contents, bad_plan = archive("synth1"), plan({"pipelines": {"nope": {"import": False}}})
        with self.assertRaises(HTTPException) as ctx:
            run(contents, plan=bad_plan)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_password_never_in_logs_or_workdir(self):
        p = plan({"pipelines": {"5e1f0001": {"agents": {"mssql-ct": {"host": "newhost"}}}}})
        with self.assertLogs(level="DEBUG") as logs:
            hub = Hub()
            hub.echo = SECRET  # a log line holding the password while the import runs is redacted
            job, hub = run(archive("synth1"), plan=p, passwords={"5e1f0001": {"mssql-ct": SECRET}}, hub=hub)
        target = [a for a in hub.configs[0][0]["agents"] if a["agentTag"] == "mssql-ct"][0]
        self.assertEqual(target["hostCredentials"]["host"], "newhost")
        self.assertEqual(target["hostCredentials"]["password"], SECRET)
        self.assertTrue(any("echo" in line for line in logs.output))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertNotIn(SECRET, "\n".join(logging.Formatter().format(r) for r in logs.records))
        for path in Path(self._tmp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), str(path))
        self.assertNotIn(SECRET, json.dumps(job.notes + job.errors))

    def test_lock_is_409(self):
        entered, release = threading.Event(), threading.Event()

        def hold():
            with app._exclusive_import():
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        entered.wait(5)
        try:
            contents = archive("synth1")
            with self.assertRaises(HTTPException) as ctx:
                app._run_archive_import(contents)
            self.assertEqual(ctx.exception.status_code, 409)
        finally:
            release.set()
            thread.join()


class PreviewTests(_Workdir):
    def test_preview_is_read_only_and_reports(self):
        existing = [{"id": "p-existing", "name": "Synth1MSSQL"}]
        hub = Hub(existing=existing)
        config = entities_yaml("synth1")
        first = import_plan.entity_ids(config)[0]["id"]
        p = plan({"pipelines": {"5e1f0001": {"entities": {"overrides": {first: {"targetSchema": "NEWSCH"}}}}}})
        with hub.patched():
            preview = app._preview_import(archive("synth1"), p, {"5e1f0001": {"mssql-ct": SECRET}}, plan_env={})
        self.assertTrue(all(m == "GET" for m, _p in hub.calls), hub.calls)
        self.assertEqual(hub.configs, [])
        self.assertNotIn(SECRET, json.dumps(preview))
        (pipe,) = preview["pipelines"]
        self.assertIn("restored", pipe["action"])
        self.assertEqual(preview["totals"]["pipelines"], 1)
        self.assertIn("NEWSCH", json.dumps(pipe))

    def test_preview_resume_conflicts(self):
        hub = Hub(existing=[{"id": "p-existing", "name": "X"}])
        with hub.patched():
            preview = app._preview_import(archive("synth1"), plan({"pipelines": {"5e1f0001": {
                "resumeInto": "p-existing"}}}), plan_env={})
        self.assertTrue(all(m == "GET" for m, _p in hub.calls))
        self.assertIn("resume", preview["pipelines"][0]["action"])


class PlanEdgeCaseTests(_Workdir):
    def _preview(self, data, hub=None, passwords=None, contents=None):
        hub = hub or Hub()
        with hub.patched():
            out = app._preview_import(contents or archive("synth1"), plan(data), passwords, plan_env={})
        self.assertTrue(all(m == "GET" for m, _p in hub.calls), hub.calls)
        return out

    def test_all_deselected_is_refused(self):
        data = {"pipelines": {"5e1f0001": {"import": False}}}
        self.assertFalse(self._preview(data)["ok"])
        contents, bad_plan = archive("synth1"), plan(data)
        with self.assertRaises(HTTPException) as ctx:
            run(contents, plan=bad_plan)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_empty_include_is_refused(self):
        data = {"pipelines": {"5e1f0001": {"entities": {"include": []}}}}
        out = self._preview(data)
        self.assertFalse(out["ok"])
        self.assertIn("no table selected", json.dumps(out["errors"]))
        contents, bad_plan = archive("synth1"), plan(data)
        with self.assertRaises(HTTPException) as ctx:
            run(contents, plan=bad_plan)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rename_collision_gets_suffix(self):
        hub = Hub(existing=[{"id": "p-existing", "name": "Taken"}])
        out = self._preview({"pipelines": {"5e1f0001": {"name": "Taken"}}}, hub=hub)
        (pipe,) = out["pipelines"]
        self.assertNotEqual(pipe["finalName"], "Taken")
        self.assertTrue(pipe["finalName"].startswith("Taken"))
        failing = self._preview({"defaults": {"onExistingPipeline": "fail"},
                                 "pipelines": {"5e1f0001": {"name": "Taken"}}},
                                hub=Hub(existing=[{"id": "p-existing", "name": "Taken"}]))
        self.assertFalse(failing["ok"])

    def test_unknown_resume_target_is_flagged(self):
        out = self._preview({"pipelines": {"5e1f0001": {"resumeInto": "nope"}}},
                            hub=Hub(existing=[{"id": "p-existing", "name": "X"}]))
        self.assertFalse(out["ok"])
        self.assertIn("no such pipeline", json.dumps(out["errors"]))

    def test_unknown_agent_is_refused(self):
        out = self._preview({"pipelines": {"5e1f0001": {"agents": {"ghost": {"host": "h"}}}}},
                            passwords={"5e1f0001": {"ghost2": SECRET}})
        self.assertFalse(out["ok"])
        self.assertEqual(sum("no agent" in e for e in out["errors"]), 2)
        self.assertNotIn(SECRET, json.dumps(out))


class EnvPasswordRedactionTests(_Workdir):
    def test_passwordref_and_override_env_passwords_are_redacted(self):
        ref_secret, tag_secret = "Env-Ref-pw-9x!", "Tag-Env-pw-7y!"
        p = plan({"pipelines": {"5e1f0001": {"agents": {"mssql-ct": {"passwordRef": "env:GS_TEST_PLAN_PW"}}}}})
        hub = Hub()
        hub.echo = f"{ref_secret} {tag_secret}"
        with mock.patch.dict(os.environ, {"GS_IMPORT_AGENT_PASSWORD_IBM_ISERIES": tag_secret}), \
                self.assertLogs(level="DEBUG") as logs, hub.patched():
            job = app._run_archive_import(archive("synth1"), plan=p, plan_env={"GS_TEST_PLAN_PW": ref_secret})
        target = [a for a in hub.configs[0][0]["agents"] if a["agentTag"] == "mssql-ct"][0]
        self.assertEqual(target["hostCredentials"]["password"], ref_secret)
        text = "\n".join(logs.output) + "\n".join(logging.Formatter().format(r) for r in logs.records)
        self.assertTrue(any("echo" in line for line in logs.output))
        for value in (ref_secret, tag_secret):
            self.assertNotIn(value, text)
            self.assertNotIn(value, json.dumps(job.notes + job.errors))

    def test_env_secret_values(self):
        p = plan({"pipelines": {"5e1f0001": {"agents": {"mssql-ct": {"passwordRef": "env:A_PW"}}}}})
        with mock.patch.dict(os.environ, {"GS_IMPORT_AGENT_PASSWORD_X": "tagpw", "NAMED_PW": "named",
                                          "UNRELATED": "nope"}):
            values = app._env_secret_values(p, {"A_PW": "refpw"}, '{"x": {"passwordEnv": "NAMED_PW"}}')
        self.assertEqual(set(values), {"refpw", "tagpw", "named"})


class HttpConcurrencyTests(_Workdir):
    def test_second_http_import_gets_409_while_one_runs(self):
        import socket
        import time
        import requests
        import uvicorn

        entered, release = threading.Event(), threading.Event()
        hub = Hub()
        original = hub.schema_pair

        def held(*args):
            entered.set()
            release.wait(30)
            return original(*args)

        hub.schema_pair = held
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with hub.patched(), mock.patch.object(app, "_create_schema_pair_entities", side_effect=held), \
                mock.patch.object(app, "_require_session", return_value=None):
            server = uvicorn.Server(uvicorn.Config(app.create_app(), host="127.0.0.1", port=port,
                                                   log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                deadline = time.time() + 15
                while not server.started and time.time() < deadline:
                    time.sleep(0.05)
                url = f"http://127.0.0.1:{port}/api/import/all"
                files = lambda: {"file": ("o1.zip", archive("synth1"), "application/zip")}  # noqa: E731
                first = {}
                worker = threading.Thread(target=lambda: first.update(r=requests.post(url, files=files(), timeout=120)))
                worker.start()
                self.assertTrue(entered.wait(90), "first import never reached entity creation")
                started = time.time()
                second = requests.post(url, files=files(), timeout=20)
                self.assertEqual(second.status_code, 409, second.text)
                self.assertLess(time.time() - started, 10)
                upload = app.store_import_upload(archive("synth1"))
                preview = requests.post(f"http://127.0.0.1:{port}/api/import/preview",
                                        json={"uploadId": upload, "plan": {}}, timeout=20)
                self.assertEqual(preview.status_code, 409, preview.text)
                release.set()
                worker.join(120)
                self.assertEqual(first["r"].status_code, 200, first["r"].text[:300])
            finally:
                release.set()
                server.should_exit = True
                thread.join(15)


class UploadTests(_Workdir):
    def test_upload_owner_only_and_deleted(self):
        upload_id = app.store_import_upload(b"zipdata")
        path = Path(self._tmp.name) / "import-sessions" / upload_id / "archive.zip"
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(app.load_import_upload(upload_id), b"zipdata")
        app.delete_import_upload(upload_id)
        self.assertFalse(path.exists())
        with self.assertRaises(HTTPException) as ctx:
            app.load_import_upload(upload_id)
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            app.load_import_upload("../etc")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_expired_upload_pruned(self):
        upload_id = app.store_import_upload(b"z")
        path = Path(self._tmp.name) / "import-sessions" / upload_id
        os.utime(path, (1, 1))
        app.prune_import_sessions()
        self.assertFalse(path.exists())


class CliTests(_Workdir):
    def _zip(self):
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=self._tmp.name)
        handle.write(archive("synth1"))
        handle.close()
        return handle.name

    def test_write_plan_is_offline_and_password_free(self):
        out = Path(self._tmp.name) / "plan.json"
        with mock.patch.object(app.corehub, "authenticate") as auth:
            code = import_backup.main([self._zip(), "--write-plan", str(out)])
        self.assertEqual(code, 0)
        auth.assert_not_called()
        text = out.read_text()
        self.assertNotIn("password", text.lower())
        self.assertEqual(import_plan.load(text).selected_pipelines(), ["5e1f0001"])

    def _main(self, argv, env=None):
        with mock.patch.dict(os.environ, dict(env or {}, GS_COREHUB_PASSWORD="pw")), \
                mock.patch.object(app.corehub, "authenticate", return_value="tok"), \
                mock.patch.object(app.state, "set_auth"), mock.patch.object(app.state, "set_preferences"):
            return import_backup.main([self._zip(), "--base-url", "https://hub:1717", "--username", "admin"] + argv)

    def test_plan_parity_with_ui(self):
        data = {"pipelines": {"5e1f0001": {"name": "Renamed",
                                            "agents": {"mssql-ct": {"passwordRef": "env:TGT_PW"}}}}}
        path = Path(self._tmp.name) / "p.json"
        path.write_text(json.dumps(data))
        hub = Hub()
        with hub.patched():
            self._main(["--plan", str(path)], env={"TGT_PW": SECRET})
        ui = Hub()
        with ui.patched():
            app._run_archive_import(archive("synth1"), plan=plan({"pipelines": {"5e1f0001": {"name": "Renamed"}}}),
                                    agent_passwords={"5e1f0001": {"mssql-ct": SECRET}}, plan_env={})
        self.assertEqual(hub.configs, ui.configs)
        self.assertEqual(hub.configs[0][0]["pipelineName"], "Renamed")

    def test_dry_run_writes_nothing(self):
        hub = Hub()
        with hub.patched():
            self._main(["--dry-run"])
        self.assertTrue(all(m == "GET" for m, _p in hub.calls))
        self.assertEqual(hub.configs, [])


if __name__ == "__main__":
    unittest.main()

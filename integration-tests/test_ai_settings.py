#!/usr/bin/env python3
"""Offline tests for the AI settings backup: ai_settings.py, the full backup and the Automator endpoints."""

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import ai_settings  # noqa: E402

PASSPHRASE = "correct horse battery staple"


def _bundle(**overrides):
    bundle = {
        "format": "gluesync-ai-settings",
        "formatVersion": 1,
        "exportedAt": 1,
        "providers": [{"id": "p1", "name": "OpenAI", "hasApiKey": True}],
        "agents": [{"id": "a1", "name": "Analyst", "providerId": "p1"}],
        "userPreferences": [{"owner": "admin", "conversationRetentionDays": 30, "updatedAt": 1}],
        "brainSettings": {"searchMode": "KEYWORD", "updatedAt": 1},
    }
    bundle.update(overrides)
    return bundle


def _http_error(status, payload=None):
    """The RuntimeError commons.fetch_core_hub raises, with the HTTP error as its cause."""
    response = mock.Mock(status_code=status)
    response.json.return_value = payload if payload is not None else {}
    if payload is None:
        response.json.side_effect = ValueError("no json")
    cause = requests.HTTPError(f"{status} Client Error", response=response)
    try:
        raise RuntimeError(f"CoreHub request POST /x failed: {cause}") from cause
    except RuntimeError as exc:
        return exc


class ExportImportTests(unittest.TestCase):
    def test_export_without_secrets_asks_for_none(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", return_value=_bundle()) as fetch:
            self.assertEqual(_bundle(), ai_settings.export_ai_settings("tok"))
        fetch.assert_called_once_with(
            "/ai-studio/settings/export", method="POST", token="tok", body={"includeSecrets": False}
        )

    def test_export_with_secrets_sends_the_passphrase(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", return_value=_bundle()) as fetch:
            ai_settings.export_ai_settings("tok", include_secrets=True, passphrase=PASSPHRASE)
        self.assertEqual({"includeSecrets": True, "passphrase": PASSPHRASE}, fetch.call_args.kwargs["body"])

    def test_a_short_or_missing_passphrase_is_refused_before_calling(self):
        with mock.patch.object(ai_settings, "fetch_core_hub") as fetch:
            with self.assertRaises(ValueError):
                ai_settings.export_ai_settings("tok", include_secrets=True, passphrase="short")
            with self.assertRaises(ValueError):
                ai_settings.export_ai_settings("tok", include_secrets=True)
        fetch.assert_not_called()

    def test_an_older_core_hub_is_reported_as_unsupported(self):
        for status in (404, 405):
            with mock.patch.object(ai_settings, "fetch_core_hub", side_effect=_http_error(status)):
                with self.assertRaises(ai_settings.AiSettingsUnsupported) as raised:
                    ai_settings.export_ai_settings("tok")
            self.assertIn("2.3.0.1", str(raised.exception))

    def test_a_refusal_carries_core_hub_s_message(self):
        error = _http_error(400, {"message": "The passphrase does not open the secrets of this bundle"})
        bundle = _bundle()
        with mock.patch.object(ai_settings, "fetch_core_hub", side_effect=error):
            with self.assertRaises(ai_settings.AiSettingsError) as raised:
                ai_settings.import_ai_settings("tok", bundle, passphrase="wrong passphrase!")
        self.assertEqual("The passphrase does not open the secrets of this bundle", str(raised.exception))

    def test_a_refusal_without_a_json_body_keeps_the_error_text(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", side_effect=_http_error(403)):
            with self.assertRaises(ai_settings.AiSettingsError) as raised:
                ai_settings.export_ai_settings("tok")
        self.assertIn("403", str(raised.exception))

    def test_server_errors_are_not_swallowed(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", side_effect=_http_error(500)):
            with self.assertRaises(RuntimeError) as raised:
                ai_settings.export_ai_settings("tok")
        self.assertNotIsInstance(raised.exception, ai_settings.AiSettingsError)

    def test_an_answer_that_is_not_a_bundle_is_an_error(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", return_value="<html>"):
            with self.assertRaises(ai_settings.AiSettingsError):
                ai_settings.export_ai_settings("tok")

    def test_import_sends_the_bundle_and_dry_run(self):
        result = {"dryRun": True, "sections": [], "warnings": []}
        with mock.patch.object(ai_settings, "fetch_core_hub", return_value=result) as fetch:
            self.assertEqual(result, ai_settings.import_ai_settings("tok", _bundle(), dry_run=True))
        self.assertEqual({"bundle": _bundle(), "dryRun": True}, fetch.call_args.kwargs["body"])

    def test_import_refuses_what_is_not_a_bundle_or_lacks_its_passphrase(self):
        sealed = _bundle(secrets={"cipher": "AES-256-GCM"})
        with mock.patch.object(ai_settings, "fetch_core_hub") as fetch:
            with self.assertRaises(ValueError):
                ai_settings.import_ai_settings("tok", {"format": "something-else"})
            with self.assertRaises(ValueError) as raised:
                ai_settings.import_ai_settings("tok", sealed)
        self.assertIn("passphrase", str(raised.exception))
        fetch.assert_not_called()


class SummaryAndCompareTests(unittest.TestCase):
    def test_summary_lists_changes_then_what_is_left_to_do(self):
        result = {
            "dryRun": False,
            "sections": [
                {"name": "providers", "created": 1, "updated": 0, "unchanged": 0, "skipped": 0},
                {"name": "apiKeys", "created": 0, "updated": 2, "unchanged": 1, "skipped": 1},
                {"name": "budgets", "created": 0, "updated": 0, "unchanged": 3, "skipped": 0},
            ],
            "warnings": ["Provider OpenAI (p1) has no API key: enter it again in AI Studio"],
        }
        self.assertEqual(
            "providers: 1 created; apiKeys: 2 updated, 1 skipped. "
            "To do: Provider OpenAI (p1) has no API key: enter it again in AI Studio",
            ai_settings.summarize_import(result),
        )

    def test_summary_of_a_dry_run_with_nothing_to_change(self):
        result = {"dryRun": True, "sections": [{"name": "providers", "unchanged": 1}]}
        self.assertEqual("Dry run, nothing written. nothing to change", ai_settings.summarize_import(result))

    def test_two_exports_of_the_same_settings_compare_equal(self):
        left = _bundle(exportedAt=1, secrets=None)
        right = _bundle(exportedAt=99, secrets={"cipher": "AES-256-GCM"})
        self.assertEqual([], ai_settings.compare_bundles(left, right))

    def test_compare_names_missing_unexpected_and_changed_rows(self):
        left = _bundle()
        right = _bundle(
            providers=[{"id": "p1", "name": "OpenAI EU", "hasApiKey": True}, {"id": "p2"}],
            agents=[],
            brainSettings={"searchMode": "EMBEDDINGS", "updatedAt": 1},
        )
        differences = ai_settings.compare_bundles(left, right)
        self.assertIn("agents: a1 missing", differences)
        self.assertIn("providers: p2 unexpected", differences)
        self.assertIn("providers: p1 differs in name", differences)
        self.assertTrue(any(d.startswith("brainSettings:") for d in differences), differences)

    def test_rows_without_ids_are_compared_by_owner_or_content(self):
        left = _bundle(userPreferences=[{"owner": "admin", "conversationRetentionDays": 30}], tags=["a"])
        right = _bundle(userPreferences=[{"owner": "admin", "conversationRetentionDays": 7}], tags=["b"])
        differences = ai_settings.compare_bundles(left, right)
        self.assertIn("userPreferences: admin differs in conversationRetentionDays", differences)
        self.assertIn('tags: "a" missing', differences)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _run(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), redirect_stdout(out), redirect_stderr(err):
            code = ai_settings.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_export_writes_the_bundle_with_the_passphrase_from_the_environment(self):
        output = self.dir / "ai.json"
        with mock.patch.object(ai_settings, "export_ai_settings", return_value=_bundle()) as export:
            code, out, _ = self._run(
                "--token", "tok", "export", "--output", str(output), "--include-secrets",
                env={ai_settings.PASSPHRASE_ENV: PASSPHRASE},
            )
        self.assertEqual(0, code)
        self.assertEqual(_bundle(), json.loads(output.read_text()))
        export.assert_called_once_with("tok", include_secrets=True, passphrase=PASSPHRASE)
        self.assertIn("with secrets", out)

    def test_import_prints_the_summary(self):
        source = self.dir / "ai.json"
        source.write_text(json.dumps(_bundle()))
        result = {"dryRun": True, "sections": [{"name": "agents", "created": 1}], "warnings": []}
        with mock.patch.object(ai_settings, "import_ai_settings", return_value=result) as do_import:
            code, out, _ = self._run("--token", "tok", "import", "--input", str(source), "--dry-run")
        self.assertEqual(0, code)
        self.assertIn("agents: 1 created", out)
        self.assertTrue(do_import.call_args.kwargs["dry_run"])

    def test_compare_exits_one_on_differences(self):
        left, right = self.dir / "l.json", self.dir / "r.json"
        left.write_text(json.dumps(_bundle()))
        right.write_text(json.dumps(_bundle(agents=[])))
        code, out, _ = self._run("compare", str(left), str(right))
        self.assertEqual(1, code)
        self.assertIn("agents: a1 missing", out)
        right.write_text(json.dumps(_bundle(exportedAt=5)))
        self.assertEqual(0, self._run("compare", str(left), str(right))[0])

    def test_errors_exit_two_with_the_reason(self):
        with mock.patch.object(ai_settings, "export_ai_settings", side_effect=ai_settings.AiSettingsUnsupported("old hub")):
            code, _, err = self._run("--token", "tok", "export", "--output", str(self.dir / "x.json"))
        self.assertEqual(2, code)
        self.assertIn("old hub", err)

    def test_without_a_token_it_logs_in_as_admin(self):
        with mock.patch.dict(os.environ, {"DEFAULT_PASSWORD": "admin"}), \
                mock.patch.object(ai_settings, "fetch_core_hub", return_value={"apiToken": "fresh"}) as fetch, \
                mock.patch.object(ai_settings, "export_ai_settings", return_value=_bundle()) as export:
            os.environ.pop(ai_settings.TOKEN_ENV, None)
            code, _, _ = self._run("export", "--output", str(self.dir / "x.json"))
        self.assertEqual(0, code)
        self.assertEqual({"username": "admin", "password": "admin"}, fetch.call_args.kwargs["body"])
        self.assertEqual("fresh", export.call_args.args[0])

    def test_a_failed_login_stops_with_a_message(self):
        with mock.patch.object(ai_settings, "fetch_core_hub", return_value={}):
            os.environ.pop(ai_settings.TOKEN_ENV, None)
            with self.assertRaises(SystemExit):
                self._run("export")


class FullBackupTests(unittest.TestCase):
    def setUp(self):
        import automator_app.corehub as corehub

        self.corehub = corehub
        empty = mock.Mock(return_value={})
        self.patches = [
            mock.patch.object(corehub, name, empty)
            for name in (
                "export_global_configs", "export_smtp_settings", "export_webhooks_config", "export_thresholds",
                "export_schedules", "export_users", "export_oidc_config", "export_user_groups",
                "export_data_access_policies",
            )
        ]
        self.patches.append(mock.patch.object(corehub, "export_all_pipelines_yaml", return_value=self._empty_zip()))
        self.patches.append(mock.patch.object(corehub, "configure_core_hub"))
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def _empty_zip():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w"):
            pass
        return buffer.getvalue()

    def _backup(self, **kwargs):
        data = self.corehub.export_full_corehub_backup(
            token="tok", base_url="http://hub", use_ssl=False, skip_verify=False, **kwargs
        )
        return zipfile.ZipFile(io.BytesIO(data))

    def test_the_backup_holds_the_ai_settings_and_seals_secrets_only_with_a_passphrase(self):
        with mock.patch.object(ai_settings, "export_ai_settings", return_value=_bundle()) as export:
            archive = self._backup(include_secrets=True, ai_settings_passphrase=PASSPHRASE)
            self._backup(include_secrets=True)
        self.assertEqual(_bundle(), json.loads(archive.read("ai-settings.json")))
        self.assertEqual(
            [mock.call("tok", include_secrets=True, passphrase=PASSPHRASE),
             mock.call("tok", include_secrets=False, passphrase=None)],
            export.call_args_list,
        )

    def test_an_older_core_hub_adds_no_ai_file(self):
        with mock.patch.object(ai_settings, "export_ai_settings", side_effect=ai_settings.AiSettingsUnsupported("old")):
            names = self._backup().namelist()
        self.assertNotIn("ai-settings.json", names)
        self.assertNotIn("ai-settings.error.yaml", names)

    def test_a_failed_ai_export_is_written_down(self):
        with mock.patch.object(ai_settings, "export_ai_settings", side_effect=RuntimeError("boom")):
            archive = self._backup()
        self.assertIn("boom", archive.read("ai-settings.error.yaml").decode())

    def _restore(self, files, passphrase=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        names = ("import_global_configs", "import_smtp_settings", "import_webhooks_config", "import_thresholds",
                 "import_schedules", "import_users", "import_oidc_config")
        with mock.patch.multiple(self.corehub, **{name: mock.Mock(return_value={}) for name in names}):
            return self.corehub.import_full_corehub_backup(
                token="tok", base_url="http://hub", use_ssl=False, skip_verify=False,
                zip_bytes=buffer.getvalue(), ai_settings_passphrase=passphrase,
            )["ai_settings"]

    def test_the_restore_imports_the_ai_settings_with_the_passphrase(self):
        result = {"dryRun": False, "sections": [{"name": "agents", "created": 1}], "warnings": ["do this"]}
        with mock.patch.object(ai_settings, "import_ai_settings", return_value=result) as do_import:
            restored = self._restore({"ai-settings.json": json.dumps(_bundle())}, passphrase=PASSPHRASE)
        do_import.assert_called_once_with("tok", _bundle(), passphrase=PASSPHRASE, dry_run=False)
        self.assertEqual("restored", restored["status"])
        self.assertEqual(["do this"], restored["warnings"])
        self.assertIn("agents: 1 created", restored["summary"])

    def test_the_restore_reports_a_missing_failed_or_refused_ai_file(self):
        self.assertIn("not found", self._restore({})["reason"])
        self.assertIn("could not export", self._restore({"ai-settings.error.yaml": "error: boom"})["reason"])
        with mock.patch.object(ai_settings, "import_ai_settings", side_effect=ai_settings.AiSettingsError("wrong passphrase")):
            failed = self._restore({"ai-settings.json": json.dumps(_bundle())})
        self.assertEqual({"status": "failed", "reason": "wrong passphrase"}, failed)
        with mock.patch.object(ai_settings, "import_ai_settings", side_effect=ai_settings.AiSettingsUnsupported("old hub")):
            skipped = self._restore({"ai-settings.json": json.dumps(_bundle())})
        self.assertEqual({"status": "skipped", "reason": "old hub"}, skipped)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.state = state
        cls.client = TestClient(app_module.create_app())

    def setUp(self):
        self.state.set_auth("tok", "http://hub", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)
        import automator_app.corehub as corehub

        patch = mock.patch.object(corehub, "configure_core_hub")
        patch.start()
        self.addCleanup(patch.stop)
        self.corehub = corehub

    def test_export_downloads_the_bundle(self):
        with mock.patch.object(ai_settings, "export_ai_settings", return_value=_bundle()) as export:
            response = self.client.post(
                "/api/export/ai-settings", json={"include_secrets": True, "passphrase": PASSPHRASE}
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(_bundle(), response.json())
        self.assertIn("attachment", response.headers["content-disposition"])
        export.assert_called_once_with("tok", include_secrets=True, passphrase=PASSPHRASE)

    def test_export_errors_map_to_clear_statuses(self):
        cases = [
            (ai_settings.AiSettingsUnsupported("needs 2.3.0.1"), 409),
            (ValueError("passphrase too short"), 400),
            (RuntimeError("down"), 500),
        ]
        for error, status in cases:
            with mock.patch.object(ai_settings, "export_ai_settings", side_effect=error):
                response = self.client.post("/api/export/ai-settings", json={})
            self.assertEqual(status, response.status_code, error)

    def test_import_returns_the_summary_and_the_warnings(self):
        restored = {"status": "checked", "summary": "Dry run, nothing written. agents: 1 created", "warnings": ["w1"]}
        with mock.patch.object(self.corehub, "restore_ai_settings", return_value=restored) as restore:
            response = self.client.post(
                "/api/import/ai-settings",
                files={"file": ("ai.json", json.dumps(_bundle()), "application/json")},
                data={"passphrase": PASSPHRASE, "dry_run": "true"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(restored["summary"], response.json()["message"])
        self.assertEqual(["w1"], response.json()["logs"])
        self.assertTrue(restore.call_args.kwargs["dry_run"])
        self.assertEqual(PASSPHRASE, restore.call_args.kwargs["passphrase"])

    def test_import_refuses_a_file_that_is_not_json_and_reports_an_old_hub(self):
        bad = self.client.post("/api/import/ai-settings", files={"file": ("ai.json", b"\xff\xfe", "application/json")})
        self.assertEqual(400, bad.status_code)
        with mock.patch.object(self.corehub, "restore_ai_settings", return_value={"status": "skipped", "reason": "old"}):
            old = self.client.post("/api/import/ai-settings", files={"file": ("ai.json", json.dumps(_bundle()))})
        self.assertEqual(409, old.status_code)

    def test_import_maps_refusals_and_failures(self):
        for error, status in ((ai_settings.AiSettingsError("wrong passphrase"), 400), (RuntimeError("down"), 500)):
            with mock.patch.object(self.corehub, "restore_ai_settings", side_effect=error):
                response = self.client.post("/api/import/ai-settings", files={"file": ("ai.json", json.dumps(_bundle()))})
            self.assertEqual(status, response.status_code)

    def test_full_backup_by_post_carries_the_ai_passphrase(self):
        with mock.patch.object(self.corehub, "export_full_corehub_backup", return_value=b"PK") as export:
            response = self.client.post(
                "/api/export/full-backup", json={"include_secrets": True, "ai_settings_passphrase": PASSPHRASE}
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(PASSPHRASE, export.call_args.kwargs["ai_settings_passphrase"])
        short = self.client.post("/api/export/full-backup", json={"include_secrets": True, "ai_settings_passphrase": "x"})
        self.assertEqual(400, short.status_code)

    def test_endpoints_need_a_login(self):
        self.state.clear_auth()
        self.assertEqual(401, self.client.post("/api/export/ai-settings", json={}).status_code)
        self.assertEqual(401, self.client.post("/api/export/full-backup", json={}).status_code)
        response = self.client.post("/api/import/ai-settings", files={"file": ("ai.json", "{}")})
        self.assertEqual(401, response.status_code)


if __name__ == "__main__":
    unittest.main()

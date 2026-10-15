#!/usr/bin/env python3
"""Credentials never reach the Bootstrapper's debug log.

fetch_core_hub logs every request and response at DEBUG, and the demo kits ship that level to ELK. The admin
login (password, apiToken), the AI settings export (passphrase, currentPassword) and the Authorization header
of a certificate upload must show as *** there.
"""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import commons  # noqa: E402
from utils.redact import REDACTED, redact  # noqa: E402

SECRETS = ("hunter2-admin", "a passphrase of twelve+", "the current password", "eyJ.session.token", "sk-live-123")


class RedactTests(unittest.TestCase):
    def test_credentials_are_hidden_at_any_depth(self):
        body = {
            "username": "admin",
            "password": "hunter2-admin",
            "currentPassword": "the current password",
            "bundle": {"providers": [{"name": "OpenAI", "apiKey": "sk-live-123"}], "secrets": {"cipher": "x"}},
            "hostCredentials": {"host": "db", "password": "p", "private_key": "k"},
            "Authorization": "Bearer eyJ.session.token",
        }
        hidden = redact(body)
        self.assertEqual("admin", hidden["username"])
        self.assertEqual("OpenAI", hidden["bundle"]["providers"][0]["name"])
        self.assertEqual("db", hidden["hostCredentials"]["host"])
        for path in (("password",), ("currentPassword",), ("bundle", "providers", 0, "apiKey"), ("bundle", "secrets"),
                     ("hostCredentials", "password"), ("hostCredentials", "private_key"), ("Authorization",)):
            value = hidden
            for step in path:
                value = value[step]
            self.assertEqual(REDACTED, value, path)

    def test_flags_and_empty_values_stay_readable(self):
        self.assertEqual({"includeSecrets": True, "hasApiKey": False, "currentPassword": None},
                         redact({"includeSecrets": True, "hasApiKey": False, "currentPassword": None}))

    def test_the_original_is_not_changed(self):
        body = {"password": "hunter2-admin"}
        redact(body)
        self.assertEqual({"password": "hunter2-admin"}, body)


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        # Other test modules switch logging off with logging.disable when imported: this test reads the log.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)

    def _debug_log_of(self, path, body, response):
        client = mock.Mock()
        client.request.return_value = response
        with mock.patch.object(commons, "core_hub_client", client), \
                self.assertLogs(commons.logger.name, level=logging.DEBUG) as logs:
            commons.fetch_core_hub(path, method="POST", token="eyJ.session.token", body=body)
        return "\n".join(logs.output)

    def test_login_and_ai_settings_export_log_no_credential(self):
        login_log = self._debug_log_of("/authentication/login", {"username": "admin", "password": "hunter2-admin"},
                                       {"apiToken": "eyJ.session.token", "changeRequired": False})
        export_log = self._debug_log_of(
            "/ai-studio/settings/export",
            {"includeSecrets": True, "passphrase": "a passphrase of twelve+", "currentPassword": "the current password"},
            {"format": "gluesync-ai-settings", "providers": [{"name": "OpenAI", "apiKey": "sk-live-123"}]},
        )
        for log in (login_log, export_log):
            for secret in SECRETS:
                self.assertNotIn(secret, log)
        self.assertIn("/authentication/login", login_log)
        self.assertIn('"includeSecrets": true', export_log)
        self.assertIn(REDACTED, export_log)


if __name__ == "__main__":
    unittest.main()

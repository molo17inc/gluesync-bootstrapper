#!/usr/bin/env python3
"""The admin password the bootstrapper sets at the first login is stored, never logged.

Core Hub answers the first admin login with ``changeRequired``; ``main.py`` then sets a random password. The
login token it saves expires after minutes and does not survive a Core Hub restart, so the new password is saved
too (``admin_credentials.json`` next to ``auth_token.json``, 0600): a re-run of the bootstrapper and the Automator
round trip log in with it. The password itself never reaches a log, which the demo kits ship to ELK.

``main.py`` runs its pipeline at import time (it logs in to CoreHub), so these tests load only the login functions
from its source, against a fake Core Hub reached through the real ``commons.fetch_core_hub``.
"""

import ast
import contextlib
import io
import json
import logging
import os
import secrets
import stat
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_roundtrip as rt  # noqa: E402
import commons  # noqa: E402
from utils.log import lockfile_failure, log_failure, log_success  # noqa: E402

FUNCTIONS = (
    "generate_random_password", "save_token", "save_admin_password", "change_password", "_login",
    "_login_with_known_passwords", "_complete_default_login",
)
DEFAULT_PASSWORD = "the-old-default"
LOGGER_NAME = "test-main-credentials"


class FakeCoreHub:
    """Core Hub's login, password reset and pipelines endpoints, behind CoreHubClient.request."""

    def __init__(self, password=DEFAULT_PASSWORD, change_required=True):
        self.password = password
        self.change_required = change_required
        self.tokens = set()
        self.login_attempts = []
        self.issued = 0
        self.refuse_new_password = False

    def restart(self):
        """Tokens do not survive a Core Hub restart."""
        self.tokens.clear()

    def request(self, path, method="GET", token=None, body=None, params=None, headers=None,
                expected_error_statuses=None):
        if path == "/authentication/login":
            self.login_attempts.append(body["password"])
            if body["password"] != self.password:
                raise RuntimeError("401 Unauthorized")
            self.issued += 1
            token = f"jwt-{self.issued}"
            self.tokens.add(token)
            return {"apiToken": token, "changeRequired": self.change_required}
        if token not in self.tokens:
            raise RuntimeError("401 Unauthorized")
        if path == "/authentication/reset-password":
            if body["oldPassword"] != self.password:
                raise RuntimeError("400 wrong old password")
            if self.refuse_new_password:
                raise RuntimeError("400 password rejected: too weak")
            self.password = body["newPassword"]
            self.change_required = False
            return "Password changed"
        if path == "/pipelines":
            return []
        raise AssertionError(path)


def load_main_functions(config_dir, user_defined_password=DEFAULT_PASSWORD, admin_password=""):
    """The login functions of main.py, in a namespace whose paths are in ``config_dir``."""
    tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assert sorted(n.name for n in wanted) == sorted(FUNCTIONS)
    token_path = os.path.join(config_dir, "auth_token.json")
    namespace = {
        "os": os, "json": json, "secrets": secrets, "string": string,
        "logger": logging.getLogger(LOGGER_NAME),
        "log_success": log_success, "log_failure": log_failure, "lockfile_failure": lockfile_failure,
        "fetch_core_hub": commons.fetch_core_hub,
        "save_admin_credentials": commons.save_admin_credentials,
        "load_admin_password": commons.load_admin_password,
        "BootstrapperError": type("BootstrapperError", (Exception,), {}),
        "AUTH_TOKEN_PATH": token_path,
        "ADMIN_CREDENTIALS_PATH": commons.admin_credentials_path(token_path),
        "LOGIN_PATH": "/authentication/login",
        "PIPELINES_PATH": "/pipelines",
        "default_user": "admin",
        "user_defined_password": user_defined_password,
        "admin_password": admin_password,
        "admin_password_problem": commons.admin_password_problem,
        "login_password_candidates": commons.login_password_candidates,
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "main.py", "exec"), namespace)  # noqa: S102
    return namespace


class AdminCredentialsTestCase(unittest.TestCase):
    def setUp(self):
        # Other test modules switch logging off with logging.disable when imported: these tests read the log.
        previous = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = tmp.name
        self.token_file = os.path.join(self.config_dir, "auth_token.json")
        self.credentials_file = os.path.join(self.config_dir, "admin_credentials.json")

        self.log_stream = io.StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setLevel(logging.DEBUG)
        self.loggers = [logging.getLogger(LOGGER_NAME), commons.logger]
        for logger in self.loggers:
            # only the capture sees the records: no console handler, no parent
            self.addCleanup(setattr, logger, "handlers", logger.handlers[:])
            self.addCleanup(setattr, logger, "propagate", logger.propagate)
            self.addCleanup(logger.setLevel, logger.level)
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.DEBUG)

        self.hub = FakeCoreHub()
        client = mock.Mock()
        client.request.side_effect = self.hub.request
        patcher = mock.patch.object(commons, "core_hub_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def first_run(self, main=None):
        """The first login: Core Hub wants the default password changed. Returns (main, token, stdout)."""
        main = main or load_main_functions(self.config_dir)
        login = main["_login_with_known_passwords"]()
        self.assertTrue(login["changeRequired"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            token = main["_complete_default_login"](login["apiToken"], True)
        return main, token, stdout.getvalue()

    def saved_credentials(self):
        with open(self.credentials_file, encoding="utf-8") as f:
            return json.load(f)

    def everything_written(self, stdout=""):
        return self.log_stream.getvalue() + stdout


class SaveAfterPasswordChangeTests(AdminCredentialsTestCase):
    def test_new_credentials_are_saved_next_to_the_token_with_0600(self):
        _, token, _ = self.first_run()

        self.assertEqual(os.path.dirname(self.credentials_file), os.path.dirname(self.token_file))
        self.assertEqual({"username": "admin", "password": self.hub.password}, self.saved_credentials())
        self.assertNotEqual(DEFAULT_PASSWORD, self.saved_credentials()["password"])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.credentials_file).st_mode))
        self.assertIn(token, self.hub.tokens)
        # the JWT is still saved as before
        with open(self.token_file, encoding="utf-8") as f:
            self.assertEqual({"token": token}, json.load(f))

    def test_the_credentials_file_is_written_under_a_private_name_and_renamed(self):
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            seen["src"], seen["dst"] = src, dst
            seen["dst_existed"] = os.path.exists(dst)
            seen["src_mode"] = stat.S_IMODE(os.stat(src).st_mode)
            seen["src_content"] = json.loads(Path(src).read_text())
            return real_replace(src, dst)

        with mock.patch.object(commons.os, "replace", side_effect=spy):
            self.first_run()

        self.assertEqual(self.credentials_file, seen["dst"])
        self.assertNotEqual(seen["src"], seen["dst"])
        self.assertEqual(os.path.dirname(seen["src"]), self.config_dir)
        self.assertFalse(seen["dst_existed"])
        self.assertEqual(0o600, seen["src_mode"])
        self.assertEqual(self.hub.password, seen["src_content"]["password"])
        self.assertFalse([n for n in os.listdir(self.config_dir) if n.endswith(".tmp")])

    def test_a_failed_write_keeps_the_previous_file_and_leaves_no_temporary_file(self):
        commons.save_admin_credentials(self.credentials_file, "previous-password")
        with mock.patch.object(commons.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                commons.save_admin_credentials(self.credentials_file, "new-password")
        self.assertEqual("previous-password", self.saved_credentials()["password"])
        self.assertEqual(["admin_credentials.json"], os.listdir(self.config_dir))

    def test_an_existing_file_gets_0600_again(self):
        Path(self.credentials_file).write_text("{}")
        os.chmod(self.credentials_file, 0o644)
        commons.save_admin_credentials(self.credentials_file, "p4ss")
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.credentials_file).st_mode))

    def test_a_save_that_fails_does_not_fail_the_login_and_says_so(self):
        main = load_main_functions(self.config_dir)
        main["save_admin_credentials"] = mock.Mock(side_effect=OSError("read-only file system"))
        _, token, stdout = self.first_run(main)

        self.assertIn(token, self.hub.tokens)
        self.assertFalse(os.path.exists(self.credentials_file))
        written = self.everything_written(stdout)
        self.assertIn("could not be stored", written)
        self.assertNotIn(self.hub.password, written)

    def test_no_new_credentials_when_core_hub_does_not_ask_for_a_change(self):
        self.hub.change_required = False
        main = load_main_functions(self.config_dir)
        login = main["_login_with_known_passwords"]()
        with contextlib.redirect_stdout(io.StringIO()):
            main["_complete_default_login"](login["apiToken"], False)
        self.assertFalse(os.path.exists(self.credentials_file))


class NoPasswordInLogsTests(AdminCredentialsTestCase):
    def test_the_new_password_never_reaches_a_log_or_stdout(self):
        _, _, stdout = self.first_run()
        written = self.everything_written(stdout)

        self.assertNotEqual(DEFAULT_PASSWORD, self.hub.password)
        self.assertNotIn(self.hub.password, written)
        self.assertNotIn("[NEW PASSWORD]", written)
        # the log says where it is stored, so an operator can find it
        self.assertIn(self.credentials_file, written)
        self.assertIn("password changed", written.lower())

    def test_a_rerun_logs_neither_password(self):
        main, _, stdout = self.first_run()
        before = self.hub.password
        self.hub.restart()
        main["_login_with_known_passwords"]()
        written = self.everything_written(stdout)
        self.assertNotIn(before, written)
        self.assertNotIn(DEFAULT_PASSWORD, written)


class RerunLoginTests(AdminCredentialsTestCase):
    def test_a_rerun_logs_in_with_the_saved_password_first(self):
        self.first_run()
        self.hub.restart()
        self.hub.login_attempts.clear()

        rerun = load_main_functions(self.config_dir)  # a new process in the same container
        login = rerun["_login_with_known_passwords"]()

        self.assertEqual([self.hub.password], self.hub.login_attempts)
        self.assertFalse(login["changeRequired"])
        self.assertIn(login["apiToken"], self.hub.tokens)

    def test_without_a_saved_password_only_default_password_is_tried(self):
        main = load_main_functions(self.config_dir)
        main["_login_with_known_passwords"]()
        self.assertEqual([DEFAULT_PASSWORD], self.hub.login_attempts)

    def test_a_stale_saved_password_falls_back_to_default_password(self):
        commons.save_admin_credentials(self.credentials_file, "stale-password")
        main = load_main_functions(self.config_dir)
        login = main["_login_with_known_passwords"]()
        self.assertEqual(["stale-password", DEFAULT_PASSWORD], self.hub.login_attempts)
        self.assertIn(login["apiToken"], self.hub.tokens)

    def test_when_both_fail_the_error_surfaces(self):
        commons.save_admin_credentials(self.credentials_file, "stale-password")
        self.hub.password = "somebody-changed-it"
        main = load_main_functions(self.config_dir)
        with self.assertRaises(RuntimeError):
            main["_login_with_known_passwords"]()
        self.assertEqual(["stale-password", DEFAULT_PASSWORD], self.hub.login_attempts)

    def test_a_saved_password_equal_to_default_password_is_tried_once(self):
        self.hub.change_required = False
        commons.save_admin_credentials(self.credentials_file, DEFAULT_PASSWORD)
        main = load_main_functions(self.config_dir)
        main["_login_with_known_passwords"]()
        self.assertEqual([DEFAULT_PASSWORD], self.hub.login_attempts)

    def test_an_unreadable_credentials_file_is_ignored(self):
        Path(self.credentials_file).write_text("not json")
        main = load_main_functions(self.config_dir)
        main["_login_with_known_passwords"]()
        self.assertEqual([DEFAULT_PASSWORD], self.hub.login_attempts)

    def test_a_password_change_that_fails_continues_with_the_known_passwords(self):
        main = load_main_functions(self.config_dir)
        login = main["_login_with_known_passwords"]()
        self.hub.change_required = False  # reset-password then fails, the login with the old one still works
        main["change_password"] = mock.Mock(side_effect=RuntimeError("boom"))
        with contextlib.redirect_stdout(io.StringIO()):
            token = main["_complete_default_login"](login["apiToken"], True)
        self.assertIn(token, self.hub.tokens)
        self.assertFalse(os.path.exists(self.credentials_file))


TESTER_PASSWORD = "Tester-Pa55word!"


class AdminPasswordTests(AdminCredentialsTestCase):
    """ADMIN_PASSWORD: the password a tester wants, set at the password change and saved like a random one."""

    def test_admin_password_is_set_saved_and_not_logged(self):
        main = load_main_functions(self.config_dir, admin_password=TESTER_PASSWORD)
        _, token, stdout = self.first_run(main)

        self.assertEqual(TESTER_PASSWORD, self.hub.password)
        self.assertEqual({"username": "admin", "password": TESTER_PASSWORD}, self.saved_credentials())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.credentials_file).st_mode))
        self.assertIn(token, self.hub.tokens)
        written = self.everything_written(stdout)
        self.assertNotIn(TESTER_PASSWORD, written)
        self.assertIn(self.credentials_file, written)

    def test_a_random_password_is_set_when_admin_password_is_unset(self):
        self.first_run(load_main_functions(self.config_dir, admin_password=""))
        password = self.hub.password
        self.assertNotIn(password, (DEFAULT_PASSWORD, TESTER_PASSWORD, ""))
        self.assertEqual(12, len(password))
        self.assertIsNone(commons.admin_password_problem(password))
        self.assertEqual(password, self.saved_credentials()["password"])

    def test_an_unacceptable_password_fails_clearly_without_the_value(self):
        for weak in ("short1A!", "alllowercase-but-long1!", "ALLUPPERCASE-LONG-1!", "NoDigitsHere-long!",
                     "NoSymbolsHere1234Abc", DEFAULT_PASSWORD):
            with self.subTest(weak=weak):
                main = load_main_functions(self.config_dir, admin_password=weak)
                login = main["_login_with_known_passwords"]()
                with self.assertRaises(main["BootstrapperError"]) as raised, \
                        contextlib.redirect_stdout(io.StringIO()):
                    main["_complete_default_login"](login["apiToken"], True)
                self.assertIn("ADMIN_PASSWORD", str(raised.exception))
                self.assertNotIn(weak, str(raised.exception))
                self.assertNotIn(weak, self.everything_written())
                self.assertEqual(DEFAULT_PASSWORD, self.hub.password)  # nothing was changed
                self.assertFalse(os.path.exists(self.credentials_file))

    def test_a_password_core_hub_refuses_fails_without_the_value_or_a_fallback(self):
        self.hub.refuse_new_password = True
        main = load_main_functions(self.config_dir, admin_password=TESTER_PASSWORD)
        login = main["_login_with_known_passwords"]()
        with self.assertRaises(main["BootstrapperError"]) as raised, contextlib.redirect_stdout(io.StringIO()):
            main["_complete_default_login"](login["apiToken"], True)
        self.assertIn("did not accept ADMIN_PASSWORD", str(raised.exception))
        self.assertNotIn(TESTER_PASSWORD, str(raised.exception))
        self.assertNotIn(TESTER_PASSWORD, self.everything_written())
        self.assertFalse(os.path.exists(self.credentials_file))

    def test_login_order_is_saved_then_admin_password_then_default_password(self):
        commons.save_admin_credentials(self.credentials_file, "stale-saved-password")
        self.hub.change_required = False
        self.hub.password = DEFAULT_PASSWORD
        main = load_main_functions(self.config_dir, admin_password=TESTER_PASSWORD)
        main["_login_with_known_passwords"]()
        self.assertEqual(["stale-saved-password", TESTER_PASSWORD, DEFAULT_PASSWORD], self.hub.login_attempts)

    def test_admin_password_is_used_when_no_credentials_file_exists(self):
        self.hub.change_required = False
        self.hub.password = TESTER_PASSWORD
        main = load_main_functions(self.config_dir, admin_password=TESTER_PASSWORD)
        login = main["_login_with_known_passwords"]()
        self.assertEqual([TESTER_PASSWORD], self.hub.login_attempts)
        self.assertIn(login["apiToken"], self.hub.tokens)

    def test_no_password_is_tried_twice(self):
        self.assertEqual(
            ["a", "b", ""],
            commons.login_password_candidates("a", "b", ""))
        self.assertEqual(["a", "b"], commons.login_password_candidates("a", "a", "b"))
        self.assertEqual(["b"], commons.login_password_candidates(None, "b", "b"))
        self.assertEqual([""], commons.login_password_candidates(None, "", ""))

    def test_round_trip_uses_admin_password_without_a_credentials_file(self):
        self.hub.change_required = False
        self.hub.password = TESTER_PASSWORD
        with mock.patch.dict("os.environ", {"USE_SDK": "false", "DEFAULT_PASSWORD": DEFAULT_PASSWORD,
                                            "ADMIN_PASSWORD": TESTER_PASSWORD}), \
                mock.patch.object(rt, "fetch_core_hub", commons.fetch_core_hub):
            token = rt.authenticate(self.token_file)
        self.assertIn(token, self.hub.tokens)
        self.assertEqual([TESTER_PASSWORD], self.hub.login_attempts)

    def test_round_trip_order_is_saved_admin_default_then_jwt(self):
        commons.save_admin_credentials(self.credentials_file, "stale-saved-password")
        Path(self.token_file).write_text(json.dumps({"token": "dead-jwt"}))
        self.hub.password = "nobody-knows"
        with mock.patch.dict("os.environ", {"USE_SDK": "false", "DEFAULT_PASSWORD": DEFAULT_PASSWORD,
                                            "ADMIN_PASSWORD": TESTER_PASSWORD}), \
                mock.patch.object(rt, "fetch_core_hub", commons.fetch_core_hub):
            with self.assertRaises(RuntimeError) as raised:
                rt.authenticate(self.token_file)
        self.assertEqual(["stale-saved-password", TESTER_PASSWORD, DEFAULT_PASSWORD], self.hub.login_attempts)
        message = str(raised.exception)
        self.assertIn("ADMIN_PASSWORD", message)
        for secret in ("stale-saved-password", TESTER_PASSWORD, DEFAULT_PASSWORD, "dead-jwt"):
            self.assertNotIn(secret, message)


class RoundTripAfterBootstrapperTests(AdminCredentialsTestCase):
    """The Automator round trip authenticates with what the bootstrapper saved."""

    def authenticate(self):
        with mock.patch.dict("os.environ", {"USE_SDK": "false", "DEFAULT_PASSWORD": DEFAULT_PASSWORD}), \
                mock.patch.object(rt, "fetch_core_hub", commons.fetch_core_hub):
            return rt.authenticate(self.token_file)

    def test_saved_password_works_after_the_jwt_expired_and_core_hub_restarted(self):
        _, saved_jwt, _ = self.first_run()
        self.hub.restart()  # the saved JWT is now refused, and DEFAULT_PASSWORD is the old password
        self.hub.login_attempts.clear()

        token = self.authenticate()

        self.assertNotEqual(saved_jwt, token)
        self.assertIn(token, self.hub.tokens)
        self.assertEqual([self.hub.password], self.hub.login_attempts)

    def test_both_sides_agree_on_the_file_location_and_format(self):
        self.first_run()
        self.assertEqual(self.hub.password, commons.load_admin_password(commons.admin_credentials_path(self.token_file)))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The Windows container image of the bootstrapper: one PyInstaller exe that does what the Linux image does.

The demo-kits run the Linux image two ways: the ``hub-first-login`` one-shot (a ``python -c`` script) and the
bootstrapper itself (``python main.py``, which starts ``python3 create_all_entities.py`` once per schema). The Windows
image has no Python, so ``bootstrapper_exe.py`` is the entry point of both and ``hub_first_login.py`` is the script.
These tests run the real code against a local fake Core Hub; the exe itself is smoke-tested in CI on the Windows
runners (scripts/windows-image-smoke.ps1).
"""

import ast
import contextlib
import io
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bootstrapper_exe  # noqa: E402
import hub_first_login  # noqa: E402

NEW_PASSWORD = "Test123!"


class FakeCoreHubServer:
    """The two authentication routes of Core Hub, on a free local port."""

    def __init__(self, password="admin", change_required=True):
        self.password = password
        self.change_required = change_required
        self.requests = []
        self.refuse_reset = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append((self.path, dict(self.headers), body))
                if self.path == "/authentication/login":
                    if body.get("password") != outer.password:
                        return self.reply(401, {"message": "bad credentials"})
                    return self.reply(200, {"apiToken": "token-1", "changeRequired": outer.change_required})
                if self.path == "/authentication/reset-password":
                    if outer.refuse_reset or self.headers.get("Authorization") != "Bearer token-1":
                        return self.reply(403, {"message": "refused"})
                    outer.password = body["newPassword"]
                    outer.change_required = False
                    return self.reply(200, {})
                return self.reply(404, {"message": "unknown"})

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def run_hub_first_login(url, *extra, password=NEW_PASSWORD, env=None):
    """hub_first_login.main with CORE_HUB_URL and HUB_ADMIN_PASSWORD set; returns (exit code, stdout, stderr)."""
    environment = {"CORE_HUB_URL": url, "HUB_ADMIN_PASSWORD": password}
    environment.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", environment, clear=True), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = hub_first_login.main(["--interval", "0", *extra])
    return code, out.getvalue(), err.getvalue()


class HubFirstLoginTests(unittest.TestCase):
    def test_fresh_hub_gets_the_new_admin_password(self):
        with FakeCoreHubServer() as hub:
            code, out, _ = run_hub_first_login(hub.url)
            self.assertEqual(code, 0)
            self.assertIn("first access completed: 200", out)
            self.assertEqual(hub.password, NEW_PASSWORD)
            reset = [r for r in hub.requests if r[0] == "/authentication/reset-password"]
            self.assertEqual(reset[0][2], {"oldPassword": "admin", "newPassword": NEW_PASSWORD})
            self.assertEqual(reset[0][1]["Authorization"], "Bearer token-1")

    def test_second_run_finds_the_password_already_set(self):
        with FakeCoreHubServer(password=NEW_PASSWORD, change_required=False) as hub:
            code, out, _ = run_hub_first_login(hub.url)
            self.assertEqual(code, 0)
            self.assertIn("admin password already set", out)
            self.assertFalse([r for r in hub.requests if r[0] == "/authentication/reset-password"])

    def test_default_password_that_is_not_the_requested_one_fails(self):
        with FakeCoreHubServer(password="someone-elses", change_required=False) as hub:
            code, _, err = run_hub_first_login(hub.url)
            self.assertEqual(code, hub_first_login.EXIT_UNEXPECTED_RESPONSE)
            self.assertIn("unexpected login response 401", err)

    def test_refused_password_change_fails(self):
        with FakeCoreHubServer() as hub:
            hub.refuse_reset = True
            code, out, err = run_hub_first_login(hub.url)
            self.assertEqual(code, hub_first_login.EXIT_UNEXPECTED_RESPONSE)
            self.assertIn("first access completed: 403", out)
            self.assertIn("refused the password change", err)

    def test_hub_that_is_not_there_exits_with_a_clear_error_and_no_traceback(self):
        code, out, err = run_hub_first_login("http://127.0.0.1:1", "--attempts", "2")
        self.assertEqual(code, hub_first_login.EXIT_UNREACHABLE)
        self.assertIn("attempt 1/2: hub not ready yet", out)
        self.assertIn("attempt 2/2: hub not ready yet", out)
        self.assertIn("Core Hub not reachable at http://127.0.0.1:1 after 2 attempt(s)", err)
        self.assertNotIn("Traceback", out + err)

    def test_attempts_and_interval_come_from_the_environment(self):
        code, out, _ = run_hub_first_login(
            "http://127.0.0.1:1", env={"HUB_LOGIN_ATTEMPTS": "3", "HUB_LOGIN_INTERVAL": "0"}
        )
        self.assertEqual(code, hub_first_login.EXIT_UNREACHABLE)
        self.assertIn("attempt 3/3", out)

    def test_waits_for_the_hub_like_the_linux_script(self):
        with FakeCoreHubServer() as hub:
            real_login = hub_first_login._login
            calls = []

            def flaky_login(session, url, password, timeout):
                calls.append(password)
                if len(calls) < 3:
                    raise hub_first_login.requests.ConnectionError("not up yet")
                return real_login(session, url, password, timeout)

            with mock.patch.object(hub_first_login, "_login", flaky_login):
                code, out, _ = run_hub_first_login(hub.url)
            self.assertEqual(code, 0)
            self.assertEqual(out.count("hub not ready yet"), 2)

    def test_missing_password_is_refused_before_any_request(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", {"CORE_HUB_URL": "http://127.0.0.1:1"}, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hub_first_login.main([])
        self.assertEqual(code, hub_first_login.EXIT_UNEXPECTED_RESPONSE)
        self.assertIn("HUB_ADMIN_PASSWORD is not set", err.getvalue())
        self.assertNotIn("attempt", out.getvalue())

    def test_default_password_variable_is_the_fallback(self):
        with FakeCoreHubServer() as hub:
            code, _, _ = run_hub_first_login(hub.url, password="", env={"DEFAULT_PASSWORD": NEW_PASSWORD})
            self.assertEqual(code, 0)
            self.assertEqual(hub.password, NEW_PASSWORD)


class LauncherTests(unittest.TestCase):
    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bootstrapper_exe.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_help_lists_every_command_and_never_loads_main(self):
        sys.modules.pop("main", None)
        for flag in ("--help", "-h", "help"):
            code, out, _ = self.call([flag])
            self.assertEqual(code, 0)
            for command in ("hub-first-login", "create-entities", "self-test", "--pipeline-name", "CORE_HUB_URL"):
                self.assertIn(command, out)
        self.assertNotIn("main", sys.modules, "--help must not run main.py: it logs in to Core Hub when imported")

    def test_version(self):
        code, out, _ = self.call(["--version"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())

    def test_hub_first_login_is_dispatched_with_its_arguments(self):
        with mock.patch.object(hub_first_login, "main", return_value=7) as hub_main:
            self.assertEqual(bootstrapper_exe.main(["hub-first-login", "--attempts", "2"]), 7)
        hub_main.assert_called_once_with(["--attempts", "2"])

    def test_bootstrapper_is_the_default_command_and_receives_its_arguments(self):
        for argv, expected in (([], []), (["--pipeline-name", "p"], ["--pipeline-name", "p"]),
                               (["run", "--pipeline-name", "p"], ["--pipeline-name", "p"])):
            with mock.patch.object(bootstrapper_exe, "run_bootstrapper", return_value=0) as run:
                self.assertEqual(bootstrapper_exe.main(argv), 0)
            run.assert_called_once_with(expected)

    def test_create_entities_runs_create_all_entities_as_a_script(self):
        with mock.patch.object(bootstrapper_exe, "run_module_as_script", return_value=0) as run:
            bootstrapper_exe.main(["create-entities", "--pipeline", "p1"])
        run.assert_called_once_with("create_all_entities", ["--pipeline", "p1"])

    def test_script_modules_get_a_python_style_argv(self):
        seen = {}

        def fake_run_module(module, run_name):
            seen.update(module=module, run_name=run_name, argv=list(sys.argv))

        with mock.patch.object(bootstrapper_exe.runpy, "run_module", fake_run_module), \
                mock.patch.object(sys, "argv", ["gluesync-bootstrapper.exe", "ignored"]):
            bootstrapper_exe.run_bootstrapper(["--pipeline-name", "p"])
        self.assertEqual(seen, {"module": "main", "run_name": "__main__",
                                "argv": ["gluesync-bootstrapper.exe", "--pipeline-name", "p"]})

    def test_main_starts_entity_creation_through_the_exe_when_frozen(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("[sys.executable, 'create-entities']", source)
        self.assertIn("getattr(sys, 'frozen', False)", source)
        self.assertNotRegex(source, r"(?m)^\s+exit\(1\)", "the builtin exit() does not exist in a PyInstaller exe")


def local_import_closure(start):
    """(local modules, third-party top-level imports) reachable from ``start``, lazy imports included."""
    local_names = {path.stem for path in PROJECT_ROOT.glob("*.py")} | {"utils"}
    seen, third_party, todo = {}, set(), [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        base = PROJECT_ROOT.joinpath(*name.split("."))
        path = base.with_suffix(".py") if base.with_suffix(".py").exists() else base / "__init__.py"
        if not path.exists():
            continue
        seen[name] = path
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            else:
                continue
            for imported in names:
                top = imported.split(".")[0]
                if top in sys.stdlib_module_names:
                    continue
                if top in local_names:
                    todo.append(imported)
                else:
                    third_party.add(top)
    return set(seen), third_party


# import name -> the distribution requirements-bootstrapper.txt must list; urllib3 comes with requests
DISTRIBUTIONS = {
    "requests": "requests", "urllib3": "requests", "yaml": "PyYAML", "faker": "faker", "colorama": "colorama",
    "pydantic": "pydantic", "logstash_async": "python-logstash-async",
}
# imported only when USE_SDK is true: the Windows image does not carry the SDK submodule
OPTIONAL_IMPORTS = {"gluesync_sdk"}


class BundleCompletenessTests(unittest.TestCase):
    """What the exe needs is bundled: a new import in the bootstrapper must reach the Windows image too."""

    def closure(self):
        modules, third_party = set(), set()
        for start in ("main", "create_all_entities", "connect_portable", "hub_first_login"):
            found_modules, found_third = local_import_closure(start)
            modules |= found_modules
            third_party |= found_third
        return modules, third_party

    def test_every_local_module_of_the_bootstrapper_is_in_the_self_test_list(self):
        modules, _ = self.closure()
        missing = sorted(modules - set(bootstrapper_exe.BUNDLED_MODULES) - {"main"})
        self.assertEqual(missing, [], "add these to BUNDLED_MODULES in bootstrapper_exe.py")

    def test_every_third_party_import_is_in_requirements_bootstrapper(self):
        _, third_party = self.closure()
        requirements = (PROJECT_ROOT / "requirements-bootstrapper.txt").read_text(encoding="utf-8")
        listed = {re.split(r"[=<>\[; ]", line.strip(), maxsplit=1)[0].lower() for line in requirements.splitlines()
                  if line.strip() and not line.startswith("#")}
        unknown = sorted(third_party - set(DISTRIBUTIONS) - OPTIONAL_IMPORTS)
        self.assertEqual(unknown, [], "map these imports to a distribution in DISTRIBUTIONS and requirements-bootstrapper.txt")
        for name in third_party - OPTIONAL_IMPORTS:
            self.assertIn(DISTRIBUTIONS[name].lower(), listed, f"{name} needs {DISTRIBUTIONS[name]} in requirements-bootstrapper.txt")


class DockerfileParityTests(unittest.TestCase):
    """Dockerfile.windows keeps the environment of the Linux Dockerfile, with C:\\ paths."""

    @staticmethod
    def env_keys(path):
        text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        text = re.sub(r"[\\`]\s*\n", " ", text)
        keys = set()
        for line in text.splitlines():
            if line.startswith("ENV "):
                keys.update(re.findall(r"(?:^ENV |\s)([A-Z_]+)=", line + " "))
        return keys

    def test_windows_image_sets_every_environment_variable_of_the_linux_image(self):
        # empty in the Linux image (a kit sets them) or the SDK, which the Windows image does not carry
        left_to_the_kit = {"SOURCE_TYPE", "TARGET_TYPE", "DEFAULT_PASSWORD", "ADMIN_PASSWORD", "GLUESYNC_LICENSE_FILE", "GLUESYNC_SECURITY_CONFIG"}
        missing = self.env_keys("Dockerfile") - self.env_keys("Dockerfile.windows") - left_to_the_kit
        self.assertEqual(missing, set())

    def test_windows_paths_are_the_linux_ones_behind_the_drive(self):
        text = (PROJECT_ROOT / "Dockerfile.windows").read_text(encoding="utf-8")
        for expected in (r"TABLE_LIST_YAML=C:\opt\config\TABLE_LIST.yaml", r"LOG_DIR=C:\logs", r"UDF_PATH=C:\opt\udfs",
                         r"FILE_CONF_PATH=C:\opt\config\config.json"):
            self.assertIn(expected, text)
        self.assertTrue(text.startswith("# escape=`"))
        self.assertIn('ENTRYPOINT ["C:\\\\opt\\\\python\\\\gluesync-bootstrapper.exe"]', text)


if __name__ == "__main__":
    unittest.main()

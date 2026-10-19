#!/usr/bin/env python3
"""A schema whose entity creation fails fails the whole bootstrapper run.

``main.py`` runs ``create_all_entities.py`` once per schema pair. It used to log the failure
and carry on, then log "PASS - Pipeline ... successfully configured and started" and exit 0,
so a restore whose entities were refused by CoreHub looked fine to the Automator round trip
and to the demo-kits. ``main.py`` runs its pipeline at import time (it logs in to CoreHub), so
these tests load only the functions that run the entity creation from its source.
"""

import ast
import logging
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = ("_run_entity_creation", "_create_schema_entities", "_fail_on_schema_errors")


def load_main_functions():
    """The entity creation functions of main.py, in a namespace whose collaborators are mocks."""
    tree = ast.parse((PROJECT_ROOT / "main.py").read_text(encoding="utf-8"))
    wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assert sorted(n.name for n in wanted) == sorted(FUNCTIONS)
    namespace = {
        "os": mock.Mock(),
        "sys": sys,
        "subprocess": subprocess,
        "logger": logging.getLogger("test-main"),
        "TABLE_LIST_YAML": "/nonexistent/tables-list.yaml",
        "log_success": mock.Mock(),
        "log_failure": mock.Mock(),
        "lockfile_failure": mock.Mock(),
        "write_run_result": mock.Mock(),
    }
    namespace["os"].path.exists.return_value = False
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "main.py", "exec"), namespace)  # noqa: S102
    return namespace


def failing(*_args, **_kwargs):
    raise subprocess.CalledProcessError(1, ["python3", "create_all_entities.py", "--token", "secret-token"])


class SchemaFailureTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.main = load_main_functions()

    def create(self, schema_pairs, run):
        with mock.patch.object(subprocess, "run", side_effect=run):
            return self.main["_create_schema_entities"](schema_pairs, "pipe1", "token", "SQL", "NoSQL")

    def test_every_schema_created_reports_nothing_and_the_run_goes_on(self):
        failed = self.create([("dbo", "dbo"), ("hr", None)], lambda *a, **k: mock.Mock(returncode=0))
        self.assertEqual(failed, [])
        self.assertEqual(self.main["log_success"].call_count, 2)
        self.main["log_failure"].assert_not_called()
        self.main["_fail_on_schema_errors"](failed, "pipe1", "name")  # returns, does not exit
        self.main["write_run_result"].assert_not_called()
        self.main["lockfile_failure"].assert_not_called()

    def test_failed_schema_is_named_and_the_other_schemas_still_run(self):
        calls = []

        def run(cmd, check):
            calls.append(cmd[cmd.index("--source-schema") + 1])
            if calls[-1] == "dbo":
                failing()
            return mock.Mock(returncode=0)

        failed = self.create([("dbo", "target"), ("hr", None)], run)
        self.assertEqual(calls, ["dbo", "hr"])
        self.assertEqual(len(failed), 1)
        self.assertIn("[Schema 1/2]", failed[0])
        self.assertIn("source 'dbo' -> target 'target'", failed[0])

    def test_failure_log_does_not_carry_the_command_line_token(self):
        self.create([("dbo", "dbo")], failing)
        messages = [call.args[1] for call in self.main["log_failure"].call_args_list]
        self.assertEqual(len(messages), 1)
        self.assertIn("Error running entity creation script", messages[0])
        self.assertNotIn("secret-token", messages[0])

    def test_failed_schemas_end_the_run_with_a_failure(self):
        failed = ["[Schema 1/2] source 'dbo' -> target 'dbo'", "[Schema 2/2] source 'hr' -> target 'hr'"]
        with self.assertRaises(SystemExit) as raised:
            self.main["_fail_on_schema_errors"](failed, "pipe1", "kit pipeline")
        self.assertEqual(raised.exception.code, 1)
        message = self.main["log_failure"].call_args.args[1]
        self.assertIn("[Schema 1/2]", message)
        self.assertIn("[Schema 2/2]", message)
        self.assertIn("pipe1", message)
        self.main["write_run_result"].assert_called_once_with("pipe1", "kit pipeline", "failure")
        self.main["lockfile_failure"].assert_called_once()

    def test_main_checks_for_failed_schemas_before_completing_the_pipeline(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        body = source[source.index("def main():"):]
        self.assertLess(body.index("_fail_on_schema_errors("), body.index("_complete_pipeline("))


if __name__ == "__main__":
    unittest.main()

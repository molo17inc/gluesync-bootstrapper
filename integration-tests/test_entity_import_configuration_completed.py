#!/usr/bin/env python3
"""Entity YAML import (Create entities from YAML) takes the pipeline out of Draft on success only. Mocked hub."""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

logging.disable(logging.CRITICAL)
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from automator_app import app  # noqa: E402


class CompleteAfterEntityImportTests(unittest.TestCase):
    def _run(self, result, *, dry_run=False, details=None, get_error=None):
        get = mock.Mock(side_effect=get_error) if get_error else mock.Mock(return_value=details)
        with mock.patch.object(app.corehub, "fetch_core_hub", get), \
                mock.patch.object(app, "_mark_configuration_completed") as mark:
            done = app._complete_pipeline_after_entity_import("p1", result, dry_run=dry_run)
        return done, mark

    def test_success_marks_completed_keeping_name(self):
        done, mark = self._run({"success": True, "successful": 10},
                               details={"name": "EB-AS400-XEPDB2-YAML", "configurationCompleted": False})
        self.assertTrue(done)
        mark.assert_called_once_with("p1", "EB-AS400-XEPDB2-YAML")

    def test_dry_run_does_not_mark(self):
        done, mark = self._run({"success": True, "successful": 10, "dry_run": True}, dry_run=True,
                               details={"name": "n"})
        self.assertFalse(done)
        mark.assert_not_called()

    def test_failure_with_zero_created_does_not_mark(self):
        done, mark = self._run({"success": False, "successful": 0, "failed": 10}, details={"name": "n"})
        self.assertFalse(done)
        mark.assert_not_called()

    def test_already_completed_is_left_alone(self):
        done, mark = self._run({"success": True, "successful": 3},
                               details={"name": "n", "configurationCompleted": True})
        self.assertFalse(done)
        mark.assert_not_called()

    def test_pipeline_read_failure_does_not_fail_import(self):
        done, mark = self._run({"success": True, "successful": 3}, get_error=RuntimeError("404"))
        self.assertFalse(done)
        mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()

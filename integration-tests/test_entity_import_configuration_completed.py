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
    def _run(self, result, *, dry_run=False, details=None, get_error=None, mark_result=True):
        get = mock.Mock(side_effect=get_error) if get_error else mock.Mock(return_value=details)
        with mock.patch.object(app.corehub, "fetch_core_hub", get), \
                mock.patch.object(app, "_mark_configuration_completed", return_value=mark_result) as mark:
            done = app._complete_pipeline_after_entity_import("p1", result, dry_run=dry_run)
        return done, mark

    def test_success_marks_completed_keeping_name_and_description(self):
        done, mark = self._run({"success": True, "successful": 10},
                               details={"name": "EB-AS400-XEPDB2-YAML", "description": "Keep me",
                                        "configurationCompleted": False})
        self.assertTrue(done)
        mark.assert_called_once_with("p1", "EB-AS400-XEPDB2-YAML", description="Keep me")

    def test_failed_put_is_reported_as_not_done(self):
        done, mark = self._run({"success": True, "successful": 3},
                               details={"name": "n", "description": "d"}, mark_result=False)
        self.assertFalse(done)
        mark.assert_called_once_with("p1", "n", description="d")

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


class MarkConfigurationCompletedTests(unittest.TestCase):
    def _put_body(self, **kwargs):
        with mock.patch.object(app.corehub, "fetch_core_hub") as fetch:
            ok = app._mark_configuration_completed("p1", "My pipeline", **kwargs)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, ("/pipelines/p1",))
        self.assertEqual(fetch.call_args.kwargs["method"], "PUT")
        return ok, fetch.call_args.kwargs["body"]

    def test_put_body_carries_name_and_description_when_given(self):
        ok, body = self._put_body(description="Existing text")
        self.assertTrue(ok)
        self.assertEqual(body["name"], "My pipeline")
        self.assertEqual(body["description"], "Existing text")

    def test_put_body_has_no_description_key_when_not_given(self):
        ok, body = self._put_body()
        self.assertTrue(ok)
        self.assertEqual(body["name"], "My pipeline")
        self.assertNotIn("description", body)

    def test_returns_false_when_put_raises(self):
        with mock.patch.object(app.corehub, "fetch_core_hub", side_effect=RuntimeError("boom")):
            self.assertFalse(app._mark_configuration_completed("p1", "n"))


if __name__ == "__main__":
    unittest.main()

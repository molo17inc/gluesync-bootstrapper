#!/usr/bin/env python3
"""Regression tests for the Automator smoke checker helper."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.automator_smoke_check import _healthz_body_is_ok


class AutomatorSmokeCheckTests(unittest.TestCase):
    def test_accepts_compact_json_payload(self):
        self.assertTrue(_healthz_body_is_ok('{"status":"ok"}'))

    def test_rejects_non_json_or_wrong_status(self):
        self.assertFalse(_healthz_body_is_ok("ok"))
        self.assertFalse(_healthz_body_is_ok('{"status":"error"}'))


if __name__ == "__main__":
    unittest.main()

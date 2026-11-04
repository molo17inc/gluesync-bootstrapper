"""The pure helpers of automator_app/static/gs1_import.js, run under node."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "automator_app" / "static" / "gs1_import.js"

SCRIPT = r"""
import { issueText, reportRows, defaultSelection, confirmationText } from './gs1_import.mjs';

function assert(condition, message) {
  if (!condition) { console.error('FAIL: ' + message); process.exit(1); }
}

const plan = {
  drafts: ['ORDERS', 'LINES', 'UNCHECKED', 'BLIND'],
  perEntity: [
    { gs1Name: 'ORDERS', bucket: 'FLAT', issues: [] },
    { gs1Name: 'LINES', bucket: 'MULTI_LEVEL', issues: [] },
    { gs1Name: 'UNCHECKED', bucket: 'FLAT', issues: [] },
    { gs1Name: 'BLIND', bucket: 'FLAT', issues: [] },
    { gs1Name: 'RECENT', bucket: 'UNSUPPORTED', issues: [
      { code: 'NON_DETERMINISTIC_QUERY', severity: 'ERROR', level: 'RECENT', location: '1:45', message: 'GETDATE' },
    ] },
  ],
  checks: {
    ORDERS: { describe: { verdict: 'CDC', proposedKeys: { ORDERS: ['ID'] } }, validate: { valid: true, issues: [] } },
    LINES: { validate: { valid: false, issues: [{ code: 'X', severity: 'ERROR', message: 'refused' }] } },
    UNCHECKED: { error: 'Core Hub unreachable' },
    RECENT: { error: 'Core Hub down' },
  },
};

const rows = reportRows(plan);
assert(rows.length === 5, 'one row per GS1 entity');
assert(rows[0].hasDraft && rows[0].verdict === 'CDC' && rows[0].proposedKeys.ORDERS[0] === 'ID', 'describe shown');
assert(rows[1].valid === false && rows[1].validationIssues.length === 1, 'validation shown');
assert(rows[2].checkError === 'Core Hub unreachable' && rows[2].valid === null, 'a check error is shown');
assert(!rows[4].hasDraft && rows[4].checkError === 'Core Hub down', 'no draft for an unsupported entity');
assert(rows.every((row) => row.structureChecked), 'checked by default');
assert(JSON.stringify(defaultSelection(rows)) === JSON.stringify(['ORDERS']),
  'only the drafts Core Hub checked and found valid are preselected: ' + JSON.stringify(defaultSelection(rows)));
const offlineRows = reportRows({ ...plan, structureChecked: false, checks: {} });
assert(offlineRows.every((row) => !row.structureChecked), 'offline rows say they were not checked');
assert(JSON.stringify(defaultSelection(offlineRows)) === JSON.stringify(['ORDERS', 'LINES', 'UNCHECKED', 'BLIND']),
  'offline: every draft is preselected, as before');
assert(issueText(plan.perEntity[4].issues[0]) === 'ERROR NON_DETERMINISTIC_QUERY [RECENT/1:45]: GETDATE', issueText(plan.perEntity[4].issues[0]));
assert(issueText({ code: 'C', severity: 'WARNING', message: 'm' }) === 'WARNING C: m', 'no location');
assert(confirmationText(['ORDERS'], 'Pipe (p1)').startsWith('Save 1 entities (ORDERS) to pipeline Pipe (p1)?'), 'prompt');
assert(reportRows({}).length === 0, 'empty plan');
console.log('ok');
"""


# In CI (CI is set) a missing node fails the test instead of skipping it: the check must never silently not run.
@unittest.skipUnless(shutil.which("node") or os.environ.get("CI"), "node not installed")
class Gs1ImportJsTests(unittest.TestCase):
    def test_report_rows_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "gs1_import.mjs").write_text(JS.read_text())
            (Path(tmp) / "t.mjs").write_text(SCRIPT)
            proc = subprocess.run(
                ["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()

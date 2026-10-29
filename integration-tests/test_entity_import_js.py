"""Entity Customize UI: buildEntityPlan under Node; skipped where Node is not installed."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "automator_app" / "static" / "entity_import.js"

SCRIPT = r"""
import { buildEntityPlan, planIsEmpty } from './entity_import.mjs';

// Minimal DOM for buildEntityPlan (no full document)
class FakeEl {
  constructor(attrs = {}) {
    this.dataset = attrs.dataset || {};
    this.value = attrs.value ?? '';
    this.checked = attrs.checked ?? false;
    this._kids = {};
  }
  querySelector(sel) {
    return this._kids[sel] || null;
  }
}

const panel = new FakeEl();
panel._kids['[data-on-existing]'] = { value: 'skip' };
panel._kids['[data-part="udfs"]'] = { checked: false };
panel._kids['[data-part="schedules"]'] = { checked: true };
panel._kids['[data-part="functionCollection"]'] = { checked: true };
panel._kids['[data-part="groups"]'] = { checked: true };
panel._kids['[data-part="createTables"]'] = { checked: false };
panel._kids['[data-include]'] = { value: 'SCH.T1\nSCH.T2' };
panel._kids['[data-exclude]'] = { value: 'SCH.T2' };

const plan = buildEntityPlan(panel, 'pipe-1');
const assert = (c, m) => { if (!c) { console.error(m); process.exit(1); } };
assert(plan.version === 1, 'version');
assert(plan.defaults.onExistingEntity === 'skip', 'onExisting');
assert(plan.pipelines['pipe-1'].parts.udfs === false, 'udfs off');
assert(plan.pipelines['pipe-1'].parts.createTables === false, 'createTables off');
assert(!('schedules' in (plan.pipelines['pipe-1'].parts || {})), 'schedules default omitted');
assert(JSON.stringify(plan.pipelines['pipe-1'].entities.include) === JSON.stringify(['SCH.T1','SCH.T2']), 'include');
assert(JSON.stringify(plan.pipelines['pipe-1'].entities.exclude) === JSON.stringify(['SCH.T2']), 'exclude');

const emptyPanel = new FakeEl();
emptyPanel._kids['[data-on-existing]'] = { value: '' };
const PARTS_KEYS = ['udfs','functionCollection','schedules','groups','createTables'];
PARTS_KEYS.forEach((k) => { emptyPanel._kids[`[data-part="${k}"]`] = { checked: true }; });
emptyPanel._kids['[data-include]'] = { value: '' };
emptyPanel._kids['[data-exclude]'] = { value: '' };
const empty = buildEntityPlan(emptyPanel, 'pipe-1');
assert(Object.keys(empty.defaults).length === 0, 'empty defaults');
assert(Object.keys(empty.pipelines).length === 0, 'empty pipelines');
assert(planIsEmpty(empty), 'planIsEmpty empty');
assert(!planIsEmpty(plan), 'planIsEmpty filled');
console.log('ok');
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class EntityImportJsTests(unittest.TestCase):
    def test_build_entity_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "entity_import.mjs").write_text(JS.read_text())
            (Path(tmp) / "t.mjs").write_text(SCRIPT)
            proc = subprocess.run(
                ["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()

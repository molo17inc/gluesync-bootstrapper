"""Entity Customize UI: buildEntityPlan / selectionError under Node; skipped where Node is not installed."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "automator_app" / "static" / "entity_import.js"

SCRIPT = r"""
import { buildEntityPlan, planIsEmpty, selectionError, resolveTarget, targetDiffersFromDefault } from './entity_import.mjs';

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

function basePanel({ include = '', exclude = '', onExisting = 'skip' } = {}) {
  const panel = new FakeEl();
  panel._kids['[data-on-existing]'] = { value: onExisting };
  panel._kids['[data-part="udfs"]'] = { checked: false };
  panel._kids['[data-part="schedules"]'] = { checked: true };
  panel._kids['[data-part="functionCollection"]'] = { checked: true };
  panel._kids['[data-part="groups"]'] = { checked: true };
  panel._kids['[data-part="createTables"]'] = { checked: false };
  panel._kids['[data-include]'] = { value: include };
  panel._kids['[data-exclude]'] = { value: exclude };
  return panel;
}

const assert = (c, m) => { if (!c) { throw new Error(m); } };

const loaded = [
  { id: 'SCH.T1', schema: 'SCH', targetSchema: 'TGT', targetTable: 'T1' },
  { id: 'SCH.T2', schema: 'SCH', targetSchema: 'TGT', targetTable: 'T2' },
  { id: 'SCH.T3', schema: 'SCH', targetSchema: 'TGT', targetTable: 'T3' },
];

// Advanced free-text still works
const advanced = basePanel({ include: 'SCH.T1\nSCH.T2', exclude: 'SCH.T2' });
const planAdv = buildEntityPlan(advanced, 'pipe-1', loaded, new Set(loaded.map((e) => e.id)));
assert(JSON.stringify(planAdv.pipelines['pipe-1'].entities.include) === JSON.stringify(['SCH.T1','SCH.T2']), 'adv include');
assert(JSON.stringify(planAdv.pipelines['pipe-1'].entities.exclude) === JSON.stringify(['SCH.T2']), 'adv exclude');

// All checked → omit include
const allPanel = basePanel();
const planAll = buildEntityPlan(allPanel, 'pipe-1', loaded, new Set(['SCH.T1','SCH.T2','SCH.T3']));
assert(!planAll.pipelines['pipe-1']?.entities?.include, 'all omit include');
assert(planAll.pipelines['pipe-1'].parts.udfs === false, 'udfs off');

// Partial picker selection → exact ids
const partPanel = basePanel();
const planPart = buildEntityPlan(partPanel, 'pipe-1', loaded, new Set(['SCH.T1','SCH.T3']));
assert(JSON.stringify(planPart.pipelines['pipe-1'].entities.include) === JSON.stringify(['SCH.T1','SCH.T3']), 'partial include');

// None checked → selectionError
const nonePanel = basePanel();
assert(selectionError(nonePanel, loaded, new Set()) !== null, 'none error');
assert(selectionError(allPanel, loaded, new Set(['SCH.T1','SCH.T2','SCH.T3'])) === null, 'all ok');
// Advanced overrides none
assert(selectionError(advanced, loaded, new Set()) === null, 'advanced skips picker error');

// Empty panel / no loaded → empty plan
const emptyPanel = basePanel({ onExisting: '' });
['udfs','functionCollection','schedules','groups','createTables'].forEach((k) => {
  emptyPanel._kids[`[data-part="${k}"]`] = { checked: true };
});
const empty = buildEntityPlan(emptyPanel, 'pipe-1', [], null);
assert(Object.keys(empty.defaults).length === 0, 'empty defaults');
assert(Object.keys(empty.pipelines).length === 0, 'empty pipelines');
assert(planIsEmpty(empty), 'planIsEmpty empty');
assert(!planIsEmpty(planAdv), 'planIsEmpty filled');

// Target overrides emit when renamed
const ov = new Map([
  ['SCH.T1', { mode: 'new', targetSchema: 'TGT', targetTable: 'T1_RENAMED' }],
  ['SCH.T2', { mode: 'existing', targetSchema: 'TGT', targetTable: 'EXISTING_T2' }],
]);
const planOv = buildEntityPlan(allPanel, 'pipe-1', loaded, new Set(['SCH.T1','SCH.T2','SCH.T3']), ov);
assert(planOv.pipelines['pipe-1'].entities.overrides['SCH.T1'].targetTable === 'T1_RENAMED', 'rename override');
assert(planOv.pipelines['pipe-1'].entities.overrides['SCH.T2'].targetTable === 'EXISTING_T2', 'existing override');
assert(!planOv.pipelines['pipe-1'].entities.overrides['SCH.T3'], 'default omits override');

// Empty target table → error
const emptyTarget = new Map([['SCH.T1', { mode: 'new', targetSchema: 'TGT', targetTable: '' }]]);
assert(selectionError(allPanel, loaded, new Set(['SCH.T1']), emptyTarget) !== null, 'empty target error');

// Duplicate targets → error
const dup = new Map([
  ['SCH.T1', { mode: 'new', targetSchema: 'TGT', targetTable: 'SAME' }],
  ['SCH.T2', { mode: 'new', targetSchema: 'TGT', targetTable: 'SAME' }],
]);
assert(selectionError(allPanel, loaded, new Set(['SCH.T1','SCH.T2']), dup) !== null, 'dup target error');

// resolveTarget / differs helpers
const r = resolveTarget(loaded[0], ov);
assert(r.targetTable === 'T1_RENAMED', 'resolve rename');
assert(targetDiffersFromDefault(loaded[0], r), 'differs');
assert(!targetDiffersFromDefault(loaded[2], resolveTarget(loaded[2], ov)), 'same default');

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

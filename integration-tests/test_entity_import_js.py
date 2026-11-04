"""Entity Customize UI: buildEntityPlan / selectionError under Node; skipped where Node is not installed."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "automator_app" / "static" / "entity_import.js"

SCRIPT = r"""
import {
  buildEntityPlan, planIsEmpty, selectionError, resolveTarget, targetDiffersFromDefault,
  columnMismatchNote, inferTargetMode, entityIdSubtitle, groupPickerRows,
} from './entity_import.mjs';

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

// inferTargetMode: typed name vs discovery list
assert(inferTargetMode('cust_primary', ['cust_primary', 'other']) === 'existing', 'infer existing');
assert(inferTargetMode('CUST_PRIMARY', ['cust_primary']) === 'existing', 'infer casefold');
assert(inferTargetMode('brand_new_table', ['cust_primary']) === 'new', 'infer new');
assert(inferTargetMode('', ['cust_primary']) === 'new', 'infer empty');

// columnMismatchNote
assert(columnMismatchNote(null, ['a']) === 'Column check unverified (discovery unavailable).', 'unverif null');
assert(columnMismatchNote([], []) === 'Column check unverified (no columns returned).', 'unverif empty');
assert(columnMismatchNote(['ID', 'NAME'], ['ID', 'NAME']) === '', 'match ok');
const mm = columnMismatchNote(['ID', 'NAME', 'EMAIL'], ['ID', 'OTHER']);
assert(mm.includes('Column mismatch'), 'mismatch prefix: ' + mm);
assert(mm.includes('missing on target'), 'missing: ' + mm);
assert(mm.includes('extra on target'), 'extra: ' + mm);

// entityIdSubtitle: only when id differs from source (e.g. @@2); do not duplicate Source column
assert(entityIdSubtitle({ id: 'dbo.CUSTOMERS@@2', source: 'dbo.CUSTOMERS', entityName: 'Cust' }) === 'dbo.CUSTOMERS@@2', '@@2 subtitle');
assert(entityIdSubtitle({ id: 'dbo.ORDERS', source: 'dbo.ORDERS', entityName: 'ORDERS' }) === '', 'same as source no subtitle');
assert(entityIdSubtitle({ id: 'dbo.ORDERS', source: 'dbo.ORDERS', entityName: 'OrdersHeaders' }) === '', 'name differs but source matches — no subtitle');
assert(entityIdSubtitle({ id: 'dbo.CUSTOMERS@@2', source: 'dbo.CUSTOMERS@@2', entityName: 'Cust' }) === '', 'id equals source hides');

// groupPickerRows: MultiTable chain collapses to one block
const chained = [
  { id: 'dbo.H', schema: 'dbo', chainId: 'multitable_ORDERS', entityName: 'OrdersChain' },
  { id: 'dbo.I', schema: 'dbo', chainId: 'multitable_ORDERS', entityName: 'OrdersChain' },
  { id: 'dbo.C', schema: 'dbo', entityName: 'Cust' },
];
const grouped = groupPickerRows(chained);
assert(grouped[0].kind === 'schema' && grouped[0].schema === 'dbo', 'schema header');
assert(grouped[1].kind === 'chain' && grouped[1].members.length === 2, 'chain block');
assert(grouped[2].kind === 'entity' && grouped[2].entity.id === 'dbo.C', 'single after chain');

// UI emits exact ids including @@2 — override keys must be exact, not bare
const loadedAt = [
  { id: 'dbo.CUSTOMERS', schema: 'dbo', source: 'dbo.CUSTOMERS', targetSchema: 'gb75_tgt', targetTable: 'cust_primary', entityName: 'CustPrimary' },
  { id: 'dbo.CUSTOMERS@@2', schema: 'dbo', source: 'dbo.CUSTOMERS', targetSchema: 'gb75_tgt', targetTable: 'cust_secondary', entityName: 'CustSecondary' },
];
const ovAt = new Map([
  ['dbo.CUSTOMERS@@2', { mode: 'existing', targetSchema: 'gb75_tgt', targetTable: 'cust_primary' }],
]);
const planAt = buildEntityPlan(allPanel, 'pipe1', loadedAt, new Set(['dbo.CUSTOMERS@@2']), ovAt);
const keysAt = Object.keys(planAt.pipelines.pipe1.entities.overrides || {});
assert(keysAt.length === 1 && keysAt[0] === 'dbo.CUSTOMERS@@2', 'exact @@2 override key: ' + keysAt);
assert(planAt.pipelines.pipe1.entities.overrides['dbo.CUSTOMERS@@2'].targetTable === 'cust_primary', '@@2 target');
assert(selectionError(allPanel, loadedAt, new Set(['dbo.CUSTOMERS', 'dbo.CUSTOMERS@@2']), new Map([
  ['dbo.CUSTOMERS', { mode: 'existing', targetSchema: 'gb75_tgt', targetTable: 'same' }],
  ['dbo.CUSTOMERS@@2', { mode: 'existing', targetSchema: 'gb75_tgt', targetTable: 'same' }],
])) !== null, 'dup exact ids blocked in UI');

// Existing pick must surface as mode=existing in resolveTarget (preview action uses this)
const existingResolved = resolveTarget(
  { id: 'dbo.ORDERS_HEADERS', targetSchema: 'gb75_tgt', targetTable: 'ORDERS_HEADERS' },
  new Map([['dbo.ORDERS_HEADERS', { mode: 'existing', targetSchema: 'gb75_tgt', targetTable: 'cust_primary' }]]),
);
assert(existingResolved.mode === 'existing', 'existing mode preserved');
assert(existingResolved.targetTable === 'cust_primary', 'existing table preserved');


// --- Regression: duplicate target + skip (Customize Preview/Import) ---
// Two sources, same table name → same default target. Both new → error.
const crossSchema = [
  { id: 'SRC_A.ORDERS', schema: 'SRC_A', source: 'SRC_A.ORDERS', targetSchema: 'TGT', targetTable: 'ORDERS', alreadyExists: false },
  { id: 'SRC_B.ORDERS', schema: 'SRC_B', source: 'SRC_B.ORDERS', targetSchema: 'TGT', targetTable: 'ORDERS', alreadyExists: false },
];
const skipPanel = basePanel({ onExisting: 'skip' });
assert(selectionError(skipPanel, crossSchema, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map()) !== null,
  'two new entities same target must error');

// One already exists + skip → only the new one is created → no error
const oneExists = [
  { id: 'SRC_A.ORDERS', schema: 'SRC_A', source: 'SRC_A.ORDERS', targetSchema: 'TGT', targetTable: 'ORDERS', alreadyExists: true },
  { id: 'SRC_B.ORDERS', schema: 'SRC_B', source: 'SRC_B.ORDERS', targetSchema: 'TGT', targetTable: 'ORDERS', alreadyExists: false },
];
assert(selectionError(skipPanel, oneExists, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map()) === null,
  'skip alreadyExists removes it from duplicate check');

// Both already exist + skip → nothing created → no error
const bothExist = oneExists.map((e) => ({ ...e, alreadyExists: true }));
assert(selectionError(skipPanel, bothExist, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map()) === null,
  'skip both existing → no duplicate error');

// Unchecked (excluded) entity does not count toward collision
assert(selectionError(skipPanel, crossSchema, new Set(['SRC_A.ORDERS']), new Map()) === null,
  'unchecked peer is not a collision');

// Per-entity rename resolves the collision
const renamed = new Map([
  ['SRC_B.ORDERS', { mode: 'new', targetSchema: 'TGT', targetTable: 'ORDERS_B' }],
]);
assert(selectionError(skipPanel, crossSchema, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), renamed) === null,
  'rename via per-entity targetTable clears duplicate');

// fail / empty onExisting still errors when both would be created
const failPanel = basePanel({ onExisting: 'fail' });
assert(selectionError(failPanel, oneExists, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map()) !== null,
  'fail policy still counts alreadyExists toward duplicate');

// --- Regression: global Customize target schema propagates ---
const globalTgt = { targetSchema: 'GLUESYNC' };
const rGlobal = resolveTarget(crossSchema[0], new Map(), globalTgt);
assert(rGlobal.targetSchema === 'GLUESYNC', 'global target schema applies: ' + rGlobal.targetSchema);
assert(rGlobal.targetTable === 'ORDERS', 'global leaves YAML table');

// Per-entity explicit schema wins over global
const rExplicit = resolveTarget(
  crossSchema[0],
  new Map([['SRC_A.ORDERS', { targetSchema: 'OTHER', targetTable: 'ORDERS' }]]),
  globalTgt,
);
assert(rExplicit.targetSchema === 'OTHER', 'per-entity schema beats global');

// Mode-only override (discovery) must NOT lock schema — global still applies
const rModeOnly = resolveTarget(
  crossSchema[0],
  new Map([['SRC_A.ORDERS', { mode: 'existing' }]]),
  globalTgt,
);
assert(rModeOnly.targetSchema === 'GLUESYNC', 'mode-only override keeps global schema');
assert(rModeOnly.mode === 'existing', 'mode-only preserves mode');

// buildEntityPlan emits global target as overrides
const planGlobal = buildEntityPlan(
  skipPanel, 'pipe-1', crossSchema, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map(), globalTgt,
);
assert(planGlobal.pipelines['pipe-1'].entities.overrides['SRC_A.ORDERS'].targetSchema === 'GLUESYNC',
  'plan emits global target for SRC_A');
assert(planGlobal.pipelines['pipe-1'].entities.overrides['SRC_B.ORDERS'].targetSchema === 'GLUESYNC',
  'plan emits global target for SRC_B');

// selectionError with global schema still detects collision under remapped schema
assert(selectionError(skipPanel, crossSchema, new Set(['SRC_A.ORDERS', 'SRC_B.ORDERS']), new Map(), globalTgt) !== null,
  'global schema remap still flags duplicate table names');

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

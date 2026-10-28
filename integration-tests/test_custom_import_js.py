"""Review #3: the custom import page's preview gate (Select none/all invalidates, stale previews ignored).

Runs the gate of automator_app/static/custom_import.js under Node; skipped where Node is not installed."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "automator_app" / "static" / "custom_import.js"

SCRIPT = r"""
import { createPreviewGate } from './custom_import.mjs';
const assert = (c, m) => { if (!c) { console.error(m); process.exit(1); } };
const g = createPreviewGate();
// an OK preview of the plan on screen enables the import
let t = g.start('A');
assert(g.accept(t, 'A', true) && g.canImport('A'), 'ok preview');
assert(!g.canImport('B'), 'another plan');
// Select none / all (or any edit) invalidates
g.invalidate();
assert(!g.canImport('A'), 'invalidated');
// a stale response (an edit after the request) is ignored
t = g.start('A'); g.invalidate();
assert(!g.accept(t, 'A', true) && !g.canImport('A'), 'stale after edit');
// out of order: the first request answers after the second
const t1 = g.start('A'); const t2 = g.start('B');
assert(!g.accept(t1, 'B', true), 'older ticket ignored');
assert(g.accept(t2, 'B', false) && !g.canImport('B'), 'failed preview does not enable');
// the plan changed without an event: the key no longer matches
t = g.start('A');
assert(!g.accept(t, 'C', true), 'plan changed');
console.log('ok');
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PreviewGateTests(unittest.TestCase):
    def test_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "custom_import.mjs").write_text(JS.read_text())
            (Path(tmp) / "t.mjs").write_text(SCRIPT)
            proc = subprocess.run(["node", "t.mjs"], cwd=tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_select_all_dispatches_change(self):
        text = JS.read_text()
        toggle = text[text.index("toggleAll.addEventListener"):]
        toggle = toggle[:toggle.index("const agents")]
        self.assertIn("dispatchEvent(new Event('change', { bubbles: true }))", toggle)


if __name__ == "__main__":
    unittest.main()

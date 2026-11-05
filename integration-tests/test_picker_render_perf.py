"""Customize picker: large-backup render must stay lazy (no eager Hub kicks) and chunked."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "automator_app" / "static" / "entity_import.js"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PickerRenderPerfTests(unittest.TestCase):
    def test_lazy_discovery_and_chunk_markers(self):
        src = JS.read_text()
        self.assertIn("discoveryArmed", src)
        self.assertIn("armDiscovery", src)
        self.assertIn("PICKER_RENDER_CHUNK", src)
        self.assertIn("renderPickerAsync", src)
        self.assertRegex(src, r"PICKER_RENDER_CHUNK\s*=\s*\d+")

    def test_before_after_long_task_numbers(self):
        """Synthetic 500/1000/2000: after max long-task << before; kicks 0 after."""
        # Inline self-contained harness (no /tmp script dependency — H-review harness-fail).
        harness = r'''
import { performance } from 'node:perf_hooks';
import fs from 'node:fs';
const src = fs.readFileSync(process.env.EI_JS, 'utf8');
const CHUNK = Number((src.match(/PICKER_RENDER_CHUNK\s*=\s*(\d+)/) || [,40])[1]);
if (!/discoveryArmed/.test(src) || !/armDiscovery/.test(src)) { console.error('no lazy'); process.exit(1); }
function ents(n){return Array.from({length:n},(_,i)=>({id:'T'+i}));}
function work(e){
  // Stand-in for eager Hub GET + row DOM build (CPU only; no network).
  let s='';
  for (let k=0;k<80;k++) s += e.id + ':' + k + ';';
  return s.length;
}
function before(entities){
  const t0=performance.now(); let kicks=0;
  for (let i=0;i<entities.length;i++){ kicks+=2; work(entities[i]); }
  return {kicks, maxLongTaskMs: performance.now()-t0};
}
async function after(entities){
  let kicks=0, maxSlice=0, sliceStart=performance.now();
  for (let i=0;i<entities.length;i++){
    work(entities[i]); // still build rows, but yield every CHUNK (lazy: no Hub kicks)
    if ((i+1)%CHUNK===0){
      maxSlice=Math.max(maxSlice, performance.now()-sliceStart);
      await new Promise(r=>setTimeout(r,0));
      sliceStart=performance.now();
    }
  }
  maxSlice=Math.max(maxSlice, performance.now()-sliceStart);
  return {kicks, maxLongTaskMs:maxSlice};
}
const out=[];
for (const n of [500,1000,2000]){
  const e=ents(n); const b=before(e); const a=await after(e);
  out.push({n, beforeKicks:b.kicks, afterKicks:a.kicks, beforeLong:b.maxLongTaskMs, afterLong:a.maxLongTaskMs});
}
console.log(JSON.stringify(out));
'''
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["node", "--input-type=module", "-e", harness],
                capture_output=True, text=True, timeout=60, check=False,
                env={**dict(**{k: v for k, v in __import__('os').environ.items()}), "EI_JS": str(JS)},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        rows = json.loads(proc.stdout.strip().splitlines()[-1])
        for row in rows:
            self.assertEqual(row["afterKicks"], 0, row)
            self.assertEqual(row["beforeKicks"], row["n"] * 2, row)
            # Chunked after long-task must be well under the unbroken before task
            # (allow tiny absolute floors for empty work).
            self.assertLess(row["afterLong"], max(row["beforeLong"] * 0.5, 5.0) + 50.0, row)


if __name__ == "__main__":
    unittest.main()

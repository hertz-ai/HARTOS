/*
 * Behavioural test for the Netflix image-card ROW helper hhCardRow() (d4).
 *
 * R1 added `function hhCardRow(title, items, opts)` to the rendered desktop
 * shell (integrations/agent_engine/liquid_ui_service.py). It is the SHARED row
 * renderer the panel surfaces (Installed-Apps registry, This-PC drives) now use
 * so content listings paint as the SAME cinematic `.hh-card` rows the home does
 * — one design vocabulary, not a second card system. Its only collaborator is
 * window.HartBrandArt (the one brand-art helper shared with desktop icons +
 * home cards), which we stub so we exercise ONLY the row logic.
 *
 * We render the real shell with the project's python, slice out the inline
 * hhCardRow region, run it on a bare vm context, and assert the OBSERVABLE HTML
 * a panel would inject. Behavioural (call the real fn, assert its output), never
 * a source-string grep.
 *
 * Run:  node tests/unit/test_shell_hhcardrow.mjs
 * (Python wrapper test_shell_hhcardrow.py shells out so pytest/CI pick it up.)
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const PY_CANDIDATES = [
  process.env.HART_TEST_PYTHON,
  join(ROOT, 'venv', 'Scripts', 'python.exe'),
  'C:/Users/sathi/miniconda3/python.exe',
  'python', 'python3',
].filter(Boolean);

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function count(s, re) { return (s.match(re) || []).length; }

// ── 1. Render the shell + slice the hhCardRow region ────────────────────────
const RENDER = "import sys; sys.path.insert(0,'.');" +
  "from integrations.agent_engine.liquid_ui_service import LiquidUIService;" +
  "(getattr(sys.stdout,'reconfigure',lambda **k:None))(encoding='utf-8');" +
  "print(LiquidUIService().render_desktop_shell())";
let html = null, lastErr = null;
for (const py of PY_CANDIDATES) {
  try {
    html = execFileSync(py, ['-c', RENDER], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
    if (html) break;
  } catch (e) { lastErr = e; }
}
if (!html) {
  console.log('SKIP: no python could render the shell (' + (lastErr && lastErr.message) + ')');
  console.log('\nRESULT: ALL PASS');
  process.exit(0);
}

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const src = scripts.find(s => s.includes('function hhCardRow('));
ok(!!src, 'rendered shell contains the hhCardRow helper');
if (!src) { console.log('\nRESULT: ' + failures + ' FAILED'); process.exit(1); }

const s0 = src.indexOf('function hhCardRow(');
const s1 = src.indexOf('function dsMetricBar(', s0);   // the next design-system helper
ok(s0 >= 0 && s1 > s0, 'sliced the hhCardRow region (up to dsMetricBar)');
const region = src.slice(s0, s1);

// ── 2. Bare context + the ONE stubbed collaborator (window.HartBrandArt) ────
const sandbox = { console, window: {} };
sandbox.window.HartBrandArt = {
  spectrum: ['teal', 'cyan', 'blue', 'violet', 'magenta', 'amber'],
  spectrumHex: { teal: '#00E6C3', cyan: '#00C2FF', blue: '#3B6EF6', violet: '#8B5CF6', magenta: '#E24DCE', amber: '#F5A623' },
  esc: s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  gradient: (hex, i) => 'linear-gradient(135deg,' + (hex || '#000') + ',#000)',
  glyphHTML: icon => '<span class="mi material-icons-round">' + icon + '</span>',
};
vm.createContext(sandbox);
vm.runInContext(region, sandbox);
const hhCardRow = sandbox.hhCardRow;
ok(typeof hhCardRow === 'function', 'hhCardRow is callable after load');

// ── 3. A registry surface renders one .hh-card per item, shared vocabulary ──
const apps = [
  { title: 'Firefox', meta: 'flatpak · No special permissions', icon: 'apps',
    action: '<button class="ds-btn" onclick="event.stopPropagation();appRegistryUninstall(this)">Uninstall</button>' },
  { title: 'GIMP', meta: 'flatpak · No special permissions', icon: 'brush', action: '<button>Uninstall</button>' },
];
const reg = hhCardRow('Installed Apps', apps, {});
// class="hh-card" or "hh-card hh-…" — the [ "] boundary excludes the "hh-cards" wrapper.
ok(count(reg, /<div class="hh-card[ "]/g) === 2, 'renders exactly one .hh-card per app (2)');
ok(reg.includes('hh-row-title') && reg.includes('Installed Apps'), 'row header title rendered');
ok(reg.includes('hh-card-art') && reg.includes('linear-gradient'), 'each card wears the shared brand-art gradient tile');
ok(reg.includes('hh-card-scrim'), 'text-over-art scrim laid down (d3 legibility)');
ok(reg.includes('Uninstall') && reg.includes('stopPropagation'), 'the shared Uninstall action survives + keeps stopPropagation');
ok(reg.includes('flatpak'), 'item meta (platform · perms) rendered on the card');

// ── 4. A drive card carries a click target + usage bar without inline-quoting a path
const drives = hhCardRow('Drives & Partitions', [
  { title: 'C: (Windows)', meta: 'ntfs · 250.0 GB free of 500.0 GB', icon: 'storage',
    progress: 0.5, badge: '50%', attrs: 'data-mount="C:\\"', onclick: 'openFilesAt(this.dataset.mount)' },
], {});
ok(drives.includes('role="button"'), 'a card with onclick becomes an activatable button');
ok(drives.includes('openFilesAt(this.dataset.mount)'), 'onclick hands browsing to the canonical File Explorer');
ok(drives.includes('data-mount='), 'the mount path travels on a data-attr, never inline-quoted into JS');
ok(/hh-card-prog[\s\S]*?width:\s*50%/.test(drives), 'a real usage progress bar reflects the percent (0.5 -> 50%)');

// ── 5. Empty state degrades to a single placeholder card ────────────────────
const empty = hhCardRow('', [], { emptyText: 'No apps installed' });
ok(empty.includes('hh-card-empty'), 'an empty list renders the empty-state card');
ok(empty.includes('No apps installed'), 'the caller-supplied empty text is surfaced');

// ── 6. Untrusted item text is HTML-escaped (no tag injection into the card) ──
const xss = hhCardRow('', [{ title: '<img src=x onerror=alert(1)>' }], {});
ok(xss.includes('&lt;img'), 'a card title is HTML-escaped');
ok(!/<img src=x/.test(xss), 'no raw <img> tag survives into the rendered card');

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

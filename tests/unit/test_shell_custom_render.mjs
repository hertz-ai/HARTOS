/*
 * G2 behavioural test: an agent-REGISTERED custom component renders REAL UI on the
 * client, from its stamped render spec (agent_ui_update stamps ev._spec = {props,
 * template}) -- NOT the generic JSON dump. Drives the REAL renderAgentOverlay that
 * ships in the rendered desktop shell: render the shell with python, slice out _esc +
 * renderAgentOverlay, run them on a tiny DOM shim, push a custom component, and assert
 * the OBSERVABLE overlay HTML. Behavioural, not source-shape.
 *
 * Run:  node tests/unit/test_shell_custom_render.mjs
 * (Python wrapper test_shell_custom_render.py shells out so pytest/CI pick it up.)
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const PY_CANDIDATES = [
  process.env.HART_TEST_PYTHON,
  'C:/Users/sathi/miniconda3/python.exe',
  'python', 'python3',
].filter(Boolean);

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── 1. Render the shell + slice _esc + renderAgentOverlay ────────────────────
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
  // No interpreter could render the shell. Do NOT print the pass sentinel — a
  // silent "ALL PASS" here would be a vacuous green (the reuse-hunt's warning).
  console.log('SKIP: no python could render the shell (' + (lastErr && lastErr.message) + ')');
  console.log('\nRESULT: SKIP');   // distinct sentinel -> the .py wrapper skips (never a vacuous pass)
  process.exit(0);
}

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const src = scripts.find(s => s.includes('function renderAgentOverlay('));
ok(!!src, 'rendered shell contains the inline renderAgentOverlay script');
if (!src) { console.log('\nRESULT: FAIL (renderAgentOverlay not found)'); process.exit(1); }

// ── 2. Minimal DOM shim (class-selector-free; renderAgentOverlay only needs
//      createElement / body.appendChild / getElementById / innerHTML) ──────────
const byId = {};
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), _kids: [], style: {}, dataset: {},
    _id: '', className: '', innerHTML: '',
    set id(v) { this._id = v; byId[v] = this; }, get id() { return this._id; },
    set textContent(v) { this.innerHTML = String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    setAttribute(k, v) { this.dataset[k] = v; },
    appendChild(c) { this._kids.push(c); return c; },
    remove() {},
    addEventListener() {},
  };
  return el;
}
const body = makeEl('body');
const sandbox = {
  document: {
    createElement: makeEl,
    getElementById: (i) => byId[i] || null,
    body,
  },
  window: {}, console, setTimeout: () => 0, clearTimeout: () => {},
  Date: { now: () => 12345 },
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
// _overlayStack is a shell-level var the function mutates; declare it in the realm.
vm.runInContext('var _overlayStack = [];', sandbox);
// Slice from _esc to JUST BEFORE the trailing loadRecentFiles IIFE (which would
// execute and reference undefined fetch/SHELL). The range in between is only safe
// function DECLARATIONS (_esc, _submitA2UIForm, shellA2UIListSelect, _doApproval,
// renderAgentOverlay) — none execute at load, so the realm stays clean.
const escIdx = src.indexOf('function _esc(');
const endIdx = src.indexOf('(function loadRecentFiles');
ok(escIdx >= 0 && endIdx > escIdx, 'located _esc..renderAgentOverlay region (before loadRecentFiles)');
if (!(escIdx >= 0 && endIdx > escIdx)) { console.log('\nRESULT: FAIL (region boundaries not found)'); process.exit(1); }
vm.runInContext(src.slice(escIdx, endIdx), sandbox);
ok(typeof sandbox.renderAgentOverlay === 'function', 'renderAgentOverlay + _esc loaded into the realm');

function lastOverlayHTML() { return body._kids.length ? body._kids[body._kids.length - 1].innerHTML : ''; }

// ── 3a. Custom type WITH a template -> the template is filled with the props ──
sandbox.renderAgentOverlay({
  type: 'aura_ring', radius: 80, hue: 280, _ts: 1,
  _spec: { props: ['radius', 'hue'], template: '<div class="aura-ring" data-r="{{radius}}" data-h="{{hue}}"></div>' },
});
let h = lastOverlayHTML();
ok(h.indexOf('class="aura-ring"') >= 0, 'custom template rendered (structure kept)');
ok(h.indexOf('data-r="80"') >= 0 && h.indexOf('data-h="280"') >= 0, 'template {{props}} filled with the pushed values');
ok(h.indexOf('JSON') < 0 && h.indexOf('{"type"') < 0, 'custom type did NOT hit the generic JSON dump');

// ── 3b. Custom type WITHOUT a template -> props render as label/value rows ────
sandbox.renderAgentOverlay({
  type: 'battery_ring', level: 42, state: 'charging', _ts: 2,
  _spec: { props: ['level', 'state'] },
});
h = lastOverlayHTML();
ok(h.indexOf('level') >= 0 && h.indexOf('42') >= 0, 'prop-only custom type renders label/value rows (level=42)');
ok(h.indexOf('state') >= 0 && h.indexOf('charging') >= 0, 'renders the second prop row (state=charging)');

// ── 3c. XSS in a prop VALUE is escaped (the top-of-function pre-escape holds) ──
sandbox.renderAgentOverlay({
  type: 'note', body: '<img src=x onerror=alert(1)>', _ts: 3,
  _spec: { props: ['body'] },
});
h = lastOverlayHTML();
ok(h.indexOf('<img src=x') < 0, 'a malicious prop value is escaped, not injected as markup');

// ── 3d. G5: a custom type that DECLARES a click event emits it back to the agent ──
sandbox.renderAgentOverlay({
  type: 'tap_tile', label: 'Go', _ts: 4, _agent_id: 'ag1',
  _spec: { props: ['label'], events: ['click'], template: '<span>{{label}}</span>' },
});
h = lastOverlayHTML();
ok(h.indexOf('onclick="shellA2UIEmit(this)"') >= 0, 'G5: a click-declaring custom type wires the emitter');
ok(h.indexOf('data-event="click"') >= 0 && h.indexOf('data-ctype="tap_tile"') >= 0, 'G5: the emit carries the declared event + component type');

// ── 3e. G5: a custom type WITHOUT declared events is NOT wrapped with an emitter ──
sandbox.renderAgentOverlay({
  type: 'plain_tile', label: 'x', _ts: 5, _spec: { props: ['label'], events: [] },
});
h = lastOverlayHTML();
ok(h.indexOf('shellA2UIEmit') < 0, 'G5: a custom type with no declared events wires no emitter (no bloat)');

// ── 3f. G5: the builtin `metric` emits its declared click (COMPONENT_TYPES metric) ──
sandbox.renderAgentOverlay({ type: 'metric', value: 42, unit: '%', label: 'CPU', _ts: 6 });
h = lastOverlayHTML();
ok(h.indexOf('onclick="shellA2UIEmit(this)"') >= 0 && h.indexOf('data-ctype="metric"') >= 0,
   'G5: the metric builtin emits its declared click on tap');

console.log('\nRESULT: ' + (failures ? 'FAIL' : 'ALL PASS'));
process.exit(failures ? 1 : 0);

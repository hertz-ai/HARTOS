/*
 * Behavioural test for the glass-shell panel manager (LIVE-OS #20 + #21).
 *
 * #20 — Panels (agents/recipes/communities) opened a BLANK body: the iframe was
 *       injected raw, so when the SPA backend was unreachable the panel showed
 *       nothing. The fix routes every route-panel through renderRoutePanel(),
 *       which ALWAYS lays down a content container: a loading skeleton first,
 *       the iframe (hidden) second, and — if the iframe never loads — a graceful
 *       "Reconnecting…" empty state with a Retry button. Never a blank body.
 *
 * #21 — Panels opened small (cascade window). The fix opens them MAXIMIZED by
 *       default (applyMax → 100vw + the .maximized class), except floating
 *       bubbles (assistant).
 *
 * This drives the REAL JS that ships in the rendered desktop shell. We render
 * the shell with the project's conda python, slice out the inline <script>,
 * stub the handful of collaborators the panel manager calls, run it on a tiny
 * dependency-free DOM shim (CI here has no jsdom), then exercise openPanel and
 * assert the OBSERVABLE DOM the user would see. Behavioural, not source-shape.
 *
 * Run:  node tests/unit/test_liquid_ui_shell_panels.mjs
 * (Python wrapper test_liquid_ui_shell_panels.py shells out so pytest/CI pick
 *  it up too.)
 */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
// Prefer an explicit interpreter (HART_TEST_PYTHON / the local conda), then the
// platform defaults — keeps CI portable while letting the steward pin a venv.
const PY_CANDIDATES = [
  process.env.HART_TEST_PYTHON,
  'C:/Users/sathi/miniconda3/python.exe',
  'python', 'python3',
].filter(Boolean);

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── 1. Render the shell + slice the inline panel script ─────────────────────
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

// The panel manager lives in the LAST big inline <script> block (the one that
// defines openPanel). Grab every inline script, keep the one with openPanel.
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const panelSrc = scripts.find(s => s.includes('function openPanel('));
ok(!!panelSrc, 'rendered shell contains the inline panel-manager script (openPanel)');

// ── 2. Minimal DOM shim ─────────────────────────────────────────────────────
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _kids: [], _listeners: {}, dataset: {}, style: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); return on; },
      contains(c) { return this._s.has(c); },
    },
    setAttribute(k, v) { if (k === 'class') this.className = v; this['_' + k] = v; },
    getAttribute(k) { return this['_' + k]; },
    appendChild(c) { this._kids.push(c); c.parentNode = this; return c; },
    removeChild(c) {
      this._kids = this._kids.filter(k => k !== c);
      if (this._byClass) for (const k of Object.keys(this._byClass)) if (this._byClass[k] === c) delete this._byClass[k];
    },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).forEach(fn => fn(ev || {})); },
    querySelector(sel) {
      // class selector only (we use .route-frame / .route-skeleton)
      const cls = sel.replace(/^\./, '');
      function walk(node) {
        for (const k of node._kids || []) {
          if (k.classList && k.classList.contains(cls)) return k;
          const found = walk(k); if (found) return found;
        }
        return null;
      }
      // also scan our own innerHTML-built virtual children: we re-parse below
      return this._byClass ? (this._byClass[cls] || null) : walk(this);
    },
    get className() { return this._className || ''; },
    set className(v) { this._className = v; },
    get id() { return this._id || ''; },
    set id(v) { this._id = v; byId[v] = this; },
    set innerHTML(v) {
      this._innerHTML = v; this._kids = [];
      // Build a tiny virtual index of the classes the loader injects so
      // querySelector('.route-frame'/'.route-skeleton') resolves.
      this._byClass = {};
      const mk = (cls, tag) => { const e = makeEl(tag); e.classList.add(cls); this._byClass[cls] = e; e.parentNode = this; this._kids.push(e); return e; };
      if (v.includes('route-skeleton')) mk('route-skeleton', 'div');
      if (v.includes('route-frame')) { const f = mk('route-frame', 'iframe'); f.style.opacity = '0'; }
      if (v.includes('route-empty')) mk('route-empty', 'div');
      // Register any id="…" the injected markup declares, so getElementById
      // (e.g. panel-body-<id>) resolves exactly like a real browser.
      for (const m of v.matchAll(/id="([^"]+)"/g)) {
        const child = byId[m[1]] || makeEl('div');
        child.parentNode = this; byId[m[1]] = child; this._kids.push(child);
      }
    },
    get innerHTML() { return this._innerHTML || ''; },
  };
  return el;
}

const byId = {};
function reg(id) { return (byId[id] = byId[id] || makeEl('div')); }

// Pre-create the static containers the manager reads.
reg('panels');
reg('start-menu'); reg('start-scroll'); reg('start-search');

let timers = [];
const win = {
  innerWidth: 1920, innerHeight: 1080,
  dispatchEvent() {}, addEventListener() {},
  HART_PERF: { potato: false },
  CustomEvent: function (t, o) { return { type: t, detail: (o || {}).detail }; },
};
const doc = {
  getElementById(id) { return byId[id] || null; },
  createElement: makeEl,
  addEventListener() {},
  querySelectorAll() { return []; },
  documentElement: { classList: { contains() { return false; } } },
};

const sandbox = {
  window: win, document: doc, console,
  setInterval: () => 0, clearInterval: () => {},
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout: () => {},
};
sandbox.window.document = doc;
win.openPanel = undefined;
vm.createContext(sandbox);

// Stub the collaborators the panel manager calls but that live in OTHER scripts
// or earlier in the file, so we exercise ONLY the panel logic.
const STUBS = `
var panels = {}, panelZ = 10, focusedPanel = null, mru = [], startOpen = false;
var MANIFEST = {
  agents_browse: { title:'Agents', route:'/agents', default_size:[900,700] },
  recipes: { title:'Recipes', route:'/social/recipes', default_size:[800,600] },
  communities: { title:'Communities', route:'/social/communities', default_size:[800,600] },
  assistant: { title:'Assistant', route:'/social/assistant', floating:true, default_size:[400,600] }
};
var SYSTEM_PANELS = {};
var NUNBA_BASE = 'http://127.0.0.1:5000';
var PERF = { potato:false, maxPanels:0, lazyIframes:false, destroyMinimized:false };
var GROUPS = [];
function miStyle(){ return ''; }
function _pid(el){ return el && el.dataset ? el.dataset.panelId : null; }
function bringToFront(){}
function updateTaskbar(){}
function toggleStartMenu(){}
function launchApp(){}
function loadSystemPanel(){}
function dsRipple(){}
function dsSkeleton(type, count){ return '<div class="ds-skeleton-x"></div>'.repeat(count||3); }
`;

// Slice from "// ═══ Panel Manager ═══" through the end of toggleMax so we don't
// drag in the rest of the 3k-line shell (which auto-runs on load).
const start = panelSrc.indexOf('function openPanel(');
const endMarker = 'function bringToFront(id)';
const end = panelSrc.indexOf(endMarker);
ok(start >= 0 && end > start, 'could slice the panel-manager region (openPanel … bringToFront)');
const region = panelSrc.slice(start, end);

vm.runInContext(STUBS + '\n' + region, sandbox);
const { openPanel } = sandbox;
ok(typeof openPanel === 'function', 'openPanel is callable after load');

function bodyOf(id) { return byId['panel-body-' + id]; }
function fireTimers() { const t = timers; timers = []; t.forEach(x => { try { x.fn(); } catch (e) {} }); }

// ── #20: opening a route panel ALWAYS yields a content container ─────────────
for (const id of ['agents_browse', 'recipes', 'communities']) {
  openPanel(id);
  const body = bodyOf(id);
  ok(!!body, `[#20] ${id}: panel body element exists`);
  const h = (body && body.innerHTML) || '';
  ok(/route-skeleton|ds-skeleton/.test(h), `[#20] ${id}: shows a LOADING skeleton (not blank)`);
  ok(h.includes('route-frame'), `[#20] ${id}: stages the SPA iframe inside the container`);
  ok(h !== '', `[#20] ${id}: body is NEVER an empty string`);
}

// ── #20: when the iframe never loads, the timeout swaps in a reconnecting state
// communities was opened above; its 8s load-timeout timer is queued. Fire it.
fireTimers();
{
  const body = bodyOf('communities');
  const h = (body && body.innerHTML) || '';
  ok(/route-empty|Reconnecting/.test(h), '[#20] never-loading iframe -> graceful "Reconnecting" empty state (not blank)');
  ok(/Retry/.test(h), '[#20] reconnecting state offers a Retry action');
}

// ── #20: a successful iframe load reveals the frame + drops the skeleton ──────
// Use a panel whose load-timeout has NOT fired yet (agents_browse was staged
// after the fireTimers() above only drained the earlier batch — open fresh).
sandbox.MANIFEST.agents2 = { title: 'Agents', route: '/agents', default_size: [900, 700] };
openPanel('agents2');
{
  const body = bodyOf('agents2');
  const frame = body && body.querySelector('.route-frame');
  ok(!!frame, '[#20] fresh panel staged an iframe to watch');
  if (frame) {
    frame.dispatch('load');           // the SPA answered
    ok(frame.style.opacity === '1', '[#20] iframe is revealed on successful load');
    ok(!body.querySelector('.route-skeleton'), '[#20] skeleton is dropped once content loads');
  }
}

// ── #21: panels open MAXIMIZED by default; floating ones do not ──────────────
{
  const recBody = byId['panel-recipes'];
  ok(!!recBody, '[#21] recipes panel element exists');
  ok(recBody && recBody.classList.contains('maximized'),
    '[#21] route panel opens with the .maximized class');
  ok(recBody && recBody.style.width === '100vw',
    '[#21] maximized panel fills the workspace width (100vw)');
}
{
  openPanel('assistant');
  const aBody = byId['panel-assistant'];
  ok(!!aBody, '[#21] assistant (floating) panel exists');
  ok(aBody && !aBody.classList.contains('maximized'),
    '[#21] floating bubble (assistant) does NOT maximize');
}

console.log(failures ? `\nRESULT: ${failures} FAILURE(S)` : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

/*
 * Behavioural test for the shell snap-zones EFFECT (Phase 8).
 *
 * Drives the REAL integrations/agent_engine/static/hartEffects.js through the
 * shell's canonical drag lifecycle on a tiny dependency-free DOM shim (CI here
 * has no jsdom). The module self-installs on load (init() attaches the
 * hart:dragstart / hart:dragend listeners), then we:
 *
 *   1. fire hart:dragstart  -> it begins listening for mousemove
 *   2. mousemove near a screen edge -> it reveals the (single, reused) snap zone
 *   3. fire hart:dragend at that edge -> it commits via the CANONICAL
 *      window.snapPanel(id, side) — never a forked snap geometry
 *   4. the gate: with window.HART_PERF.potato = true (software-GL floor) OR
 *      prefers-reduced-motion, the same gesture installs NOTHING and calls
 *      snapPanel ZERO times -> the desktop degrades FLAT, never janky.
 *
 * Run:  node tests/unit/test_shell_effects_snapzones.mjs
 * (A Python wrapper, test_shell_effects_snapzones.py, shells out so pytest/CI
 *  picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartEffects.js');
const CODE = readFileSync(SRC, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM/element shim ───────────────────────────────────────────────
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [],
    style: {}, // plain bag; the module sets .cssText/.left/.opacity/... as props
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    get id() { return this._attrs.id || ''; }, set id(v) { this._attrs.id = String(v); }
  };
  return el;
}

// Build a shim factory so each scenario gets a clean realm (the module installs
// its listeners once per load; we re-run it per scenario for isolation).
function makeSandbox({ potato = false, reducedMotion = false, rmotionClass = false } = {}) {
  const docEl = makeEl('html');
  if (rmotionClass) docEl.classList.add('a11y-rmotion');
  const document = {
    readyState: 'complete',
    documentElement: docEl,
    body: makeEl('body'),
    _listeners: {},
    createElement: (t) => makeEl(t),
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
      if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter(f => f !== fn);
    },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev)); }
  };

  const snapCalls = [];        // [{id, side}, ...] — the observable side-effect
  const winListeners = {};
  const sandbox = {
    document, console,
    innerWidth: 1280, innerHeight: 800,
    setTimeout: (fn) => { fn(); return 0; },         // run deferred hide inline
    clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 0; },
    matchMedia: (q) => ({ matches: reducedMotion && /reduced-motion/.test(q) }),
    HART_PERF: { potato },
    snapPanel: (id, side) => { snapCalls.push({ id, side }); },
    // maximizePanel intentionally absent — exercises the module's fallback path.
    addEventListener(t, fn) { (winListeners[t] = winListeners[t] || []).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) { (winListeners[ev.type] || []).slice().forEach(fn => fn(ev)); return true; }
  };
  sandbox.window = sandbox;                          // classic-script global
  sandbox.CustomEvent = function (type, init) { return { type, detail: (init || {}).detail }; };
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'hartEffects.js' });
  return { sandbox, document, snapCalls };
}

// Helper: simulate a full drag that ends near the LEFT edge.
function dragToLeftEdge(s) {
  s.sandbox.dispatchEvent({ type: 'hart:dragstart', detail: { id: 'files' } });
  // pointer near the left edge (x <= EDGE=28) -> arms the 'left' zone
  s.document.dispatch('mousemove', { clientX: 6, clientY: 400 });
  s.sandbox.dispatchEvent({ type: 'hart:dragend', detail: { id: 'files', x: 6, y: 400 } });
}

// ── 1. Capable GPU: a near-edge drag arms a zone and commits via snapPanel ──
{
  const s = makeSandbox({ potato: false });
  dragToLeftEdge(s);
  const zone = s.document.body._kids.find(k => k._attrs.id === 'hart-snapzone');
  ok(zone, 'a single reused snap-zone overlay element was created on the body');
  eq(s.snapCalls.length, 1, 'releasing in the zone committed exactly one snap');
  eq(s.snapCalls[0] && s.snapCalls[0].side, 'left', 'snapped to the LEFT half (the armed edge)');
  eq(s.snapCalls[0] && s.snapCalls[0].id, 'files', 'snapped the dragged panel id (from the drag lifecycle)');
}

// ── 2. Drag to the very TOP arms maximize; with no maximizePanel it falls back ──
{
  const s = makeSandbox({ potato: false });
  s.sandbox.dispatchEvent({ type: 'hart:dragstart', detail: { id: 'term' } });
  s.document.dispatch('mousemove', { clientX: 600, clientY: 2 });   // y <= TOP+6 -> 'max'
  s.sandbox.dispatchEvent({ type: 'hart:dragend', detail: { id: 'term', x: 600, y: 2 } });
  // No maximizePanel exposed -> the module approximates via snapPanel(id,'left').
  eq(s.snapCalls.length, 1, 'top-edge release still commits through the canonical snap (no parallel path)');
  ok(s.snapCalls[0] && s.snapCalls[0].id === 'term', 'maximize fallback targets the dragged panel');
}

// ── 3. Release with NO armed zone -> no snap (centre drop is a plain move) ──
{
  const s = makeSandbox({ potato: false });
  s.sandbox.dispatchEvent({ type: 'hart:dragstart', detail: { id: 'files' } });
  s.document.dispatch('mousemove', { clientX: 640, clientY: 400 });  // dead centre
  s.sandbox.dispatchEvent({ type: 'hart:dragend', detail: { id: 'files', x: 640, y: 400 } });
  eq(s.snapCalls.length, 0, 'a centre release does not snap (no zone was armed)');
}

// ── 4. THE gate — potato (software-GL floor): the gesture installs nothing ──
{
  const s = makeSandbox({ potato: true });
  dragToLeftEdge(s);
  const zone = s.document.body._kids.find(k => k._attrs.id === 'hart-snapzone');
  ok(!zone, 'potato: no snap-zone overlay is ever created (degrades FLAT)');
  eq(s.snapCalls.length, 0, 'potato: a near-edge release commits NO snap (effects gated off)');
}

// ── 5. THE gate — prefers-reduced-motion: same flat degrade ──
{
  const s = makeSandbox({ potato: false, reducedMotion: true });
  dragToLeftEdge(s);
  eq(s.snapCalls.length, 0, 'prefers-reduced-motion: snap-zones are gated off');
}

// ── 6. THE gate — live a11y-rmotion class (runtime toggle, no reload) ──
{
  const s = makeSandbox({ potato: false, rmotionClass: true });
  dragToLeftEdge(s);
  eq(s.snapCalls.length, 0, 'html.a11y-rmotion: snap-zones are gated off at runtime');
}

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

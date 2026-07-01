/*
 * Behavioural tests for the post-seal COMPANION step in hartOnboarding.js.
 *
 * After the HART name seals, the driver enters a 'setup_companion' phase: it
 * reveals the name, then renders a determinate progress bar that polls
 * /api/onboarding/advance (action=companion_progress) while the backend
 * downloads the Nunba AppImage. This drives the REAL static module through a
 * tiny dependency-free DOM shim (CI here has no jsdom) and asserts OBSERVABLE
 * behaviour: the bar is built, the fill width reflects the determinate percent,
 * an indeterminate (null) percent dims a full-width bar, 'done' completes and
 * closes the overlay, and error/offline stops polling and offers Retry + Skip
 * (never traps the user).
 *
 * The shim uses a synchronous-thenable fetch (so .then(r=>r.json()) resolves
 * inline) and a CAPTURABLE timer queue, so the poll loop can be stepped one
 * tick at a time and asserted deterministically.
 *
 * Run:  node tests/unit/test_onboarding_companion.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC = readFileSync(join(STATIC, 'hartOnboarding.js'), 'utf8');

let failures = 0;
function ok(c, m) { if (c) { console.log('  OK   ' + m); } else { failures++; console.log(' FAIL  ' + m); } }
function eq(a, b, m) { ok(a === b, m + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// Synchronous thenable: cb runs inline; a thenable returned by cb is flattened
// (r.json() returns one), matching real Promise chaining without a microtask.
function sync(v) {
  function isThen(x) { return x && typeof x.then === 'function'; }
  return { then(cb) { var r = cb ? cb(v) : v; return isThen(r) ? r : sync(r); }, catch() { return this; } };
}

function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _kids: [], _listeners: {}, _innerHTML: '',
    style: {}, textContent: '', type: '', parentNode: null,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; },
    get children() { return this._kids; },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    insertBefore(node, ref) {
      node.parentNode = el;
      const i = this._kids.indexOf(ref);
      if (i < 0) this._kids.push(node); else this._kids.splice(i, 0, node);
      return node;
    },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || {})); },
    get className() { return this._className || ''; },
    set className(v) { this._className = v; },
    get id() { return this._id || ''; },
    set id(v) { this._id = String(v); }
  };
  return el;
}

function makeRealm() {
  const registry = {};
  const timers = [];
  const docEl = makeEl('html');
  const document = {
    readyState: 'complete', documentElement: docEl, _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  };
  function reg(id) { const e = makeEl('div'); e.id = id; registry[id] = e; return e; }
  const sandbox = {
    document, console, JSON,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 1; },
    HartTimeoutSignal: () => null
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return {
    sandbox, document, docEl, registry, reg, timers,
    step() { if (timers.length) { timers.shift()(); return true; } return false; }
  };
}

function findByClass(el, cls) {
  if (!el) return null;
  if (((el._className || '').split(' ').indexOf(cls) >= 0)) return el;
  for (const k of (el._kids || [])) { const r = findByClass(k, cls); if (r) return r; }
  return null;
}
function findByText(el, txt) {
  if (!el) return null;
  if (el.textContent === txt) return el;
  for (const k of (el._kids || [])) { const r = findByText(k, txt); if (r) return r; }
  return null;
}

// Build a realm with the real overlay element ids and a fetch driver that
// returns the queued /advance responses in order.
function setup(advanceResponses) {
  const R = makeRealm();
  const overlay = R.reg('hart-onboarding');
  const name = R.reg('hart-onboarding-name');
  const narr = R.reg('hart-onboarding-narr');
  const opts = R.reg('hart-onboarding-opts');
  overlay.appendChild(name); overlay.appendChild(narr); overlay.appendChild(opts);

  // /start short-circuits straight to the post-seal companion entry (the full
  // ceremony is covered by the existing tests; here we exercise MY new path).
  const begin = {
    phase: 'setup_companion',
    pa_lines: [{ id: 'post_reveal', text: 'This is yours.', pause_after_ms: 5 }],
    hart_name: 'auren', emoji_combo: 'XY', name_sealed: true, begin_companion: true
  };

  const q = advanceResponses.slice();
  const seen = [];
  R.sandbox.fetch = function (url, opts) {
    let body = {};
    try { body = (opts && opts.body) ? JSON.parse(opts.body) : {}; } catch (e) {}
    let resp = {};
    if (url.indexOf('/api/onboarding/status') >= 0) resp = { onboarded: false };
    else if (url.indexOf('/api/onboarding/start') >= 0) resp = begin;
    else if (url.indexOf('/api/onboarding/advance') >= 0) {
      seen.push(body.action);
      resp = q.length ? q.shift() : { companion: { status: 'downloading', percent: 99 } };
    }
    return sync({ json: () => sync(resp) });
  };

  return { R, overlay, name, narr, opts, seen };
}

function fillOf(overlay) {
  const bar = findByClass(overlay, 'hob-companion');
  if (!bar) return null;
  // wrap._kids = [msg, track]; track._kids = [fill]
  return bar._kids[1] && bar._kids[1]._kids[0];
}

// ── [1] happy path: determinate download -> done -> closes ──────────────────
(function happy() {
  console.log('\n[1] companion: downloading(42%) -> done -> overlay closes');
  const E = setup([
    { companion: { status: 'downloading', percent: 42, message: 'dl' } },
    { companion: { status: 'done', percent: 100, message: 'ready' }, sealed: true }
  ]);
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  ok(E.overlay.classList.contains('open'), 'overlay shown after /start returns content');
  E.R.step();   // typeLines done -> begin_companion -> startCompanion -> poll #1
  const bar = findByClass(E.overlay, 'hob-companion');
  ok(!!bar, 'progress bar (.hob-companion) built after the name seals');
  const fill = fillOf(E.overlay);
  ok(!!fill, 'progress fill element exists');
  // Brand duotone (b1.2): the fill is the teal->violet gradient the module wrote,
  // NOT the deprecated indigo #6c63ff. Assert the ACTUAL inline style it applied.
  const grad = (fill.style.cssText || '').toLowerCase();
  ok(grad.indexOf('#00e6c3') >= 0, 'companion fill leads with brand teal #00E6C3');
  ok(grad.indexOf('#9b5cff') >= 0, 'companion fill accents with brand violet #9B5CFF');
  ok(grad.indexOf('#6c63ff') < 0, 'companion fill carries NO deprecated indigo #6c63ff');
  eq(fill.style.width, '42%', 'determinate fill reflects percent=42');
  eq(E.seen[0], 'companion_progress', 'first post-seal advance is a companion_progress poll');
  E.R.step();   // poll #1 -> advance(done)
  eq(fill.style.width, '100%', 'done -> fill at 100%');
  ok(E.overlay.classList.contains('open'), 'still open until the finish timer fires');
  E.R.step();   // finish timer
  ok(!E.overlay.classList.contains('open'), 'finish() closed the overlay');
  ok(!E.R.docEl.classList.contains('onboarding-active'), 'documentElement onboarding-active cleared');
})();

// ── [2] error -> Retry + Skip (never traps) -> retry succeeds ───────────────
(function errorRetry() {
  console.log('\n[2] companion: offline -> Retry/Skip buttons -> retry -> done');
  const E = setup([
    { companion: { status: 'offline', percent: null, message: 'no net' } },
    { companion: { status: 'downloading', percent: 55 } },   // after retry
    { companion: { status: 'done' }, sealed: true }
  ]);
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  E.R.step();   // -> poll -> offline
  const bar = findByClass(E.overlay, 'hob-companion');
  ok(!!bar, 'bar built on the error path too');
  ok(!!findByText(E.opts, 'Retry'), 'Retry button shown on offline');
  ok(!!findByText(E.opts, 'Skip for now'), 'Skip for now button shown on offline');
  eq(E.R.timers.length, 0, 'error STOPS the auto-poll loop (no trapped polling)');

  findByText(E.opts, 'Retry').dispatch('click', {});   // user taps Retry
  eq(E.seen[E.seen.length - 1], 'retry_companion', 'Retry fires the retry_companion action');
  const fill = fillOf(E.overlay);
  eq(fill.style.width, '55%', 'after retry, downloading resumes at 55%');
  E.R.step();   // poll -> done
  eq(fill.style.width, '100%', 'retry path reaches done -> 100%');
  E.R.step();   // finish
  ok(!E.overlay.classList.contains('open'), 'overlay closes after a successful retry');
})();

// ── [3] skipped fast (unsupported platform / offline-fast) -> closes ────────
(function skipped() {
  console.log('\n[3] companion: skipped -> closes, no runaway polling');
  const E = setup([{ companion: { status: 'skipped', message: 'later' } }]);
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  E.R.step();   // -> poll -> skipped
  eq(E.R.timers.length, 1, 'skipped queues ONLY the finish timer (no further polls)');
  E.R.step();   // finish
  ok(!E.overlay.classList.contains('open'), 'skipped path closes the overlay');
})();

// ── [4] indeterminate (percent null) -> dim full-width bar, still completes ─
(function indeterminate() {
  console.log('\n[4] companion: indeterminate (null %) -> dim full bar -> done');
  const E = setup([
    { companion: { status: 'downloading', percent: null } },
    { companion: { status: 'done' }, sealed: true }
  ]);
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  E.R.step();   // -> downloading null
  const fill = fillOf(E.overlay);
  eq(fill.style.width, '100%', 'indeterminate -> full-width bar');
  eq(fill.style.opacity, '0.45', 'indeterminate -> dimmed (opacity 0.45)');
  E.R.step(); E.R.step();   // done -> finish
  ok(!E.overlay.classList.contains('open'), 'indeterminate path still reaches finish');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

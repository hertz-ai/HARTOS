/*
 * Behavioural tests for the NON-LOCKOUT + keyboard-navigation logic in
 * hartOnboarding.js (#134: a dead pointer must never lock the user out of the
 * full-screen onboarding overlay).
 *
 * Drives the REAL static module through a tiny dependency-free DOM shim (CI here
 * has no jsdom) and asserts OBSERVABLE behaviour:
 *   [1] an actionable Skip control is rendered in the overlay (not just the static
 *       "Press Esc to skip" text), focus is pulled INTO the modal on open (onto
 *       the first option), Tab / Shift+Tab CYCLE focus among the overlay's own
 *       controls (a focus trap — never escaping into the hidden desktop) with
 *       preventDefault called, and Esc finishes (the never-trap hatch).
 *   [2] clicking the Skip control finishes (closes) the overlay — the same exit
 *       path Esc uses.
 *
 * The shim adds focus()/document.activeElement/setAttribute + a document-level
 * keydown dispatcher on top of the companion test's pattern, and uses a
 * synchronous-thenable fetch + synchronous requestAnimationFrame so the render +
 * focus run inline and can be asserted deterministically.
 *
 * Run:  node tests/unit/test_onboarding_skip_keyboard.mjs
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

function sync(v) {
  function isThen(x) { return x && typeof x.then === 'function'; }
  return { then(cb) { var r = cb ? cb(v) : v; return isThen(r) ? r : sync(r); }, catch() { return this; } };
}

function makeRealm() {
  const registry = {};
  const timers = [];
  const state = { active: null };
  function makeEl(tag) {
    const el = {
      tagName: (tag || 'div').toUpperCase(),
      _kids: [], _listeners: {}, _innerHTML: '', _attrs: {},
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
      setAttribute(k, v) { this._attrs[k] = v; },
      // The real DOM contract this guards: focus() moves document.activeElement.
      focus() { state.active = el; },
      // No querySelector on the shim -> the driver's legacy-hint hide path is
      // exercised as a guarded no-op (which is the real WebKitGTK-safe behaviour).
      get className() { return this._className || ''; },
      set className(v) { this._className = v; },
      get id() { return this._id || ''; },
      set id(v) { this._id = String(v); }
    };
    return el;
  }
  const document = {
    readyState: 'complete', documentElement: makeEl('html'), _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    get activeElement() { return state.active; }
  };
  function reg(id) { const e = makeEl('div'); e.id = id; registry[id] = e; return e; }
  function fireKey(key, opts) {
    let prevented = false;
    const ev = {
      key: key,
      shiftKey: !!(opts && opts.shiftKey),
      preventDefault() { prevented = true; }
    };
    (document._listeners.keydown || []).slice().forEach(fn => fn(ev));
    return prevented;
  }
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
    sandbox, document, registry, reg, timers, fireKey,
    activeEl() { return state.active; },
    step() { if (timers.length) { timers.shift()(); return true; } return false; }
  };
}

function walk(el, pred) {
  if (!el) return null;
  if (pred(el)) return el;
  for (const k of (el._kids || [])) { const r = walk(k, pred); if (r) return r; }
  return null;
}
function byClass(el, cls) { return walk(el, (e) => ((e._className || '').split(' ').indexOf(cls) >= 0)); }
function byText(el, txt) { return walk(el, (e) => e.textContent === txt); }
function isDescendant(root, node) { return !!walk(root, (e) => e === node); }

// A realm whose /start lands on an OPTIONS screen (the language phase renders a
// button per language synchronously), so focus-first-option + Tab cycling are
// observable without stepping the typeLines timer.
function setup() {
  const R = makeRealm();
  const overlay = R.reg('hart-onboarding');
  const name = R.reg('hart-onboarding-name');
  const narr = R.reg('hart-onboarding-narr');
  const opts = R.reg('hart-onboarding-opts');
  overlay.appendChild(name); overlay.appendChild(narr); overlay.appendChild(opts);

  R.sandbox.fetch = function (url) {
    let resp = {};
    if (url.indexOf('/api/onboarding/status') >= 0) resp = { onboarded: false };
    else if (url.indexOf('/api/onboarding/start') >= 0) {
      resp = { phase: 'language', language_prompt: { en: 'English', es: 'Espanol' } };
    } else resp = {};
    return sync({ json: () => sync(resp) });
  };
  return { R, overlay, name, narr, opts };
}

// ── [1] Skip control + focus-into-modal + Tab focus-trap + Esc ──────────────
(function navTrap() {
  console.log('\n[1] non-lockout: visible Skip, focus into modal, Tab trap, Esc finishes');
  const E = setup();
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });

  ok(E.overlay.classList.contains('open'), 'overlay shown after /start returns content');

  // A real, actionable Skip control exists in the overlay (a <button>, NOT in the
  // options row) — the visible Skip the static "Press Esc to skip" text lacked.
  const skip = byClass(E.overlay, 'hob-skip-btn');
  ok(!!skip, 'an actionable Skip control (.hob-skip-btn) is rendered in the overlay');
  ok(skip && skip.tagName === 'BUTTON', 'the Skip control is a native <button> (Enter-activatable)');
  eq(skip && skip.textContent, 'Skip setup', 'the Skip control is visibly labelled');
  ok(!isDescendant(E.opts, skip), 'the Skip control is NOT inside the options row (always present, every screen)');
  ok(!!byText(E.overlay, 'Esc to skip'), 'an "Esc to skip" hint is shown');

  // The language phase rendered two option buttons; focus was pulled onto the FIRST.
  const en = byText(E.opts, 'English');
  const es = byText(E.opts, 'Espanol');
  ok(!!en && !!es, 'language options rendered');
  ok(E.R.activeEl() === en, 'focus moved INTO the modal onto the first option (dead-pointer can drive it)');

  // Tab forward cycles option1 -> option2 -> skip -> wraps to option1, never
  // leaving the overlay (the focus trap), and preventDefault is called each time.
  let p = E.R.fireKey('Tab', {});
  ok(p, 'Tab calls preventDefault (focus stays trapped in the modal)');
  ok(E.R.activeEl() === es, 'Tab moves focus to the second option');
  ok(isDescendant(E.overlay, E.R.activeEl()), 'focus stays inside the overlay');

  E.R.fireKey('Tab', {});
  ok(E.R.activeEl() === skip, 'Tab reaches the Skip control');

  E.R.fireKey('Tab', {});
  ok(E.R.activeEl() === en, 'Tab WRAPS from Skip back to the first option (trap, never escapes)');

  // Shift+Tab goes backward (and wraps).
  E.R.fireKey('Tab', { shiftKey: true });
  ok(E.R.activeEl() === skip, 'Shift+Tab wraps backward from the first option to Skip');

  // Esc is the never-trap hatch — it finishes regardless of where focus is.
  E.R.fireKey('Escape', {});
  ok(!E.overlay.classList.contains('open'), 'Esc finishes (closes) the overlay');
  ok(!E.R.document.documentElement.classList.contains('onboarding-active'),
     'Esc clears the onboarding-active flag (desktop interactive again)');
})();

// ── [2] clicking the Skip control finishes (same exit path as Esc) ──────────
(function skipClick() {
  console.log('\n[2] clicking Skip finishes the overlay (reuses finish())');
  const E = setup();
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  ok(E.overlay.classList.contains('open'), 'overlay open before Skip');
  const skip = byClass(E.overlay, 'hob-skip-btn');
  ok(!!skip, 'Skip control present');
  skip.dispatch('click', {});
  ok(!E.overlay.classList.contains('open'), 'clicking Skip closed the overlay');
  ok(!E.R.document.documentElement.classList.contains('onboarding-active'),
     'Skip clears the onboarding-active flag');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

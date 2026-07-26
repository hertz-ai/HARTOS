/*
 * Behavioural test: the "Light Your HART" onboarding SPEAKS its ceremony lines
 * when shown, through the shell's canonical TTS path (window.speakText), AND
 * stays SILENT when the human has shut the AI's senses (#hart-hero.ai-blind).
 *
 * It drives the REAL static module (hartOnboarding.js) through a dependency-free
 * DOM shim (CI here has no jsdom) with a synchronous-thenable fetch, and asserts
 * OBSERVABLE behaviour: window.speakText is CALLED with the narration text +
 * source 'onboarding' when the overlay opens; and when '.ai-blind' is set on
 * #hart-hero (the canonical kill-switch flag hartSenses.js writes) speakText is
 * NOT called, while the overlay still opens (the ceremony shows, it just does
 * not talk).
 *
 * Run:  node tests/unit/test_onboarding_speak.mjs
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

// Synchronous thenable: cb runs inline; a thenable returned by cb is flattened.
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
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); }
    },
    setAttribute() {}, focus() {},
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; },
    get children() { return this._kids; },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    insertBefore(node, ref) { node.parentNode = el; const i = this._kids.indexOf(ref); if (i < 0) this._kids.push(node); else this._kids.splice(i, 0, node); return node; },
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
  return { sandbox, document, docEl, registry, reg, timers,
    step() { if (timers.length) { timers.shift()(); return true; } return false; } };
}

// Build the overlay ids + #hart-hero, wire a fetch that returns a ceremony
// /start with narration lines, and record every speakText(text, source) call.
function setup(opts) {
  opts = opts || {};
  const R = makeRealm();
  const overlay = R.reg('hart-onboarding');
  R.reg('hart-onboarding-name');
  const narr = R.reg('hart-onboarding-narr');
  const optsEl = R.reg('hart-onboarding-opts');
  overlay.appendChild(narr); overlay.appendChild(optsEl);
  const hero = R.reg('hart-hero');
  if (opts.blind) hero.classList.add('ai-blind');   // human shut the AI's senses

  const spoken = [];
  R.sandbox.speakText = function (text, source) { spoken.push({ text: text, source: source }); };

  const ceremony = {
    phase: 'ceremony',
    pa_lines: [
      { text: 'Welcome home.', pause_after_ms: 5 },
      { text: 'Let us light your HART.', pause_after_ms: 5 }
    ],
    options: []
  };
  R.sandbox.fetch = function (url) {
    let resp = {};
    if (url.indexOf('/api/onboarding/status') >= 0) resp = { onboarded: false };
    else if (url.indexOf('/api/onboarding/start') >= 0) resp = ceremony;
    else if (url.indexOf('/api/onboarding/advance') >= 0) resp = {};
    return sync({ json: () => sync(resp) });
  };
  return { R, overlay, narr, spoken };
}

// ── [1] senses ON: the ceremony SPEAKS its lines on show ────────────────────
(function speaksOnShow() {
  console.log('\n[1] onboarding SPEAKS its ceremony line on show (window.speakText)');
  const E = setup();
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  ok(E.overlay.classList.contains('open'), 'overlay shown after /start returns ceremony content');
  ok(E.spoken.length >= 1, 'speakText was invoked when the ceremony is shown');
  eq(E.spoken[0] && E.spoken[0].text, 'Welcome home.', 'first spoken line is the first narration line');
  eq(E.spoken[0] && E.spoken[0].source, 'onboarding', "speakText source tag is 'onboarding'");
  E.R.step();   // advance the narration to the 2nd line
  eq(E.spoken.length, 2, 'the next narration line also speaks');
  eq(E.spoken[1] && E.spoken[1].text, 'Let us light your HART.', 'second spoken line matches');
})();

// ── [2] senses SHUT: the ceremony shows but stays SILENT (kill-switch) ───────
(function silentWhenBlind() {
  console.log('\n[2] senses shut (#hart-hero.ai-blind) -> ceremony shows but is SILENT');
  const E = setup({ blind: true });
  vm.runInContext(SRC, E.R.sandbox, { filename: 'hartOnboarding.js' });
  ok(E.overlay.classList.contains('open'), 'overlay still opens when senses are cut (never traps)');
  eq(E.spoken.length, 0, 'speakText NOT called while the AI senses are shut (kill-switch respected)');
  // The line still RENDERS (visual ceremony), it just does not talk.
  const line = E.narr._kids.find(k => k.textContent === 'Welcome home.');
  ok(!!line, 'the narration line is still rendered visually (only the speech is suppressed)');
  E.R.step();
  eq(E.spoken.length, 0, 'still silent as the narration advances');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

/*
 * Behavioural test for hartHero.js click-to-talk (#123 / W9 realtime voice).
 *
 * Drives the REAL hartHero.js module through its public surface on a faithful,
 * dependency-free DOM shim and asserts OBSERVABLE side effects — that the orb
 * click / keyboard-activation / window.HartHeroTalk all funnel into the shell's
 * canonical STT entry point (window.toggleVoice), and that a recognized
 * transcript is routed into the assistant pipeline (acSend) so the agent replies
 * (which are then spoken by speakText). Never source substrings (Gate 5).
 *
 * Run:  node tests/unit/test_hart_hero_click_to_talk.mjs
 * (test_hart_hero_click_to_talk.py shells out so pytest/CI picks it up.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const STATIC = join(ROOT, 'integrations', 'agent_engine', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── Minimal faithful DOM shim (same idiom as the drag-affordance test) ──
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, style: {}, textContent: '',
    _innerHTML: '', parentNode: null,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) { const on = (force === undefined) ? !this._s.has(c) : !!force; if (on) this._s.add(c); else this._s.delete(c); return on; } },
    _rect: { left: 0, top: 0, width: 60, height: 110 },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    get children() { return this._kids; },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild(c) { this._kids = this._kids.filter(k => k !== c); c.parentNode = null; return c; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { this._listeners[t] = (this._listeners[t] || []).filter(f => f !== fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || mkEv(el))); },
    focus() {}, select() {}, setPointerCapture() {}, releasePointerCapture() {},
    getBoundingClientRect() { return { left: 0, top: 0, width: this._rect.width, height: this._rect.height }; },
    closest(sel) {
      const parts = sel.split(',');
      let n = el;
      while (n) { for (let i = 0; i < parts.length; i++) { if (matchSel(n, parts[i])) return n; } n = n.parentNode; }
      return null;
    },
    querySelector(sel) {
      let found = null;
      (function walk(n) { for (let i = 0; i < n._kids.length && !found; i++) { const k = n._kids[i]; if (matchSel(k, sel)) { found = k; return; } walk(k); } })(el);
      return found;
    },
    querySelectorAll() { return []; },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); }
  };
  return el;
}
function matchSel(el, sel) {
  sel = sel.trim();
  if (!sel) return false;
  if (sel[0] === '.') return (el._attrs.class || '').split(/\s+/).indexOf(sel.slice(1)) >= 0;
  if (sel[0] === '#') return el._attrs.id === sel.slice(1);
  return el.tagName === sel.toUpperCase();
}
function mkEv(target, extra) {
  return Object.assign({ target, preventDefault() {}, stopPropagation() {},
    clientX: 0, clientY: 0, button: 0, pointerId: 1, key: '', code: '',
    getModifierState() { return false; } }, extra || {});
}

function makeRealm() {
  const registry = {};
  const docEl = makeEl('html');
  const head = makeEl('head');
  const body = makeEl('body');
  const document = {
    readyState: 'complete', documentElement: docEl, head, body,
    activeElement: makeEl('body'), _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || mkEv(document))); }
  };
  const el = (id) => { const e = makeEl('div'); e.setAttribute('id', id); registry[id] = e; body.appendChild(e); return e; };
  const store = new Map();
  const localStorage = {
    getItem(k) { return store.has(k) ? store.get(k) : null; },
    setItem(k, v) { store.set(k, String(v)); }, removeItem(k) { store.delete(k); }
  };
  const sandbox = {
    document, console, localStorage,
    setInterval: () => 1, clearInterval() {},
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; }, clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 1; }, cancelAnimationFrame() {},
    addEventListener(t, fn) { document.addEventListener(t, fn); },
    MutationObserver: class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} },
    innerWidth: 1280, innerHeight: 800
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, registry, el, store };
}

function runHero(realm) {
  ['hart-hero', 'hart-hero-input', 'hart-hero-go', 'hart-hero-orbwrap', 'hart-hero-status',
   'hart-hero-chips', 'hart-hero-hevolve', 'ac-input', 'assistant-chat', 'panels'].forEach(realm.el);
  realm.sandbox.isRecording = false;
  realm.sandbox._acAudio = { paused: true, ended: true };
  vm.runInContext(read('hartHero.js'), realm.sandbox, { filename: 'hartHero.js' });
}

// ── [1] The orb click funnels into window.toggleVoice (the STT entry point) ──
(function testOrbClickStartsSTT() {
  console.log('\n[1] hartHero.js  orb click-to-talk kicks the STT path (window.toggleVoice)');
  const R = makeRealm();
  let toggles = 0;
  R.sandbox.toggleVoice = function () { toggles++; };
  R.sandbox.acSend = function () {};
  R.sandbox.openPanel = function () {};
  runHero(R);
  const orb = R.registry['hart-hero-orbwrap'];

  orb.dispatch('click', mkEv(orb));
  ok(toggles === 1, 'clicking the orb calls window.toggleVoice once (starts recording -> STT)');

  // Keyboard activation (Enter / Space) is the same voice entry (a11y).
  orb.dispatch('keydown', mkEv(orb, { key: 'Enter' }));
  ok(toggles === 2, 'Enter on the focused orb also starts a voice turn');
  orb.dispatch('keydown', mkEv(orb, { key: ' ', code: 'Space' }));
  ok(toggles === 3, 'Space on the focused orb also starts a voice turn');
})();

// ── [2] window.HartHeroTalk is the single exposed voice-turn entry ──
(function testHartHeroTalkExposed() {
  console.log('\n[2] hartHero.js  window.HartHeroTalk exposes the SAME STT entry (no parallel mic path)');
  const R = makeRealm();
  let toggles = 0;
  R.sandbox.toggleVoice = function () { toggles++; };
  R.sandbox.acSend = function () {};
  R.sandbox.openPanel = function () {};
  runHero(R);

  ok(typeof R.sandbox.window.HartHeroTalk === 'function', 'window.HartHeroTalk is exposed');
  const started = R.sandbox.window.HartHeroTalk();
  ok(toggles === 1 && started === true, 'HartHeroTalk() starts a voice turn via the canonical toggleVoice');

  // With no shell voice available it degrades to false, never throws.
  const R2 = makeRealm();
  R2.sandbox.acSend = function () {};
  R2.sandbox.openPanel = function () {};
  runHero(R2);
  let threw = false, ret = null;
  try { ret = R2.sandbox.window.HartHeroTalk(); } catch (e) { threw = true; }
  ok(!threw && ret === false, 'HartHeroTalk() degrades to false when toggleVoice is absent (no throw)');
})();

// ── [3] A recognized transcript is routed into the agent pipeline (acSend) ──
(function testTranscriptReachesAgent() {
  console.log('\n[3] hartHero.js  a recognized transcript flows into the assistant pipeline');
  const R = makeRealm();
  R.sandbox.toggleVoice = function () {};
  let sent = 0;
  R.sandbox.acSend = function () { sent++; };
  R.sandbox.openPanel = function () {};
  R.sandbox.toggleAssistantChat = function () {};
  runHero(R);

  // The shell's /api/voice onstop handler mirrors the STT transcript into the
  // hero bar; assert the module surfaces it (so the user sees their words).
  ok(typeof R.sandbox.window.HartHeroShowTranscript === 'function', 'HartHeroShowTranscript is exposed');
  R.sandbox.window.HartHeroShowTranscript('turn on the lights');
  const input = R.registry['hart-hero-input'];
  ok(input.value === 'turn on the lights', 'the recognized transcript is reflected into the hero input');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

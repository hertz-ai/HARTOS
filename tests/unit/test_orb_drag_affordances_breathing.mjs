/*
 * Behavioural tests for the orb/hero drag-affordance + breathing refinements
 * (steward 2026-06-30).  They drive the REAL static modules through their public
 * surface on a faithful, dependency-free DOM shim and assert OBSERVABLE side
 * effects (classes toggled, child nodes built/torn, persisted flags, drawn-arc
 * geometry) — never source substrings (CLAUDE.md Gate 5 / feedback_no_grep_tests).
 *
 *  FIX A — drag affordances appear ONLY while dragging:
 *    [A] hartHero.js  the orb minimise control (.hart-hero-min) is revealed by the
 *        DRAG handlers (onDown -> showMin, onUp -> hideMin), NEVER by a passive
 *        hover (pointerenter/focusin do nothing), and STAYS visible while compact
 *        so the restore affordance is always reachable.
 *    [C] hartSenses.js  the sensory pod's grip-reveal HOOK: the module adds
 *        '.dragging' on pointerdown and removes it on pointerup — that is the class
 *        the CSS keys the grip's opacity:0 -> 1 reveal to (paired with [E]).
 *
 *  FIX B — breathing rings toggle (DEFAULT ON):
 *    [B] hartHero.js  buildOrbAura's concentric brand rings (.hart-hero-aura) are
 *        GATED on the persisted 'hart_orb_breathing' flag: built when ON/unset,
 *        absent when OFF; window.HartOrbBreathing.set()/the orb right-click toggle
 *        builds/tears them live and flips the persisted flag.
 *    [D] voiceOrbViz.js  setBreathing(false) DAMPENS the canvas breathe glow (the
 *        idle glow-arc oscillation collapses to ~0), while the default (breathing
 *        ON) keeps it alive — proven on a recording 2D context.
 *
 *    [E] source-guard (explicitly labelled, paired with the behavioural [A]/[C]):
 *        the generated shell CSS hides the grip by default (opacity:0) and reveals
 *        it ONLY under .hart-senses.dragging, with no :hover reveal.
 *
 * Run:  node tests/unit/test_orb_drag_affordances_breathing.mjs
 * (test_orb_drag_affordances_breathing.py shells out so pytest/CI picks it up.)
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
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Faithful DOM shim ────────────────────────────────────────────────────────
// querySelector/closest walk REAL appended children by class/id/tag and return
// null when nothing matches (so buildOrbAura's "build once" + draggableTarget work
// the way the browser does); classList/style/appendChild/removeChild are real.
function matchSel(el, sel) {
  sel = sel.trim();
  if (!sel) return false;
  if (sel[0] === '.') return (el._attrs.class || '').split(/\s+/).indexOf(sel.slice(1)) >= 0;
  if (sel[0] === '#') return el._attrs.id === sel.slice(1);
  return el.tagName === sel.toUpperCase();
}
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
    set innerHTML(v) { this._innerHTML = v; this._kids = []; },   // we don't parse HTML strings
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
    getBoundingClientRect() {
      const L = parseInt(this.style.left, 10), T = parseInt(this.style.top, 10);
      return { left: isNaN(L) ? this._rect.left : L, top: isNaN(T) ? this._rect.top : T,
               width: this._rect.width, height: this._rect.height };
    },
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
    querySelectorAll(sel) {
      const out = [];
      (function walk(n) { for (let i = 0; i < n._kids.length; i++) { const k = n._kids[i]; if (matchSel(k, sel)) out.push(k); walk(k); } })(el);
      return out;
    },
    get dataset() {
      const a = this._attrs, ds = {};
      Object.keys(a).forEach(k => { if (k.indexOf('data-') === 0) ds[k.slice(5)] = a[k]; });
      return new Proxy(ds, { set: (o, k, v) => { o[k] = v; a['data-' + k] = String(v); return true; }, get: (o, k) => o[k] });
    },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); }
  };
  return el;
}
function mkEv(target, extra) {
  return Object.assign({ target, preventDefault() {}, stopPropagation() {},
    clientX: 0, clientY: 0, button: 0, pointerId: 1, key: '', code: '',
    getModifierState() { return false; } }, extra || {});
}

function makeRealm(opts) {
  opts = opts || {};
  const registry = {};
  const intervals = [];
  const docEl = makeEl('html');
  const head = makeEl('head');
  const body = makeEl('body');
  const document = {
    readyState: 'complete', documentElement: docEl, head, body,
    activeElement: makeEl('body'), _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    querySelector(sel) { return body.querySelector(sel); },
    querySelectorAll(sel) { return body.querySelectorAll(sel); },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || mkEv(document))); }
  };
  const el = (id) => { const e = makeEl('div'); e.setAttribute('id', id); registry[id] = e; body.appendChild(e); return e; };

  // Map-backed localStorage — the breathing pref's persistence boundary.
  const store = new Map();
  const localStorage = {
    getItem(k) { return store.has(k) ? store.get(k) : null; },
    setItem(k, v) { store.set(k, String(v)); },
    removeItem(k) { store.delete(k); }
  };
  const sandbox = {
    document, console, localStorage,
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval() {},
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 1; },
    cancelAnimationFrame() {},
    addEventListener(t, fn) { document.addEventListener(t, fn); },
    MutationObserver: class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} },
    innerWidth: opts.w || 1280, innerHeight: opts.h || 800
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, docEl, registry, el, intervals, store, localStorage,
           tick() { intervals.slice().forEach(fn => fn()); } };
}

// Build the full hero element set the module destructures, run hartHero.js.
function runHero(realm) {
  ['hart-hero', 'hart-hero-input', 'hart-hero-go', 'hart-hero-orbwrap', 'hart-hero-status',
   'hart-hero-chips', 'hart-hero-hevolve', 'ac-input', 'assistant-chat', 'panels'].forEach(realm.el);
  realm.sandbox.isRecording = false;
  realm.sandbox._acAudio = { paused: true, ended: true };
  realm.sandbox.toggleVoice = function () {};
  realm.sandbox.acSend = function () {};
  realm.sandbox.openPanel = function () {};
  realm.sandbox.toggleAssistantChat = function () {};
  vm.runInContext(read('hartHero.js'), realm.sandbox, { filename: 'hartHero.js' });
  return realm.registry['hart-hero-orbwrap'];
}

// ════════════════════════════════════════════════════════════════════════════
// [B] FIX B — buildOrbAura rings gated on the persisted breathing flag (default ON)
// ════════════════════════════════════════════════════════════════════════════
(function testBreathingGatesRings() {
  console.log('\n[B] hartHero.js  breathing flag gates the brand rings (default ON)');

  // Default (key unset) => ON => rings built.
  const On = makeRealm();
  const orbOn = runHero(On);
  ok(orbOn.querySelector('.hart-hero-aura') !== null,
     'breathing unset (default ON): the .hart-hero-aura brand rings ARE built');
  ok(On.sandbox.window.HartOrbBreathing && typeof On.sandbox.window.HartOrbBreathing.get === 'function',
     'window.HartOrbBreathing read/write surface is exposed');
  eq(On.sandbox.window.HartOrbBreathing.get(), true, 'HartOrbBreathing.get() reflects the default ON');

  // Persisted OFF => rings NOT built.
  const Off = makeRealm();
  Off.store.set('hart_orb_breathing', '0');
  const orbOff = runHero(Off);
  ok(orbOff.querySelector('.hart-hero-aura') === null,
     "breathing persisted '0' (OFF): the brand rings are NOT built (calm, static orb)");
  eq(Off.sandbox.window.HartOrbBreathing.get(), false, 'HartOrbBreathing.get() reflects the persisted OFF');

  // Live toggle on the default-ON realm: set(false) tears the rings + persists '0';
  // set(true) rebuilds + persists '1' — a single mutator, no parallel path.
  On.sandbox.window.HartOrbBreathing.set(false);
  ok(orbOn.querySelector('.hart-hero-aura') === null, 'set(false): rings torn down live');
  eq(On.store.get('hart_orb_breathing'), '0', 'set(false): persisted flag is "0" (hartHero is the sole writer)');
  On.sandbox.window.HartOrbBreathing.set(true);
  ok(orbOn.querySelector('.hart-hero-aura') !== null, 'set(true): rings rebuilt live');
  eq(On.store.get('hart_orb_breathing'), '1', 'set(true): persisted flag is "1"');

  // The orb right-click toggles breathing + announces via the existing toast surface
  // (reuses the right-click context affordance; no second settings panel).
  const toasts = [];
  On.sandbox.window.showToast = (t, m) => { toasts.push([t, m]); };
  On.sandbox.window.HartOrbBreathing.set(true);          // ensure ON before the right-click
  orbOn.dispatch('contextmenu', mkEv(orbOn));
  ok(orbOn.querySelector('.hart-hero-aura') === null, 'right-click on the orb toggles breathing OFF (rings gone)');
  eq(On.store.get('hart_orb_breathing'), '0', 'right-click persisted the OFF flag');
  ok(toasts.length === 1 && /breathing/i.test(toasts[0][0]) && toasts[0][1] === 'Off',
     'right-click announced "Orb breathing: Off" via the existing toast surface');
})();

// ════════════════════════════════════════════════════════════════════════════
// [A] FIX A — the orb minimise control shows on DRAG, never on hover; stays compact
// ════════════════════════════════════════════════════════════════════════════
(function testMinBtnDragNotHover() {
  console.log('\n[A] hartHero.js  minimise control: shown on DRAG, not on hover; kept while compact');
  const R = makeRealm();
  const orb = runHero(R);
  const hero = R.registry['hart-hero'];
  const minBtn = orb.querySelector('.hart-hero-min');
  ok(minBtn !== null, 'the minimise control was built onto the orb');

  // 1) Passive hover/focus must NOT reveal it (the hover handlers were removed).
  orb.dispatch('pointerenter', mkEv(orb));
  orb.dispatch('focusin', mkEv(orb));
  ok(minBtn.style.opacity !== '0.85' && minBtn.style.opacity !== '1',
     'hover/focus on the orb does NOT reveal the control (no hover affordance)  (got ' + JSON.stringify(minBtn.style.opacity) + ')');

  // 2) Starting a DRAG of the spine reveals it.
  hero.dispatch('pointerdown', mkEv(orb, { button: 0, pointerId: 1 }));
  eq(minBtn.style.opacity, '0.85', 'dragging the orb reveals the minimise control');

  // 3) Dropping the drag hides it again (not compact).
  hero.dispatch('pointerup', mkEv(orb, { pointerId: 1 }));
  eq(minBtn.style.opacity, '0', 'dropping the drag hides the control again');

  // 4) Compact (double-click) keeps the control visible — the restore affordance.
  orb.dispatch('dblclick', mkEv(orb));
  eq(minBtn.style.opacity, '1', 'while compact the restore affordance stays visible');
  // A passive hover while compact does not change that (still no hover dependency).
  orb.dispatch('pointerleave', mkEv(orb));
  eq(minBtn.style.opacity, '1', 'compact stays visible regardless of hover (no hover dep)');

  // 5) Expanding again hides it (at rest, not dragging, not compact).
  orb.dispatch('dblclick', mkEv(orb));
  eq(minBtn.style.opacity, '0', 'expanding back hides the control at rest');
})();

// ════════════════════════════════════════════════════════════════════════════
// [C] FIX A — hartSenses drives the '.dragging' grip-reveal hook on drag start/end
// ════════════════════════════════════════════════════════════════════════════
(function testSensesDraggingHook() {
  console.log('\n[C] hartSenses.js  .dragging grip-reveal hook (only during an active drag)');
  const R = makeRealm();
  const pod = R.el('hart-senses'); pod.setAttribute('class', 'hart-senses');
  R.el('hart-senses-btn'); R.el('hart-senses-panel'); R.el('hart-senses-proof'); R.el('hart-hero');
  pod._rect = { left: 40, top: 40, width: 120, height: 52 };

  // A synchronous thenable so fetch().then(r=>r.json()).then(apply) resolves inline.
  function sync(v) { return { then(cb) { const r = cb ? cb(v) : v; return (r && r.then) ? r : sync(r); }, catch() { return this; } }; }
  R.sandbox.fetch = () => sync({ json: () => sync({ disabled: {}, proof: {} }) });
  R.sandbox.HartTimeoutSignal = () => null;

  vm.runInContext(read('hartSenses.js'), R.sandbox, { filename: 'hartSenses.js' });

  ok(!pod.classList.contains('dragging'), 'at rest the pod is NOT .dragging (grip stays hidden by CSS)');
  // Drag the pod by its BODY (target = pod, not a .hart-senses-btn) -> .dragging on.
  pod.dispatch('pointerdown', mkEv(pod, { button: 0, pointerId: 1 }));
  ok(pod.classList.contains('dragging'), 'pointerdown on the body adds .dragging (reveals the grip via CSS)');
  // Release -> .dragging off (grip hides again).
  pod.dispatch('pointerup', mkEv(pod, { pointerId: 1 }));
  ok(!pod.classList.contains('dragging'), 'pointerup removes .dragging (grip hidden again)');

  // A button press must NOT start a drag (it acts) -> never toggles .dragging.
  const btn = R.registry['hart-senses-btn']; btn.setAttribute('class', 'hart-senses-btn'); btn.parentNode = pod;
  pod.dispatch('pointerdown', mkEv(btn, { button: 0, pointerId: 2 }));
  ok(!pod.classList.contains('dragging'), 'pressing a sensory button does not start a drag (grip stays hidden)');
})();

// ════════════════════════════════════════════════════════════════════════════
// [D] FIX B — voiceOrbViz.setBreathing(false) dampens the canvas breathe glow
// ════════════════════════════════════════════════════════════════════════════
(function testVizBreathingDamp() {
  console.log('\n[D] voiceOrbViz.js  setBreathing gates the canvas breathe glow');
  function runViz(frames, opts) {
    const frameArcs = []; let cur = null;
    const ctx = { _f: null, set fillStyle(v) { this._f = v; }, get fillStyle() { return this._f; },
      set strokeStyle(v) {}, get strokeStyle() { return null; },
      set globalCompositeOperation(v) {}, get globalCompositeOperation() { return 's'; },
      clearRect() { if (cur) frameArcs.push(cur); cur = []; },
      beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, fill() {}, stroke() {},
      arc(x, y, r) { if (cur) cur.push(r); },
      createRadialGradient() { return { addColorStop() {} }; } };
    const canvas = { width: 480, height: 480, getContext: () => ctx };
    let cb = null;
    const sb = { requestAnimationFrame: (f) => { cb = f; return 1; }, cancelAnimationFrame() {} };
    sb.window = sb; vm.createContext(sb);
    vm.runInContext(read('voiceOrbViz.js'), sb, { filename: 'voiceOrbViz.js' });
    const orb = sb.HartVoiceOrbViz(canvas, opts || {});
    orb.setActive(false);     // IDLE: the glow is then a clean function of breath alone
    for (let i = 0; i < frames; i++) { if (cb) cb(); }
    if (cur) frameArcs.push(cur);
    orb.destroy();
    return frameArcs;
  }
  // The breathe glow is the 2nd-largest arc per frame (largest = the static bg disc).
  function glowRange(frameArcs) {
    const glow = frameArcs.filter(f => f.length >= 2).map(f => f.slice().sort((a, b) => b - a)[1]);
    let mn = Infinity, mx = -Infinity;
    glow.forEach(v => { if (v < mn) mn = v; if (v > mx) mx = v; });
    return mx - mn;
  }
  const onRange = glowRange(runViz(300, {}));                    // default => breathing ON
  const offRange = glowRange(runViz(300, { breathing: false }));  // breathing OFF
  ok(onRange > 3, 'default (breathing ON): the idle glow BREATHES (oscillation range > 3px)  (got ' + onRange.toFixed(2) + ')');
  ok(offRange < 0.5, 'setBreathing(false): the idle glow is calm/static (range ~0)  (got ' + offRange.toFixed(2) + ')');
  ok(onRange > offRange + 3, 'the breathing flag visibly gates the glow (ON ' + onRange.toFixed(2) + ' >> OFF ' + offRange.toFixed(2) + ')');
})();

// ════════════════════════════════════════════════════════════════════════════
// [E] source-guard (labelled) — the grip CSS hides by default, reveals only on drag
// ════════════════════════════════════════════════════════════════════════════
(function testGripCssSourceGuard() {
  console.log('\n[E] source-guard  .hart-senses-grip hidden at rest, revealed only under .dragging');
  const css = readFileSync(join(ROOT, 'integrations', 'agent_engine', 'liquid_ui_service.py'), 'utf8');

  // Default grip rule sets opacity:0 (hidden at rest) while keeping its width.
  const def = /\.hart-senses-grip\{[^}]*opacity:0[^}]*\}/.test(css);
  ok(def, 'the default .hart-senses-grip rule sets opacity:0 (hidden at rest)');
  ok(/\.hart-senses-grip\{[^}]*width:22px/.test(css), 'the grip keeps its width (stays part of the drag hit-area)');

  // Reveal happens ONLY under .hart-senses.dragging .hart-senses-grip (opacity:1).
  ok(/\.hart-senses\.dragging\s+\.hart-senses-grip\{[^}]*opacity:1/.test(css),
     'the grip is revealed (opacity:1) ONLY under .hart-senses.dragging');

  // No :hover reveal remains (the affordance must not appear on a passive hover).
  ok(!/\.hart-senses-grip:hover/.test(css), 'no .hart-senses-grip:hover reveal remains (drag-only affordance)');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

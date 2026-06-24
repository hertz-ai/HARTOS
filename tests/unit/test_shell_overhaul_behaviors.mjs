/*
 * Behavioural tests for the HART OS shell OVERHAUL interaction logic.
 *
 * The overhaul's core behaviours were previously guarded only by HTML-substring
 * presence checks + the icon-customize dialog .mjs — none drove the real state
 * machines, which is exactly the gap that let the data-speaking dead-write and
 * the _hartThinking timer race ship green (CLAUDE.md Gate 5 / no-grep-tests).
 *
 * This drives the REAL static modules through their public surface on a tiny
 * dependency-free DOM shim (CI here has no jsdom) and asserts OBSERVABLE
 * side-effects (attributes stamped, classes toggled, persisted blobs):
 *
 *   A. hartVisibility.js — the state->attribute engine. Flip the shell's real
 *      globals (isRecording / _hartThinking / _acAudio / navigator.onLine /
 *      #agent-status chips / #panels / activeElement / idle clock) and assert the
 *      correct <html data-*> is stamped — INCLUDING data-speaking / data-agents /
 *      data-online (the 3 signals the review flagged; they now drive CSS, and the
 *      engine must keep writing them).
 *   B. hartHero.js — dispatch() must NOT arm a fixed-duration _hartThinking clear
 *      (the race: the local 4B runs 64-600s, a 4.5s force-flip makes the lamp
 *      lie). acSend stays the sole writer; the hero only mirrors. The orb reflect
 *      loop lights data-orb-state=thinking off the real flag and clears the hevolve
 *      dot + status when the flag clears.
 *   C. hartSenses.js — restore() RE-CLAMPS a stale saved position to the current
 *      viewport (a pod saved off the right edge of a wide screen lands on-screen on
 *      a narrow one), and the eye lamp lights .is-sensing for ANY live sense
 *      (mic on while the camera service is idle => still lit), not camera-only.
 *   D. hartWorkspaces.js — the pager reveals (data-multiws='1') as soon as the
 *      feature is USABLE (any window open OR navigated off desktop 1), breaking
 *      the old ">1 occupied" discoverability deadlock; stays hidden on a pristine
 *      empty desktop.
 *
 * Run:  node tests/unit/test_shell_overhaul_behaviors.mjs
 * (A Python wrapper, test_shell_overhaul_behaviors.py, shells out so pytest/CI
 *  picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM shim ────────────────────────────────────────────────────────
// Real enough for these modules: attributes/style/classList/children, listeners
// with dispatch(), getBoundingClientRect (drives the senses clamp math), and a
// capturable timer/rAF so the test can tick the engines deterministically.
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _bySel: {}, _listeners: {},
    style: {}, textContent: '', _innerHTML: '', parentNode: null,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const on = (force === undefined) ? !this._s.has(c) : !!force;
        if (on) this._s.add(c); else this._s.delete(c);
        return on;
      } },
    _rect: { left: 0, top: 0, width: 60, height: 110 },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; this._bySel = {}; },
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
      // reflect any absolute placement the module set via style.left/top
      const L = parseInt(this.style.left, 10), T = parseInt(this.style.top, 10);
      return { left: isNaN(L) ? this._rect.left : L, top: isNaN(T) ? this._rect.top : T,
               width: this._rect.width, height: this._rect.height };
    },
    closest(sel) {
      let n = el;
      const cls = sel.replace(/^\./, '');
      while (n) { if ((n._attrs.class || '').split(' ').includes(cls)) return n; n = n.parentNode; }
      return null;
    },
    get dataset() {
      const a = this._attrs, ds = {};
      Object.keys(a).forEach(k => { if (k.indexOf('data-') === 0) ds[k.slice(5)] = a[k]; });
      // proxy writes back onto attributes (the modules set el.dataset.edge / .ws)
      return new Proxy(ds, { set: (o, k, v) => { o[k] = v; a['data-' + k] = String(v); return true; },
                            get: (o, k) => o[k] });
    },
    querySelector(sel) {
      for (const k of this._kids) if (k._attrs.id && ('#' + k._attrs.id) === sel) return k;
      if (!this._bySel[sel]) { const s = makeEl('div'); s.parentNode = el; this._bySel[sel] = s; }
      return this._bySel[sel];
    },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, '').split('[')[0];
      const dm = /\[data-ws="(\d+)"\]/.exec(sel);
      let out = this._kids.filter(k => (k._attrs.class || '').split(' ').includes(cls));
      if (dm) out = out.filter(k => k._attrs['data-ws'] === dm[1]);
      return out;
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

// A capturable scheduler: collect setInterval callbacks so the test can tick
// them on demand (the engines run on 250/350ms loops). rAF + setTimeout run
// inline so drag math + deferred init resolve synchronously.
function makeRealm(opts) {
  opts = opts || {};
  const registry = {};
  const intervals = [];
  const docEl = makeEl('html');
  const document = {
    readyState: 'complete',
    documentElement: docEl,
    body: makeEl('body'),
    activeElement: makeEl('body'),
    _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    querySelector(sel) { return document.body.querySelector(sel); },
    querySelectorAll(sel) {
      // walk the live tree (the modules query .start-item / .panel globally)
      const cls = sel.replace(/^\./, '').split('[')[0], out = [];
      const walk = (n) => { for (const k of n._kids) { if ((k._attrs.class || '').split(' ').includes(cls)) out.push(k); walk(k); } };
      walk(document.body);
      return out;
    },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || mkEv(document))); }
  };
  const el = (id) => { const e = makeEl('div'); e.setAttribute('id', id); registry[id] = e; document.body.appendChild(e); return e; };

  const navigator = { onLine: true };
  const sandbox = {
    document, console, navigator,
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval() {},
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout() {},
    requestAnimationFrame: (fn) => { fn(); return 1; },
    cancelAnimationFrame() {},
    addEventListener(t, fn) { document.addEventListener(t, fn); },
    MutationObserver: class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} },
    innerWidth: opts.w || 1280, innerHeight: opts.h || 800
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, docEl, registry, el, intervals,
           tick() { intervals.slice().forEach(fn => fn()); } };
}

// ════════════════════════════════════════════════════════════════════════════
// A. hartVisibility.js — state -> <html data-*> engine (incl. the 3 ex-dead ones)
// ════════════════════════════════════════════════════════════════════════════
(function testVisibility() {
  console.log('\n[A] hartVisibility.js  state -> data-* engine');
  const R = makeRealm();
  const agentBar = R.el('agent-status');
  const panels = R.el('panels');
  // shell globals the engine reads (top-level lets in the inline script):
  R.sandbox.isRecording = false;
  R.sandbox._acAudio = { paused: true, ended: true };
  R.sandbox._hartThinking = false;
  vm.runInContext(read('hartVisibility.js'), R.sandbox, { filename: 'hartVisibility.js' });
  R.tick();   // run one sample with everything idle/at-rest

  const D = (k) => R.docEl.getAttribute('data-' + k);
  // every signal is WRITTEN (none absent) — the dead-write guard: the engine must
  // emit all 10, especially data-speaking/agents/online (now CSS-consumed).
  ['voice','thinking','speaking','blind','panels','typing','idle','agents','online','busy']
    .forEach(k => ok(D(k) !== null, 'data-' + k + ' is stamped (signal not dead/absent)'));

  // at rest: nothing active
  eq(D('voice'), '0', 'idle: data-voice=0');
  eq(D('speaking'), '0', 'idle: data-speaking=0');
  eq(D('busy'), '0', 'idle: data-busy=0');
  eq(D('agents'), '0', 'no agent chips -> data-agents=0');
  eq(D('online'), '1', 'navigator.onLine -> data-online=1');

  // speaking: TTS audio playing -> data-speaking flips (the ex-dead signal works)
  R.sandbox._acAudio = { paused: false, ended: false };
  R.tick();
  eq(D('speaking'), '1', 'TTS playing -> data-speaking=1 (drives the ambient wash)');
  eq(D('busy'), '1', 'speaking counts as busy');

  // listening + thinking
  R.sandbox._acAudio = { paused: true, ended: true };
  R.sandbox.isRecording = true; R.sandbox._hartThinking = true;
  R.tick();
  eq(D('voice'), '1', 'isRecording -> data-voice=1');
  eq(D('thinking'), '1', '_hartThinking -> data-thinking=1');
  eq(D('speaking'), '0', 'speaking back to 0 when TTS stops');

  // agents present -> data-agents=1 (DOM-derived count of .agent-chip)
  const chip = makeEl('span'); chip.className = 'agent-chip'; agentBar.appendChild(chip);
  R.tick();
  eq(D('agents'), '1', 'an .agent-chip in #agent-status -> data-agents=1');

  // panels open -> data-panels=1
  panels.appendChild(makeEl('div'));
  R.tick();
  eq(D('panels'), '1', 'a child in #panels -> data-panels=1');

  // offline -> data-online=0 (drives the offline desaturation)
  R.sandbox.navigator.onLine = false;
  R.tick();
  eq(D('online'), '0', 'navigator.onLine=false -> data-online=0');
})();

// ════════════════════════════════════════════════════════════════════════════
// B. hartHero.js — no fixed-duration _hartThinking clear (the race)
// ════════════════════════════════════════════════════════════════════════════
(function testHeroThinkingRace() {
  console.log('\n[B] hartHero.js  _hartThinking is acSend-owned, no hero timer race');
  const R = makeRealm();
  R.el('hart-hero'); const input = R.el('hart-hero-input'); R.el('hart-hero-go');
  const orb = R.el('hart-hero-orbwrap'); R.el('hart-hero-status');
  R.el('hart-hero-chips'); const hev = R.el('hart-hero-hevolve');
  const aci = R.el('ac-input'); const chat = R.el('assistant-chat');

  R.sandbox.isRecording = false;
  R.sandbox._acAudio = { paused: true, ended: true };
  R.sandbox.MANIFEST = { files: { title: 'Files' } };   // a known app (deterministic launch)
  let acSendCalls = 0, openPanelCalls = 0;
  // The CANONICAL writer: sets the flag now, clears on its real terminal path.
  R.sandbox.acSend = function () { acSendCalls++; R.sandbox.window._hartThinking = true; };
  R.sandbox.openPanel = function () { openPanelCalls++; };
  R.sandbox.toggleAssistantChat = function () { chat.classList.add('open'); };
  R.sandbox.toggleVoice = function () {};

  vm.runInContext(read('hartHero.js'), R.sandbox, { filename: 'hartHero.js' });

  // A real question (not an app name) -> delegates to acSend, which sets the flag.
  // CRITICAL: the hero must NOT have armed a 4.5s clear. Our shim runs setTimeout
  // INLINE, so if the hero still owned `setTimeout(()=>thinking(false),4500)` the
  // flag would be FALSE right after dispatch. We assert it stays TRUE.
  input.value = 'what is the weather like on mars';
  input.dispatch('keydown', mkEv(input, { key: 'Enter' }));
  ok(acSendCalls === 1, 'a real question delegates to acSend exactly once');
  ok(R.sandbox.window._hartThinking === true,
     'after dispatch the brain flag is STILL set (no hero 4.5s force-clear race)');

  // The reflect loop lights the orb thinking-state off the REAL flag, and mirrors
  // the hevolve dot. Tick it; thinking should win.
  R.tick();
  eq(orb.getAttribute('data-orb-state'), 'thinking', 'orb reflects data-orb-state=thinking off the real flag');
  ok(hev.classList.contains('on'), 'hevolve dot mirrors the real thinking flag (on)');

  // acSend resolves (its real terminal clear). Now the loop must drop the dot +
  // orb state — driven by the flag, not a guessed timer.
  R.sandbox.window._hartThinking = false;
  R.tick();
  eq(orb.getAttribute('data-orb-state'), 'idle', 'flag cleared -> orb returns to idle');
  ok(!hev.classList.contains('on'), 'flag cleared -> hevolve dot off (mirror, not timer)');

  // A known app name takes the deterministic fast-path: openPanel, no acSend,
  // no "thinking" left hanging.
  acSendCalls = 0;
  input.value = 'files';
  input.dispatch('keydown', mkEv(input, { key: 'Enter' }));
  ok(openPanelCalls === 1, 'a known app name launches via openPanel (deterministic)');
  ok(acSendCalls === 0, 'app-name fast-path does NOT hit the brain');
})();

// ════════════════════════════════════════════════════════════════════════════
// C. hartSenses.js — restore() re-clamps; eye lamp lights for ANY sense
// ════════════════════════════════════════════════════════════════════════════
(function testSensesRestoreAndLamp() {
  console.log('\n[C] hartSenses.js  restore() re-clamp + any-sense eye lamp');
  // Saved at the bottom-right of a 1920-wide screen; shell now boots at 1280.
  const R = makeRealm({ w: 1280, h: 800 });
  const pod = R.el('hart-senses');
  pod._rect = { left: 0, top: 0, width: 60, height: 110 };
  const eye = R.el('hart-senses-btn');
  R.el('hart-senses-mic'); R.el('hart-senses-panel'); R.el('hart-senses-proof');
  R.el('hart-hero');

  const savedBlob = { senses_pos: { x: 1840, y: 760, edge: 'rb' } };   // off-screen on 1280
  let lastSet = null;
  R.sandbox.HartSession = {
    ready(cb) { cb(savedBlob); },
    get(k) { return savedBlob[k]; },
    set(k, v) { savedBlob[k] = v; lastSet = { k, v }; }
  };
  // A SYNCHRONOUS thenable so fetch().then(r=>r.json()).then(apply) resolves inline
  // (real Promises defer to the microtask queue, which R.tick() can't drain) — this
  // lets us assert the eye lamp deterministically right after a poll. Like a real
  // Promise it FLATTENS a thenable returned by a callback (r.json() returns one), so
  // apply() receives the resolved status object, not a wrapped thenable. .catch noop.
  function isThenable(x) { return x && typeof x.then === 'function'; }
  function sync(v) {
    return {
      then(cb) { var r = cb ? cb(v) : v; return isThenable(r) ? r : sync(r); },
      catch() { return this; }
    };
  }
  var STATUS = {};
  R.sandbox.fetch = () => sync({ json: () => sync(STATUS) });
  R.sandbox.HartTimeoutSignal = () => null;

  vm.runInContext(read('hartSenses.js'), R.sandbox, { filename: 'hartSenses.js' });
  // init()/initDrag()/restore() ran (setTimeout inline). The pod must be placed
  // ON-SCREEN: left <= viewport - width, not the stale 1840 that came from disk.
  const left = parseInt(pod.style.left, 10), top = parseInt(pod.style.top, 10);
  ok(!isNaN(left), 'restore() placed the pod (absolute left set)');
  ok(left <= 1280 - 60, 'restore() RE-CLAMPED x into the 1280 viewport (not the stale 1840) -> on-screen  (got ' + left + ')');
  ok(top <= 800 - 110, 'restore() re-clamped y into the viewport  (got ' + top + ')');

  // ── Eye lamp: ANY live sense, not camera-only ──
  function applyState(st) {
    // refresh() routes the polled status through the module's internal apply();
    // swap the synchronous status payload, then re-run the poll loop so apply()
    // executes inline (no microtask defer) and we can assert the eye class.
    STATUS = st;
    R.tick();
  }
  // Mic ON (not disabled), camera service IDLE (not running), nothing cut.
  applyState({ disabled: { mic: false, camera: false, screen: false },
               proof: { camera_service_running: false } });
  ok(eye.classList.contains('is-sensing'),
     'mic on + camera service idle + nothing cut -> eye lit is-sensing (ANY sense, not camera-only)');
  ok(!eye.classList.contains('off'), 'nothing cut -> eye not in the red .off state');

  // Everything cut -> red .off, lamp off.
  applyState({ disabled: { mic: true, camera: true, screen: true },
               proof: { camera_service_running: false } });
  ok(eye.classList.contains('off'), 'all senses cut -> eye shows the red .off (blind) state');
  ok(!eye.classList.contains('is-sensing'), 'all senses cut -> is-sensing cleared');

  // Camera genuinely running (also a live sense) -> lit.
  applyState({ disabled: { mic: false, camera: false, screen: false },
               proof: { camera_service_running: true } });
  ok(eye.classList.contains('is-sensing'), 'camera service running -> eye lit is-sensing');
})();

// ════════════════════════════════════════════════════════════════════════════
// D. hartWorkspaces.js — pager reveals once USABLE (deadlock fix), hidden when empty
// ════════════════════════════════════════════════════════════════════════════
(function testWorkspacesReveal() {
  console.log('\n[D] hartWorkspaces.js  data-multiws reveal (deadlock fix)');
  const R = makeRealm();
  const panels = R.el('panels');
  const switcher = R.el('hart-ws-switcher');

  vm.runInContext(read('hartWorkspaces.js'), R.sandbox, { filename: 'hartWorkspaces.js' });
  // init() ran (setTimeout inline): bar built, apply() called once, no panels yet.
  const MW = () => R.docEl.getAttribute('data-multiws');
  eq(MW(), '0', 'pristine empty desktop -> data-multiws=0 (pager hidden)');
  ok(switcher.querySelectorAll('.hart-pager-seg').length === 4, 'pager built its 4 desktop segments');

  // Open a panel on the current desktop. The MutationObserver is a no-op in the
  // shim, so call the public re-evaluation the same way the app re-applies state:
  // a workspace switch runs apply()->paintOccupancy(). First, prove a window makes
  // the feature usable by tagging a panel + re-applying via a no-op switch+back.
  const p = makeEl('div'); p.className = 'panel'; p.setAttribute('data-ws', '1'); panels.appendChild(p);
  // hartSwitchWorkspace(2) then back to (1) forces apply() twice with a window present.
  R.sandbox.window.hartSwitchWorkspace(2);
  ok(MW() === '1', 'navigated off desktop 1 -> data-multiws=1 (switcher reachable, not stranded)');
  R.sandbox.window.hartSwitchWorkspace(1);
  eq(MW(), '1', 'a window is open -> data-multiws=1 even back on desktop 1 (deadlock broken)');

  // Remove the window and return to a pristine desktop 1 -> hidden again.
  panels.removeChild(p);
  R.sandbox.window.hartSwitchWorkspace(2);
  R.sandbox.window.hartSwitchWorkspace(1);
  eq(MW(), '0', 'no windows + back on desktop 1 -> data-multiws=0 (clean again)');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

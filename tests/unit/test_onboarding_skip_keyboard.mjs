/*
 * Behavioural tests for hartOnboarding.js: the first-run overlay that hosts
 * Nunba's own "Light Your HART" ceremony (2026-10-12), and its NON-LOCKOUT
 * contract (#134: a dead pointer must never lock the user out of a full-screen
 * overlay).
 *
 * Drives the REAL static module through the shared DOM shim
 * (shell_dom_shim.mjs; CI here has no jsdom) and asserts OBSERVABLE behaviour:
 *   [1] with Nunba up and no seal, the overlay opens with Nunba's /local page
 *       in a frame, an actionable Skip control, focus pulled onto it, a Tab
 *       trap between the Skip control and the frame, and Esc finishing
 *   [2] clicking Skip finishes through the same exit path
 *   [3] Nunba's own gate (localStorage hart_sealed) decides: sealed -> nothing
 *       is asked, not even the status route
 *   [4] the seal written by Nunba's frame (the storage event) closes the overlay
 *   [5] Nunba's nunba:onboarding message closes it, and only from the frame
 *   [6] Nunba not up (companion_ready false) -> no overlay, a bounded reprobe
 *   [7] the owner's seal on record (onboarded true) -> no overlay
 *   [8] the poll under the storage event closes the overlay too
 *
 * Run:  node tests/unit/test_onboarding_skip_keyboard.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeRealm, mkEv } from './shell_dom_shim.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC = readFileSync(join(STATIC, 'hartOnboarding.js'), 'utf8');
const ORIGIN = 'http://127.0.0.1:6800';

let failures = 0;
function ok(c, m) { if (c) { console.log('  OK   ' + m); } else { failures++; console.log(' FAIL  ' + m); } }
function eq(a, b, m) { ok(a === b, m + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// A synchronous thenable so fetch().then(...).then(...) resolves inline.
function sync(v) {
  function isThen(x) { return x && typeof x.then === 'function'; }
  return { then(cb) { var r = cb ? cb(v) : v; return isThen(r) ? r : sync(r); }, catch() { return this; } };
}

function setup(status, opts) {
  opts = opts || {};
  const R = makeRealm();
  const overlay = R.el('hart-onboarding');
  const fetched = [];
  R.sandbox.location = { origin: ORIGIN };
  R.sandbox.fetch = function (url) {
    fetched.push(String(url));
    return sync({ json: () => sync(status) });
  };
  if (opts.sealed) R.sandbox.localStorage.setItem('hart_sealed', 'true');
  R.run(SRC, 'hartOnboarding.js');
  return { R, overlay, fetched,
    frame() { return overlay.querySelector('.hob-frame'); },
    skip() { return overlay.querySelector('.hob-skip-btn'); },
    key(key, extra) { return R.document.dispatch('keydown', mkEv(R.document.body, Object.assign({ key }, extra || {}))); },
    open() { return overlay.classList.contains('open'); },
    active() { return R.docEl.classList.contains('onboarding-active'); } };
}

// ── [1] Nunba's page in a frame, Skip control, focus, Tab trap, Esc ────────
(function hostsNunba() {
  console.log('\n[1] with Nunba up and no seal: Nunba\'s page is hosted, Skip + Esc never trap');
  const E = setup({ onboarded: false, companion_ready: true });
  ok(E.fetched.length === 1 && E.fetched[0].indexOf('/api/onboarding/status') >= 0, 'asked the status route once');
  ok(E.fetched[0].indexOf('user_id=') < 0, 'no hard-coded user_id: the server names the desktop owner');
  ok(E.open(), 'overlay shown');
  ok(E.active(), 'onboarding-active set on <html> (hartSessionUI reads it)');

  const f = E.frame();
  ok(!!f && f.tagName === 'IFRAME', 'Nunba\'s page is mounted in an <iframe class="hob-frame">');
  eq(f && f.src, '/local', 'the frame points at Nunba\'s /local (its first-run state is the ceremony)');
  ok(!!f && f.getAttribute('allow') && f.getAttribute('allow').indexOf('microphone') >= 0, 'the frame may use the mic (the ceremony speaks and listens)');

  const skip = E.skip();
  ok(!!skip && skip.tagName === 'BUTTON', 'an actionable Skip control (.hob-skip-btn) is a native <button>');
  eq(skip && skip.textContent, 'Skip setup', 'the Skip control is visibly labelled');
  ok(!!overlayText(E.overlay, 'Esc to skip'), 'an "Esc to skip" hint is shown');
  ok(E.R.state.active === skip, 'focus pulled INTO the modal onto the Skip control (a dead pointer can drive it)');

  // Tab cycles Skip -> frame -> Skip, with preventDefault, never the desktop behind.
  let ev = E.key('Tab');
  ok(ev.defaultPrevented, 'Tab calls preventDefault (focus stays trapped)');
  ok(E.R.state.active === f, 'Tab from Skip moves focus INTO Nunba\'s frame');
  ev = E.key('Tab');
  ok(E.R.state.active === skip, 'Tab from the frame returns to the Skip control (the trap)');
  ev = E.key('Tab', { shiftKey: true });
  ok(E.R.state.active === f, 'Shift+Tab also moves between the two stops');

  E.key('Escape');
  ok(!E.open(), 'Esc finishes (closes) the overlay');
  ok(!E.active(), 'Esc clears onboarding-active (desktop interactive again)');
  ok(!E.frame(), 'the frame is dropped on finish (the assistant panel is the one chat from here)');
})();

// ── [2] clicking Skip finishes (same exit path as Esc) ─────────────────────
(function skipClick() {
  console.log('\n[2] clicking Skip finishes the overlay (reuses finish())');
  const E = setup({ onboarded: false, companion_ready: true });
  ok(E.open(), 'overlay open before Skip');
  E.skip().click();
  ok(!E.open() && !E.active(), 'clicking Skip closed the overlay and cleared the flag');
})();

// ── [3] Nunba's own gate decides ───────────────────────────────────────────
(function sealedGate() {
  console.log('\n[3] localStorage hart_sealed=true (Nunba\'s gate): no ceremony, nothing asked');
  const E = setup({ onboarded: false, companion_ready: true }, { sealed: true });
  ok(!E.open(), 'overlay never opens for a sealed person');
  eq(E.fetched.length, 0, 'the status route is not even asked: the flag is the same truth Nunba reads');
})();

// ── [4] the storage event from Nunba's frame closes it ─────────────────────
(function storageEvent() {
  console.log('\n[4] Nunba\'s frame writes hart_sealed: the storage event closes the overlay');
  const E = setup({ onboarded: false, companion_ready: true });
  ok(E.open(), 'open');
  E.R.window.dispatch('storage', { key: 'nunba_media_mode' });
  ok(E.open(), 'an unrelated key changing does nothing');
  E.R.sandbox.localStorage.setItem('hart_sealed', 'true');
  E.R.window.dispatch('storage', { key: 'hart_sealed' });
  ok(!E.open() && !E.active(), 'hart_sealed=true -> finished');
})();

// ── [5] nunba:onboarding, from the frame only ──────────────────────────────
(function sealedMessage() {
  console.log('\n[5] nunba:onboarding {state:sealed} from the frame closes the overlay; other sources/origins do not');
  const E = setup({ onboarded: false, companion_ready: true });
  const f = E.frame();
  f.contentWindow = { id: 'nunba-frame' };
  const msg = (extra) => Object.assign({ origin: ORIGIN, source: f.contentWindow, data: { type: 'nunba:onboarding', state: 'sealed' } }, extra || {});
  E.R.window.dispatch('message', msg({ origin: 'https://attacker.example' }));
  ok(E.open(), 'a foreign origin is ignored');
  E.R.window.dispatch('message', msg({ source: { id: 'some-other-window' } }));
  ok(E.open(), 'another window of our origin (the assistant panel) is ignored');
  E.R.window.dispatch('message', msg({ data: { type: 'nunba:turn', state: 'done' } }));
  ok(E.open(), 'another nunba: message type is ignored');
  E.R.window.dispatch('message', msg());
  ok(!E.open() && !E.active(), 'the frame\'s own nunba:onboarding sealed -> finished');
})();

// ── [6] Nunba not up: no overlay, bounded reprobe ──────────────────────────
(function nunbaDown() {
  console.log('\n[6] companion_ready=false: never show a page that cannot load; reprobe later');
  const E = setup({ onboarded: false, companion_ready: false });
  ok(!E.open(), 'overlay stays closed');
  ok(E.R.timers.length >= 1 && E.R.timers.some((t) => t.ms === 5000), 'a 5 s reprobe is queued');
  // Nunba comes up: the reprobe shows it.
  E.R.sandbox.fetch = () => sync({ json: () => sync({ onboarded: false, companion_ready: true }) });
  E.R.flushTimers();
  ok(E.open(), 'once Nunba answers, the ceremony is hosted');
})();

// ── [7] the owner's seal on record ─────────────────────────────────────────
(function sealOnRecord() {
  console.log('\n[7] onboarded=true from the server: no overlay');
  const E = setup({ onboarded: true, companion_ready: true });
  ok(!E.open(), 'a sealed owner never sees the ceremony again');
  eq(E.R.timers.length, 0, 'and nothing is re-asked');
})();

// ── [8] the poll under the storage event ───────────────────────────────────
(function pollFloor() {
  console.log('\n[8] the 2 s poll closes the overlay when the flag flips without an event');
  const E = setup({ onboarded: false, companion_ready: true });
  ok(E.R.intervals.some((i) => i.ms === 2000), 'a 2 s poll runs while the overlay is open');
  E.R.tick();
  ok(E.open(), 'unsealed: still open after a tick');
  E.R.sandbox.localStorage.setItem('hart_sealed', 'true');
  E.R.tick();
  ok(!E.open(), 'sealed: the poll finished the overlay');
})();

function overlayText(root, txt) {
  const out = [];
  (function walk(n) { for (const k of n._kids || []) { if (k.textContent === txt) out.push(k); walk(k); } })(root);
  return out[0] || null;
}

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

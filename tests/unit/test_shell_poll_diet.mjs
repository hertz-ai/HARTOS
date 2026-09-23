/*
 * Behavioural test for the shell's idle POLL DIET (client half).
 *
 * MEASURED on the box 2026-09-22: ~18 GETs per 5 s at idle, from four pollers
 * in every shell document (system/metrics 4 s in hartSessionUI.js, ai-sensing
 * 4 s in hartSenses.js, connectivity/summary 8 s in hartConnectivity.js,
 * dashboard/agents 5 s in the inline shell). The server now PUSHES each of
 * those as `shell_state` events on the SSE stream the shell already holds, the
 * inline script publishes them on window.HartShellState, and the modules
 * subscribe. What is left of the polls is a 30 s fallback that runs only while
 * the stream is down, and only in the HOST document.
 *
 * Drives the REAL modules and the REAL inline registry + agent-status block
 * (sliced from the rendered shell) on the shared shim, and asserts the
 * OBSERVABLE side effects: how many fetches are issued per tick with the
 * stream up and down, what cadence each fallback asked for, what a push paints,
 * and that an iframed document registers no poller at all.
 *
 * Run:  node tests/unit/test_shell_poll_diet.mjs
 */
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeRealm, makeEl, flush } from './shell_dom_shim.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const STATIC = join(ROOT, 'integrations', 'agent_engine', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

const FALLBACK_MIN_MS = 30000;   // the idle HTTP budget: no fetching interval under this

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── the real inline registry + agent-status block, sliced from the served shell ──
function renderShell() {
  const PY = [process.env.HART_TEST_PYTHON, 'C:/Users/sathi/miniconda3/python.exe', 'python', 'python3'].filter(Boolean);
  const RENDER = "import sys; sys.path.insert(0,'.');" +
    "from integrations.agent_engine.liquid_ui_service import LiquidUIService;" +
    "(getattr(sys.stdout,'reconfigure',lambda **k:None))(encoding='utf-8');" +
    "print(LiquidUIService().render_desktop_shell())";
  for (const py of PY) {
    try { return execFileSync(py, ['-c', RENDER], { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] }); }
    catch (e) { /* next interpreter */ }
  }
  return null;
}
const HTML = renderShell();
ok(!!HTML, 'rendered the real desktop shell through python');
function inlineBus() {
  const s = HTML.indexOf('window.HartShellState = (function()');
  const e = HTML.indexOf('// ═══ Start Menu ═══');
  ok(s >= 0 && e > s, 'sliced the inline shell-state bus + agent status block');
  return HTML.slice(s, e);
}
function perfOf(name) {
  const m = new RegExp(name + ':\\s*(\\d+)').exec(HTML);
  return m ? parseInt(m[1], 10) : NaN;
}

// A fetch that records URLs and resolves with canned JSON per endpoint.
function makeFetch(answers) {
  const calls = [];
  function fetchImpl(url) {
    calls.push(String(url));
    for (const k of Object.keys(answers)) {
      if (String(url).indexOf(k) >= 0) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(answers[k]) });
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
  }
  return { calls, fetchImpl, count(frag) { return calls.filter((u) => u.indexOf(frag) >= 0).length; } };
}

function bootHost(opts) {
  opts = opts || {};
  const R = makeRealm();
  if (opts.iframe) R.sandbox.top = {};            // window.self !== window.top
  const F = makeFetch({
    'system/metrics': { cpu_percent: 7, ram: { percent: 41 }, disks: [{ percent: 63 }] },
    'ai-sensing': { disabled: { mic: false, camera: false, screen: false }, proof: { camera_service_running: false } },
    'connectivity/summary': { wifi: { available: true, enabled: true, connected: true, signal: 90 } },
    'dashboard/agents': { agents: [{ name: 'Researcher', status: 'running' }] }
  });
  R.sandbox.fetch = F.fetchImpl;
  // Inline-script globals the sliced block and the modules reach for.
  R.sandbox.BACKEND = 'http://backend';
  R.sandbox.SHELL = '';
  R.sandbox._sig = () => null;
  R.sandbox.PERF = { potato: false, clockMs: 1000, agentStatusMs: perfOf('agentStatusMs') };
  R.sandbox.showToast = () => {};
  R.sandbox.HartSession = { ready(cb) { cb({}); }, get() { return undefined; }, set() {} };
  // DOM the four consumers paint into.
  const topbar = makeEl('div', R.state); topbar.setAttribute('class', 'top-bar-right'); R.body.appendChild(topbar);
  R.el('agent-status'); R.el('hart-widgets'); R.el('hw-sys-body'); R.el('hc-net-list');
  const pod = R.el('hart-senses'); R.el('hart-senses-panel', pod); const eye = R.el('hart-senses-btn', pod); R.el('hart-senses-proof', pod);
  R.el('hart-hero'); R.el('lock-screen'); R.el('lock-pw'); R.el('lock-status');
  R.run(read('hartDismiss.js'), 'hartDismiss.js');
  R.run(inlineBus(), 'inline-shell-state.js');
  R.run(read('hartSessionUI.js'), 'hartSessionUI.js');
  R.run(read('hartSenses.js'), 'hartSenses.js');
  R.run(read('hartConnectivity.js'), 'hartConnectivity.js');
  R.flushTimers();
  return { R, F, eye,
    bus: R.window.HartShellState,
    fetching() { return R.intervals.filter((i) => i.fn.toString().indexOf('sseUp') >= 0); } };
}

// ════════════════════════════════════════════════════════════════════════════
console.log('\n[A] the host document with the stream UP issues no idle GET');
{
  const S = await (async () => { const s = bootHost(); await flush(); return s; })();
  const n0 = S.F.calls.length;
  ok(n0 <= 4, 'first paint costs at most one GET per consumer (got ' + n0 + ')');
  S.bus.setSse(true);
  for (let i = 0; i < 20; i++) S.R.tick();
  await flush();
  eq(S.F.calls.length - n0, 0, '20 fallback ticks with the stream up: ZERO GETs');
}

console.log('\n[B] with the stream DOWN the fallback polls, at a slow cadence');
{
  const S = await (async () => { const s = bootHost(); await flush(); return s; })();
  const n0 = S.F.calls.length;
  S.bus.setSse(false);
  S.R.tick();
  await flush();
  const perTick = S.F.calls.length - n0;
  ok(perTick >= 4, 'one tick with the stream down: every consumer polls once (got ' + perTick + ')');
  const pollers = S.fetching();
  eq(pollers.length, 4, 'exactly four fallback pollers in the host document (metrics, senses, connectivity, agents)');
  for (const p of pollers) ok(p.ms >= FALLBACK_MIN_MS, 'fallback cadence >= ' + FALLBACK_MIN_MS + ' ms (got ' + p.ms + ')');
  for (const i of S.R.intervals) ok(i.ms >= 200 || i.fn.toString().indexOf('fetch') < 0, 'no interval faster than 200 ms (got ' + i.ms + ')');
}

console.log('\n[C] a push paints without any GET');
{
  const S = await (async () => { const s = bootHost(); await flush(); return s; })();
  S.bus.setSse(true);
  const n0 = S.F.calls.length;
  S.bus.publish('metrics', { cpu_percent: 42, ram: { percent: 55 }, disk_percent: 71 });
  const sys = S.R.document.getElementById('hw-sys-body').innerHTML;
  ok(sys.indexOf('42%') >= 0 && sys.indexOf('55%') >= 0 && sys.indexOf('71%') >= 0, 'metrics push paints CPU / Memory / Disk bars');
  S.bus.publish('senses', { disabled: { mic: true, camera: true, screen: true }, proof: { camera_service_running: false } });
  ok(S.eye.classList.contains('off'), 'senses push paints the eye SHUT');
  S.bus.publish('senses', { disabled: { mic: false, camera: false, screen: false }, proof: { camera_service_running: false } });
  ok(S.eye.classList.contains('is-sensing') && !S.eye.classList.contains('off'), 'senses push paints the eye sensing again');
  S.bus.publish('agents', { count: 2, names: ['Researcher', 'Writer'] });
  const bar = S.R.document.getElementById('agent-status').innerHTML;
  ok(bar.indexOf('Researcher') >= 0 && bar.indexOf('agent-chip') >= 0, 'agents push paints the top-bar chips');
  S.bus.publish('agents', { count: 0, names: [] });
  ok(S.R.document.getElementById('agent-status').innerHTML.indexOf('No agents') >= 0, 'an empty agents push paints the empty state');
  S.bus.publish('connectivity', { wifi: { available: true, enabled: true, connected: true, ssid: 'PushedNet', signal: 95 } });
  eq(S.F.calls.length - n0, 0, 'none of that cost a GET');
  // The tray glyphs are innerHTML strings (the shim keeps no children for
  // those), so read the connectivity paint through the popover it renders
  // from the same STATE. Opening it is a user action and refreshes once.
  S.R.window.HartConnectivity.open();
  const pop = S.R.document.getElementById('hc-popover').innerHTML;
  ok(pop.indexOf('PushedNet') >= 0 && pop.indexOf('>wifi<') >= 0, 'connectivity push painted the Wi-Fi state (ssid + full-signal glyph)');
}

console.log('\n[D] a late subscriber gets the last pushed state (no GET to catch up)');
{
  const R = makeRealm();
  R.run(inlineBus().slice(0, inlineBus().indexOf('// ═══ Agent Status')), 'inline-bus-only.js');
  const bus = R.window.HartShellState;
  bus.publish('senses', { disabled: { mic: true } });
  let got = null;
  bus.on('senses', (p) => { got = p; });
  ok(got && got.disabled.mic === true, 'on() replays the last payload to a late subscriber');
  eq(bus.last('nothing'), undefined, 'last() of an unknown kind is undefined');
  ok(bus.isHost() === true, 'a top-level document is the host');
}

console.log('\n[E] an iframed shell document runs NO poller (the host owns them)');
{
  const S = await (async () => { const s = bootHost({ iframe: true }); await flush(); return s; })();
  eq(S.fetching().length, 0, 'no fallback poller registered in an iframe document');
  eq(S.F.calls.length, 0, 'no first-paint GET either: an iframed copy never fetches state');
  ok(S.bus.isHost() === false, 'the bus reports non-host');
}

console.log('\n[F] the clocks write the DOM only when the text changes');
{
  const R = makeRealm();
  R.el('lock-screen'); R.el('lock-pw'); R.el('lock-status'); R.el('hart-widgets'); R.el('hw-sys-body');
  const clock = R.el('hw-clock-time');
  let writes = 0;
  Object.defineProperty(clock, 'textContent', { get() { return this._t; }, set(v) { this._t = v; writes++; } });
  R.sandbox.fetch = () => new Promise(() => {});
  R.sandbox.HartSession = { ready(cb) { cb({}); }, get() { return undefined; }, set() {} };
  R.sandbox.top = {};   // iframe: keeps the metrics poller out of the way; the clock still ticks
  R.run(read('hartSessionUI.js'), 'hartSessionUI.js');
  const before = writes;
  for (let i = 0; i < 5; i++) R.tick();
  eq(writes - before, 0, 'five 1 s ticks inside the same minute write nothing (was: four DOM writes per second)');
}

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

/*
 * Behavioural test for the app-integration bridge SDK surface
 * (integrations/agent_engine/static/hartOSBridge.js -> hartOS.apps) — #117 / W3.
 *
 * The apps SDK is the WebView side of the typed app bridge:
 *   hartOS.apps.launch('firefox', 'linux')
 *     -> POST /api/os/invoke {domain:'apps', op:'launch', params:{app_id, subsystem}}
 * The OS server routes launch to the app_bridge cross-subsystem dispatch and
 * RESULT-CHECKS it. This proves the client-side envelope + the #133 invariant
 * (a failed launch REJECTS, never a masked success) on the REAL module, with fetch
 * MOCKED — no source-string assertions (the no-grep-test rule).
 *
 * Run:  node tests/unit/test_os_bridge_apps_sdk.mjs
 * (A pytest wrapper, test_os_bridge_apps_sdk.py, shells out so CI picks it up too.)
 *
 * WEBKIT-SAFE: no backticks / template literals anywhere (string concat only).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartOSBridge.js');
const CODE = readFileSync(SRC, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

function resp(okFlag, status, jsonData) {
  return { ok: okFlag, status: status, json: () => Promise.resolve(jsonData) };
}

function makeRealm(planner) {
  const calls = [];
  function fetchImpl(url, opts) {
    calls.push({ url: url, opts: opts || {} });
    return Promise.resolve(planner(url, opts || {}));
  }
  const sandbox = { console, fetch: fetchImpl };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'hartOSBridge.js' });
  return { hartOS: sandbox.window.hartOS, calls };
}

// ════════════════════════════════════════════════════════════════════════════
// A. apps.launch(appId, subsystem) posts the typed envelope + resolves on ok:true
// ════════════════════════════════════════════════════════════════════════════
async function testLaunchEnvelope() {
  console.log('\n[A] apps.launch(appId, subsystem) -> POST {apps, launch, {app_id, subsystem}}');
  const R = makeRealm(() => resp(true, 200, { ok: true, op: 'launch', app_id: 'firefox' }));
  const data = await R.hartOS.apps.launch('firefox', 'linux');
  eq(data.ok, true, 'launch() resolves with the server ok:true payload');
  eq(R.calls.length, 1, 'exactly one fetch issued');
  eq(R.calls[0].url, '/api/os/invoke', 'posts to the ONE typed dispatcher endpoint');
  eq(R.calls[0].opts.method, 'POST', 'uses POST');
  const body = JSON.parse(R.calls[0].opts.body);
  eq(body.domain, 'apps', 'typed domain = apps');
  eq(body.op, 'launch', 'typed op = launch');
  eq(body.params.app_id, 'firefox', 'app_id carried in typed params');
  eq(body.params.subsystem, 'linux', 'subsystem carried in typed params');
}

async function testLaunchOmitsSubsystem() {
  console.log('\n[A2] apps.launch(appId) omits subsystem (server defaults to linux)');
  const R = makeRealm(() => resp(true, 200, { ok: true }));
  await R.hartOS.apps.launch('gedit');
  const body = JSON.parse(R.calls[0].opts.body);
  eq(body.params.app_id, 'gedit', 'app_id present');
  ok(!('subsystem' in body.params), 'no subsystem key when the caller omits it');
}

// ════════════════════════════════════════════════════════════════════════════
// B. list / focus / close post the right typed envelope
// ════════════════════════════════════════════════════════════════════════════
async function testListFocusClose() {
  console.log('\n[B] apps.list / focus / close send the right typed op + params');
  const L = makeRealm(() => resp(true, 200, { ok: true, windows: [] }));
  await L.hartOS.apps.list();
  eq(JSON.parse(L.calls[0].opts.body).op, 'list', 'list -> op=list');

  const F = makeRealm(() => resp(true, 200, { ok: true }));
  await F.hartOS.apps.focus(7);
  const fb = JSON.parse(F.calls[0].opts.body);
  eq(fb.op, 'focus', 'focus -> op=focus');
  eq(fb.params.window_id, 7, 'focus carries the window_id');

  const C = makeRealm(() => resp(true, 200, { ok: true }));
  await C.hartOS.apps.close(9);
  const cb = JSON.parse(C.calls[0].opts.body);
  eq(cb.op, 'close', 'close -> op=close');
  eq(cb.params.window_id, 9, 'close carries the window_id');
}

// ════════════════════════════════════════════════════════════════════════════
// C. THE #133 invariant: a failed launch REJECTS, never a masked success
// ════════════════════════════════════════════════════════════════════════════
async function testLaunchFailureRejects() {
  console.log('\n[C] a failed launch REJECTS the promise (never masked as success)');
  const R = makeRealm(() => resp(false, 500, { ok: false, op: 'launch', error: 'no such app' }));
  let threw = false, caught = null;
  try { await R.hartOS.apps.launch('nope'); } catch (e) { threw = true; caught = e; }
  ok(threw, 'launch() REJECTS when the OS could not launch the app');
  ok(caught && /no such app/.test(caught.message), 'the rejection carries the real server error');
  eq(caught && caught.status, 500, 'err.status is the HTTP status');
}

await testLaunchEnvelope();
await testLaunchOmitsSubsystem();
await testListFocusClose();
await testLaunchFailureRejects();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

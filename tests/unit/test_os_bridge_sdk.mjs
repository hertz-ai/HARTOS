/*
 * Behavioural test for the typed Shell<->OS bridge SDK
 * (integrations/agent_engine/static/hartOSBridge.js) — #133 / W3.
 *
 * The SDK is the WebView side of the typed bridge: hartOS.power.reboot() ->
 * POST /api/os/invoke {domain, op, params} -> the OS server runs logind natively
 * and RESULT-CHECKS. The one client-side invariant this proves (the whole point of
 * #133) is that a DENIAL / failure REJECTS the returned promise with a real Error —
 * it is NEVER masked as success — so a caller that does power.reboot().then(...) can
 * only reach the success path when the OS actually accepted the op.
 *
 * Per the no-grep-test rule this drives the REAL module through its public surface
 * on a tiny dependency-free sandbox (CI here has no jsdom) with fetch MOCKED, and
 * asserts the OBSERVABLE side-effects — which URL/body was posted, and whether the
 * promise resolved or rejected — rather than any source string.
 *
 * Run:  node tests/unit/test_os_bridge_sdk.mjs
 * (A pytest wrapper, test_os_bridge_sdk.py, shells out so CI picks it up too.)
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

// A Response-like object (only what the SDK's invoke()/capabilities()/contract() read).
function resp(okFlag, status, jsonData) {
  return { ok: okFlag, status: status, json: () => Promise.resolve(jsonData) };
}

// Build a fresh sandbox with the REAL SDK loaded and a controllable fetch mock.
// planner(url, opts) returns either a Response-like object, or { __reject: 'msg' }
// to model a network-level failure (fetch's promise rejecting).
function makeRealm(planner) {
  const calls = [];
  function fetchImpl(url, opts) {
    calls.push({ url: url, opts: opts || {} });
    const r = planner(url, opts || {});
    if (r && r.__reject) return Promise.reject(new Error(r.__reject));
    return Promise.resolve(r);
  }
  const sandbox = { console, fetch: fetchImpl };
  sandbox.window = sandbox;           // the SDK attaches to `window` (== global here)
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'hartOSBridge.js' });
  return { hartOS: sandbox.window.hartOS, calls };
}

// ════════════════════════════════════════════════════════════════════════════
// A. power.reboot() posts the typed op and resolves on ok:true
// ════════════════════════════════════════════════════════════════════════════
async function testRebootPostsTypedOp() {
  console.log('\n[A] power.reboot() -> POST /api/os/invoke {power, reboot}');
  const R = makeRealm(() => resp(true, 200, { ok: true, op: 'reboot' }));
  const data = await R.hartOS.power.reboot();
  eq(data.ok, true, 'reboot() resolves with the server ok:true payload');
  eq(R.calls.length, 1, 'exactly one fetch issued');
  eq(R.calls[0].url, '/api/os/invoke', 'posts to the ONE typed dispatcher endpoint');
  eq(R.calls[0].opts.method, 'POST', 'uses POST');
  const body = JSON.parse(R.calls[0].opts.body);
  eq(body.domain, 'power', 'typed domain = power');
  eq(body.op, 'reboot', 'typed op = reboot');
  ok(body.params && typeof body.params === 'object', 'params is an object (typed, not a magic string)');
}

// ════════════════════════════════════════════════════════════════════════════
// B. THE #133 client invariant: a denial REJECTS, never a masked success
// ════════════════════════════════════════════════════════════════════════════
async function testDenialRejects() {
  console.log('\n[B] a polkit denial REJECTS the promise (never masked as success)');
  const R = makeRealm(() => resp(false, 500, { ok: false, op: 'shutdown', error: 'Access denied' }));
  let threw = false, caught = null;
  try { await R.hartOS.power.shutdown(); } catch (e) { threw = true; caught = e; }
  ok(threw, 'shutdown() REJECTS when the OS denied the op');
  ok(caught && /Access denied/.test(caught.message), 'the rejection carries the REAL server error');
  eq(caught && caught.status, 500, 'err.status is the HTTP status');
  ok(caught && caught.detail && caught.detail.ok === false, 'err.detail is the server payload');
}

async function testOkFalseOn200AlsoRejects() {
  console.log('\n[B2] ok:false on a 200 still rejects (result-check, not status-check)');
  const R = makeRealm(() => resp(true, 200, { ok: false, error: 'logind Reboot timed out' }));
  let threw = false, caught = null;
  try { await R.hartOS.power.reboot(); } catch (e) { threw = true; caught = e; }
  ok(threw, 'a 200 body with ok:false is treated as a FAILURE (never a masked success)');
  ok(caught && /timed out/.test(caught.message), 'the rejection carries the real reason');
}

// ════════════════════════════════════════════════════════════════════════════
// C. firmwareSetup maps to the firmware_setup op
// ════════════════════════════════════════════════════════════════════════════
async function testFirmwareSetupMapping() {
  console.log('\n[C] power.firmwareSetup() -> op firmware_setup');
  const R = makeRealm(() => resp(true, 200, { ok: true, op: 'firmware_setup' }));
  await R.hartOS.power.firmwareSetup();
  eq(JSON.parse(R.calls[0].opts.body).op, 'firmware_setup', 'firmwareSetup() sends op=firmware_setup');
}

// ════════════════════════════════════════════════════════════════════════════
// D. capabilities() reads the caps endpoint + degrades to {} (never throws)
// ════════════════════════════════════════════════════════════════════════════
async function testCapabilities() {
  console.log('\n[D] power.capabilities() success + degrade-not-die');
  const R = makeRealm(() => resp(true, 200, { capabilities: { reboot: true, firmware_setup: false } }));
  const caps = await R.hartOS.power.capabilities();
  eq(R.calls[0].url, '/api/os/power/capabilities', 'GETs the capabilities endpoint');
  eq(caps.reboot, true, 'reflects the server capability map (reboot)');
  eq(caps.firmware_setup, false, 'reflects the server capability map (firmware_setup gated off)');

  const R2 = makeRealm(() => ({ __reject: 'network down' }));
  const caps2 = await R2.hartOS.power.capabilities();
  ok(caps2 && typeof caps2 === 'object', 'capabilities() degrades to an object on network failure');
  eq(Object.keys(caps2).length, 0, 'capabilities() is {} on failure — never throws');
}

// ════════════════════════════════════════════════════════════════════════════
// E. contract() introspects the self-describing manifest
// ════════════════════════════════════════════════════════════════════════════
async function testContract() {
  console.log('\n[E] hartOS.contract() -> GET /api/os/contract');
  const R = makeRealm(() => resp(true, 200, { version: 1, domains: { power: { implemented: true } } }));
  const c = await R.hartOS.contract();
  eq(R.calls[0].url, '/api/os/contract', 'GETs the contract manifest');
  ok(c && c.domains && c.domains.power, 'returns the self-describing manifest');
}

await testRebootPostsTypedOp();
await testDenialRejects();
await testOkFalseOn200AlsoRejects();
await testFirmwareSetupMapping();
await testCapabilities();
await testContract();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

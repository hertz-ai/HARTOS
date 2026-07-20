/*
 * Behavioural test for the HART OS connectivity cluster IN-FLIGHT GUARDS
 * (integrations/agent_engine/static/hartConnectivity.js).
 *
 * THE BUG it guards: on a software-rendered box the shell server runs on a 1-2
 * thread pool and each connectivity/summary + network/wifi probe shells out to
 * nmcli/bluetoothctl/wpctl, taking several seconds. The 8s poller, popover-open,
 * toggles and every Rescan all re-trigger those fetches; without a guard a slow
 * probe pile-up saturates the pool and freezes every other shell fetch. Aborting
 * the client fetch does NOT cancel the server subprocess, so the only safe lever
 * is: never STACK a new request on a pending one.
 *
 * This drives the REAL module through its public surface on a tiny dependency-free
 * DOM shim (CI here has no jsdom) and asserts the OBSERVABLE side-effect — the
 * number of fetches actually issued — rather than any source string:
 *
 *   A. refresh()      — a second call while the first summary probe is still
 *                       pending issues NO new fetch (coalesced); once the pending
 *                       probe settles the guard RELEASES and the next call fetches.
 *   B. loadNetworks() — same contract for the wifi scan: a re-render / Rescan while
 *                       a scan is in flight is coalesced; it releases on settle.
 *
 * Run:  node tests/unit/test_shell_connectivity_inflight.mjs
 * (A Python wrapper, test_shell_connectivity_inflight.py, shells out so pytest/CI
 *  picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartConnectivity.js');
const CODE = readFileSync(SRC, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// Drain all pending microtasks (the fetch().then() chains). A macrotask boundary
// (setImmediate) fires only after every queued microtask has run.
const flush = () => new Promise((r) => setImmediate(r));

// ── Minimal DOM shim (only what hartConnectivity.js touches) ─────────────────
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, style: {},
    textContent: '', _innerHTML: '', parentNode: null,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) { const on = (force === undefined) ? !this._s.has(c) : !!force;
        if (on) this._s.add(c); else this._s.delete(c); return on; } },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; },   // string assign drops kids
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    addEventListener() {}, removeEventListener() {},
    contains() { return false; },
    getBoundingClientRect() { return { left: 0, top: 0, right: 60, bottom: 30, width: 60, height: 30 }; },
    querySelector(sel) {
      const cls = sel.replace(/^[.#]/, '');
      for (const k of el._kids) {
        if (sel[0] === '#' && k._attrs.id === cls) return k;
        if (sel[0] === '.' && (k._attrs.class || '').split(' ').indexOf(cls) >= 0) return k;
      }
      return null;
    },
    querySelectorAll() { return []; },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; }
  };
  return el;
}

function makeRealm() {
  const registry = {};
  const body = makeEl('body');
  const head = makeEl('head');
  function findById(node, id) {
    for (const k of node._kids) {
      if (k._attrs.id === id) return k;
      const f = findById(k, id);
      if (f) return f;
    }
    return null;
  }
  function findByClass(node, cls, out) {
    for (const k of node._kids) {
      if ((k._attrs.class || '').split(' ').indexOf(cls) >= 0) out.push(k);
      findByClass(k, cls, out);
    }
    return out;
  }
  const document = {
    readyState: 'complete',   // -> the module runs mount() immediately
    body, head,
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || findById(body, id) || findById(head, id) || null; },
    querySelector(sel) {
      if (sel[0] === '.') { const out = []; findByClass(body, sel.slice(1), out); return out[0] || null; }
      if (sel[0] === '#') return this.getElementById(sel.slice(1));
      return null;
    },
    addEventListener() {}, removeEventListener() {}
  };
  // Register an element addressable by getElementById (mirrors a real id in the tree).
  const reg = (id) => { const e = makeEl('div'); e.setAttribute('id', id); registry[id] = e; body.appendChild(e); return e; };

  const intervals = [];
  const sandbox = {
    document, console,
    HartTimeoutSignal() { return null; },   // -> sig() returns null, no AbortController needed
    setInterval(fn) { intervals.push(fn); return intervals.length; },
    clearInterval() {},
    setTimeout() { return 0; },              // capture-and-drop: avoids any inline re-entrancy
    clearTimeout() {},
    addEventListener() {}, removeEventListener() {}
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, body, reg, intervals };
}

// A fetch mock: one deferred per call, bucketed by URL, so the test controls when
// (and whether) each settles and can count how many were actually issued.
function makeFetch() {
  const summary = [];   // deferreds for connectivity/summary
  const wifi = [];      // deferreds for network/wifi (scan)
  function deferred() { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return { p, resolve: res, reject: rej }; }
  function fetchImpl(url) {
    const d = deferred();
    if (url.indexOf('connectivity/summary') >= 0) summary.push(d);
    else if (url.indexOf('network/wifi') >= 0) wifi.push(d);
    return d.p;
  }
  function resolveJSON(d, data) { d.resolve({ ok: true, json: () => Promise.resolve(data) }); }
  return { fetchImpl, summary, wifi, resolveJSON };
}

// ════════════════════════════════════════════════════════════════════════════
// A. refresh() in-flight guard
// ════════════════════════════════════════════════════════════════════════════
async function testRefreshGuard() {
  console.log('\n[A] refresh()  summary probe in-flight guard');
  const R = makeRealm();
  const F = makeFetch();
  R.sandbox.fetch = F.fetchImpl;
  const topbar = makeEl('div'); topbar.setAttribute('class', 'top-bar-right'); R.body.appendChild(topbar);

  vm.runInContext(CODE, R.sandbox, { filename: 'hartConnectivity.js' });
  // mount() ran (readyState complete) and fired ONE summary probe (still pending).
  eq(F.summary.length, 1, 'mount fires exactly one summary probe');

  // A second refresh while the first is pending must NOT issue a new fetch.
  R.sandbox.window.HartConnectivity.refresh();
  R.sandbox.window.HartConnectivity.refresh();
  eq(F.summary.length, 1, 'refresh() while a probe is pending is COALESCED (no stacked fetch)');

  // Settle the pending probe -> the guard releases.
  F.resolveJSON(F.summary[0], { wifi: { available: true, enabled: true } });
  await flush();
  R.sandbox.window.HartConnectivity.refresh();
  eq(F.summary.length, 2, 'after the pending probe settles, the guard RELEASES (next refresh fetches)');
}

// ════════════════════════════════════════════════════════════════════════════
// B. loadNetworks() in-flight guard (driven through openPopover -> renderPopover)
// ════════════════════════════════════════════════════════════════════════════
async function testLoadNetworksGuard() {
  console.log('\n[B] loadNetworks()  wifi scan in-flight guard');
  const R = makeRealm();
  const F = makeFetch();
  R.sandbox.fetch = F.fetchImpl;
  const topbar = makeEl('div'); topbar.setAttribute('class', 'top-bar-right'); R.body.appendChild(topbar);
  R.reg('hc-net-list');   // loadNetworks writes/queries this box

  vm.runInContext(CODE, R.sandbox, { filename: 'hartConnectivity.js' });
  // Resolve the mount probe so STATE.wifi is available+enabled (loadNetworks needs it).
  F.resolveJSON(F.summary[0], { wifi: { available: true, enabled: true } });
  await flush();

  // Opening the popover renders it and calls loadNetworks(false) -> one scan fetch.
  R.sandbox.window.HartConnectivity.open();
  eq(F.wifi.length, 1, 'opening the popover fires exactly one wifi scan');

  // Re-render / re-open while the scan is pending must NOT stack a second scan.
  R.sandbox.window.HartConnectivity.open();
  R.sandbox.window.HartConnectivity.open();
  eq(F.wifi.length, 1, 'loadNetworks() while a scan is pending is COALESCED (no stacked fetch)');

  // Settle the scan -> the guard releases; the next open scans again.
  F.resolveJSON(F.wifi[0], { networks: [] });
  await flush();
  R.sandbox.window.HartConnectivity.open();
  eq(F.wifi.length, 2, 'after the scan settles, the guard RELEASES (next open scans)');
}

await testRefreshGuard();
await testLoadNetworksGuard();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

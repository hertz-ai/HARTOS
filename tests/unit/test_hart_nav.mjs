/*
 * Behavioural test for the unified navigation core
 * (integrations/agent_engine/static/hartNav.js) — #169.
 *
 * hartNav.js is a plain classic IIFE that (a) exports a pure, DOM-free history +
 * reuse-vs-new-instance CORE on window.HartNavCore, then (b) wires it to the DOM.
 * When loaded in a realm with NO `document`, the file returns before touching the
 * DOM, leaving just the pure core — so we can drive its REAL logic headlessly
 * (no jsdom) and assert observable behaviour, per the no-grep-test rule.
 *
 * We assert the OBSERVABLE navigation contract:
 *   - history push / back / forward / canBack / canForward
 *   - a new navigation from the middle truncates the forward (redo) tail
 *   - re-navigating to the current id is a no-op (reuse: no duplicate entry)
 *   - remove() (a closed panel) drops its entries and keeps the pointer valid
 *   - decideOpen(): reuse an open panel, create a first open, and mint a NEW
 *     instance id (base#N) under {newInstance:true}
 *
 * Run:  node tests/unit/test_hart_nav.mjs
 * (test_hart_nav.py shells out so pytest/CI picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartNav.js');
const CODE = readFileSync(SRC, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// Load the REAL module in a realm with a `window` but NO `document`, so only the
// pure core evaluates (the DOM wiring self-skips).
function loadCore() {
  const sandbox = { console };
  sandbox.window = sandbox;          // the IIFE attaches to window (== global here)
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox, { filename: 'hartNav.js' });
  ok(typeof sandbox.window.HartNavCore === 'function', 'HartNavCore factory is exported for headless use');
  ok(typeof sandbox.window.HartNav === 'undefined', 'the DOM wiring (HartNav) is skipped when there is no document');
  return sandbox.window.HartNavCore;
}

const HartNavCore = loadCore();

// ════════════════════════════════════════════════════════════════════════════
// A. push / back / forward + the canBack/canForward guards
// ════════════════════════════════════════════════════════════════════════════
function testHistoryLinear() {
  console.log('\n[A] linear push -> back -> forward');
  const h = HartNavCore();
  eq(h.canBack(), false, 'empty history: canBack is false');
  eq(h.canForward(), false, 'empty history: canForward is false');
  eq(h.current(), null, 'empty history: current is null');

  h.push({ id: 'agents', title: 'Agents' });
  h.push({ id: 'recipes', title: 'Recipes' });
  h.push({ id: 'social', title: 'Communities' });
  eq(h.current().id, 'social', 'current is the last pushed');
  eq(h.canBack(), true, 'canBack after 3 pushes');
  eq(h.canForward(), false, 'no forward at the tip');

  eq(h.back().id, 'recipes', 'back() -> recipes');
  eq(h.back().id, 'agents', 'back() -> agents');
  eq(h.canBack(), false, 'at the head: canBack false');
  eq(h.back().id, 'agents', 'back() at the head is a clamped no-op');

  eq(h.forward().id, 'recipes', 'forward() -> recipes');
  eq(h.canForward(), true, 'canForward mid-stack');
  eq(h.forward().id, 'social', 'forward() -> social (the tip)');
  eq(h.canForward(), false, 'no forward past the tip');
}

// ════════════════════════════════════════════════════════════════════════════
// B. a NEW navigation from the middle truncates the forward (redo) tail
// ════════════════════════════════════════════════════════════════════════════
function testForwardTruncation() {
  console.log('\n[B] pushing after a back() discards the redo tail');
  const h = HartNavCore();
  h.push({ id: 'a', title: 'A' });
  h.push({ id: 'b', title: 'B' });
  h.push({ id: 'c', title: 'C' });
  h.back();                                  // now at B, redo tail = [C]
  eq(h.current().id, 'b', 'positioned at B');
  h.push({ id: 'd', title: 'D' });           // new nav -> C is discarded
  eq(h.current().id, 'd', 'current is D');
  eq(h.canForward(), false, 'the C redo entry was truncated');
  const ids = h.stack().map(function (e) { return e.id; });
  eq(ids.join(','), 'a,b,d', 'stack is a,b,d (C dropped)');
}

// ════════════════════════════════════════════════════════════════════════════
// C. re-navigating to the CURRENT id is a reuse no-op (no duplicate entry)
// ════════════════════════════════════════════════════════════════════════════
function testReusePushNoop() {
  console.log('\n[C] pushing the current id again does not duplicate it');
  const h = HartNavCore();
  h.push({ id: 'agents', title: 'Agents' });
  h.push({ id: 'agents', title: 'Agents (refreshed)' });
  eq(h.stack().length, 1, 'still one entry');
  eq(h.current().title, 'Agents (refreshed)', 'title is refreshed in place');
}

// ════════════════════════════════════════════════════════════════════════════
// D. remove() drops a closed panel and keeps the pointer valid
// ════════════════════════════════════════════════════════════════════════════
function testRemoveKeepsPointerValid() {
  console.log('\n[D] remove(current) leaves a valid pointer on a still-open panel');
  const h = HartNavCore();
  h.push({ id: 'a', title: 'A' });
  h.push({ id: 'b', title: 'B' });
  h.push({ id: 'c', title: 'C' });          // at C (idx 2)
  h.remove('c');                             // close the focused panel
  ok(h.current() !== null, 'current is still a real entry after closing C');
  eq(h.current().id, 'b', 'pointer fell back to B');
  eq(h.canForward(), false, 'nothing forward of B now');

  h.remove('a');                             // close a non-current panel
  eq(h.current().id, 'b', 'closing A keeps current on B');
  eq(h.stack().length, 1, 'only B remains');

  h.remove('b');                             // close the last one
  eq(h.current(), null, 'empty again after the last panel closes');
  eq(h.canBack(), false, 'canBack false on empty');
}

// ════════════════════════════════════════════════════════════════════════════
// E. decideOpen(): reuse vs first-create vs explicit new-instance
// ════════════════════════════════════════════════════════════════════════════
function testDecideOpen() {
  console.log('\n[E] reuse-vs-new-instance decision');
  const h = HartNavCore();

  const first = h.decideOpen('agents', {}, []);
  eq(first.action, 'create', 'opening a not-yet-open panel -> create');
  eq(first.instanceId, 'agents', 'first open uses the plain base id');

  const reuse = h.decideOpen('agents', {}, ['agents']);
  eq(reuse.action, 'focus', 'opening an already-open panel -> focus (reuse)');
  eq(reuse.instanceId, 'agents', 'reuse targets the same id');

  const second = h.decideOpen('agents', { newInstance: true }, ['agents']);
  eq(second.action, 'create', 'newInstance -> create');
  eq(second.instanceId, 'agents#2', 'newInstance mints agents#2 when agents is open');

  const third = h.decideOpen('agents', { newInstance: true }, ['agents', 'agents#2']);
  eq(third.instanceId, 'agents#3', 'newInstance mints agents#3 when #1+#2 are open');

  const freshNew = h.decideOpen('files', { newInstance: true }, []);
  eq(freshNew.instanceId, 'files', 'newInstance with nothing open uses the plain base id');
}

// ════════════════════════════════════════════════════════════════════════════
// F. nextInstance() sequence is independent per base
// ════════════════════════════════════════════════════════════════════════════
function testNextInstance() {
  console.log('\n[F] nextInstance mints per-base ids');
  const h = HartNavCore();
  eq(h.nextInstance('recipes', ['agents', 'agents#2']), 'recipes',
    'a base with nothing open -> the plain id (unaffected by other bases)');
  eq(h.nextInstance('agents', ['agents', 'agents#2', 'recipes']), 'agents#3',
    'skips over an unrelated open base when counting');
}

testHistoryLinear();
testForwardTruncation();
testReusePushNoop();
testRemoveKeepsPointerValid();
testDecideOpen();
testNextInstance();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

/**
 * The orb's animation loop must STOP when the compositor owns the orb.
 *
 * THE DEFECT, measured on the box 2026-09-07. When the native-chrome verdict
 * claims 'orb', the served shell sets the orb canvas visibility:hidden so the
 * compositor's native orb shows through. But CSS only stops an element
 * PAINTING; it cannot stop a script. voiceOrbViz drives its canvas from a
 * self-perpetuating requestAnimationFrame loop, and rAF throttling keys off
 * DOCUMENT visibility, not element visibility -- so the loop kept running at
 * full rate, computing trig and stroking paths into a surface nobody would
 * ever see. WebKitWebProcess held a full core (1188 CPU ticks in 12s) while
 * the compositor reported zero page flips.
 *
 * This drives the REAL shipped module with a fake canvas and counts how many
 * frames it actually renders. No source grepping: a grep would have passed
 * before the fix, because the CSS rule it would have found was already there.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', '..', 'integrations', 'agent_engine',
                      'static', 'voiceOrbViz.js');

let pass = 0, fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log('  [PASS] ' + label); }
  else { fail++; console.log('  [FAIL] ' + label + (detail ? ' :: ' + detail : '')); }
}

// A canvas 2d context that records nothing but answers every call.
function fakeCtx() {
  const noop = () => {};
  return new Proxy({}, {
    get(_t, k) {
      if (k === 'canvas') return { width: 200, height: 200 };
      if (k === 'createLinearGradient' || k === 'createRadialGradient') {
        return () => ({ addColorStop: noop });
      }
      if (k === 'getImageData') return () => ({ data: new Uint8ClampedArray(4) });
      if (k === 'measureText') return () => ({ width: 10 });
      return noop;
    },
    set() { return true; },
  });
}

function makeGlobal(claim) {
  let frames = 0;
  const g = {
    HART_NATIVE_CHROME: claim,
    devicePixelRatio: 1,
    requestAnimationFrame(cb) { frames++; g.__cb = cb; return frames; },
    cancelAnimationFrame() {},
    document: { hidden: false },
    console: { debug() {}, error() {}, log() {} },
  };
  g.__frames = () => frames;
  return g;
}

function loadModule(g) {
  const src = fs.readFileSync(SRC, 'utf8');
  // The module is a UMD-ish IIFE taking the global; run it with ours bound as
  // both `this` and `globalThis`-alike so `global.requestAnimationFrame` and
  // `global.HART_NATIVE_CHROME` resolve to the fake.
  const fn = new Function('global', 'window', 'self', 'document',
                          src + '\nreturn global.HartVoiceOrbViz;');
  return fn(g, g, g, g.document);
}

function makeCanvas() {
  const ctx = fakeCtx();
  return {
    width: 200, height: 200,
    getContext: () => ctx,
    getBoundingClientRect: () => ({ width: 200, height: 200, left: 0, top: 0 }),
    addEventListener: () => {},
    removeEventListener: () => {},
    style: {},
    parentNode: { clientWidth: 200, clientHeight: 200 },
  };
}

console.log('=== compositor does NOT own the orb: the loop must run ===');
{
  const g = makeGlobal([]);
  const create = loadModule(g);
  check('module exposes a factory', typeof create === 'function');
  if (typeof create === 'function') {
    create(makeCanvas(), {});
    // Pump a few frames the way a browser would.
    for (let i = 0; i < 5 && g.__cb; i++) { const cb = g.__cb; g.__cb = null; cb(0); }
    check('it keeps animating when the shell owns the orb',
          g.__frames() > 1, 'frames=' + g.__frames());
  }
}

console.log('=== compositor OWNS the orb: the loop must stand down ===');
{
  const g = makeGlobal(['bloom', 'orb']);
  const create = loadModule(g);
  if (typeof create === 'function') {
    create(makeCanvas(), {});
    for (let i = 0; i < 5 && g.__cb; i++) { const cb = g.__cb; g.__cb = null; cb(0); }
    check('it schedules NO frames at all',
          g.__frames() === 0, 'frames=' + g.__frames());
  }
}

console.log('=== an unreadable claim must not blank the orb ===');
{
  const g = makeGlobal(undefined);
  const create = loadModule(g);
  if (typeof create === 'function') {
    create(makeCanvas(), {});
    for (let i = 0; i < 3 && g.__cb; i++) { const cb = g.__cb; g.__cb = null; cb(0); }
    check('unknown claim keeps drawing (the safe direction)',
          g.__frames() > 1, 'frames=' + g.__frames());
  }
}

console.log('');
console.log(fail === 0 ? 'RESULT: ALL PASS' : 'RESULT: ' + fail + ' FAILED');
process.exit(fail === 0 ? 0 : 1);

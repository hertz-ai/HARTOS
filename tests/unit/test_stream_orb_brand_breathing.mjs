/*
 * Behavioural tests for the STREAM-orb redesign — the LIVING, BREATHING brand
 * centerpiece. These drive the REAL static modules through their public surface
 * and assert OBSERVABLE side-effects, not source substrings (CLAUDE.md Gate 5 /
 * feedback_no_grep_tests.md). They guard the four redesign intents:
 *
 *  A. voiceOrbViz.js EDGELESS + BRAND SPECTRUM — run the real canvas renderer on a
 *     recording 2D context and assert: it NEVER strokes (no ring outlines), it NEVER
 *     sets a flat colour string as fillStyle (every body is a gradient -> no solid
 *     outer disc), at least one gradient stop is fully transparent at the rim (alpha
 *     0 at offset 1 -> radial alpha falloff that merges with the background), the
 *     off-brand purple rgba(108,99,255) is GONE, and the Hevolve brand spectrum
 *     (teal/cyan + blue/violet + magenta) is actually painted (iridescent, not flat).
 *
 *  B. voiceOrbViz.js BREATHING — pump > 5s of frames in IDLE (energy decays to 0 so
 *     the core radius is a clean function of the breath alone) and assert the core
 *     rises AND falls (a slow oscillation, not a frozen disc), then prove the breath
 *     INTENSIFIES with energy (active synthetic energy yields a larger peak core than
 *     idle).
 *
 *  C. hartHero.js FLOAT OVER WINDOWS — run the real hero module on a tiny DOM shim
 *     and assert place() (the single style writer) stamps a high z-index on #hart-hero
 *     so the orb floats ABOVE app windows (focused .panel reaches z 999) yet stays
 *     BELOW the persistent system chrome (ctx-menu z 3000, taskbar z 8000), keeps
 *     pointer-events reachable, and NEVER calls focus() during init (never traps focus).
 *
 * Run:  node tests/unit/test_stream_orb_brand_breathing.mjs
 * (A Python wrapper, test_stream_orb_brand_breathing.py, shells out so pytest/CI
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

// ── A recording 2D canvas context ───────────────────────────────────────────
// Real enough for voiceOrbViz: it captures every gradient colour-stop, every arc
// radius (grouped per frame at each clearRect boundary), every fillStyle TYPE, and
// any stroke/strokeStyle/composite use, so the test can assert the drawn shape.
function makeRecorder() {
  const rec = { stops: [], strokeCalls: 0, strokeStyleSets: 0, fillStyleTypes: [], gco: [], frameArcs: [] };
  let curFrame = null;
  const ctx = {
    _fill: null,
    set fillStyle(v) { rec.fillStyleTypes.push(typeof v); this._fill = v; },
    get fillStyle() { return this._fill; },
    set strokeStyle(v) { rec.strokeStyleSets++; },
    get strokeStyle() { return null; },
    set globalCompositeOperation(v) { rec.gco.push(v); },
    get globalCompositeOperation() { return 'source-over'; },
    clearRect() { if (curFrame) rec.frameArcs.push(curFrame); curFrame = []; },
    beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, fill() {},
    stroke() { rec.strokeCalls++; },
    arc(x, y, r) { if (curFrame) curFrame.push(r); },
    createRadialGradient(x0, y0, r0, x1, y1, r1) {
      return { addColorStop(off, col) { rec.stops.push({ off: off, col: col }); } };
    }
  };
  rec._flush = function () { if (curFrame) { rec.frameArcs.push(curFrame); curFrame = null; } };
  return { ctx: ctx, rec: rec };
}

// Run the REAL renderer for `frames` rAF ticks at the given active state.
function runViz(active, frames) {
  const built = makeRecorder();
  const canvas = { width: 480, height: 480, getContext: function () { return built.ctx; } };
  let rafCb = null;
  const sandbox = {
    requestAnimationFrame: function (cb) { rafCb = cb; return 1; },
    cancelAnimationFrame: function () {}
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read('voiceOrbViz.js'), sandbox, { filename: 'voiceOrbViz.js' });
  const orb = sandbox.HartVoiceOrbViz(canvas, {});   // first frame renders synchronously
  orb.setActive(active);
  for (let i = 0; i < frames; i++) { if (rafCb) rafCb(); }
  built.rec._flush();
  orb.destroy();
  return built.rec;
}

function rgbaParse(s) {
  const m = /rgba\((\d+),(\d+),(\d+),([\d.]+)\)/.exec(s);
  return m ? [+m[1], +m[2], +m[3], parseFloat(m[4])] : null;
}

// ════════════════════════════════════════════════════════════════════════════
// A. voiceOrbViz.js — edgeless soft glow + brand spectrum (no disc, no rings)
// ════════════════════════════════════════════════════════════════════════════
(function testEdgelessBrand() {
  console.log('\n[A] voiceOrbViz.js  edgeless soft-glow + brand spectrum');
  const rec = runViz(false, 60);

  ok(rec.frameArcs.length > 0, 'the renderer actually drew frames');
  ok(rec.strokeCalls === 0, 'the orb NEVER strokes (no hard ring outlines)');
  ok(rec.strokeStyleSets === 0, 'the orb NEVER sets strokeStyle (no ring colour)');
  ok(rec.fillStyleTypes.length > 0 && rec.fillStyleTypes.every(function (t) { return t === 'object'; }),
     'every fillStyle is a gradient OBJECT, never a flat colour string (no solid outer disc)');

  // No off-brand purple anywhere in the painted colours.
  ok(!rec.stops.some(function (s) { return /108\s*,\s*99\s*,\s*255/.test(s.col); }),
     'the off-brand purple rgba(108,99,255) is gone from every gradient stop');

  // Edgeless: at least one gradient fades to FULLY transparent at the rim.
  ok(rec.stops.some(function (s) {
       const c = rgbaParse(s.col);
       return s.off >= 0.99 && c && c[3] === 0;
     }),
     'a gradient stop is fully transparent (alpha 0) at the rim (offset 1) -> edgeless radial falloff');

  // Additive compositing -> volumetric glow that merges with the background.
  ok(rec.gco.indexOf('lighter') >= 0, 'uses the "lighter" composite for a volumetric, merging glow');

  // Brand spectrum: the three hue families must all be painted (iridescent), proving
  // it is NOT a single flat colour. Anchors: teal[0,230,195] cyan[41,197,255]
  // blue[59,130,246] violet[155,92,255] magenta[255,46,154].
  let hasTeal = false, hasBlueViolet = false, hasMagenta = false;
  rec.stops.forEach(function (s) {
    const c = rgbaParse(s.col);
    if (!c || c[3] <= 0.02) return;
    const r = c[0], g = c[1], b = c[2];
    if (r === 255 && g === 255 && b === 255) return;          // skip the white core
    if (g >= 150 && b >= 120 && r <= 120) hasTeal = true;      // teal / cyan
    if (b >= 180 && r <= 170 && g <= 170) hasBlueViolet = true;// blue / violet
    if (r >= 180 && g <= 130 && b >= 110) hasMagenta = true;   // magenta
  });
  ok(hasTeal, 'a teal/cyan brand hue is painted');
  ok(hasBlueViolet, 'a blue/violet brand hue is painted');
  ok(hasMagenta, 'a magenta brand hue is painted');
  ok(hasTeal && hasBlueViolet && hasMagenta,
     'the full brand spectrum (teal/cyan + blue/violet + magenta) is iridescent, not flat');
})();

// ════════════════════════════════════════════════════════════════════════════
// B. voiceOrbViz.js — BREATHING idle (slow oscillation) + intensifies with energy
// ════════════════════════════════════════════════════════════════════════════
(function testBreathing() {
  console.log('\n[B] voiceOrbViz.js  breathing idle + energy-intensified breath');
  // 400 frames * 0.016s ~= 6.4s -> covers a full ~5s breath cycle.
  const idle = runViz(false, 400);

  // Per-frame draw order is fixed: 3 halo glows, then the CORE arc (index 3), then
  // the spark dot (index 4) -> 5 arcs/frame. In idle, energy decays to 0, so the
  // core radius is a clean function of the breath alone (no audio wave term).
  const fiveArcFrames = idle.frameArcs.filter(function (f) { return f.length === 5; });
  ok(fiveArcFrames.length > 300, 'every idle frame draws the 5 layered arcs (3 halos + core + spark)');
  const core = fiveArcFrames.map(function (f) { return f[3]; });
  let min = Infinity, max = -Infinity;
  core.forEach(function (v) { if (v < min) min = v; if (v > max) max = v; });
  ok((max - min) > 1.0, 'the idle core radius oscillates (breathes), range > 1px  (got ' + (max - min).toFixed(2) + 'px)');

  let up = false, down = false;
  for (let i = 1; i < core.length; i++) {
    if (core[i] > core[i - 1] + 0.001) up = true;
    if (core[i] < core[i - 1] - 0.001) down = true;
  }
  ok(up && down, 'the breath both INHALES and EXHALES (rises and falls), not a frozen disc');

  // Energy intensifies the breath: the active (synthetic-energy) peak core exceeds
  // the idle peak core.
  const active = runViz(true, 400);
  const aCore = active.frameArcs.filter(function (f) { return f.length === 5; }).map(function (f) { return f[3]; });
  let aMax = -Infinity;
  aCore.forEach(function (v) { if (v > aMax) aMax = v; });
  ok(aMax > max + 0.5, 'energy intensifies the breath: active peak core ' + aMax.toFixed(2) +
     'px > idle peak core ' + max.toFixed(2) + 'px');
})();

// ── A tiny DOM shim for hartHero.js (z-index float) ─────────────────────────
function makeEl(tag, realm) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), _attrs: {}, _kids: [], _listeners: {},
    style: {}, textContent: '', innerHTML: '', parentNode: null,
    classList: { _s: {},
      add: function (c) { this._s[c] = 1; }, remove: function (c) { delete this._s[c]; },
      contains: function (c) { return !!this._s[c]; },
      toggle: function (c, f) { const on = (f === undefined) ? !this._s[c] : !!f; if (on) this._s[c] = 1; else delete this._s[c]; return on; } },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute: function (k) { delete this._attrs[k]; },
    get children() { return this._kids; },
    appendChild: function (c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild: function (c) { this._kids = this._kids.filter(function (k) { return k !== c; }); return c; },
    addEventListener: function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener: function () {},
    focus: function () { realm.focusCalls++; }, select: function () {},
    setPointerCapture: function () {}, releasePointerCapture: function () {},
    getBoundingClientRect: function () { return { left: 0, top: 0, width: 360, height: 200 }; },
    closest: function () { return null; },
    querySelector: function () { return makeEl('div', realm); },
    querySelectorAll: function () { return []; },
    get dataset() { const a = this._attrs; const ds = {}; Object.keys(a).forEach(function (k) { if (k.indexOf('data-') === 0) ds[k.slice(5)] = a[k]; }); return ds; },
    get className() { return this._attrs.class || ''; }, set className(v) { this._attrs.class = v; },
    get id() { return this._attrs.id || ''; }, set id(v) { this._attrs.id = String(v); }
  };
  return el;
}

function runHero() {
  const realm = { focusCalls: 0, registry: {} };
  const docEl = makeEl('html', realm);
  const document = {
    readyState: 'complete', documentElement: docEl, body: makeEl('body', realm),
    _listeners: {},
    createElement: function (t) { return makeEl(t, realm); },
    getElementById: function (id) { return realm.registry[id] || null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    addEventListener: function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  };
  const mk = function (id) { const e = makeEl('div', realm); e.setAttribute('id', id); realm.registry[id] = e; return e; };
  ['hart-hero', 'hart-hero-input', 'hart-hero-go', 'hart-hero-orbwrap', 'hart-hero-status',
   'hart-hero-chips', 'hart-hero-hevolve', 'ac-input', 'assistant-chat', 'panels'].forEach(mk);

  const sandbox = {
    document: document, console: console,
    setInterval: function () { return 1; }, clearInterval: function () {},
    setTimeout: function (fn) { if (typeof fn === 'function') fn(); return 0; }, clearTimeout: function () {},
    requestAnimationFrame: function (fn) { fn(); return 1; }, cancelAnimationFrame: function () {},
    MutationObserver: function () { return { observe: function () {}, disconnect: function () {} }; },
    innerWidth: 1280, innerHeight: 800,
    addEventListener: function (t, fn) { document.addEventListener(t, fn); },
    toggleVoice: function () {}, acSend: function () {}, openPanel: function () {}, toggleAssistantChat: function () {}
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(read('hartHero.js'), sandbox, { filename: 'hartHero.js' });
  return { realm: realm, sandbox: sandbox, hero: realm.registry['hart-hero'] };
}

// ════════════════════════════════════════════════════════════════════════════
// C. hartHero.js — orb FLOATS over windows (high z), reachable, never traps focus
// ════════════════════════════════════════════════════════════════════════════
(function testFloatOverWindows() {
  console.log('\n[C] hartHero.js  float over windows (high z) + reachable + no focus trap');
  const H = runHero();
  const z = parseInt(H.hero.style.zIndex, 10);
  ok(!isNaN(z), 'place() stamped a z-index on #hart-hero  (got ' + JSON.stringify(H.hero.style.zIndex) + ')');
  ok(z > 999, 'the orb floats ABOVE app windows (z ' + z + ' > focused .panel z 999)');
  ok(z < 3000 && z < 8000, 'the orb stays BELOW system chrome (ctx-menu z 3000 / taskbar z 8000) so chrome stays reachable');
  ok(H.hero.style.pointerEvents === 'auto', 'the floating orb stays pointer-reachable (never inert wallpaper)');
  ok(H.hero.style.opacity !== undefined && H.hero.style.opacity !== '', 'place() set an opacity (merge/demerge state)');
  ok(/translate\(-50%,-50%\)/.test(H.hero.style.transform || ''), 'place() keeps the CSS centring contract in the transform');

  // The single writer is idempotent: re-running place() keeps the float layer.
  ok(typeof H.sandbox.HartOrbPlace === 'function', 'HartOrbPlace is exposed for external re-placement');
  H.hero.style.zIndex = '5';            // simulate a stale/other write
  H.sandbox.HartOrbPlace();
  ok(parseInt(H.hero.style.zIndex, 10) > 999, 'place() re-asserts the high float z-index (single writer wins)');

  // Never traps focus: the hero only READS the chat open-state; it must not focus()
  // anything during init.
  ok(H.realm.focusCalls === 0, 'init never calls focus() on any element (never traps focus)');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

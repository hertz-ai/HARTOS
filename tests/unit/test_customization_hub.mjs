/*
 * Behavioural tests for the Customization hub (#140 orb varieties / #161 palette /
 * #162 media backgrounds). They drive the REAL static modules (hartPersonalize.js,
 * voiceOrbViz.js) through their public surface on a faithful, dependency-free DOM
 * shim and assert OBSERVABLE side effects — brand CSS vars actually set on
 * documentElement, HartSession keys persisted, the live orb instance told to
 * switch style, the .wallpaper host's media child created OR degraded — never
 * source substrings (CLAUDE.md Gate 5 / feedback_no_grep_tests).
 *
 *  [P] PALETTE — window.HartPalette.apply(palette): paints --hart-accent / --hart-a2
 *      / --hart-background (+ their rgb triples) on documentElement INSTANTLY,
 *      persists {a,a2,b} under HartSession.palette, and extends
 *      /api/appearance/apply with secondary_accent + custom (best-effort POST).
 *  [C] CUSTOM PALETTE — the rendered custom colour picker (accent/secondary/bg
 *      inputs + Apply) applies + persists the ad-hoc palette.
 *  [O] ORB VARIETY — window.HartOrbStyle.set()/restore() persists HartSession.orb_style
 *      and drives the ONE live orb instance (_hartVoiceOrb.setStyle); + voiceOrbViz
 *      setStyle('nebula') visibly RE-TINTS the canvas (recorded gradient colours).
 *  [B] BACKGROUND DEGRADE — hartSetWallpaperMedia video/lottie DEGRADE to a static
 *      frame on the software floor (no <video>/lottie child created), render live on
 *      hardware, and gif always renders (cheap). Never throws.
 *
 * Run:  node tests/unit/test_customization_hub.mjs
 * (test_customization_hub.py shells out so pytest/CI picks it up.)
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
function matchSel(el, sel) {
  sel = sel.trim();
  if (!sel) return false;
  if (sel[0] === '.') return (el._attrs.class || '').split(/\s+/).indexOf(sel.slice(1)) >= 0;
  if (sel[0] === '#') return el._attrs.id === sel.slice(1);
  return el.tagName === sel.toUpperCase();
}
function makeStyle() {
  var vars = {};
  var st = {
    _vars: vars,
    setProperty: function (k, v) { vars[k] = String(v); },
    getPropertyValue: function (k) { return Object.prototype.hasOwnProperty.call(vars, k) ? vars[k] : ''; },
    removeProperty: function (k) { delete vars[k]; }
  };
  return st;
}
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, style: makeStyle(), textContent: '',
    _innerHTML: '', parentNode: null, value: '', type: '',
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) { const on = (force === undefined) ? !this._s.has(c) : !!force; if (on) this._s.add(c); else this._s.delete(c); return on; } },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; },
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
    play() { return { then() { return this; }, catch() { return this; } }; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 60, height: 40 }; },
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
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); }
  };
  return el;
}
function mkEv(target, extra) {
  return Object.assign({ target, preventDefault() {}, stopPropagation() {},
    clientX: 0, clientY: 0, button: 0, pointerId: 1, key: '', code: '' }, extra || {});
}

// Map-backed HartSession stand-in (get/set/ready) — the persistence boundary.
function makeSession(initial) {
  const blob = Object.assign({}, initial || {});
  const calls = [];
  return {
    _blob: blob, _calls: calls,
    ready(cb) { try { cb(blob); } catch (e) {} },
    get(k, dflt) { return Object.prototype.hasOwnProperty.call(blob, k) ? blob[k] : dflt; },
    set(k, v) { blob[k] = v; calls.push([k, v]); }
  };
}

function makeRealm(opts) {
  opts = opts || {};
  const registry = {};
  const docEl = makeEl('html');
  const head = makeEl('head');
  const body = makeEl('body');
  const document = {
    readyState: 'complete', documentElement: docEl, head, body,
    _listeners: {},
    createElement: (t) => makeEl(t),
    getElementById(id) { return registry[id] || null; },
    querySelector(sel) { return body.querySelector(sel); },
    querySelectorAll(sel) { return body.querySelectorAll(sel); },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
  };
  // .wallpaper host (the visible desktop background the media/css paints).
  const wp = makeEl('div'); wp.setAttribute('class', 'wallpaper'); body.appendChild(wp);

  const fetchCalls = [];
  const sandbox = {
    document, console,
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout() {},
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: (url, o) => { fetchCalls.push({ url, opts: o }); return { then() { return this; }, catch() { return this; } }; },
    HartTimeoutSignal: () => null
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, docEl, body, wp, registry, fetchCalls };
}

function runModules(realm, files) {
  files.forEach((f) => vm.runInContext(read(f), realm.sandbox, { filename: f }));
}

// ════════════════════════════════════════════════════════════════════════════
// [P] PALETTE — applies brand vars instantly + persists + extends theme/apply
// ════════════════════════════════════════════════════════════════════════════
(function testPaletteApply() {
  console.log('\n[P] hartPersonalize.js  palette applies brand vars + persists + posts theme/apply');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});     // empty => restore paints nothing
  R.sandbox.window.hartLoadInto = function () {};      // stub the Images loader (fetch)
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  ok(R.sandbox.window.HartPalette && typeof R.sandbox.window.HartPalette.apply === 'function',
     'window.HartPalette.apply is exposed');

  const vibrant = { id: 'vibrant', a: '#00E6C3', a2: '#9B5CFF', b: '#05060C' };
  R.sandbox.window.HartPalette.apply(vibrant);

  const st = R.docEl.style;
  eq(st.getPropertyValue('--hart-accent'), '#00E6C3', 'palette set --hart-accent on documentElement (instant)');
  eq(st.getPropertyValue('--hart-a2'), '#9B5CFF', 'palette set the secondary accent --hart-a2');
  eq(st.getPropertyValue('--hart-background'), '#05060C', 'palette set --hart-background');
  eq(st.getPropertyValue('--hart-accent-rgb'), '0,230,195', 'palette set --hart-accent-rgb (parsed triple)');
  eq(st.getPropertyValue('--hart-a2-rgb'), '155,92,255', 'palette set --hart-a2-rgb (parsed triple)');

  const persisted = R.sandbox.window.HartSession.get('palette');
  ok(persisted && persisted.a === '#00E6C3' && persisted.a2 === '#9B5CFF' && persisted.b === '#05060C',
     'palette persisted {a,a2,b} under HartSession.palette');

  const post = R.fetchCalls.filter(c => String(c.url).indexOf('/api/appearance/apply') >= 0);
  ok(post.length === 1, 'exactly one POST to /api/appearance/apply (extend, not fork)');
  const body = JSON.parse(post[0].opts.body);
  eq(body.secondary_accent, '#9B5CFF', 'the theme/apply body carries secondary_accent (a2)');
  ok(body.custom && body.custom.accent === '#00E6C3' && body.custom.secondary === '#9B5CFF' && body.custom.background === '#05060C',
     'the theme/apply body carries the custom colours (accent/secondary/background)');

  // Restore path: a persisted palette re-paints on boot (no server post needed).
  const R2 = makeRealm();
  R2.sandbox.window.HartSession = makeSession({ palette: { a: '#00D4AA', a2: '#00D4AA', b: '#0F0E17' } });
  R2.sandbox.window.hartLoadInto = function () {};
  runModules(R2, ['voiceOrbViz.js', 'hartPersonalize.js']);
  eq(R2.docEl.style.getPropertyValue('--hart-accent'), '#00D4AA', 'restore() re-paints the persisted palette accent on boot');
  eq(R2.docEl.style.getPropertyValue('--hart-a2'), '#00D4AA', 'restore() re-paints the persisted secondary (monotone-teal)');
})();

// ════════════════════════════════════════════════════════════════════════════
// [M] MOOD DOCK + AMBIENT QUAD — applyPalette retints --hart-amb-1..4 (the mood),
//     PINS the functional --hart-accent to the palette's accent (steward hybrid:
//     teal on function even when the ambient goes violet-lead), persists the full
//     quad, and the on-desktop mood dock's swatch CLICK drives the SAME applyPalette.
// ════════════════════════════════════════════════════════════════════════════
(function testMoodQuadAndDock() {
  console.log('\n[M] hartPersonalize.js  applyPalette retints the ambient quad + the mood dock drives it');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});
  R.sandbox.window.hartLoadInto = function () {};
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  // The Aura "aurora" mood: functional accent pinned teal, ambient QUAD violet-lead.
  const list = R.sandbox.window.HART_PALETTES;
  const aurora = list.filter(p => p.id === 'aurora')[0];
  ok(aurora && aurora.accent === '#00E6C3' && aurora.a === '#B182FF',
     'HART_PALETTES carries the Aura "aurora" mood (accent teal, ambient violet-lead)');

  R.sandbox.window.HartPalette.apply(aurora);
  const st = R.docEl.style;
  // Ambient quad retinted (the MOOD) — all four hues reach the DOM.
  eq(st.getPropertyValue('--hart-amb-1'), '#B182FF', 'applyPalette set --hart-amb-1 (mood hue #1 = violet)');
  eq(st.getPropertyValue('--hart-amb-2'), '#00DDF9', 'applyPalette set --hart-amb-2');
  eq(st.getPropertyValue('--hart-amb-3'), '#FB66B6', 'applyPalette set --hart-amb-3');
  eq(st.getPropertyValue('--hart-amb-4'), '#FFB330', 'applyPalette set --hart-amb-4');
  eq(st.getPropertyValue('--hart-amb-1-rgb'), '177,130,255', 'applyPalette set --hart-amb-1-rgb (parsed triple)');
  // FUNCTIONAL signifier stays teal (accent override wins over the lead ambient hue).
  eq(st.getPropertyValue('--hart-accent'), '#00E6C3', 'functional --hart-accent pinned teal (NOT the violet lead) — steward hybrid');
  // Persisted so restore() re-paints the full quad, not just the duotone.
  const persisted = R.sandbox.window.HartSession.get('palette');
  ok(persisted && persisted.a3 === '#FB66B6' && persisted.a4 === '#FFB330' && persisted.accent === '#00E6C3',
     'the full quad + functional accent persisted under HartSession.palette');

  // The theme/apply POST carries the ambient quad as custom overrides (server round-trip).
  const post = R.fetchCalls.filter(c => String(c.url).indexOf('/api/appearance/apply') >= 0);
  ok(post.length >= 1, 'applyPalette posted to /api/appearance/apply');
  const cbody = JSON.parse(post[post.length - 1].opts.body);
  ok(cbody.custom && cbody.custom.ambient_1 === '#B182FF' && cbody.custom.ambient_4 === '#FFB330',
     'the apply body carries the ambient quad (ambient_1..4) for server persistence');
  eq(cbody.custom.accent, '#00E6C3', 'the apply body pins the functional accent teal');

  // On-desktop MOOD DOCK: renders swatches; a swatch CLICK drives the same applyPalette.
  const R2 = makeRealm();
  R2.sandbox.window.HartSession = makeSession({});
  R2.sandbox.window.hartLoadInto = function () {};
  runModules(R2, ['voiceOrbViz.js', 'hartPersonalize.js']);
  ok(typeof R2.sandbox.window.hartRenderMoodDock === 'function', 'window.hartRenderMoodDock is exposed');
  const dock = makeEl('div');
  R2.sandbox.window.hartRenderMoodDock(dock);
  const swatches = dock.querySelectorAll('.hart-mood-sw');
  eq(swatches.length, R2.sandbox.window.HART_PALETTES.length, 'the dock rendered one swatch per palette');
  const auroraSw = swatches.filter(s => s.title === 'Aurora')[0];
  ok(auroraSw, 'the Aurora mood swatch rendered (title)');
  auroraSw.dispatch('click', mkEv(auroraSw));
  eq(R2.docEl.style.getPropertyValue('--hart-amb-1'), '#B182FF', 'clicking the dock swatch retints --hart-amb-1 via applyPalette');
  eq(R2.docEl.style.getPropertyValue('--hart-accent'), '#00E6C3', 'dock swatch keeps the functional accent teal');
})();

// ════════════════════════════════════════════════════════════════════════════
// [G1] MOOD-BY-ID — the LLM emits a mood ID (compose_home mood=); HartPalette.byId
//      resolves it to the PALETTES entry that paint() applies live. This is the EXACT
//      chain the SSE home_compose branch runs (byId -> paint), so the LLM-composed
//      mood reaches the DOM; an unknown id is a graceful no-op (never a broken paint).
// ════════════════════════════════════════════════════════════════════════════
(function testMoodById() {
  console.log('\n[G1] hartPersonalize.js  HartPalette.byId resolves a mood id -> paint applies it live');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});
  R.sandbox.window.hartLoadInto = function () {};
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  const HP = R.sandbox.window.HartPalette;
  ok(typeof HP.byId === 'function', 'HartPalette.byId is exposed');

  const solar = HP.byId('solar');
  ok(solar && solar.id === 'solar' && solar.a === '#FF7600', 'byId("solar") resolves to the solar quad');
  ok(HP.byId('AURORA') && HP.byId('AURORA').id === 'aurora', 'byId is case-insensitive');
  eq(HP.byId('no-such-mood'), null, 'byId returns null for an unknown id (graceful)');
  eq(HP.byId(''), null, 'byId returns null for empty');
  eq(HP.byId(undefined), null, 'byId returns null for undefined');

  // The SSE-branch chain: resolve an id, then paint -> the ambient quad reaches the DOM.
  HP.paint(HP.byId('solar'));
  const st = R.docEl.style;
  eq(st.getPropertyValue('--hart-amb-1'), '#FF7600', 'byId->paint set --hart-amb-1 to the solar lead (the LLM mood reaches the DOM)');
  eq(st.getPropertyValue('--hart-amb-2'), '#FF9B92', 'byId->paint set --hart-amb-2');
})();

// ════════════════════════════════════════════════════════════════════════════
// [G3] THEME GALLERY — renders from the SERVER preset source (/api/appearance/presets,
//      which includes Aura + high-contrast), with the built-in PRESETS as the instant
//      offline fallback so the picker never empties (zero regression). Kills the
//      hardcoded parallel preset list that had drifted (no Aura).
// ════════════════════════════════════════════════════════════════════════════
(function testThemeGalleryFromServer() {
  console.log('\n[G3] hartPersonalize.js  theme gallery renders the offline fallback + fetches the server preset list');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});
  R.sandbox.window.hartLoadInto = function () {};
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  const host = makeEl('div');
  R.sandbox.window.hartRenderPersonalize(host);

  // Offline floor: the built-in PRESETS render synchronously (the picker never empties).
  const cards = host.querySelectorAll('.hart-theme-card');
  ok(cards.length === R.sandbox.window.HART_THEME_PRESETS.length,
     'theme gallery renders the built-in PRESETS as the instant offline fallback (zero regression)');

  // Server source: it fetches the ONE preset list (/api/appearance/presets) to surface
  // Aura + high-contrast — the DRY fix, no hardcoded parallel list.
  const pf = R.fetchCalls.filter(c => String(c.url).indexOf('/api/appearance/presets') >= 0);
  ok(pf.length === 1, 'theme gallery fetches /api/appearance/presets (the ONE preset source)');
})();

// ════════════════════════════════════════════════════════════════════════════
// [C] CUSTOM PALETTE — the rendered picker applies + persists an ad-hoc palette
// ════════════════════════════════════════════════════════════════════════════
(function testCustomPalette() {
  console.log('\n[C] hartPersonalize.js  the custom colour picker applies + persists');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});
  R.sandbox.window.hartLoadInto = function () {};
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  const host = makeEl('div');
  R.sandbox.window.hartRenderPersonalize(host);

  // Three <input type=color> — accent, secondary, background (in that order).
  const colors = host.querySelectorAll('input').filter(function (i) { return i.type === 'color'; });
  ok(colors.length === 3, 'the custom picker rendered three colour inputs (accent/secondary/bg)  (got ' + colors.length + ')');
  colors[0].value = '#112233'; colors[1].value = '#445566'; colors[2].value = '#010203';

  const applyBtn = host.querySelector('.hart-cp-apply');
  ok(applyBtn !== null, 'the custom "Apply" button rendered');
  applyBtn.dispatch('click', mkEv(applyBtn));

  eq(R.docEl.style.getPropertyValue('--hart-accent'), '#112233', 'custom apply painted the chosen accent');
  eq(R.docEl.style.getPropertyValue('--hart-a2'), '#445566', 'custom apply painted the chosen secondary');
  eq(R.docEl.style.getPropertyValue('--hart-background'), '#010203', 'custom apply painted the chosen background');
  const p = R.sandbox.window.HartSession.get('palette');
  ok(p && p.a === '#112233' && p.a2 === '#445566' && p.b === '#010203', 'custom palette persisted under HartSession.palette');
})();

// ════════════════════════════════════════════════════════════════════════════
// [O] ORB VARIETY — set/restore persists + drives the live orb; setStyle re-tints
// ════════════════════════════════════════════════════════════════════════════
(function testOrbSwitchPersists() {
  console.log('\n[O] hartPersonalize.js  orb variety persists + drives the live orb instance');
  const R = makeRealm();
  R.sandbox.window.HartSession = makeSession({});
  R.sandbox.window.hartLoadInto = function () {};
  const applied = [];
  R.sandbox.window._hartVoiceOrb = { setStyle: function (id) { applied.push(id); } };
  runModules(R, ['voiceOrbViz.js', 'hartPersonalize.js']);

  ok(R.sandbox.window.HartOrbStyle && typeof R.sandbox.window.HartOrbStyle.set === 'function',
     'window.HartOrbStyle.set is exposed');
  eq(R.sandbox.window.HartOrbStyle.get(), 'vibrant', 'default orb style is vibrant when nothing persisted');

  R.sandbox.window.HartOrbStyle.set('nebula');
  eq(R.sandbox.window.HartSession.get('orb_style'), 'nebula', 'setting the orb style persists HartSession.orb_style');
  ok(applied.indexOf('nebula') >= 0, 'setting the orb style drives the live orb (_hartVoiceOrb.setStyle("nebula"))');

  // Restore: a persisted style is applied to the live orb on boot.
  const R2 = makeRealm();
  R2.sandbox.window.HartSession = makeSession({ orb_style: 'pulse' });
  R2.sandbox.window.hartLoadInto = function () {};
  const applied2 = [];
  R2.sandbox.window._hartVoiceOrb = { setStyle: function (id) { applied2.push(id); } };
  runModules(R2, ['voiceOrbViz.js', 'hartPersonalize.js']);
  eq(R2.sandbox.window.HartOrbStyle.get(), 'pulse', 'restore reads the persisted orb style');
  ok(applied2.indexOf('pulse') >= 0, 'restore applied the persisted style to the live orb on boot');
})();

(function testOrbStyleRetints() {
  console.log('\n[O] voiceOrbViz.js  setStyle live re-tints the canvas gradient colours');
  function runViz(afterStyle) {
    const colors = [];
    const ctx = { _f: null,
      set fillStyle(v) { if (typeof v === 'string') colors.push(v); },
      get fillStyle() { return this._f; },
      set strokeStyle(v) { if (typeof v === 'string') colors.push(v); },
      get strokeStyle() { return null; },
      set globalCompositeOperation(v) {}, get globalCompositeOperation() { return 's'; },
      clearRect() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, fill() {}, stroke() {},
      arc() {},
      createRadialGradient() { return { addColorStop(_p, c) { if (typeof c === 'string') colors.push(c); } }; } };
    const canvas = { width: 480, height: 480, getContext: () => ctx };
    let cb = null;
    const sb = { requestAnimationFrame: (f) => { cb = f; return 1; }, cancelAnimationFrame() {} };
    sb.window = sb; vm.createContext(sb);
    vm.runInContext(read('voiceOrbViz.js'), sb, { filename: 'voiceOrbViz.js' });
    const orb = sb.HartVoiceOrbViz(canvas, {});
    orb.setActive(true);
    for (let i = 0; i < 10; i++) { if (cb) cb(); }
    colors.length = 0;                 // measure AFTER the (optional) style switch
    if (afterStyle) orb.setStyle(afterStyle);
    for (let i = 0; i < 10; i++) { if (cb) cb(); }
    orb.destroy();
    return colors.join(' | ');
  }
  const vib = runViz(null);
  const neb = runViz('nebula');
  ok(vib.indexOf('0,230,195') >= 0, 'default (vibrant) paints the teal glow (0,230,195)');
  ok(neb.indexOf('180,70,220') >= 0, 'setStyle("nebula") paints the nebula magenta/violet glow (180,70,220)');
  ok(neb.indexOf('0,230,195') < 0, 'after setStyle("nebula") the teal glow is gone (the switch really re-tints)');

  // Exposed style list is a single source for the picker.
  const sb2 = { requestAnimationFrame: () => 1, cancelAnimationFrame() {} };
  sb2.window = sb2; vm.createContext(sb2);
  vm.runInContext(read('voiceOrbViz.js'), sb2, { filename: 'voiceOrbViz.js' });
  const list = sb2.HartVoiceOrbViz.STYLES;
  ok(Array.isArray(list) && list.length >= 3 && list.length <= 5, 'HartVoiceOrbViz.STYLES exposes 3-5 orb varieties  (got ' + (list && list.length) + ')');
  eq(sb2.HartVoiceOrbViz.DEFAULT_STYLE, 'vibrant', 'the default orb variety is vibrant');
})();

// ════════════════════════════════════════════════════════════════════════════
// [B] BACKGROUND DEGRADE — video/lottie shed on the software floor; gif always ok
// ════════════════════════════════════════════════════════════════════════════
(function testBackgroundDegrade() {
  console.log('\n[B] hartPersonalize.js  media backgrounds degrade on the software floor');

  // Hardware floor: a video creates a live <video> child + returns not-degraded.
  const HW = makeRealm();
  HW.sandbox.window.HartSession = makeSession({});
  HW.sandbox.window.hartLoadInto = function () {};
  HW.sandbox.window.HART_PERF = { potato: false };
  runModules(HW, ['voiceOrbViz.js', 'hartPersonalize.js']);
  const degHw = HW.sandbox.window.hartSetWallpaperMedia('video', '/x/clip.webm');
  eq(degHw, false, 'video on the HARDWARE floor plays live (not degraded)');
  ok(HW.wp.querySelector('#hart-wp-media') !== null && HW.wp.querySelector('#hart-wp-media').tagName === 'VIDEO',
     'a live <video> element was mounted into .wallpaper on hardware');
  const bgPersist = HW.sandbox.window.HartSession.get('wallpaper_bg');
  ok(bgPersist && bgPersist.type === 'video' && bgPersist.url === '/x/clip.webm', 'the media background persisted under HartSession.wallpaper_bg');

  // Software floor (potato): video DEGRADES to a static frame — no <video>.
  const SW = makeRealm();
  SW.sandbox.window.HartSession = makeSession({});
  SW.sandbox.window.hartLoadInto = function () {};
  SW.sandbox.window.HART_PERF = { potato: true };
  runModules(SW, ['voiceOrbViz.js', 'hartPersonalize.js']);
  const degSw = SW.sandbox.window.hartSetWallpaperMedia('video', '/x/clip.webm', '/x/poster.jpg');
  eq(degSw, true, 'video on the SOFTWARE floor DEGRADES to a static frame');
  ok(SW.wp.querySelector('#hart-wp-media') === null, 'no <video> mounted on the software floor (never plays on a potato)');
  ok(String(SW.wp.style.background).indexOf('poster.jpg') >= 0, 'the software floor painted the static poster frame instead');

  // Software floor via body.gpu-software class (not just potato) also degrades.
  const SW2 = makeRealm();
  SW2.sandbox.window.HartSession = makeSession({});
  SW2.sandbox.window.hartLoadInto = function () {};
  SW2.body.classList.add('gpu-software');
  runModules(SW2, ['voiceOrbViz.js', 'hartPersonalize.js']);
  const degLottieSw = SW2.sandbox.window.hartSetWallpaperMedia('lottie', '/x/anim.json');
  eq(degLottieSw, true, 'lottie on body.gpu-software DEGRADES (no lottie player mounted)');
  ok(SW2.wp.querySelector('#hart-wp-media') === null, 'no lottie container mounted on the software floor');

  // Lottie on hardware WITH a player mounts the container; without a player it degrades.
  const HL = makeRealm();
  HL.sandbox.window.HartSession = makeSession({});
  HL.sandbox.window.hartLoadInto = function () {};
  HL.sandbox.window.HART_PERF = { potato: false };
  const lottieCalls = [];
  HL.sandbox.window.lottie = { loadAnimation: function (o) { lottieCalls.push(o); return { destroy() {} }; } };
  runModules(HL, ['voiceOrbViz.js', 'hartPersonalize.js']);
  const degLottieHw = HL.sandbox.window.hartSetWallpaperMedia('lottie', '/shell/static/hevolve-anim.json');
  eq(degLottieHw, false, 'lottie on hardware with a bundled player plays live (not degraded)');
  ok(lottieCalls.length === 1 && lottieCalls[0].path === '/shell/static/hevolve-anim.json',
     'the bundled lottie player was driven with the anim path');

  // GIF is cheap -> always renders, even on a potato, and never mounts a heavy child.
  const G = makeRealm();
  G.sandbox.window.HartSession = makeSession({});
  G.sandbox.window.hartLoadInto = function () {};
  G.sandbox.window.HART_PERF = { potato: true };
  runModules(G, ['voiceOrbViz.js', 'hartPersonalize.js']);
  const degGif = G.sandbox.window.hartSetWallpaperMedia('gif', '/x/loop.gif');
  eq(degGif, false, 'gif renders even on a potato (cheap, not degraded)');
  ok(String(G.wp.style.background).indexOf('loop.gif') >= 0, 'the gif was painted as the wallpaper background');
  ok(G.wp.querySelector('#hart-wp-media') === null, 'gif needs no heavy media child (native <img> background)');

  // Switching to a plain CSS wallpaper clears any prior media child (no stale video).
  HW.sandbox.window.hartSetWallpaper('#101014');
  ok(HW.wp.querySelector('#hart-wp-media') === null, 'setting a CSS wallpaper tears down the prior <video> (no stale media)');
  ok(HW.sandbox.window.HartSession.get('wallpaper_bg') === null, 'a CSS wallpaper clears the persisted media background');
})();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

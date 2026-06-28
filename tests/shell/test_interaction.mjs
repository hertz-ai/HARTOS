/*
 * tests/shell/test_interaction.mjs
 *
 * Behavioural coverage for the HART OS desktop INTERACTION stream:
 *   - a TAP synthesizes a launch (mouse AND touch single-tap), which is the
 *     regression this stream fixes: icons used to open only on dblclick, so a
 *     touchscreen tap never launched anything.
 *   - a context menu builds the RIGHT items per target (icon / desktop / window)
 *     and dismisses on Escape, outside-click, and offline (dependency missing).
 *   - edge-flip / clamp keeps the menu fully ON-SCREEN at every viewport edge.
 *
 * It drives the REAL static modules through their public surface on a tiny,
 * dependency-free DOM shim (the CI runner here has no jsdom):
 *   integrations/agent_engine/static/hartContextMenu.js  (window.HartCtxMenu)
 *   integrations/agent_engine/static/hartDesktop.js       (tap/drag + menus)
 *
 * Every assertion checks OBSERVABLE behaviour (a mock spy's args, a DOM
 * mutation, isOpen() state) and NEVER a source string. No grep tests.
 *
 * Run:  node tests/shell/test_interaction.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC_CTX = join(STATIC, 'hartContextMenu.js');
const SRC_DESK = join(STATIC, 'hartDesktop.js');

let passes = 0, failures = 0;
function ok(cond, msg) { if (cond) { passes++; console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM shim ───────────────────────────────────────────────────────
// Query membership is resolved against the className STRING (clsOf); the
// classList Set is a SEPARATE live store the modules mutate (add/remove/toggle).
function clsOf(el) { return (el._attrs.class || '').split(/\s+/); }
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _bySel: {}, _listeners: {},
    style: {}, offsetWidth: 200, offsetHeight: 250, offsetLeft: 0, offsetTop: 0,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { if (this._s.has(c)) this._s.delete(c); else this._s.add(c); } else if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    textContent: '', value: '', _innerHTML: '', parentNode: null,
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; this._bySel = {}; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild(c) { this._kids = this._kids.filter(k => k !== c); c.parentNode = null; return c; },
    insertBefore(c) { c.parentNode = el; this._kids.push(c); return c; },
    contains(node) {
      if (node === el) return true;
      for (const k of el._kids) { if (k === node) return true; if (k.contains && k.contains(node)) return true; }
      return false;
    },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter(f => f !== fn); },
    dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev || baseEvent(el))); },
    click() { this.dispatch('click', baseEvent(this)); },
    focus() {}, select() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; },
    querySelector(sel) {
      for (const k of this._kids) if (k._attrs.id && ('#' + k._attrs.id) === sel) return k;
      const m = /^\.desktop-icon\[data-id="(.+)"\]$/.exec(sel);
      if (m) return this._kids.find(k => k._attrs['data-id'] === m[1] && clsOf(k).includes('desktop-icon')) || null;
      if (!this._bySel[sel]) { const s = makeEl('div'); s.parentNode = el; this._bySel[sel] = s; }
      return this._bySel[sel];
    },
    querySelectorAll(sel) {
      if (sel === '.desktop-icon') return this._kids.filter(k => clsOf(k).includes('desktop-icon'));
      if (sel === '.desktop-icon.selected') return this._kids.filter(k => k.classList.contains('selected'));
      if (sel.indexOf('hart-ctx-item') >= 0) {
        const noDis = sel.indexOf(':not(.disabled)') >= 0;
        return this._kids.filter(k => clsOf(k).includes('hart-ctx-item') && (!noDis || !clsOf(k).includes('disabled')));
      }
      return [];
    },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); }
  };
  return el;
}
function baseEvent(target) {
  return { target: target, button: 0, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} };
}
// A target whose .closest()/.classList answer a scripted map (used by the
// document-level contextmenu handler that resolves titlebar / wallpaper).
function fakeTarget(opts) {
  opts = opts || {};
  return {
    classList: { contains(c) { return (opts.classes || []).indexOf(c) >= 0; } },
    closest(sel) { return (opts.closest && opts.closest[sel]) || null; },
    getAttribute(k) { return (opts.attrs || {})[k] || null; }
  };
}

const registry = {};
const document = {
  readyState: 'complete',
  documentElement: makeEl('html'),
  head: makeEl('head'),
  body: makeEl('body'),
  _listeners: {},
  createElement: (t) => makeEl(t),
  getElementById(id) {
    if (registry[id]) return registry[id];
    const walk = (n) => { for (const k of n._kids) { if (k._attrs.id === id) return k; const r = walk(k); if (r) return r; } return null; };
    return walk(this.body) || walk(this.head);
  },
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
  removeEventListener(t, fn) { if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter(f => f !== fn); },
  dispatch(t, ev) { (this._listeners[t] || []).slice().forEach(fn => fn(ev)); }
};
const layer = makeEl('div'); layer.setAttribute('id', 'hart-desktop'); registry['hart-desktop'] = layer;
const ctxMenu = makeEl('div'); ctxMenu.setAttribute('id', 'ctx-menu'); registry['ctx-menu'] = ctxMenu;
document.body.appendChild(layer);

// ── Controllable timers (a long-press must NOT auto-fire inside a quick tap) ──
let timerSeq = 1;
const timers = new Map();
function flushTimers() { const fns = Array.from(timers.values()); timers.clear(); fns.forEach(f => { try { f(); } catch (e) {} }); }

// ── Spies / mocks (the side-effects we assert) ──
const opened = [];
const calls = { bringToFront: [], closePanel: [], minimizePanel: [], toggleMax: [] };
const fetchCalls = [];
let lastSaved = null;
const sessionBlob = {};

const sandbox = {
  document, console,
  innerWidth: 1200, innerHeight: 800,
  setTimeout: (fn) => { const id = timerSeq++; timers.set(id, fn); return id; },
  clearTimeout: (id) => { timers.delete(id); },
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  matchMedia: () => ({ matches: false }),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  location: { reload() {} },
  confirm: () => true,                       // uninstall path: themed dsConfirm absent -> native confirm
  fetch: (url, opts) => { fetchCalls.push({ url: url, opts: opts }); return Promise.resolve({ ok: true }); },
  MANIFEST: {
    feed: { title: 'Feed', icon: 'rss_feed' },
    recipes: { title: 'Recipes', icon: 'menu_book' },
    appearance: { title: 'Appearance', icon: 'palette' },
    terminal: { title: 'Terminal', icon: 'terminal' },                       // NOT a default icon -> shows in the add-app picker
    store_app: { title: 'Store App', icon: 'storefront', installed: true }   // an installed app -> "Uninstall"
  },
  openPanel: (id) => { opened.push(id); },
  bringToFront: (id) => { calls.bringToFront.push(id); },
  closePanel: (id) => { calls.closePanel.push(id); },
  minimizePanel: (id) => { calls.minimizePanel.push(id); },
  toggleMax: (id) => { calls.toggleMax.push(id); },
  addEventListener() {}, removeEventListener() {},
  HartSession: {
    ready(cb) { cb(sessionBlob); },
    get(k, d) { return Object.prototype.hasOwnProperty.call(sessionBlob, k) ? sessionBlob[k] : d; },
    set(k, v) { sessionBlob[k] = v; if (k === 'desktop_icons') lastSaved = v; }
  }
};
sandbox.window = sandbox;
vm.createContext(sandbox);
// Load order matters: the ctx-menu module must define window.HartCtxMenu BEFORE
// hartDesktop's injectCtxMenu runs (it then skips the async <script> inject).
vm.runInContext(readFileSync(SRC_CTX, 'utf8'), sandbox, { filename: 'hartContextMenu.js' });
vm.runInContext(readFileSync(SRC_DESK, 'utf8'), sandbox, { filename: 'hartDesktop.js' });

const W = sandbox.window;
const iconOf = (id) => layer._kids.find(k => k._attrs['data-id'] === id);
const menuInBody = () => document.body._kids.filter(k => k._attrs.id === 'hart-ctxmenu');

// Capture the items a menu opens with, while still rendering the real menu.
let lastItems = null;
const realOpen = W.HartCtxMenu.open;
W.HartCtxMenu.open = function (items, x, y) { lastItems = items; return realOpen.call(this, items, x, y); };
const labelsOf = (items) => (items || []).filter(it => it && !it.sep).map(it => it.label);
const itemBy = (items, label) => (items || []).find(it => it && it.label === label);

// event factories
function pe(type, x, y, ptype, ts) {
  return { type: type, button: 0, clientX: x, clientY: y, pointerType: ptype || 'mouse',
           pointerId: 1, timeStamp: ts || 0, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} };
}
function ctxEvent(target, x, y) {
  let prevented = false;
  return { target: target, button: 0, clientX: x, clientY: y, pointerType: 'mouse',
           preventDefault() { prevented = true; }, stopPropagation() {}, stopImmediatePropagation() {},
           wasPrevented() { return prevented; } };
}

// ════════════════════════════════════════════════════════════════════════
console.log('# 0. boot — modules wired, default icons rendered');
ok(W.HartCtxMenu && typeof W.HartCtxMenu.open === 'function', 'HartCtxMenu module loaded');
ok(iconOf('feed') && iconOf('recipes') && iconOf('appearance'), 'default desktop icons rendered');

// ── A. A TAP SYNTHESIZES A LAUNCH ───────────────────────────────────────────
console.log('# A1. mouse single tap launches exactly once');
opened.length = 0;
let feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 100, 100, 'mouse', 1000));
feed.dispatch('pointerup', pe('pointerup', 101, 101, 'mouse', 1120));        // 120ms / 2px -> tap
eq(opened.length, 1, 'a quick mouse press+release launches once');
eq(opened[0], 'feed', 'launch synthesized via openPanel(icon id)');

console.log('# A2. touch single tap launches (THE regression — was dblclick-only)');
opened.length = 0;
feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 200, 200, 'touch', 2000));    // schedules the 500ms long-press
feed.dispatch('pointerup', pe('pointerup', 202, 203, 'touch', 2150));        // 150ms / 5px -> tap, clears long-press
flushTimers();                                                              // any stray long-press timer must now no-op
eq(opened.length, 1, 'a touchscreen tap launches');
eq(opened[0], 'feed', 'touch tap synthesized openPanel(feed)');

console.log('# A3. a DRAG does NOT launch — it rearranges + persists');
opened.length = 0; lastSaved = null;
const rec = iconOf('recipes');
const startLeft = parseInt(rec.style.left, 10) || 0;
rec.dispatch('pointerdown', pe('pointerdown', 300, 300, 'mouse', 3000));
rec.dispatch('pointermove', pe('pointermove', 360, 360, 'mouse', 3050));     // 120px -> past TAP_PX
rec.dispatch('pointerup', pe('pointerup', 360, 360, 'mouse', 3200));
eq(opened.length, 0, 'a drag does NOT launch');
ok(Array.isArray(lastSaved), 'drag committed -> persist() wrote desktop_icons');
ok((parseInt(rec.style.left, 10) || 0) !== startLeft, 'icon moved to a new grid column');

console.log('# A4. a slow stationary press selects but does NOT launch (boundary: dt > TAP_MS)');
opened.length = 0;
feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 150, 150, 'mouse', 5000));
feed.dispatch('pointerup', pe('pointerup', 151, 151, 'mouse', 5500));        // 500ms -> NOT a tap
eq(opened.length, 0, 'a slow press past TAP_MS does not launch');
ok(feed.classList.contains('selected'), 'a slow press selects the icon instead');

console.log('# A5. dblclick still launches (back-compat / idempotent)');
opened.length = 0;
iconOf('feed').dispatch('dblclick', baseEvent(iconOf('feed')));
eq(opened[0], 'feed', 'dblclick remains a launch path');

console.log('# A6. boundary — tap is graceful when openPanel is missing (no crash)');
opened.length = 0;
const savedOpen = sandbox.openPanel;
sandbox.openPanel = undefined;                                              // host helper absent
let threw = false;
try {
  feed = iconOf('feed');
  feed.dispatch('pointerdown', pe('pointerdown', 110, 110, 'mouse', 6000));
  feed.dispatch('pointerup', pe('pointerup', 111, 111, 'mouse', 6100));
} catch (e) { threw = true; }
ok(!threw, 'a tap with no openPanel helper does not throw');
eq(opened.length, 0, 'nothing launched when the helper is gone');
sandbox.openPanel = savedOpen;

// ── B. CONTEXT MENU BUILDS THE RIGHT ITEMS PER TARGET ───────────────────────
console.log('# B1. ICON menu (not installed) — Open / Rename / Properties / Remove');
lastItems = null; opened.length = 0;
feed = iconOf('feed');
feed.dispatch('contextmenu', pe('contextmenu', 120, 120, 'mouse', 4000));
ok(lastItems, 'right-click on an icon opened a menu');
let il = labelsOf(lastItems);
ok(il.indexOf('Open') >= 0 && il.indexOf('Rename') >= 0 && il.indexOf('Properties') >= 0,
   'icon menu offers Open / Rename / Properties  (got ' + JSON.stringify(il) + ')');
ok(il.indexOf('Remove from desktop') >= 0 && il.indexOf('Uninstall') < 0,
   'a non-installed app offers "Remove from desktop", not "Uninstall"');
itemBy(lastItems, 'Open').onClick();
eq(opened[0], 'feed', 'icon-menu Open routes through launch -> openPanel(feed)');
W.HartCtxMenu.close();

console.log('# B2. ICON menu (installed app) — offers a danger "Uninstall"');
W.hartPinIcon('store_app');                                                 // place the installed app on the desktop
const storeIcon = iconOf('store_app');
ok(storeIcon, 'installed app pinned to desktop');
lastItems = null;
storeIcon.dispatch('contextmenu', pe('contextmenu', 130, 130, 'mouse', 4100));
const stl = labelsOf(lastItems);
ok(stl.indexOf('Uninstall') >= 0 && stl.indexOf('Remove from desktop') < 0,
   'an installed app offers "Uninstall" instead of "Remove from desktop"');
ok(itemBy(lastItems, 'Uninstall').danger === true, 'Uninstall is flagged danger');
W.HartCtxMenu.close();

console.log('# B3. DESKTOP background menu — full action set');
lastItems = null; opened.length = 0;
document.dispatch('contextmenu', ctxEvent(fakeTarget({ classes: ['wallpaper'] }), 140, 140));
ok(lastItems, 'right-click on empty desktop opened a menu');
const dl = labelsOf(lastItems);
['Personalize', 'Change wallpaper', 'Add app to desktop', 'New folder', 'Auto-arrange icons', 'Display settings', 'Refresh']
  .forEach(function (l) { ok(dl.indexOf(l) >= 0, 'desktop menu offers "' + l + '"'); });
itemBy(lastItems, 'Personalize').onClick();
eq(opened[0], 'wallpaper_manager', 'Personalize opens the wallpaper manager panel');
W.HartCtxMenu.close();

console.log('# B4. WINDOW titlebar menu — items + raises window + Close routes through');
lastItems = null; calls.bringToFront.length = 0; calls.closePanel.length = 0;
const panelFake = { getAttribute: (k) => (k === 'data-panel-id' ? 'win1' : null) };
const titlebarFake = fakeTarget({ closest: { '.panel-titlebar': {}, '.panel[data-panel-id]': panelFake } });
document.dispatch('contextmenu', ctxEvent(titlebarFake, 300, 60));
ok(lastItems, 'right-click on a titlebar opened a menu');
const wl = labelsOf(lastItems);
ok(wl.indexOf('Minimize') >= 0 && wl.indexOf('Maximize') >= 0 && wl.indexOf('Close') >= 0,
   'window menu offers Minimize / Maximize / Close  (got ' + JSON.stringify(wl) + ')');
eq(calls.bringToFront[0], 'win1', 'opening the window menu RAISES the window (multi-window focus)');
itemBy(lastItems, 'Close').onClick();
eq(calls.closePanel[0], 'win1', 'window-menu Close routes through closePanel(win1)');
W.HartCtxMenu.close();

console.log('# B5. WINDOW menu reflects state — a maximized panel shows "Restore"');
const maxPanel = makeEl('div'); maxPanel.setAttribute('id', 'panel-win2'); maxPanel.classList.add('maximized');
registry['panel-win2'] = maxPanel;
lastItems = null;
const panelFake2 = { getAttribute: (k) => (k === 'data-panel-id' ? 'win2' : null) };
const titlebarFake2 = fakeTarget({ closest: { '.panel-titlebar': {}, '.panel[data-panel-id]': panelFake2 } });
document.dispatch('contextmenu', ctxEvent(titlebarFake2, 320, 60));
const wl2 = labelsOf(lastItems);
ok(wl2.indexOf('Restore') >= 0 && wl2.indexOf('Maximize') < 0,
   'a maximized window shows "Restore" not "Maximize"');
W.HartCtxMenu.close();

console.log('# B6. touch LONG-PRESS on the desktop == right-click (opens the desktop menu)');
lastItems = null;
// The long-press handler resolves the target by selector, so the pointerdown
// must carry a wallpaper target. A stationary 500ms hold then opens the menu.
const bgTouch = pe('pointerdown', 170, 170, 'touch', 7000);
bgTouch.target = fakeTarget({ classes: ['wallpaper'] });
document.dispatch('pointerdown', bgTouch);                                   // schedules the 500ms long-press
flushTimers();                                                              // fire it (the finger never moved)
ok(lastItems && labelsOf(lastItems).indexOf('Personalize') >= 0,
   'a stationary desktop long-press opens the desktop context menu');
W.HartCtxMenu.close();
document.dispatch('pointerup', pe('pointerup', 170, 170, 'touch', 7600));    // tidy up the marquee band

console.log('# B7. ADD-APP picker lists apps NOT yet on the desktop (happy)');
lastItems = null;
itemBy_desktop_addapp();
function itemBy_desktop_addapp() {
  // open the desktop menu, click "Add app to desktop", flush the 0ms timer.
  document.dispatch('contextmenu', ctxEvent(fakeTarget({ classes: ['wallpaper'] }), 180, 180));
  const add = itemBy(lastItems, 'Add app to desktop');
  lastItems = null;
  add.onClick();
  flushTimers();
}
const pick = labelsOf(lastItems);
ok(pick.indexOf('Terminal') >= 0, 'picker lists "Terminal" (a manifest app not on the desktop)');
ok(pick.indexOf('Feed') < 0, 'picker omits "Feed" (already on the desktop)');
W.HartCtxMenu.close();

// ── C. ESCAPE / OUTSIDE / OFFLINE DISMISSAL ─────────────────────────────────
console.log('# C1. Escape closes the menu');
W.HartCtxMenu.open([{ label: 'X', onClick() {} }], 50, 50);
ok(W.HartCtxMenu.isOpen(), 'menu open before Escape');
document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
ok(!W.HartCtxMenu.isOpen(), 'Escape dismisses the menu');
eq(menuInBody().length, 0, 'Escape removed the menu element from the DOM');

console.log('# C2. an outside pointerdown closes the menu');
W.HartCtxMenu.open([{ label: 'X', onClick() {} }], 50, 50);
const outside = makeEl('div');
document.dispatch('pointerdown', { target: outside, button: 0, pointerType: 'mouse',
  preventDefault() {}, stopPropagation() {} });
ok(!W.HartCtxMenu.isOpen(), 'a click outside the menu dismisses it');

console.log('# C3. OFFLINE boundary — contextmenu falls through to the shell when HartCtxMenu is absent');
const savedCtx = sandbox.HartCtxMenu;
sandbox.HartCtxMenu = undefined;                                            // module not loaded yet
const ev = ctxEvent(fakeTarget({ classes: ['wallpaper'] }), 200, 200);
let crashed = false;
try { document.dispatch('contextmenu', ev); } catch (e) { crashed = true; }
ok(!crashed, 'a right-click with no HartCtxMenu does not crash');
ok(!ev.wasPrevented(), 'the event is NOT swallowed (left for the shell\'s own #ctx-menu)');
sandbox.HartCtxMenu = savedCtx;

// ── D. EDGE-FLIP / CLAMP KEEPS THE MENU ON-SCREEN ───────────────────────────
console.log('# D1. near the right+bottom corner the menu flips left + up');
// viewport 1200x800, menu 200x250 (shim offsets) -> open at (1150,700).
W.HartCtxMenu.open([{ label: 'EdgeA', onClick() {} }, { label: 'EdgeB', onClick() {} }], 1150, 700);
let menuEl = menuInBody()[0];
eq(menuEl.style.left, '950px', 'flipped left of the pointer near the right edge (1150-200)');
eq(menuEl.style.top, '450px', 'flipped above the pointer near the bottom edge (700-250)');
W.HartCtxMenu.close();

console.log('# D2. near the top-left origin the menu clamps to an 8px margin (never negative)');
W.HartCtxMenu.open([{ label: 'A', onClick() {} }], 0, 0);
menuEl = menuInBody()[0];
eq(menuEl.style.left, '8px', 'left clamped to the 8px pad at x=0');
eq(menuEl.style.top, '8px', 'top clamped to the 8px pad at y=0');
W.HartCtxMenu.close();

console.log('# D3. a tiny viewport smaller than the menu still clamps on-screen (no off-screen)');
sandbox.innerWidth = 120; sandbox.innerHeight = 120;                        // smaller than the 200x250 menu
W.HartCtxMenu.open([{ label: 'A', onClick() {} }], 60, 60);
menuEl = menuInBody()[0];
ok(parseInt(menuEl.style.left, 10) >= 8, 'left stays >= the 8px margin even when the menu overflows the viewport');
ok(parseInt(menuEl.style.top, 10) >= 8, 'top stays >= the 8px margin even when the menu overflows the viewport');
W.HartCtxMenu.close();
sandbox.innerWidth = 1200; sandbox.innerHeight = 800;

// ── E. HartCtxMenu ROBUSTNESS (empty / disabled / error / no-stack) ─────────
console.log('# E1. empty boundary — open([]) and open(null) render nothing');
W.HartCtxMenu.open([], 10, 10);
ok(!W.HartCtxMenu.isOpen(), 'open([]) opens no menu');
W.HartCtxMenu.open(null, 10, 10);
ok(!W.HartCtxMenu.isOpen(), 'open(null) opens no menu');
eq(menuInBody().length, 0, 'no menu element leaked into the DOM');

console.log('# E2. a disabled row is non-actionable (skipped by keyboard + inert on click)');
let disClicked = false, liveClicked = false;
W.HartCtxMenu.open([
  { label: 'Off', disabled: true, onClick: function () { disClicked = true; } },
  { label: 'On', onClick: function () { liveClicked = true; } }
], 50, 50);
menuEl = menuInBody()[0];
const disabledRow = menuEl._kids.find(k => clsOf(k).includes('hart-ctx-item') && clsOf(k).includes('disabled'));
ok(disabledRow, 'disabled row was rendered');
disabledRow.dispatch('click', baseEvent(disabledRow));
ok(!disClicked, 'clicking a disabled row does nothing (no onClick wired)');
document.dispatch('keydown', { key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
document.dispatch('keydown', { key: 'Enter', preventDefault() {}, stopPropagation() {} });
ok(liveClicked && !disClicked, 'ArrowDown+Enter lands on the only ENABLED row and activates it');
ok(!W.HartCtxMenu.isOpen(), 'keyboard activation also closes the menu');

console.log('# E3. error boundary — an onClick that throws is caught, menu still closes');
let boomRan = false;
W.HartCtxMenu.open([{ label: 'Boom', onClick: function () { boomRan = true; throw new Error('kaboom'); } }], 50, 50);
menuEl = menuInBody()[0];
const boomRow = menuEl._kids.find(k => clsOf(k).includes('hart-ctx-item'));
let bubbled = false;
try { boomRow.dispatch('click', baseEvent(boomRow)); } catch (e) { bubbled = true; }
ok(boomRan, 'the throwing handler did run');
ok(!bubbled, 'the thrown error was swallowed (did not escape the click handler)');
ok(!W.HartCtxMenu.isOpen(), 'menu closed despite the handler throwing');

console.log('# E4. open() never stacks two menus');
W.HartCtxMenu.open([{ label: 'First', onClick() {} }], 50, 50);
W.HartCtxMenu.open([{ label: 'Second', onClick() {} }], 80, 80);
eq(menuInBody().length, 1, 'a second open() replaces the first (exactly one menu in the DOM)');
W.HartCtxMenu.close();
ok(!W.HartCtxMenu.isOpen(), 'close() clears the menu');

// ════════════════════════════════════════════════════════════════════════
console.log('\n' + (failures ? ('RESULT: ' + failures + ' FAILED, ' + passes + ' passed')
                              : ('RESULT: ALL ' + passes + ' PASSED')));
process.exit(failures ? 1 : 0);

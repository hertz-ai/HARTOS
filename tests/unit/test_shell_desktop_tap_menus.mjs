/*
 * Behavioural test for the HART OS desktop TAP fix + CONTEXT MENUS.
 *
 * Drives the REAL static modules through their public surface on a tiny,
 * dependency-free DOM shim (CI runners here have no jsdom):
 *   integrations/agent_engine/static/hartContextMenu.js  (window.HartCtxMenu)
 *   integrations/agent_engine/static/hartDesktop.js       (tap/drag + menus)
 *
 * It asserts OBSERVABLE behaviour, never source strings:
 *   1. TAP regression — a quick press+release on an icon LAUNCHES (the bug was:
 *      icons opened only on dblclick, which never fires on a touchscreen tap).
 *      Covered for MOUSE and TOUCH; a touch long-press opens the icon menu.
 *   2. DRAG does NOT launch — a press that moves past threshold rearranges +
 *      persists (HartSession.set) and must NOT call openPanel.
 *   3. dblclick still launches (kept harmless / back-compat).
 *   4. ICON / DESKTOP / WINDOW context menus offer the right actions, and each
 *      action routes to the EXISTING helper (openPanel / closePanel / launch)
 *      with the right argument. The window menu also raises the window first
 *      (bringToFront == multi-window focus).
 *   5. HartCtxMenu module — click activates onClick + closes, Escape closes,
 *      outside pointerdown closes, keyboard Enter activates, and place() FLIPS
 *      the menu at the right/bottom screen edges (never off-screen).
 *
 * Run:  node tests/unit/test_shell_desktop_tap_menus.mjs
 * (A Python wrapper, test_shell_desktop_tap_menus.py, shells out so pytest/CI
 *  picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC_CTX = join(STATIC, 'hartContextMenu.js');
const SRC_DESK = join(STATIC, 'hartDesktop.js');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM shim ───────────────────────────────────────────────────────
function clsOf(el) { return (el._attrs.class || '').split(/\s+/); }
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _bySel: {}, _listeners: {},
    style: {}, offsetWidth: 200, offsetHeight: 250,
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
// A target whose .closest()/.classList answer a scripted map (for document-level
// contextmenu handlers that resolve titlebars / wallpaper by selector).
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

// ── Controllable timers (long-press must NOT auto-fire during a quick tap) ──
let timerSeq = 1;
const timers = new Map();
function flushTimers() { const fns = Array.from(timers.values()); timers.clear(); fns.forEach(f => { try { f(); } catch (e) {} }); }

// ── Spies / mocks (the side-effects we assert) ──
const opened = [];
const calls = { bringToFront: [], closePanel: [], minimizePanel: [], toggleMax: [] };
let lastSaved = null;
const sessionBlob = {};

const sandbox = {
  document, console,
  innerWidth: 1200, innerHeight: 800,
  setTimeout: (fn, ms) => { const id = timerSeq++; timers.set(id, fn); return id; },
  clearTimeout: (id) => { timers.delete(id); },
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  matchMedia: () => ({ matches: false }),
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  MANIFEST: { feed: { title: 'Feed', icon: 'rss_feed' },
              recipes: { title: 'Recipes', icon: 'menu_book' },
              appearance: { title: 'Appearance', icon: 'palette' } },
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
// hartDesktop init() runs injectCtxMenu (which then skips the <script> inject).
vm.runInContext(readFileSync(SRC_CTX, 'utf8'), sandbox, { filename: 'hartContextMenu.js' });
vm.runInContext(readFileSync(SRC_DESK, 'utf8'), sandbox, { filename: 'hartDesktop.js' });

const W = sandbox.window;
const iconOf = (id) => layer._kids.find(k => k._attrs['data-id'] === id);

// Capture the items every menu opens with, while still rendering the real menu.
let lastItems = null;
const realOpen = W.HartCtxMenu.open;
W.HartCtxMenu.open = function (items, x, y) { lastItems = items; return realOpen.call(this, items, x, y); };
const labelsOf = (items) => (items || []).filter(it => it && !it.sep).map(it => it.label);
const itemBy = (items, label) => (items || []).find(it => it && it.label === label);

// pointer event factory
function pe(type, x, y, ptype, ts) {
  return { type: type, button: 0, clientX: x, clientY: y, pointerType: ptype || 'mouse',
           pointerId: 1, timeStamp: ts || 0, preventDefault() {}, stopPropagation() {} };
}

// ════════════════════════════════════════════════════════════════════════
console.log('# 0. default icons rendered');
ok(W.HartCtxMenu && typeof W.HartCtxMenu.open === 'function', 'HartCtxMenu module loaded');
ok(iconOf('feed') && iconOf('recipes'), 'default icons rendered (feed + recipes present)');

console.log('# 1. TAP launches — MOUSE');
opened.length = 0;
let feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 100, 100, 'mouse', 1000));
feed.dispatch('pointerup', pe('pointerup', 101, 101, 'mouse', 1120));   // 120ms, 2px -> tap
eq(opened.length, 1, 'a quick mouse press+release launches exactly once');
eq(opened[0], 'feed', 'launch reused openPanel with the icon id');

console.log('# 2. TAP launches — TOUCH (the real regression)');
opened.length = 0;
feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 200, 200, 'touch', 2000));  // schedules long-press
feed.dispatch('pointerup', pe('pointerup', 202, 203, 'touch', 2150));      // 150ms, 5px -> tap (clears long-press)
flushTimers();                                                            // any leftover timer must be a no-op now
eq(opened.length, 1, 'a touch tap launches (dblclick-only bug fixed)');
eq(opened[0], 'feed', 'touch tap reused openPanel with the icon id');

console.log('# 3. DRAG does NOT launch, it rearranges + persists');
opened.length = 0; lastSaved = null;
let rec = iconOf('recipes');
const startLeft = parseInt(rec.style.left, 10) || 0;
rec.dispatch('pointerdown', pe('pointerdown', 300, 300, 'mouse', 3000));
rec.dispatch('pointermove', pe('pointermove', 360, 360, 'mouse', 3050));   // 120px -> past threshold
rec.dispatch('pointerup', pe('pointerup', 360, 360, 'mouse', 3200));
eq(opened.length, 0, 'a drag does NOT launch the app');
ok(Array.isArray(lastSaved), 'drag committed -> persist() wrote desktop_icons');
ok((parseInt(rec.style.left, 10) || 0) !== startLeft, 'icon moved to a new grid column');

console.log('# 4. dblclick still launches (back-compat, idempotent)');
opened.length = 0;
iconOf('feed').dispatch('dblclick', baseEvent(iconOf('feed')));
eq(opened[0], 'feed', 'dblclick remains a launch path');

console.log('# 5. ICON context menu — actions + wiring');
lastItems = null; opened.length = 0;
feed = iconOf('feed');
feed.dispatch('contextmenu', pe('contextmenu', 120, 120, 'mouse', 4000));
ok(lastItems, 'right-click on an icon opened a menu');
const il = labelsOf(lastItems);
ok(il.indexOf('Open') >= 0 && il.indexOf('Rename') >= 0 && il.indexOf('Properties') >= 0,
   'icon menu offers Open / Rename / Properties  (got ' + JSON.stringify(il) + ')');
ok(il.indexOf('Remove from desktop') >= 0 || il.indexOf('Uninstall') >= 0,
   'icon menu offers Remove/Uninstall');
itemBy(lastItems, 'Open').onClick();
eq(opened[0], 'feed', 'icon-menu Open routes through launch -> openPanel(feed)');
W.HartCtxMenu.close();

console.log('# 6. DESKTOP background context menu — actions + wiring');
lastItems = null; opened.length = 0;
document.dispatch('contextmenu', { target: fakeTarget({ classes: ['wallpaper'] }),
  clientX: 140, clientY: 140, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} });
ok(lastItems, 'right-click on empty desktop opened a menu');
const dl = labelsOf(lastItems);
['Personalize', 'Change wallpaper', 'New folder', 'Display settings', 'Refresh'].forEach(function (l) {
  ok(dl.indexOf(l) >= 0, 'desktop menu offers "' + l + '"');
});
itemBy(lastItems, 'Personalize').onClick();
eq(opened[0], 'wallpaper_manager', 'Personalize opens the wallpaper manager panel');
W.HartCtxMenu.close();

console.log('# 7. WINDOW titlebar context menu — actions + focus + wiring');
lastItems = null; calls.bringToFront.length = 0; calls.closePanel.length = 0;
const panelFake = { getAttribute: (k) => (k === 'data-panel-id' ? 'win1' : null) };
const titlebarFake = fakeTarget({ closest: { '.panel-titlebar': {}, '.panel[data-panel-id]': panelFake } });
document.dispatch('contextmenu', { target: titlebarFake, clientX: 300, clientY: 60,
  preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} });
ok(lastItems, 'right-click on a titlebar opened a menu');
const wl = labelsOf(lastItems);
ok(wl.indexOf('Minimize') >= 0 && wl.indexOf('Maximize') >= 0 && wl.indexOf('Close') >= 0,
   'window menu offers Minimize / Maximize / Close  (got ' + JSON.stringify(wl) + ')');
eq(calls.bringToFront[0], 'win1', 'opening the window menu RAISES the window (multi-window focus)');
itemBy(lastItems, 'Close').onClick();
eq(calls.closePanel[0], 'win1', 'window-menu Close routes through closePanel(win1)');
W.HartCtxMenu.close();

console.log('# 8. TOUCH long-press on an icon opens its menu');
lastItems = null;
feed = iconOf('feed');
feed.dispatch('pointerdown', pe('pointerdown', 150, 150, 'touch', 5000));
flushTimers();                                  // fire the 500ms long-press
ok(lastItems && labelsOf(lastItems).indexOf('Open') >= 0, 'a stationary touch long-press opens the icon menu');
W.HartCtxMenu.close();

console.log('# 9. HartCtxMenu — click activates + closes');
let clicked = null;
W.HartCtxMenu.open([
  { icon: 'star', label: 'Alpha', onClick: function () { clicked = 'Alpha'; } },
  { sep: true },
  { icon: 'bolt', label: 'Beta', onClick: function () { clicked = 'Beta'; } }
], 100, 100);
ok(W.HartCtxMenu.isOpen(), 'menu is open after open()');
let menuEl = document.body._kids.find(k => k._attrs.id === 'hart-ctxmenu');
ok(menuEl, 'menu element appended to <body>');
const itemRows = menuEl._kids.filter(k => clsOf(k).includes('hart-ctx-item'));
eq(itemRows.length, 2, 'two actionable rows rendered (separator excluded)');
itemRows[1].dispatch('click', baseEvent(itemRows[1]));
eq(clicked, 'Beta', 'clicking a row runs its onClick');
ok(!W.HartCtxMenu.isOpen(), 'menu closes after activation');

console.log('# 10. HartCtxMenu — Escape closes');
W.HartCtxMenu.open([{ label: 'X', onClick() {} }], 50, 50);
ok(W.HartCtxMenu.isOpen(), 'open before Escape');
document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
ok(!W.HartCtxMenu.isOpen(), 'Escape dismisses the menu');

console.log('# 11. HartCtxMenu — outside pointerdown closes');
W.HartCtxMenu.open([{ label: 'X', onClick() {} }], 50, 50);
const outside = makeEl('div');                  // not inside the menu
document.dispatch('pointerdown', { target: outside, button: 0, pointerType: 'mouse',
  preventDefault() {}, stopPropagation() {} });
ok(!W.HartCtxMenu.isOpen(), 'a click outside the menu dismisses it');

console.log('# 12. HartCtxMenu — keyboard Enter activates the active row');
let kClicked = null;
W.HartCtxMenu.open([
  { label: 'One', onClick: function () { kClicked = 'One'; } },
  { label: 'Two', onClick: function () { kClicked = 'Two'; } }
], 50, 50);
document.dispatch('keydown', { key: 'ArrowDown', preventDefault() {}, stopPropagation() {} });
document.dispatch('keydown', { key: 'Enter', preventDefault() {}, stopPropagation() {} });
eq(kClicked, 'One', 'ArrowDown + Enter activates the first item (keyboard navigable)');
ok(!W.HartCtxMenu.isOpen(), 'Enter activation also closes the menu');

console.log('# 13. HartCtxMenu — place() FLIPS at the right/bottom edges');
// viewport 1200x800, menu 200x250 (shim offsetWidth/Height). Open near the
// corner: it must flip left + up so it is never off-screen.
W.HartCtxMenu.open([{ label: 'EdgeA', onClick() {} }, { label: 'EdgeB', onClick() {} }], 1150, 700);
menuEl = document.body._kids.find(k => k._attrs.id === 'hart-ctxmenu');
eq(menuEl.style.left, '950px', 'flipped left of the pointer near the right edge (1150-200)');
eq(menuEl.style.top, '450px', 'flipped above the pointer near the bottom edge (700-250)');
W.HartCtxMenu.close();

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

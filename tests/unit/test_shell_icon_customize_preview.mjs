/*
 * Regression test for two REAL defects in the HART OS desktop layer that the
 * sibling harness (test_shell_desktop_tap_menus.mjs) could NOT catch because its
 * DOM shim returns a truthy stub for EVERY querySelector (so a genuinely-missing
 * node never reads back as null).
 *
 *   FIX 1 — applyIconVisual() crashed on the customize-dialog PREVIEW.
 *     The preview element (.hic-prev) is a glyph plate with a .di-glyph child and
 *     NO .di-label child. applyIconVisual did an UNCONDITIONAL
 *       el.querySelector('.di-label').textContent = ...
 *     which is null.textContent in a real browser -> TypeError thrown mid-setup,
 *     so the rest of hartCustomizeIcon (Save / Cancel / Escape wiring) never ran:
 *     a dead, undismissable modal. This shim faithfully returns null for the
 *     preview's absent .di-label, so the un-fixed code throws and the un-fixed
 *     dialog is NOT dismissable -> the test below fails without the guard.
 *
 *   FIX 2 — the contextmenu takeover must NOT swallow the event before the
 *     async-injected HartCtxMenu module has loaded, or an early right-click is a
 *     dead no-op (the shell's own #ctx-menu fallback was suppressed). With
 *     window.HartCtxMenu absent, a desktop right-click must fall through
 *     (preventDefault / stopImmediatePropagation NOT called).
 *
 * Run:  node tests/unit/test_shell_icon_customize_preview.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC_BRAND = join(STATIC, 'hartBrandArt.js');   // shared brand-art (glyph + gradient)
const SRC_DESK = join(STATIC, 'hartDesktop.js');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── Faithful-enough DOM shim ────────────────────────────────────────────────
// The one fidelity that matters: a class selector that has no matching child
// reads back as null (real DOM), EXCEPT for the lazily-fabricated structural
// children a real element would have. The customize PREVIEW is explicitly
// flagged _noLabel so its .di-label query returns null, exactly like the real
// <div class="hic-prev"><div class="di-glyph"></div></div> markup.
function hasClass(el, c) { return ((el._attrs.class || '').split(/\s+/)).indexOf(c) >= 0; }
function findDesc(el, pred) {
  for (const k of el._kids) { if (pred(k)) return k; const r = findDesc(k, pred); if (r) return r; }
  return null;
}
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _bySel: {}, _listeners: {}, _noLabel: false,
    _glyph: null, _label: null, _mi: null, _preview: null,
    style: {}, offsetWidth: 120, offsetHeight: 140, offsetLeft: 0, offsetTop: 0,
    value: '', textContent: '', _innerHTML: '', parentNode: null,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; this._bySel = {}; this._glyph = null; this._label = null; this._mi = null; this._preview = null; },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === 'class') this.classList._s = new Set(String(v).split(/\s+/)); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    appendChild(c) { c.parentNode = el; el._kids.push(c); return c; },
    removeChild(c) { el._kids = el._kids.filter(k => k !== c); c.parentNode = null; return c; },
    contains(node) { if (node === el) return true; for (const k of el._kids) { if (k === node || (k.contains && k.contains(node))) return true; } return false; },
    addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { if (el._listeners[t]) el._listeners[t] = el._listeners[t].filter(f => f !== fn); },
    dispatch(t, ev) { (el._listeners[t] || []).slice().forEach(fn => fn(ev || { target: el, preventDefault() {}, stopPropagation() {} })); },
    focus() {}, select() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; },
    querySelector(sel) {
      if (sel[0] === '#') {
        const id = sel.slice(1);
        const found = findDesc(el, k => k._attrs.id === id);
        if (found) return found;
        if (!el._bySel[sel]) { const s = makeEl('div'); s.setAttribute('id', id); s.parentNode = el; el._bySel[sel] = s; }
        return el._bySel[sel];
      }
      const di = /^\.desktop-icon\[data-id="(.+)"\]$/.exec(sel);
      if (di) return findDesc(el, k => k._attrs['data-id'] === di[1] && hasClass(k, 'desktop-icon'));
      if (sel === '.hic-prev') { if (!el._preview) { const p = makeEl('div'); p._noLabel = true; p.parentNode = el; el._preview = p; } return el._preview; }
      if (sel === '.di-glyph') { if (!el._glyph) { const g = makeEl('div'); g.parentNode = el; el._glyph = g; } return el._glyph; }
      if (sel === '.di-label') { if (el._noLabel) return null; if (!el._label) { const l = makeEl('div'); l.parentNode = el; el._label = l; } return el._label; }
      if (sel === '.mi') { if (!el._mi) { const m = makeEl('span'); m.parentNode = el; el._mi = m; } return el._mi; }
      if (!el._bySel[sel]) { const s = makeEl('div'); s.parentNode = el; el._bySel[sel] = s; }
      return el._bySel[sel];
    },
    querySelectorAll(sel) {
      if (sel === '.desktop-icon') return el._kids.filter(k => hasClass(k, 'desktop-icon'));
      if (sel === '.desktop-icon.selected') return el._kids.filter(k => hasClass(k, 'desktop-icon') && k.classList.contains('selected'));
      return [];
    },
    get className() { return el._attrs.class || ''; },
    set className(v) { el.setAttribute('class', v); },
    get id() { return el._attrs.id || ''; },
    set id(v) { el._attrs.id = String(v); }
  };
  return el;
}

function makeWorld() {
  const registry = {};
  const document = {
    readyState: 'complete', documentElement: makeEl('html'), head: makeEl('head'), body: makeEl('body'),
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
  document.body.appendChild(layer);
  const saved = { desktop_icons: null };
  const blob = {};
  const sandbox = {
    document, console, innerWidth: 1280, innerHeight: 800,
    setTimeout: () => 0, clearTimeout: () => {}, requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    matchMedia: () => ({ matches: false }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    MANIFEST: { feed: { title: 'Feed', icon: 'rss_feed' }, recipes: { title: 'Recipes', icon: 'menu_book' }, appearance: { title: 'Appearance', icon: 'palette' } },
    openPanel: () => {},
    addEventListener() {}, removeEventListener() {},
    HartSession: { ready(cb) { cb(blob); }, get(k) { return Object.prototype.hasOwnProperty.call(blob, k) ? blob[k] : undefined; }, set(k, v) { blob[k] = v; if (k === 'desktop_icons') saved.desktop_icons = v; } }
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  // hartDesktop renders icon glyphs/art tiles through the shared window.HartBrandArt
  // (loaded first in the real shell); define it before the module runs.
  vm.runInContext(readFileSync(SRC_BRAND, 'utf8'), sandbox, { filename: 'hartBrandArt.js' });
  vm.runInContext(readFileSync(SRC_DESK, 'utf8'), sandbox, { filename: 'hartDesktop.js' });
  return { sandbox, document, layer, saved };
}

// ════════════════════════════════════════════════════════════════════════
console.log('# A. Customize dialog PREVIEW does not crash (FIX 1)');
const W = makeWorld();
const win = W.sandbox.window;
const iconFeed = W.layer._kids.find(k => k._attrs['data-id'] === 'feed');
ok(iconFeed, 'default "feed" icon rendered (setup)');

let threw = false;
try { win.hartCustomizeIcon('feed'); } catch (e) { threw = true; console.log('   threw: ' + (e && e.message)); }
ok(!threw, 'hartCustomizeIcon() completes WITHOUT throwing (null .di-label guarded)');

const dlg = W.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
ok(dlg, 'customize dialog was appended to <body>');

// Save must be wired (proves setup ran PAST refreshPreview) and must persist.
W.saved.desktop_icons = null;
const saveBtn = dlg.querySelector('#hic-save');
saveBtn.dispatch('click', { target: saveBtn, preventDefault() {}, stopPropagation() {} });
ok(Array.isArray(W.saved.desktop_icons), 'Save is wired -> applyIconVisual + persist() ran (HartSession.set)');
ok(!W.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize'), 'Save closed the dialog');

// Escape must dismiss it (the un-fixed dead modal could NOT be closed).
threw = false;
try { win.hartCustomizeIcon('feed'); } catch (e) { threw = true; }
ok(!threw, 're-open still does not throw');
const dlg2 = W.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
ok(dlg2, 'dialog re-opened');
dlg2.dispatch('keydown', { key: 'Escape', target: dlg2, tagName: 'DIV', preventDefault() {}, stopPropagation() {} });
ok(!W.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize'), 'Escape dismisses the dialog (not a dead modal)');

console.log('# B. Right-click falls through to the shell until HartCtxMenu loads (FIX 2)');
ok(!win.HartCtxMenu, 'HartCtxMenu is NOT loaded in this world (simulating the async-inject gap)');
let prevented = false, stoppedImm = false;
const wallpaper = { classList: { contains: (c) => c === 'wallpaper' }, closest: () => null };
W.document.dispatch('contextmenu', {
  target: wallpaper, clientX: 200, clientY: 200,
  preventDefault() { prevented = true; }, stopPropagation() {}, stopImmediatePropagation() { stoppedImm = true; }
});
ok(!prevented, 'desktop right-click is NOT preventDefault-swallowed while HartCtxMenu is absent');
ok(!stoppedImm, 'event is NOT stopImmediatePropagation-ed -> the shell #ctx-menu fallback still runs');

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

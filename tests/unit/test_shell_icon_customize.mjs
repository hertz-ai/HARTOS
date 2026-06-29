/*
 * Behavioural test for desktop-icon CUSTOMIZATION (glyph / label / color).
 *
 * Drives the REAL integrations/agent_engine/static/hartDesktop.js through its
 * public API on a tiny dependency-free DOM shim (CI runners here have no jsdom):
 *   1. load the real module  -> init() renders the default icons
 *   2. window.hartCustomizeIcon('feed')  -> builds the real dialog, seeds inputs
 *   3. type a Material glyph + label + color, click Save
 *   4. assert the icon ELEMENT updated (observable DOM)   AND
 *      HartSession.set('desktop_icons', …) persisted the override (side-effect)
 *   5. repeat with an EMOJI glyph -> asserts it is NOT wrapped in the icon font
 *   6. Reset -> override cleared from both the DOM and the persisted blob
 *
 * Run:  node tests/unit/test_shell_icon_customize.mjs
 * (A Python wrapper, test_shell_icon_customize.py, shells out to this so the
 *  suite/CI picks it up via pytest too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const SRC_BRAND = join(STATIC, 'hartBrandArt.js');   // shared brand-art (glyph + gradient)
const SRC = join(STATIC, 'hartDesktop.js');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM shim ───────────────────────────────────────────────────────
// Real enough to run the module: elements track attributes/style/children, and
// innerHTML resets children (the code re-queries known selectors afterwards, so
// querySelector lazily materializes a child stub per selector — no HTML parser).
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _bySel: {}, _listeners: {},
    style: {}, classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    textContent: '', _innerHTML: '', parentNode: null, pointerType: '',
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; this._kids = []; this._bySel = {}; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild(c) { this._kids = this._kids.filter(k => k !== c); c.parentNode = null; return c; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener() {},
    dispatch(t, ev) { (this._listeners[t] || []).forEach(fn => fn(ev || { target: el, preventDefault() {}, stopPropagation() {} })); },
    focus() {}, select() {}, setPointerCapture() {}, releasePointerCapture() {},
    closest() { return null; },
    querySelector(sel) {
      // exact-id match against real children first
      for (const k of this._kids) if (k._attrs.id && ('#' + k._attrs.id) === sel) return k;
      // `.desktop-icon[data-id="X"]` — match a real child by its data-id
      const m = /^\.desktop-icon\[data-id="(.+)"\]$/.exec(sel);
      if (m) return this._kids.find(k => k._attrs['data-id'] === m[1] &&
                                          (k._attrs.class || '').split(' ').includes('desktop-icon')) || null;
      if (!this._bySel[sel]) { const s = makeEl('div'); s.parentNode = el; this._bySel[sel] = s; }
      return this._bySel[sel];
    },
    querySelectorAll(sel) {
      if (sel === '.desktop-icon') return this._kids.filter(k => (k._attrs.class || '').split(' ').includes('desktop-icon'));
      if (sel === '.desktop-icon.selected') return this._kids.filter(k => k.classList.contains('selected'));
      return [];
    },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; },
    // Real DOM reflects the `id` / `dataset`-free property to the attribute.
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); }
  };
  return el;
}

const registry = {};   // id -> element (so getElementById finds layer + ctx-menu)
const document = {
  readyState: 'complete',
  documentElement: makeEl('html'),
  body: makeEl('body'),
  _listeners: {},
  createElement: (t) => makeEl(t),
  getElementById(id) {
    if (registry[id]) return registry[id];
    // also search the live tree (dialogs are appended to <body>, not registered)
    const walk = (n) => { for (const k of n._kids) { if (k._attrs.id === id) return k; const r = walk(k); if (r) return r; } return null; };
    return walk(this.body);
  },
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
};
const layer = makeEl('div'); layer.setAttribute('id', 'hart-desktop'); registry['hart-desktop'] = layer;
const ctxMenu = makeEl('div'); ctxMenu.setAttribute('id', 'ctx-menu'); registry['ctx-menu'] = ctxMenu;
document.body.appendChild(layer);

// Real-ish HartSession: capture every persisted blob (the side-effect we assert).
let lastSaved = null;
const sessionBlob = {};
const sandbox = {
  document, console,
  setTimeout: (fn) => { fn(); return 0; },   // run deferred work inline
  clearTimeout() {}, requestAnimationFrame() { return 0; }, cancelAnimationFrame() {},
  getComputedStyle: () => ({ getPropertyValue: () => '#6c63ff' }),
  // `appearance` carries a manifest-stamped colour (shell_manifest.with_icon_colors)
  // to exercise the de-monochrome path: it should RENDER coloured but a plain Save
  // must NOT persist it as a per-icon override (blob stays lean).
  MANIFEST: { feed: { title: 'Feed', icon: 'rss_feed' }, recipes: { title: 'Recipes', icon: 'menu_book' },
              appearance: { title: 'Appearance', icon: 'palette', color: '#22cc88' } },
  ctxItem: (icon, label, action) => '<i>' + icon + '|' + label + '|' + action + '</i>',
  ctxSep: () => '<sep>',
  openPanel() {},
  HartSession: {
    ready(cb) { cb(sessionBlob); },
    get(k, d) { return Object.prototype.hasOwnProperty.call(sessionBlob, k) ? sessionBlob[k] : d; },
    set(k, v) { sessionBlob[k] = v; if (k === 'desktop_icons') lastSaved = v; }
  }
};
sandbox.window = sandbox;            // classic-script `window` is the global
vm.createContext(sandbox);
// hartDesktop renders glyphs/art tiles through the shared window.HartBrandArt
// (loaded first in the real shell), so define it here before the module runs.
vm.runInContext(readFileSync(SRC_BRAND, 'utf8'), sandbox, { filename: 'hartBrandArt.js' });
vm.runInContext(readFileSync(SRC, 'utf8'), sandbox, { filename: 'hartDesktop.js' });

// ── 0. defaults rendered (feed present) ──
const iconOf = (id) => layer._kids.find(k => k._attrs['data-id'] === id);
ok(iconOf('feed'), 'default icons rendered (feed icon present)');

// ── 1. open the customize dialog for `feed` ──
sandbox.window.hartCustomizeIcon('feed');
const dlg = registry['hart-icon-customize'] || sandbox.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
ok(dlg, 'customize dialog appended to <body>');
// the dialog seeds inputs from the MANIFEST default
const gIn = dlg.querySelector('#hic-glyph'), lIn = dlg.querySelector('#hic-label'), cIn = dlg.querySelector('#hic-color');
eq(gIn.value, 'rss_feed', 'glyph input seeded from MANIFEST default');
eq(lIn.value, 'Feed', 'label input seeded from MANIFEST default');

// ── 2. type a Material glyph + label, pick a color, Save ──
gIn.value = 'auto_awesome'; gIn.dispatch('input');
lIn.value = 'My Feed'; lIn.dispatch('input');
cIn.value = '#ff8800'; cIn.dispatch('input');                 // turns the color override on
dlg.querySelector('#hic-save').dispatch('click', { target: dlg.querySelector('#hic-save'), preventDefault() {} });

// observable DOM mutation on the real icon element
const fe = iconOf('feed');
eq(fe.getAttribute('data-ov-glyph'), 'auto_awesome', 'icon stores glyph override on data-attr');
eq(fe.getAttribute('data-ov-label'), 'My Feed', 'icon stores label override on data-attr');
eq(fe.getAttribute('data-ov-color'), '#ff8800', 'icon stores color override on data-attr');
eq(fe.querySelector('.di-label').textContent, 'My Feed', 'label element shows the override');
eq(fe.getAttribute('aria-label'), 'My Feed', 'aria-label updated for screen readers');
// glyphSpan() renders the inner markup as a string into .di-glyph.innerHTML;
// assert against that real output (the shim does not parse HTML into children).
const feGlyphHTML = fe.querySelector('.di-glyph').innerHTML;
ok(/class="mi material-icons-round"/.test(feGlyphHTML), 'Material-name glyph uses the icon font');
ok(feGlyphHTML.indexOf('color:#ff8800') >= 0, 'glyph color applied inline (overrides theme accent)');

// observable persistence side-effect: the override is in the saved blob
ok(Array.isArray(lastSaved), 'persist() wrote desktop_icons');
const saved = lastSaved.find(r => r.id === 'feed');
eq(saved.glyph, 'auto_awesome', 'persisted entry carries glyph override');
eq(saved.label, 'My Feed', 'persisted entry carries label override');
eq(saved.color, '#ff8800', 'persisted entry carries color override');
// un-customized icons stay lean (no override keys)
const recipesSaved = lastSaved.find(r => r.id === 'recipes');
ok(recipesSaved && !('glyph' in recipesSaved) && !('label' in recipesSaved) && !('color' in recipesSaved),
   'un-customized icon serializes to a plain {id,x,y} (no override noise)');

// ── 3. EMOJI glyph is NOT wrapped in the Material icon font ──
sandbox.window.hartCustomizeIcon('recipes');
const dlg2 = sandbox.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
const g2 = dlg2.querySelector('#hic-glyph');
g2.value = '🚀'; g2.dispatch('input');
dlg2.querySelector('#hic-save').dispatch('click', { target: dlg2.querySelector('#hic-save'), preventDefault() {} });
const re = iconOf('recipes');
const reGlyphBox = re.querySelector('.di-glyph');
ok(/class="mi di-emoji"/.test(reGlyphBox.innerHTML), 'emoji glyph uses a plain span, not the icon font');
ok(!/material-icons-round/.test(reGlyphBox.innerHTML), 'emoji glyph is NOT wrapped in the icon font');
eq(reGlyphBox.querySelector('.mi').textContent, '🚀', 'emoji rendered as text content');
eq(lastSaved.find(r => r.id === 'recipes').glyph, '🚀', 'emoji override persisted');

// ── 4. Reset clears the override (DOM + persisted) ──
sandbox.window.hartCustomizeIcon('feed');
const dlg3 = sandbox.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
dlg3.querySelector('#hic-reset').dispatch('click', { target: dlg3.querySelector('#hic-reset'), preventDefault() {} });
const fe2 = iconOf('feed');
ok(!fe2.hasAttribute('data-ov-glyph') && !fe2.hasAttribute('data-ov-label') && !fe2.hasAttribute('data-ov-color'),
   'Reset clears override data-attrs from the icon');
eq(fe2.querySelector('.di-label').textContent, 'Feed', 'Reset restores the MANIFEST label');
const feSaved = lastSaved.find(r => r.id === 'feed');
ok(feSaved && !('glyph' in feSaved) && !('color' in feSaved), 'Reset removes the override from the persisted blob');

// ── 5. A MANIFEST-stamped colour renders but is NOT persisted as an override ──
// (de-monochrome path: opening Customize on a manifest-coloured icon and Saving
//  without touching the swatch must keep the desktop blob lean.)
const ap = iconOf('appearance');
ok(ap.querySelector('.di-glyph').innerHTML.indexOf('color:#22cc88') >= 0,
   'manifest colour is applied to the glyph on render');
ok(!ap.hasAttribute('data-ov-color'), 'manifest colour is NOT stashed as a per-icon override on render');
sandbox.window.hartCustomizeIcon('appearance');
const dlg4 = sandbox.document.body._kids.find(k => k._attrs.id === 'hart-icon-customize');
dlg4.querySelector('#hic-save').dispatch('click', { target: dlg4.querySelector('#hic-save'), preventDefault() {} });
const apSaved = lastSaved.find(r => r.id === 'appearance');
ok(apSaved && !('color' in apSaved), 'plain Save on a manifest-coloured icon persists NO colour override (lean blob)');

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

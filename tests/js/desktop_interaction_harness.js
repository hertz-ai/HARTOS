/*
 * desktop_interaction_harness.js — execute the REAL hartDesktop.js under a tiny
 * DOM shim and assert the interaction-latency contracts that a browser would
 * enforce but node --check cannot:
 *
 *   A) the desktop right-click menu is built + shown SYNCHRONOUSLY on the
 *      'contextmenu' event (no await / no setTimeout before HartCtxMenu.open),
 *   B) the marquee (rubber-band) select reads every icon's rect ONCE at
 *      pointerdown and NEVER calls getBoundingClientRect again per pointermove
 *      (no forced reflow on the move hot path), while still toggling .selected
 *      on the icons the band covers.
 *
 * Usage:  node desktop_interaction_harness.js /abs/path/to/hartDesktop.js
 * Exit 0 + "PASS" on success; non-zero + "FAIL: ..." otherwise.
 *
 * No jsdom (not installed in CI): a purpose-built shim covers exactly what
 * hartDesktop.js touches. Old-WebKit-safe JS (var/function, string concat).
 */
'use strict';

global.window = global;
global.requestAnimationFrame = function (f) { return setTimeout(f, 0); };
global.cancelAnimationFrame = function (id) { clearTimeout(id); };

var GBCR_COUNT = 0;   // total getBoundingClientRect calls across all icons

function El(tag) {
  this.tagName = String(tag || 'div').toUpperCase();
  this._classes = [];
  this._attrs = {};
  this.style = { cssText: '' };
  this.children = [];
  this.parentNode = null;
  this._listeners = [];
  this._rect = null;          // icons get an explicit rect
  this._innerHTML = '';
  this.textContent = '';
}
Object.defineProperty(El.prototype, 'className', {
  get: function () { return this._classes.join(' '); },
  set: function (v) { this._classes = String(v || '').split(/\s+/).filter(Boolean); }
});
Object.defineProperty(El.prototype, 'nextSibling', {
  get: function () {
    var p = this.parentNode; if (!p) return null;
    var i = p.children.indexOf(this);
    return (i >= 0 && i + 1 < p.children.length) ? p.children[i + 1] : null;
  }
});
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { return this._innerHTML; },
  set: function (html) {
    this._innerHTML = String(html || '');
    this.children = [];
    var re = /<(\w+)([^>]*?)\/?>/g, m;
    while ((m = re.exec(this._innerHTML))) {
      var child = new El(m[1]);
      var cls = /class="([^"]*)"/.exec(m[2]);
      if (cls) child.className = cls[1];
      child.parentNode = this;
      this.children.push(child);
    }
  }
});
El.prototype.classList = null;   // installed per-instance below
function installClassList(el) {
  el.classList = {
    add: function (c) { if (el._classes.indexOf(c) < 0) el._classes.push(c); },
    remove: function (c) { var i = el._classes.indexOf(c); if (i >= 0) el._classes.splice(i, 1); },
    contains: function (c) { return el._classes.indexOf(c) >= 0; },
    toggle: function (c, on) {
      var has = el._classes.indexOf(c) >= 0;
      var want = (arguments.length > 1) ? !!on : !has;
      if (want && !has) el._classes.push(c);
      else if (!want && has) el._classes.splice(el._classes.indexOf(c), 1);
      return want;
    }
  };
}
El.prototype.setAttribute = function (k, v) { this._attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return (k in this._attrs) ? this._attrs[k] : null; };
El.prototype.removeAttribute = function (k) { delete this._attrs[k]; };
El.prototype.appendChild = function (c) { c.parentNode = this; this.children.push(c); return c; };
El.prototype.insertBefore = function (c, ref) {
  c.parentNode = this;
  var i = ref ? this.children.indexOf(ref) : -1;
  if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
  return c;
};
El.prototype.removeChild = function (c) {
  var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1);
  c.parentNode = null; return c;
};
El.prototype.setPointerCapture = function () {};
El.prototype.releasePointerCapture = function () {};
El.prototype.focus = function () {};
El.prototype.getBoundingClientRect = function () {
  GBCR_COUNT++;
  return this._rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
};
El.prototype.addEventListener = function (type, fn, capture) {
  this._listeners.push({ type: type, fn: fn, capture: !!capture });
};
El.prototype.removeEventListener = function (type, fn, capture) {
  for (var i = this._listeners.length - 1; i >= 0; i--) {
    var L = this._listeners[i];
    if (L.type === type && L.fn === fn && L.capture === !!capture) this._listeners.splice(i, 1);
  }
};
function _selMatch(el, sel) {
  sel = sel.replace(/\[[^\]]*\]/g, '').trim();     // drop attr filters ([data-x])
  if (!sel) return true;
  if (sel.charAt(0) === '.') {
    var cls = sel.split('.').filter(Boolean);
    for (var i = 0; i < cls.length; i++) if (el._classes.indexOf(cls[i]) < 0) return false;
    return true;
  }
  return el.tagName === sel.toUpperCase();
}
El.prototype.matches = function (sel) {
  var parts = String(sel).split(',');
  for (var i = 0; i < parts.length; i++) if (_selMatch(this, parts[i])) return true;
  return false;
};
El.prototype.closest = function (sel) {
  var n = this;
  while (n) { if (n.matches && n.matches(sel)) return n; n = n.parentNode; }
  return null;
};
function _collect(el, sel, out) {
  for (var i = 0; i < el.children.length; i++) {
    var c = el.children[i];
    if (_selMatch(c, sel)) out.push(c);
    _collect(c, sel, out);
  }
}
El.prototype.querySelectorAll = function (sel) { var out = []; _collect(this, sel, out); return out; };
El.prototype.querySelector = function (sel) { var out = this.querySelectorAll(sel); return out.length ? out[0] : null; };

function mkEl(tag) { var e = new El(tag); installClassList(e); return e; }

// ── document shim ──
var byId = {};
var doc = mkEl('#document');
doc.readyState = 'complete';
doc.head = mkEl('head');
doc.documentElement = mkEl('html');
doc.body = mkEl('body');
doc.body.className = 'body';
doc.createElement = function (tag) { return mkEl(tag); };
doc.getElementById = function (id) { return byId[id] || null; };
global.document = doc;

// ── fire an event through capture(document) -> target -> bubble(document) ──
function fireEvent(target, type, props) {
  var evt = {
    type: type, target: target, button: 0, pointerType: 'mouse', pointerId: 1,
    clientX: 0, clientY: 0, timeStamp: Date.now(),
    defaultPrevented: false, _stop: false, _stopImm: false,
    preventDefault: function () { this.defaultPrevented = true; },
    stopPropagation: function () { this._stop = true; },
    stopImmediatePropagation: function () { this._stop = true; this._stopImm = true; }
  };
  if (props) for (var k in props) evt[k] = props[k];
  function run(listeners, capture) {
    for (var i = 0; i < listeners.length && !evt._stopImm; i++) {
      if (listeners[i].capture !== capture) continue;
      if (listeners[i].type !== type) continue;
      listeners[i].fn.call(evt.currentTarget || target, evt);
    }
  }
  evt.currentTarget = doc; run(doc._listeners, true);              // capture
  if (!evt._stop) { evt.currentTarget = target; run(target._listeners, false); }  // at target
  if (!evt._stop) { evt.currentTarget = doc; run(doc._listeners, false); }        // bubble to document
  return evt;
}

// ── stub the shell globals hartDesktop.js depends on ──
var ctxOpenCalls = [];
window.HartCtxMenu = {
  open: function (items, x, y) { ctxOpenCalls.push({ items: items, x: x, y: y }); },
  close: function () {}, isOpen: function () { return false; }
};
window.HartBrandArt = {
  glyphHTML: function (g, c) { return '<span class="mi">' + (g || '') + '</span>'; },
  gradient: function () { return 'linear-gradient(#000,#111)'; },
  glyphTint: function () { return '#8b80ff'; },
  spectrum: ['teal', 'violet'], spectrumHex: { teal: '#00e6c3', violet: '#8b80ff' }
};
window.MANIFEST = { feed: { title: 'Feed', icon: 'feed' } };
window.SYSTEM_PANELS = {};
window.openPanel = function () {};
var _readyCb = null;
window.HartSession = {
  ready: function (cb) { _readyCb = cb; },
  get: function () { return []; },
  set: function () {}
};

// the desktop layer the module binds to
var layer = mkEl('div');
layer.setAttribute && layer.setAttribute('id', 'hart-desktop');
byId['hart-desktop'] = layer;

function fail(msg) { console.error('FAIL: ' + msg); process.exit(1); }

// ── load the REAL module (runs its IIFE -> init() synchronously) ──
var modPath = process.argv[2];
if (!modPath) fail('no hartDesktop.js path argument');
try { require(modPath); } catch (e) { fail('loading hartDesktop.js threw: ' + (e && e.stack || e)); }

// ── TEST A: contextmenu on the wallpaper opens the menu SYNCHRONOUSLY ──
var wallpaper = mkEl('div');
wallpaper.className = 'wallpaper';
wallpaper.parentNode = doc.body;
doc.body.children.push(wallpaper);

ctxOpenCalls.length = 0;
fireEvent(wallpaper, 'contextmenu', { clientX: 120, clientY: 90 });
// If open() ran DURING the synchronous dispatch above, it is recorded already.
if (ctxOpenCalls.length !== 1) {
  fail('desktop context menu was not opened synchronously on contextmenu (open calls=' +
    ctxOpenCalls.length + '); a fetch/await/setTimeout must not gate the menu');
}
if (!ctxOpenCalls[0].items || !ctxOpenCalls[0].items.length) {
  fail('context menu opened with no items (menu must be fully built before it shows)');
}

// ── TEST B: marquee reads icon rects ONCE, never per pointermove ──
function addIcon(rect) {
  var el = mkEl('div');
  el.className = 'desktop-icon';
  el._rect = rect;
  layer.appendChild(el);
  return el;
}
// icon0 falls inside the band (10,10)->(200,200); icon1 is far outside.
var icon0 = addIcon({ left: 50, top: 50, right: 90, bottom: 90 });
var icon1 = addIcon({ left: 500, top: 500, right: 540, bottom: 540 });

GBCR_COUNT = 0;
fireEvent(wallpaper, 'pointerdown', { clientX: 10, clientY: 10, button: 0, pointerType: 'mouse' });
var afterDown = GBCR_COUNT;   // should be exactly one read per icon (2)
if (afterDown !== 2) {
  fail('marquee did not cache both icon rects at pointerdown (getBoundingClientRect count=' +
    afterDown + ', expected 2)');
}
// several moves — with the fix these must NOT read layout again
fireEvent(wallpaper, 'pointermove', { clientX: 80, clientY: 80 });
fireEvent(wallpaper, 'pointermove', { clientX: 140, clientY: 140 });
fireEvent(wallpaper, 'pointermove', { clientX: 200, clientY: 200 });
if (GBCR_COUNT !== afterDown) {
  fail('marquee called getBoundingClientRect on pointermove (forced reflow on the ' +
    'hot path): count went ' + afterDown + ' -> ' + GBCR_COUNT);
}
// selection still works off the cached rects
if (!icon0.classList.contains('selected')) fail('icon inside the band was not selected');
if (icon1.classList.contains('selected')) fail('icon outside the band was wrongly selected');

// releasing clears marquee state (no throw)
fireEvent(wallpaper, 'pointerup', { clientX: 200, clientY: 200 });

console.log('PASS');
process.exit(0);

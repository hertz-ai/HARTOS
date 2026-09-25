/*
 * shell_dom_shim.mjs - ONE tiny dependency-free DOM shim for the shell's
 * behavioural .mjs drivers (CI here has no jsdom).
 *
 * Every earlier driver carried its own copy of this shim, each a little
 * different, so a module that worked in one harness could fail in another for
 * shim reasons rather than shell reasons. This is the shared one: it models
 * exactly the DOM surface the /shell/static modules touch (a tree, classList,
 * contains/closest, id and class queries, listeners with capture, focus) plus a
 * capturable scheduler so a test can tick intervals on demand and read the
 * cadence each module asked for.
 *
 * It is a test helper, not a test: pytest never collects it (no test_ prefix).
 */
import vm from 'node:vm';

function clsOf(el) { return (el._attrs.class || '').split(/\s+/).filter(Boolean); }

function matches(el, sel) {
  if (!el || !el._attrs) return false;
  // A comma list: any branch matches.
  if (sel.indexOf(',') >= 0) return sel.split(',').some((s) => matches(el, s.trim()));
  // Attribute filters: [attr] or [attr="value"], any number, after the rest.
  let rest = sel;
  let am;
  while ((am = /\[([\w-]+)(?:="([^"]*)")?\]$/.exec(rest))) {
    const have = el._attrs[am[1]];
    if (have === undefined) return false;
    if (am[2] !== undefined && have !== am[2]) return false;
    rest = rest.slice(0, am.index);
  }
  if (!rest) return true;
  if (rest[0] === '#') return el._attrs.id === rest.slice(1);
  if (rest[0] === '.') return rest.slice(1).split('.').every((c) => clsOf(el).indexOf(c) >= 0);
  return el.tagName === rest.toUpperCase();
}

export function makeEl(tag, state) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, _rect: null,
    style: {}, dataset: {}, value: '', placeholder: '', textContent: '', _innerHTML: '',
    parentNode: null, offsetParent: {}, offsetLeft: 0, offsetTop: 0,
    offsetWidth: (state && state.offsetWidth) || 200, offsetHeight: (state && state.offsetHeight) || 120,
    _bySel: {},
    classList: {
      _s: new Set(),
      add(...cs) { cs.forEach((c) => this._s.add(c)); el._syncClass(); },
      remove(...cs) { cs.forEach((c) => this._s.delete(c)); el._syncClass(); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const on = (force === undefined) ? !this._s.has(c) : !!force;
        if (on) this._s.add(c); else this._s.delete(c);
        el._syncClass();
        return on;
      }
    },
    _syncClass() { el._attrs.class = Array.from(el.classList._s).join(' '); },
    // A string set wins; otherwise mirror textContent so the shell's esc()
    // idiom (set textContent, read innerHTML) yields the text, not ''.
    get innerHTML() { return this._innerHTML || this.textContent || ''; },
    set innerHTML(v) { this._innerHTML = String(v); this._kids = []; this._bySel = {}; },
    get children() { return this._kids; },
    get firstChild() { return this._kids[0] || null; },
    setAttribute(k, v) {
      this._attrs[k] = String(v);
      if (k === 'class') { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
      if (k.indexOf('data-') === 0) this.dataset[k.slice(5)] = String(v);
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = el; this._kids.push(c); return c; },
    insertBefore(node, ref) {
      node.parentNode = el;
      const i = this._kids.indexOf(ref);
      if (i < 0) this._kids.push(node); else this._kids.splice(i, 0, node);
      return node;
    },
    removeChild(c) { this._kids = this._kids.filter((k) => k !== c); c.parentNode = null; return c; },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    contains(node) {
      if (node === el) return true;
      for (const k of el._kids) { if (k === node || (k.contains && k.contains(node))) return true; }
      return false;
    },
    closest(sel) {
      let n = el;
      while (n && n._attrs) { if (matches(n, sel)) return n; n = n.parentNode; }
      return null;
    },
    matches(sel) { return matches(el, sel); },
    querySelector(sel) {
      const out = []; walk(el, sel, out, 1);
      if (out[0]) return out[0];
      // Opt-in (makeRealm({ stubMissing: true })): a module that paints through
      // innerHTML strings and then queries INTO them (hartDesktop's icon tiles)
      // gets a stable stub per selector instead of null, reset on innerHTML set.
      // An attribute selector is an EXISTENCE probe ('.desktop-icon[data-id=x]':
      // is it pinned already?), so it must answer null; only bare class and id
      // lookups (the parts of a painted tile) get a stub.
      if (state && state.stubMissing && sel.indexOf('[') < 0) {
        if (!this._bySel[sel]) { const s = makeEl('div', state); s.parentNode = el; this._bySel[sel] = s; }
        return this._bySel[sel];
      }
      return null;
    },
    querySelectorAll(sel) { const out = []; walk(el, sel, out, 0); return out; },
    addEventListener(t, fn, cap) { (this._listeners[t] = this._listeners[t] || []).push({ fn, cap: !!(cap && (cap === true || cap.capture)) }); },
    removeEventListener(t, fn) { if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter((l) => l.fn !== fn); },
    dispatch(t, ev) {
      ev = ev || mkEv(el);
      (this._listeners[t] || []).slice().forEach((l) => l.fn(ev));
      return ev;
    },
    click() { return this.dispatch('click', mkEv(this)); },
    focus() { if (state) state.active = el; },
    blur() { if (state && state.active === el) state.active = null; },
    select() {}, setPointerCapture() {}, releasePointerCapture() {},
    getBoundingClientRect() {
      return this._rect || { left: 0, top: 0, right: this.offsetWidth, bottom: this.offsetHeight, width: this.offsetWidth, height: this.offsetHeight };
    },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); },
    get className() { return this._attrs.class || ''; },
    set className(v) { this.setAttribute('class', v); }
  };
  return el;
}

function walk(node, sel, out, limit) {
  for (const k of node._kids) {
    // ':not(.x)' is the one pseudo the shell uses (the context menu's rows).
    const m = /^(.*?):not\((.*)\)$/.exec(sel);
    const hit = m ? (matches(k, m[1]) && !matches(k, m[2])) : matches(k, sel);
    if (hit) { out.push(k); if (limit && out.length >= limit) return; }
    walk(k, sel, out, limit);
    if (limit && out.length >= limit) return;
  }
}

export function mkEv(target, extra) {
  const ev = {
    target, currentTarget: target, defaultPrevented: false, _stopped: false,
    clientX: 0, clientY: 0, button: 0, pointerId: 1, key: '', code: '', shiftKey: false, metaKey: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
    stopImmediatePropagation() { this._stopped = true; },
    getModifierState() { return false; }
  };
  return Object.assign(ev, extra || {});
}

/**
 * makeRealm({ w, h, offsetWidth, offsetHeight, stubMissing }) -> a fresh vm
 * context with window === sandbox. offsetWidth/Height set every element's
 * measured size (the context menu's edge-flip tests need a known box);
 * stubMissing makes querySelector return a stable stub instead of null.
 *
 *   R.el(id)            register an element reachable by getElementById (appended to body)
 *   R.document.dispatch('pointerdown', ev)   run document listeners, capture ones first
 *   R.window.dispatch('blur')                run window listeners
 *   R.intervals         [{fn, ms}] every setInterval the modules asked for
 *   R.timers            [{fn, ms}] every setTimeout (deferred; R.flushTimers() runs them)
 *   R.tick()            run every interval callback once
 */
export function makeRealm(opts) {
  opts = opts || {};
  const state = { active: null, offsetWidth: opts.offsetWidth, offsetHeight: opts.offsetHeight,
                  stubMissing: !!opts.stubMissing };
  const registry = {};
  const docEl = makeEl('html', state);
  const head = makeEl('head', state);
  const body = makeEl('body', state);
  docEl.appendChild(head); docEl.appendChild(body);

  function findById(node, id) {
    for (const k of node._kids) {
      if (k._attrs.id === id) return k;
      const f = findById(k, id);
      if (f) return f;
    }
    return null;
  }

  const document = {
    readyState: 'complete',
    documentElement: docEl, head, body,
    _listeners: {},
    get activeElement() { return state.active || body; },
    createElement: (t) => makeEl(t, state),
    createTextNode: (t) => ({ textContent: t, _attrs: {}, _kids: [] }),
    getElementById(id) { return registry[id] || findById(body, id) || findById(head, id) || null; },
    querySelector(sel) { return docEl.querySelector(sel); },
    querySelectorAll(sel) { return docEl.querySelectorAll(sel); },
    addEventListener(t, fn, cap) { (this._listeners[t] = this._listeners[t] || []).push({ fn, cap: !!(cap && (cap === true || cap.capture)) }); },
    removeEventListener(t, fn) { if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter((l) => l.fn !== fn); },
    dispatch(t, ev) {
      ev = ev || mkEv(body);
      const ls = (this._listeners[t] || []).slice();
      ls.filter((l) => l.cap).forEach((l) => { if (!ev._stopped) l.fn(ev); });
      ls.filter((l) => !l.cap).forEach((l) => { if (!ev._stopped) l.fn(ev); });
      return ev;
    },
    contains(node) { return docEl.contains(node); },
    hasFocus() { return true; },
    _attrs: {}, _kids: []
  };

  const intervals = [];
  const timers = [];
  const sandbox = {
    document, console,
    navigator: { onLine: true },
    innerWidth: opts.w || 1280, innerHeight: opts.h || 800,
    setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval(id) { if (intervals[id - 1]) intervals[id - 1].fn = function () {}; },
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1].fn = function () {}; },
    requestAnimationFrame(fn) { fn(); return 1; },
    cancelAnimationFrame() {},
    matchMedia() { return { matches: false }; },
    getComputedStyle() { return { getPropertyValue() { return ''; } }; },
    localStorage: { _m: {}, getItem(k) { return Object.prototype.hasOwnProperty.call(this._m, k) ? this._m[k] : null; }, setItem(k, v) { this._m[k] = String(v); } },
    MutationObserver: class {
      constructor(cb) { this.cb = cb; sandbox._observers.push(this); }
      observe() {} disconnect() {}
    },
    _observers: [],
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    _listeners: {},
    addEventListener(t, fn, cap) { (this._listeners[t] = this._listeners[t] || []).push({ fn, cap: !!(cap && (cap === true || cap.capture)) }); },
    removeEventListener(t, fn) { if (this._listeners[t]) this._listeners[t] = this._listeners[t].filter((l) => l.fn !== fn); },
    dispatch(t, ev) {
      ev = ev || mkEv(document);
      (this._listeners[t] || []).slice().forEach((l) => { if (!ev._stopped) l.fn(ev); });
      return ev;
    },
    HartTimeoutSignal() { return null; },
    JSON, Math, Date, Promise, Array, Object, String, Number, parseInt, parseFloat, isNaN, Error, TextEncoder,
    performance: { now() { return 1234; } }
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  vm.createContext(sandbox);

  return {
    sandbox, window: sandbox, document, docEl, head, body, registry, state, intervals, timers,
    el(id, parent) {
      const e = makeEl('div', state);
      e.setAttribute('id', id);
      registry[id] = e;
      (parent || body).appendChild(e);
      return e;
    },
    run(code, name) { return vm.runInContext(code, sandbox, { filename: name || 'shell.js' }); },
    tick() { intervals.slice().forEach((i) => i.fn()); },
    flushTimers() { const t = timers.splice(0, timers.length); t.forEach((x) => x.fn()); return t.length; },
    // Observers registered through the shim's MutationObserver class.
    notifyObservers(records) { sandbox._observers.slice().forEach((o) => o.cb(records || [], o)); }
  };
}

/* Drain every queued microtask (the fetch().then() chains). A macrotask boundary
 * (setImmediate) fires only after every queued microtask has run. */
export const flush = () => new Promise((r) => setImmediate(r));

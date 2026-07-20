/*
 * Behavioural test for the File Explorer P1 UI wiring.
 *
 * Drives the REAL integrations/agent_engine/static/hartFiles.js through its
 * public API (window.HartFiles.mount) on a dependency-free DOM shim (CI runners
 * here have no jsdom). The shim parses the module's rendered HTML so the real
 * querySelectorAll/closest/dispatch paths run. We mock ONLY the network
 * boundary (fetch) and assert OBSERVABLE behaviour + the exact backend calls:
 *
 *   1. SEARCH TOGGLE  — clicking the subfolders button + typing in the Filter
 *      box calls GET /api/shell/files/search?...&recursive=true and lists the
 *      returned hits (with their relative path) instead of the dir contents.
 *   2. THUMBNAIL      — an image row/tile renders an <img.hf-thumb> whose src is
 *      /api/shell/files/thumbnail; a non-image renders only the material glyph.
 *   3. DRAG-DROP      — pointer-dragging a file row onto a FOLDER row highlights
 *      the drop target and, on drop, calls /move (same root) or /copy (Ctrl);
 *      dropping onto a Places sidebar entry works too.
 *   4. CHMOD CONTROLS — Properties on a file shows the rwx permission grid;
 *      toggling a checkbox enables Apply, which POSTs /api/shell/files/chmod
 *      with the recomputed octal mode.
 *
 * Run:  node tests/unit/test_shell_file_explorer_p1.mjs
 * (test_shell_file_explorer_p1.py shells out to this so pytest/CI picks it up.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartFiles.js');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Tiny HTML-parsing DOM shim ─────────────────────────────────────────────
// Real enough for hartFiles: parse rendered HTML into an element tree so
// querySelectorAll('.hf-row,.hf-tile'), getAttribute, closest, classList and
// event dispatch all behave. Supports the handful of selectors the module uses.
const VOID = new Set(['input', 'img', 'br', 'hr', 'span']);  // span isn't void; handled below

function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, _text: '',
    parentNode: null, pointerType: '',
    style: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); el._syncClass(); },
      remove(c) { this._s.delete(c); el._syncClass(); },
      contains(c) { return this._s.has(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c); f ? this._s.add(c) : this._s.delete(c); el._syncClass(); return f; }
    },
    _syncClass() { this._attrs.class = Array.from(this.classList._s).join(' '); },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === 'class') this._loadClass(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; if (k === 'class') this.classList._s = new Set(); },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    _loadClass(v) { this.classList._s = new Set(String(v || '').split(/\s+/).filter(Boolean)); },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild(c) { this._kids = this._kids.filter(k => k !== c); c.parentNode = null; return c; },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { this._listeners[t] = (this._listeners[t] || []).filter(f => f !== fn); },
    dispatch(t, ev) {
      ev = ev || {};
      ev.target = ev.target || el; ev.preventDefault = ev.preventDefault || function () {};
      ev.stopPropagation = ev.stopPropagation || function () {};
      // direct handlers (onX) + addEventListener handlers
      const on = el['on' + t]; if (typeof on === 'function') on.call(el, ev);
      (this._listeners[t] || []).slice().forEach(fn => fn.call(el, ev));
    },
    focus() {}, blur() {}, select() {}, setSelectionRange() {},
    setPointerCapture() {}, releasePointerCapture() {},
    get textContent() {
      if (this._kids.length) return this._kids.map(k => k.textContent).join('');
      return this._text;
    },
    set textContent(v) { this._text = String(v); this._kids = []; },
    get innerHTML() { return this._html || ''; },
    set innerHTML(v) { this._html = String(v); this._kids = []; parseInto(el, String(v)); },
    get className() { return this._attrs.class || ''; },
    set className(v) { this._attrs.class = v; this._loadClass(v); },
    get id() { return this._attrs.id || ''; },
    set id(v) { this._attrs.id = String(v); },
    get value() { return this._attrs.value != null ? this._attrs.value : ''; },
    set value(v) { this._attrs.value = String(v); },
    get checked() { return !!this._checked; },
    set checked(v) { this._checked = !!v; if (v) this._attrs.checked = ''; else delete this._attrs.checked; },
    get previousElementSibling() {
      if (!this.parentNode) return null;
      const k = this.parentNode._kids, i = k.indexOf(this);
      return i > 0 ? k[i - 1] : null;
    },
    get firstChild() { return this._kids[0] || null; },
    get firstElementChild() { return this._kids.find(k => k.tagName !== '#TEXT') || null; },
    get children() { return this._kids.filter(k => k.tagName !== '#TEXT'); },
    closest(sel) { let n = el; while (n) { if (matchSel(n, sel)) return n; n = n.parentNode; } return null; },
    contains(node) { let n = node; while (n) { if (n === el) return true; n = n.parentNode; } return false; },
    matches(sel) { return matchSel(el, sel); },
    querySelector(sel) { return queryAll(el, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(el, sel); }
  };
  if (el._attrs.checked !== undefined) el._checked = true;
  return el;
}

// Minimal HTML tokenizer: handles <tag attr="v" attr2='v2'>…</tag>, void tags,
// self-closing, and text nodes. Good enough for hartFiles' rendered markup.
function parseInto(parent, html) {
  let i = 0; const stack = [parent];
  function top() { return stack[stack.length - 1]; }
  while (i < html.length) {
    if (html[i] === '<') {
      if (html.startsWith('<!--', i)) { const e = html.indexOf('-->', i); i = e < 0 ? html.length : e + 3; continue; }
      const close = html[i + 1] === '/';
      const gt = html.indexOf('>', i); if (gt < 0) break;
      let inner = html.slice(i + 1, gt).trim();
      i = gt + 1;
      if (close) { if (stack.length > 1) stack.pop(); continue; }
      const selfClose = inner.endsWith('/'); if (selfClose) inner = inner.slice(0, -1).trim();
      const sp = inner.search(/\s/);
      const tag = (sp < 0 ? inner : inner.slice(0, sp)).toLowerCase();
      const attrStr = sp < 0 ? '' : inner.slice(sp + 1);
      const node = makeEl(tag);
      // parse attributes: name="v" | name='v' | name=v | name
      const re = /([:\w-]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|(\S+)))?/g;
      let m;
      while ((m = re.exec(attrStr))) {
        const name = m[1];
        const val = m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : m[5] !== undefined ? m[5] : '';
        node.setAttribute(name, val);
      }
      if (node.hasAttribute('value')) node._attrs.value = node.getAttribute('value');
      if (node.hasAttribute('checked')) node._checked = true;
      top().appendChild(node);
      const isVoid = ['input', 'img', 'br', 'hr', 'meta', 'link'].includes(tag);
      if (!selfClose && !isVoid) stack.push(node);
    } else {
      const lt = html.indexOf('<', i);
      const text = html.slice(i, lt < 0 ? html.length : lt);
      i = lt < 0 ? html.length : lt;
      if (text.trim()) { const t = makeEl('#text'); t._text = decodeEntities(text); top()._kids.push(t); t.parentNode = top(); }
    }
  }
}
function decodeEntities(s) {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

// Selector matching: supports "tag", ".cls", "[attr]", "[attr=\"v\"]",
// "tag.cls", ".a.b", "#id", comma lists, and a single descendant combinator.
function matchSimple(node, sel) {
  if (!node || node.tagName === '#TEXT') return false;
  // split off attribute filters
  const attrFilters = [];
  sel = sel.replace(/\[([^\]]+)\]/g, (_, body) => { attrFilters.push(body); return ''; });
  for (const f of attrFilters) {
    const mm = /^([:\w-]+)(?:([~|^$*]?=)\s*"?([^"]*)"?)?$/.exec(f.trim());
    if (!mm) return false;
    const name = mm[1], op = mm[2], val = mm[3];
    const have = node.getAttribute(name);
    if (op === undefined) { if (have == null) return false; }
    else if (have !== val) return false;
  }
  const parts = sel.match(/([.#]?[\w-]+)/g) || [];
  for (const p of parts) {
    if (p[0] === '.') { if (!node.classList.contains(p.slice(1))) return false; }
    else if (p[0] === '#') { if (node.getAttribute('id') !== p.slice(1)) return false; }
    else { if (node.tagName !== p.toUpperCase()) return false; }
  }
  return true;
}
function matchSel(node, selList) {
  return selList.split(',').some(s => {
    s = s.trim();
    const chain = s.split(/\s+/);
    if (chain.length === 1) return matchSimple(node, chain[0]);
    // only need the rightmost to match `node`, with some ancestor matching the rest
    if (!matchSimple(node, chain[chain.length - 1])) return false;
    let n = node.parentNode, idx = chain.length - 2;
    while (n && idx >= 0) { if (matchSimple(n, chain[idx])) idx--; n = n.parentNode; }
    return idx < 0;
  });
}
function queryAll(root, selList) {
  const out = [];
  (function walk(n) {
    for (const k of n._kids) {
      if (k.tagName !== '#TEXT' && matchSel(k, selList)) out.push(k);
      walk(k);
    }
  })(root);
  return out;
}

// ── document / window ──────────────────────────────────────────────────────
const head = makeEl('head'); const body = makeEl('body');
let dropStack = [];   // elements elementsFromPoint should return (test-driven)
const document = {
  readyState: 'complete', head, body,
  documentElement: makeEl('html'),
  createElement: (t) => makeEl(t),
  getElementById(id) {
    const find = (n) => { for (const k of n._kids) { if (k.getAttribute && k.getAttribute('id') === id) return k; const r = find(k); if (r) return r; } return null; };
    return find(body) || find(head);
  },
  addEventListener() {}, removeEventListener() {},
  elementsFromPoint() { return dropStack.slice(); },
  elementFromPoint() { return dropStack[0] || null; }
};

let fetchLog = [];
let fetchHandler = null;   // (url, opts) => responseObj
function jsonResp(obj, status) { return { ok: (status || 200) < 400, status: status || 200, json: () => Promise.resolve(obj), text: () => Promise.resolve(JSON.stringify(obj)) }; }

const sandbox = {
  document, console,
  SHELL: '',
  setTimeout: (fn) => { fn(); return 0; },     // run deferred + debounced work inline
  clearTimeout() {},
  requestAnimationFrame(fn) { fn && fn(); return 0; }, cancelAnimationFrame() {},
  ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
  AbortController: function () { return { abort() {}, signal: {} }; },
  CSS: { escape: (s) => String(s).replace(/["\\]/g, '\\$&') },
  fetch: (url, opts) => { fetchLog.push({ url, opts }); return Promise.resolve(fetchHandler(url, opts)); },
  showToast() {}, dsConfirm(t, b, yes) { yes(); }, confirm() { return true; }
};
sandbox.window = sandbox;
sandbox.window.innerWidth = 1200; sandbox.window.innerHeight = 800;
vm.createContext(sandbox);
vm.runInContext(readFileSync(SRC, 'utf8'), sandbox, { filename: 'hartFiles.js' });

// ── helpers to drive the module ────────────────────────────────────────────
// mount() replaces panel.innerHTML and keeps ITS OWN .hf-root, so always resolve
// the live root from the panel rather than holding a stale reference.
const panel = makeEl('div');
function root() { return panel.querySelector('.hf-root'); }

// Directory fixtures the mocked backend serves.
const DIR = '/home/u';
const browseEntries = [
  { name: 'docs', path: DIR + '/docs', is_dir: true, size: 0, modified: 1700000000, extension: '' },
  { name: 'photo.png', path: DIR + '/photo.png', is_dir: false, size: 2048, modified: 1700000100, extension: '.png' },
  { name: 'notes.txt', path: DIR + '/notes.txt', is_dir: false, size: 12, modified: 1700000200, extension: '.txt' }
];
function defaultBackend(url) {
  if (url.indexOf('/api/shell/files/browse') >= 0)
    return jsonResp({ path: DIR, parent: '/home', entries: browseEntries, count: browseEntries.length });
  if (url.indexOf('/api/shell/files/info') >= 0)
    return jsonResp({ path: DIR + '/notes.txt', name: 'notes.txt', is_dir: false, size: 12, modified: 1700000200, created: 1699990000, permissions: '644', extension: '.txt' });
  return jsonResp({});
}
fetchHandler = defaultBackend;

function flush() { return Promise.resolve().then(() => Promise.resolve()).then(() => Promise.resolve()).then(() => Promise.resolve()); }
function setSearch(val) { const s = root().querySelector('.hf-search'); s.value = val; s.dispatch('input'); return s; }
function rows() { return root().querySelectorAll('.hf-row'); }
function rowFor(name) { return rows().find(r => (r.getAttribute('data-path') || '').endsWith('/' + name)); }
function btn(act) { return root().querySelectorAll('.hf-tb-btn,.hf-subfx').find(b => b.getAttribute('data-act') === act); }

// ── boot ───────────────────────────────────────────────────────────────────
const HF = sandbox.window.HartFiles;
ok(HF && typeof HF.mount === 'function', 'HartFiles.mount is exposed');
HF.mount(panel);

await flush();
ok(rows().length === 3, 'initial browse rendered 3 rows (dir + 2 files)');

// ════════════════════════════════════════════════════════════════════════
// 3. THUMBNAILS — image row carries <img.hf-thumb>, non-image does not
// ════════════════════════════════════════════════════════════════════════
{
  const imgRow = rowFor('photo.png');
  const thumb = imgRow.querySelector('img.hf-thumb');
  ok(thumb, 'image row renders an <img.hf-thumb>');
  ok(thumb && /\/api\/shell\/files\/thumbnail\?path=/.test(thumb.getAttribute('src')),
     'thumbnail src points at the /api/shell/files/thumbnail route');
  ok(thumb && decodeURIComponent(thumb.getAttribute('src')).indexOf('photo.png') >= 0,
     'thumbnail src carries the image path');
  const txtRow = rowFor('notes.txt');
  ok(!txtRow.querySelector('img.hf-thumb'), 'non-image row has NO thumbnail img (glyph only)');
  ok(txtRow.querySelector('.mi'), 'non-image row keeps the material glyph');
}

// ════════════════════════════════════════════════════════════════════════
// 1. SEARCH TOGGLE — recursive route + rel-pathed results
// ════════════════════════════════════════════════════════════════════════
{
  // before toggling: typing filters the current dir locally (no /search call)
  fetchLog = [];
  setSearch('note');
  await flush();
  ok(!fetchLog.some(f => f.url.indexOf('/api/shell/files/search') >= 0),
     'plain Filter does NOT hit the search route (instant local filter)');
  ok(rows().length === 1 && rowFor('notes.txt'), 'local filter narrows to the matching row');
  setSearch('');                       // reset filter
  await flush();

  // turn the subfolders toggle on
  const sb = btn('subfx');
  ok(sb, 'search-subfolders toggle button is present');
  sb.dispatch('click');
  await flush();
  ok(root().querySelector('.hf-subfx.on'), 'toggle shows the active (on) state');

  // serve a recursive search result with a nested hit (rel path)
  fetchHandler = (url) => {
    if (url.indexOf('/api/shell/files/search') >= 0) {
      return jsonResp({
        path: DIR, query: 'deep', recursive: true, truncated: false,
        entries: [{ name: 'deep.txt', path: DIR + '/sub/inner/deep.txt', rel: 'sub/inner/deep.txt',
                    is_dir: false, size: 5, modified: 1700000300, extension: '.txt' }],
        count: 1
      });
    }
    return defaultBackend(url);
  };
  fetchLog = [];
  setSearch('deep');
  await flush(); await flush();

  const call = fetchLog.find(f => f.url.indexOf('/api/shell/files/search') >= 0);
  ok(call, 'typing with the toggle on calls the /search route');
  ok(call && /recursive=true/.test(call.url), 'search call requests recursive=true');
  ok(call && /[?&]q=deep/.test(call.url), 'search call passes the query q=deep');

  const hit = rowFor('deep.txt');
  ok(hit, 'subfolder hit is listed as a row');
  const rel = hit && hit.querySelector('.hf-rel');
  ok(rel && rel.textContent.indexOf('sub/inner') >= 0, 'result row shows its relative subfolder path');
}

// reset to a clean browse view for the drag + chmod tests
fetchHandler = defaultBackend;
HF.mount(panel);
await flush();

// ════════════════════════════════════════════════════════════════════════
// 2. DRAG-DROP — file row onto a folder row -> MOVE; Ctrl -> COPY; onto a Place
// ════════════════════════════════════════════════════════════════════════
function drag(srcRow, dropTargets, opts) {
  opts = opts || {};
  srcRow.dispatch('pointerdown', { button: 0, clientX: 10, clientY: 10, pointerId: 1 });
  // move past threshold; elementsFromPoint will report the drop target
  dropStack = dropTargets;
  srcRow.dispatch('pointermove', { clientX: 80, clientY: 80, pointerId: 1 });
  srcRow.dispatch('pointermove', { clientX: 120, clientY: 120, pointerId: 1 });
  srcRow.dispatch('pointerup', { clientX: 120, clientY: 120, pointerId: 1,
                                 ctrlKey: !!opts.ctrl, metaKey: !!opts.meta });
}

{
  const file = rowFor('notes.txt');
  const folder = rowFor('docs');
  fetchLog = [];
  drag(file, [folder]);                 // plain drag -> MOVE (same root)
  // during the move the source should have been marked draggable & target highlit;
  // assert the resulting backend call:
  const mv = fetchLog.find(f => f.url.indexOf('/api/shell/files/move') >= 0);
  ok(mv, 'dragging a file onto a folder calls /move');
  const body = mv && JSON.parse(mv.opts.body);
  eq(body && body.source, DIR + '/notes.txt', 'move source is the dragged file');
  eq(body && body.destination, DIR + '/docs/notes.txt', 'move destination is inside the folder');
  ok(!fetchLog.some(f => f.url.indexOf('/api/shell/files/copy') >= 0), 'plain drag does not COPY');
}

// re-render (refresh after drop triggers a browse) then COPY with Ctrl
await flush();
HF.mount(panel); await flush();
{
  const file = rowFor('photo.png');
  const folder = rowFor('docs');
  fetchLog = [];
  drag(file, [folder], { ctrl: true });    // Ctrl -> COPY
  const cp = fetchLog.find(f => f.url.indexOf('/api/shell/files/copy') >= 0);
  ok(cp, 'Ctrl-dragging a file onto a folder calls /copy');
  ok(!fetchLog.some(f => f.url.indexOf('/api/shell/files/move') >= 0), 'Ctrl-drag does not MOVE');
}

await flush();
HF.mount(panel); await flush();
{
  // drop onto a Places sidebar entry (Documents)
  const file = rowFor('notes.txt');
  const place = root().querySelectorAll('.hf-side-item').find(s => /Documents/.test(s.textContent));
  ok(place, 'Places sidebar has a Documents entry');
  fetchLog = [];
  drag(file, [place]);
  const mv = fetchLog.find(f => f.url.indexOf('/api/shell/files/move') >= 0);
  ok(mv, 'dropping a file onto a Places entry dispatches a file op (move)');
  const body = mv && JSON.parse(mv.opts.body);
  ok(body && /\/Documents\/notes\.txt$/.test(body.destination), 'destination resolves under the Place');
}

// ════════════════════════════════════════════════════════════════════════
// 4. CHMOD CONTROLS — Properties shows the rwx grid; Apply POSTs /chmod
// ════════════════════════════════════════════════════════════════════════
await flush();
HF.mount(panel); await flush();
{
  // open Properties on notes.txt via the public navigate? properties() is internal,
  // so drive it through the context menu path: select the row, open item menu,
  // click "Properties". Simpler: select + call the menu item by simulating the
  // right-click then clicking the Properties entry in the rendered hf-ctx.
  const file = rowFor('notes.txt');
  file.dispatch('contextmenu', { clientX: 30, clientY: 30, preventDefault() {} });
  const menu = document.getElementById('hf-ctx');
  ok(menu, 'context menu opened');
  const propsItem = menu.querySelectorAll('.hf-ctx-item').find(e => /Properties/.test(e.textContent));
  ok(propsItem, 'context menu has a Properties entry');
  propsItem.dispatch('click', { stopPropagation() {} });
  await flush();

  const grid = root().querySelector('.hf-perm-grid');
  ok(grid, 'Properties dialog renders the permissions (rwx) grid');
  const cbs = root().querySelectorAll('.hf-perm-cb');
  ok(cbs.length === 9, 'grid has 9 permission checkboxes (owner/group/other × rwx)');
  const octEl = root().querySelector('[data-perm-oct]');
  eq(octEl && octEl.textContent, '644', 'octal readout reflects the current mode (644)');
  const save = root().querySelector('[data-perm-save]');
  ok(save && save.hasAttribute('disabled'), 'Apply is disabled until a bit changes');

  // turn ON owner-execute (owner row, x bit) -> 644 becomes 744
  const ownerX = cbs.find(cb => cb.getAttribute('data-grp') === '0' && cb.getAttribute('data-bit') === 'x');
  ownerX.checked = true; ownerX.dispatch('change');
  eq(octEl.textContent, '744', 'toggling owner-x recomputes the octal to 744');
  ok(!save.hasAttribute('disabled'), 'Apply enables once the mode differs');

  // Apply -> POST /chmod with the new mode
  fetchHandler = (url, opts) => {
    if (url.indexOf('/api/shell/files/chmod') >= 0) {
      const b = JSON.parse(opts.body);
      return jsonResp({ path: b.path, mode: b.mode, requested: b.mode });
    }
    return defaultBackend(url);
  };
  fetchLog = [];
  save.dispatch('click', { stopPropagation() {} });
  await flush();
  const ch = fetchLog.find(f => f.url.indexOf('/api/shell/files/chmod') >= 0);
  ok(ch, 'clicking Apply POSTs /api/shell/files/chmod');
  const cbody = ch && JSON.parse(ch.opts.body);
  eq(cbody && cbody.mode, '744', 'chmod body carries the recomputed octal mode');
  eq(cbody && cbody.path, DIR + '/notes.txt', 'chmod body carries the file path');
}

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

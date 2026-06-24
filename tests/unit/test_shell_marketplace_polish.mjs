/*
 * Behavioural test for the App Store (marketplace) polish — LIVE-OS #22.
 *
 * After #20 made the store actually render, it "looked clumpy / not top-notch":
 * flat horizontal tiles, cramped spacing. The fix (premium liquid-glass cards +
 * a header + a sticky search + category sections) is structural, so this drives
 * the REAL integrations/agent_engine/static/hartMarketplace.js through a tiny
 * dependency-free DOM shim and asserts the OBSERVABLE structure it builds:
 *
 *   - a .hart-mkt wrapper with a header (title "App Store" + subtitle)
 *   - a .hart-mkt-search row (sticky search input + button)
 *   - one .hart-mkt-section per populated category, each with a section label
 *   - premium vertical cards: .hac-top (icon + body) ABOVE a full-width Install,
 *     with a .hac-cat category chip — not the old inline icon+text+button row
 *   - app names/descriptions inserted as TEXT (no innerHTML injection)
 *
 * Run:  node tests/unit/test_shell_marketplace_polish.mjs
 * (Python wrapper test_shell_marketplace_polish.py shells out for pytest/CI.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartMarketplace.js');
const CODE = readFileSync(SRC, 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

// ── Minimal DOM shim ────────────────────────────────────────────────────────
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), _kids: [], style: {}, dataset: {},
    _attrs: {}, _text: '',
    classList: { _s: new Set(), add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); }, contains(c) { return this._s.has(c); } },
    set className(v) { this._cls = v; (v || '').split(/\s+/).forEach(c => c && this.classList.add(c)); },
    get className() { return this._cls || ''; },
    set textContent(v) { this._text = v; }, get textContent() { return this._text; },
    set innerHTML(v) { this._innerHTML = v; }, get innerHTML() { return this._innerHTML || ''; },
    set title(v) { this._title = v; }, get title() { return this._title; },
    setAttribute(k, v) { this._attrs[k] = v; if (k === 'class') this.className = v; },
    getAttribute(k) { return this._attrs[k]; },
    appendChild(c) { this._kids.push(c); c.parentNode = this; return c; },
    addEventListener() {},
    get children() { return this._kids; },
    // helpers for the test
    find(cls) { for (const k of this._kids) { if (k.classList && k.classList.contains(cls)) return k; const d = k.find && k.find(cls); if (d) return d; } return null; },
    findAll(cls, acc) { acc = acc || []; for (const k of this._kids) { if (k.classList && k.classList.contains(cls)) acc.push(k); if (k.findAll) k.findAll(cls, acc); } return acc; },
    text() { let t = this._text || ''; for (const k of this._kids) t += (k.text ? k.text() : ''); return t; },
  };
  return el;
}

const doc = { createElement: makeEl, getElementById() { return null; } };
const sandbox = {
  window: {}, document: doc, console,
  setTimeout: () => 0, clearTimeout: () => {},
  fetch: () => Promise.reject(new Error('offline in test')),
};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(CODE, sandbox);

const render = sandbox.window.hartRenderMarketplace;
ok(typeof render === 'function', 'hartMarketplace.js exposes window.hartRenderMarketplace');

const host = makeEl('div');
render(host);

// ── Wrapper + header ────────────────────────────────────────────────────────
const wrap = host.find('hart-mkt');
ok(!!wrap, '[#22] renders a .hart-mkt wrapper (premium container, not a bare grid)');
const head = host.find('hart-mkt-head');
ok(!!head, '[#22] has a header block');
ok(head && /App Store/.test(head.text()), '[#22] header titles it "App Store"');
ok(head && /Flathub/.test(head.text()), '[#22] header carries the descriptive subtitle');

// ── Sticky search row ───────────────────────────────────────────────────────
const search = host.find('hart-mkt-search');
ok(!!search, '[#22] has a dedicated (sticky) search row');
ok(search && search.find('ds-input'), '[#22] search row contains the input');

// ── Category sections ───────────────────────────────────────────────────────
const sections = host.findAll('hart-mkt-section');
ok(sections.length >= 5, '[#22] groups apps into category sections (got ' + sections.length + ')');
ok(sections.every(s => s.find('ds-section-label')), '[#22] every section has a category label');
ok(sections.every(s => s.find('hart-app-grid')), '[#22] every section has an app grid');

// ── Premium vertical cards ──────────────────────────────────────────────────
const cards = host.findAll('hart-app-card');
ok(cards.length >= 15, '[#22] renders the curated catalogue (15+ cards, got ' + cards.length + ')');
const c0 = cards[0];
ok(c0 && c0.find('hac-top'), '[#22] card stacks an icon+body row (.hac-top) ABOVE the button');
ok(c0 && c0.find('hac-ic'), '[#22] card has an icon tile');
ok(c0 && c0.find('hac-cat'), '[#22] card shows a category chip (.hac-cat)');
ok(c0 && c0.find('hac-name') && c0.find('hac-name').textContent, '[#22] card name set as TEXT (no innerHTML injection)');
// The Install button is a direct child of the card, AFTER .hac-top (full-width action).
const last = c0 && c0._kids[c0._kids.length - 1];
ok(last && last.tagName === 'BUTTON' && /Install/.test(last.textContent),
  '[#22] full-width Install action sits below the content');

// ── Installed / pre-bundled section: reuses /api/apps/installed (issue 1c) ────
// Re-render with a fetch that RESOLVES the installed-apps endpoint with rows, and
// assert the store surfaces them in a dedicated .hart-mkt-installed section with
// non-interactive "Installed" cards (a disabled .is-installed button).
async function installedPath() {
  let installedCalled = false;
  const fetchMock = (url) => {
    if (String(url).indexOf('/api/apps/installed') >= 0) {
      installedCalled = true;
      return Promise.resolve({ json: () => Promise.resolve({
        apps: [
          { name: 'Firefox', app_id: 'org.mozilla.firefox', platform: 'flatpak', version: '120' },
          { name: 'VLC', app_id: 'org.videolan.VLC', platform: 'flatpak', version: '3.0' },
        ], count: 2 }) });
    }
    return Promise.reject(new Error('offline in test'));
  };
  const sb2 = { window: {}, document: doc, console,
    setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout: () => {}, fetch: fetchMock };
  sb2.window.document = doc;
  vm.createContext(sb2);
  vm.runInContext(CODE, sb2);
  const host2 = makeEl('div');
  sb2.window.hartRenderMarketplace(host2);
  // let the installed-fetch promise chain settle (fetch -> .then -> json() ->
  // .then). A real macrotask tick drains all queued microtasks reliably.
  await new Promise(function (r) { setTimeout(r, 0); });
  await new Promise(function (r) { setTimeout(r, 0); });

  ok(installedCalled, '[#1c] marketplace fetches /api/apps/installed');
  const inst = host2.find('hart-mkt-installed');
  ok(!!inst, '[#1c] renders a dedicated .hart-mkt-installed section');
  ok(inst && /Installed \(2\)/.test(inst.text()), '[#1c] installed section is labelled with the count');
  const instCards = inst ? inst.findAll('hart-app-card') : [];
  ok(instCards.length === 2, '[#1c] renders one card per installed app (got ' + instCards.length + ')');
  const ibtn = instCards[0] && instCards[0]._kids[instCards[0]._kids.length - 1];
  ok(ibtn && ibtn.classList.contains('is-installed') && ibtn.disabled === true,
    '[#1c] installed card shows a non-interactive "Installed" button (no dead Install click)');
  ok(ibtn && /Installed/.test(ibtn.textContent), '[#1c] installed button reads "Installed"');
}

installedPath().then(function () {
  console.log(failures ? `\nRESULT: ${failures} FAILURE(S)` : '\nRESULT: ALL PASS');
  process.exit(failures ? 1 : 0);
});

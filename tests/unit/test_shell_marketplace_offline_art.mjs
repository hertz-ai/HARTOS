/*
 * Behavioural test for the marketplace OFFLINE app logos (#143 offline-art).
 *
 * Drives the REAL integrations/agent_engine/static/hartMarketplace.js through a
 * dependency-free DOM shim and asserts the OBSERVABLE behaviour:
 *
 *   - a card for a known Flathub app renders an <img> in its .hac-ic tile whose
 *     src is the BUNDLED, same-origin logo (/shell/static/app_art/apps/<id>.svg),
 *     with NO network fetch (offline-first);
 *   - the <img> has an onerror handler that, on a missing tile, swaps in the
 *     Material glyph (the documented fallback) - never a broken-image icon;
 *   - a card whose id is NOT a reverse-DNS Flathub id renders the Material glyph
 *     directly (no odd <img> path).
 *
 * Run:  node tests/unit/test_shell_marketplace_offline_art.mjs
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
    find(cls) { for (const k of this._kids) { if (k.classList && k.classList.contains(cls)) return k; const d = k.find && k.find(cls); if (d) return d; } return null; },
    findAll(cls, acc) { acc = acc || []; for (const k of this._kids) { if (k.classList && k.classList.contains(cls)) acc.push(k); if (k.findAll) k.findAll(cls, acc); } return acc; },
    kidTag(t) { for (const k of this._kids) { if (k.tagName === t) return k; } return null; },
  };
  return el;
}

const doc = { createElement: makeEl, getElementById() { return null; } };
let fetchCalls = 0;
const sandbox = {
  window: {}, document: doc, console,
  setTimeout: () => 0, clearTimeout: () => {},
  fetch: () => { fetchCalls++; return Promise.reject(new Error('offline in test')); },
};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(CODE, sandbox);

const render = sandbox.window.hartRenderMarketplace;
ok(typeof render === 'function', 'hartMarketplace.js exposes window.hartRenderMarketplace');

const host = makeEl('div');
render(host);

// The featured catalogue leads with Firefox (org.mozilla.firefox) - a valid id.
const cards = host.findAll('hart-app-card');
ok(cards.length >= 15, 'renders the curated catalogue');
const ic = cards[0] && cards[0].find('hac-ic');
ok(!!ic, 'first card has an icon tile (.hac-ic)');
const img = ic && ic.kidTag('IMG');
ok(!!img, '[#143] a known app renders a bundled logo <img> in the icon tile');
ok(img && /^\/shell\/static\/app_art\/apps\/[a-z0-9.]+\.svg$/i.test(img.src),
  '[#143] the logo src is the same-origin bundled path (' + (img && img.src) + ')');
// The only fetch is the pre-existing best-effort /api/apps/installed probe; the
// LOGOS add zero network (they are same-origin static <img src>), so the whole
// featured grid paints its art offline.
ok(fetchCalls <= 1, '[#143] the bundled logos add NO network fetch (offline-first; got ' + fetchCalls + ')');

// onerror swaps in the Material glyph (never a broken-image icon).
ok(img && typeof img.onerror === 'function', '[#143] the <img> carries an onerror fallback');
if (img && typeof img.onerror === 'function') {
  img.onerror();
  ok(/material-icons-round/.test(ic.innerHTML),
    '[#143] on a missing tile the icon falls back to the Material glyph');
}

// The id guard: the installed section builds cards via the SAME appCard() with
// id = app_id || name. A valid reverse-DNS id gets a logo <img>; a name-only
// (undotted) id skips straight to the Material glyph (no odd <img> path).
async function idGuardPath() {
  const fetchMock = (url) => {
    if (String(url).indexOf('/api/apps/installed') >= 0) {
      return Promise.resolve({ json: () => Promise.resolve({ apps: [
        { name: 'VLC', app_id: 'org.videolan.VLC', platform: 'flatpak' },      // valid id
        { name: 'Local Tool', app_id: 'localtool', platform: 'nix' },          // undotted id
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
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  const inst = host2.find('hart-mkt-installed');
  const instCards = inst ? inst.findAll('hart-app-card') : [];
  ok(instCards.length === 2, '[#143] installed section rendered both apps');
  const icValid = instCards[0] && instCards[0].find('hac-ic');
  const icJunk = instCards[1] && instCards[1].find('hac-ic');
  ok(icValid && icValid.kidTag('IMG'), '[#143] valid Flathub id -> logo <img>');
  ok(icJunk && !icJunk.kidTag('IMG') && /material-icons-round/.test(icJunk.innerHTML),
    '[#143] undotted id -> Material glyph directly (no <img>)');
}
idGuardPath().then(function () {
  console.log(failures ? `\nRESULT: ${failures} FAILURE(S)` : '\nRESULT: ALL PASS');
  process.exit(failures ? 1 : 0);
});

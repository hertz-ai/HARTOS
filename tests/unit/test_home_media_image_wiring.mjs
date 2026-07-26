/*
 * Behavioural test for the W10 HOME card-image wiring in hartHome.js.
 *
 * W10 connects the (already-coded) local semantic media index to the Netflix
 * home: every card with no producer photo asks /api/media/search keyed by its
 * topic/title, and on a hit loads the local file as a cached thumbnail through
 * the EXISTING shell file route - lazy, same-origin, gradient-fallback on a miss.
 * Producer-supplied photos go straight in (card.image) or through the same-origin
 * fetch-once ImageCache (card.image_url -> /api/media/image).
 *
 * This drives the REAL hartHome.js module on a tiny dependency-free DOM shim
 * (CI here has no jsdom) through its public surface (window.HartHome.compose) and
 * asserts OBSERVABLE side-effects: which URLs were fetched and which <img>
 * elements (and their resolved src) landed in the card art - never grep source.
 *
 * Run:  node tests/unit/test_home_media_image_wiring.mjs
 * (test_home_media_image_wiring.py shells out so pytest/CI picks it up too.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = join(HERE, '..', '..', 'integrations', 'agent_engine', 'static');
const read = (f) => readFileSync(join(STATIC, f), 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }
function eq(a, b, msg) { ok(a === b, msg + '  (got ' + JSON.stringify(a) + ', want ' + JSON.stringify(b) + ')'); }

// ── Minimal DOM shim (only what hartHome.js touches) ────────────────────────
const byId = {};
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    _attrs: {}, _kids: [], _listeners: {}, style: {}, _text: '', _html: '',
    parentNode: null, _hhImaged: undefined,
    classList: { _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, f) { const on = (f === undefined) ? !this._s.has(c) : !!f; if (on) this._s.add(c); else this._s.delete(c); return on; } },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === 'id') byId[this._attrs.id] = this; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    appendChild(c) { c.parentNode = el; this._kids.push(c); return c; },
    removeChild(c) { this._kids = this._kids.filter((k) => k !== c); c.parentNode = null; return c; },
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener() {},
    get children() { return this._kids; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; this._kids = []; },   // mirror real DOM: clears children
    get textContent() { return this._text; }, set textContent(v) { this._text = v; },
    get src() { return this._attrs.src || ''; }, set src(v) { this._attrs.src = String(v); },
    set alt(v) { this._attrs.alt = String(v); },
    get className() { return this._attrs.class || ''; }, set className(v) { this._attrs.class = String(v); },
    get id() { return this._attrs.id || ''; }, set id(v) { this._attrs.id = String(v); byId[this._attrs.id] = el; },
    querySelector() { return null; },
    focus() {}
  };
  return el;
}

function makeRealm() {
  const body = makeEl('body');
  const documentElement = makeEl('html');
  const document = {
    body, documentElement, readyState: 'complete',
    createElement: (t) => makeEl(t),
    getElementById: (id) => byId[id] || null,
    querySelector: () => null,                 // '.wallpaper' absent -> root mounts on body
    addEventListener() {}
  };

  const fetchLog = [];
  const json = (data) => Promise.resolve(data);
  function resp(okFlag, data) { return Promise.resolve({ ok: okFlag, status: okFlag ? 200 : 500, json: () => json(data) }); }
  function fakeFetch(url) {
    fetchLog.push(String(url));
    const u = String(url);
    if (u.indexOf('/api/media/search') !== -1) {
      const m = /[?&]q=([^&]*)/.exec(u);
      const q = m ? decodeURIComponent(m[1]) : '';
      if (q === 'Beach Trip') {
        return resp(true, { query: q, count: 1, results: [
          { path: '/home/u/Pictures/beach.jpg', name: 'beach.jpg', kind: 'image', caption: 'a beach at sunset', match: 'semantic', score: 0.91 } ] });
      }
      if (q === 'Old Clip') {                   // a VIDEO hit -> must be skipped (thumbnail route is images only)
        return resp(true, { query: q, count: 1, results: [
          { path: '/home/u/Videos/clip.mp4', name: 'clip.mp4', kind: 'video', match: 'prefix', score: 1.0 } ] });
      }
      return resp(true, { query: q, count: 0, results: [] });   // miss -> gradient fallback
    }
    return resp(false, {});                     // wallet/dashboard/recipes/estimate -> getJSON throws, caught
  }

  const brandArt = {
    spectrum: ['teal', 'violet', 'magenta', 'amber', 'cyan', 'blue'],
    spectrumHex: { teal: '#00e6c3', cyan: '#22d3ee', blue: '#3b82f6', violet: '#8b5cf6', magenta: '#ec4899', amber: '#f59e0b' },
    gradient: (hex, seed) => 'linear-gradient(' + hex + ',' + seed + ')',
    glyphHTML: (icon) => '<i class="mi">' + icon + '</i>'
  };

  const sandbox = {
    document, console,
    setTimeout: () => 0, clearTimeout() {},      // sig() abort timer is a no-op here
    requestAnimationFrame: (fn) => { fn(); return 1; }, cancelAnimationFrame() {},
    fetch: fakeFetch,
    HartBrandArt: brandArt,
    SHELL: '', BACKEND: '',
    matchMedia: () => ({ matches: false }),
    innerWidth: 1280, innerHeight: 1080,
    addEventListener() {}
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  return { sandbox, document, fetchLog };
}

// Walk the live element tree collecting every descendant.
function walk(node, out) { (node._kids || []).forEach((k) => { out.push(k); walk(k, out); }); return out; }
function hasClass(el, c) { return (el._attrs.class || '').split(' ').indexOf(c) !== -1; }
function descend(node, pred) { const all = walk(node, []); for (const e of all) if (pred(e)) return e; return null; }

const flush = () => new Promise((r) => setTimeout(r, 0));

// ════════════════════════════════════════════════════════════════════════════
(async function main() {
  const R = makeRealm();
  vm.runInContext(read('hartHome.js'), R.sandbox, { filename: 'hartHome.js' });

  // Let the auto-start refresh() (sample paint + the live-endpoint fetches it
  // fires) fully settle BEFORE we drive a controlled composition, so its trailing
  // re-render cannot clobber our cards.
  await flush(); await flush(); await flush();

  R.fetchLog.length = 0;   // only assert on the searches OUR cards trigger

  R.sandbox.window.HartHome.compose({
    rows: [{
      title: 'Photos', accent: 'teal',
      cards: [
        { title: 'Beach Trip', icon: 'beach_access' },       // local image hit  -> thumbnail
        { title: 'No Match Here', icon: 'help' },             // miss             -> gradient + glyph
        { title: 'Old Clip', icon: 'movie' },                 // video hit        -> skipped, gradient
        { title: 'Direct', icon: 'image', image: '/shell/x.png' },                 // producer photo, as-is
        { title: 'Web', icon: 'public', image_url: 'https://cdn.example.com/p.jpg' } // remote -> cache route
      ]
    }]
  });

  await flush(); await flush(); await flush();

  const root = R.document.getElementById('hart-home');
  ok(!!root, 'home root mounted (#hart-home)');

  // Index the rendered cards by their title text.
  const cardEls = walk(root, []).filter((e) => hasClass(e, 'hh-card') && !hasClass(e, 'hh-card-empty'));
  const byTitle = {};
  cardEls.forEach((card) => {
    const t = descend(card, (e) => hasClass(e, 'hh-card-title'));
    const img = descend(card, (e) => e.tagName === 'IMG');
    const glyph = descend(card, (e) => hasClass(e, 'hh-card-ic'));
    if (t) byTitle[t._text] = { card, img, glyph };
  });

  eq(Object.keys(byTitle).length, 5, 'all five composed cards rendered');

  // 1. local-search hit -> the search fired keyed by the card TITLE, limit=3.
  const searched = R.fetchLog.filter((u) => u.indexOf('/api/media/search') !== -1);
  ok(searched.some((u) => u.indexOf('q=Beach%20Trip') !== -1 && u.indexOf('limit=3') !== -1),
    'imageless card searches /api/media/search?q=<title>&limit=3');

  // 2. a local IMAGE hit -> a lazy <img> loaded from the EXISTING thumbnail route.
  const beach = byTitle['Beach Trip'] || {};
  ok(!!beach.img, 'Beach Trip card got a hydrated <img>');
  eq(beach.img && beach.img._attrs.src,
    '/api/shell/files/thumbnail?path=' + encodeURIComponent('/home/u/Pictures/beach.jpg') + '&size=512',
    'hydrated <img> src is the local thumbnail route (lazy-resolved)');
  ok(beach.img && beach.img.getAttribute('loading') === 'lazy', 'hydrated <img> is lazy');
  ok(!beach.glyph, 'the photo replaced the placeholder glyph');

  // 3. a MISS -> no <img>, the brand gradient + glyph remain (never an empty box).
  const miss = byTitle['No Match Here'] || {};
  ok(!miss.img, 'a search miss leaves NO <img> (gradient fallback)');
  ok(!!miss.glyph, 'a search miss keeps the brand glyph');
  const missArt = miss.card && descend(miss.card, (e) => hasClass(e, 'hh-card-art'));
  ok(!!missArt && /linear-gradient/.test((missArt.style.background || '')),
    'the brand gradient art still paints on a miss');

  // 4. a VIDEO hit is skipped (the thumbnail route serves images only).
  const clip = byTitle['Old Clip'] || {};
  ok(!clip.img, 'a video-only hit is skipped -> no <img>');
  ok(!!clip.glyph, 'a video-only hit keeps the brand glyph');

  // 5. a producer photo (card.image) is used AS-IS, no media search for it.
  const direct = byTitle['Direct'] || {};
  ok(!!direct.img, 'card.image renders an <img>');
  eq(direct.img && direct.img._attrs.src, '/shell/x.png', 'card.image is used verbatim');
  ok(!R.fetchLog.some((u) => u.indexOf('q=Direct') !== -1), 'no media search fired for an already-imaged card');

  // 6. a remote web/news photo (card.image_url) is routed through the same-origin
  //    fetch-once ImageCache so it is cached + CSP-safe in the old cage WebKit.
  const web = byTitle['Web'] || {};
  ok(!!web.img, 'card.image_url renders an <img>');
  eq(web.img && web.img._attrs.src,
    '/api/media/image?url=' + encodeURIComponent('https://cdn.example.com/p.jpg'),
    'card.image_url is routed through /api/media/image (same-origin cache)');
  ok(!R.fetchLog.some((u) => u.indexOf('q=Web') !== -1), 'no media search fired for a card with image_url');

  console.log('RESULT:', failures ? (failures + ' FAILED') : 'ALL PASS');
  process.exit(failures ? 1 : 0);
})();

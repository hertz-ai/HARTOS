/*
 * The App Store front-end adopts the CANONICAL catalog (2026-07-24, audit #0.3).
 *
 * hartMarketplace.js rendered from a HARDCODED JS CATALOG, so apps BAKED INTO THE
 * IMAGE (Firefox, VLC, GIMP...) showed a plain "Install"; clicking it ran a
 * Flathub install that failed offline and flipped the tile to "Retry". The fix
 * fetches /api/apps/catalog (app_catalog.py -> hart-app-catalog.json, the same list
 * that feeds the NixOS preinstall set), which carries a LOCAL `installed` flag.
 * appCard already renders a non-interactive "Installed" state from app.installed.
 *
 * This drives the REAL loadCatalog() out of the REAL shipped hartMarketplace.js
 * (sliced + evaluated in a vm) against a stubbed fetch, and asserts the OBSERVABLE
 * mapping: backend shape -> compact card shape, carrying installed. Behavioural.
 *
 * Run:  node tests/unit/test_app_store_shows_installed.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const SRC = readFileSync(
  join(REPO, 'integrations', 'agent_engine', 'static', 'hartMarketplace.js'), 'utf8');

const fails = [];
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); }
  else { console.log('  FAIL  ' + name + (detail ? ' -- ' + detail : '')); fails.push(name); }
}

// ── Slice the REAL loadCatalog() out of the shipped file ────────────────────
const m = SRC.match(/function loadCatalog\(done\)\s*\{[\s\S]*?\n  \}/);
if (!m) {
  console.log('FAIL: could not slice loadCatalog() from hartMarketplace.js -- the ' +
              'extractor drifted; this guard would be vacuous.');
  process.exit(1);
}

// Backend payload shape as /api/apps/catalog really serves it.
const PAYLOAD = {
  apps: [
    { id: 'org.mozilla.firefox', name: 'Firefox', category: 'Web', icon: 'public',
      description: 'Fast, private web browser', preinstall: true, exec: 'firefox',
      installed: true },
    { id: 'com.discordapp.Discord', name: 'Discord', category: 'Chat', icon: 'forum',
      description: 'Voice & text chat', preinstall: false, exec: 'discord',
      installed: false }
  ],
  categories: ['Web', 'Chat'],
  count: 2
};

function run(payload) {
  const ctx = {
    CATALOG: [{ id: 'seed', n: 'Seed', c: 'Web', i: 'public', d: 'seed entry' }],
    CATS: ['Web'],
    console: { debug() {} },
    window: {},
    fetch: () => Promise.resolve({ json: () => Promise.resolve(payload) }),
    module: {}
  };
  vm.createContext(ctx);
  vm.runInContext(m[0] + '\nmodule.exports = loadCatalog;', ctx);
  return new Promise((resolve) => {
    let called = false;
    ctx.module.exports(() => { called = true; });
    // fetch stub resolves on the microtask queue; let it drain.
    setTimeout(() => resolve({ ctx, called }), 10);
  });
}

const { ctx, called } = await run(PAYLOAD);

check('loadCatalog replaced the seed list with the canonical catalog',
      ctx.CATALOG.length === 2 && !ctx.CATALOG.some((a) => a.id === 'seed'),
      JSON.stringify(ctx.CATALOG.map((a) => a.id)));
check('it re-renders (calls the done callback)', called === true);
check('canonical category order adopted', JSON.stringify(ctx.CATS) === '["Web","Chat"]',
      JSON.stringify(ctx.CATS));

const ff = ctx.CATALOG.find((a) => a.id === 'org.mozilla.firefox');
check('a BAKED-IN app carries installed:true (renders "Installed", not Install)',
      !!ff && ff.installed === true, JSON.stringify(ff));
check('backend fields map onto the compact card shape the card reads',
      !!ff && ff.n === 'Firefox' && ff.c === 'Web' && ff.i === 'public' &&
      ff.d === 'Fast, private web browser', JSON.stringify(ff));

const dc = ctx.CATALOG.find((a) => a.id === 'com.discordapp.Discord');
check('a NOT-installed app stays installable (honest both ways)',
      !!dc && dc.installed === false, JSON.stringify(dc));

// ── Degrade-not-die: a failing/empty catalog must keep the seed list ─────────
const bad = await run({ apps: [] });
check('an EMPTY catalog response keeps the seed list (store never goes blank)',
      bad.ctx.CATALOG.length === 1 && bad.ctx.CATALOG[0].id === 'seed',
      JSON.stringify(bad.ctx.CATALOG.map((a) => a.id)));

console.log(fails.length ? '\nRESULT: ' + fails.length + ' FAILED' : '\nRESULT: ALL PASS');
process.exit(fails.length ? 1 : 0);

/*
 * A curated row survives refresh() because something READS `flagship`.
 *
 * The home prompt offers the curator three emphases,
 * `"emphasis": <flagship|ranked|normal>`. _home_curate maps flagship onto the row,
 * _sanitize_home_payload validates and forwards it, and the flag arrived at the
 * client having never been read by anything: `ranked` restyles every card in its
 * row, `normal` is the absence of both, and `flagship` did nothing at all.
 *
 * What actually kept the "Flagship agents" row alive was accidental. _replaceRow
 * matches on a DISPLAY TITLE, and 'Flagship agents' simply never collided with
 * 'Continue' or 'Recipes'. Retitle the row, or let a curator title its own row
 * 'Continue', and the live dashboard silently took it.
 *
 * This drives the REAL _replaceRow out of the REAL shipped hartHome.js and asserts
 * the observable payload after a replace: behavioural, the actual function, its
 * actual effect on rows.
 *
 * Run:  node tests/unit/test_home_row_protection.mjs
 * (Python wrapper test_home_row_protection.py shells out for pytest/CI.)
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const SRC = readFileSync(
  join(REPO, 'integrations', 'agent_engine', 'static', 'hartHome.js'), 'utf8');

const fails = [];
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); }
  else { console.log('  FAIL  ' + name + (detail ? ' -- ' + detail : '')); fails.push(name); }
}

// ── Slice the REAL _replaceRow out of the shipped file and evaluate it ──────
const m = SRC.match(/function _replaceRow\(payload, title, newRow, appendIfMissing\)\s*\{[\s\S]*?\n  \}/);
if (!m) {
  console.log('FAIL: could not slice _replaceRow() from hartHome.js -- the ' +
              'extractor drifted; this guard would be vacuous.');
  process.exit(1);
}
const ctx = { module: {} };
vm.createContext(ctx);
vm.runInContext(m[0] + '\nmodule.exports = _replaceRow;', ctx);
const replaceRow = ctx.module.exports;

const live = { title: 'Continue', accent: 'teal', cards: [{ title: 'live agent' }] };
const titles = (p) => (p.rows || []).map((r) => r.title);
const cardTitles = (r) => ((r && r.cards) || []).map((c) => c.title);

// ── 1. The ordinary case is untouched: an unprotected row IS replaced ───────
{
  const p = { rows: [{ title: 'Continue', cards: [{ title: 'stale' }] }] };
  replaceRow(p, 'Continue', live);
  check('an ordinary row is still replaced by the live fetch',
        cardTitles(p.rows[0]).join() === 'live agent',
        JSON.stringify(cardTitles(p.rows[0])));
}

// ── 2. The flag is READ: a curated row keeps its own cards ─────────────────
{
  const curated = { title: 'Continue', flagship: true, cards: [{ title: 'curated' }] };
  const p = { rows: [curated] };
  replaceRow(p, 'Continue', live);
  check('a flagship row is NOT replaced by the live fetch',
        cardTitles(p.rows[0]).join() === 'curated',
        JSON.stringify(cardTitles(p.rows[0])));
  check('and it is the SAME row object, not a copy that dropped the flag',
        p.rows[0] === curated && p.rows[0].flagship === true);
}

// ── 3. Protection must not fork the row: appendIfMissing stays off ─────────
// fetchRecipes passes appendIfMissing. A "found but protected" row that fell
// through to the append would put a SECOND row under the same title on the home,
// which is worse than the replacement it was protecting against.
{
  const p = { rows: [{ title: 'Recipes', flagship: true, cards: [{ title: 'curated' }] }] };
  replaceRow(p, 'Recipes', { title: 'Recipes', cards: [{ title: 'fetched' }] }, true);
  check('a protected row is not ALSO shadowed by an appended duplicate',
        titles(p).join() === 'Recipes', JSON.stringify(titles(p)));
  check('and the protected row is the one still standing',
        cardTitles(p.rows[0]).join() === 'curated');
}

// ── 4. appendIfMissing still appends when the row is genuinely absent ───────
{
  const p = { rows: [{ title: 'Continue', cards: [] }] };
  replaceRow(p, 'Recipes', { title: 'Recipes', cards: [{ title: 'fetched' }] }, true);
  check('an absent row is still appended when the caller asks',
        titles(p).join() === 'Continue,Recipes', JSON.stringify(titles(p)));
}

// ── 5. Without appendIfMissing an absent row is still not invented ─────────
{
  const p = { rows: [] };
  replaceRow(p, 'Continue', live);
  check('an absent row is not invented when the caller did not ask',
        titles(p).length === 0, JSON.stringify(titles(p)));
}

// ── 6. The shipped sample's own flagship row is protected under its title ──
// The end-to-end claim, against the real payload rather than a fixture: retitling
// the curated row to a fetched row's title no longer exposes it.
{
  const sm = SRC.match(/function samplePayload\(\)\s*\{[\s\S]*?\n  \}/);
  if (!sm) {
    check('samplePayload() is still sliceable for the end-to-end case', false);
  } else {
    const c2 = { module: {} };
    vm.createContext(c2);
    vm.runInContext(sm[0] + '\nmodule.exports = samplePayload;', c2);
    const p = c2.module.exports();
    const flag = (p.rows || []).find((r) => r.flagship === true);
    check('the shipped sample still carries a flagship row', !!flag);
    if (flag) {
      const before = cardTitles(flag).join();
      flag.title = 'Continue';                     // the collision that used to win
      replaceRow(p, 'Continue', live);
      check('a retitled curated row survives the live fetch on the flag alone',
            cardTitles(p.rows.find((r) => r.flagship === true)).join() === before);
    }
  }
}

console.log(fails.length ? '\nRESULT: ' + fails.length + ' FAILED' : '\nRESULT: ALL PASS');
process.exit(fails.length ? 1 : 0);

/*
 * The home surface must never FABRICATE the user's data (2026-07-24 real-HW).
 *
 * A just-installed node showed "2,140 Spark - 3 agents - 41 tasks" plus a
 * "Continue" row of invented half-finished work ("Trip to Goa 30%", "Invoice
 * chaser 80%", "Fix STT streaming 45%") and a "Top agents in the hive today" row
 * of fake network activity. None of it was the user's. It persisted because:
 *   - fetchEarnings KEEPS the sample on 401/offline (comment: "never a fabricated
 *     rupee figure" -- but the sample itself carried 2140), and
 *   - fetchAgents returns early when there are no agents, so the invented
 *     Continue cards were never displaced on a fresh box, and
 *   - the hive row has NO fetch at all -- permanent fiction labelled "from the
 *     network".
 *
 * This drives the REAL samplePayload() out of the REAL shipped hartHome.js (sliced
 * from the file and evaluated in a vm) and asserts the OBSERVABLE payload a fresh
 * box paints. Behavioural: the actual function, its actual return value.
 *
 * It also pins what must NOT be lost: the curated "Flagship agents" row is REAL
 * product (always-featured local agents with real prompts), not fabrication.
 *
 * Run:  node tests/unit/test_hart_home_no_fabricated_data.mjs
 * (Python wrapper test_hart_home_no_fabricated_data.py shells out for pytest/CI.)
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

// ── Slice the REAL samplePayload() out of the shipped file and evaluate it ──
const m = SRC.match(/function samplePayload\(\)\s*\{[\s\S]*?\n  \}/);
if (!m) {
  console.log('FAIL: could not slice samplePayload() from hartHome.js -- the ' +
              'extractor drifted; this guard would be vacuous.');
  process.exit(1);
}
const ctx = { module: {} };
vm.createContext(ctx);
vm.runInContext(m[0] + '\nmodule.exports = samplePayload;', ctx);
const p = ctx.module.exports();

const hero = p.hero || {};
const rows = p.rows || [];
const rowByTitle = (t) => rows.find((r) => (r.title || '').toLowerCase() === t.toLowerCase());

console.log('hero:', JSON.stringify({ amount: hero.amount, agents: hero.agents, tasks: hero.tasks }));

// ── 1. The hero figures are ZERO, not invented ──────────────────────────────
check('hero.amount is 0 (was a fabricated 2140)', hero.amount === 0,
      'got ' + hero.amount);
check('hero.agents is 0 (was a fabricated 3)', hero.agents === 0,
      'got ' + hero.agents);
check('hero.tasks is 0 (was a fabricated 41)', hero.tasks === 0,
      'got ' + hero.tasks);
check('hero.spark_series carries no invented settlements',
      Array.isArray(hero.spark_series) && hero.spark_series.length === 0,
      'got ' + JSON.stringify(hero.spark_series));
// The honesty flags stay: payout is genuinely not wired, and the count-up needs a
// number (0 is a number, so the animation still works).
check('hero.payout_pending stays true (honest: no payout rail wired)',
      hero.payout_pending === true);
check('hero.amount is still a NUMBER so the count-up animates',
      typeof hero.amount === 'number');

// ── 2. No invented user work / network activity ─────────────────────────────
const cont = rowByTitle('Continue');
check('a "Continue" row still exists (fetchAgents fills it from live agents)', !!cont);
check('"Continue" ships NO invented in-progress tasks',
      !!cont && Array.isArray(cont.cards) && cont.cards.length === 0,
      cont ? JSON.stringify((cont.cards || []).map((c) => c.title)) : 'row missing');

const hive = rowByTitle('Top agents in the hive today');
check('"Top agents in the hive today" ships NO fake network activity',
      !hive || (Array.isArray(hive.cards) && hive.cards.length === 0),
      hive ? JSON.stringify((hive.cards || []).map((c) => c.title)) : 'row absent');

// The specific fictions the steward saw must not reappear ANYWHERE in the sample.
const asText = JSON.stringify(p);
['Trip to Goa', 'Invoice chaser', 'Fix STT streaming', 'Inbox triage',
 'Weekly recap', 'Sheet Wizard', 'Deal Hunter'].forEach((fake) => {
  check('sample does not fabricate "' + fake + '"', asText.indexOf(fake) === -1);
});
check('sample does not carry the fabricated 2140 figure', asText.indexOf('2140') === -1);

// ── 3. What must NOT be lost: the curated REAL product agents ───────────────
const flag = rows.find((r) => r.flagship === true);
check('the curated "Flagship agents" row survives (real product, not fabrication)',
      !!flag && Array.isArray(flag.cards) && flag.cards.length >= 6,
      flag ? String((flag.cards || []).length) + ' cards' : 'row missing');
check('flagship cards still carry real launch prompts',
      !!flag && flag.cards.every((c) => typeof c.prompt === 'string' && c.prompt.length > 0));

console.log(fails.length ? '\nRESULT: ' + fails.length + ' FAILED' : '\nRESULT: ALL PASS');
process.exit(fails.length ? 1 : 0);

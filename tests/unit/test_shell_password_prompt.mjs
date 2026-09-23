/*
 * Behavioural test for the first-run password prompt in hartSessionUI.js.
 *
 * THE DEFECT (found 2026-09-22): maybeSetup() ran ONCE, 4 s after the session
 * blob loaded, and wrote `lock_setup_skipped` the moment the prompt was OFFERED.
 * So a user still inside onboarding at that moment was never asked, and a user
 * who was asked and simply did nothing had "skipped" written for them forever.
 * Onboarding's Esc hatch made it worse: it removes `onboarding-active` without
 * marking the user onboarded, and nothing re-checked after that.
 *
 * The contract this drives, through the REAL module on the shared DOM shim:
 *   [1] no prompt while `onboarding-active` is on <html>; when the class is
 *       REMOVED (onboarding finished or Esc-skipped) the prompt is offered, by
 *       observing the change, not by polling
 *   [2] being offered writes NOTHING; only an explicit decline (Escape, or the
 *       "Not now" control) writes `lock_setup_skipped`
 *   [3] `lock_setup_skipped` already set -> never offered
 *   [4] setting a password from the prompt stores the hash and does not mark skipped
 *   [5] an offer left unanswered is not re-offered in the same session, and the
 *       flag stays unset so the next boot asks again
 *
 * Run:  node tests/unit/test_shell_password_prompt.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { makeRealm, mkEv, flush } from './shell_dom_shim.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(HERE, '..', '..', 'integrations', 'agent_engine', 'static', 'hartSessionUI.js'), 'utf8');

let failures = 0;
function ok(cond, msg) { if (cond) { console.log('  OK   ' + msg); } else { failures++; console.log(' FAIL  ' + msg); } }

function boot(opts) {
  opts = opts || {};
  const R = makeRealm();
  const ls = R.el('lock-screen');
  const pw = R.el('lock-pw', ls); pw.tagName = 'INPUT';
  R.el('lock-status', ls);
  R.el('hart-widgets'); R.el('hw-sys-body');
  if (opts.onboarding) R.docEl.classList.add('onboarding-active');
  const blob = Object.assign({}, opts.blob || {});
  const writes = [];
  R.sandbox.HartSession = {
    ready(cb) { cb(blob); },
    get(k, d) { return Object.prototype.hasOwnProperty.call(blob, k) ? blob[k] : d; },
    set(k, v) { blob[k] = v; writes.push(k); }
  };
  R.sandbox.fetch = () => new Promise(() => {});   // metrics probe: never settles here
  R.run(SRC, 'hartSessionUI.js');
  return { R, ls, pw, blob, writes,
    prompted() { return ls.classList.contains('setup') && ls.classList.contains('active'); },
    // The 4 s post-ready check (and any focus timers) are recorded setTimeouts.
    elapse() { R.flushTimers(); },
    endOnboarding() { R.docEl.classList.remove('onboarding-active'); R.notifyObservers([{ attributeName: 'class' }]); } };
}

console.log('\n[1] onboarding gates the offer; its END triggers the re-check (observed, not polled)');
{
  const S = boot({ onboarding: true });
  S.elapse();
  ok(!S.prompted(), 'no prompt while onboarding-active is on <html>');
  ok(S.writes.indexOf('lock_setup_skipped') < 0, 'nothing written while onboarding runs');
  ok(S.R.intervals.every((i) => i.ms >= 1000), 'the re-check is not a fast poll (no sub-second interval was added)');
  S.endOnboarding();
  ok(S.prompted(), 'removing onboarding-active (finish or the Esc hatch) offers the password prompt');
  ok(S.writes.indexOf('lock_setup_skipped') < 0, 'being OFFERED writes nothing');
}

console.log('\n[2] only an explicit decline writes lock_setup_skipped');
{
  const S = boot({});
  S.elapse();
  ok(S.prompted(), 'with no onboarding the prompt is offered after the post-ready check');
  ok(S.blob.lock_setup_skipped === undefined, 'offer alone: flag unset');
  S.R.document.dispatch('keydown', mkEv(S.pw, { key: 'Escape' }));
  ok(S.blob.lock_setup_skipped === true, 'Escape on the prompt is a decline: flag written');
  ok(!S.ls.classList.contains('active') && !S.ls.classList.contains('setup'), 'decline closes the prompt');

  const T = boot({});
  T.elapse();
  const notNow = T.ls.querySelector('#lock-skip');
  ok(!!notNow, 'the prompt carries a visible "Not now" control (a mouse-only user can decline)');
  if (notNow) notNow.click();
  ok(T.blob.lock_setup_skipped === true, '"Not now" is a decline: flag written');
  ok(!T.prompted(), '"Not now" closes the prompt');
}

console.log('\n[3] a recorded decline is honoured');
{
  const S = boot({ onboarding: true, blob: { lock_setup_skipped: true } });
  S.elapse(); S.endOnboarding();
  ok(!S.prompted(), 'lock_setup_skipped already set: never offered');
}

console.log('\n[4] setting a password from the prompt is not a decline');
{
  const S = boot({});
  S.elapse();
  S.pw.value = 'abcd';
  S.R.window.unlock();
  await flush(); await flush();
  ok(typeof S.blob.lock_pw_hash === 'string' && S.blob.lock_pw_hash.length > 0, 'Enter with 4+ chars stores the salted hash');
  ok(S.blob.lock_setup_skipped === undefined, 'a set password does not write the skipped flag');
  ok(!S.prompted(), 'the prompt closes once the password is set');
}

console.log('\n[5] an unanswered offer is not re-offered this session, and stays unset for next boot');
{
  const S = boot({ onboarding: true });
  S.elapse(); S.endOnboarding();
  ok(S.prompted(), 'offered once');
  S.ls.classList.remove('active', 'setup');      // the user dismissed it some other way
  S.R.docEl.classList.add('onboarding-active'); S.endOnboarding();
  ok(!S.prompted(), 'a second onboarding-end in the same session does not re-offer');
  ok(S.blob.lock_setup_skipped === undefined, 'flag still unset: the next boot will ask again');
}

console.log(failures ? ('\nRESULT: ' + failures + ' FAILED') : '\nRESULT: ALL PASS');
process.exit(failures ? 1 : 0);

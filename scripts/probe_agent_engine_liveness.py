#!/usr/bin/env python
"""Live probe — is the agent engine alive AND draining?  Real HTTP, real LLM.

WHY THIS EXISTS AND WHY IT IS A SCRIPT, NOT A TEST
--------------------------------------------------
`tests/conftest.py` seals the network autouse for the whole test tree,
and that seal is load-bearing: a pooled connection opened by one test
hung a LATER one, so suites passed alone and hung together.  So NO
pytest suite here can do real HTTP or call a real model.

The J250 journey (`tests/e2e/test_agent_engine_liveness_journey.py`)
therefore covers the decision logic at the sanctioned seam, and THIS
script covers the other half: a real socket to a real Nunba, and a real
local model in the loop.  Together they are the whole assertion.

It reuses `LedgerProbe` from the harness rather than reimplementing the
checks — one implementation, so the live probe and the journey can never
disagree about what "alive" means.

WHAT IT CATCHES
---------------
2026-08-16: the engine completed NOTHING for 14 hours while /health said
up — Flask up, LLM up, DB up, 9,591 pending, 0 in flight, 1,010 reaped.
Every existing check passed.  Run this after a build, or on a schedule,
and that outage is a non-zero exit instead of a discovery hours later.

USAGE
    python scripts/probe_agent_engine_liveness.py
    python scripts/probe_agent_engine_liveness.py --url http://127.0.0.1:5000
    python scripts/probe_agent_engine_liveness.py --settle 30   # drain window
    python scripts/probe_agent_engine_liveness.py --no-llm      # skip the judge

EXIT CODES
    0  every check passed
    1  a check failed (daemon dead / queue not draining / LLM says unhealthy)
    2  could not probe at all (nothing serving) — a SKIP, not a pass
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..')))

from tests.e2e.agentic_harness import LedgerProbe   # noqa: E402

DEFAULT_URL = 'http://127.0.0.1:5000'
DEFAULT_LLM = 'http://127.0.0.1:8080/v1/chat/completions'

_OK, _FAIL, _SKIP = 'PASS', 'FAIL', 'SKIP'
_results = []


def check(name, fn):
    try:
        detail = fn()
        _results.append((_OK, name, detail or ''))
    except AssertionError as e:
        _results.append((_FAIL, name, str(e)))
    except Exception as e:                      # noqa: BLE001
        _results.append((_FAIL, name, '%s: %s' % (type(e).__name__, e)))


def ask_llm(url, prompt, timeout=120):
    """Real local model.  Returns text, or raises."""
    body = json.dumps({
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 200, 'temperature': 0,
    }).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode('utf-8', 'replace'))
    return data['choices'][0]['message']['content']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', default=DEFAULT_URL)
    ap.add_argument('--llm-url', default=DEFAULT_LLM)
    ap.add_argument('--settle', type=float, default=0.0,
                    help='seconds to watch for queue progress (0 = liveness '
                         'only; 30-60 gives a meaningful drain signal)')
    ap.add_argument('--no-llm', action='store_true')
    args = ap.parse_args()

    probe = LedgerProbe()
    stats = probe.agent_engine_stats(args.url)
    if stats is None:
        print('SKIP: nothing serving at %s — cannot probe.  This is NOT a '
              'pass; start Nunba and re-run.' % args.url)
        return 2

    by = (stats.get('stats') or {}).get('by_status') or {}
    daemon = stats.get('daemon')
    print('node   : %s' % args.url)
    print('ledger : %s' % json.dumps(by))
    print('daemon : %s' % json.dumps(daemon))
    print()

    if daemon is None:
        # A build predating the in-process liveness block.  Say so loudly:
        # absence of the signal is exactly how the 14h outage stayed hidden.
        print('FAIL  daemon-block-present')
        print('      the running build has no `daemon` block — it predates '
              'the in-process liveness probe.  Rebuild; do NOT read this '
              'absence as health.')
        return 1

    check('daemon-alive', lambda: probe.assert_daemon_alive(args.url))
    check('ledger-draining',
          lambda: probe.assert_ledger_advancing(
              base_url=args.url, settle_s=args.settle))

    if not args.no_llm:
        def _judge():
            summary = (
                'Agent node report: pending=%s in_progress=%s completed=%s '
                'failed=%s; worker thread_alive=%s tick_count=%s.'
                % (by.get('pending', 0), by.get('in_progress', 0),
                   by.get('completed', 0), by.get('failed', 0),
                   daemon.get('thread_alive'), daemon.get('tick_count')))
            answer = ask_llm(args.llm_url, summary + (
                '\n\nA healthy node drains its work queue. Is this node '
                'healthy? Reply with exactly one word: HEALTHY or UNHEALTHY.'))
            verdict = (answer or '').strip().upper()
            # Assert on the VERDICT, never on wording — harness doctrine.
            if 'UNHEALTHY' in verdict:
                raise AssertionError(
                    'local model judged this node UNHEALTHY: %s'
                    % answer.strip()[:200])
            if 'HEALTHY' not in verdict:
                raise AssertionError(
                    'model gave no usable verdict (got %r) — treat as '
                    'inconclusive, not as health' % answer.strip()[:120])
            return 'model says HEALTHY'
        check('llm-judge', _judge)

    print('-' * 66)
    failed = 0
    for status, name, detail in _results:
        print('%-5s %-18s %s' % (status, name, detail))
        if status == _FAIL:
            failed += 1
    print('-' * 66)
    print('%d passed, %d failed' % (len(_results) - failed, failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

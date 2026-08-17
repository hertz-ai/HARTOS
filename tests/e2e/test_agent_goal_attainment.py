"""Do the agents actually meet their goals?

Not "did the model write a good plan" and not "is the status column green" —
whether the chain from dispatch to metered work to completion is intact, per
agent, with the contradictions named.

WHY THIS EXISTS

The daemon decides completion on one condition (agent_daemon.py, dispatch
tail):

    elif spark_spent > 0:
        goal.status = 'completed'

Spend is the only evidence required. A dispatch that burned tokens and
produced nothing satisfies it, and so does any path that moved spark without
running the goal at all. Measured on production 2026-08-17:

    127 goals
     50 never dispatched            (39%)
     53 marked 'completed'
     43 of those 53 never dispatched (81%)
     37 marketing goals -> 23 never dispatched, 26 'completed', 1283 spark

So four fifths of the completions describe work that has no record of running.
This repo has already published 416 fabricated "PROOF: 0.0%" posts and kept a
benchmark ledger of 567 rows with zero results. A completion flag with no
side-effect check is that same failure a third time, and it is the one that
decides whether marketing and sales agents are working.

WHAT IS ASSERTED

Only the weakest honest invariant: completed implies dispatched. It does not
claim the work was good. Anything laxer would pass on all 43 rows that caused
this file to be written, and anything stronger would need a per-goal-type
definition of "produced a result" that does not exist yet.

Runtime, against the real DB (plan gate B6). Reads through LedgerProbe, which
is the existing read-only ledger view; no raw SQL and no second schema.
"""
from __future__ import annotations

import os
from collections import Counter

import pytest

from tests.e2e.agentic_harness import harness, skip_if_missing


@pytest.fixture(scope='module')
def attainment():
    skip_if_missing('integrations.social.models:AgentGoal')
    with harness() as h:
        records = h.ledger.goal_attainment()
    if not records:
        pytest.skip('no agent goals readable (DB not available in this env)')
    return records


def test_report_attainment_per_agent(attainment):
    """The deliverable. Always passes; its job is the table.

    Separate from the invariant below on purpose: when the invariant fails you
    still want the full picture in the same run, and an assertion that stops
    early would hide it.
    """
    total = len(attainment)
    dispatched = [r for r in attainment if r['dispatched']]
    phantom = [r for r in attainment if r['phantom_completion']]
    idle = [r for r in attainment if r['dispatched_but_idle']]
    completed = [r for r in attainment if r['status'] == 'completed']

    print('\n\n=== agent goal attainment ===')
    print(f'goals                       : {total}')
    print(f'ever dispatched             : {len(dispatched)} '
          f'({len(dispatched) / total:.0%})')
    print(f'marked completed            : {len(completed)}')
    print(f'completed but NEVER ran     : {len(phantom)}')
    print(f'dispatched but 0 spark spent: {len(idle)}')

    by_type = Counter(r['goal_type'] for r in attainment)
    print('\nper goal_type   goals  dispatched  completed  phantom')
    for gt, n in by_type.most_common():
        rows = [r for r in attainment if r['goal_type'] == gt]
        print(f'  {gt:<22}{n:<7}'
              f'{sum(1 for r in rows if r["dispatched"]):<12}'
              f'{sum(1 for r in rows if r["status"] == "completed"):<11}'
              f'{sum(1 for r in rows if r["phantom_completion"])}')

    if phantom:
        print('\nphantom completions (claimed done, never dispatched):')
        for r in phantom[:15]:
            print(f'  {r["goal_type"]:<20} spark={r["spark_spent"]:<6} '
                  f'{r["title"][:52]}')

    paused = [r for r in attainment if r['pause_reason']]
    if paused:
        print('\nauto-paused (the daemon already noticed these):')
        for r in paused[:8]:
            print(f'  {r["title"][:44]}: {r["pause_reason"][:70]}')

    out = os.environ.get('HEVOLVE_ATTAINMENT_OUT', '')
    if out:
        import json
        try:
            with open(out, 'w', encoding='utf-8') as fh:
                json.dump(attainment, fh, indent=2, default=str)
        except Exception as exc:
            print(f'(could not write {out}: {exc})')

    assert total > 0


@pytest.mark.xfail(
    strict=False,
    reason='known-failing on production as of 2026-08-17: 43 of 53 completed '
           'goals were never dispatched. xfail so the suite reports the '
           'breach without going red on a state nobody has fixed yet; remove '
           'the marker once completion requires evidence of work.',
)
def test_completed_goals_were_actually_dispatched(attainment):
    """completed implies dispatched. The weakest honest invariant.

    xfail rather than skip: a skip is invisible in a summary line, and this
    should stay legible until the daemon stops treating spend as attainment.
    """
    with harness() as h:
        h.ledger.assert_no_phantom_completions()


def test_a_goal_that_never_ran_is_not_counted_as_attained(attainment):
    """Guards the DEFINITION rather than the data.

    If someone later marks phantom_completion by status alone, or drops the
    dispatch check, the report above would quietly start showing healthy
    numbers for the same broken state. This pins the meaning.
    """
    for r in attainment:
        if r['status'] == 'completed' and not r['dispatched']:
            assert r['phantom_completion'], (
                'a completed-but-never-dispatched goal was NOT flagged as a '
                'phantom completion; the attainment definition has drifted'
            )
        if r['dispatched'] and r['spark_spent'] == 0:
            assert r['dispatched_but_idle'], (
                'a dispatched goal with zero metered work was not flagged '
                'idle; the attainment definition has drifted'
            )

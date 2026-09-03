"""Agent selection must not spend the tick's concurrency budget on skipped goals.

Line-traced on central 2026-09-03. The dispatch loop examined 2 of 5 active
goals, dispatched none, and logged "dispatched 1 goal(s)". Three defects in six
lines of `AgentDaemon._tick`:

  1. `dispatched` was BOTH the dispatch count (compared against max_concurrent)
     and the cursor into idle_agents (`candidate = idle_agents[dispatched]`).
     Stepping over an already-taken agent incremented the count, so it consumed
     a concurrency slot without dispatching anything.
  2. `used_agents.add(...)` ran at selection time, before build_prompt. Every
     robot goal returns None from build_prompt on a host with no robot hardware
     (central has none), so each one permanently consumed an idle agent.
  3. With max_concurrent == 1 (the headroom ceiling central runs under), 1 and 2
     compose into a dead tick: goal one burns the agent and skips, goal two
     finds it taken, the increment trips the budget, and `break` ends the tick.

Consequence: central's goals showed last_dispatched_at frozen at 2026-09-01
21:05 for over two days while the daemon ticked every 30s.

These are structural assertions, the same style as
test_the_speculative_branch_reaches_the_gate in test_goal_settlement_gate.py:
_tick is a 500-line method over live DB, guardrail, ledger and peer surfaces,
and the defect lives entirely in its control flow.

Run:
  pytest tests/unit/test_daemon_agent_selection.py -v
"""

import inspect
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import integrations.agent_engine.agent_daemon as ad  # noqa: E402


def _tick_source():
    return inspect.getsource(ad.AgentDaemon._tick)


def _selection_block(src):
    """The agent-selection block: from the scan to the availability break."""
    start = src.index('agent = next(') if 'agent = next(' in src \
        else src.index('agent = None')
    end = src.index('# No more available agents', start)
    return src[start:end]


def test_the_agent_scan_does_not_spend_the_dispatch_budget():
    """The cursor/count conflation itself. Walking past a taken agent must not
    increment `dispatched`, which is the loop's concurrency budget."""
    block = _selection_block(_tick_source())
    assert 'dispatched += 1' not in block, (
        'the agent scan increments the dispatch count, so stepping over a '
        'taken agent burns a concurrency slot and can end the tick with '
        'nothing dispatched')


def test_the_scan_does_not_index_idle_agents_by_the_dispatch_count():
    """`idle_agents[dispatched]` is the conflation in its clearest form."""
    src = _tick_source()
    assert 'idle_agents[dispatched]' not in src, (
        'idle_agents is indexed by the dispatch count, so the two meanings of '
        '`dispatched` cannot drift apart safely')


def test_an_agent_is_reserved_only_after_the_goal_clears_build_prompt():
    """A goal skipped after selection must leave its agent for the next goal.
    build_prompt returning None is the common case: every robot goal, on every
    host without robot hardware."""
    src = _tick_source()
    reserve = src.index("used_agents.add(agent['user_id'])")
    build = src.index('prompt = GoalManager.build_prompt(')
    assert reserve > build, (
        'the agent is reserved before build_prompt can reject the goal, so a '
        'skipped goal permanently consumes an idle agent')


def test_the_agent_is_reserved_before_the_goal_is_dispatched():
    """The other side of the same invariant: whatever the reservation moves
    past, it must still happen before dispatch, or two goals in one tick can be
    handed the same agent."""
    src = _tick_source()
    reserve = src.index("used_agents.add(agent['user_id'])")
    stamp = src.index('goal.last_dispatched_at = datetime.utcnow()')
    assert reserve < stamp, \
        'the agent is not reserved before dispatch, so it can be handed out twice'


def test_dispatched_counts_only_real_dispatches():
    """Every remaining `dispatched += 1` must sit next to an actual handoff, so
    the count the daemon logs and budgets against is the truth."""
    src = _tick_source()
    for line_no, line in enumerate(src.splitlines()):
        if 'dispatched += 1' not in line:
            continue
        window = '\n'.join(src.splitlines()[max(0, line_no - 6):line_no + 2])
        assert ('dispatch_goal(' in window
                or 'dispatch_speculative(' in window), (
            f'a `dispatched += 1` at offset {line_no} is not adjacent to a '
            f'dispatch, so the count overstates the work done')

"""TTL cache for check_goal_budget — daemon-tick storm guard.

py-spy on the live process showed the speculative-dispatch path
hitting check_goal_budget on every (goal × idle_agent) pair per
agent_daemon tick.  Each call opened a NEW SQLite connection
(NullPool by design for daemon write safety), ran the WAL PRAGMA
trio, ran an SQLAlchemy first() with row-lock, and committed.
Multiplied by goals × agents × ticks/minute, the GIL was saturated
(py-spy reported 8s+ sampling lag).

Fix: a 10-second per-goal TTL cache breaks the storm.  Within the
window, repeated calls return the cached tuple without touching
the DB.  Bounds under-counting at one un-deducted hit per goal
per TTL window — acceptable because this function is the only
writer to goal.spark_spent in the daemon path.

This test fails CI if any future change:
  - removes the cache fast-path
  - widens the TTL silently past 30s
  - returns a stale 'allowed' tuple after the cached remaining no
    longer covers the new estimated_cost (would let an over-budget
    daemon dispatch slip through)
  - drops the invalidate hook (admin top-ups must take effect
    promptly)
"""
import time
from unittest.mock import patch, MagicMock

import pytest

from integrations.agent_engine import budget_gate
from integrations.agent_engine.budget_gate import (
    check_goal_budget,
    invalidate_goal_budget_cache,
    _BUDGET_CACHE_TTL_S,
)


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Each test gets a clean cache so prior tests don't leak."""
    invalidate_goal_budget_cache()
    yield
    invalidate_goal_budget_cache()


def _mock_get_db_returning_goal(spark_budget=1000, spark_spent=0):
    """Build a get_db() patch that returns a goal with the given budget."""
    goal = MagicMock()
    goal.spark_budget = spark_budget
    goal.spark_spent = spark_spent

    db = MagicMock()
    query = db.query.return_value
    filter_chain = query.filter_by.return_value
    lock_chain = filter_chain.with_for_update.return_value
    lock_chain.first.return_value = goal
    return db, goal


def test_first_call_hits_db_subsequent_calls_use_cache():
    """The hot-path collapse: 100 calls within TTL → 1 DB roundtrip."""
    db, goal = _mock_get_db_returning_goal(spark_budget=1000, spark_spent=0)
    with patch.object(budget_gate, 'logger'):
        with patch('integrations.social.models.get_db', return_value=db):
            with patch('integrations.social.models.AgentGoal'):
                # First call: hits DB, deducts 5, caches result
                allowed, remaining, _ = check_goal_budget('goal_a', 5)
                assert allowed is True
                first_call_count = db.query.call_count

                # 99 more calls within TTL — should NOT hit DB again
                for _ in range(99):
                    allowed_n, _, _ = check_goal_budget('goal_a', 5)
                    assert allowed_n is True

                assert db.query.call_count == first_call_count, (
                    f"Cache must absorb repeat checks within TTL. "
                    f"DB query.call_count grew from {first_call_count} to "
                    f"{db.query.call_count} — cache is broken")


def test_no_goal_id_short_circuits_without_cache_or_db():
    """goal_id=None / '' must return immediately — no DB, no cache.
    This is the cheapest path and must stay fast."""
    db = MagicMock()
    with patch('integrations.social.models.get_db', return_value=db):
        allowed, remaining, reason = check_goal_budget(None, 100)
        allowed2, _, _ = check_goal_budget('', 100)
    assert allowed is True
    assert remaining == -1
    assert reason == 'no_goal_constraint'
    assert allowed2 is True
    assert db.query.call_count == 0


def test_denied_result_stays_denied_for_ttl_window():
    """An 'insufficient_budget' verdict must stick for the cache window.
    Re-querying every dispatch on a known-empty goal is wasted work."""
    db, _ = _mock_get_db_returning_goal(spark_budget=10, spark_spent=10)
    with patch('integrations.social.models.get_db', return_value=db):
        with patch('integrations.social.models.AgentGoal'):
            allowed, remaining, reason = check_goal_budget('goal_b', 5)
            assert allowed is False
            assert 'insufficient_budget' in reason
            first_calls = db.query.call_count

            # Repeated checks must hit cache, not DB
            for _ in range(50):
                allowed_n, _, reason_n = check_goal_budget('goal_b', 5)
                assert allowed_n is False
                assert 'insufficient_budget' in reason_n

            assert db.query.call_count == first_calls


def test_higher_cost_invalidates_cached_allowed():
    """A cached 'allowed' with remaining=N must NOT cover a new request
    for cost > N.  Otherwise an over-budget dispatch would slip through.

    This is the safety check: the cache is per-goal, but the affordability
    answer depends on cost.  Cached 'allowed' implies 'allowed for THAT
    cost' — a larger cost forces a fresh check.
    """
    db, goal = _mock_get_db_returning_goal(spark_budget=100, spark_spent=0)
    with patch('integrations.social.models.get_db', return_value=db):
        with patch('integrations.social.models.AgentGoal'):
            # First check — cost 10, plenty of budget; cache stores
            # remaining=90 after deduct.
            allowed1, remaining1, _ = check_goal_budget('goal_c', 10)
            assert allowed1 is True
            assert remaining1 == 90
            first_calls = db.query.call_count

            # Now ask for cost=50.  Cache has remaining=90 ≥ 50 → cache hit.
            allowed2, remaining2, _ = check_goal_budget('goal_c', 50)
            assert allowed2 is True
            assert db.query.call_count == first_calls, (
                "remaining=90 ≥ cost=50 — cache must serve")

            # Now ask for cost=200 > cached remaining=90.  Must NOT trust
            # cache; must fall through to DB.
            # We update the mock so the DB shows updated state
            goal.spark_spent = 90  # 10 + 50 reserved already in cache
            allowed3, _, reason3 = check_goal_budget('goal_c', 200)
            assert db.query.call_count == first_calls + 1, (
                f"cost=200 > cached remaining=90 — must re-query DB. "
                f"call_count went from {first_calls} to {db.query.call_count}")


def test_ttl_expiry_forces_fresh_db_call():
    """After TTL elapses, the next check must re-query the DB."""
    db, _ = _mock_get_db_returning_goal(spark_budget=1000, spark_spent=0)
    with patch('integrations.social.models.get_db', return_value=db):
        with patch('integrations.social.models.AgentGoal'):
            check_goal_budget('goal_d', 5)
            calls_after_first = db.query.call_count

            # Advance time past TTL
            real_time = time.time
            with patch('time.time', return_value=real_time() + _BUDGET_CACHE_TTL_S + 1):
                check_goal_budget('goal_d', 5)
            assert db.query.call_count == calls_after_first + 1, (
                "Cache must expire after TTL — stale verdicts would prevent "
                "topped-up budgets from being seen by the daemon")


def test_invalidate_clears_specific_goal_only():
    """invalidate_goal_budget_cache(goal_id) must scrub one entry, leave others."""
    db, _ = _mock_get_db_returning_goal(spark_budget=1000, spark_spent=0)
    with patch('integrations.social.models.get_db', return_value=db):
        with patch('integrations.social.models.AgentGoal'):
            check_goal_budget('goal_e', 1)
            check_goal_budget('goal_f', 1)
            calls_after_warm = db.query.call_count

            # Invalidate only goal_e — goal_f cache should still be live
            invalidate_goal_budget_cache('goal_e')
            check_goal_budget('goal_e', 1)
            check_goal_budget('goal_f', 1)
            assert db.query.call_count == calls_after_warm + 1, (
                f"Only goal_e should re-query; goal_f cache must remain hot. "
                f"Got {db.query.call_count - calls_after_warm} new queries")


def test_invalidate_all_clears_everything():
    """invalidate_goal_budget_cache() with no arg clears the whole cache."""
    db, _ = _mock_get_db_returning_goal(spark_budget=1000, spark_spent=0)
    with patch('integrations.social.models.get_db', return_value=db):
        with patch('integrations.social.models.AgentGoal'):
            check_goal_budget('g1', 1)
            check_goal_budget('g2', 1)
            warm = db.query.call_count

            invalidate_goal_budget_cache()
            check_goal_budget('g1', 1)
            check_goal_budget('g2', 1)
            assert db.query.call_count == warm + 2


def test_db_unavailable_path_does_not_crash_or_cache():
    """If get_db raises, the function must return the safe 'allow,
    unknown' tuple and NOT cache it (so the next attempt retries)."""
    with patch('integrations.social.models.get_db',
               side_effect=RuntimeError('db down')):
        allowed, remaining, reason = check_goal_budget('goal_z', 5)
    assert allowed is True
    assert remaining == -1
    assert reason == 'budget_system_unavailable'

    # Verify nothing got cached for goal_z
    from integrations.agent_engine.budget_gate import _budget_cache
    assert 'goal_z' not in _budget_cache, (
        "DB-unavailable path must NOT cache — caching would freeze the "
        "system in a 'silently allowing everything' state long after the "
        "DB recovered")


def test_ttl_constant_within_safe_bounds():
    """The TTL constant guard — too long = stale, too short = no benefit.
    User explicitly asked for 'smaller than 30s' to avoid staleness."""
    assert 1.0 <= _BUDGET_CACHE_TTL_S <= 30.0, (
        f"_BUDGET_CACHE_TTL_S={_BUDGET_CACHE_TTL_S} outside safe range. "
        f"Below 1s: cache provides no meaningful storm guard. "
        f"Above 30s: stale verdicts hold over admin top-ups too long.")

"""A stuck federation epoch must not park the goal-dispatch loop.

2026-08-17: nine agent_daemon threads were all parked at

    agent_daemon.py:790  in _loop
    agent_daemon.py:1590 in _tick        ->  fed_result = fed.tick()

get_federated_aggregator() is a singleton holding 7 locks, and tick() holds
them across peer broadcast / embedding / resonance work.  Called inline, one
stuck epoch stops _tick from returning, so goals never dispatch: the ledger sat
at 10,023 pending and the hive session stayed disconnected.

The try/except around the inline call could not help -- a hang is not an
exception.

agent_daemon.py:756-763 records the same failure from 2026-04-29
(WorldModelBridge.record_interaction).  That one was fixed by moving the
proactive tick off the dispatch loop; federation was left inline and reproduced
it at a new call site.  These tests pin the async shape and its single-flight
guard so it cannot regress a third time.
"""
import threading
import time

import pytest

from integrations.agent_engine.agent_daemon import AgentDaemon


def test_a_hanging_federation_epoch_does_not_block_the_caller(monkeypatch):
    """The dispatch loop must return while a federation epoch is still stuck."""
    d = AgentDaemon()
    release = threading.Event()
    entered = threading.Event()

    class _StuckAggregator:
        def tick(self):
            entered.set()
            release.wait(timeout=30)      # never released during the assert
            return {}

    import integrations.agent_engine.federated_aggregator as fa
    monkeypatch.setattr(fa, 'get_federated_aggregator',
                        lambda: _StuckAggregator())

    t0 = time.monotonic()
    d._spawn_federation_tick_async()
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0, (
        f"spawning the federation tick blocked the caller for {elapsed:.1f}s -- "
        f"a stuck epoch would park the dispatch loop")
    assert entered.wait(timeout=5), "federation tick never ran"
    release.set()


def test_single_flight_no_second_epoch_while_one_is_stuck(monkeypatch):
    """A stuck epoch must not accumulate threads on later ticks.

    Federation runs every 2nd tick.  Without this guard a permanently stuck
    aggregator would spawn a new thread every other tick forever.
    """
    d = AgentDaemon()
    release = threading.Event()
    starts = []

    class _StuckAggregator:
        def tick(self):
            starts.append(1)
            release.wait(timeout=30)
            return {}

    import integrations.agent_engine.federated_aggregator as fa
    monkeypatch.setattr(fa, 'get_federated_aggregator',
                        lambda: _StuckAggregator())

    for _ in range(5):
        d._spawn_federation_tick_async()
        time.sleep(0.05)

    assert len(starts) == 1, (
        f"{len(starts)} federation epochs started while one was still stuck -- "
        f"single-flight guard missing, threads would accumulate")
    release.set()


def test_a_failing_epoch_is_swallowed_and_retryable(monkeypatch):
    """An exception inside the epoch must not escape, and must not wedge the
    single-flight guard for later ticks."""
    d = AgentDaemon()
    calls = []

    class _BrokenAggregator:
        def tick(self):
            calls.append(1)
            raise RuntimeError("peer broadcast failed")

    import integrations.agent_engine.federated_aggregator as fa
    monkeypatch.setattr(fa, 'get_federated_aggregator',
                        lambda: _BrokenAggregator())

    d._spawn_federation_tick_async()
    for _ in range(40):
        prior = getattr(d, '_federation_thread', None)
        if prior is None or not prior.is_alive():
            break
        time.sleep(0.05)

    d._spawn_federation_tick_async()
    for _ in range(40):
        if len(calls) >= 2:
            break
        time.sleep(0.05)

    assert len(calls) == 2, (
        f"a failed epoch left the guard stuck: only {len(calls)} attempt(s) ran")


def test_result_is_logged_not_returned(monkeypatch, caplog):
    """tick()'s value only ever fed a log line; moving it off-thread must keep
    that behaviour so operators still see epoch/convergence."""
    d = AgentDaemon()

    class _AggregatingAggregator:
        def tick(self):
            return {'aggregated': True, 'epoch': 7, 'convergence': 0.5}

    import integrations.agent_engine.federated_aggregator as fa
    monkeypatch.setattr(fa, 'get_federated_aggregator',
                        lambda: _AggregatingAggregator())

    with caplog.at_level('INFO'):
        d._spawn_federation_tick_async()
        for _ in range(40):
            prior = getattr(d, '_federation_thread', None)
            if prior is None or not prior.is_alive():
                break
            time.sleep(0.05)

    assert any('epoch=7' in r.message or 'epoch=7' in r.getMessage()
               for r in caplog.records), (
        "aggregated epoch was not logged -- operators lose federation visibility")

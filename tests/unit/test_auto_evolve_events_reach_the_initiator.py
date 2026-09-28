"""auto_evolve.* progress events are addressed to the person who started the cycle.

Live evidence (frozen_debug.log, 2026-09-25 21:53:04 / 22:08:11 / 22:24:12):

    SSE broadcast refused (P3a privacy guard):
    topic='auto_evolve.none_approved' has no user_id in payload ...

AutoEvolveOrchestrator emitted every auto_evolve.* event with a payload that
carried no user_id, although start() is told who initiated the cycle (the
admin's id from the API, 'system' from the agent daemon).  The EventBus P3a
guard (core/platform/events.py) refuses a user-less SSE broadcast for any
topic that is not declared global, so every cycle logged a WARNING and the
initiator never received the event.

These tests drive the REAL EventBus (only the platform registry and the SSE
transport function are mocked) and assert what reaches the SSE transport.
"""
import logging
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import core.platform.events as events_mod  # noqa: E402
from integrations.agent_engine.auto_evolve import (  # noqa: E402
    AutoEvolveOrchestrator, EvolveSession)

INITIATOR = 'livetest_auto_evolve_admin'


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class _BusHarness:
    """Real EventBus, synchronous, registered as the platform 'events' service."""

    def __enter__(self):
        self.bus = events_mod.EventBus()
        self.bus.emit_async = self.bus.emit  # deterministic: same thread
        self.broadcasts = []
        registry = MagicMock()
        registry.has.return_value = True
        registry.get.return_value = self.bus
        self._patches = [
            patch('core.platform.registry.get_registry',
                  return_value=registry),
            patch.object(events_mod, 'broadcast_sse_safe',
                         side_effect=lambda topic, data, user_id=None:
                         self.broadcasts.append((topic, dict(data), user_id))),
        ]
        for p in self._patches:
            p.start()
        self.log = _Capture()
        logging.getLogger('hevolve.platform').addHandler(self.log)
        return self

    def __exit__(self, *exc):
        logging.getLogger('hevolve.platform').removeHandler(self.log)
        for p in reversed(self._patches):
            p.stop()
        return False

    def refusals(self):
        return [m for m in self.log.messages if 'P3a' in m]

    def delivered(self, topic):
        return [b for b in self.broadcasts if b[0] == topic]


def _run_cycle_to_end(orch, user_id):
    """start() runs the cycle on a named thread; wait for that thread."""
    result = orch.start(user_id=user_id)
    assert result['success'], result
    name = f"auto-evolve-{result['session_id']}"
    for t in threading.enumerate():
        if t.name == name:
            t.join(timeout=10)
            assert not t.is_alive(), 'cycle thread did not finish'
    return result


class TestAutoEvolveEventsReachTheInitiator(unittest.TestCase):

    def test_none_approved_is_delivered_to_the_admin_who_started_it(self):
        orch = AutoEvolveOrchestrator()
        with _BusHarness() as h, patch.object(
                orch, '_gather_candidates',
                return_value=[{'id': 'e1', 'title': 't'}]), patch.object(
                orch, '_constitutional_filter',
                side_effect=lambda s, c: c), patch.object(
                orch, '_rank_by_votes', return_value=[]):
            _run_cycle_to_end(orch, INITIATOR)

            self.assertEqual(h.refusals(), [])
            sent = h.delivered('auto_evolve.none_approved')
            self.assertEqual(len(sent), 1)
            topic, data, user_id = sent[0]
            self.assertEqual(user_id, INITIATOR)
            self.assertEqual(data['user_id'], INITIATOR)
            self.assertEqual(data['status'], 'completed')

    def test_daemon_cycle_with_no_candidates_is_addressed_to_system(self):
        orch = AutoEvolveOrchestrator()
        with _BusHarness() as h, patch.object(
                orch, '_gather_candidates', return_value=[]):
            _run_cycle_to_end(orch, 'system')

            self.assertEqual(h.refusals(), [])
            sent = h.delivered('auto_evolve.no_candidates')
            self.assertEqual([s[2] for s in sent], ['system'])

    def test_dispatching_and_started_carry_the_initiator(self):
        orch = AutoEvolveOrchestrator()

        def _dispatch(session, winners, user_id):
            session.dispatched = len(winners)

        with _BusHarness() as h, patch.object(
                orch, '_gather_candidates',
                return_value=[{'id': 'e1'}]), patch.object(
                orch, '_constitutional_filter',
                side_effect=lambda s, c: c), patch.object(
                orch, '_rank_by_votes',
                return_value=[{'id': 'e1'}]), patch.object(
                orch, '_dispatch_winners_parallel', side_effect=_dispatch):
            _run_cycle_to_end(orch, INITIATOR)

            self.assertEqual(h.refusals(), [])
            for topic in ('auto_evolve.dispatching', 'auto_evolve.started'):
                sent = h.delivered(topic)
                self.assertEqual([s[2] for s in sent], [INITIATOR], topic)
            self.assertEqual(
                h.delivered('auto_evolve.dispatching')[0][1]['experiments'],
                ['e1'])

    def test_reconcile_completion_reaches_the_initiator(self):
        orch = AutoEvolveOrchestrator()
        session = EvolveSession(status='running', dispatched=1,
                                user_id=INITIATOR)
        session.started_at = time.time()
        session.experiments = [
            {'id': 'e1', 'goal_id': 'g1', 'status': 'dispatched'}]
        orch._active_session = session
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [
            SimpleNamespace(id='g1', status='completed')]
        ctx = MagicMock()
        ctx.__enter__.return_value = db
        ctx.__exit__.return_value = False
        with _BusHarness() as h, patch(
                'integrations.social.models.db_session', return_value=ctx):
            orch.reconcile()

            self.assertEqual(h.refusals(), [])
            sent = h.delivered('auto_evolve.completed')
            self.assertEqual([s[2] for s in sent], [INITIATOR])

    def test_status_api_payload_does_not_expose_the_initiator(self):
        """GET /auto-evolve/status is open to any authenticated user; the
        initiator id travels only on the events addressed to that initiator."""
        session = EvolveSession(user_id=INITIATOR)
        self.assertNotIn('user_id', session.to_dict())

    def test_a_missing_initiator_falls_back_to_system(self):
        """The admin API passes body.get('user_id') when no auth id resolves;
        an explicit null must not reopen the user-less broadcast."""
        orch = AutoEvolveOrchestrator()
        with _BusHarness() as h, patch.object(
                orch, '_gather_candidates', return_value=[]):
            _run_cycle_to_end(orch, None)

            self.assertEqual(h.refusals(), [])
            self.assertEqual(
                [s[2] for s in h.delivered('auto_evolve.no_candidates')],
                ['system'])


if __name__ == '__main__':
    unittest.main()

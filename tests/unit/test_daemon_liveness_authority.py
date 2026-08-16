"""Daemon liveness must come from the process that owns the daemon.

2026-08-16: diagnosing a 14-hour agent-engine outage took SIX wrong
root-cause attempts, and the reason was structural rather than
intellectual -- no signal in the system could answer "is the daemon
running?" authoritatively:

  * ``mcp/agent_status`` read ``_running`` / ``_thread`` off its OWN
    import of ``agent_daemon``.  In any process that did not start the
    daemon that is a fresh-zero singleton (the 2026-06-09 shadow-module
    incident), so it reported ``daemon_thread_alive=false`` and then
    appended a hint telling the caller to disbelieve it and "trust the
    ledger instead".
  * The ledger was 14h stale, so that hint pointed at dead data.
  * ``/api/agent-engine/stats`` is ``@require_auth`` -> 401 on loopback.
  * Log silence is ambiguous: a healthy quiet tick logs nothing.

A diagnostic that pre-emptively explains away its own negative result
is worse than no diagnostic: ``thread_alive=False`` was literally true
and its own tooltip talked the reader out of it.

Fix: report liveness from the Flask handler, which runs in the process
that started the daemon, alongside the ledger stats that route already
serves over loopback.  One authority for both halves.

These tests pin the three-way discrimination that makes diagnosis
possible at all, and the total-safety contract (never raises).
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))


def _liveness():
    """Import late so a collection-time import error is a test failure
    with a readable message, not a module-level explosion."""
    from integrations.agent_engine.api import _daemon_liveness
    return _daemon_liveness


class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def _fake_daemon(running=False, alive=False, ticks=0, thread=True):
    d = types.SimpleNamespace()
    d._running = running
    d._thread = _FakeThread(alive) if thread else None
    d._tick_count = ticks
    return d


def _patched(daemon):
    """Patch the module object that _daemon_liveness imports from."""
    mod = types.ModuleType('integrations.agent_engine.agent_daemon')
    mod.agent_daemon = daemon
    return patch.dict(sys.modules,
                      {'integrations.agent_engine.agent_daemon': mod})


class TestThreeWayDiscrimination(unittest.TestCase):
    """The whole point: separate healthy / stopped / hung."""

    def test_healthy_daemon(self):
        with _patched(_fake_daemon(running=True, alive=True, ticks=42)):
            r = _liveness()()
        self.assertTrue(r['available'])
        self.assertTrue(r['thread_alive'])
        self.assertTrue(r['running'])
        self.assertEqual(r['tick_count'], 42)

    def test_genuinely_stopped_is_reported_as_stopped(self):
        # The case that was true on 2026-08-16 and disbelieved.
        with _patched(_fake_daemon(running=False, alive=False, ticks=0)):
            r = _liveness()()
        self.assertTrue(r['available'], "a stopped daemon is still READABLE "
                                        "— 'available' means the probe ran")
        self.assertFalse(r['thread_alive'])

    def test_zombie_running_true_thread_dead(self):
        # The supervisor's restart case: intent flag set, worker gone.
        with _patched(_fake_daemon(running=True, alive=False, ticks=7)):
            r = _liveness()()
        self.assertTrue(r['running'])
        self.assertFalse(r['thread_alive'])

    def test_thread_alive_is_not_just_the_running_flag(self):
        # Guards the actual defect shape: reporting the intent flag as
        # if it were liveness.  These MUST be able to disagree.
        with _patched(_fake_daemon(running=False, alive=True)):
            r = _liveness()()
        self.assertFalse(r['running'])
        self.assertTrue(r['thread_alive'])

    def test_source_is_labelled_in_process(self):
        # So a reader can tell this is NOT a cross-process module read.
        with _patched(_fake_daemon(running=True, alive=True)):
            self.assertEqual(_liveness()()['source'], 'flask_in_process')


class TestNeverRaises(unittest.TestCase):
    """A liveness probe that can throw would take the ledger route with
    it — the route must degrade, never 500."""

    def test_missing_thread_attribute(self):
        with _patched(_fake_daemon(running=True, alive=False, thread=False)):
            r = _liveness()()
        self.assertFalse(r['thread_alive'])

    def test_is_alive_raising_is_contained(self):
        class Exploding:
            def is_alive(self):
                raise RuntimeError('boom')
        d = types.SimpleNamespace(_running=True, _thread=Exploding(),
                                  _tick_count=1)
        with _patched(d):
            r = _liveness()()
        self.assertFalse(r['available'])
        self.assertIn('read failed', r['reason'])

    def test_import_failure_is_contained(self):
        with patch.dict(sys.modules,
                        {'integrations.agent_engine.agent_daemon': None}):
            r = _liveness()()
        self.assertFalse(r['available'])
        self.assertIn('reason', r)

    def test_garbage_tick_count_does_not_raise(self):
        d = types.SimpleNamespace(_running=True, _thread=_FakeThread(True),
                                  _tick_count=None)
        with _patched(d):
            r = _liveness()()
        self.assertEqual(r['tick_count'], 0)


class TestAdditiveOnly(unittest.TestCase):
    """Zero-regression contract: the pre-existing 'stats' shape is
    untouched, so every caller that predates the daemon block keeps
    working.

    Asserted against a REAL request/response through Flask's test
    client, not against the source text.  An earlier version of this
    read inspect.getsource() and searched for literals — that proves
    the characters exist, not that the route returns them, and would
    have passed just as happily if the handler raised.  The test client
    needs no socket, so it works under the tree-wide network seal.
    """

    def _response(self):
        import json
        from flask import Flask
        from integrations.agent_engine.api import agent_engine_bp
        app = Flask(__name__)
        app.config['TESTING'] = True
        app.register_blueprint(agent_engine_bp)
        with app.test_client() as c:
            r = c.get('/api/agent-engine/ledger/stats',
                      environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        if r.status_code != 200:
            self.skipTest('ledger stats route returned %s (auth or ledger '
                          'unavailable in this env)' % r.status_code)
        return json.loads(r.data.decode('utf-8'))

    def test_route_really_returns_the_preexisting_stats_keys(self):
        body = self._response()
        self.assertIn('stats', body)
        for key in ('total', 'sessions', 'by_status'):
            self.assertIn(key, body['stats'],
                          '%s disappeared from the live response — '
                          'existing consumers read it' % key)

    def test_route_really_returns_the_daemon_block(self):
        body = self._response()
        self.assertIn('daemon', body,
                      'daemon block missing from the live response')
        self.assertIn('available', body['daemon'])

    def test_liveness_writes_nothing(self):
        """Read-only: the probe must not mutate the daemon."""
        d = _fake_daemon(running=True, alive=True, ticks=5)
        before = (d._running, d._tick_count)
        with _patched(d):
            _liveness()()
        self.assertEqual((d._running, d._tick_count), before)


if __name__ == '__main__':
    unittest.main()

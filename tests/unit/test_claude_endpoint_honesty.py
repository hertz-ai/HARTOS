"""The claude-code shim must not claim capability the node lacks.

Two defects, both measured on central 2026-09-01 (langchain_gpt:c6514889):

  GET  /api/claude/v1/models           -> 200 {"data":[{"id":"claude-code",...}]}
  POST /api/claude/v1/chat/completions -> 502 {"message":"claude not on PATH"}

1. /models was a hardcoded constant, so a node with no `claude` binary reported
   the model as available while every completion failed. A probe of that route
   said "healthy" about a backend that cannot run. It cost me an hour: I read
   the 200 as proof the backend worked and reasoned from it.

2. 'notfound' mapped to 502. This module's own docstring says a lapsed
   subscription "must not error the OS; it degrades to local" and gives it 503.
   A missing binary is the same situation -- permanent, node-local, not the
   caller's fault -- so it degrades too.

What is NOT broken, and must stay that way: model_registry gates registration
on claude_code_available(), so a node without the binary never registers the
EXPERT backend and get_expert_model() cannot hand it out. Verified live on
central: claude_code_available() is False there, and its EXPERT tier is served
by Qwen (pooled_post HTTP 200). These tests must not push anyone toward
removing that gate or the endpoint on nodes that DO have Claude Code.

Runs standalone (`python tests/unit/test_claude_endpoint_honesty.py`).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from flask import Flask

from integrations.providers import claude_code_endpoint as cce


def _client():
    app = Flask(__name__)
    app.register_blueprint(cce.claude_code_bp)
    return app.test_client()


class ModelsRouteHonestyTest(unittest.TestCase):

    def test_models_empty_when_binary_absent(self):
        """The central case: no binary, so advertise nothing."""
        with patch.object(cce, 'claude_code_available', return_value=False):
            r = _client().get('/api/claude/v1/models')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['data'], [],
                         'route advertised a backend this node cannot run')

    def test_models_lists_when_binary_present(self):
        """The desktop case must be untouched — copilot still advertised."""
        with patch.object(cce, 'claude_code_available', return_value=True):
            r = _client().get('/api/claude/v1/models')
        self.assertEqual(r.status_code, 200)
        ids = [m['id'] for m in r.get_json()['data']]
        self.assertIn('claude-code', ids,
                      'a node WITH Claude Code must still advertise it')

    def test_models_shape_stays_openai_compatible(self):
        for avail in (True, False):
            with patch.object(cce, 'claude_code_available', return_value=avail):
                body = _client().get('/api/claude/v1/models').get_json()
            self.assertEqual(body.get('object'), 'list')
            self.assertIsInstance(body.get('data'), list)


class FailureStatusTest(unittest.TestCase):

    def test_missing_binary_degrades_not_errors(self):
        """'notfound' rides the fallback ladder like 'auth' does."""
        self.assertEqual(cce._FAIL_STATUS['notfound'], 503)

    def test_degrade_statuses_match_the_documented_ladder(self):
        self.assertEqual(cce._FAIL_STATUS['overload'], 503)
        self.assertEqual(cce._FAIL_STATUS['auth'], 503)
        self.assertEqual(cce._FAIL_STATUS['timeout'], 504)
        self.assertEqual(cce._FAIL_STATUS['other'], 502,
                         'a genuinely unknown failure should still surface')

    def test_notfound_completion_returns_503(self):
        fake = {'ok': False, 'category': 'notfound',
                'error': 'claude not on PATH', 'stderr': ''}
        with patch.object(cce, 'invoke_claude', return_value=fake):
            r = _client().post('/api/claude/v1/chat/completions',
                               json={'model': 'claude-code',
                                     'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status_code, 503,
                         'a missing binary must degrade to local, not error')
        self.assertEqual(r.get_json()['error']['category'], 'notfound')

    def test_success_still_returns_a_completion(self):
        fake = {'ok': True, 'stdout': 'PONG', 'stderr': '', 'rc': 0}
        with patch.object(cce, 'invoke_claude', return_value=fake):
            r = _client().post('/api/claude/v1/chat/completions',
                               json={'model': 'claude-code',
                                     'messages': [{'role': 'user', 'content': 'ping'}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.get_json()['choices'][0]['message']['content'], 'PONG')


class RegistryGateMustSurviveTest(unittest.TestCase):
    """The existing guard is the reason central never routed to the dead
    backend. Pin it so nobody 'simplifies' it away."""

    def test_registration_is_gated_on_availability(self):
        import inspect
        from integrations.agent_engine import model_registry
        src = inspect.getsource(model_registry)
        self.assertIn('if claude_code_available():', src,
                      'EXPERT registration must stay gated on the binary')


if __name__ == '__main__':
    unittest.main(verbosity=2)

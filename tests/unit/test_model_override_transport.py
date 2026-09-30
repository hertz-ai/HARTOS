"""#106d: a turn that names its model reaches /chat with that model.

The expert tier is tried before a human (#106d): the daemon dispatches a stuck
goal's next turn with the expert's model_config through dispatch_goal, whose
Tier 1 is local_chat_dispatch, the ONE in-process call to this node's /chat.
Nunba's adapter builds its /chat body from named fields, so an override handed
to it was dropped and the turn ran on the default model.  A turn with an
override therefore takes the native test-client path, which posts to the same
HARTOS app with model_config in the body; a turn without one is unchanged.

Adapter stand-ins are plain functions, so a call to them is recorded exactly.

    python -m pytest tests/unit/test_model_override_transport.py --noconftest -q
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import integrations.agent_engine.dispatch as dispatch_mod  # noqa: E402

_EXPERT = [{'model': 'claude-code',
            'base_url': 'http://127.0.0.1:5000/api/claude/v1'}]


def _adapter(fn):
    return {'routes.hartos_backend_adapter': types.SimpleNamespace(chat=fn)}


def _no_adapter():
    # None in sys.modules makes the import raise ImportError.
    return {'routes.hartos_backend_adapter': None, 'hartos_backend_adapter': None}


class _FakeApp:
    """hart_intelligence_entry.app's test client, recording each /chat body."""

    def __init__(self):
        self.bodies = []

    def test_client(self):
        app = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, path, json=None, headers=None):
                app.bodies.append((path, json))
                return types.SimpleNamespace(get_json=lambda: {'response': 'ran'})
        return _Client()


def _native(app):
    return {'hart_intelligence_entry': types.SimpleNamespace(app=app)}


@patch.object(dispatch_mod, 'is_user_recently_active', return_value=False)
def test_an_override_skips_the_adapter_that_would_drop_it(_active):
    adapter_calls = []

    def chat(**kw):          # Nunba's adapter: named fields only
        adapter_calls.append(kw)
        return {'text': 'ran on the default model'}
    app = _FakeApp()
    with patch.dict(sys.modules, dict(_adapter(chat), **_native(app))), \
            patch.object(dispatch_mod, '_internal_auth_headers', return_value={}):
        status = dispatch_mod.local_chat_dispatch(
            'p', 'u1', 11, daemon_id='g1', model_config=_EXPERT)
    assert status == ('ok', 'ran')
    assert adapter_calls == [], 'the adapter would run it on the default model'
    (path, body), = app.bodies
    assert path == '/chat'
    assert body['model_config'] == _EXPERT
    assert body['prompt'] == 'p' and body['prompt_id'] == 11
    assert body['autonomous'] is True
    assert body['request_id'] == dispatch_mod.daemon_request_id('g1')


@patch.object(dispatch_mod, 'is_user_recently_active', return_value=False)
def test_the_override_reaches_the_body_on_native_hartos(_active):
    app = _FakeApp()
    with patch.dict(sys.modules, dict(_no_adapter(), **_native(app))), \
            patch.object(dispatch_mod, '_internal_auth_headers', return_value={}):
        status = dispatch_mod.local_chat_dispatch(
            'p', 'u1', 11, daemon_id='g1', model_config=_EXPERT)
    assert status == ('ok', 'ran')
    assert app.bodies[0][1]['model_config'] == _EXPERT


@patch.object(dispatch_mod, 'is_user_recently_active', return_value=False)
def test_without_an_override_the_adapter_still_takes_the_turn(_active):
    chat = MagicMock(return_value={'text': 'ok'})
    app = _FakeApp()
    with patch.dict(sys.modules, dict(_adapter(chat), **_native(app))):
        status = dispatch_mod.local_chat_dispatch('p', 'u1', 11, daemon_id='g1')
    assert status == ('ok', 'ok')
    assert 'model_config' not in chat.call_args.kwargs
    assert app.bodies == []


@patch.object(dispatch_mod, 'is_user_recently_active', return_value=True)
def test_an_override_turn_still_yields_to_a_live_user(_active):
    app = _FakeApp()
    with patch.dict(sys.modules, dict(_no_adapter(), **_native(app))):
        status = dispatch_mod.local_chat_dispatch(
            'p', 'u1', 11, daemon_id='g1', model_config=_EXPERT)
    assert status == ('deferred', None)
    assert app.bodies == []


def test_dispatch_goal_hands_the_override_to_tier_one():
    guard = MagicMock()
    guard.GuardrailEnforcer.before_dispatch.return_value = (True, 'ok', 'the prompt')
    mods = {
        'integrations.agent_engine.budget_gate': MagicMock(
            pre_dispatch_budget_gate=MagicMock(return_value=(True, 'ok'))),
        'integrations.social.models': None,   # no goal row; no DB opened
        'security.hive_guardrails': guard,
        'security.immutable_audit_log': MagicMock(
            get_audit_log=MagicMock(return_value=MagicMock())),
    }
    tier2 = MagicMock()
    with patch.dict(sys.modules, mods), \
            patch.object(dispatch_mod, '_get_distributed_coordinator', return_value=None), \
            patch.object(dispatch_mod, 'local_chat_dispatch',
                         return_value=('ok', 'expert answer')) as tier1, \
            patch.object(dispatch_mod, 'pooled_post', tier2):
        result = dispatch_mod.dispatch_goal(
            'the prompt', 'u1', 'g1', 'marketing', model_config=_EXPERT)
    assert result == 'expert answer'
    assert tier1.call_args.kwargs['model_config'] == _EXPERT
    tier2.assert_not_called()

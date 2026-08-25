"""require_consent enforcement lives INSIDE GuardrailEnforcer.before_dispatch.

Owner directive 2026-08-25 (#698): daemons work autonomously WITH human
consent.  goal_seeding has carried 'require_consent': True with zero
enforcement sites.  First attempt (4bbb1758) added a standalone gate in
dispatch.py — owner called the parallel path: before_dispatch step 3 is
the designed goal-specific policy point (constitutional, ethos) and the
consent check belongs there.  This rework folds it in: ONE policy gate,
N policies.  dispatch_goal now passes goal.to_dict() + user_id the same
way agent_daemon.py:1268 always has.

The denied case was proven RED before any enforcement existed.
"""
import contextlib
import types
from unittest.mock import MagicMock

import pytest


def _patch_consent(monkeypatch, granted):
    from integrations.social import models as social_models
    from integrations.social.consent_service import ConsentService

    db = MagicMock()

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db

    monkeypatch.setattr(social_models, 'db_session', db_session)
    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: granted))
    req_spy = MagicMock(return_value=types.SimpleNamespace(granted=False))
    monkeypatch.setattr(ConsentService, 'request_consent',
                        staticmethod(req_spy))
    return req_spy


def _quiet_other_policies(monkeypatch):
    """Isolate the consent policy from its guardrail siblings."""
    from security import hive_guardrails as hg
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_prompt',
                        staticmethod(lambda p: (True, 'ok')))
    monkeypatch.setattr(hg.ConstitutionalFilter, 'check_goal',
                        staticmethod(lambda g: (True, 'ok')))
    monkeypatch.setattr(hg.HiveEthos, 'check_goal_ethos',
                        staticmethod(lambda g: (True, 'ok')))
    monkeypatch.setattr(hg.HiveEthos, 'rewrite_prompt_for_togetherness',
                        staticmethod(lambda p: p))


FLAGGED = {'config_json': {'require_consent': True}}


def test_flagged_goal_without_consent_blocks_and_files_request(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id='user-1')

    assert allowed is False
    assert 'consent' in reason.lower()
    assert req_spy.call_count == 1, (
        "gate must file the pending request the UserConsent UI surfaces")


def test_flagged_goal_with_consent_passes(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=True)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id='user-1')

    assert allowed is True
    assert req_spy.call_count == 0


def test_unflagged_goal_touches_no_consent_machinery(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)

    allowed, _, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict={'config_json': {}}, user_id='user-1')

    assert allowed is True
    assert req_spy.call_count == 0


def test_flagged_goal_without_user_context_blocks(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    _patch_consent(monkeypatch, granted=True)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'user context' in reason


def test_dispatch_goal_feeds_goal_dict_and_user_to_the_one_gate(monkeypatch):
    """dispatch_goal must load the goal row and pass it to before_dispatch —
    prompt-only left the goal-specific policies dormant on this path."""
    from integrations.agent_engine import dispatch
    from integrations.social import models as social_models
    from security import hive_guardrails as hg

    goal = types.SimpleNamespace(
        id='g-1', to_dict=lambda: dict(FLAGGED))
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = goal

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db

    monkeypatch.setattr(social_models, 'db_session', db_session)

    seen = {}

    def spy_before_dispatch(prompt, goal_dict=None, node_id=None,
                            user_id=None):
        seen['goal_dict'] = goal_dict
        seen['user_id'] = user_id
        return False, 'stop-here', prompt

    monkeypatch.setattr(hg.GuardrailEnforcer, 'before_dispatch',
                        staticmethod(spy_before_dispatch))
    from integrations.agent_engine import budget_gate as bg
    monkeypatch.setattr(bg, 'pre_dispatch_budget_gate',
                        lambda *a, **k: (True, 'ok'))

    result = dispatch.dispatch_goal('p', 'user-1', 'g-1', 'marketing')

    assert result is None
    assert seen['goal_dict'] == FLAGGED
    assert seen['user_id'] == 'user-1'

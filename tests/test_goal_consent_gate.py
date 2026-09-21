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
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'user context' in reason


def test_daemon_goal_falls_back_to_boot_owner_and_asks(monkeypatch):
    """A user-less goal on an owned desktop asks the OWNER, not a wall.

    Before this fallback the daemon's require_consent goals looped
    blocked forever because request_consent needs a user to file the
    pending record against; HEVOLVE_OWNER_USER_ID (boot-set, same trust
    as crossbar publish auth) names that user.
    """
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-9')

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is False
    assert 'pending request filed' in reason
    assert req_spy.call_count == 1
    assert req_spy.call_args[0][1] == 'owner-9'


def test_daemon_goal_with_boot_owner_and_granted_consent_dispatches(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=True)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', 'owner-9')

    allowed, _, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED, user_id=None)

    assert allowed is True
    assert req_spy.call_count == 0


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


# ── #96: the plural spelling was written but never enforced ─────────────
# goal_seeding writes BOTH require_consent (:1745,1863,1892,1948,2009) AND
# requires_consent (:118,514,580) into config_json, but this gate was the only
# enforcement site and read the singular only — so every goal seeded with the
# plural dispatched UNGATED. The gate now reads both spellings.
FLAGGED_PLURAL = {'config_json': {'requires_consent': True}}


def test_plural_spelling_is_also_gated(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED_PLURAL, user_id='user-1')

    assert allowed is False, (
        "a goal seeded with requires_consent must be gated too (#96) — it "
        "dispatched ungated while only require_consent was read")
    assert 'consent' in reason.lower()
    assert req_spy.call_count == 1


def test_plural_spelling_passes_with_consent(monkeypatch):
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=True)

    allowed, _reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED_PLURAL, user_id='user-1')

    assert allowed is True
    assert req_spy.call_count == 0


def test_gate_files_no_request_when_it_cannot_name_a_human(monkeypatch):
    """The dead end this gate has when nobody can be asked.

    With no user_id AND no HEVOLVE_OWNER_USER_ID (central/regional set it
    NOWHERE — only desktop does, from guest_identity), the gate refuses with
    'without user context' and files NOTHING: blocked, nobody asked, nothing to
    grant. Pinned so it is a KNOWN dead end, not a surprise — it is why callers
    must pass the requester they already hold.
    """
    from security.hive_guardrails import GuardrailEnforcer
    _quiet_other_policies(monkeypatch)
    req_spy = _patch_consent(monkeypatch, granted=False)
    monkeypatch.delenv('HEVOLVE_OWNER_USER_ID', raising=False)

    allowed, reason, _ = GuardrailEnforcer.before_dispatch(
        'p', goal_dict=FLAGGED_PLURAL, user_id=None)

    assert allowed is False
    assert 'user context' in reason
    assert req_spy.call_count == 0, (
        "no human to name, so no request can be filed — this is the dead end")


def test_daemon_path_passes_the_goals_owner_as_requester():
    """agent_daemon must not drop the requester it already holds.

    The daemon loop has the goal in hand and AgentGoal.owner_id is a real
    column, but it used to call before_dispatch(prompt, goal.to_dict()) with no
    user_id — so on any topology without HEVOLVE_OWNER_USER_ID every
    consent-flagged goal hit the dead end above. dispatch_goal already passes
    its user_id (dispatch.py); this pins that the daemon path does too, since a
    silent regression here stops goals with no way to grant.
    """
    import inspect
    from integrations.agent_engine import agent_daemon
    src = inspect.getsource(agent_daemon)
    assert 'user_id=goal.owner_id' in src, (
        "daemon before_dispatch must pass the goal's owner as the requester, or "
        "a consent-flagged goal is blocked with nobody to ask")
    # and no guardrail call may pass the goal WITHOUT naming the requester
    for seg in src.split('GuardrailEnforcer.before_dispatch(')[1:]:
        call = seg[:160]
        if 'goal.to_dict()' in call:
            assert 'user_id=' in call, (
                f"requester-less guardrail call is the dead end: {call!r}")

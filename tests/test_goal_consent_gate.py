"""Goals flagged require_consent must NOT dispatch without a granted consent.

Owner directive 2026-08-25 (#698): daemons work fully autonomously WITH human
consent.  goal_seeding.py has carried 'require_consent': True on seeded goals
(:1745 parent approval, :1863/:1892 microphone, :1948, :2009) with ZERO
enforcement sites — the flag was decorative and every flagged goal dispatched
freely.  This wires the flag at the canonical dispatch chokepoint
(dispatch.dispatch_goal), BEFORE the budget gate (consent outranks spend),
using the existing ConsentService: check_consent (3-tier, blanket-aware) and
the previously caller-less request_consent (#666) which creates the pending
record the shipped UserConsent UI surfaces for the human to grant.

Proven RED before the gate existed: the denied case dispatched straight into
the budget gate.
"""
import contextlib
import types
from unittest.mock import MagicMock

import pytest


def _fake_models(config):
    """Fake integrations.social.models surface for the gate's imports."""
    goal = types.SimpleNamespace(config_json=config, id='g-1')
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = goal

    @contextlib.contextmanager
    def db_session(commit=False):
        yield db

    return db, db_session


def _arm(monkeypatch, *, config, granted, budget_spy):
    from integrations.social import models as social_models
    from integrations.social.consent_service import ConsentService
    from integrations.agent_engine import budget_gate as bg

    db, db_session = _fake_models(config)
    monkeypatch.setattr(social_models, 'db_session', db_session)

    monkeypatch.setattr(ConsentService, 'check_consent',
                        staticmethod(lambda *a, **k: granted))
    req_spy = MagicMock(return_value=types.SimpleNamespace(granted=False))
    monkeypatch.setattr(ConsentService, 'request_consent',
                        staticmethod(req_spy))

    monkeypatch.setattr(bg, 'pre_dispatch_budget_gate', budget_spy)
    return req_spy


def test_flagged_goal_blocks_and_files_pending_request(monkeypatch):
    from integrations.agent_engine import dispatch

    budget_spy = MagicMock(return_value=(False, 'sentinel'))
    req_spy = _arm(monkeypatch,
                   config={'require_consent': True}, granted=False,
                   budget_spy=budget_spy)

    result = dispatch.dispatch_goal('p', 'user-1', 'g-1', 'marketing')

    assert result is None, "consent-denied goal must not dispatch"
    assert req_spy.call_count == 1, (
        "gate must file the pending consent request (the UserConsent UI "
        "surfaces it for the human)")
    assert budget_spy.call_count == 0, (
        "consent gate must sit BEFORE the budget gate")


def test_granted_goal_passes_consent_gate(monkeypatch):
    from integrations.agent_engine import dispatch

    budget_spy = MagicMock(return_value=(False, 'sentinel-stop'))
    req_spy = _arm(monkeypatch,
                   config={'require_consent': True}, granted=True,
                   budget_spy=budget_spy)

    result = dispatch.dispatch_goal('p', 'user-1', 'g-1', 'marketing')

    assert budget_spy.call_count == 1, (
        "granted goal must reach the budget gate (passage proven)")
    assert req_spy.call_count == 0
    assert result is None  # stopped by the budget sentinel, not consent


def test_unflagged_goal_untouched(monkeypatch):
    from integrations.agent_engine import dispatch

    budget_spy = MagicMock(return_value=(False, 'sentinel-stop'))
    req_spy = _arm(monkeypatch, config={}, granted=False,
                   budget_spy=budget_spy)

    dispatch.dispatch_goal('p', 'user-1', 'g-1', 'marketing')

    assert budget_spy.call_count == 1
    assert req_spy.call_count == 0, "no consent machinery for unflagged goals"

"""The owner's answer to a copilot ask acts on the copilot switch itself.

When a goal's stuck action is handed to the expert and the expert is the
owner's Claude Code subscription, the daemon consults the copilot switch
(claude_code_backend.copilot_enabled).  Off means the owner is asked ONCE
through the consent card every other ask uses, named by the goal, and the
goal is skipped this tick but stays active; the grant flips the SAME switch
the admin page flips (set_copilot_enabled), so the next tick takes the turn
with no park and no resume handler.  A standing "Don't allow" hands the
action to a person through escalate_goal, as an unavailable expert does.

The switch is read live: a copilot turned on after boot gets its backend
registered by ensure_claude_code_registered without a restart.

    python -m pytest tests/unit/test_copilot_consent_gate.py -q
"""
import os
import tempfile

os.environ['HEVOLVE_DB_PATH'] = ':memory:'
os.environ['CLAUDE_CONFIG_DIR'] = tempfile.mkdtemp(prefix='copilot_gate_')

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

from integrations.coding_agent import claude_code_backend as cc  # noqa: E402
from integrations.social.consent_service import ConsentService  # noqa: E402
from integrations.social.models import (  # noqa: E402
    AgentGoal, Base, UserConsent, db_session, get_engine)

OWNER = 'owner-1'


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    engine = get_engine()
    Base.metadata.create_all(engine)
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', OWNER)
    monkeypatch.delenv('HARTOS_COPILOT_ENABLED', raising=False)
    cc.set_copilot_enabled(True)
    yield
    cc.set_copilot_enabled(True)
    Base.metadata.drop_all(engine)


def _goal(db):
    g = AgentGoal(goal_type='marketing', title='Spider-Man', status='active',
                  config_json={'escalation': {
                      'next': 'expert', 'expert': 'claude-code', 'at': 't1',
                      'action_id': 2, 'action': 'write the launch post',
                      'user_prompt': 'p', 'prompt_id': '77', 'flow': 0}})
    db.add(g)
    db.flush()
    return g


def _asks(db, user):
    return db.query(UserConsent).filter_by(
        user_id=user, consent_type=cc.COPILOT_CONSENT_TYPE).all()


# ── the consent acts on the switch ──────────────────────────────────────────

def test_a_grant_turns_the_copilot_on_and_a_revoke_turns_it_off():
    cc.set_copilot_enabled(False)
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, cc.COPILOT_CONSENT_TYPE)
    assert cc.copilot_enabled() is True
    with db_session() as db:
        ConsentService.revoke_consent(db, OWNER, cc.COPILOT_CONSENT_TYPE)
    assert cc.copilot_enabled() is False


def test_another_consent_type_leaves_the_switch_alone():
    cc.set_copilot_enabled(False)
    with db_session() as db:
        ConsentService.grant_consent(db, OWNER, 'screen_capture')
    assert cc.copilot_enabled() is False


# ── the daemon's gate ────────────────────────────────────────────────────────

def test_switch_off_asks_the_owner_once_and_skips_without_parking():
    from integrations.agent_engine.agent_daemon import _escalation_model_config
    cc.set_copilot_enabled(False)
    with patch('integrations.social.consent_service._emit') as emit, db_session() as db:
        goal = _goal(db)
        assert _escalation_model_config(db, goal) == (None, True)
        assert goal.status == 'active', 'a pending ask must not park the goal'
        assert len(_asks(db, OWNER)) == 1
        topic, ask = emit.call_args.args
        assert topic == 'consent.request'
        assert ask['consent_type'] == cc.COPILOT_CONSENT_TYPE
        assert ask['requester_name'] == 'Spider-Man'
        assert 'launch post' in ask['reason']
        # a second tick does not file a second ask
        assert _escalation_model_config(db, goal) == (None, True)
        assert len(_asks(db, OWNER)) == 1


def test_the_grant_unparks_by_itself_on_the_next_tick():
    from integrations.agent_engine.agent_daemon import _escalation_model_config
    from integrations.agent_engine.model_registry import model_registry
    cc.set_copilot_enabled(False)
    with patch('integrations.social.consent_service._emit'), db_session() as db:
        goal = _goal(db)
        assert _escalation_model_config(db, goal) == (None, True)
        ConsentService.grant_consent(db, OWNER, cc.COPILOT_CONSENT_TYPE)
        assert cc.copilot_enabled() is True
        cfg, parked = _escalation_model_config(db, goal)
        assert parked is False
        registered = model_registry.get_model('claude-code') is not None
        # With the CLI present the turn proceeds on claude-code; without it the
        # backend cannot register and the existing "unavailable" park applies.
        if registered:
            assert cfg and cfg[0]['model'] == 'claude-code'
        else:
            assert cfg is None and goal.status == 'paused'


def test_a_standing_no_hands_the_action_to_a_person():
    from integrations.agent_engine.agent_daemon import _escalation_model_config
    cc.set_copilot_enabled(False)
    with patch('integrations.social.consent_service._emit'), db_session() as db:
        goal = _goal(db)
        ConsentService.request_consent(db, OWNER, cc.COPILOT_CONSENT_TYPE)
        ConsentService.revoke_consent(db, OWNER, cc.COPILOT_CONSENT_TYPE)   # "Don't allow"
        assert _escalation_model_config(db, goal) == (None, True)
        assert goal.status == 'paused'
        assert 'not allowed' in (goal.config_json or {}).get('pause_reason', '') \
            or 'not allowed' in str((goal.config_json or {}).get('escalation'))


def test_switch_on_is_never_asked():
    from integrations.agent_engine.agent_daemon import _escalation_model_config
    cc.set_copilot_enabled(True)
    with patch('integrations.social.consent_service._emit') as emit, db_session() as db:
        goal = _goal(db)
        _escalation_model_config(db, goal)
        assert not _asks(db, OWNER)
        assert not any(c.args[0] == 'consent.request' for c in emit.call_args_list)

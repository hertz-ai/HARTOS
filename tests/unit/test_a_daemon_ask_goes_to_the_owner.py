"""A background goal's step whose output is a question goes to its PERSON.

The owner's ruling, 31 Oct (relayed by hartos-00): a daemon, hive or timer
goal's step whose output is a question pauses the goal, the owner is sent
the question, and their answer resumes the step: not GAVE_UP-and-retry, not
skip, never "done".

Part B1, the park: GoalManager.escalate_goal takes the question as ``ask``.
From hartos-00's review of the design:
  * Q1: an explicit ask selects the human stage directly; the question is the
    owner's to answer, not the expert model's.  Never a faked
    tried=['expert'].
  * Q3: a node with nobody to tell parks with the existing no-owner cause
    and sends nothing.
  * item 3: the question rides the in-app row only; the push carries the
    goal's scrubbed title, never the question.
From hartos-c9's conditions (2 Nov):
  * the person told is core.event_attribution.person_for(the goal's owner):
    measured on the owner's desktop, 38 of 38 active goals name no owner
    (system_bootstrap) and a goal's REUSE session runs as an agent row, so
    nearly every background question is the node owner's;
  * the notice says what works today and nothing more (no answer route
    until Part B3);
  * a no-owner park is never silent: a warning names the goal and the cause;
  * a dashboard resume parks again, tells nobody twice, and never ends the
    goal done, skipped or retried.

Real GoalManager, NotificationService and in-memory social DB; Firebase is
recorded at core.fcm_sync.send_fcm_push (the harness of
test_central_asks_reach_the_person.py).

    python -m pytest tests/unit/test_a_daemon_ask_goes_to_the_owner.py -q
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from types import SimpleNamespace

os.environ['HEVOLVE_DB_PATH'] = ':memory:'

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from integrations.social.models import AgentGoal, User, db_session  # noqa: E402
from tests.unit.test_central_asks_reach_the_person import (  # noqa: E402,F401
    _goal, _notifications, _person, _settle, pushes)

_QUESTION = 'Which topic should the next lesson cover, fractions or decimals?'
_EXPERT = SimpleNamespace(model_id='qwen3.8-27b-expert')


def _ask(goal_id, monkeypatch, question=_QUESTION):
    """The daemon turn's ask, handed on the way CREATE's _ask_for_help hands
    a stuck action on, with the question as ``ask``.  An expert IS
    configured: the question must still go to the person."""
    from integrations.agent_engine.goal_manager import GoalManager
    monkeypatch.setattr(GoalManager, '_escalation_expert',
                        staticmethod(lambda goal_type=None: _EXPERT))
    record = {'action_id': 3, 'action': 'Ask the learner which topic comes next',
              'reason': 'the step asked a question', 'tried': ['local'],
              'user_prompt': 'u_7'}
    if question is not None:
        record['ask'] = question
    with db_session() as db:
        return GoalManager.escalate_goal(db, goal_id, record)


def _config(goal_id):
    with db_session(commit=False) as db:
        goal = db.query(AgentGoal).filter_by(id=goal_id).first()
        return goal.status, dict(goal.config_json or {})


def _agent(owner_id=None):
    """An agent's user row, the kind a background goal's session runs as."""
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'agent_{uid[:8]}', display_name='an agent',
                    user_type='agent', owner_id=owner_id, api_token=f'tok-{uid}'))
    return uid


def test_an_ask_goes_to_the_owner_even_with_an_expert_configured(pushes, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Tutor the learner')
    result = _ask(gid, monkeypatch)
    _settle()
    assert result.get('stage') == 'human', result
    status, cfg = _config(gid)
    assert status == 'paused'
    assert cfg.get('pause_cause') == 'needs_help'
    assert cfg['escalation'].get('ask') == _QUESTION
    assert 'expert' not in cfg['escalation'], cfg['escalation']
    notes = _notifications('10202', gid)
    assert [t for t, _g, _m in notes] == ['goal_needs_help']
    assert _QUESTION in notes[0][2], 'the owner reads the question in the app'
    assert 'no way to answer it from the app yet' in notes[0][2], (
        'the notice says what works today: no answer route until Part B3')
    assert len(pushes) == 1
    _user, title, body, _data = pushes[0]
    assert _QUESTION not in title and _QUESTION not in body, \
        'the question never rides the push'


def test_a_goal_owned_by_an_agent_tells_the_agents_person(pushes, monkeypatch):
    """person_for: an agent's question goes to the agent's owner; an agent
    with no owner (the desktop's agent_27130929014_0) to the node's owner."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    _person('learner-parent')
    owned = _goal(owner_id=_agent(owner_id='learner-parent'), title='Coach the learner')
    _ask(owned, monkeypatch)
    orphan = _goal(owner_id=_agent(), title='Watch the news')
    _ask(orphan, monkeypatch)
    _settle()
    assert [t for t, _g, _m in _notifications('learner-parent', owned)] == ['goal_needs_help']
    assert _notifications('10202', owned) == []
    assert [t for t, _g, _m in _notifications('10202', orphan)] == ['goal_needs_help']


def test_a_node_with_nobody_to_tell_parks_no_owner_and_says_so(pushes, monkeypatch,
                                                              caplog):
    """Central today: HEVOLVE_OWNER_USER_ID names an id with no users row
    there.  The park is never silent (hartos-c9's condition 3)."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202-absent')
    gid = _goal(created_by='system_bootstrap', title='Tutor the learner')
    with caplog.at_level(logging.WARNING):
        _ask(gid, monkeypatch)
    _settle()
    status, cfg = _config(gid)
    assert status == 'paused'
    assert cfg.get('pause_cause') == 'no_owner_to_ask'
    assert cfg['escalation'].get('ask') == _QUESTION
    assert 'told_at' not in cfg['escalation']
    assert _notifications('10202-absent', gid) == []
    assert [p for p in pushes if p[3].get('goal_id') == gid] == []
    assert [r for r in caplog.records
            if gid in r.getMessage() and 'no_owner_to_ask' in r.getMessage()
            and r.levelno == logging.WARNING], 'the no-owner park names the goal and cause'


def test_the_same_ask_again_tells_the_owner_once(pushes, monkeypatch):
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Tutor the learner again')
    for _ in range(3):
        assert _ask(gid, monkeypatch).get('stage') == 'human'
    _settle()
    assert [t for t, _g, _m in _notifications('10202', gid)] == ['goal_needs_help']
    assert [p[3].get('goal_id') for p in pushes if p[3].get('goal_id') == gid] == [gid]


def test_a_dashboard_resume_parks_again_and_tells_nobody_twice(pushes, monkeypatch):
    """hartos-c9's condition 4.  The goal is resumed from the dashboard while
    its step still waits; its next background turn hands the same question
    on again (REUSE's held-step gate, CREATE's ask for help).  The goal parks
    again, nobody is told twice, and it never ends done, skipped or retried."""
    from integrations.agent_engine.goal_manager import GoalManager
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Tutor after a resume')
    _ask(gid, monkeypatch)
    with db_session() as db:
        assert GoalManager.resume_goal(db, gid, resumed_by='10202').get('success')
    assert _config(gid)[0] == 'active'
    _ask(gid, monkeypatch)
    _settle()
    status, cfg = _config(gid)
    assert status == 'paused'
    assert cfg.get('pause_cause') == 'needs_help'
    assert [t for t, _g, _m in _notifications('10202', gid)] == ['goal_needs_help']
    assert status not in ('completed', 'archived', 'skipped', 'failed')


def test_a_stuck_action_that_asks_nothing_still_goes_to_the_expert_first(
        pushes, monkeypatch):
    """Unchanged for an action that could not be done (#106d): the expert
    gets it first and the goal stays active."""
    monkeypatch.setenv('HEVOLVE_OWNER_USER_ID', '10202')
    _person('10202')
    gid = _goal(created_by='system_bootstrap', title='Publish the post')
    result = _ask(gid, monkeypatch, question=None)
    assert result.get('stage') == 'expert', result
    status, cfg = _config(gid)
    assert status == 'active'
    assert cfg['escalation'].get('next') == 'expert'

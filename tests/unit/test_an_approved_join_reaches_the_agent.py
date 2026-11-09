"""An approved join reaches the agent on the joiner's next message, a join
that cannot be made yet keeps its approval, and an older run judged late never
moves the child back.

The review of 2eb963781 (REJECTED, hartos-16's independent reviewer, 11-07)
ran these shapes as probes against the commit; each test below is that shape
with the outcome the owner's design asks for:
  R1 (probe1, probe1c): the child's side approved; the joiner's next message
     in the same process went to the persona-selection group again (its 2 h
     journey still said 'Roles'), and that group's model could ask the
     child's side a second time.
  R2 (probe5): approved, but the child's chat was not found; the reason was
     dropped and the approval used up.
  R3 (probe3): two finished runs wait; the model fails once on the older one,
     the newer one is judged first and moves the child on, then the older one
     is judged met and moved the child BACK.

Real reuse_recipe, core.flow_progress and person_ask state handling; the
model, the role group's LLM and the session builders are stand-ins.

    python -m pytest tests/unit/test_an_approved_join_reaches_the_agent.py -q
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

CHILD, PARENT, GUARDIAN, P = 'u-child', 'u-parent', 'u-guardian', 77
PKEY = f'{PARENT}_{P}'
PERSONAS = ['Child', 'Parent', 'Therapist']
FLOWS = [
    {'flow_name': 'Name calling response', 'persona': 'Child', 'sub_goal': 'Arjun turns when called by name'},
    {'flow_name': 'Family photos', 'persona': 'Parent', 'sub_goal': 'the parent sent three family photos'},
    {'flow_name': 'Family member association', 'persona': 'Child', 'sub_goal': 'Arjun names Amma in a photo'},
    {'flow_name': 'Flash cards', 'persona': 'Child', 'sub_goal': 'Arjun names five flash cards'},
]
APPROVED = {'status': 'answered', 'answer': {'decision': 'approve'}}


class _Built(Exception):
    """The agent's own session is being built: the turn reached the agent."""


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users', 'user_agents',
                 'user_tasks', 'user_ledgers', 'recipes', 'time_actions', 'request_id_list',
                 'user_journey', 'role_agents', 'llm_call_track', 'user_delegation_bridges'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': n} for n in PERSONAS], 'flows': FLOWS}),
                      encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    # No consent store in this test process: a join lasts this process
    # (core.persona_registry.record_join's None), as on a node without one.
    monkeypatch.setattr(rr, 'record_join', lambda *_a: None)
    with Flask('approved-join').app_context():
        yield rr


class Asks:
    """person_ask as the child's side sees it: every card sent is recorded,
    and the state of the last one is whatever the test sets."""

    def __init__(self):
        self.sent, self.state = [], {'status': 'open'}

    def ask(self, user_id, agent_id, component, *, prompt_id=None, **_k):
        self.sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(self.sent)}', 'refused': None, 'card': True}

    def ask_state(self, ask_id, user_id):
        return dict(self.state)


@pytest.fixture
def asks(monkeypatch):
    import core.event_attribution as event_attribution
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(event_attribution, 'person_for',
                        lambda uid, *_a, **_k: GUARDIAN if str(uid) == CHILD else str(uid))
    a = Asks()
    monkeypatch.setattr(person_ask, 'ask', a.ask)
    monkeypatch.setattr(person_ask, 'ask_state', a.ask_state)
    return a


class _RoleGroup:
    """The persona-selection group the joiner's asking turn went to."""

    def __init__(self):
        self.messages, self.calls = [], []

    def initiate_chat(self, manager, message=None, **_k):
        self.calls.append(message)
        self.messages.append({'role': 'user', 'content': message})
        self.messages.append({'role': 'assistant', 'name': 'assistant',
                              'content': 'Which persona are you: Child, Parent or Therapist?'})


@pytest.fixture
def turn(rr, monkeypatch):
    """chat_agent with the run-boundary resets stubbed and the session
    builders recorded; building the agent's own session ends the turn."""
    monkeypatch.setattr(rr, 'clear_action_states', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_forget_reasks', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_settle_person_flow', lambda *a, **k: None)
    monkeypatch.setattr(rr.helper_fun, 'pause_requested', lambda: False)

    @contextlib.contextmanager
    def group_turn(*_a, **_k):
        yield True
    monkeypatch.setattr(rr.helper_fun, 'group_turn', group_turn)
    built = []
    monkeypatch.setattr(rr, 'create_schedule', lambda *a, **k: built.append('create_schedule'))

    def agents_for_user(*_a, **_k):
        built.append('create_agents_for_user')
        raise _Built()

    def agents_for_role(*_a, **_k):
        built.append('create_agents_for_role')
        raise _Built()
    monkeypatch.setattr(rr, 'create_agents_for_user', agents_for_user)
    monkeypatch.setattr(rr, 'create_agents_for_role', agents_for_role)

    def run(text, request_id='msg_5_parent'):
        try:
            return rr.chat_agent(PARENT, text, P, None, request_id)
        except _Built:
            return None
    run.built = built
    return run


def _child_chat(rr):
    rr.agents_session[f'{CHILD}_{P}'] = [{'user_id': CHILD, 'role': 'Child'}]


def _asked_from_the_persona_group(rr):
    """The joiner's turn that asked to join went to the persona group (that is
    where update_persona(new=False) runs), so their journey says 'Roles'."""
    group = _RoleGroup()
    rr.user_journey[PKEY] = 'Roles'
    rr.role_agents[PKEY] = (object(), group, group, object(), object(), False)
    return group


# -- R1: an approved join reaches the agent --------------------------------

def test_an_approved_joiner_is_answered_by_the_agent_not_the_persona_group(rr, asks, turn):
    from core.flow_progress import pending_join
    _child_chat(rr)
    group = _asked_from_the_persona_group(rr)
    assert rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD).startswith('I have asked')
    asks.state = APPROVED
    assert turn('They said yes') is None
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD, PARENT]
    assert rr.get_role(PARENT, P) == 'Parent' and pending_join(PARENT, P) is None
    assert group.calls == []                                   # never the persona group
    assert turn.built == ['create_schedule', 'create_agents_for_user']   # the agent, this turn
    assert rr.user_journey[PKEY] == 'UseBot' and PKEY not in rr.role_agents
    assert len(asks.sent) == 1                                 # the child's side asked once


def test_the_next_message_after_joining_asks_the_childs_side_nothing(rr, asks, turn):
    _child_chat(rr)
    _asked_from_the_persona_group(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = APPROVED
    turn('They said yes')
    turn('How is Arjun doing?', 'msg_6_parent')
    assert len(asks.sent) == 1
    assert turn.built.count('create_agents_for_role') == 0


# -- R2: a join that cannot be made yet keeps its approval -----------------

def test_an_approved_join_to_a_chat_not_started_says_so_and_keeps_the_approval(rr, asks, turn):
    from core.flow_progress import pending_join
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = APPROVED
    reply = turn('They said yes')
    assert 'start this chat first' in str(reply)
    assert turn.built == []                                    # no persona question either
    assert pending_join(PARENT, P)['ask_id'] == 'ask-1'        # the approval is kept
    assert not rr.chat_joinees.get(PARENT)
    _child_chat(rr)                                            # the child starts the chat
    assert turn('Started now', 'msg_6_parent') is None
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD, PARENT]
    assert pending_join(PARENT, P) is None
    assert turn.built == ['create_schedule', 'create_agents_for_user']
    assert len(asks.sent) == 1                                 # joined on the same approval


def test_an_approval_that_expired_before_the_chat_started_is_asked_again(rr, asks, turn):
    from core.flow_progress import pending_join
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = APPROVED
    turn('They said yes')                                      # chat not started: kept
    asks.state = {'status': 'unknown'}                         # the answer is gone (1 h, restart)
    _child_chat(rr)
    reply = turn('Started now', 'msg_6_parent')
    assert str(reply).startswith('I have asked the account holder of u-child')
    assert len(asks.sent) == 2 and pending_join(PARENT, P)['ask_id'] == 'ask-2'
    assert [m['user_id'] for m in rr.agents_session[f'{CHILD}_{P}']] == [CHILD]   # not joined


def test_a_join_that_could_not_be_saved_keeps_the_approval_and_tries_again(rr, asks, turn, monkeypatch):
    from core.flow_progress import pending_join
    _child_chat(rr)
    rr._reuse_ask_to_join(PARENT, P, 'Parent', CHILD)
    asks.state = APPROVED
    monkeypatch.setattr(rr, 'record_join', lambda *_a: False)
    reply = turn('They said yes')
    assert 'could not be saved' in str(reply) and 'try once more' in str(reply)
    assert pending_join(PARENT, P) is not None and not rr.chat_joinees.get(PARENT)
    monkeypatch.setattr(rr, 'record_join', lambda *_a: True)
    assert turn('Again', 'msg_6_parent') is None
    assert rr.get_role(PARENT, P) == 'Parent' and len(asks.sent) == 1


# -- R3: an older run judged late never moves the child back ---------------

@pytest.fixture
def fp(tmp_path, monkeypatch):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    import core.flow_progress as fp
    return fp


def _model(fail_flow0=False):
    def ask(content, schema, name):
        if name == 'flow_goal_checkable':
            return {'checkable': True}
        if name == 'flow_goal_met':
            if 'Arjun turns when called by name' in content:
                return None if fail_flow0 else {'met': True, 'evidence': 'turned'}
            return {'met': True, 'evidence': 'Amma'}
        return {'flow': ''}
    return ask


def test_an_older_run_judged_after_a_newer_one_does_not_move_the_child_back(fp):
    fp.record_flow_finished(CHILD, P, 0, 'Child', 'run-1', 'person answered at step 1: turned', 's')
    fp.record_flow_finished(CHILD, P, 2, 'Child', 'run-2', 'person answered at step 1: Amma', 's')
    assert fp.settle(CHILD, P, FLOWS, 'Child', _model(fail_flow0=True)) == 3
    assert fp.settle(CHILD, P, FLOWS, 'Child', _model()) == 3          # kept, not moved back
    assert fp.read(CHILD, P)['verdicts']['0'][-1]['met'] is True       # judged, and kept
    assert not fp.read(CHILD, P).get('unjudged')


def test_a_persons_move_after_the_run_finished_is_kept_when_it_is_judged(fp):
    fp.record_flow_finished(CHILD, P, 0, 'Child', 'run-1', 'person answered at step 1: turned', 's')
    time.sleep(0.02)
    fp.set_person_flow(CHILD, P, FLOWS, 'Child', 3, 'therapist-7')     # moved before judging
    assert fp.settle(CHILD, P, FLOWS, 'Child', _model()) == 3


def test_a_verdicts_move_made_while_a_run_was_going_on_does_not_stop_it(fp):
    """hartos-16's warm-session case: the verdict on run 1 moves the child on
    at the start of run 2; run 2's own verdict still moves them on."""
    fp.record_flow_finished(CHILD, P, 0, 'Child', 'run-1', 'person answered at step 1: turned', 's')
    fp.note_run_started(CHILD, P, 'Child', 2)
    time.sleep(0.02)
    assert fp.settle(CHILD, P, FLOWS, 'Child', _model()) == 2          # run 1 judged: on to 2
    time.sleep(0.02)
    fp.record_flow_finished(CHILD, P, 2, 'Child', 'run-2', 'person answered at step 1: Amma', 's')
    assert fp.settle(CHILD, P, FLOWS, 'Child', _model()) == 3          # run 2 judged: on to 3

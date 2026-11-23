"""What send_message_to_roles did before it answered from the asked agent's
notes, and still does.

Each test here passes on the tool as it was (it published to the multichat
topic) and on the tool as it is (core.persona_registry
._send_message_to_roles_impl answers from saved notes), so the change kept
them: who is refused, with which words, and who is reached.  "Reached" is
read the same way on both: the reply is not a failed call and names no
missing persona, and a person who is not reached has nothing published to
them and no model asked about them.

Not kept, by the review of c53452437: the child's agent reaching a joined
parent, and one joiner reaching another.  The join card covers only a joiner
asking about the person whose chat they joined, so those two are refused
(tests/unit/test_an_agent_asks_another_persons_agent.py pins the refusals).

    python -m pytest tests/unit/test_send_message_to_roles_keeps_its_refusals.py -q
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402,F401

from tests.unit.test_an_agent_asks_another_persons_agent import (  # noqa: E402,F401
    CHILD, P, PARENT, PERSONAS, STRANGER, THERAPIST, ask, childs_chat, env, revoke,
    saved, uses)


def reached(env, reply):
    """The call went through to that person: by a publish (as it was) or by
    an answer from the agent's notes about them (as it is)."""
    from core.constants import tool_reply_failed
    return (not tool_reply_failed(reply) and 'No persona' not in reply
            and bool(env.seen.published or env.seen.posts
                     or 'has no notes about' in reply))


def not_reached(env):
    return env.seen.published == [] and env.seen.posts == []


def test_an_empty_role_is_refused(env):
    uses((CHILD, 'Child'))
    childs_chat(env)
    assert 'role argument is empty' in ask(env, CHILD, '').lower()
    assert not_reached(env)


def test_a_person_with_no_chat_and_no_join_is_told_no_personas_are_registered(env, caplog):
    import logging
    uses((CHILD, 'Child'))
    childs_chat(env)
    with caplog.at_level(logging.WARNING, logger='core.persona_registry'):
        out = ask(env, STRANGER, 'Child')
    assert f'No personas registered for session {STRANGER}_{P}' in out
    assert 'gather-requirements first' in out
    assert any('no agent_session' in r.message for r in caplog.records)
    assert not_reached(env)


def test_a_role_nobody_has_is_named_in_the_refusal(env):
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    assert "No persona with role='Principal'" in ask(env, CHILD, 'Principal')
    assert not_reached(env)


def test_a_joiner_with_no_chat_of_their_own_reaches_the_chat_they_joined(env):
    """The joined-creator fallback."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    saved(env, CHILD, {'name_response': 'answered'})
    assert f'{PARENT}_{P}' not in env.sessions
    assert reached(env, ask(env, PARENT, 'Child'))


def test_a_revoked_join_stops_the_childs_agent_reaching_the_joiner(env):
    """Review of 31c54b71b, MF1: nothing takes the joiner out of the child's
    member list, so every call asks whether the member is still joined."""
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    saved(env, PARENT, {'family_members': ['Amma']})
    revoke(CHILD, PARENT)
    assert 'No persona' in ask(env, CHILD, 'Parent')
    assert not_reached(env)


def test_a_joiner_reaches_no_one_whose_join_to_the_same_chat_was_revoked(env):
    uses((CHILD, 'Child'), (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    childs_chat(env, (PARENT, 'Parent'), (THERAPIST, 'Therapist'))
    saved(env, CHILD, {'name_response': 'answered'})
    saved(env, THERAPIST, {'plan': 'two sessions a week'})
    revoke(CHILD, THERAPIST)
    assert 'No persona' in ask(env, PARENT, 'Therapist')
    assert not_reached(env)
    assert reached(env, ask(env, PARENT, 'Child'))


def test_on_a_node_without_a_consent_store_this_process_join_still_reaches(env, monkeypatch):
    def no_store(*_a, **_k):
        raise RuntimeError('no consent store on this node')
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    saved(env, CHILD, {'name_response': 'answered'})
    monkeypatch.setattr(env.pr, '_kept_joins', no_store)
    monkeypatch.setattr(env.pr, '_consent_store_kept_here', lambda: False)
    env.joins[PARENT] = {P: CHILD}
    assert reached(env, ask(env, PARENT, 'Child'))


def test_a_joiner_whose_join_cannot_be_read_is_not_reached(env, monkeypatch):
    class Unreadable:
        def get(self, *_a, **_k):
            raise RuntimeError('cache boom')

        def items(self):
            raise RuntimeError('cache boom')

    def no_store(*_a, **_k):
        raise RuntimeError('no consent store on this node')
    uses((CHILD, 'Child'), (PARENT, 'Parent'))
    childs_chat(env, (PARENT, 'Parent'))
    saved(env, PARENT, {'family_members': ['Amma']})
    monkeypatch.setattr(env.pr, '_kept_joins', no_store)
    monkeypatch.setattr(env.pr, '_consent_store_kept_here', lambda: False)
    monkeypatch.setattr(env.pr, 'chat_joinees', Unreadable())
    assert 'No persona' in ask(env, CHILD, 'Parent')
    assert not_reached(env)


def test_a_malformed_join_record_is_survived(env, monkeypatch):
    """This process's join map holding None for a person (an older writer)
    is no join and no crash."""
    def no_store(*_a, **_k):
        raise RuntimeError('no consent store on this node')
    uses((CHILD, 'Child'))
    childs_chat(env)
    monkeypatch.setattr(env.pr, '_kept_joins', no_store)
    monkeypatch.setattr(env.pr, '_consent_store_kept_here', lambda: False)
    env.joins[STRANGER] = None
    assert 'No personas registered' in ask(env, STRANGER, 'Child')
    assert not_reached(env)


def test_the_impl_keeps_its_arguments_and_the_topic_exports(env):
    import inspect
    assert list(inspect.signature(env.pr._send_message_to_roles_impl).parameters) == [
        'user_id', 'prompt_id', 'role', 'message', 'publish_fn']
    assert env.pr.multichat_topic_for('alice') == f'{env.pr.MULTICHAT_TOPIC_BASE}.alice'
    for name in ('MULTICHAT_TOPIC_BASE', 'multichat_topic_for', '_send_message_to_roles_impl'):
        assert name in env.pr.__all__

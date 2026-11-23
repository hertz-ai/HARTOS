"""A join the chat's owner already allowed is made again without a second ask.

Owner, 2026-11-21: a consent asked once is kept until the user turns it off.
The join's approval is kept as a chat_join grant (core.persona_registry
.record_join), and every reader of the join asks joined_chats.  The join
tool did not: update_persona(new=False) -> _reuse_join_by_tool looked only at
the join still waiting for an answer, so a person whose join stood (after a
restart, a lapsed session cache, or picking their persona again) had the
child's side asked a second time.

Owner, 2026-11-22: an approved join covers only the persona it was approved
for.  Coming back as the same persona is not asked again; as another persona
the chat's owner is asked again, by the same join card (which names the
joiner by name and user id, and the agent: owner 2026-11-10).  The persona an
approval was for is kept on the grant row itself, one row per persona
allowed (core.persona_registry.record_join, read back by joined_as).

Review of 96cb6cdeb, fixed here:
- a join from before the persona was kept read the joiner's OWN pick as the
  approval, and the pick changes with nobody's approval: three ways through
  with no ask.  Such a join covers no persona; its owner is asked once.
- only the latest approval was kept, and an unanswered ask for another
  persona locked the standing join out of every later turn.
- the approval sat in the joiner's own progress record, beside a grant that
  did not say the persona.  It is one record now, on the creator's account.

The real reuse_recipe join, core.flow_progress store (temp data dir) and
consent table (in memory); person_ask is a recorded fake.

    python -m pytest tests/unit/test_a_kept_join_is_not_asked_again.py -q
"""
from __future__ import annotations

import json
import os
import sys
import uuid

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

PARENT, P, OTHER_AGENT = 'u-parent-kept', 78, 79


class Asks:
    def __init__(self):
        self.sent = []

    def ask(self, user_id, agent_id, component, *, prompt_id=None, **_k):
        self.sent.append((user_id, component))
        return {'ok': True, 'ask_id': f'ask-{len(self.sent)}', 'refused': None, 'card': True}


@pytest.fixture
def rr(monkeypatch, tmp_path):
    import core.platform_paths as platform_paths
    monkeypatch.setattr(platform_paths, '_cached_data_dir', str(tmp_path))
    from hartos import reuse_recipe as rr
    for name in ('agents_session', 'agents_roles', 'chat_joinees', 'temp_users'):
        monkeypatch.setattr(rr, name, {}, raising=False)
    prompt = tmp_path / f'{P}.json'
    prompt.write_text(json.dumps({'personas': [{'name': 'Child'}, {'name': 'Parent'},
                                               {'name': 'Therapist'}], 'flows': []}),
                      encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    from integrations.social import consent_service
    monkeypatch.setattr(consent_service, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(consent_service, '_audit', lambda *a, **k: None)
    from integrations.social.models import Base, get_engine
    Base.metadata.create_all(get_engine())
    with Flask('kept-join-test').app_context():
        yield rr


@pytest.fixture
def asks(monkeypatch):
    from integrations.agent_engine import person_ask
    import core.event_attribution as event_attribution
    fake = Asks()
    monkeypatch.setattr(person_ask, 'ask', fake.ask)
    monkeypatch.setattr(event_attribution, 'person_for', lambda uid, *_a, **_k: str(uid))
    return fake


def _child(rr, started=True):
    """A child's account on this node, with (by default) their chat of agent
    P started."""
    from integrations.social.models import User, db_session
    uid = str(uuid.uuid4())
    with db_session() as db:
        db.add(User(id=uid, username=f'child_{uid[:8]}', display_name='Arjun',
                    user_type='human', api_token=f'tok-{uid}'))
    if started:
        rr.agents_session[f'{uid}_{P}'] = [
            {'agentInstanceID': f'x.{P}.{uid}', 'user_id': uid, 'role': 'Child',
             'deviceID': 'something'}]
    return uid


def _members(rr, child):
    return [m['user_id'] for m in rr.agents_session.get(f'{child}_{P}') or []]


def _end_the_join(child):
    """The child's side ends the join: the consent API's revoke of the kept
    grant."""
    from integrations.social.models import UserConsent, db_session
    from datetime import datetime
    with db_session() as db:
        for row in db.query(UserConsent).filter_by(user_id=child,
                                                   consent_type='chat_join').all():
            row.revoked_at = datetime.utcnow()


def _joined(rr, child, persona='Parent', agent=P):
    """The child's side allowed PARENT to join as ``persona`` and the join
    was made (what _reuse_pending_join_turn does on a yes); then the process
    forgot its sessions, as after a restart, keeping the child's own chat."""
    assert rr._reuse_join_chat(PARENT, agent, persona, child) == 'terminate'
    key = f'{child}_{agent}'
    rr.agents_session[key] = [m for m in rr.agents_session[key]
                              if m['user_id'] != PARENT]
    rr.agents_roles.pop(f'{PARENT}_{agent}', None)
    rr.chat_joinees.clear()


def _an_old_join(rr, child, persona='Parent'):
    """A join made before the approved persona was kept: the grant, and the
    pick the join itself wrote."""
    from core.flow_progress import set_persona
    assert rr.record_join(child, P, PARENT) is True
    set_persona(PARENT, P, persona, PERSONAS)
    assert _kept_as(child) is None


PERSONAS = ['Child', 'Parent', 'Therapist']


def _allowed_as(child, agent=P):
    """The personas ``child``'s side allowed PARENT to join as, read as the
    join tool reads them; a build that keeps none reads as none."""
    import core.persona_registry as pr
    reader = getattr(pr, 'joined_as', None)
    return list(reader(PARENT, agent, child)) if reader else []


def _kept_as(child):
    """The persona ``child``'s side allowed last, or None."""
    return (_allowed_as(child) or [None])[-1]


def test_a_join_that_stands_is_made_again_without_asking(rr, asks):
    child = _child(rr)
    _joined(rr, child)                                  # the child's side allowed it
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert asks.sent == [], (
        f'the child\'s side was asked again for a join it already allowed: {reply}')
    assert reply == 'terminate'
    assert PARENT in _members(rr, child)


def test_no_second_grant_row_and_no_waiting_join_are_written(rr, asks):
    from core.flow_progress import pending_join
    from integrations.social.models import UserConsent, db_session
    child = _child(rr)
    _joined(rr, child)
    rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    with db_session() as db:
        assert db.query(UserConsent).filter_by(
            user_id=child, consent_type='chat_join').count() == 1
    assert pending_join(PARENT, P) is None


# -- the approval is for one persona (owner 2026-11-22) ----------------------

def test_coming_back_as_another_persona_asks_the_childs_side_again(rr, asks):
    from core.flow_progress import pending_join
    child = _child(rr)
    _joined(rr, child, 'Parent')
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], (
        f'joined as a persona the child\'s side never allowed: {reply}')
    assert reply.startswith('I have asked the account holder of ' + child)
    assert 'as Therapist' in reply
    assert PARENT not in _members(rr, child), 'joined before the answer'
    assert f'{PARENT}_{P}' not in rr.agents_roles
    held = pending_join(PARENT, P)
    assert held and held['creator'] == child and held['persona'] == 'Therapist'


def test_the_second_ask_is_the_join_card_naming_who_which_agent_and_the_persona(rr, asks):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert len(asks.sent) == 1, f'asked {len(asks.sent)} times'
    (_who, card), = asks.sent
    assert card == rr._reuse_join_card(PARENT, P, 'Therapist')
    assert card['type'] == 'approval' and card['action'] == f'join_chat:{P}:{PARENT}'
    assert f'user {PARENT}' in card['title'].lower() or f'User {PARENT}' in card['title']
    assert f'agent {P}' in card['description'] and 'as Therapist' in card['description']


def test_once_the_other_persona_is_allowed_it_is_the_one_kept(rr, asks):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    # The child's side says yes: the join is made as Therapist.
    assert rr._reuse_join_chat(PARENT, P, 'Therapist', child) == 'terminate'
    assert _kept_as(child) == 'Therapist'
    assert rr.agents_roles[f'{PARENT}_{P}'][PARENT] == 'Therapist'
    del asks.sent[:]
    assert rr._reuse_join_by_tool(PARENT, P, 'Therapist', child) == 'terminate'
    assert asks.sent == [], 'the persona just allowed was asked for again'
    # Every persona the child's side allowed stays covered: going back to
    # the earlier one asks nobody.
    assert _allowed_as(child) == ['Parent', 'Therapist']
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == 'terminate'
    assert asks.sent == [], 'a persona already allowed was asked for again'
    # A third one was never allowed.
    rr._reuse_join_by_tool(PARENT, P, 'Child', child)
    assert [who for who, _card in asks.sent] == [child]


@pytest.mark.parametrize('said', ['parent', ' Parent ', 'PARENT'])
def test_the_same_persona_in_another_case_is_the_same_persona(rr, asks, said):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    assert rr._reuse_join_by_tool(PARENT, P, said, child) == 'terminate'
    assert asks.sent == []


@pytest.mark.parametrize('said', ['Par ent', 'Parent.', 'Parent (mom)', 'Parent Therapist'])
def test_a_name_that_only_looks_like_the_persona_is_not_covered(rr, asks, said):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    reply = rr._reuse_join_by_tool(PARENT, P, said, child)
    assert [who for who, _card in asks.sent] == [child], reply


def test_picking_another_persona_in_their_own_chat_is_not_an_approval(rr, asks):
    """update_persona(new=True) rewrites the joiner's own pick with nobody's
    approval; the join tool still asks for the persona nobody allowed.  (The
    role the joiner then acts under is get_role's, which reads that pick:
    not this test's, and not fixed here.)"""
    from core.flow_progress import set_persona
    child = _child(rr)
    _joined(rr, child, 'Parent')
    set_persona(PARENT, P, 'Therapist', ['Child', 'Parent', 'Therapist'])
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], reply
    del asks.sent[:]
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == 'terminate'
    assert asks.sent == []


def test_an_approval_is_kept_per_agent(rr, asks):
    """The same child's chats of two agents: what was allowed for one agent
    covers nothing for the other (owner 2026-11-10: per agent)."""
    child = _child(rr)
    rr.agents_session[f'{child}_{OTHER_AGENT}'] = [
        {'agentInstanceID': f'x.{OTHER_AGENT}.{child}', 'user_id': child,
         'role': 'Child', 'deviceID': 'something'}]
    _joined(rr, child, 'Parent')
    _joined(rr, child, 'Therapist', agent=OTHER_AGENT)
    assert _allowed_as(child) == ['Parent']
    assert _allowed_as(child, OTHER_AGENT) == ['Therapist']
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], reply


def test_an_approval_is_kept_per_chat_joined(rr, asks):
    first, second = _child(rr), _child(rr)
    _joined(rr, first, 'Parent')
    _joined(rr, second, 'Therapist')
    assert _kept_as(first) == 'Parent'
    assert _kept_as(second) == 'Therapist'
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', first) == 'terminate'
    assert rr._reuse_join_by_tool(PARENT, P, 'Therapist', second) == 'terminate'
    assert asks.sent == []
    rr._reuse_join_by_tool(PARENT, P, 'Parent', second)
    assert [who for who, _card in asks.sent] == [second]


# -- a join from before the persona was kept (review of 96cb6cdeb) ------------

def test_a_join_from_before_the_persona_was_kept_asks_once_and_is_then_kept(rr, asks):
    """Nothing on record says which persona that join was allowed for, so
    the chat's owner is asked, once: their yes is kept with its persona."""
    child = _child(rr)
    _an_old_join(rr, child, 'Parent')
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert [who for who, _card in asks.sent] == [child], (
        f'an old join was read as allowed for a persona nobody recorded: {reply}')
    assert PARENT not in _members(rr, child)
    # The child's side says yes.
    assert rr._reuse_join_chat(PARENT, P, 'Parent', child) == 'terminate'
    assert _allowed_as(child) == ['Parent']
    del asks.sent[:]
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == 'terminate'
    assert asks.sent == [], 'asked a second time after the yes'


def test_an_old_join_whose_pick_changed_does_not_let_the_new_pick_in(rr, asks):
    """Measured on 96cb6cdeb: an old Parent join, the joiner picks Therapist
    in their own chat (update_persona(new=True)) and joins as Therapist with
    no ask, and the self-pick was then recorded as approved."""
    from core.flow_progress import set_persona
    child = _child(rr)
    _an_old_join(rr, child, 'Parent')
    set_persona(PARENT, P, 'Therapist', PERSONAS)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], reply
    assert PARENT not in _members(rr, child)
    assert _allowed_as(child) == [], 'a pick nobody approved was recorded as approved'


def test_a_pick_written_in_the_same_turn_is_not_an_approval(rr, asks):
    """An old grant with no pick at all: the model writes the pick
    (update_persona(new=True)) and joins in the same turn."""
    from core.flow_progress import set_persona
    child = _child(rr)
    assert rr.record_join(child, P, PARENT) is True
    set_persona(PARENT, P, 'Therapist', PERSONAS)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], reply
    assert _allowed_as(child) == []


def test_two_old_joins_do_not_lend_each_other_a_persona(rr, asks):
    from core.flow_progress import set_persona
    first, second = _child(rr), _child(rr)
    assert rr.record_join(first, P, PARENT) is True
    set_persona(PARENT, P, 'Parent', PERSONAS)
    assert rr.record_join(second, P, PARENT) is True
    set_persona(PARENT, P, 'Therapist', PERSONAS)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', first)
    assert [who for who, _card in asks.sent] == [first], reply


def test_what_the_joiner_writes_in_their_own_record_is_not_an_approval(rr, asks):
    """The approval is the creator's grant row.  96cb6cdeb kept it in the
    joiner's own progress record; nothing reads that now."""
    from core.flow_progress import _update
    child = _child(rr)
    _joined(rr, child, 'Parent')
    _update(PARENT, P, lambda d: d.update(joined={child: 'Therapist'}))
    reply = rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    assert [who for who, _card in asks.sent] == [child], reply


def test_the_persona_is_on_the_grant_row_one_row_per_persona_allowed(rr, asks):
    from integrations.social.models import UserConsent, db_session
    child = _child(rr)
    _joined(rr, child, 'Parent')
    assert rr._reuse_join_chat(PARENT, P, 'Therapist', child) == 'terminate'
    assert rr._reuse_join_chat(PARENT, P, 'Therapist', child) == 'terminate'
    with db_session() as db:
        rows = db.query(UserConsent).filter_by(
            user_id=child, consent_type='chat_join').order_by(UserConsent.granted_at).all()
        assert [(r.label, r.agent_id, r.scope.split('#')[0], bool(r.granted))
                for r in rows] == [('Parent', str(P), PARENT, True),
                                   ('Therapist', str(P), PARENT, True)]


def test_ending_the_join_ends_every_persona_it_covered(rr, asks):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    assert rr._reuse_join_chat(PARENT, P, 'Therapist', child) == 'terminate'
    _end_the_join(child)
    assert _allowed_as(child) == []
    for persona in ('Parent', 'Therapist'):
        del asks.sent[:]
        from core.flow_progress import clear_pending_join
        clear_pending_join(PARENT, P)
        reply = rr._reuse_join_by_tool(PARENT, P, persona, child)
        assert [who for who, _card in asks.sent] == [child], reply


def test_an_agent_with_no_personas_keeps_a_plain_join_and_asks_no_one_again(
        rr, asks, tmp_path, monkeypatch):
    from integrations.social.models import UserConsent, db_session
    prompt = tmp_path / 'no_personas.json'
    prompt.write_text(json.dumps({'personas': [], 'flows': []}), encoding='utf-8')
    monkeypatch.setattr(rr.helper_fun, 'safe_prompt_path', lambda *_a, **_k: str(prompt))
    child = _child(rr)
    assert rr._reuse_join_chat(PARENT, P, 'anything the model said', child) == 'terminate'
    with db_session() as db:
        assert [r.label for r in db.query(UserConsent).filter_by(
            user_id=child, consent_type='chat_join').all()] == [None], (
            'raw text was kept as an approved persona')
    assert rr._reuse_join_by_tool(PARENT, P, 'something else', child) == 'terminate'
    assert asks.sent == []


# -- an open ask for another persona does not lock the standing join out ------

def _ask_state(monkeypatch, state):
    from integrations.agent_engine import person_ask
    monkeypatch.setattr(person_ask, 'ask_state', lambda *a, **k: state)


def _tells_a_later_turn(rr):
    import inspect
    return 'later_turn' in inspect.signature(rr._reuse_pending_join_turn).parameters


def _a_later_turn(rr):
    """The start of a turn after the one that asked."""
    assert _tells_a_later_turn(rr), 'a later turn is not told from the turn that asked'
    return rr._reuse_pending_join_turn(PARENT, P, later_turn=True)


def test_only_the_cold_start_of_a_turn_is_the_later_turn():
    """recipe() cannot be run here; its two join checks are read from source:
    the one before a session is built is the later turn, the one after the
    role group ran is the turn that asked."""
    from pathlib import Path
    src = (Path(ROOT) / 'hartos' / 'reuse_recipe.py').read_text(encoding='utf-8')
    later = '_join_reply = _reuse_pending_join_turn(user_id, prompt_id, later_turn=True)'
    asked = '_join_reply = _reuse_pending_join_turn(user_id, prompt_id)\n'
    assert src.count(later) == 1 and src.count(asked) == 1
    assert src.index(later) < src.index("# A person's persona is asked at their first use")
    assert src.index(later) < src.index(asked)
    assert src.index('# A join this chat asked for waits on the child') < src.index(asked)


def test_a_standing_join_goes_on_while_the_ask_for_another_persona_is_open(
        rr, asks, monkeypatch):
    from core.flow_progress import pending_join
    child = _child(rr)
    _joined(rr, child, 'Parent')
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    _ask_state(monkeypatch, {'status': 'open'})
    # The turn that asked says so.
    assert 'Still waiting' in rr._reuse_pending_join_turn(PARENT, P)
    # A later turn goes on, as the Parent they already are.
    reply = _a_later_turn(rr)
    assert reply is None, f'the standing join was locked out: {reply}'
    assert pending_join(PARENT, P), 'the open ask was forgotten'
    assert _allowed_as(child) == ['Parent']
    del asks.sent[:]
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == 'terminate'
    assert asks.sent == []


def test_a_first_join_still_waits_on_every_turn(rr, asks, monkeypatch):
    child = _child(rr)
    rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    _ask_state(monkeypatch, {'status': 'open'})
    replies = [rr._reuse_pending_join_turn(PARENT, P)]
    if _tells_a_later_turn(rr):
        replies.append(_a_later_turn(rr))
    for reply in replies:
        assert reply and 'Still waiting' in reply, (
            'someone who was never let in went on as a member')


def test_a_yes_to_the_other_persona_is_read_on_a_later_turn(rr, asks, monkeypatch):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    _ask_state(monkeypatch, {'status': 'answered', 'answer': {'decision': 'approve'}})
    assert _a_later_turn(rr) is None
    assert _allowed_as(child) == ['Parent', 'Therapist']
    assert rr.agents_roles[f'{PARENT}_{P}'][PARENT] == 'Therapist'


def test_a_no_to_the_other_persona_leaves_the_standing_join(rr, asks, monkeypatch):
    child = _child(rr)
    _joined(rr, child, 'Parent')
    rr._reuse_join_by_tool(PARENT, P, 'Therapist', child)
    _ask_state(monkeypatch, {'status': 'answered', 'answer': {'decision': 'deny'}})
    reply = _a_later_turn(rr)
    assert reply.endswith('did not allow you to join as Therapist.')
    assert _allowed_as(child) == ['Parent']
    del asks.sent[:]
    assert rr._reuse_join_by_tool(PARENT, P, 'Parent', child) == 'terminate'
    assert asks.sent == []


# -- a node that keeps no consent rows, and a store that does not answer ------

def test_a_node_with_no_consent_store_keeps_the_personas_for_this_process(monkeypatch):
    import core.persona_registry as pr

    def no_store(*_a, **_k):
        raise RuntimeError('no such table: user_consents')
    assert hasattr(pr, 'joined_as'), 'nothing reads which personas a join was allowed for'
    monkeypatch.setattr(pr, '_kept_joins', no_store)
    monkeypatch.setattr(pr, '_kept_join_personas', no_store)
    monkeypatch.setattr(pr, '_consent_store_kept_here', lambda: False)
    monkeypatch.setattr(pr, '_process_join_personas', {})
    assert pr.joined_as('j1', P, 'c1') == []
    assert pr.record_join('c1', P, 'j1', 'Parent') is None
    assert pr.record_join('c1', P, 'j1', 'parent') is None
    assert pr.record_join('c1', P, 'j1', 'Therapist') is None
    assert pr.record_join('c2', P, 'j1') is None
    assert pr.joined_as('j1', P, 'c1') == ['Parent', 'Therapist']
    assert pr.joined_as('j1', P, 'c2') == []
    assert pr.joined_as('j2', P, 'c1') == []


def test_a_store_that_does_not_answer_covers_no_persona(monkeypatch):
    import core.persona_registry as pr

    def locked(*_a, **_k):
        raise RuntimeError('database is locked')
    assert hasattr(pr, 'joined_as'), 'nothing reads which personas a join was allowed for'
    monkeypatch.setattr(pr, '_process_join_personas', {('j1', str(P), 'c1'): ['Parent']})
    monkeypatch.setattr(pr, '_kept_join_personas', locked)
    monkeypatch.setattr(pr, '_consent_store_kept_here', lambda: True)
    assert pr.joined_as('j1', P, 'c1') == [], (
        'what this process remembered stood in for a store that could have a revoke')


def test_a_grant_with_no_persona_on_record_asks(rr, asks):
    child = _child(rr)
    assert rr.record_join(child, P, PARENT) is True
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert [who for who, _card in asks.sent] == [child], reply
    assert PARENT not in _members(rr, child)


def test_a_kept_join_whose_chat_is_not_started_says_so_and_asks_no_one(rr, asks):
    child = _child(rr)
    _joined(rr, child)
    rr.agents_session.pop(f'{child}_{P}')
    from core.flow_progress import _update
    _update(child, P, lambda d: d.pop('persona', None))
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert 'to start this chat first' in reply
    assert asks.sent == []


# -- what must not change -----------------------------------------------------

def test_a_first_join_still_asks_the_childs_side(rr, asks):
    child = _child(rr)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]
    assert PARENT not in _members(rr, child)


def test_a_join_the_childs_side_ended_is_asked_for_again(rr, asks):
    child = _child(rr)
    _joined(rr, child)
    _end_the_join(child)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]
    assert PARENT not in _members(rr, child)


def test_a_join_kept_for_another_agent_opens_nothing_here(rr, asks):
    """The grant is per agent (owner 2026-11-10)."""
    child = _child(rr)
    rr.agents_session[f'{child}_{OTHER_AGENT}'] = [
        {'agentInstanceID': f'x.{OTHER_AGENT}.{child}', 'user_id': child,
         'role': 'Child', 'deviceID': 'something'}]
    _joined(rr, child, agent=OTHER_AGENT)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', child)
    assert reply.startswith('I have asked the account holder of ' + child)
    assert [who for who, _card in asks.sent] == [child]


def test_a_join_kept_with_someone_else_does_not_cover_this_child(rr, asks):
    first, second = _child(rr), _child(rr)
    _joined(rr, first)
    reply = rr._reuse_join_by_tool(PARENT, P, 'Parent', second)
    assert reply.startswith('I have asked the account holder of ' + second)
    assert [who for who, _card in asks.sent] == [second]

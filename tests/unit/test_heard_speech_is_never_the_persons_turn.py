"""Words the always-on mic heard that were not said to the agent stay on
record (the whole meeting, owner 2026-11-24/25), but they are never the
person's turn.  They carry their own role, and every reader of the
conversation table that feeds an agent or a device skips them unless it asks
for heard speech; the open-mic judge reads them under their own label.

Review of 4e58dddcb (hartos-17, rv_mic5): record_heard wrote the words as
role 'user' on channel open_mic_heard, so "my PIN is 4471, do not tell the
kid" came back from the agent's get_chat_history tool
(hartos.helper.get_time_based_history) as something the person said, and the
next judge call listed it like a real turn.  pull_since (the cross-device
feed) and the unified inbox (/api/social/channels/conversations) read the
same table.

Every reader here runs on a real conversation table (in-memory SQLite)
holding one turn said to the agent and its reply, one overheard line written
by record_heard, one overheard line written the way 4e58dddcb wrote them
(role 'user' on the heard channel: such rows are on the installed build's
disk), and one turn of another agent of the same person.
"""
import json
from types import SimpleNamespace
from unittest import mock

import pytest

USER = 'u-heard-1'
AGENT = '95568353503'
OTHER_AGENT = '1794031134533'
SAID = 'Can we play the ball game?'
REPLY = 'Yes! Say ball.'
OVERHEARD = 'my PIN is 4471, do not tell the kid'
OVERHEARD_BEFORE = 'the parent said the session ends at four'
OTHER_AGENTS_TURN = 'Tell me a story about a fox'
HEARD_CHANNEL = 'open_mic_heard'


@pytest.fixture
def table(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from integrations.audio import addressee
    from integrations.social import chat_messages, models

    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    models.ConversationEntry.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(chat_messages, '_models',
                        lambda: (Session, models.ConversationEntry))
    monkeypatch.setattr(models, 'get_db', Session)
    # The table's insert listener puts every row on the audit trail; the
    # lines it would queue are kept here, nothing is written.
    audit = mock.Mock()
    monkeypatch.setattr('security.audit_trail.record', audit)

    chat_messages.persist(USER, 'user', SAID, prompt_id=AGENT, request_id='r1')
    chat_messages.persist(USER, 'assistant', REPLY, prompt_id=AGENT, request_id='r1')
    heard_row = addressee.record_heard(USER, OVERHEARD, prompt_id=AGENT,
                                       request_id='r2')
    db = Session()
    try:
        db.add(models.ConversationEntry(
            user_id=USER, role='user', channel_type=HEARD_CHANNEL,
            content=OVERHEARD_BEFORE, prompt_id=AGENT, msg_id='legacy-heard-1'))
        db.commit()
    finally:
        db.close()
    chat_messages.persist(USER, 'user', OTHER_AGENTS_TURN, prompt_id=OTHER_AGENT,
                          request_id='r3')
    return SimpleNamespace(Session=Session, audit=audit, heard_row=heard_row,
                           models=models)


def _contents(rows):
    return [r['content'] for r in rows]


def test_record_heard_keeps_the_words_under_their_own_role(table):
    """Kept word for word (the meeting is captured), as what the mic heard,
    never as what the person said to the agent."""
    row = table.heard_row
    assert row is not None, 'the overheard words were not written'
    assert row['content'] == OVERHEARD
    assert row['channel_type'] == HEARD_CHANNEL
    assert row['role'] == 'heard', (
        f"overheard words were stored as role {row['role']!r}: every reader "
        f"that does not know the channel takes them for the person's turn")


def test_the_agents_history_tool_never_reads_overheard_words(table):
    """hartos.helper.get_time_based_history serves the agent's
    get_chat_history tool and the FULL_HISTORY tool."""
    from hartos.helper import get_time_based_history
    out = json.loads(get_time_based_history(
        '', f'user_{USER}', '2000-01-01T00:00:00', '2100-01-01T00:00:00'))
    said = [r['message']['content'] for r in out['res_in_filter']]
    assert SAID in said and REPLY in said
    assert OVERHEARD not in said, (
        'the agent read words it overheard as the person speaking to it')
    assert OVERHEARD_BEFORE not in said, (
        'an overheard line written before the heard role reached the agent')


def test_the_cross_device_feed_skips_overheard_words_unless_asked(table):
    from integrations.social.chat_messages import pull_since
    feed = _contents(pull_since(USER, 0))
    assert SAID in feed and REPLY in feed
    assert OVERHEARD not in feed and OVERHEARD_BEFORE not in feed, (
        "another device's chat showed overheard words as the person's turn")
    asked = _contents(pull_since(USER, 0, channel_type=HEARD_CHANNEL))
    assert asked == [OVERHEARD, OVERHEARD_BEFORE], (
        'a reader that asks for heard speech must still get it')


def _inbox(table, monkeypatch, query=''):
    from flask import Flask

    from integrations.social import api_channels, auth

    monkeypatch.setattr(auth, '_get_user_from_token',
                        lambda _token: (SimpleNamespace(id=USER), table.Session()))
    monkeypatch.setattr('integrations.social.feature_flags.get_flags_for_tenant',
                        lambda *_a, **_k: {}, raising=False)
    monkeypatch.delenv('HEVOLVE_CLOUD_MODE', raising=False)
    app = Flask('inbox_under_test')
    app.register_blueprint(api_channels.channel_user_bp)
    resp = app.test_client().get('/api/social/channels/conversations' + query,
                                 headers={'Authorization': 'Bearer t'})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()


def test_the_unified_inbox_skips_overheard_words_unless_asked(table, monkeypatch):
    body = _inbox(table, monkeypatch)
    shown = _contents(body['data'])
    assert SAID in shown and REPLY in shown
    assert OVERHEARD not in shown and OVERHEARD_BEFORE not in shown, (
        "the unified inbox listed overheard words as the person's turn")
    assert body['pagination']['total'] == len(shown)
    asked = _inbox(table, monkeypatch, f'?channel_type={HEARD_CHANNEL}')
    assert sorted(_contents(asked['data'])) == sorted([OVERHEARD, OVERHEARD_BEFORE])


def _judged_context(user, prompt_id, words):
    """The context the open-mic judge gets when its own readers read the
    table (no open ask, no voice window)."""
    from integrations.audio import addressee
    judge = mock.Mock(return_value=(True, 'reply'))
    with mock.patch.object(addressee, '_open_ask_for', return_value=None), \
            mock.patch.object(addressee, '_voice_hint', return_value=''):
        addressee.decide_open_mic_turn(user, prompt_id, words, judge=judge)
    return judge.call_args[0][1]


def test_the_judge_reads_said_to_you_and_heard_in_the_room_apart(table):
    """The judge gets the overheard lines, labelled as heard in the room,
    and the turns said to the agent, labelled as said to it."""
    from integrations.audio import addressee
    context = _judged_context(USER, AGENT, 'baw')
    turns = context['recent_turns']
    assert ('user', SAID) in turns and ('assistant', REPLY) in turns
    assert ('heard', OVERHEARD) in turns and ('heard', OVERHEARD_BEFORE) in turns, (
        f'overheard words reached the judge as the person speaking: {turns}')
    with mock.patch('core.llm_json.ask_model_json',
                    return_value={'for_the_agent': True, 'reason': 'r'}) as ask:
        addressee._ask_the_turns_model('baw', context)
    lines = ask.call_args[0][0].splitlines()
    said_to_you = [ln for ln in lines if 'said to you:' in ln]
    heard_in_room = [ln for ln in lines if 'heard in the room:' in ln]
    assert any(SAID in ln for ln in said_to_you)
    assert any(OVERHEARD in ln for ln in heard_in_room)
    assert any(OVERHEARD_BEFORE in ln for ln in heard_in_room)
    assert not any(OVERHEARD in ln or OVERHEARD_BEFORE in ln for ln in said_to_you)


def test_the_judge_reads_only_the_turns_agents_conversation(table):
    """Another agent's turns with the same person are not this agent's
    context (review of bb839586d: the recent turns were the person's, of
    every agent)."""
    context = _judged_context(USER, AGENT, 'baw')
    assert OTHER_AGENTS_TURN not in [line for _, line in context['recent_turns']], (
        "another agent's turn was given to the judge as this agent's chat")
    other = _judged_context(USER, OTHER_AGENT, 'and then?')
    assert [line for _, line in other['recent_turns']] == [OTHER_AGENTS_TURN]


def test_overheard_room_talk_never_pushes_the_agents_own_line_out(table):
    """The agent's last line is read from its turns, so a burst of room talk
    after it (a parent and a therapist talking) leaves it in the judge's
    context: the child's one-word answer to it is judged with it."""
    from integrations.audio import addressee
    for i in range(addressee.RECENT_TURNS + 2):
        addressee.record_heard(USER, f'room talk {i}', prompt_id=AGENT)
    context = _judged_context(USER, AGENT, 'baw')
    assert context['last_agent_line'] == REPLY, (
        "room talk pushed the agent's own last line out of the judge's context")


def test_the_audit_trail_keeps_heard_speech_as_a_persons_words_coming_in(table):
    """The audit line of a heard row stays inbound from a person, on the
    heard channel: the meeting record names who spoke."""
    lines = [c.kwargs for c in table.audit.call_args_list
             if c.kwargs.get('text') == OVERHEARD]
    assert len(lines) == 1, table.audit.call_args_list
    assert lines[0]['direction'] == 'in'
    assert lines[0]['actor'] == 'person'
    assert lines[0]['channel'] == HEARD_CHANNEL

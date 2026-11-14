"""A REUSE agent says its greeting once a session, not again at the start of
a later message.

MEASURED LIVE 2026-11-13 on Nunba build 75, local Qwen3.5-4B, test account
learner1.  The REUSE prompt gives the greeting as the opening ("- Opening:
...", core.agent_personality.build_personality_prompt, from spoken_greeting),
and the model opened later messages with it:

    18:59:16 / 19:01:04  Personalised Learning Tutor (agent 54), its 3rd and
        4th replies: "Hey there! I'm Personalised Learning Tutor, and I'm
        genuinely excited to work with you on this.\\n\\nI see you're ready to
        start learning! ..."
    18:50:07 / 18:52:00  Singing Agent Rhyme and Sing Along (1793373034536),
        its 2nd and 3rd turns' sends, and the 3rd turn's reply (the held
        step's question, the send's own text): "Hey there! I'm Singing Agent
        Rhyme and Sing Along, and I'm genuinely excited ...\\n\\nHi Arjun! ..."

The fix does not ask the model again: REUSE records the greeting its prompt
carries (_reuse_remember_greeting, from spoken_greeting, where the session's
agents are built), and once the agent has said something to the person in
the session, a send (send_message_to_user1) or a reply (chat_agent) that
opens with that greeting goes out without it.

These tests drive the REAL chat_agent (its run-boundary steps and the group
turn stubbed, get_agent_response returning the live reply) and the REAL
send_message_to_user1 on the desktop branch (the publisher captured).

    python -m pytest tests/unit/test_the_greeting_is_said_once.py -q --noconftest
"""
import contextlib
import inspect
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))

from flask import Flask  # noqa: E402

LEARNER = 'f4f3ba0a-591e-4971-80bb-e87ae519940a'
TUTOR_ID, TUTOR_NAME = '54', 'Personalised Learning Tutor'
SING_ID, SING_NAME = '1793373034536', 'Singing Agent Rhyme and Sing Along'


@pytest.fixture
def rr(monkeypatch):
    pytest.importorskip('autogen', reason='autogen not installed')
    import hartos.reuse_recipe as mod
    if hasattr(mod, '_reuse_greeting'):
        monkeypatch.setattr(mod, '_reuse_greeting', {}, raising=True)
    monkeypatch.setattr(mod, 'request_id_list_sent_intermediate', {},
                        raising=True)
    with Flask('greeting-once-test').app_context():
        yield mod


@pytest.fixture
def publisher(monkeypatch):
    """The desktop branch: the hart_intelligence publish_async captured."""
    monkeypatch.setenv('NUNBA_BUNDLED', '1')
    pub = MagicMock(name='publish_async')
    monkeypatch.setitem(sys.modules, 'hart_intelligence',
                        SimpleNamespace(publish_async=pub))
    return pub


def _named(agent_id, display):
    """The agent's personality as REUSE names it for the turn: saved with a
    persona name, spoken with the name the person gave it."""
    from core import agent_personality as ap
    saved = ap.AgentPersonality(
        agent_name=f'skill.local.{agent_id}', role='tutor', persona_name='Aria',
        primary_traits=[], tone='warm-casual',
        greeting_style=ap._GREETING_STYLES['warm-casual'].format(name='Aria'))
    return ap.with_spoken_name(saved, display, agent_id=agent_id)


def _greeting(agent_id, display):
    from core.agent_personality import spoken_greeting
    return spoken_greeting(_named(agent_id, display))


def _session_built(rr, agent_id, display):
    """What create_agents_for_user does for the greeting when it builds the
    session's agents.  HEAD has no such step, so there it records nothing."""
    remember = getattr(rr, '_reuse_remember_greeting', None)
    if remember is not None:
        remember(f'{LEARNER}_{agent_id}', _named(agent_id, display))


def _chat_agent_says(rr, monkeypatch, agent_id, reply):
    """chat_agent on a warm session, the group turn answering ``reply``."""
    session = f'{LEARNER}_{agent_id}'
    for name, value in (('clear_action_states', None),
                        ('_reuse_forget_reasks', None),
                        ('_reuse_settle_person_flow', None),
                        ('_reuse_held_step_gate', None),
                        ('_start_reuse_action', None),
                        ('get_role', None)):
        monkeypatch.setattr(rr, name, lambda *a, _v=value, **k: _v, raising=True)
    monkeypatch.setattr(rr, 'get_action_state',
                        lambda *a, **k: rr.ActionState.IN_PROGRESS, raising=True)
    monkeypatch.setattr(rr, 'request_id_list', {}, raising=True)
    monkeypatch.setattr(rr, 'user_agents', {session: tuple(range(12))},
                        raising=True)
    monkeypatch.setattr(rr, 'user_journey', {session: 'UseBot'}, raising=True)
    monkeypatch.setitem(rr.user_tasks, session,
                        SimpleNamespace(current_action=1, actions=[{}]))
    monkeypatch.setattr(rr, 'llm_call_track',
                        {session: {'count': 3, 'original_prompt': False}},
                        raising=True)

    @contextlib.contextmanager
    def _turn(*a, **k):
        yield True
    monkeypatch.setattr(rr.helper_fun, 'group_turn', _turn, raising=True)
    monkeypatch.setattr(rr, 'get_agent_response', lambda *a, **k: reply,
                        raising=True)
    return rr.chat_agent(LEARNER, 'hello', agent_id, None, 'msg_test_sim')


def _send(rr, agent_id, text):
    with patch.object(rr, 'scheduler') as sched:
        sched.get_job.return_value = None
        return rr.send_message_to_user1(LEARNER, text, '', agent_id)


def _published_texts(pub):
    out = []
    for call in pub.call_args_list:
        payload = call[0][1]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        text = payload.get('text')
        out.append(text[0] if isinstance(text, list) else text)
    return out


class TestTheGreetingIsSaidOnce:

    def test_the_live_greeting_is_the_one_source(self):
        """The text the live replies opened with is spoken_greeting's."""
        assert _greeting(TUTOR_ID, TUTOR_NAME) == (
            "Hey there! I'm Personalised Learning Tutor, and I'm genuinely "
            "excited to work with you on this.")

    def test_a_later_reply_does_not_open_with_the_greeting(
            self, rr, monkeypatch):
        """Tutor, 18:59:16 and 19:01:04.  RED before the fix: the reply
        opened with the greeting again."""
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _session_built(rr, TUTOR_ID, TUTOR_NAME)
        first = (greeting + "\n\nSawubona! I can hear how fractions are "
                 "challenging you right now.")
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID, first) == first, (
            'the first reply of the session keeps its greeting')
        second = ("Ah, I see what happened! I was trying to access a book that "
                  "hasn't been uploaded yet.")
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID, second) == second
        body = ("I see you're ready to start learning! Right now, I haven't "
                "seen any books in your library yet.")
        third = _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                 greeting + "\n\n" + body)
        assert third == body, 'a later reply opened with the greeting again'
        fourth = _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                  greeting + " " + body)
        assert fourth == body

    def test_a_later_send_does_not_open_with_the_greeting(
            self, rr, monkeypatch, publisher):
        """Singing agent, 18:50:07 and 18:52:00: the 2nd and 3rd turns'
        sends.  RED before the fix."""
        greeting = _greeting(SING_ID, SING_NAME)
        _session_built(rr, SING_ID, SING_NAME)
        rhyme = ("Hi Arjun! \U0001f31f Let's sing together!\n\nHere's a fun "
                 "rhyme for you:\n\nThe red ball, bouncing high,")
        twinkle = ("Let's sing \"Twinkle, Twinkle, Little Star\"! \U0001f31f"
                   "\n\nTwinkle, twinkle, little star,\nHow I wonder what you "
                   "are!")
        assert _send(rr, SING_ID, rhyme).startswith('Message sent successfully')
        assert _send(rr, SING_ID, greeting + "\n\n" + twinkle).startswith(
            'Message sent successfully')
        assert _published_texts(publisher) == [rhyme, twinkle], (
            "the 2nd turn's send opened with the greeting again")

    def test_the_held_reply_and_its_send_say_it_once(
            self, rr, monkeypatch, publisher):
        """Singing agent, 18:52:00-18:52:05: the send opened with the
        greeting and the held step's reply is the send's own text (read from
        the call's arguments), so both carried it.  RED before the fix."""
        greeting = _greeting(SING_ID, SING_NAME)
        _session_built(rr, SING_ID, SING_NAME)
        _chat_agent_says(rr, monkeypatch, SING_ID, 'Shall we sing a rhyme?')
        ask = ("Hi Arjun! \U0001f31f Let's sing together!\n\nWould you like to "
               "sing a fun rhyme with me?")
        _send(rr, SING_ID, greeting + "\n\n" + ask)
        assert _published_texts(publisher) == [ask]
        assert _chat_agent_says(rr, monkeypatch, SING_ID,
                                greeting + "\n\n" + ask) == ask


class TestWhatTheGreetingKeeps:

    def test_the_first_message_of_a_session_keeps_it(
            self, rr, monkeypatch, publisher):
        greeting = _greeting(SING_ID, SING_NAME)
        _session_built(rr, SING_ID, SING_NAME)
        _send(rr, SING_ID, greeting + "\n\nHi Arjun!")
        assert _published_texts(publisher) == [greeting + "\n\nHi Arjun!"]

    def test_a_message_that_is_only_the_greeting_stays(self, rr, monkeypatch):
        """Never emptied: an empty reply is a failed turn (#797)."""
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _session_built(rr, TUTOR_ID, TUTOR_NAME)
        _chat_agent_says(rr, monkeypatch, TUTOR_ID, 'Hello again.')
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                greeting + "\n\n") == greeting + "\n\n"

    def test_only_a_whole_greeting_comes_off_never_the_start_of_a_word(
            self, rr, monkeypatch):
        """Nit (b) of the review of 0da57d374: a greeting 'Hi' turned
        'History is fun today.' into 'story is fun today.'.  The greeting
        comes off only where it ends a word."""
        rr._reuse_greeting[f'{LEARNER}_{TUTOR_ID}'] = {'greeting': 'Hi',
                                                       'said': True}
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                'History is fun today.') == (
            'History is fun today.'), 'the greeting cut a word in half'
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                'Hi Arjun, history is fun today.') == (
            'Arjun, history is fun today.'), (
            'anti-vacuity: a whole greeting still comes off')
        # Tamil: the greeting ends on a virama, a combining mark, and the
        # word goes on after it.
        rr._reuse_greeting[f'{LEARNER}_{TUTOR_ID}'] = {'greeting': 'வணக்கம்',
                                                       'said': True}
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                'வணக்கம்மா, இன்று பாடலாம்.') == (
            'வணக்கம்மா, இன்று பாடலாம்.')
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                'வணக்கம் அர்ஜுன்!') == 'அர்ஜுன்!'
        # The live greeting ends on '.', so a word right after it is a new
        # one and the greeting still comes off.
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        rr._reuse_greeting[f'{LEARNER}_{TUTOR_ID}'] = {'greeting': greeting,
                                                       'said': True}
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                greeting + 'Ready?') == 'Ready?'

    def test_a_later_reply_opening_on_a_new_line_drops_it_too(
            self, rr, monkeypatch):
        """The model often opens with a blank line; the greeting after it
        is still the opening."""
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _session_built(rr, TUTOR_ID, TUTOR_NAME)
        _chat_agent_says(rr, monkeypatch, TUTOR_ID, 'Hello again.')
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                '\n' + greeting + '\n\nOn we go.') == 'On we go.'

    def test_the_greeting_inside_a_reply_is_left_alone(self, rr, monkeypatch):
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _session_built(rr, TUTOR_ID, TUTOR_NAME)
        _chat_agent_says(rr, monkeypatch, TUTOR_ID, 'Hello again.')
        quoted = f'You asked how I open a lesson: "{greeting}"'
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID, quoted) == quoted

    def test_an_agent_with_no_recorded_greeting_is_unchanged(
            self, rr, monkeypatch):
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _chat_agent_says(rr, monkeypatch, TUTOR_ID, 'Hello again.')
        reply = greeting + "\n\nOn we go."
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID, reply) == reply

    def test_a_send_that_reached_nobody_does_not_count(self, rr, monkeypatch):
        """No publisher: the send failed, so the agent has not spoken yet and
        the next message keeps its greeting."""
        monkeypatch.setenv('NUNBA_BUNDLED', '1')
        monkeypatch.delitem(sys.modules, 'hart_intelligence', raising=False)
        monkeypatch.delitem(sys.modules, 'hart_intelligence_entry', raising=False)
        greeting = _greeting(SING_ID, SING_NAME)
        _session_built(rr, SING_ID, SING_NAME)
        assert _send(rr, SING_ID, 'Hi Arjun!').startswith('Failed to send')
        reply = greeting + "\n\nHi Arjun!"
        assert _chat_agent_says(rr, monkeypatch, SING_ID, reply) == reply

    def test_a_send_that_reached_the_backend_counts_on_central(
            self, rr, monkeypatch):
        """Central posts the send to the backend; a 2xx is a delivery."""
        monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
        monkeypatch.delattr(sys, 'frozen', raising=False)
        monkeypatch.setenv('AUTOGEN_RESPONSE_URL', 'http://backend.test/r')
        greeting = _greeting(SING_ID, SING_NAME)
        _session_built(rr, SING_ID, SING_NAME)
        post = MagicMock(return_value=SimpleNamespace(status_code=200, text=''))
        with patch('core.http_pool.pooled_post_once', post):
            assert _send(rr, SING_ID, 'Hi Arjun!').startswith(
                'Message sent successfully')
        assert _chat_agent_says(rr, monkeypatch, SING_ID,
                                greeting + "\n\nReady?") == 'Ready?'

    def test_the_recorded_greeting_is_the_prompts_opening(self, rr):
        from core.agent_personality import build_personality_prompt
        named = _named(TUTOR_ID, TUTOR_NAME)
        rr._reuse_remember_greeting(f'{LEARNER}_{TUTOR_ID}', named)
        recorded = rr._reuse_greeting[f'{LEARNER}_{TUTOR_ID}']['greeting']
        assert recorded == _greeting(TUTOR_ID, TUTOR_NAME)
        assert ('- Opening: ' + recorded) in build_personality_prompt(
            named, execution_mode=True)

    def test_a_rebuild_of_the_session_keeps_that_it_was_said(
            self, rr, monkeypatch):
        greeting = _greeting(TUTOR_ID, TUTOR_NAME)
        _session_built(rr, TUTOR_ID, TUTOR_NAME)
        _chat_agent_says(rr, monkeypatch, TUTOR_ID, 'Hello again.')
        _session_built(rr, TUTOR_ID, TUTOR_NAME)       # agents rebuilt
        assert _chat_agent_says(rr, monkeypatch, TUTOR_ID,
                                greeting + "\n\nOn we go.") == 'On we go.'

    def test_the_session_build_records_the_prompts_greeting(self, rr):
        """create_agents_for_user records the greeting right after naming the
        personality the prompt is built from (too large to drive here)."""
        src = inspect.getsource(rr.create_agents_for_user)
        named_at = src.index('_saved_personality = with_spoken_name(')
        record_at = src.index(
            '_reuse_remember_greeting(user_prompt, _saved_personality)')
        prompt_at = src.index('_personality_block = build_personality_prompt(')
        assert named_at < record_at < prompt_at

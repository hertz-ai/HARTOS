"""An open-mic turn (a final the chat page's always-on mic heard, unprompted)
is judged by the turn's own model, every time, with the context as facts:
no word count, time window or length rule decides (owner 2026-11-25: "it shd
be intelligent enough to demarcate rather than stats based").  A turn judged
not for the agent is still captured in the chat history as heard, with no
agent reply, and the page gets the agent's feedback (owner 2026-11-25: "the
agent shd decide and give a feedback to ui even if sent, so that entire
meeting is caotured").  A model that cannot judge never drops the words.

Signals and the model are passed in or mocked, so nothing here reads a
database, a model or a mic.
"""
import pathlib
from unittest import mock

import pytest

from integrations.audio import addressee
from integrations.audio.addressee import decide_open_mic_turn, record_heard

NOW = 1_000_000.0

# A presenter talking to the audience with the mic on: long, and not for the
# agent.  A length rule answered speech like this (the 11-03 failure).
PRESENTER = 'So here you can see the agent asking the child to say ball'


def _judge(answer):
    return mock.Mock(return_value=answer)


def test_one_word_with_an_open_ask_is_judged_by_the_model_and_its_yes_answers():
    judge = _judge((True, 'answers the drill question'))
    respond, signals, reason = decide_open_mic_turn(
        'u1', 5, 'baw', open_ask='Say ball.', recent_turns=[],
        judge=judge, now=NOW)
    assert respond is True
    assert reason == 'answers the drill question'
    assert signals == 'ask_open=1,last_agent_s=none,turns=0,judge=yes'
    judge.assert_called_once()
    assert judge.call_args[0][0] == 'baw'


def test_a_presenter_with_no_ask_is_judged_by_the_model_and_its_no_is_silent():
    judge = _judge((False, 'a presenter talking to the audience'))
    respond, signals, reason = decide_open_mic_turn(
        'u1', 5, PRESENTER, open_ask=None, recent_turns=[],
        judge=judge, now=NOW)
    assert respond is False
    assert reason == 'a presenter talking to the audience'
    assert signals == 'ask_open=0,last_agent_s=none,turns=0,judge=no'
    judge.assert_called_once()


def test_the_model_gets_the_open_question_the_last_line_its_age_and_the_turns():
    judge = _judge((True, 'reply'))
    turns = [('assistant', 'Here is the story.', NOW - 12), ('user', 'ok', NOW - 5)]
    decide_open_mic_turn('u1', 5, 'the red one', open_ask='Which colour is the ball?',
                         recent_turns=turns, judge=judge, now=NOW)
    words, context = judge.call_args[0]
    assert words == 'the red one'
    assert context['open_question'] == 'Which colour is the ball?'
    assert context['last_agent_line'] == 'Here is the story.'
    assert context['last_agent_line_age_s'] == 12
    assert context['recent_turns'] == [('assistant', 'Here is the story.'), ('user', 'ok')]


def test_the_question_names_room_talk_and_carries_the_context():
    """The default judge is core.llm_json.ask_model_json (the node's own
    model, through pooled_post and the llama priority scheduler)."""
    context = {'open_question': 'Which colour is the ball?',
               'last_agent_line': 'Here is the story.', 'last_agent_line_age_s': 12,
               'recent_turns': [('assistant', 'Here is the story.')]}
    with mock.patch('core.llm_json.ask_model_json',
                    return_value={'for_the_agent': False,
                                  'reason': 'people talking to each other'}) as ask:
        verdict = addressee._ask_the_turns_model('what about that one', context)
    assert verdict == (False, 'people talking to each other')
    content = ask.call_args[0][0]
    for part in ('what about that one', 'Which colour is the ball?',
                 'Here is the story.', '12 seconds ago', 'presenter',
                 'people talking to each other', addressee.STAY_SILENT_LINE):
        assert part in content
    assert ask.call_args[0][1]['required'] == ['for_the_agent', 'reason']


def test_a_model_that_fails_answers_the_turn():
    judge = mock.Mock(side_effect=RuntimeError('model down'))
    respond, signals, _ = decide_open_mic_turn(
        'u1', 5, PRESENTER, open_ask=None, recent_turns=[], judge=judge, now=NOW)
    assert respond is True
    assert signals.endswith('judge=unknown')


def test_a_model_with_no_answer_answers_the_turn():
    respond, signals, _ = decide_open_mic_turn(
        'u1', 5, 'hmm okay', open_ask=None, recent_turns=[],
        judge=_judge(None), now=NOW)
    assert respond is True
    assert signals.endswith('judge=unknown')


@pytest.mark.parametrize('verdict', [True, False])
@pytest.mark.parametrize('text', ['ok', 'yes please', PRESENTER])
@pytest.mark.parametrize('open_ask', [None, 'Say ball.'])
@pytest.mark.parametrize('agent_spoke_s_ago', [None, 5, 3600])
def test_no_number_decides_the_model_does(verdict, text, open_ask, agent_spoke_s_ago):
    """However short or long the words, with or without an open ask, and
    however recently the agent spoke, the outcome is the model's verdict."""
    turns = ([] if agent_spoke_s_ago is None
             else [('assistant', 'Say ball.', NOW - agent_spoke_s_ago)])
    judge = _judge((verdict, 'why'))
    respond, _, _ = decide_open_mic_turn('u1', 5, text, open_ask=open_ask,
                                         recent_turns=turns, judge=judge, now=NOW)
    assert respond is verdict
    judge.assert_called_once()


def test_no_threshold_is_left_in_the_module():
    for gone in ('ENGAGED_WINDOW_S', 'CHATTER_MAX_WORDS', 'REQUEST_MIN_WORDS'):
        assert not hasattr(addressee, gone)


def test_a_signal_lookup_that_fails_counts_as_no_evidence():
    """Defaults read the open ask and the recent turns; a failing reader is
    no evidence, not a crash (and not a drop: the model is asked)."""
    judge = _judge((True, 'reply'))
    with mock.patch.object(addressee, '_open_ask_for',
                           side_effect=RuntimeError('boom')), \
            mock.patch.object(addressee, '_recent_turns',
                              side_effect=RuntimeError('boom')):
        respond, signals, _ = decide_open_mic_turn(
            'u1', 5, 'what about that one', judge=judge, now=NOW)
    assert respond is True
    assert signals.startswith('ask_open=0,last_agent_s=none,turns=0,')
    judge.assert_called_once()


def test_not_addressed_speech_is_captured_as_heard_and_no_agent_reply_is_written():
    with mock.patch('integrations.social.chat_messages.persist',
                    return_value={'id': 1}) as persist:
        row = record_heard('u1', PRESENTER, prompt_id=5, request_id='r1')
    assert row == {'id': 1}
    persist.assert_called_once_with('u1', 'user', PRESENTER, prompt_id='5',
                                    request_id='r1',
                                    channel_type=addressee.HEARD_CHANNEL)
    assert all(c.args[1] != 'assistant' for c in persist.call_args_list)


def _chat_hook():
    src = pathlib.Path(__file__).resolve().parents[2].joinpath(
        'hart_intelligence_entry.py').read_text(encoding='utf-8')
    start = src.index("@app.route('/chat', methods=['POST'])")
    body = src[start:start + 40000]
    hook = body.index("if data.get('open_mic'):")
    end = body.index('create_agent = data.get(', hook)
    return body, hook, body[hook:end]


def test_the_chat_route_captures_and_answers_with_feedback_when_not_addressed():
    """Not addressed: the words are recorded as heard, and the reply is the
    agent's feedback (addressed false + reason), with no text to speak."""
    body, hook, block = _chat_hook()
    assert 'decide_open_mic_turn(' in block
    assert block.index('record_heard(') < block.index('return jsonify(')
    for part in ("'addressed': False", "'not_addressed': True",
                 "'reason': _om_reason", "'text': ''"):
        assert part in block
    # Judged before the turn stamps user activity or dispatches.
    assert hook < body.index('USER PRIORITY')


def test_an_addressed_turn_runs_as_a_normal_turn():
    """The hook's only return is the not-addressed one; an addressed turn
    falls through to the normal turn."""
    _, _, block = _chat_hook()
    assert block.count('return ') == 1
    assert 'if not _om_respond:' in block
    assert '_chat_reply(' not in block

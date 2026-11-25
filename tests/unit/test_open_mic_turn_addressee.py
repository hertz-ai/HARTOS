"""An open-mic turn (a final the chat page's always-on mic heard, unprompted)
is answered when the cheap signals say it was meant for the agent, stays
silent when they say chatter, and asks the turn's own model only when they
do not settle it (owner 2026-11-24/25: mic on by default; no wake word; no
single modality vetoes; no keyword or regex gate).

Signals are passed in, so nothing here reads a database, a model or a mic.
"""
from unittest import mock

from integrations.audio import addressee
from integrations.audio.addressee import ENGAGED_WINDOW_S, decide_open_mic_turn

NOW = 1_000_000.0


def _judge(answer=True):
    return mock.Mock(return_value=answer)


def test_an_open_ask_means_respond_without_asking_the_model():
    judge = _judge(False)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'blue', open_ask='Which colour do you like?',
        last_agent_turn_at=None, judge=judge, now=NOW)
    assert respond is True
    assert 'ask_open=1' in signals
    judge.assert_not_called()


def test_a_recent_agent_turn_means_respond_without_asking_the_model():
    judge = _judge(False)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'ok', open_ask=None,
        last_agent_turn_at=(NOW - 20, 'Shall we start the drill?'),
        judge=judge, now=NOW)
    assert respond is True
    assert 'engaged=1' in signals
    judge.assert_not_called()


def test_an_old_agent_turn_is_not_engagement():
    judge = _judge(False)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'ok then', open_ask=None,
        last_agent_turn_at=(NOW - ENGAGED_WINDOW_S - 5, 'Bye'),
        judge=judge, now=NOW)
    assert respond is False
    assert 'engaged=0' in signals
    judge.assert_not_called()


def test_a_short_final_with_no_context_is_silent_and_the_model_is_not_asked():
    judge = _judge(True)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'yeah sure', open_ask=None, last_agent_turn_at=None,
        judge=judge, now=NOW)
    assert respond is False
    assert signals == 'ask_open=0,engaged=0,words=2,judge=none'
    judge.assert_not_called()


def test_long_speech_is_answered_without_asking_the_model():
    judge = _judge(False)
    respond, _ = decide_open_mic_turn(
        'u1', 5, 'can you tell me what the weather is like today please',
        open_ask=None, last_agent_turn_at=None, judge=judge, now=NOW)
    assert respond is True
    judge.assert_not_called()


def test_unsure_asks_the_model_once_and_its_no_keeps_silent():
    judge = _judge(False)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'what about that one', open_ask=None,
        last_agent_turn_at=(NOW - 600, 'Here is the story.'),
        judge=judge, now=NOW)
    assert respond is False
    assert signals.endswith('judge=no')
    judge.assert_called_once()
    words, open_question, last_line = judge.call_args[0]
    assert words == 'what about that one'
    assert open_question is None
    assert last_line == 'Here is the story.'


def test_unsure_asks_the_model_once_and_its_yes_answers():
    judge = _judge(True)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'what about that one', open_ask=None,
        last_agent_turn_at=None, judge=judge, now=NOW)
    assert respond is True
    assert signals.endswith('judge=yes')
    judge.assert_called_once()


def test_a_model_that_fails_never_drops_the_persons_words():
    judge = mock.Mock(side_effect=RuntimeError('model down'))
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'what about that one', open_ask=None,
        last_agent_turn_at=None, judge=judge, now=NOW)
    assert respond is True
    assert signals.endswith('judge=unknown')


def test_a_model_with_no_answer_never_drops_the_persons_words():
    judge = _judge(None)
    respond, signals = decide_open_mic_turn(
        'u1', 5, 'what about that one', open_ask=None,
        last_agent_turn_at=None, judge=judge, now=NOW)
    assert respond is True
    assert signals.endswith('judge=unknown')


def test_a_signal_lookup_that_fails_counts_as_no_evidence():
    """Defaults read the open ask and the last agent turn; a failing reader
    is no evidence, not a crash (and not a drop: the model is asked)."""
    judge = _judge(True)
    with mock.patch.object(addressee, '_open_ask_for',
                           side_effect=RuntimeError('boom')), \
            mock.patch.object(addressee, '_last_agent_turn',
                              side_effect=RuntimeError('boom')):
        respond, signals = decide_open_mic_turn(
            'u1', 5, 'what about that one', judge=judge, now=NOW)
    assert respond is True
    assert signals.startswith('ask_open=0,engaged=0,')
    judge.assert_called_once()


def test_the_model_is_asked_through_the_nodes_one_json_question():
    """The default judge is core.llm_json.ask_model_json (the node's own
    model, one stateless question), carrying the words and the context."""
    with mock.patch('core.llm_json.ask_model_json',
                    return_value={'for_the_agent': False}) as ask:
        verdict = addressee._ask_the_turns_model(
            'what about that one', 'Which colour?', 'Here is the story.')
    assert verdict is False
    content = ask.call_args[0][0]
    assert 'what about that one' in content
    assert 'Which colour?' in content
    assert 'Here is the story.' in content
    assert addressee.STAY_SILENT_LINE in content


def test_the_chat_route_judges_an_open_mic_turn_before_anything_else():
    """The /chat handler calls the one decision for an open_mic body, and
    returns the silent reply shape the page reads."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2].joinpath(
        'hart_intelligence_entry.py').read_text(encoding='utf-8')
    start = src.index("@app.route('/chat', methods=['POST'])")
    body = src[start:start + 40000]
    hook = body.index("data.get('open_mic')")
    assert 'decide_open_mic_turn' in body[hook:hook + 2000]
    assert "'not_addressed': True" in body[hook:hook + 2000]
    # Judged before the turn stamps user activity or dispatches.
    assert hook < body.index('USER PRIORITY')

"""An open-mic turn (a final the chat page's always-on mic heard, unprompted)
is judged by the turn's own model, every time, with the context as facts:
no word count, time window or length rule decides (owner 2026-11-25: "it shd
be intelligent enough to demarcate rather than stats based").  A turn judged
not for the agent is still captured in the chat history as heard, with no
agent reply, and the page gets the agent's feedback (owner 2026-11-25: "the
agent shd decide and give a feedback to ui even if sent, so that entire
meeting is caotured").  A model that cannot judge never drops the words.

Signals and the model are passed in or mocked, so nothing here reads a
database, a model or a mic.  The /chat side is driven through the real route
in test_the_open_mic_judge_runs_as_the_persons_turn.py, and the readers of
the heard rows in test_heard_speech_is_never_the_persons_turn.py.
"""
import json
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


@pytest.fixture(autouse=True)
def prompts(tmp_path, monkeypatch):
    """The voice window is RAM the mic stream fills; empty here unless a
    test passes one.  The agent's config and saved personality are read from
    an empty prompts folder unless a test writes one there."""
    import core.cache_loaders as cache_loaders
    import core.platform_paths as platform_paths
    monkeypatch.setattr(cache_loaders, 'PROMPTS_DIR', str(tmp_path))
    monkeypatch.setattr(platform_paths, 'get_prompts_dir', lambda: str(tmp_path))
    with mock.patch.object(addressee, '_voice_hint', return_value=''):
        yield tmp_path


def _agent(folder, pid, name, persona_name=None):
    """prompts/<pid>.json as CREATE saves it, and the saved personality."""
    (folder / f'{pid}.json').write_text(json.dumps(
        {'name': name, 'prompt_id': pid, 'goal': 'speech drills for a child'}))
    if persona_name is not None:
        (folder / f'{pid}_personality.json').write_text(json.dumps(
            {'agent_name': f'skill.local.agent_{pid}', 'role': 'speech coach',
             'persona_name': persona_name}))


# Owner 2026-11-25: "Nunba is like the name for the AI so the wake word shd
# still be there but intelligently while understanding context in a multi
# user environment".  The name goes to the model as a strong cue, not a gate.

def test_called_by_name_the_model_is_asked_with_the_name_and_its_yes_answers():
    judge = _judge((True, 'called by name with a request'))
    respond, _, reason = decide_open_mic_turn(
        'u1', 5, "Nunba, what's the weather", open_ask=None, recent_turns=[],
        judge=judge, now=NOW)
    assert respond is True
    assert reason == 'called by name with a request'
    judge.assert_called_once()
    words, context = judge.call_args[0]
    assert words == "Nunba, what's the weather"
    assert context['assistant_name'] == 'Nunba'


def test_talk_about_nunba_to_someone_else_is_silent_but_captured():
    judge = _judge((False, 'talking about the assistant to someone else'))
    said = 'I told Nunba yesterday about it'
    respond, signals, reason = decide_open_mic_turn(
        'u1', 5, said, open_ask=None, recent_turns=[], judge=judge, now=NOW)
    assert respond is False
    assert signals == 'ask_open=0,last_agent_s=none,turns=0,judge=no'
    assert reason == 'talking about the assistant to someone else'
    with mock.patch('integrations.social.chat_messages.persist',
                    return_value={'id': 2}) as persist:
        record_heard('u1', said, prompt_id=5, request_id='r2')
    persist.assert_called_once_with('u1', 'heard', said, prompt_id='5',
                                    request_id='r2',
                                    channel_type=addressee.HEARD_CHANNEL)


def test_the_question_carries_the_name_and_the_voice_hint():
    context = {'assistant_name': 'Nunba', 'voice_hint': 'voice A: "say ball"',
               'open_question': None, 'last_agent_line': None,
               'last_agent_line_age_s': None, 'recent_turns': []}
    with mock.patch('core.llm_json.ask_model_json',
                    return_value={'for_the_agent': True, 'reason': 'r'}) as ask:
        addressee._ask_the_turns_model('Nunba, what is the weather', context)
    content = ask.call_args[0][0]
    assert 'Your name is Nunba' in content
    assert 'talk about you, not to you' in content
    assert 'voice A: "say ball"' in content


# Review of bb839586d (hartos-17, rv_mic5): the judge was told "Your name is
# Nunba" for every agent while the agent's own last line said "Hi, I'm
# Arjun!", and prompt_id was never used.  The judge is told the name the
# turn's agent says it has (core.agent_personality.spoken_name, read the way
# its prompt reads it); Nunba stays a name it answers to, the app's (owner
# 2026-11-25: "Nunba is like the name for the AI so the wake word shd still
# be there").

def test_the_judge_is_told_the_name_the_turns_agent_says_it_has(prompts):
    _agent(prompts, '343', 'Arjun')
    judge = _judge((True, 'called by its name'))
    decide_open_mic_turn('u1', '343', 'Arjun, I said ball!', open_ask=None,
                         recent_turns=[], judge=judge, now=NOW)
    context = judge.call_args[0][1]
    assert context['assistant_name'] == 'Arjun', (
        f"the judge was told the agent is {context['assistant_name']!r}")
    with mock.patch('core.llm_json.ask_model_json',
                    return_value={'for_the_agent': True, 'reason': 'r'}) as ask:
        addressee._ask_the_turns_model('Arjun, I said ball!', context)
    content = ask.call_args[0][0]
    assert 'Your name is Arjun' in content
    assert 'Your name is Nunba' not in content
    assert '"Nunba, ..."' in content, 'Nunba is still a name it answers to'


def test_an_agent_without_a_given_name_is_judged_by_the_name_it_speaks(prompts):
    """427 of 1236 desktop configs are named "Agent <id>": such an agent
    says its saved persona name (spoken_name), so that is its name here."""
    _agent(prompts, '95568353503', 'Agent 95568353503', persona_name='Meera')
    judge = _judge((True, 'r'))
    decide_open_mic_turn('u1', '95568353503', 'Meera, again', open_ask=None,
                         recent_turns=[], judge=judge, now=NOW)
    assert judge.call_args[0][1]['assistant_name'] == 'Meera'


def test_a_turn_with_no_agent_of_its_own_is_judged_as_nunba():
    judge = _judge((True, 'r'))
    decide_open_mic_turn('u1', None, 'Nunba, the weather', open_ask=None,
                         recent_turns=[], judge=judge, now=NOW)
    assert judge.call_args[0][1]['assistant_name'] == 'Nunba'


def test_the_open_question_is_the_turns_agents_own_with_its_words():
    """The open ask read for the judge is this agent's own ask of the
    person's turns (person_ask.open_ask_ids), never another agent's or
    background work's, and its words are the card's title and description
    (the card keeps the reason under 'description', not 'body')."""
    from integrations.agent_engine import person_ask
    card = {'title': 'Agent Arjun asks you', 'description': 'Say ball.'}
    judge = _judge((True, 'answers the drill'))
    with mock.patch.object(person_ask, 'open_ask_ids',
                           return_value=['older', 'newest']) as ids, \
            mock.patch.object(person_ask, 'read_ask',
                              return_value=(card, 200)) as read:
        decide_open_mic_turn('u1', '343', 'baw', recent_turns=[], judge=judge,
                             now=NOW)
    assert judge.call_args[0][1]['open_question'] == 'Agent Arjun asks you: Say ball.'
    ids.assert_called_once_with('u1', '343')
    read.assert_called_once_with('newest', 'u1')


def test_an_ask_that_is_no_longer_open_is_no_open_question():
    from integrations.agent_engine import person_ask
    judge = _judge((True, 'r'))
    with mock.patch.object(person_ask, 'open_ask_ids', return_value=['gone']), \
            mock.patch.object(person_ask, 'read_ask',
                              return_value=({'reason': 'gone'}, 404)):
        decide_open_mic_turn('u1', '343', 'baw', recent_turns=[], judge=judge,
                             now=NOW)
    assert judge.call_args[0][1]['open_question'] is None


def test_the_voice_window_is_passed_as_a_hint_when_the_turn_has_one():
    judge = _judge((True, 'reply'))
    with mock.patch.object(addressee, '_voice_hint',
                           return_value='voice B: "the red one"'):
        decide_open_mic_turn('u1', 5, 'the red one', open_ask=None,
                             recent_turns=[], judge=judge, now=NOW)
    assert judge.call_args[0][1]['voice_hint'] == 'voice B: "the red one"'


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
    persist.assert_called_once_with('u1', 'heard', PRESENTER, prompt_id='5',
                                    request_id='r1',
                                    channel_type=addressee.HEARD_CHANNEL)
    assert all(c.args[1] != 'assistant' for c in persist.call_args_list)

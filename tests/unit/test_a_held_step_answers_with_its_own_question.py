"""A held step answers its person with its own question, never "Step N is waiting".

LIVE (2026-11-26, installed build #5, speech agent 95568353503): the build
answered its creator 'Step 5 is waiting for your reply' while a step waited
for her, and 'I need your input to finish building this agent. Step 5
("send_message_to_user: Get every flow's saved results, known_words and
syllable_progress from memory.") isn't coming together ...' (16:16:34 and
16:22:47 IST).  REUSE's turn with no request id answered "Step N is waiting
for your reply." the same way (reuse_recipe.py, the #46 branch).

The ONE hold-reply rule (hevolve-66e6 with hartos-17, 2026-11-26; owner: no
machinery text reaches a person), read once in
lifecycle_hooks.held_step_question and answered by held_step_reply at both
sites: the open ask record's 'asked', else the question part of its
'question'; else the newest question the step's window still holds open, a
send that asked or a card with no answer (its own title and description);
with none, the plain line HELD_STEP_FALLBACK_LINE, and no model is asked:
the node's model's line in the person's language failed hartos-17's held-out
run on the 4B (rv_c13, 2026-11-26: English in 8 of 8 child cases whose Tamil
came from the store).  Never empty, no step number, no tool name.  The
creator line keeps its step number (she reviewed the numbered plan) but not
the plan's tool syntax.  The hold itself does not change.

What this person saved under the names a step's words use
(step_saved_values: whole identifiers, top-level keys, bounded; one line,
step_saved_values_line) is read for REUSE's step message (#295 F5).  The
speech agent's child, parent and therapist share one store, so a parent's
step that names no saved key shows nothing of the child's.

    python -m pytest tests/unit/test_a_held_step_answers_with_its_own_question.py -q -p no:cacheprovider --noconftest
"""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('autogen', reason='autogen not installed')

from tests.unit.test_completion_needs_real_work import _call, _result  # noqa: E402

STEP = ("send_message_to_user: Get every flow's saved results, known_words and "
        "syllable_progress from memory.")
DRAFT = ("Here is this week's plan: three short sessions with paal and poo. "
         "Shall I start with Monday?")
LINE_TA = 'உங்கள் பதிலுக்காக நான் காத்திருக்கிறேன்.'


# ── tests/conftest.py's fixtures, here so the file runs under --noconftest ──

@pytest.fixture
def test_user_id():
    return 123


@pytest.fixture
def test_prompt_id():
    return 456


@pytest.fixture
def mock_flask_app():
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def sample_config_json(tmp_path, test_prompt_id, monkeypatch):
    prompts = tmp_path / 'prompts'
    prompts.mkdir()
    import hartos.create_recipe as cr
    import hartos.helper as h
    for mod in (cr, h):
        if hasattr(mod, 'PROMPTS_DIR'):
            monkeypatch.setattr(mod, 'PROMPTS_DIR', str(prompts))
    monkeypatch.chdir(tmp_path)
    config = {'personas': [{'name': 'Parent'}],
              'flows': [{'persona': 'Parent', 'actions': [STEP]}]}
    (prompts / f'{test_prompt_id}.json').write_text(json.dumps(config),
                                                     encoding='utf-8')
    return config


def _window(*rounds, action_id=1):
    """The step's window: its dispatch, then each round's messages."""
    log = [{'role': 'user', 'name': 'ChatInstructor',
            'content': f'Execute Action {action_id}: {STEP} ,Latest User message: '}]
    for messages in rounds:
        log += messages
    return log


def _send(call_id, text, expects_reply=None):
    args = {'text': text}
    if expects_reply is not None:
        args['expects_reply'] = expects_reply
    return [_call(call_id, 'send_message_to_user', json.dumps(args)),
            _result(call_id, f'Message sent successfully to user with request_id: '
                             f'msg_{call_id}-intermediate')]


def _card(call_id, ask_id, status, title="Weekly review",
          description="Which day suits you for the weekly review?", answer=None):
    component = {'type': 'form', 'title': title, 'description': description,
                 'fields': [{'name': 'day', 'label': 'Day', 'type': 'text'}]}
    result = {'answered': status == 'answered', 'ask_id': ask_id, 'status': status}
    if status == 'answered':
        result['answer'] = answer
    return [_call(call_id, 'ask_person', json.dumps(
                {'component': json.dumps(component), 'wait_seconds': 60})),
            _result(call_id, json.dumps(result))]


@pytest.fixture
def hooks(monkeypatch):
    """lifecycle_hooks with the step's ask record and window set per test,
    and the node's model recorded instead of called, ready with a line
    (``line``), so a test shows the held step never asks it."""
    import hartos.lifecycle_hooks as lh
    import core.llm_json
    state = SimpleNamespace(record=None, messages=[], asked=[], line=None,
                            stores={'456': {}, '123_456': {}})
    monkeypatch.setattr(lh, 'ledger_open_ask', lambda up, aid, flow: state.record)
    monkeypatch.setattr(lh, 'recorded_answer_requests', lambda up, aid, *a, **k: [])
    monkeypatch.setattr(lh, 'get_registered_groupchat',
                        lambda up: SimpleNamespace(messages=state.messages))

    def model(content, schema, name, **kw):
        state.asked.append((content, name, kw))
        return {'line': state.line} if state.line is not None else None
    monkeypatch.setattr(core.llm_json, 'ask_model_json', model)
    # The saved stores, by store id (the agent's: '456'; this person's:
    # '123_456'; another person's: '999_456'), as the loader get_data_by_key
    # uses reads them; a store not given here reads as the real loader does.
    import hartos.helper
    real_load = hartos.helper.load_agent_data_from_file

    def load(store_id, agent_data):
        if str(store_id) in state.stores:
            agent_data[store_id] = state.stores[str(store_id)]
            return True
        return real_load(store_id, agent_data)
    monkeypatch.setattr(hartos.helper, 'load_agent_data_from_file', load)
    state.lh, state.real_load = lh, real_load
    return state


UP = '123_456'


# ── The chain: one reader ──

def test_the_records_asked_is_the_question(hooks):
    hooks.record = {'asked': 'Shall I start with Monday?', 'question': DRAFT}
    hooks.messages = _window(_send('c1', 'something else entirely?'))
    assert hooks.lh.held_step_question(UP, 1, 0) == 'Shall I start with Monday?'


def test_an_older_record_gives_the_question_part_of_what_it_asked(hooks):
    hooks.record = {'asked': None, 'question': DRAFT}
    assert hooks.lh.held_step_question(UP, 1, 0) == 'Shall I start with Monday?'


def test_with_no_question_on_record_the_windows_open_send_is_the_question(hooks):
    hooks.record = {'asked': None, 'question': None}
    hooks.messages = _window(_send('c1', DRAFT))
    assert hooks.lh.held_step_question(UP, 1, 0) == 'Shall I start with Monday?'


def test_a_card_after_the_send_is_the_question_in_its_own_words(hooks):
    hooks.messages = _window(_send('c1', DRAFT), _card('c2', 'A1', 'waiting_for_person'))
    assert hooks.lh.held_step_question(UP, 1, 0) == (
        'Weekly review: Which day suits you for the weekly review?')


def test_an_answered_card_leaves_no_question_open(hooks):
    hooks.messages = _window(_send('c1', DRAFT),
                             _card('c2', 'A1', 'answered', answer={'day': 'Monday'}))
    assert hooks.lh.held_step_question(UP, 1, 0) == ''


def test_a_card_that_never_reached_the_person_is_no_question(hooks):
    refused = [_call('c2', 'ask_person', json.dumps({'component': json.dumps(
                   {'type': 'form', 'description': 'Which day?'})})),
               _result('c2', json.dumps({'answered': False, 'refused': 'no_agent',
                                         'status': 'refused'}))]
    hooks.messages = _window(refused)
    assert hooks.lh.held_step_question(UP, 1, 0) == ''


# ── With no question anywhere: the plain line, and no model is asked ──

STEP_CHILD = ("send_message_to_user: Ask the child to say amma in home_language "
              "and praise child_name.")
STEP_PARENT = "send_message_to_user: Ask the parent in English about the child's day."


def test_no_question_answers_with_the_plain_line_and_asks_no_model(hooks):
    """hartos-17's ruling on the held-out run of 2026-11-26 (rv_c13): the
    model's line in the person's language failed on the 4B, so with no
    question the held step answers with the floor, even with the child's
    language saved and a model ready with a Tamil line."""
    hooks.stores['123_456'] = {'home_language': 'Tamil', 'child_name': 'Zoey'}
    hooks.line = LINE_TA
    reply = hooks.lh.held_step_reply(UP, 1, 0)
    assert reply == hooks.lh.HELD_STEP_FALLBACK_LINE and reply.strip()
    assert 'Step' not in reply and 'send_message' not in reply, reply
    assert hooks.asked == [], 'the model was asked for the held step line'


# ── What a step's words name of this person's saved values (#295 F5) ──

def test_the_saved_line_shows_only_what_the_steps_words_name(hooks):
    hooks.stores['123_456'] = {'home_language': 'Tamil', 'child_name': 'Zoey',
                               'known_words': ['paal', 'poo']}
    line = hooks.lh.step_saved_values_line(UP, STEP_CHILD)
    assert line == ('Saved for this person: home_language: "Tamil"; '
                    'child_name: "Zoey".'), line


def test_one_store_the_childs_step_carries_its_language_the_parents_does_not(hooks):
    """hartos-17's ruling, sub N's case: the speech agent's child, parent and
    therapist run as one account and share one store, home_language Tamil
    after #295's repair.  The parent's step, written in English and naming no
    saved key, is not shown Tamil."""
    hooks.stores['123_456'] = {'home_language': 'Tamil'}
    assert 'Tamil' in hooks.lh.step_saved_values_line(UP, STEP_CHILD)
    assert hooks.lh.step_saved_values_line(UP, STEP_PARENT) == ''


def test_a_key_named_language_is_read_the_same_way(hooks):
    """No key is named in the code: 'language' is read as 'home_language'
    is, where the step's words name it as a whole identifier."""
    hooks.stores['123_456'] = {'language': 'Hindi'}
    line = hooks.lh.step_saved_values_line
    assert line(UP, 'Greet the learner in the language they chose.') == (
        'Saved for this person: language: "Hindi".')
    assert line(UP, 'Greet the learner in their languages.') == '', (
        'a key matched inside another word')


def test_another_persons_store_never_appears(hooks):
    hooks.stores['999_456'] = {'home_language': 'Hindi'}
    hooks.stores['123_456'] = {}
    assert hooks.lh.step_saved_values(UP, STEP_CHILD) == []
    assert hooks.lh.step_saved_values_line(UP, STEP_CHILD) == ''


def test_values_and_keys_are_bounded(hooks):
    hooks.stores['123_456'] = {f'note_{n}': 'x' * 500 for n in range(1, 9)}
    pairs = hooks.lh.step_saved_values(
        UP, ' and '.join(f'note_{n}' for n in range(1, 9)))
    assert len(pairs) == hooks.lh.STEP_SAVED_VALUES_MAX_KEYS, pairs
    assert [k for k, _ in pairs] == [f'note_{n}' for n in range(1, 6)], pairs
    assert all(len(v) <= hooks.lh.STEP_SAVED_VALUE_MAX_CHARS for _, v in pairs), pairs


def test_a_true_or_a_dict_is_shown_as_saved_and_an_empty_value_is_not(hooks):
    """#282 left home_language True on the speech agent's store; a record is
    a dict.  Both are shown as saved, as JSON, and nothing raises.  An empty
    value says nothing and is left out."""
    hooks.stores['123_456'] = {'home_language': True, 'child_profile': {'name': 'Zoey'},
                               'child_name': '', 'known_words': []}
    pairs = dict(hooks.lh.step_saved_values(
        UP, 'Use home_language, child_profile, child_name and known_words.'))
    assert pairs == {'home_language': 'true', 'child_profile': '{"name": "Zoey"}'}, pairs


@pytest.mark.parametrize('store_file', ['missing', 'unreadable'])
def test_reading_saved_values_never_touches_the_memory_the_tools_hold(
        hooks, mock_flask_app, monkeypatch, tmp_path, store_file):
    """sub N's safety check: the loader ASSIGNS the store it reads and sets it
    to {} when the file is missing or cannot be read (the agent's own store
    file does not exist for the speech agent).  step_saved_values reads into
    its own dict, so the memory the tools hold (create_recipe.agent_data,
    reuse_recipe.agent_data) is never swapped or emptied by a read."""
    import hartos.create_recipe as cr
    import hartos.helper
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(hartos.helper, 'AGENT_DATA_DIR', str(tmp_path))
    if store_file == 'unreadable':
        (tmp_path / '123_456_agent_data.json').write_bytes(b'\x00 not a saved store')
    own = {'home_language': 'Tamil'}
    monkeypatch.setitem(cr.agent_data, '123_456', own)
    monkeypatch.setitem(rr.agent_data, '123_456', own)
    # The real loader, over this test's store files.
    monkeypatch.setattr(hartos.helper, 'load_agent_data_from_file', hooks.real_load)

    assert hooks.lh.step_saved_values(UP, 'Say it in home_language.') == []
    assert cr.agent_data['123_456'] is own and rr.agent_data['123_456'] is own
    assert own == {'home_language': 'Tamil'}


def test_the_saved_values_line_is_one_line_for_every_caller(hooks):
    """hartos-17's MUST 2 on #295 F5: one writer of "Saved for this person"
    (step_saved_values_line), for REUSE's step message.  It strips the
    plan's tool syntax itself."""
    hooks.stores['123_456'] = {'home_language': 'Tamil', 'child_name': 'Zoey'}
    line = hooks.lh.step_saved_values_line
    assert line(UP, 'Say it in home_language.') == (
        'Saved for this person: home_language: "Tamil".')
    assert line(UP, 'Praise child_name in home_language.') == (
        'Saved for this person: child_name: "Zoey"; home_language: "Tamil".')
    # The authored "<tool>: ..." prefix is not the step's words.
    assert line(UP, 'home_language: Ask the parent how the day went.') == ''
    assert line(UP, 'Ask how the day went.') == ''
    assert line(UP, '') == '' and line(UP, None) == ''


def test_a_question_found_asks_the_model_nothing(hooks):
    hooks.record = {'asked': 'Shall I start with Monday?'}
    hooks.line = LINE_TA
    assert hooks.lh.held_step_reply(UP, 1, 0) == 'Shall I start with Monday?'
    assert hooks.asked == []


# ── CREATE: the build's turn on a held step ──

@pytest.fixture
def create_turn(hooks, test_user_id, test_prompt_id, mock_flask_app, sample_config_json,
                monkeypatch):
    """create_recipe._ask_for_help on the person's own turn, over the real
    create_agents seam (test_create_gate_stops_the_chat._built_selector)."""
    from tests.unit.test_create_gate_stops_the_chat import _built_selector
    from hartos.threadlocal import thread_local_data
    import hartos.create_recipe as cr
    thread_local_data.set_request_id('msg_her_turn')
    with _built_selector(test_user_id, test_prompt_id) as built:
        monkeypatch.setattr(cr, 'get_registered_groupchat',
                            lambda up: SimpleNamespace(messages=hooks.messages))
        built.cr, built.hooks = cr, hooks
        yield built


def _held(task, kind):
    task._needs_user_input_action_id, task._needs_user_input_kind = 1, kind


def test_create_answers_a_held_step_with_its_recorded_question(create_turn):
    from core.constants import USER_INPUT_GATE_AWAITING_REPLY
    s = create_turn
    _held(s.task, USER_INPUT_GATE_AWAITING_REPLY)
    s.hooks.record = {'asked': 'Shall I start with Monday?'}
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP, 'the step waits for her')
    assert reply == 'Shall I start with Monday?'
    # The hold is the gate's and stays as it was.
    assert (s.task._needs_user_input_action_id, s.task._needs_user_input_kind) == (
        1, USER_INPUT_GATE_AWAITING_REPLY)


def test_create_answers_with_the_windows_question_when_the_record_has_none(create_turn):
    from core.constants import USER_INPUT_GATE_HUMAN_REQUIRED
    s = create_turn
    # The gate recorded no reply kind, but the step's question is out.
    _held(s.task, USER_INPUT_GATE_HUMAN_REQUIRED)
    s.hooks.record = {'asked': None, 'question': None}
    s.hooks.messages = _window(_send('c1', DRAFT))
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP, 'the step waits for her')
    assert reply == 'Shall I start with Monday?', reply
    assert "isn't coming together" not in reply


def test_create_answers_with_an_open_cards_question_whatever_kind_the_gate_kept(
        create_turn):
    """The step asked on a card, which the gate's kind does not say: its
    question is still the reply, not the creator line."""
    from core.constants import USER_INPUT_GATE_HUMAN_REQUIRED
    s = create_turn
    _held(s.task, USER_INPUT_GATE_HUMAN_REQUIRED)
    s.hooks.messages = _window(_card('c2', 'A1', 'waiting_for_person'))
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP, 'it waits for her card')
    assert reply == 'Weekly review: Which day suits you for the weekly review?', reply


@pytest.mark.parametrize('answered_on', ['card', 'send'])
def test_create_never_answers_with_a_question_already_answered(
        create_turn, monkeypatch, answered_on):
    """hartos-17's condition on _ask_for_help reading held_step_question
    first: an ANSWERED question, on a card or a send, is never the reply (a
    stuck step would ask the creator again what she answered); only an open
    one is."""
    from core.constants import USER_INPUT_GATE_HUMAN_REQUIRED
    s = create_turn
    _held(s.task, USER_INPUT_GATE_HUMAN_REQUIRED)
    if answered_on == 'card':
        s.hooks.messages = _window(_card('c2', 'A1', 'answered', answer={'day': 'Monday'}))
    else:
        s.hooks.messages = _window(_send('c1', DRAFT), [
            {'role': 'user', 'name': 'UserProxy', 'content': 'Monday works'}])
        monkeypatch.setattr(s.hooks.lh, 'recorded_answer_requests',
                            lambda up, aid, *a, **k: [('Monday works', 'msg_her_answer')])
        monkeypatch.setattr(s.cr, 'recorded_answers',
                            lambda up, aid, *a, **k: ['Monday works'])
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP, 'it did not complete')
    assert reply.startswith('I need your input to finish building this agent.'), reply
    assert 'Monday' not in reply and 'Which day' not in reply, reply
    assert s.hooks.asked == [], 'an answered question is no reason to ask the model'


def test_create_after_a_restart_answers_with_the_plain_line_and_asks_no_model(
        create_turn):
    """After a restart the window is empty and the record carries no
    question: the build's real _ask_for_help answers with the floor, even
    with the child's language saved and a model ready with a Tamil line
    (hartos-17's ruling on the held-out run of 2026-11-26)."""
    from core.constants import USER_INPUT_GATE_AWAITING_REPLY
    s = create_turn
    _held(s.task, USER_INPUT_GATE_AWAITING_REPLY)
    s.hooks.messages = []
    s.hooks.stores['123_456'] = {'home_language': 'Tamil'}
    s.hooks.line = LINE_TA
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP_CHILD, 'the step waits for her')
    assert reply == s.hooks.lh.HELD_STEP_FALLBACK_LINE, reply
    assert s.hooks.asked == [], 'the model was asked for the held step line'
    # The hold is the gate's and stays as it was.
    assert (s.task._needs_user_input_action_id, s.task._needs_user_input_kind) == (
        1, USER_INPUT_GATE_AWAITING_REPLY)


def test_the_creator_line_names_the_step_without_the_plans_tool_syntax(create_turn):
    from core.constants import USER_INPUT_GATE_HUMAN_REQUIRED
    s = create_turn
    _held(s.task, USER_INPUT_GATE_HUMAN_REQUIRED)
    reply = s.cr._ask_for_help(s.up, 456, 1, STEP, 'it did not complete')
    assert reply.startswith('I need your input to finish building this agent. Step 1 '), reply
    assert "Get every flow's saved results" in reply, reply
    assert 'send_message_to_user' not in reply and '—' not in reply, reply
    assert s.hooks.asked == [], 'the creator line needs no model'


# ── REUSE: a turn with no request id on a held step (#46) ──

@pytest.fixture
def reuse_gate(hooks, mock_flask_app, monkeypatch):
    import hartos.reuse_recipe as rr
    monkeypatch.setattr(rr, '_reuse_held_action', lambda up: 1)
    monkeypatch.setattr(rr, '_reuse_action_flow', lambda up, aid: 0)
    return rr


def test_reuse_answers_a_held_step_with_its_question(reuse_gate, hooks):
    hooks.record = {'asked': 'Shall I start with Monday?', 'question': DRAFT}
    assert reuse_gate._reuse_held_step_gate(UP, '') == 'Shall I start with Monday?'


def test_reuse_with_no_question_on_record_answers_with_the_plain_line_not_step_n(
        reuse_gate, hooks):
    hooks.record = {'asked': None, 'question': None}
    hooks.stores['123_456'] = {'home_language': 'Tamil'}
    hooks.line = LINE_TA
    reply = reuse_gate._reuse_held_step_gate(UP, '')
    assert reply == hooks.lh.HELD_STEP_FALLBACK_LINE
    assert not reply.startswith('Step'), reply
    assert hooks.asked == [], 'the model was asked for the held step line'

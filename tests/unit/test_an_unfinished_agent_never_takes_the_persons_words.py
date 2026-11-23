"""A person who writes to an agent whose build never finished is not the
build's input.

MEASURED live 2026-11-10: a parent typed to an unfinished speech-therapy
agent (its config was saved, its flow-0 recipe was not).  /chat routes such a
turn into creation so the build finishes on its own (steward rule
2026-08-22), and it handed recipe() the PERSON'S chat words.  The build took
the parent's words as the child's turn and praised them as the child's
speech.

The steward rule stays: the turn still finishes the build autonomously.  What
changes is the input and the reply.
  * The build resumes on the agent's own creation input, the `goal` its saved
    config carries (the gather phase's schema: "The ultimate goal of the
    agent"), or a fixed neutral sentence when the config has none.  Never the
    turn's text.
  * The person is told plainly that the agent is not finished yet.  Their
    message is not used for the build.
  * When recipe() finishes, the existing ready reply is unchanged.

Rework after the review of cfa9e29ee (REJECTED): the claim held only for the
first message.  The second message went through Phase 2 with the person's
words (probe c2a), so did a partly built agent through the flow ladder (c2b);
a build ending in the CREATE error envelope read "being completed" (c2c); and
the notice said things that were not true.  And on a person's request id the
create loop takes whatever text recipe() gets as that person's reply (a held
step, or any question the window holds), so even the goal sentence was
recorded as the child's answer.  Now: the build's own input is recorded for
the next turns (resume_build_input), answers no step (text_is_the_persons),
one reply rule serves both sites, and only a step the build asked THIS person
for takes their words.

hart_intelligence_entry cannot be imported in the test venv (it starts the
whole backend), so the REAL block is taken from the source by AST and run
with the names it reads bound to fakes, as
tests/unit/test_yield_pauses_every_agent.py does for the autonomous create.
"""
import ast
import functools
import io
import json
import os
import threading
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_HIE = os.path.join(_ROOT, 'hart_intelligence_entry.py')

_PID = '4242'
_LABEL = 'Speech Buddy'
_GOAL = 'Help a young child practise saying their speech sounds clearly.'
# What the parent typed to the agent: a person's chat words, not a child's turn.
_TURN = 'Hi, this is his dad. Can you start the session with Arjun now?'
# What an unfinished build answers when it ran on a turn: the build's own
# chatter, addressed to whoever it took the input from.
_BUILD_CHATTER = 'Wonderful speaking, great job! Let us try the next sound.'
_READY_HEAD = f'"{_LABEL}" is ready'
_READY_TAIL = 'its recipe is now complete. Ask it again and it will run.'


def _calls_flow_recipe_exists_on_prompt_id(test):
    for node in ast.walk(test):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == '_flow_recipe_exists'
                and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                and node.args[0].id == 'prompt_id'):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _unfinished_agent_block():
    """The REAL `if ... not _flow_recipe_exists(prompt_id):` block of /chat's
    reuse route, compiled as a function body."""
    tree = ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())
    blocks = [n for n in ast.walk(tree)
              if isinstance(n, ast.If)
              and _calls_flow_recipe_exists_on_prompt_id(n.test)]
    assert len(blocks) == 1, (
        f'expected one `if ... _flow_recipe_exists(prompt_id)` block in '
        f'{_HIE}, found {len(blocks)}')
    fn = ast.parse('def _unfinished_turn():\n    pass\n').body[0]
    fn.body = [blocks[0]]
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, _HIE, 'exec')


# The module-level pieces both resuming sites share (review of cfa9e29ee:
# one recogniser set, one reply rule, one label rule), taken from the source.
_HELPERS = ('_own_build_input', '_unfinished_agent_label', '_resumed_build_reply')


@functools.lru_cache(maxsize=1)
def _helpers():
    """Whichever of the shared helpers the source has.  No existence check:
    on a tree without them the blocks run as that tree wrote them, so the
    behaviour tests fail by their assertions there, not on a missing name
    (review of 49eee800d, must-fix 3: 26 of its 36 parent failures were
    this check)."""
    tree = ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in _HELPERS]
    module = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, _HIE, 'exec')


def _speech_config():
    return {'status': 'completed', 'name': _LABEL,
            'agent_name': 'speech.buddy', 'goal': _GOAL,
            'personas': [{'name': 'Coach', 'description': 'speech coach'}],
            'flows': [{'flow_name': 'main', 'persona': 'Coach',
                       'actions': ['Listen to the child say a sound',
                                   'Praise the attempt'],
                       'sub_goal': 'one practice round'}],
            'prompt_id': _PID, 'creator_user_id': 'u1'}


def _names(recipe_reply, calls, holds=False, **extra):
    """The names the /chat blocks read, bound to fakes, plus the real shared
    helpers.  `holds`: whether the build holds a step for this person."""
    def _recipe(*a, **k):
        calls.append((a, k))
        return recipe_reply

    ns = {
        'prompt_id': _PID, 'prompt': _TURN,
        'user_id': 'u1', 'file_id': None, 'request_id': 'req-person-1',
        '_ak': f'u1_{_PID}', 'app': MagicMock(),
        '_get_user_lock': lambda uid: threading.Lock(),
        '_user_lock': threading.Lock(),
        'review_agents': {}, 'conversation_agent': {},
        'resume_build_input': {},
        'build_holds_a_step_for_the_person': lambda uid, pid: holds,
        '_touch_agent_timestamp': lambda *a: None,
        'recipe': _recipe,
        '_create_social_agent_from_prompt': lambda *a: None,
        '_record_lifecycle': lambda *a, **k: None,
        '_push_workflow_flowchart': lambda *a, **k: None,
        '_chat_reply': lambda uid, rid, text, **payload: (text, payload),
    }
    ns.update(extra)
    exec(_helpers(), ns)
    return ns


def _run(recipe_reply, config=None, holds=False):
    """Run the block for a person's turn on agent _PID, whose config is
    `config` and whose flow-0 recipe is missing.  Returns
    (reply text, reply payload, the calls recipe() received, the names)."""
    calls = []
    ns = _names(recipe_reply, calls, holds=holds,
                _flow_recipe_exists=lambda pid: False,
                created_json=_speech_config() if config is None else config)
    exec(_unfinished_agent_block(), ns)
    text, payload = ns['_unfinished_turn']()
    return text, payload, calls, ns


def _turn(recipe_reply, config=None):
    text, payload, calls, _ = _run(recipe_reply, config=config)
    return text, payload, calls


def _build_text(call):
    a, k = call
    return a[1] if len(a) > 1 else k.get('text')


class TestTheBuildRunsOnTheAgentsOwnInput:

    def test_recipe_receives_the_configs_goal_not_the_turn(self):
        _, _, calls = _turn(_BUILD_CHATTER)
        assert len(calls) == 1, calls
        assert _build_text(calls[0]) == _GOAL

    def test_the_turn_text_reaches_recipe_nowhere(self):
        _, _, calls = _turn(_BUILD_CHATTER)
        a, k = calls[0]
        everything = [str(v) for v in a] + [str(v) for v in k.values()]
        assert not any(_TURN in v for v in everything), calls

    def test_the_steward_rule_still_finishes_it_autonomously(self):
        _, _, calls = _turn(_BUILD_CHATTER)
        a, k = calls[0]
        assert k.get('autonomous') is True
        assert a[0] == 'u1' and a[2] == _PID

    def test_a_config_without_a_goal_resumes_on_a_neutral_sentence(self):
        config = {'status': 'completed', 'name': _LABEL,
                  'flows': [], 'personas': [], 'prompt_id': _PID}
        _, _, calls = _turn(_BUILD_CHATTER, config=config)
        built_on = _build_text(calls[0])
        assert isinstance(built_on, str) and built_on.strip(), calls
        assert _TURN not in built_on


class TestThePersonIsToldTheAgentIsNotFinished:

    def test_the_reply_says_the_agent_is_not_finished(self):
        text, payload, _ = _turn(_BUILD_CHATTER)
        assert _LABEL in text, text
        assert 'not finished' in text.lower(), text
        assert payload['Agent_status'] == 'Creation Mode'
        assert payload['prompt_id'] == _PID

    def test_the_build_chatter_is_not_answered_to_the_person(self):
        text, _, _ = _turn(_BUILD_CHATTER)
        assert _BUILD_CHATTER not in text, text
        assert _TURN not in text, text


class TestAFailedOrPausedBuildStillSaysSo:
    """The not-finished notice is for a build under way.  A build that failed,
    stopped for a person or was held keeps its own words, as before, so a
    failure is never shown as "being completed"."""

    def test_a_failed_build_reaches_the_person_in_its_own_words(self):
        from core.agent_tools import user_facing_error
        failed = user_facing_error(RuntimeError('402 Payment Required'))
        text, payload, _ = _turn(failed)
        assert text == failed, text
        assert payload['Agent_status'] == 'Creation Mode'

    def test_a_build_that_stopped_for_the_person_says_so(self):
        from core.agent_tools import user_pause_reply
        paused = user_pause_reply(1)
        text, _, _ = _turn(paused)
        assert text == paused, text


class TestAFinishedBuildAnswersAsBefore:

    def test_the_ready_reply_is_unchanged(self):
        for done in ('Agent Created Successfully',
                     'Agent Already Created Successfully'):
            text, payload, _ = _turn(done)
            assert text.startswith(_READY_HEAD), text
            assert text.endswith(_READY_TAIL), text
            # The one character between the two halves (with its spaces) is
            # the existing wording's dash: nothing was added or dropped.
            assert len(text) == len(_READY_HEAD) + 3 + len(_READY_TAIL), text
            assert payload['Agent_status'] == 'completed'
            assert payload['prompt_id'] == _PID


# ── Rework of cfa9e29ee ─────────────────────────────────────────────────────
# What the build asks a person when it holds a step for them (create_recipe
# _needs_input_reply): the one build reply their words may answer.
_BUILD_ASKS = ("I need your input to finish building this agent. Step 2 "
               "(\"Praise the attempt\") isn't coming together from what I "
               "have so far.")
_ERROR_ENVELOPE = ('{"status": "error", "action": "Praise the attempt", '
                   '"action_id": 2, "message": "the tool failed"}')


def _stop_replies():
    from core.agent_tools import (held_for_another_turn_reply,
                                  user_facing_error, user_pause_reply)
    from core.constants import HELP_PAUSED_REPLY_PREFIX
    return {
        'error': user_facing_error(RuntimeError('402 Payment Required')),
        'user pause': user_pause_reply(1),
        'held for another turn': held_for_another_turn_reply(),
        'help pause': (f'{HELP_PAUSED_REPLY_PREFIX} step 2 could not be '
                       f'finished autonomously (no tool). It is waiting for '
                       f'the owner or the co-pilot.'),
        'error envelope': _ERROR_ENVELOPE,
    }


class TestTheBuildInputAnswersNoStep:

    def test_recipe_is_told_the_text_is_not_the_persons(self):
        _, _, calls = _turn(_BUILD_CHATTER)
        assert calls[0][1].get('text_is_the_persons') is False, calls

    def test_the_next_turns_resume_on_the_same_input(self):
        _, _, _, ns = _run(_BUILD_CHATTER)
        assert ns['resume_build_input'][f'u1_{_PID}'] == {
            'input': _GOAL, 'label': f'"{_LABEL}"'}

    def test_a_finished_build_leaves_nothing_to_resume(self):
        _, _, _, ns = _run('Agent Created Successfully')
        assert f'u1_{_PID}' not in ns['resume_build_input']


class TestTheNoticeIsTrue:

    def test_it_claims_no_completion_and_no_unused_message(self):
        text, _, _ = _turn(_BUILD_CHATTER)
        assert 'being completed' not in text, text
        assert 'was not used' not in text, text
        assert text.startswith(f'"{_LABEL}" is not finished yet'), text

    def test_a_placeholder_name_is_never_the_label(self):
        config = dict(_speech_config(), name=f'Agent {_PID}', agent_name='')
        text, _, _ = _turn(_BUILD_CHATTER, config=config)
        assert text.startswith('This agent is not finished yet'), text
        assert _PID not in text, text

    def test_no_name_is_never_the_bare_prompt_id(self):
        config = {'status': 'completed', 'goal': _GOAL, 'flows': [],
                  'personas': [], 'prompt_id': _PID}
        text, _, _ = _turn(_BUILD_CHATTER, config=config)
        assert text.startswith('This agent is not finished yet'), text
        assert _PID not in text, text


class TestEveryStopReplyIsShownAsWritten:
    """One recogniser set for every site (core.agent_tools.is_build_stop_reply):
    the copy cfa9e29ee made left out the error envelope (probe c2c), and its
    held-turn and help-pause branches had no test (mutants M2d, M2e)."""

    def test_each_stop_reply_reaches_the_person_as_written(self):
        for kind, reply in _stop_replies().items():
            text, payload, _ = _turn(reply)
            assert text == reply, (kind, text)
            assert payload['Agent_status'] == 'Creation Mode', kind

    def test_a_step_the_build_asked_this_person_for_is_shown(self):
        text, _, _, _ = _run(_BUILD_ASKS, holds=True)
        assert text == _BUILD_ASKS, text

    def test_the_same_question_unasked_reads_as_not_finished(self):
        text, _, _, _ = _run(_BUILD_ASKS, holds=False)
        assert text != _BUILD_ASKS and 'not finished' in text, text


# ── Phase 2: the second message (probe c2a) and the creator's own turns ────

def _cond_is(node, src, text):
    return ast.get_source_segment(src, node.test) == text


@functools.lru_cache(maxsize=None)
def _block(test_text, fn_name, tail=None):
    """The REAL `if <test_text>:` block of /chat, compiled as `fn_name`'s body
    (plus `tail` statements)."""
    src = io.open(_HIE, encoding='utf-8', errors='replace').read()
    tree = ast.parse(src)
    blocks = [n for n in ast.walk(tree)
              if isinstance(n, ast.If) and _cond_is(n, src, test_text)]
    assert len(blocks) == 1, (test_text, len(blocks))
    fn = ast.parse(f'def {fn_name}():\n    pass\n').body[0]
    fn.body = [blocks[0]] + (ast.parse(tail).body if tail else [])
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, _HIE, 'exec')


def _phase2(monkeypatch, recipe_reply, saved=True, holds=False):
    import sys
    import types
    fake = types.ModuleType('integrations.agent_engine.dispatch')
    fake.mark_create_start = lambda **k: None
    fake.mark_create_end = lambda: None
    monkeypatch.setitem(sys.modules, 'integrations.agent_engine.dispatch', fake)
    calls = []
    ns = _names(recipe_reply, calls, holds=holds,
                _in_review=True, _in_convo=False)
    if saved:
        ns['resume_build_input'][f'u1_{_PID}'] = {'input': _GOAL,
                                                 'label': f'"{_LABEL}"'}
    exec(_block('_in_review and not _in_convo', '_phase2_turn'), ns)
    text, payload = ns['_phase2_turn']()
    return text, payload, calls, ns


class TestTheSecondMessageIsNotTheBuildsInput:

    def test_the_parents_second_message_never_reaches_recipe(self, monkeypatch):
        text, payload, calls, _ = _phase2(monkeypatch, _BUILD_CHATTER)
        assert len(calls) == 1, calls
        a, k = calls[0]
        assert a == ('u1', _GOAL, _PID, None, 'req-person-1'), a
        assert k == {'autonomous': True, 'text_is_the_persons': False}, k
        assert _BUILD_CHATTER not in text and 'not finished' in text, text
        assert payload['Agent_status'] == 'Review Mode'

    def test_a_stop_reply_on_a_resumed_turn_is_shown(self, monkeypatch):
        text, _, _, _ = _phase2(monkeypatch, _ERROR_ENVELOPE)
        assert text == _ERROR_ENVELOPE, text

    def test_the_answer_to_a_step_it_asked_for_goes_to_the_build(self, monkeypatch):
        text, _, calls, _ = _phase2(monkeypatch, _BUILD_ASKS, holds=True)
        a, k = calls[0]
        assert a == ('u1', _TURN, _PID, None, 'req-person-1'), a
        assert k == {'autonomous': True}, k
        assert text == _BUILD_ASKS, text

    def test_a_finished_resumed_build_drops_its_saved_input(self, monkeypatch):
        text, payload, _, ns = _phase2(monkeypatch, 'Agent Created Successfully')
        assert f'u1_{_PID}' not in ns['resume_build_input']
        assert text == 'Agent Created Successfully'
        assert payload['Agent_status'] == 'completed'

    def test_the_creators_own_turn_is_unchanged(self, monkeypatch):
        """No saved input: the creator's ordinary Phase 2 turn calls recipe()
        exactly as before and gets the build's reply byte for byte."""
        text, payload, calls, ns = _phase2(monkeypatch, _BUILD_CHATTER, saved=False)
        assert calls == [(('u1', _TURN, _PID, None, 'req-person-1'),
                          {'autonomous': True})], calls
        assert text == _BUILD_CHATTER
        assert payload['Agent_status'] == 'Review Mode'
        assert ns['resume_build_input'] == {}


# ── The flow ladder: a partly built agent (probe c2b) ───────────────────────

_LADDER_TEST = ("os.path.exists(os.path.join(PROMPTS_DIR, "
                "f'{prompt_id}_{no_of_flow}_recipe.json'))")


def _ladder(tmp_path, mid_build=False, last_flow_built=False):
    pdir = tmp_path / 'prompts'
    pdir.mkdir()
    (pdir / f'{_PID}_0_recipe.json').write_text('{}')
    if last_flow_built:
        (pdir / f'{_PID}_1_recipe.json').write_text('{}')
    ns = _names(_BUILD_CHATTER, [], os=os, PROMPTS_DIR=str(pdir),
                no_of_flow=1, data=_speech_config(),
                set_flags_to_enter_reuse_mode=lambda *a, **k: False)
    if mid_build:
        ns['review_agents'][f'u1_{_PID}'] = True
    if last_flow_built:
        ns['resume_build_input'][f'u1_{_PID}'] = {'input': _GOAL, 'label': 'x'}
    exec(_block(_LADDER_TEST, '_ladder_turn', tail='return create_agent'), ns)
    return ns['_ladder_turn'](), ns


class TestAPartlyBuiltAgentResumesOnItsOwnInput:

    def test_a_person_writing_to_it_records_the_agents_own_input(self, tmp_path):
        create_agent, ns = _ladder(tmp_path)
        assert create_agent is True
        assert ns['resume_build_input'][f'u1_{_PID}'] == {
            'input': _GOAL, 'label': f'"{_LABEL}"'}
        assert ns['review_agents'][f'u1_{_PID}'] is True

    def test_a_creators_turn_mid_build_is_untouched(self, tmp_path):
        create_agent, ns = _ladder(tmp_path, mid_build=True)
        assert create_agent is True
        assert ns['resume_build_input'] == {}

    def test_a_finished_agent_has_nothing_to_resume(self, tmp_path):
        create_agent, ns = _ladder(tmp_path, last_flow_built=True)
        assert create_agent is False
        assert ns['resume_build_input'] == {}


# ── The ladder with no step built yet, on a request's create flag ───────────

def _flagged(tmp_path, mid_build=False, create_agent=True, autonomous=False):
    """The REAL `if create_agent or autonomous:` block of the flow ladder: the
    agent's config exists and no flow-0 recipe does.  Review of 49eee800d,
    must-fix 1: a request carrying create_agent handed the person's words to
    recipe() here, unlike the partly built branch."""
    pdir = tmp_path / 'prompts'
    pdir.mkdir()
    (pdir / f'{_PID}.json').write_text(json.dumps(_speech_config()), encoding='utf-8')
    ns = _names(_BUILD_CHATTER, [], os=os, json=json, PROMPTS_DIR=str(pdir),
                create_agent=create_agent, autonomous=autonomous)
    if mid_build:
        ns['review_agents'][f'u1_{_PID}'] = True
    exec(_block('create_agent or autonomous', '_flagged_turn', tail='return None'), ns)
    ns['_flagged_turn']()
    return ns


class TestARequestsCreateFlagDoesNotMakeItsWordsTheBuilds:

    def test_a_create_request_not_mid_build_resumes_on_the_agents_input(self, tmp_path):
        ns = _flagged(tmp_path)
        assert ns['resume_build_input'].get(f'u1_{_PID}') == {
            'input': _GOAL, 'label': f'"{_LABEL}"'}, ns['resume_build_input']
        assert ns['review_agents'][f'u1_{_PID}'] is True

    def test_an_autonomous_request_not_mid_build_resumes_on_it_too(self, tmp_path):
        ns = _flagged(tmp_path, create_agent=False, autonomous=True)
        assert ns['resume_build_input'].get(f'u1_{_PID}', {}).get('input') == _GOAL

    def test_the_creators_turn_mid_build_is_untouched(self, tmp_path):
        ns = _flagged(tmp_path, mid_build=True)
        assert ns['resume_build_input'] == {}
        assert ns['review_agents'][f'u1_{_PID}'] is True


# ── The create loop's gate: a resumed build's text answers no step ─────────

class TestTheGateTakesOnlyThePersonsWords:
    """hartos.create_recipe: on a person's request id the gate takes the
    turn's text as their reply, to a held step and to a question the window
    holds.  A turn whose text is not theirs (recipe(...,
    text_is_the_persons=False)) answers nothing."""

    _UP = 'u9_p9'

    def _cr(self):
        from hartos import create_recipe
        return create_recipe

    def setup_method(self):
        from hartos.lifecycle_hooks import clear_action_states
        clear_action_states(self._UP)
        self._tokens = []

    def teardown_method(self):
        from hartos.lifecycle_hooks import clear_action_states
        cr = self._cr()
        for token in reversed(self._tokens):
            cr.turn_text_is_the_persons.reset(token)
        for cache in (cr.user_tasks, cr.request_id_list):
            try:
                del cache[self._UP]
            except KeyError:
                pass
        clear_action_states(self._UP)

    def _mark(self, value):
        """What recipe() writes at the turn's start, in this thread."""
        self._tokens.append(self._cr().turn_text_is_the_persons.set(value))

    def _held(self, kind='human_required'):
        from types import SimpleNamespace
        task = SimpleNamespace(_needs_user_input_action_id=2,
                               _needs_help_reason="need the child's word",
                               _needs_user_input_kind=kind, current_action=2)
        cr = self._cr()
        cr.user_tasks[self._UP] = task
        cr.request_id_list[self._UP] = 'user-request-7'
        return task

    def test_a_resumed_builds_goal_is_not_recorded_as_the_answer(self):
        from unittest.mock import patch
        cr = self._cr()
        task = self._held()
        self._mark(False)
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=True) as resume, \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            assert cr._resume_prior_user_input_block(self._UP, _GOAL) is False
        resume.assert_not_called()
        record.assert_not_called()
        assert task._needs_user_input_action_id == 2

    def test_nor_taken_for_a_question_the_window_holds(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        cr = self._cr()
        cr.user_tasks[self._UP] = SimpleNamespace(
            _needs_user_input_action_id=None, current_action=2)
        cr.request_id_list[self._UP] = 'user-request-7'
        self._mark(False)
        with patch('hartos.create_recipe.ledger_holds_no_block',
                   return_value=True), \
                patch('hartos.create_recipe.reply_closes_open_question',
                      return_value=True) as closes, \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            assert cr._resume_prior_user_input_block(self._UP, _GOAL) is False
        closes.assert_not_called()
        record.assert_not_called()

    def test_the_persons_own_answer_is_still_taken(self):
        from unittest.mock import patch
        cr = self._cr()
        task = self._held()
        self._mark(True)
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=True), \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            assert cr._resume_prior_user_input_block(self._UP, 'ball') is True
        assert record.call_args[0][:3] == (self._UP, 2, 'ball')
        assert task._needs_user_input_action_id is None

    def test_a_step_held_for_the_person_is_seen(self):
        from hartos.create_recipe import build_holds_a_step_for_the_person
        self._held()
        assert build_holds_a_step_for_the_person('u9', 'p9') is True

    def test_a_loop_break_flag_asked_nobody(self):
        from hartos.create_recipe import (USER_INPUT_GATE_RECOVERABLE_STALL,
                                          build_holds_a_step_for_the_person)
        self._held(kind=USER_INPUT_GATE_RECOVERABLE_STALL)
        assert build_holds_a_step_for_the_person('u9', 'p9') is False

    def test_no_session_holds_nothing(self):
        from hartos.create_recipe import build_holds_a_step_for_the_person
        assert build_holds_a_step_for_the_person('u9', 'p9') is False

    def test_an_overlapping_turn_cannot_flip_this_turns_mark(self):
        """Nothing serializes two turns of one agent (hartos-d9, 11-11): the
        person's ordinary turn starting in another thread while a resumed
        build runs here must not make this build's text an answer."""
        import threading
        from unittest.mock import patch
        cr = self._cr()
        task = self._held()
        self._mark(False)
        other = threading.Thread(
            target=lambda: cr.turn_text_is_the_persons.set(True))
        other.start()
        other.join()
        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=True), \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            assert cr._resume_prior_user_input_block(self._UP, _GOAL) is False
        record.assert_not_called()
        assert task._needs_user_input_action_id == 2

    def test_a_resumed_turns_mark_reaches_no_other_turn(self):
        """...and the resumed build's False does not reach the person's own
        turn running in another thread: their answer is taken."""
        import threading
        from unittest.mock import patch
        cr = self._cr()
        self._held()
        self._mark(False)
        taken = {}

        def persons_turn():
            cr.turn_text_is_the_persons.set(True)  # what recipe() writes
            taken['ok'] = cr._resume_prior_user_input_block(self._UP, 'ball')

        with patch('hartos.create_recipe.resume_from_user_input',
                   return_value=True), \
                patch('hartos.create_recipe.record_user_answer',
                      return_value=True) as record:
            t = threading.Thread(target=persons_turn)
            t.start()
            t.join()
        assert taken.get('ok') is True, taken
        assert record.call_args[0][:3] == (self._UP, 2, 'ball')

    def test_a_resumed_build_through_recipe_answers_nothing_then_the_person_does(self):
        """The real recipe(), twice on one thread: a resumed build on the
        agent's goal (text_is_the_persons=False) records nothing for the step
        held for the person; the person's own next turn (an ordinary call)
        is taken as the answer.  Only the model round is stood in for: the
        fake get_response_group does what the real one does first, calling
        the gate with the turn's text.  Kills a mark never written, a mark
        written only when False (it would stick to the person's turn), and
        a round hopped onto another thread (it would read the default)."""
        import contextvars
        from unittest.mock import patch
        from flask import Flask
        cr = self._cr()
        task = self._held()
        answered = []

        def _round(user_id, text, prompt_id, *a, **k):
            # get_response_group's first statement: the gate, on the turn's text.
            answered.append(cr._resume_prior_user_input_block(
                f'{user_id}_{prompt_id}', text))
            return 'the build ran'

        def _two_turns():
            with Flask(__name__).app_context(), \
                    patch('hartos.create_recipe.get_response_group', _round), \
                    patch.object(cr.helper_fun, 'load_agent_data_from_file',
                                 lambda *a, **k: None), \
                    patch('hartos.create_recipe.resume_from_user_input',
                          return_value=True), \
                    patch('hartos.create_recipe.record_user_answer',
                          return_value=True) as record:
                first = cr.recipe('u9', _GOAL, 'p9', None, 'user-request-7',
                                  autonomous=True, text_is_the_persons=False)
                held_after_resume = task._needs_user_input_action_id
                recorded_after_resume = record.call_count
                second = cr.recipe('u9', 'ball', 'p9', None, 'user-request-8',
                                   autonomous=True)
                return (first, held_after_resume, recorded_after_resume,
                        second, record.call_args)

        # Its own context, so nothing it writes reaches another test.
        first, held, recorded, second, last_call = \
            contextvars.copy_context().run(_two_turns)
        assert first == 'the build ran' and second == 'the build ran'
        assert answered[0] is False, answered
        assert recorded == 0, 'the goal sentence was recorded as an answer'
        assert held == 2, 'the resumed build released the step held for the person'
        assert answered[1] is True, answered
        assert last_call[0][:3] == (self._UP, 2, 'ball'), last_call

    def test_source_guard_the_gate_is_reached_only_on_recipes_own_thread(self):
        """A context variable does not follow work into another thread
        (hartos-d9, 11-11): a read off recipe()'s thread sees the default
        True and adopts the text again.  The gate is reached only by direct
        calls: recipe() -> get_response_group -> the gate, and
        get_response_group -> safe_action_boundary_check / _next_flow_reply
        -> get_response_group.  Each call sits directly in one of those
        functions (no nested def or lambda that a hook, timer or executor
        could run), none of them is passed anywhere as a value (a callback,
        Thread target or executor.submit argument), and no other module
        calls them."""
        chain = {'_resume_prior_user_input_block', 'get_response_group',
                 '_next_flow_reply', 'safe_action_boundary_check'}
        callers = chain | {'recipe'}
        path = os.path.join(_ROOT, 'hartos', 'create_recipe.py')
        tree = ast.parse(io.open(path, encoding='utf-8', errors='replace').read())
        direct = set()
        bad = []

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda)):
                    inner = getattr(child, 'name', '<lambda>')
                    visit(child, inner if owner == '<module>' else
                          f'{owner}>{inner}')
                    continue
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                        and child.func.id in chain):
                    direct.add(id(child.func))
                    if owner not in callers:
                        bad.append((child.func.id, child.lineno, owner))
                visit(child, owner)

        visit(tree, '<module>')
        values = [(n.id, n.lineno) for n in ast.walk(tree)
                  if isinstance(n, ast.Name) and n.id in chain
                  and isinstance(n.ctx, ast.Load) and id(n) not in direct]
        assert bad == [], f'reached off recipe()\'s own call chain: {bad}'
        assert values == [], f'passed as a value (may run on another thread): {values}'
        for root, dirs, files in os.walk(_ROOT):
            dirs[:] = [d for d in dirs if d not in (
                '.git', 'venv', 'node_modules', '__pycache__', 'tests')]
            for name in files:
                full = os.path.join(root, name)
                if not name.endswith('.py') or os.path.samefile(full, path):
                    continue
                try:
                    other = ast.parse(io.open(full, encoding='utf-8',
                                              errors='replace').read())
                except (SyntaxError, ValueError):
                    continue
                for n in ast.walk(other):
                    if (isinstance(n, ast.Attribute) and n.attr in chain) or (
                            isinstance(n, ast.Name) and n.id in chain):
                        bad.append((n.lineno, full))
        assert bad == [], f'reached from another module: {bad}'

    def test_source_guard_recipe_rewrites_the_mark_on_every_call(self):
        """recipe() writes the mark from its own parameter as an
        unconditional statement of its body, before its first
        get_response_group call: every ordinary turn writes True, so a
        resumed build's False never sticks to the next turn on a reused
        thread."""
        src = io.open(os.path.join(_ROOT, 'hartos', 'create_recipe.py'),
                      encoding='utf-8', errors='replace').read()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == 'recipe')
        marks = [n.lineno for n in fn.body
                 if isinstance(n, ast.Expr)
                 and (ast.get_source_segment(src, n) or '')
                 == 'turn_text_is_the_persons.set(bool(text_is_the_persons))']
        rounds = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == 'get_response_group']
        assert len(marks) == 1, 'recipe() does not write the mark on every call'
        assert rounds and marks[0] < min(rounds)


# ── A reply after a restart is still the person's answer (review of 49eee800d,
#    must-fix 2): the ladder's predicate reads the ledger the gate reads ────

class TestAReplyAfterARestartIsStillTaken:
    """build_holds_a_step_for_the_person with no gate flag: the real
    lifecycle_hooks wait (mark_action_waiting_for_user on a person's turn,
    which records the ask) on a real SmartLedger registered for the session.
    Without it, a restart between the build's question and the person's reply
    sent the reply nowhere: the ladder resumed the build on its saved input."""

    _UP = 'u7_p7'

    def setup_method(self):
        import sys
        sys.path.insert(0, os.path.join(_ROOT, 'agent-ledger-opensource'))
        from agent_ledger.core import SmartLedger, Task, TaskType
        import hartos.lifecycle_hooks as L
        import tempfile
        self._dir = tempfile.mkdtemp(prefix='restart_reply_')
        self.led = SmartLedger(agent_id='p7', session_id='u7_p7_1', ledger_dir=self._dir)
        for n in (1, 2):
            self.led.tasks[f'action_{n}'] = Task(task_id=f'action_{n}', description=f'a{n}',
                                                 task_type=TaskType.PRE_ASSIGNED)
        L.register_ledger_for_session(self._UP, self.led)
        with L._state_lock:
            L.action_states.pop(self._UP, None)

    def teardown_method(self):
        import shutil
        import hartos.lifecycle_hooks as L
        from hartos import create_recipe as cr
        with L._state_lock:
            L.action_states.pop(self._UP, None)
            L._ledger_registry.pop(self._UP, None)
        for cache in (cr.user_tasks, cr.recipe_for_persona):
            try:
                del cache[self._UP]
            except KeyError:
                pass
        shutil.rmtree(self._dir, ignore_errors=True)

    def _wait(self, action, asked_in):
        import hartos.lifecycle_hooks as L
        assert L.safe_set_state(self._UP, action, L.ActionState.IN_PROGRESS, 'start') is True
        assert L.mark_action_waiting_for_user(self._UP, action, "need the child's word",
                                              flow=0, asked_in=asked_in) is True

    def test_with_the_session_but_no_flag_the_ledgers_wait_is_seen(self):
        from types import SimpleNamespace
        from hartos import create_recipe as cr
        self._wait(2, '1762822187123')
        cr.user_tasks[self._UP] = SimpleNamespace(current_action=2)
        cr.recipe_for_persona[self._UP] = 0
        assert cr.build_holds_a_step_for_the_person('u7', 'p7') is True

    def test_after_a_restart_the_saved_place_and_the_ledger_say_it(self, monkeypatch):
        """No session at all: where the build stands comes from its saved
        progress (detect_and_resume_progress, stood in here; it scans the
        prompts folder), the wait from the ledger."""
        from hartos import create_recipe as cr
        self._wait(2, '1762822187123')
        monkeypatch.setattr(cr, 'detect_and_resume_progress',
                            lambda pid, up: (0, 2, {}, []))
        assert self._UP not in cr.user_tasks
        assert cr.build_holds_a_step_for_the_person('u7', 'p7') is True

    def test_a_wait_nobody_was_asked_about_is_not_a_held_step(self, monkeypatch):
        """A goal's turn parked the step: blocked, but no ask recorded."""
        from hartos import create_recipe as cr
        self._wait(2, 'daemon_goal-7')
        monkeypatch.setattr(cr, 'detect_and_resume_progress',
                            lambda pid, up: (0, 2, {}, []))
        assert cr.build_holds_a_step_for_the_person('u7', 'p7') is False

    def test_a_wait_on_another_action_is_not_this_step(self, monkeypatch):
        from hartos import create_recipe as cr
        self._wait(1, '1762822187123')
        monkeypatch.setattr(cr, 'detect_and_resume_progress',
                            lambda pid, up: (0, 2, {}, []))
        assert cr.build_holds_a_step_for_the_person('u7', 'p7') is False


def test_the_saved_build_input_ages_out_with_the_agents_build_state():
    """Review of 49eee800d (minor): resume_build_input was never evicted.
    The REAL _cleanup_stale_agents, on an agent idle past _AGENT_TTL."""
    import time as _time
    tree = ast.parse(io.open(_HIE, encoding='utf-8', errors='replace').read())
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == '_cleanup_stale_agents']
    assert len(fns) == 1
    ns = {'time': _time, '_AGENT_TTL': 3600,
          '_agent_timestamps': {'u1_4242': _time.time() - 7200, 'u2_4242': _time.time()},
          'review_agents': {'u1_4242': True, 'u2_4242': True},
          'conversation_agent': {'u1_4242': False, 'u2_4242': False},
          'resume_build_input': {'u1_4242': {'input': _GOAL, 'label': 'x'},
                                 'u2_4242': {'input': _GOAL, 'label': 'x'}},
          '_gather_turn_counts': {}, '_gather_reviews_shown': {},
          '_gather_failures': {}}
    module = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, _HIE, 'exec'), ns)
    ns['_cleanup_stale_agents']()
    assert 'u1_4242' not in ns['resume_build_input'], ns['resume_build_input']
    assert 'u2_4242' in ns['resume_build_input'], 'a live build lost its input'

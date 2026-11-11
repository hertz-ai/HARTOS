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
  * The person is told plainly that the agent is not finished yet and that
    its build is being completed.  Their message is not used for the build.
  * When recipe() finishes, the existing ready reply is unchanged.

hart_intelligence_entry cannot be imported in the test venv (it starts the
whole backend), so the REAL block is taken from the source by AST and run
with the names it reads bound to fakes, as
tests/unit/test_yield_pauses_every_agent.py does for the autonomous create.
"""
import ast
import functools
import io
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


def _turn(recipe_reply, config=None):
    """Run the block for a person's turn on agent _PID, whose config is
    `config` and whose flow-0 recipe is missing.  Returns
    (reply text, reply payload, the calls recipe() received)."""
    if config is None:
        config = {'status': 'completed', 'name': _LABEL,
                  'agent_name': 'speech.buddy', 'goal': _GOAL,
                  'personas': [{'name': 'Coach', 'description': 'speech coach'}],
                  'flows': [{'flow_name': 'main', 'persona': 'Coach',
                             'actions': ['Listen to the child say a sound',
                                         'Praise the attempt'],
                             'sub_goal': 'one practice round'}],
                  'prompt_id': _PID, 'creator_user_id': 'u1'}
    calls = []

    def _recipe(*a, **k):
        calls.append((a, k))
        return recipe_reply

    ns = {
        '_flow_recipe_exists': lambda pid: False,
        'prompt_id': _PID, 'prompt': _TURN, 'created_json': config,
        'user_id': 'u1', 'file_id': None, 'request_id': 'req-person-1',
        '_ak': f'u1_{_PID}', 'app': MagicMock(),
        '_get_user_lock': lambda uid: threading.Lock(),
        'review_agents': {}, 'conversation_agent': {},
        '_touch_agent_timestamp': lambda *a: None,
        'recipe': _recipe,
        '_create_social_agent_from_prompt': lambda *a: None,
        '_record_lifecycle': lambda *a, **k: None,
        '_chat_reply': lambda uid, rid, text, **payload: (text, payload),
    }
    exec(_unfinished_agent_block(), ns)
    text, payload = ns['_unfinished_turn']()
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

"""A self-heal agent gets the backend repair tools its prompt tells it to call.

Measured live 13 Nov 2026 on the owner's desktop (Nunba build 75): a
capability-setup consent raised a ``self_heal`` goal for the ``mms_tts``
backend.  Its prompt (goal_manager._build_self_heal_prompt, backend branch)
says "call the repair tool FIRST: repair_backend_venv(backend_name='mms_tts')".
The agent's action failed at 18:15:43 IST with "the tool 'repair_backend_venv'
does not exist" and was retried 148 times.  BACKEND_REPAIR_TOOLS
(integrations/coding_agent/backend_repair_tools.py) was consumed only by the
MCP bridge, which serves outside callers; in-process agents get a tool family
only through the Tier-2 goal-gated loaders in create_recipe and reuse_recipe,
keyed off marketing_tools.detect_goal_tags.

What this pins, behaviourally:
  * the one resolver both constructors use (resolve_goal_tags, held to that
    by test_tool_registration_parity) tags the real self-heal prompt, and the
    TTS provisioning seed, 'backend_repair', and nothing else does;
  * each recipe's own Tier-2 block (cut out of the file with ast and run with
    recording agents, since the constructors around it cannot be driven in a
    unit test) attaches NO repair tool, for that tag or any other.

Why the second point changed (review of ea8e9b3a4, REJECTED 14 Nov 2026):
8e944550b attached them here, and ea8e9b3a4 then put them in the
ServiceToolRegistry, tagged 'backend_repair', so the per-turn attach both
pipelines run could reach CREATE, whose build-time gate resolves the record's
stored tags and gave the live self-heal agent goal_tags=[].  On a REUSE build
whose goal names the tool, the Tier-1 filter kept the registry copies and this
block then replaced them on the Helper: one tool, two attach paths, two
schemas.  The registry path is the one that reaches a live turn on both
pipelines, so it is the only one
(test_a_self_heal_turn_attaches_the_repair_tool pins it attaching).
"""
import ast
import logging
import os
from types import SimpleNamespace

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
LEGS = ['hartos/create_recipe.py', 'hartos/reuse_recipe.py']
REPAIR_NAMES = {'repair_backend_venv', 'next_tts_venv_to_provision'}
LOG = logging.getLogger('test.self_heal_backend_repair')


def _self_heal_prompt(backend='mms_tts'):
    """The prompt the live goal carried: error_advice's self_heal config for
    a tts.probe failure whose context names the backend."""
    from integrations.agent_engine.goal_manager import _build_self_heal_prompt
    goal = {
        'id': 'g-live-shape',
        'title': 'Self-heal: tts.probe (RuntimeError)',
        'description': ('A high-severity tts.probe failure escaped the '
                        'deterministic recovery loop.  Investigate and '
                        'remediate.'),
        'goal_type': 'self_heal',
        'config': {
            'exc_type': 'RuntimeError',
            'source_module': 'tts/tts_engine.py',
            'source_function': 'synthesize',
            'occurrence_count': 1,
            'sample_traceback': 'RuntimeError: worker did not start in 120.0s',
            'category': 'tts.probe',
            'severity': 'high',
            'context': {'backend': backend},
        },
    }
    return _build_self_heal_prompt(goal)


def _provision_seed_text():
    from integrations.agent_engine.goal_seeding import SEED_BOOTSTRAP_GOALS
    seed = next(s for s in SEED_BOOTSTRAP_GOALS
                if s.get('slug') == 'bootstrap_provision_tts_venvs')
    return f"{seed['title']}\n{seed['description']}"


MARKETING_GOAL = 'Run a launch campaign for our new app on social media'
PLAIN_CHAT = 'Say good morning to me'


class _RecordingAgent:
    """Records what an autogen agent is given: schema and execution maps."""

    def __init__(self):
        self.llm = {}
        self.exec = {}

    def register_for_llm(self, name=None, description=None, **_kw):
        def deco(func):
            self.llm[name] = func
            return func
        return deco

    def register_for_execution(self, name=None, **_kw):
        def deco(func):
            self.exec[name] = func
            return func
        return deco


def _is_tag_branch(node, tag):
    t = getattr(node, 'test', None)
    return (isinstance(node, ast.If) and isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Constant) and t.left.value == tag
            and isinstance(t.ops[0], ast.In)
            and isinstance(t.comparators[0], ast.Name)
            and t.comparators[0].id == 'goal_tags')


def _tier2_block(leg):
    """The recipe's Tier-2 family loader: the one try block whose body holds
    the "if 'news' in goal_tags" branch, as compiled code."""
    path = os.path.join(REPO, leg)
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
              and any(_is_tag_branch(b, 'news') for b in n.body)]
    assert len(blocks) == 1, f'{leg}: expected one Tier-2 try block'
    module = ast.Module(body=blocks, type_ignores=[])
    return compile(module, path, 'exec')


def _attach(leg, text):
    """Run ``leg``'s real Tier-2 block for a goal whose words are ``text``,
    with goal_tags from the resolver both constructors bind it from."""
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    helper, assistant, executor = (_RecordingAgent(), _RecordingAgent(),
                                   _RecordingAgent())
    ns = {
        'goal_tags': resolve_goal_tags(None, text),
        'helper': helper, 'assistant': assistant, 'executor': executor,
        'user_id': 'owner-desktop',
        'tool_logger': LOG,                          # create_recipe's logger
        'current_app': SimpleNamespace(logger=LOG),  # reuse_recipe's logger
    }
    exec(_tier2_block(leg), ns)
    return helper, assistant, executor


def test_the_live_prompt_shape_names_the_repair_tool():
    """Anti-vacuity: the text the attach tests feed is the backend branch of
    the real prompt builder, the one that tells the agent to call the tool."""
    prompt = _self_heal_prompt()
    assert "repair_backend_venv(backend_name='mms_tts')" in prompt


class TestTheTagComesFromTheToolName:
    def test_self_heal_prompt_is_tagged_backend_repair(self):
        from integrations.agent_engine.marketing_tools import resolve_goal_tags
        assert 'backend_repair' in resolve_goal_tags(None, _self_heal_prompt())

    def test_provision_seed_is_tagged_backend_repair(self):
        from integrations.agent_engine.marketing_tools import detect_goal_tags
        assert 'backend_repair' in detect_goal_tags(_provision_seed_text())

    @pytest.mark.parametrize('text', [
        MARKETING_GOAL, PLAIN_CHAT,
        'Fix the RuntimeError in tts_engine.py and open a pull request',
        'my text to speech backend sounds robotic',
    ])
    def test_other_goals_are_not(self, text):
        from integrations.agent_engine.marketing_tools import detect_goal_tags
        assert 'backend_repair' not in detect_goal_tags(text)


@pytest.mark.parametrize('text', ['self_heal', 'provision_seed'])
@pytest.mark.parametrize('leg', LEGS)
def test_the_tier2_block_leaves_the_repair_tools_to_the_registry(
        leg, text, caplog):
    """One attach path: a goal tagged 'backend_repair' gets the repair tools
    from the registry (BackendRepairTool, through the Tier-1 filter and the
    per-turn attach), never a second copy with a second schema from here."""
    from integrations.agent_engine.marketing_tools import resolve_goal_tags
    words = (_self_heal_prompt() if text == 'self_heal'
             else _provision_seed_text())
    assert 'backend_repair' in resolve_goal_tags(None, words)
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        helper, assistant, executor = _attach(leg, words)
    assert not [r for r in caplog.records if 'FAILED' in r.getMessage()], (
        f'{leg}: the Tier-2 block raised: {caplog.text}')
    attached = (set(helper.llm) | set(assistant.llm) | set(assistant.exec)
                | set(executor.exec))
    assert not (REPAIR_NAMES & attached), (
        f'{leg}: the Tier-2 block attached {sorted(REPAIR_NAMES & attached)} '
        f'beside the registry copies')


@pytest.mark.parametrize('leg', LEGS)
def test_a_marketing_goal_does_not_get_the_repair_tools(leg):
    helper, assistant, executor = _attach(leg, MARKETING_GOAL)
    # Anti-vacuity: the block ran and gave this goal its own family.
    assert helper.llm, f'{leg}: the marketing family did not attach either'
    attached = set(helper.llm) | set(assistant.llm) | set(executor.exec)
    assert not (REPAIR_NAMES & attached)


@pytest.mark.parametrize('leg', LEGS)
def test_a_plain_chat_gets_no_repair_tools(leg):
    helper, assistant, executor = _attach(leg, PLAIN_CHAT)
    attached = set(helper.llm) | set(assistant.llm) | set(executor.exec)
    assert not (REPAIR_NAMES & attached)

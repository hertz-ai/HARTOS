"""A step that names a tool by one of its names takes a receipt from the other.

WHAT WENT WRONG (central, build 641d7e289, 2026-11-20, goal 09bbc67a)
  The image step was written "text_2_image: ..." and the Assistant called
  txt2img, which answered its own finish-without refusal.  The planner is
  taught text_2_image (hartos/gather_agentdetails.py "generate an image ->
  text_2_image", goal_manager's marketing plan, goal_seeding), and it is a
  real tool: core.agent_tools registers text_2_image, and txt2img as its
  alias over the same work; CREATE's main leg registers txt2img
  (MAIN_LEG_CORE_TOOLS) and keeps text_2_image attachable by name
  (_hart_core_tools), REUSE attaches it by name.  Every gate compared the
  name that ran with the names the step wrote, by spelling: measured on
  59ae091a with central's tool setup, the CREATE completion gate answered
  force_completion to a completed verdict citing the txt2img receipt, and
  REUSE's fabrication gate called text_2_image unrun.

THE RULE (core.constants.TOOL_NAME_ALIASES + tool_name_in, one table)
  - a step naming text_2_image accepts txt2img's receipt, and the other way
    round; likewise img2txt / get_text_from_image and get_data_from_memory /
    get_data_by_key: names core.agent_tools registers over one tool;
  - CREATE's completion gate, REUSE's fabrication gate and receipt finder,
    and CREATE's trace banker all read it;
  - "named" does not widen: a txt2img receipt never satisfies a step that
    names Generate_video.

    python -m pytest tests/unit/test_a_step_named_by_one_name_takes_the_other_names_receipt.py -q
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from core.tool_refusal import image_not_made

IMAGE_STEP = 'text_2_image: a shopper in her kitchen for the campaign'
VIDEO_STEP = 'Generate_video: make a 10 second clip of a shopper in her kitchen'
IMAGE_URL = 'https://images.example.invalid/shopper_kitchen.png'


def _txt2img_refusal():
    return image_not_made('txt2img', 'it is unreachable or busy')


def _f(**_kw):
    return None


# ── CREATE: the completion gate, with central's tool setup ────────────

def _central_create_agents():
    """CREATE's main leg: txt2img registered (MAIN_LEG_CORE_TOOLS), the full
    closure list, text_2_image included, attachable by name
    (create_recipe: assistant._hart_core_tools = core_tools)."""
    helper = SimpleNamespace(name='Helper', llm_config=None,
                             _function_map={'txt2img': _f,
                                            'save_data_in_memory': _f})
    assistant = SimpleNamespace(
        name='Assistant', llm_config=None, _function_map={'txt2img': _f},
        _hart_core_tools=[('text_2_image', '', _f), ('txt2img', '', _f),
                          ('Generate_video', '', _f)])
    return [helper, assistant]


@pytest.fixture()
def create_case():
    from tests.unit.test_a_step_its_tool_lets_finish_completes_in_create import (
        _CompletionCase)
    made = []

    def _make(step, body):
        case = _CompletionCase(step, body, 'txt2img')
        case.h.gc.agents = _central_create_agents()
        made.append(case)
        return case
    yield _make
    for case in made:
        case.close()


def test_create_a_text_2_image_step_completes_on_txt2img_refusal(create_case):
    case = create_case(IMAGE_STEP, _txt2img_refusal())
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_fallback', out
    assert case.state() == case.S.COMPLETED


def test_create_a_generate_video_step_refuses_a_txt2img_receipt(create_case):
    case = create_case(VIDEO_STEP, IMAGE_URL)
    out = case.verdict('completed',
                       evidence={'message_index': 2, 'kind': 'tool_receipt'})
    assert out['action'] == 'force_completion', out
    assert case.state() != case.S.COMPLETED


# ── REUSE: the fabrication gate and the receipt finder ────────────────

class _Task:
    def __init__(self, text):
        self._text = text
        self.evidence_seen_call_ids = set()

    def get_action(self, idx):
        return self._text


def _reuse_step(step, body):
    """A REUSE turn: txt2img registered on the main leg, text_2_image and
    Generate_video attachable by name; the Helper called txt2img."""
    msgs = [
        {'role': 'user', 'name': 'ChatInstructor',
         'content': f'Perform this action -> Action #1: {step}'},
        {'role': 'assistant', 'name': 'Helper', 'content': None,
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'txt2img', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Assistant', 'content': body,
         'tool_responses': [{'tool_call_id': 'c1', 'role': 'tool',
                             'content': body}]},
    ]
    agents = []
    for name in ('Helper', 'Assistant'):
        agent = SimpleNamespace(
            name=name, _function_map={'txt2img': _f},
            llm_config={'tools': [{'function': {'name': 'txt2img'}}]},
            _oai_messages={})
        agents.append(agent)
    agents[1]._hart_core_tools = [('text_2_image', '', _f), ('txt2img', '', _f),
                                  ('Generate_video', '', _f)]
    return SimpleNamespace(messages=msgs, agents=agents), agents


@pytest.fixture()
def reuse(monkeypatch):
    from hartos import reuse_recipe

    def _with(step):
        monkeypatch.setattr(reuse_recipe, '_reuse_person_turn', lambda *_a: False)
        monkeypatch.setitem(reuse_recipe.user_tasks, 'u_alias', _Task(step))
        return reuse_recipe
    return _with


def test_reuse_a_text_2_image_step_ran_as_txt2img_is_not_fabricated(reuse):
    rr = reuse(IMAGE_STEP)
    gc, agents = _reuse_step(IMAGE_STEP, _txt2img_refusal())
    assert rr._reuse_fabricated_tools('u_alias', 1, gc, agents) == []


def test_reuse_a_text_2_image_step_finds_the_txt2img_receipt(reuse):
    rr = reuse(IMAGE_STEP)
    gc, _agents = _reuse_step(IMAGE_STEP, _txt2img_refusal())
    ev = rr._reuse_completion_evidence('u_alias', 1, gc)
    assert ev and ev.get('kind') == 'tool_receipt', ev


def test_reuse_a_generate_video_step_is_not_done_by_txt2img(reuse):
    rr = reuse(VIDEO_STEP)
    gc, agents = _reuse_step(VIDEO_STEP, IMAGE_URL)
    assert rr._reuse_fabricated_tools('u_alias', 1, gc, agents) == [
        'Generate_video']
    assert rr._reuse_completion_evidence('u_alias', 1, gc) is None


# ── the table names only aliases the core tools really register ──────

def test_every_alias_and_its_tool_are_registered_core_tools():
    from core.constants import TOOL_NAME_ALIASES
    from tests.unit.test_a_memory_or_image_step_can_move_on import _tools
    tools = _tools(helper_fun=mock.Mock())
    assert TOOL_NAME_ALIASES
    for alias, tool in TOOL_NAME_ALIASES.items():
        assert alias in tools and tool in tools, (alias, tool)

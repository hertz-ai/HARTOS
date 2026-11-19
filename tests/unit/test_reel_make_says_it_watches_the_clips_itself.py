"""reel_make says that it watches the clips itself.

Live 2026-11-17 14:50-15:02 IST: the Reel Studio agent (342) spent 12 minutes
on its plan step "Extract the key visual elements and transitions from each
clip", sent 7 ask cards that were refused, handed the step to its owner, and
never called reel_make.  reel_make already does that step: its pipeline
watches every clip (media/analyze.py analyze_clip, "Watching clip N of M"),
picks the best moments with the vision model (media/keyframes.py
pick_keyframes, then media/moments.py score_moments) and plans the cuts.

The model and the plan writer see one line of reel_make: the first line of
its docstring (tools._describe, read by register_reel_studio_tools for the
schema and by describe_reel_tools for gather_agentdetails.family_tool_catalog).
That line said only that it makes the reel and sends it, so nothing told the
agent that a step which looks at the clips first is reel_make's own work.

Nothing here runs a reel: the tool closures are only read.

    python -m pytest tests/unit/test_reel_make_says_it_watches_the_clips_itself.py -q --noconftest
"""
from unittest.mock import patch

import pytest

from integrations.reel_studio import tools as reel_tools

#: What reel_make's one line must tell the model and the plan writer.
WATCHES_ITSELF = ('It watches each clip and picks the best moments itself with the '
                  'vision model, so no separate step needs to look at the clips first.')

#: What reel_make does, as its line said before this fix.
MAKES_AND_SENDS = ('Make an Instagram reel video from her attached makeup clips and '
                   'send it to her phone.')


@pytest.fixture(autouse=True)
def _no_real_data_dir(tmp_path, monkeypatch):
    """Any path a module asks for lands in this test's own folder."""
    monkeypatch.setenv('NUNBA_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HARTOS_DATA_DIR', str(tmp_path))
    from core import platform_paths
    platform_paths.reset_cache()
    yield
    platform_paths.reset_cache()


def _described():
    return dict(reel_tools.describe_reel_tools())


class _Seat:
    """Records what register_reel_studio_tools puts on an agent's schema."""

    def __init__(self):
        self.llm = {}

    def register_for_llm(self, name, description):
        self.llm[name] = description
        return lambda f: f

    def register_for_execution(self, name):
        return lambda f: f


def _registered():
    helper, assistant = _Seat(), _Seat()
    with patch.object(reel_tools, '_inert_here', return_value=False), \
            patch.object(reel_tools, '_first_registration'), \
            patch.object(reel_tools, '_her_agent', return_value=True):
        reel_tools.register_reel_studio_tools(helper, assistant, '10202', prompt_id='342',
                                              stored_tags=['reel_studio'])
    return helper, assistant


# ── the defect: the one line never said reel_make watches the clips ──────────

def test_the_plan_writer_reads_that_reel_make_watches_the_clips_itself():
    assert WATCHES_ITSELF in _described()['reel_make']


def test_her_agent_reads_that_reel_make_watches_the_clips_itself():
    helper, assistant = _registered()
    assert WATCHES_ITSELF in helper.llm['reel_make']
    assert WATCHES_ITSELF in assistant.llm['reel_make']


# ── guards: what the line already said and how it is read stay the same ──────

def test_the_line_still_says_what_reel_make_makes_and_where_it_goes():
    assert _described()['reel_make'].startswith(MAKES_AND_SENDS)


def test_the_schema_and_the_plan_writer_read_the_same_line():
    helper, assistant = _registered()
    described = _described()
    assert helper.llm == assistant.llm == described
    assert set(described) == set(reel_tools.REEL_TOOL_NAMES)


def test_every_reel_line_is_one_line_with_no_braces():
    """The autonomous plan writer's system message is an f-string, so a brace
    in a tool line would break it; a line break would cut the line short."""
    for name, line in reel_tools.describe_reel_tools():
        assert line and '\n' not in line, name
        assert '{' not in line and '}' not in line, name


def test_the_docstring_still_says_it_keeps_her_filmed_order():
    for name, func in reel_tools.build_reel_tools('', prompt_id=None):
        if name == 'reel_make':
            assert 'in the order she filmed them' in ' '.join(func.__doc__.split())
            break
    else:
        pytest.fail('build_reel_tools built no reel_make')

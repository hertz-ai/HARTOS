"""Only her Reel Studio agent can be given a reel tool by request_tools
(plan WS-8b, critique finding 2; narrowed by critic report item 2.6).

``request_tools`` -- the never-say-unavailable escape -- attaches whatever its
need text matches, including the tools the Assistant can run but the Helper
no longer sees (discover_and_attach's third source, matched on the first line
of their docstrings).  The reel family used to be registered for execution on
EVERY agent so that request_tools could find it there, and so any agent found
it for needs that have nothing to do with her reels: measured (critic report
2.6) a "trending news today" request attached reel_trending_songs with
"Immediately CALL", and once a watchlist exists that call can raise the
research-account consent card.

Now the family is registered only on her Reel Studio agent: an agent whose
record STORES reel_studio (only her explicit creation hint stamps it,
amendment A8 c), on a node where the tag is live (never central, A8 d).
There, request_tools gives it back whenever the schema fit has deferred it,
so "make a reel from my videos" keeps working for her.  Any other agent has
no reel tool to find -- not for a news need, not for a reel need, and not
when its goal merely SAYS "edit my videos": detection never makes an agent
hers (A8 b).  On the central node there is nothing to find either.

The agents are built by each pipeline's real constructor (the ``seats``
harness of test_reel_tools_wired_both_pipelines.py), and the request_tools
called is the one the constructor registered.

    python -m pytest tests/unit/test_reel_tools_discoverable.py -q -p no:cacheprovider
"""
import pytest

from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, reel_names, runs, schema, seats)


@pytest.fixture
def roomy(monkeypatch):
    """A window the attach fits into, so nothing it attaches is deferred."""
    from core import llm_outbound_logger as wire
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: 1 << 17)


def _request_tools(seats, built, need):
    request_tools = built.assistant.function_map['request_tools']
    with seats.env.app.app_context():
        return request_tools(need)


def _every_reel_name():
    from integrations.reel_studio.tools import REEL_ON_DEMAND_TOOL_NAMES
    return reel_names() | set(REEL_ON_DEMAND_TOOL_NAMES)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_her_agent_gets_reel_make_back_when_she_asks(seats, roomy, pipeline):
    """Her Reel Studio agent: the family deferred off both seats (what the
    live-n_ctx fit does when the window is tight), then "make a reel from my
    videos" puts reel_make back on both."""
    from core.agent_tool_menu import defer_helper_schema
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    for seat in (built.helper, built.assistant):
        defer_helper_schema(seat, reel_names())
    assert 'reel_make' not in schema(built.helper) | schema(built.assistant), (
        'the premise: the family was deferred')
    assert 'reel_make' in runs(built.assistant), 'deferred, never excluded'
    reply = _request_tools(seats, built, 'make a reel from my videos')
    assert 'reel_make' in schema(built.helper), (
        f'{pipeline}: request_tools did not give her agent reel_make back')
    # The need names the tool, so the seat that proposes gets it too.
    assert 'reel_make' in schema(built.assistant), pipeline
    assert 'reel_make' in reply
    attached = next(e['function'] for e in built.helper.llm_config['tools']
                    if e['function']['name'] == 'reel_make')
    doc = (built.assistant.function_map['reel_make'].__doc__ or '').strip()
    assert attached['description'] == doc.split('\n')[0].strip(), (
        'attached with the docstring line discover_and_attach matched on')


@pytest.mark.parametrize('pipeline', PIPELINES)
@pytest.mark.parametrize('need', [
    'trending news today',
    'make a reel from my videos',
    'track these instagram accounts for trending songs',
])
def test_any_other_agent_is_never_offered_a_reel_tool(seats, roomy, pipeline, need):
    built = seats(pipeline)
    every = _every_reel_name()
    for seat in (built.helper, built.assistant, built.executor):
        assert not every & (schema(seat) | runs(seat)), (
            f'{pipeline}: an agent that is not hers carries '
            f'{sorted(every & (schema(seat) | runs(seat)))} on {seat.name}')
    reply = _request_tools(seats, built, need)
    for seat in (built.helper, built.assistant, built.executor):
        assert not every & (schema(seat) | runs(seat)), (
            f'{pipeline}: request_tools({need!r}) offered '
            f'{sorted(every & (schema(seat) | runs(seat)))} to an agent that '
            f'is not her Reel Studio agent')
    assert 'reel_' not in reply, reply


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_an_agent_whose_goal_only_says_edit_my_videos_is_not_hers(seats, roomy, pipeline):
    """Amendment A8 b: detection never makes an agent hers.  Its words
    detect reel_studio (REUSE resolves its goal text; CREATE reads the
    session's task), its record stores nothing, and it gets no reel tool."""
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    goal = 'Edit my videos into reels for my salon'
    assert 'reel_studio' in detect_goal_tags(goal), 'the premise'
    built = seats(pipeline, goal=goal)
    every = _every_reel_name()
    _request_tools(seats, built, 'make a reel from my videos')
    for seat in (built.helper, built.assistant, built.executor):
        assert not every & (schema(seat) | runs(seat)), (pipeline, seat.name)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_on_central_request_tools_never_attaches_a_reel_tool(seats, roomy, monkeypatch,
                                                             pipeline):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    reply = _request_tools(seats, built, 'instagram reels for our product')
    every = _every_reel_name()
    for seat in (built.helper, built.assistant, built.executor):
        assert not every & (schema(seat) | runs(seat)), (pipeline, seat.name)
    assert 'reel_' not in reply

"""An agent that is not her Reel Studio agent can still ask for a reel
(plan WS-8b, critique finding 2).

Every other agent carries the reel family for execution only: the model is
not shown its ~900 tokens of schema, but ``request_tools`` -- the
never-say-unavailable escape -- finds it through discover_and_attach's third
source (the tools the Assistant can run but the Helper no longer sees, matched
on the first line of their docstrings) and attaches it on the spot.  On the
central node there is nothing to find (amendment A8 d): a commerce agent there
asking about "instagram reels for our lash extensions" never gets reel_make.

The agents are built by each pipeline's real constructor (the ``seats``
harness of test_reel_tools_wired_both_pipelines.py), and the request_tools
called is the one the constructor registered.

    python -m pytest tests/unit/test_reel_tools_discoverable.py -q -p no:cacheprovider
"""
import pytest

from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    PIPELINES, reel_names, runs, schema, seats)


@pytest.fixture
def roomy(monkeypatch):
    """A window the attach fits into, so nothing it attaches is deferred."""
    from core import llm_outbound_logger as wire
    monkeypatch.setattr(wire, '_get_budget_per_slot', lambda: 1 << 17)


def _request_tools(seats, built, need):
    request_tools = built.assistant.function_map['request_tools']
    with seats.env.app.app_context():
        return request_tools(need)


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_an_agent_without_the_tag_gets_reel_make_when_it_asks(seats, roomy, pipeline):
    built = seats(pipeline)
    assert 'reel_make' not in schema(built.helper) | schema(built.assistant), (
        'the premise: this agent is not shown the reel family')
    reply = _request_tools(seats, built, 'make a reel from my videos')
    assert 'reel_make' in schema(built.helper), (
        f'{pipeline}: request_tools found no reel_make to attach: the family '
        f'is not registered for execution on this agent')
    # The need names the tool, so the seat that proposes gets it too.
    assert 'reel_make' in schema(built.assistant), pipeline
    assert 'reel_make' in reply
    attached = next(e['function'] for e in built.helper.llm_config['tools']
                    if e['function']['name'] == 'reel_make')
    doc = (built.assistant.function_map['reel_make'].__doc__ or '').strip()
    assert attached['description'] == doc.split('\n')[0].strip(), (
        'attached with the docstring line discover_and_attach matched on')


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_on_central_request_tools_never_attaches_a_reel_tool(seats, roomy, monkeypatch,
                                                             pipeline):
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    built = seats(pipeline)
    reply = _request_tools(seats, built, 'instagram reels for our lash extensions')
    reel = reel_names()
    for seat in (built.helper, built.assistant):
        assert not reel & (schema(seat) | runs(seat)), (pipeline, seat.name)
    assert 'reel_' not in reply

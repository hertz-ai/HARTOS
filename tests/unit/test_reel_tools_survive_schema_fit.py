"""Her reel family survives REUSE's per-action schema fit (plan WS-8b,
critique finding 7).

``reuse_recipe._attach_named_tools_for_action`` is the one per-turn door every
REUSE turn and every advance goes through, and its ``_fit`` reconciles both
seats' tool schemas with the live n_ctx (core.agent_tools.fit_schema_to_ctx):
anything not protected may be deferred when the window is tight, and the reel
family registers last, so it is the first to go.  Most of her turns are REUSE
turns and reel_make is her agent's whole action, so when ``reel_studio`` is
unlocked for the session the fit keeps REEL_TOOL_NAMES -- the ones this agent
can run, which on the central node is none (amendment A8 d: there no reel
tool is registered, and a turn that says "edit my clips" does not unlock the
tag either, since attach_for_turn resolves turns through resolve_goal_tags).

The agents are built by the real REUSE constructor (the ``seats`` harness of
test_reel_tools_wired_both_pipelines.py); the live window is a stub
(``schema_token_room``) sized so that only the protected tools fit, which
forces the deferral this file is about.

    python -m pytest tests/unit/test_reel_tools_survive_schema_fit.py -q -p no:cacheprovider
"""
import pytest

from tests.unit.test_create_loop_end_to_end import create_env  # noqa: F401  (fixture)
from tests.unit.test_reel_tools_wired_both_pipelines import (  # noqa: F401  (fixture)
    HER_GOAL, PIPELINES, UP, reel_names, runs, schema, seats)

#: What the door protects whatever the session: the escape that re-arms a
#: deferral, the delivery tool, and the tool the action's recipe names (the
#: harness recipe names google_search).
ALWAYS_KEPT = {'request_tools', 'send_message_to_user', 'google_search'}


def _entries(agent, names):
    return [e for e in agent.llm_config['tools']
            if (e.get('function') or {}).get('name') in names]


def _tokens(entries):
    from core.llm_outbound_logger import _schema_tokens
    return _schema_tokens({'tools': entries})


def _cost_to_keep(agent, names):
    """The window fit_schema_to_ctx needs to keep ``names`` on ``agent``: its
    first pass adds entries one at a time and its second settles the
    assembled block, so both counts must fit.  (Measured on these seats the
    per-entry sum is the larger: 1254 against 1245 assembled.)"""
    entries = _entries(agent, names)
    return max(_tokens(entries), sum(_tokens([e]) for e in entries))


@pytest.fixture
def door(seats, monkeypatch):
    """``open(goal_tags, room_for)`` builds the agent with REUSE, makes it the
    session the door serves (as chat_agent does), pins the live window to just
    the cost of ``room_for`` on the fuller seat, and returns a callable that
    runs the door once."""
    from core import llm_outbound_logger as wire

    def open_(goal_tags, room_for):
        built = seats('reuse', goal_tags=goal_tags, goal=HER_GOAL)
        built.module.user_agents[UP] = built.built
        room = max(_cost_to_keep(seat, room_for)
                   for seat in (built.helper, built.assistant)) + 4
        monkeypatch.setattr(wire, 'schema_token_room', lambda: room)

        def run():
            with seats.env.app.app_context():
                return built.module._attach_named_tools_for_action(UP)
        built.run = run
        built.room = room
        return built
    return open_


def test_her_reel_family_stays_on_both_seats_through_the_fit(door):
    reel = reel_names()
    built = door(['reel_studio'], ALWAYS_KEPT | reel)
    assert reel <= schema(built.helper) and reel <= schema(built.assistant)
    before = schema(built.helper)
    built.run()
    assert schema(built.helper) < before, (
        'the stub window did not force a deferral: this test proves nothing')
    assert reel <= schema(built.helper), (
        f'the fit deferred her reel tools from the Helper: '
        f'{sorted(reel - schema(built.helper))}')
    assert reel <= schema(built.assistant), (
        f'the fit deferred her reel tools from the Assistant: '
        f'{sorted(reel - schema(built.assistant))}')
    # The action's own tool and the escape stay too: the window held exactly
    # the protected set.
    for seat in (built.helper, built.assistant):
        assert ALWAYS_KEPT <= schema(seat), (seat.name, ALWAYS_KEPT - schema(seat))
    # The protection outlasts this fit: the turn's later fits (request_tools,
    # the per-turn attach) read the same record.
    assert reel <= built.helper._hart_turn_protect
    assert reel <= built.assistant._hart_turn_protect


def test_without_reel_studio_unlocked_the_same_fit_defers_them(door):
    """Anti-vacuity: the same agent, the same window, the tag taken away --
    the family is deferred, so it was the protection that kept it above.
    Deferred, never excluded: the Assistant still runs every one."""
    reel = reel_names()
    built = door(['reel_studio'], ALWAYS_KEPT | reel)
    built.assistant._hart_unlocked_tags.discard('reel_studio')
    built.run()
    assert reel - schema(built.helper), (
        'nothing of the reel family was deferred without the tag')
    assert reel <= runs(built.assistant)


def test_on_central_a_turn_that_says_edit_my_clips_protects_nothing(
        door, monkeypatch):
    """Amendment A8 d.  On central no reel tool is registered, and a turn
    whose words detect reel_studio does not unlock it either:
    attach_for_turn resolves the turn through resolve_goal_tags, which
    drops the tag on central.  Should the tag ever be unlocked there, the
    fit keeps only reel tools this agent can run -- none; so the fit
    protects no reel tool either way."""
    from security import key_delegation
    monkeypatch.setattr(key_delegation, 'get_node_tier', lambda: 'central')
    built = door(['reel_studio'], ALWAYS_KEPT)
    assert 'reel_studio' not in built.assistant._hart_unlocked_tags
    from core.agent_tool_menu import attach_for_turn
    from integrations.agent_engine.marketing_tools import detect_goal_tags
    from integrations.service_tools import service_tool_registry
    turn = 'Please edit my clips for Instagram'
    assert 'reel_studio' in detect_goal_tags(turn), 'the premise: the words detect it'
    new, _n = attach_for_turn(turn, built.helper, built.assistant,
                              service_tool_registry)
    assert 'reel_studio' not in new
    assert 'reel_studio' not in built.assistant._hart_unlocked_tags
    import core.agent_tools as agent_tools
    real_fit = agent_tools.fit_schema_to_ctx
    protected = []

    def spy(agent, protect=(), room=None, *, turn_protect=False):
        protected.append(set(protect or ()))
        return real_fit(agent, protect=protect, room=room,
                        turn_protect=turn_protect)
    monkeypatch.setattr(agent_tools, 'fit_schema_to_ctx', spy)
    built.run()
    assert len(protected) == 2, 'both seats are fitted'
    for names in protected:
        assert not names & reel_names(), (
            f'central protected {sorted(names & reel_names())}')


@pytest.mark.parametrize('pipeline', PIPELINES)
def test_the_family_costs_at_most_900_schema_tokens_per_seat(seats, pipeline):
    """Plan WS-8b step 3: her agent carries the family next to the core tools
    on both seats, measured with the counter fit_schema_to_ctx uses."""
    built = seats(pipeline, goal_tags=['reel_studio'], goal=HER_GOAL)
    reel = reel_names()
    for seat in (built.helper, built.assistant):
        entries = _entries(seat, reel)
        assert {e['function']['name'] for e in entries} == reel, seat.name
        assert _tokens(entries) <= 900, (pipeline, seat.name, _tokens(entries))

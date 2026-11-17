"""Only a person's own turn may look through their camera or screen.

Review of HARTOS 23ccd0eb3 (REJECTED, critical, privacy): once that commit
made the tools read Nunba's caption stamps, two tools of the REUSE agents
handed the owner's camera captions (and, for the search, the screen
captions) to every turn: a background goal turn (daemon_<goal>), a peer
agent's A2A turn (daemon_a2a_<task>, run as the owner, its answer going back
to the peer's node) and an untagged turn.  The reviewer's probe
(rv_66e6_1111/probe_cam_privacy_test.py, part B) showed it; on the parent the
tools raised, so nothing leaked.  The live-frame tool get_user_camera_inp had
no gate either.

Each of the three now asks core.agent_tools.turn_may_see_the_camera (the
sole authority, dispatch.is_genuine_user_request) and answers
camera_refusal otherwise.  Driven like the probe: the REAL REUSE agents
(create_agents_for_user under the create-loop fixture) and the tools called
the way the group calls them, autogen execute_function, with the turn's id
where chat_agent writes it; ids built with core.chat_client.daemon_request_id
as dispatch builds them.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.chat_client import daemon_request_id
from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, UP as CUP, USER_ID, create_env)

PERSON = '1762822187123'          # the Nunba adapter's timestamp id
NOT_A_PERSON = {
    'daemon goal': daemon_request_id('goal-7'),
    'a2a peer': daemon_request_id('a2a_task-9'),   # dynamic_agent_registry's id
    'untagged': '',
}
CAPTION = 'PRIVATE: a child in a red shirt sits at the kitchen table'
SCREEN = 'PRIVATE SCREEN: a bank statement'
LIVE = 'PRIVATE LIVE FRAME: the child is holding a red ball'
TOOLS = {
    'get_user_camera_inp_by_mins': {'minutes': 5},
    'search_visual_history': {'query': '', 'minutes_back': 30, 'channel': 'both'},
    'get_user_camera_inp': {'inp': 'what is the child holding?'},
}


def _rows(n=3):
    """Caption rows stamped the way Nunba stamps them (+00:00, microseconds)."""
    now = datetime.now(timezone.utc)
    return [{'action': f'{CAPTION} #{i}', 'zeroshot_label': 'Video Reasoning',
             'gpt3_label': 'Visual Context',
             'created_date': (now - timedelta(seconds=20 * i)).isoformat(
                 timespec='microseconds')}
            for i in range(n)] + [{
                'action': SCREEN, 'zeroshot_label': 'Screen Reasoning',
                'gpt3_label': 'Screen Context',
                'created_date': (now - timedelta(seconds=10)).isoformat(
                    timespec='microseconds')}]


class _Reply:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _in_a_turn(rid, fn, *args):
    """Run ``fn`` inside a turn whose own id is ``rid``: what with_llm_context
    binds around chat_agent and recipe() (core.llm_outbound_logger
    .request_id_context).  A goal's and a peer's turns run through those
    entries too, with their own ids."""
    from core.llm_outbound_logger import request_id_context
    with request_id_context(rid):
        return fn(*args)


class TestTheRule:

    def test_a_persons_turn_may_look(self):
        from core.agent_tools import turn_may_see_the_camera
        assert _in_a_turn(PERSON, turn_may_see_the_camera) is True

    @pytest.mark.parametrize('kind', list(NOT_A_PERSON))
    def test_no_other_turn_may(self, kind):
        from core.agent_tools import turn_may_see_the_camera
        assert _in_a_turn(NOT_A_PERSON[kind], turn_may_see_the_camera) is False

    def test_outside_any_turn_nothing_may(self):
        """A scheduled run, a relay and a worker thread run in no turn."""
        from core.agent_tools import turn_may_see_the_camera
        assert turn_may_see_the_camera() is False

    def test_an_unreadable_classifier_refuses(self, monkeypatch):
        """Fail closed (review of efd223acf, surviving mutant M2)."""
        import integrations.agent_engine.dispatch as dispatch
        from core.agent_tools import turn_may_see_the_camera

        def boom(_rid):
            raise RuntimeError('classifier down')
        monkeypatch.setattr(dispatch, 'is_genuine_user_request', boom)
        assert _in_a_turn(PERSON, turn_may_see_the_camera) is False

    @pytest.mark.parametrize('rid, says', [
        ('', 'a scheduled run'),
        (NOT_A_PERSON['a2a peer'], "another agent's request"),
        (NOT_A_PERSON['daemon goal'], 'a goal in the background'),
    ])
    def test_the_refusal_names_the_turn_and_says_what_to_do(self, rid, says):
        """Built with core.tool_refusal.refuse (review of efd223acf, finding 4):
        a failed call the gates read, a hint the framework counts, no "try
        later", and what the turn is."""
        from core.agent_tools import camera_refusal
        from core.constants import tool_reply_failed
        from core.tool_refusal import refusal_has_hint
        text = _in_a_turn(rid, camera_refusal, 'search_visual_history')
        assert tool_reply_failed(text), "a refused look must never credit a step"
        assert refusal_has_hint(text), text
        assert says in text and "person's own" in text, text
        assert 'try later' not in text and 'background one' not in text, text


@pytest.fixture
def reuse_agents(create_env, monkeypatch):
    env = create_env
    import hartos.reuse_recipe as rr
    import hartos.helper as h
    import core.llm_outbound_logger as lol
    from apscheduler.schedulers.background import BackgroundScheduler
    from unittest import mock
    monkeypatch.setattr(rr, 'HAS_SIMPLEMEM', False)
    for name in ('user_tasks', 'recipes', 'final_recipe', 'request_id_list',
                 'user_agents', 'time_actions', 'user_ledgers'):
        cache = getattr(rr, name)
        if hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
        cache.clear()
    if getattr(rr, 'get_production_backend', None) is not None:
        from agent_ledger.backends import InMemoryBackend
        monkeypatch.setattr(rr, 'get_production_backend',
                            lambda *a, **k: InMemoryBackend())
    monkeypatch.setattr(lol, 'schema_token_room', lambda: 100_000)
    scheduler = mock.Mock(spec=BackgroundScheduler)
    scheduler.running = True
    monkeypatch.setattr(rr, 'scheduler', scheduler)
    (env.prompts / f'{PROMPT_ID}_0_recipe.json').write_text(json.dumps({
        'status': 'done', 'actions': [{
            'action_id': 1, 'action': 'Tell the person what you can see right now',
            'persona': 'Researcher', 'can_perform_without_user_input': 'yes',
            'recipe': [{'steps': 'look at the camera',
                        'tool_name': 'get_user_camera_inp_by_mins'}]}]}),
        encoding='utf-8')
    rows = _rows()
    monkeypatch.setattr(h, 'get_visual_context_api',
                        lambda user_id, mins=5: 'http://store/get_visual_bymins')
    monkeypatch.setattr(h, 'pooled_request', lambda *a, **k: _Reply(rows))
    # The live frame is filed under the turn's own id (output_images/{user}_{rid}_call.jpg),
    # so a tool that passed another id is a wrong answer (review of efd223acf, mutant M6).
    monkeypatch.setattr(h, 'get_user_camera_inp',
                        lambda inp, uid, rid: LIVE if rid == PERSON else f'WRONG ID {rid!r}')
    with env.app.app_context():
        built = rr.create_agents_for_user(USER_ID, PROMPT_ID)
    rr.user_agents[CUP] = built
    yield env, rr, built
    rr.user_agents.pop(CUP, None)
    rr.request_id_list.pop(CUP, None)


def _holder(built, name):
    """The first built agent whose function map executes ``name``."""
    seen = set()
    for obj in built:
        for agent in (obj.values() if isinstance(obj, dict) else [obj]):
            fm = getattr(agent, '_function_map', None)
            if isinstance(fm, dict) and id(agent) not in seen:
                seen.add(id(agent))
                if name in fm:
                    return agent
    return None


def _call(env, rr, built, tool, rid):
    agent = _holder(built, tool)
    assert agent is not None, f"{tool} is not in any built agent's function map"
    rr.request_id_list[CUP] = rid

    def run():
        with env.app.app_context():
            _ok, result = agent.execute_function(
                {'name': tool, 'arguments': json.dumps(TOOLS[tool])})
        return str(result.get('content'))
    return _in_a_turn(rid, run)


@pytest.mark.parametrize('tool', list(TOOLS))
def test_no_background_turn_sees_the_camera_through_a_tool(reuse_agents, tool):
    """THE DEFECT: RED on 23ccd0eb3 for the two caption tools (the probe's
    table), and for the live frame, which had no gate at all."""
    env, rr, built = reuse_agents
    leaks = {}
    for kind, rid in NOT_A_PERSON.items():
        out = _call(env, rr, built, tool, rid)
        if CAPTION in out or SCREEN in out or LIVE in out:
            leaks[kind] = out[:120]
        else:
            assert "person's own" in out, (
                f"{tool} on a {kind} turn neither leaked nor said why: {out[:160]!r}")
    assert not leaks, f"{tool} handed the owner's camera or screen to: {leaks}"


@pytest.mark.parametrize('tool', list(TOOLS))
def test_the_persons_own_turn_still_sees_it(reuse_agents, tool):
    """Non-regression: the person asking about their own camera is answered."""
    env, rr, built = reuse_agents
    out = _call(env, rr, built, tool, PERSON)
    assert CAPTION in out or LIVE in out, f"{tool} refused the person: {out[:160]!r}"

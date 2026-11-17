"""Only the person's own turn may look through their camera or screen.

Review of HARTOS efd223acf (REJECTED, board NOTE 2026-11-11T14:47:24Z,
review_efd223acf.md, probes rv_efd2/): the camera and screen tools judged
the turn by request_id_list, the session's LAST /chat id, so
  1. a scheduled run (REUSE time_based_execution, also through
     connect_time_main into the main group) read the camera, the screen and
     the live frame whenever a person spoke last, and every scheduled read
     refreshed the cached entry, so the "2 h" never closed;
  2. a goal turn gained the camera once the person's message arrived in the
     middle of it;
  3. the person's own turn lost it once a goal turn started, and was told
     it was a background one.
Now the tools judge the turn by its own id: what with_llm_context binds for
the running chat_agent / recipe() turn (core.llm_outbound_logger
.turn_request_id), '' when no such turn runs.

Driven through the real entries and the real scheduled and relay paths, with
the REUSE agents create_agents_for_user builds; a tool runs the way autogen
runs it, execute_function on the thread that drives the chat.
"""
import json
import threading

import pytest

from core.chat_client import daemon_request_id
from tests.unit.test_create_loop_end_to_end import (  # noqa: F401  (fixture)
    PROMPT_ID, UP as CUP, USER_ID, create_env)
from tests.unit.test_the_camera_tools_answer_only_the_persons_turn import (  # noqa: F401
    CAPTION, LIVE, PERSON, SCREEN, TOOLS, _holder, reuse_agents)


def _look(env, built, tool):
    """Run ``tool`` the way the group runs it, on this thread and context."""
    agent = _holder(built, tool)
    assert agent is not None, f"{tool} is not in any built agent's function map"
    with env.app.app_context():
        _ok, result = agent.execute_function(
            {'name': tool, 'arguments': json.dumps(TOOLS[tool])})
    return str(result.get('content'))


def _saw(out):
    return CAPTION in out or SCREEN in out or LIVE in out


def _refused(out):
    return not _saw(out) and "person's own" in out


@pytest.fixture
def reuse_turn(reuse_agents, monkeypatch):
    """The real REUSE chat_agent on the cached agents, its model round stood
    in for by ``respond``: everything else between the entry and the group's
    chat runs as it does on a person's turn."""
    env, rr, built = reuse_agents
    monkeypatch.setattr(rr, 'clear_action_states', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_settle_person_flow', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_reuse_held_step_gate', lambda *a, **k: None)
    monkeypatch.setattr(rr, '_start_reuse_action', lambda *a, **k: None)
    monkeypatch.setattr(rr, 'get_role', lambda *a, **k: 'role')
    monkeypatch.setitem(rr.user_journey, CUP, 'UseBot')
    monkeypatch.setitem(rr.llm_call_track, CUP, {'count': 0, 'original_prompt': False})
    state = {'respond': None}
    monkeypatch.setattr(rr, 'get_agent_response',
                        lambda *a, **k: state['respond']())

    def turn(request_id, respond):
        state['respond'] = respond
        with env.app.app_context():
            return rr.chat_agent(USER_ID, 'what do you see?', PROMPT_ID, None, request_id)
    return env, rr, built, turn


# ── The person's own turn, through the real entry ───────────────────────────

@pytest.mark.parametrize('tool', list(TOOLS))
def test_the_persons_turn_through_chat_agent_sees_it(reuse_turn, tool):
    """Non-regression: the entry marks its own context and the tool runs in
    it (autogen runs a tool on the chat's thread)."""
    env, rr, built, turn = reuse_turn
    reply = turn(PERSON, lambda: _look(env, built, tool))
    assert _saw(reply), f"{tool} refused the person on their own turn: {reply[:160]!r}"


def test_a_goal_turn_through_the_same_entry_is_still_refused(reuse_turn):
    """The mark is half the rule: a goal's turn runs through chat_agent too."""
    env, rr, built, turn = reuse_turn
    reply = turn(daemon_request_id('goal-7'),
                 lambda: _look(env, built, 'get_user_camera_inp_by_mins'))
    assert _refused(reply), reply[:160]


# ── Runs that record no id of their own, with the person's id cached ────────

@pytest.mark.parametrize('tool', list(TOOLS))
def test_a_scheduled_run_with_the_persons_id_cached_is_refused(reuse_agents, monkeypatch, tool):
    """THE DEFECT: the real REUSE time_based_execution, within the session's
    cache window after the person's message, looked as the person."""
    env, rr, built = reuse_agents
    rr.request_id_list[CUP] = PERSON          # what the person's last turn left
    seen = []
    time_user = built[7]
    monkeypatch.setattr(time_user, 'initiate_chat',
                        lambda *a, **k: seen.append(_look(env, built, tool)))
    with env.app.app_context():
        rr.time_based_execution('Check on the child', USER_ID, PROMPT_ID, 1)
    assert seen, "the scheduled run never reached its chat"
    assert _refused(seen[0]), f"a scheduled run looked as the person: {seen[0][:160]!r}"


def test_a_scheduled_run_on_a_thread_still_naming_the_person_is_refused(reuse_agents, monkeypatch):
    """The thread-local id names the request a thread last served; the tools
    read the running turn's own id, never that (review of efd223acf,
    finding 5, trap a)."""
    from hartos.threadlocal import thread_local_data
    env, rr, built = reuse_agents
    rr.request_id_list[CUP] = PERSON
    seen = []
    monkeypatch.setattr(built[7], 'initiate_chat',
                        lambda *a, **k: seen.append(_look(env, built, 'search_visual_history')))
    thread_local_data.set_request_id(PERSON)
    try:
        with env.app.app_context():
            rr.time_based_execution('Check on the child', USER_ID, PROMPT_ID, 1)
    finally:
        thread_local_data.set_request_id(None)
    assert seen and _refused(seen[0]), seen


def test_the_time_agent_reaching_into_the_main_group_is_refused(reuse_agents, monkeypatch):
    """connect_time_main runs the MAIN group ('reuse', the person's group
    name) from the scheduled run's thread: a group-name rule let it look."""
    env, rr, built = reuse_agents
    rr.request_id_list[CUP] = PERSON
    seen = []
    multi_role, time_user = built[5], built[7]
    monkeypatch.setattr(multi_role, 'initiate_chat',
                        lambda *a, **k: seen.append(
                            _look(env, built, 'get_user_camera_inp_by_mins')))

    def time_chat(*a, **k):
        agent = _holder(built, 'connect_time_main')
        assert agent is not None, 'connect_time_main is not registered'
        agent.execute_function({'name': 'connect_time_main',
                                'arguments': json.dumps({'message': 'look now'})})
    monkeypatch.setattr(time_user, 'initiate_chat', time_chat)
    with env.app.app_context():
        rr.time_based_execution('Check on the child', USER_ID, PROMPT_ID, 1)
    assert seen, "connect_time_main never ran the main group"
    assert _refused(seen[0]), seen[0][:160]


def test_an_agent_to_agent_relay_is_refused(reuse_agents, monkeypatch):
    """crossbar_multiagent relays another agent's message ('Role: <caller
    role>'), on its subscriber's thread, through the main group."""
    env, rr, built = reuse_agents
    rr.request_id_list[CUP] = PERSON
    seen = []
    monkeypatch.setattr(built[5], 'initiate_chat',
                        lambda *a, **k: seen.append(
                            _look(env, built, 'search_visual_history')))
    monkeypatch.setattr(rr, 'send_message_to_user1', lambda *a, **k: None)
    msg = {'user_id': USER_ID, 'caller_user_id': USER_ID, 'caller_prompt_id': PROMPT_ID,
           'caller_role': 'Coach', 'role': 'Helper', 'message': 'what is she doing?'}

    def relay():
        with env.app.app_context():
            try:
                rr.crossbar_multiagent(msg)
            except Exception:       # the relay's own reply handling, not under test
                pass
    t = threading.Thread(target=relay)
    t.start()
    t.join(30)
    assert seen, "the relay never ran the main group"
    assert all(_refused(s) for s in seen), [s[:120] for s in seen]


def test_a_worker_thread_holding_the_persons_id_is_refused(reuse_agents):
    env, rr, built = reuse_agents
    rr.request_id_list[CUP] = PERSON
    seen = []
    t = threading.Thread(target=lambda: seen.append(_look(env, built, 'get_user_camera_inp')))
    t.start()
    t.join(30)
    assert seen and _refused(seen[0]), seen


def test_a_goal_turn_stays_refused_when_the_persons_message_arrives_mid_turn(reuse_turn):
    """Finding 2: the person's chat_agent writes request_id_list while the
    goal's turn is still running."""
    env, rr, built, turn = reuse_turn

    def respond():
        rr.request_id_list[CUP] = PERSON      # the person's /chat arrives
        return _look(env, built, 'search_visual_history')
    reply = turn(daemon_request_id('goal-7'), respond)
    assert _refused(reply), f"the goal's turn gained the camera: {reply[:160]!r}"


def test_the_persons_turn_keeps_the_camera_when_a_goal_turn_starts_mid_turn(reuse_turn):
    """Finding 3: a goal's chat_agent writes request_id_list while the
    person's turn is still running."""
    env, rr, built, turn = reuse_turn

    def respond():
        rr.request_id_list[CUP] = daemon_request_id('goal-7')
        return _look(env, built, 'get_user_camera_inp_by_mins')
    reply = turn(PERSON, respond)
    assert _saw(reply), f"the person's own turn was refused: {reply[:160]!r}"
    assert 'background' not in reply


def test_scheduled_runs_hours_after_the_persons_turn_stay_refused(reuse_turn, monkeypatch):
    """Finding 1, the window: a person's turn, then scheduled runs at +110,
    +220 and +330 minutes.  Each run's cache read kept the person's id fresh
    (TTLCache touches on read), so every run saw everything."""
    import core.session_cache as sc
    env, rr, built, turn = reuse_turn
    turn(PERSON, lambda: 'ok')
    assert rr.request_id_list.get(CUP) == PERSON
    t0 = sc.time.monotonic()
    seen = []
    monkeypatch.setattr(built[7], 'initiate_chat',
                        lambda *a, **k: seen.append(_look(env, built, 'get_user_camera_inp')))
    for minutes in (110, 220, 330):
        monkeypatch.setattr(sc.time, 'monotonic', lambda m=minutes: t0 + m * 60)
        with env.app.app_context():
            rr.time_based_execution('Check on the child', USER_ID, PROMPT_ID, 1)
    assert len(seen) == 3, seen
    assert all(_refused(s) for s in seen), [s[:100] for s in seen]


def test_a_stale_thread_id_does_not_make_an_untagged_turn_the_persons(reuse_turn):
    """with_llm_context binds the thread-local id for a turn called without
    one.  /chat sets that id and nothing cleared it, so a later untagged turn
    on a reused thread could inherit the person's (review of efd223acf,
    finding 5).  The REAL request teardown now clears it."""
    import ast
    import io
    import os as _os
    from hartos.threadlocal import thread_local_data
    env, rr, built, turn = reuse_turn
    hie = _os.path.join(_os.path.dirname(__file__), '..', '..', 'hart_intelligence_entry.py')
    tree = ast.parse(io.open(hie, encoding='utf-8', errors='replace').read())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name == '_a_turns_model_ends_with_its_request']
    assert len(fns) == 1
    fns[0].decorator_list = []
    ns = {'thread_local_data': thread_local_data}
    module = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, hie, 'exec'), ns)
    thread_local_data.set_request_id(PERSON)     # the person's /chat on this thread
    try:
        ns['_a_turns_model_ends_with_its_request']()   # ...and its request ended
        assert thread_local_data.get_request_id() is None
        reply = turn('', lambda: _look(env, built, 'get_user_camera_inp'))
        assert _refused(reply), f"an untagged turn looked as the person: {reply[:160]!r}"
    finally:
        thread_local_data.set_request_id(None)


# ── CREATE ──────────────────────────────────────────────────────────────────

class _Stop(Exception):
    pass


def test_the_create_turn_marks_its_own_context(create_env, monkeypatch):
    """The real CREATE recipe(): inside its body (the first helper it calls)
    a person's id may look and a goal's may not; after it returns, nothing."""
    import hartos.create_recipe as cr
    from hartos import helper as helper_fun
    from core.agent_tools import turn_may_see_the_camera
    seen = []

    def inside():
        seen.append(turn_may_see_the_camera())
        raise _Stop
    monkeypatch.setattr(helper_fun, 'clear_pause_request', inside)
    for rid in (PERSON, daemon_request_id('goal-7')):
        with create_env.app.app_context(), pytest.raises(_Stop):
            cr.recipe(USER_ID, 'hello', PROMPT_ID, None, rid)
    assert seen == [True, False], seen
    assert turn_may_see_the_camera() is False


def test_a_create_scheduled_run_with_the_persons_id_cached_is_refused(create_env, monkeypatch):
    import hartos.create_recipe as cr
    from core.agent_tools import turn_may_see_the_camera
    monkeypatch.setitem(cr.request_id_list, CUP, PERSON)
    monkeypatch.setitem(cr.time_agents, CUP, {})
    seen = []
    monkeypatch.setattr(cr, '_run_time_group',
                        lambda *a, **k: seen.append(turn_may_see_the_camera()))
    with create_env.app.app_context():
        cr.time_based_execution('Check on the child', USER_ID, PROMPT_ID, 1)
    assert seen == [False], seen


def test_the_expert_worker_forgets_the_last_persons_id(monkeypatch):
    """hartos-d9's thread-local audit: the speculative expert pool re-binds
    the originating rid on its pooled worker and never cleared it, so a later
    task with no rid ran on the earlier person's id.  The REAL
    _expert_background_task, its expert leg stood in."""
    import threading
    from integrations.agent_engine import speculative_dispatcher as sd
    from hartos.threadlocal import thread_local_data
    from security.hive_guardrails import HiveCircuitBreaker
    d = sd.SpeculativeDispatcher.__new__(sd.SpeculativeDispatcher)
    d._lock = threading.Lock()
    d._active = {}
    seen = []
    monkeypatch.setattr(d, '_run_collapsed_expert_path',
                        lambda *a, **k: seen.append(thread_local_data.get_request_id()))
    monkeypatch.setattr(sd.SpeculativeDispatcher, '_release_expert_reservation',
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(HiveCircuitBreaker, 'is_halted', classmethod(lambda cls: False))

    def worker():
        d._expert_background_task('s1', 'p', 'f', None, 'u', 'p1', None, 't', request_id=PERSON)
        d._expert_background_task('s2', 'p', 'f', None, 'u', 'p1', None, 't', request_id='')
        seen.append(thread_local_data.get_request_id())
    t = threading.Thread(target=worker)
    t.start()
    t.join(30)
    assert seen == [PERSON, None, None], seen


def test_the_expert_legs_keep_their_lanes(monkeypatch):
    """hartos-d9 (11-11): the lane (day cap, Claude offer) resolves through
    the request id.  A revenue goal's expert leg stays 'revenue' and a
    person's stays 'user' on the pooled worker, also after a task with no id
    ran on it."""
    import threading
    from integrations.agent_engine import speculative_dispatcher as sd
    from core import llm_outbound_logger as lol
    from security.hive_guardrails import HiveCircuitBreaker
    goal_rid = daemon_request_id('goal-revenue-1')
    lol.register_request_lane(goal_rid, lol.LANE_REVENUE)
    d = sd.SpeculativeDispatcher.__new__(sd.SpeculativeDispatcher)
    d._lock = threading.Lock()
    d._active = {}
    lanes = []
    monkeypatch.setattr(d, '_run_collapsed_expert_path',
                        lambda *a, **k: lanes.append(lol.current_lane()))
    monkeypatch.setattr(sd.SpeculativeDispatcher, '_release_expert_reservation',
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(HiveCircuitBreaker, 'is_halted', classmethod(lambda cls: False))

    def worker():
        for rid in (goal_rid, PERSON, '', goal_rid, PERSON):
            d._expert_background_task('s', 'p', 'f', None, 'u', 'p1', None, 't', request_id=rid)
    t = threading.Thread(target=worker)
    t.start()
    t.join(30)
    assert lanes == [lol.LANE_REVENUE, lol.LANE_USER, lol.LANE_DAEMON,
                     lol.LANE_REVENUE, lol.LANE_USER], lanes

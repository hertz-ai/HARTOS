"""A turn runs on the model chosen for it, on its own group's agents (#172).

MEASURED on central (2026-10-18, 10-20, 10-23): a revenue goal's step was
"Handed to the expert model" (Claude, the revenue lane's expert, day cap
$3.30) and the goal's next daemon turn ran with that expert as its
model_config override, yet the turn called openrouter and failed "Reasoning
is mandatory"; api.anthropic.com was called 0 times all day.  The session's
agents had been built on an earlier turn, on the node's default model, and
autogen builds an agent's client once (ConversableAgent._validate_llm_config),
so a cached agent kept that model for every later turn.  Built on an expert
turn, the agents would keep the expert for every ordinary turn after it,
outside the day-cap check.

The first fix (75dc20add) was REJECTED by its review, which measured: a chat
turn switched the model of a scheduled run still going (P1); /chat never
cleared the override, so a scheduled run on a reused thread ran on an earlier
expert turn's model (P2); CREATE's visual run was never bound (P3); one
registry key per session let a REUSE turn hold back CREATE's agents (P4) and
its orphaned-tool-call settle (P5); five bind sites were untested (M5); a
turn whose switch was skipped still ran and was reported nowhere.

Contracts, through the REAL turn functions with REAL autogen agents; only the
LLM call boundary is captured.  Each group chat holds its own agents, as the
builders make them (reuse_recipe.create_agents_for_user builds the main, time
and visual groups from separate agents):
  - every turn that picks up a cached group runs it on its own model, and
    only that group: the CREATE chat turn, visual run and scheduled run; the
    REUSE chat turn (role group and main group), scheduled run, visual run and
    crossbar legs;
  - a turn of one group never switches another group's model (P1) and never
    holds back another group's orphan settle or model (P4, P5);
  - a turn whose group is running another turn on a different model does not
    run: it answers held_for_another_turn_reply, which every dispatcher holds
    for a retry, and the running turn keeps its model; turns asking the same
    model run;
  - a turn's model ends with its request (/chat's teardown), however the
    request ends.
"""
import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

autogen = pytest.importorskip('autogen', reason='autogen not installed')

from hartos import create_recipe as cr  # noqa: E402
from hartos import helper as helper_fun  # noqa: E402
from hartos import reuse_recipe as rr  # noqa: E402
from hartos.helper import Action  # noqa: E402
from hartos.lifecycle_hooks import clear_action_states  # noqa: E402
from hartos.threadlocal import thread_local_data  # noqa: E402

DEFAULT = [{'model': 'free-model', 'api_key': 'k', 'base_url': 'http://127.0.0.1:9/v1'}]
EXPERT = [{'model': 'expert-model', 'api_key': 'k2', 'base_url': 'http://127.0.0.1:9/v2'},
          {'model': 'free-model', 'api_key': 'k', 'base_url': 'http://127.0.0.1:9/v1'}]
UP, USER, PROMPT = '123_456', 123, 456
ON_DEFAULT = ['free-model'] * 3
ON_EXPERT = ['expert-model'] * 3
WAIT = 30


def _probe(text: str) -> str:
    """A registered tool, so the test sees whether binding keeps tools."""
    return text


def _held_reply():
    from core.agent_tools import held_for_another_turn_reply
    return held_for_another_turn_reply()


def _is_held(reply):
    from core.agent_tools import is_held_for_another_turn
    return is_held_for_another_turn(reply)


def _group(prefix, config=DEFAULT):
    """One group chat as the builders make it: its own Assistant and Helper
    (the Helper carrying a tool) and its manager, built on ``config``."""
    llm = {'config_list': list(config), 'cache_seed': None}
    a = autogen.AssistantAgent(f'{prefix}Assistant', llm_config=dict(llm))
    h = autogen.AssistantAgent(f'{prefix}Helper', llm_config=dict(llm))
    h.register_for_llm(name='probe', description='echo')(_probe)
    gc = autogen.GroupChat(agents=[a, h], messages=[], max_round=2)
    m = autogen.GroupChatManager(groupchat=gc, llm_config=dict(llm))
    return SimpleNamespace(a=a, h=h, gc=gc, m=m)


def _models(g):
    return [x.client._config_list[0]['model'] for x in (g.a, g.h, g.m)]


def _recorder(sink, group, gate=None, speaks=False):
    """An initiate_chat that records the models its group runs on, then, with
    ``gate``, holds the FIRST turn open until the test releases it (any later
    caller only records).  ``speaks``: the round leaves a line in the group
    chat, as a real autogen round does; a CREATE turn's pause exit copies
    the group chat into the session (_answer_the_pause), and a session left
    empty sends the next turn down the first-turn branch, never to the call."""
    def initiate(*args, **kwargs):
        sink.append(_models(group))
        if speaks:
            group.gc.messages.append({'role': 'user', 'name': 'User', 'content': 'a round'})
        if gate is not None and not gate.entered.is_set():
            gate.entered.set()
            gate.release.wait(WAIT)
            sink.append(_models(group))
    return initiate


def _gate():
    return SimpleNamespace(entered=threading.Event(), release=threading.Event())


@pytest.fixture(autouse=True)
def no_override():
    thread_local_data.clear_model_config_override()
    yield
    thread_local_data.clear_model_config_override()


def _on_thread(app, fn):
    """Run ``fn`` on its own thread inside an app context, the way a request
    thread runs a turn; returns the started thread."""
    def body():
        thread_local_data.clear_model_config_override()
        with app.app_context():
            fn()
    t = threading.Thread(target=body)
    t.start()
    return t


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in reversed(patches):
        p.stop()


# ── CREATE ─────────────────────────────────────────────────────────────────

def _create_session(mock_flask_app, main_on=DEFAULT, time_on=DEFAULT):
    clear_action_states(UP)
    g, tg = _group('', main_on), _group('Time', time_on)
    seen = {'turn': [], 'visual': [], 'time': []}
    starter, author, time_user = MagicMock(), MagicMock(), MagicMock()
    starter.initiate_chat.side_effect = _recorder(seen['turn'], g, speaks=True)
    author.initiate_chat.side_effect = _recorder(seen['visual'], g)
    time_user.initiate_chat.side_effect = _recorder(seen['time'], tg)
    tg.gc.messages[:] = [{'role': 'user', 'name': 'User', 'content': 'done'}]
    chat_instructor = MagicMock()
    chat_instructor.chat_messages = {}
    agents_object = {'user': starter, 'helper': g.h, 'assistant': g.a,
                     'verify': MagicMock(), 'executor': MagicMock()}
    entry = (author, g.a, MagicMock(), g.gc, g.m, chat_instructor, agents_object)
    step = MagicMock()
    step.get_action_byaction_id.return_value = {
        'action': 'Post the digest', 'can_perform_without_user_input': 'no'}
    patches = [
        patch.object(cr, 'user_tasks', {UP: Action(['Do the step'])}),
        patch.object(cr, 'user_agents', {UP: entry}),
        patch.object(cr, 'time_agents', {UP: {
            'time_group_chat': tg.gc, 'time_user': time_user, 'time_manager': tg.m,
            'chat_instructor1': MagicMock(), 'time_agent': tg.a, 'helper1': tg.h}}),
        patch.object(cr, 'time_actions', {UP: step}),
        patch.object(cr, 'messages', {UP: [{'role': 'user', 'content': 'start'}]}),
        patch.object(cr, 'request_id_list', {UP: 'req-172'}),
        patch.object(cr, 'config_list', list(DEFAULT)),
        patch.object(cr, '_resume_prior_user_input_block', return_value=False),
        patch.object(cr, '_attach_for_create_turn'),
        patch.object(cr, 'get_frame', return_value='frame'),
        patch.object(helper_fun, 'get_visual_context', return_value='ctx'),
        # The model question ends at the captured call: every CREATE turn
        # then leaves through its own pause exit (get_response_group reads
        # pause_requested only AFTER the call, at the top of its loop and
        # after it).  Without this the loop after the call runs on into
        # Redis and the ledger, ~40 s a turn, and a turn on a test's thread
        # outlived the test (measured 2026-10-24: blocked in redis
        # connection._connect when the run ended).
        patch.object(helper_fun, 'pause_requested', return_value=True),
    ]
    _start(patches)
    return SimpleNamespace(app=mock_flask_app, g=g, tg=tg, seen=seen, starter=starter,
                           patches=patches)


@pytest.fixture
def create(mock_flask_app):
    s = _create_session(mock_flask_app)
    try:
        yield s
    finally:
        _stop(s.patches)
        clear_action_states(UP)


@pytest.fixture
def create_built_on_expert(mock_flask_app):
    """A CREATE session whose groups an expert turn built."""
    s = _create_session(mock_flask_app, main_on=EXPERT, time_on=EXPERT)
    try:
        yield s
    finally:
        _stop(s.patches)
        clear_action_states(UP)


class TestCreateTurnsRunOnTheirModel:

    def test_an_expert_turn_on_a_cached_session_calls_the_expert(self, create):
        thread_local_data.set_model_config_override(list(EXPERT))
        cr.get_response_group(USER, 'take the step', PROMPT)
        assert create.seen['turn'] == [ON_EXPERT], (
            'the expert turn ran on the model the cached agents were built on: '
            'central 10-23, 0 api.anthropic.com calls after "Handed to the expert model"')
        assert [t['function']['name'] for t in create.g.h.llm_config['tools']] == ['probe']

    def test_the_next_ordinary_turn_is_back_on_the_default_model(self, create):
        thread_local_data.set_model_config_override(list(EXPERT))
        cr.get_response_group(USER, 'take the step', PROMPT)
        thread_local_data.clear_model_config_override()
        cr.get_response_group(USER, 'and the next', PROMPT)
        assert create.seen['turn'][-1] == ON_DEFAULT, (
            'an ordinary turn after an expert turn stayed on the expert: Claude '
            'spent outside the day-cap check')

    def test_a_visual_run_after_an_expert_turn_is_on_the_default(self, create):
        """P3: visual_execution picked up the session's agents unbound."""
        thread_local_data.set_model_config_override(list(EXPERT))
        cr.get_response_group(USER, 'take the step', PROMPT)
        thread_local_data.clear_model_config_override()
        cr.visual_execution('look', USER, PROMPT)
        assert create.seen['visual'] == [ON_DEFAULT], create.seen

    def test_a_scheduled_run_of_a_group_an_expert_turn_built_is_on_the_default(
            self, create_built_on_expert):
        """create_time_agents builds the time group on the model of the turn
        that builds it (recipe() completing on an expert turn); the scheduled
        runs after it picked the group up as built."""
        cr.time_based_execution('Post the digest', USER, PROMPT, 1)
        assert create_built_on_expert.seen['time'] == [ON_DEFAULT]


class TestACreateTurnWaitsForTheTurnHoldingItsGroup:

    def test_an_ordinary_turn_does_not_run_while_an_expert_turn_runs(self, create, caplog):
        gate = _gate()
        create.starter.initiate_chat.side_effect = _recorder(create.seen['turn'], create.g, gate,
                                                             speaks=True)

        def expert_turn():
            thread_local_data.set_model_config_override(list(EXPERT))
            with cr._turn_in_flight(UP):   # as recipe() marks it
                cr.get_response_group(USER, 'take the step', PROMPT)
        t = _on_thread(create.app, expert_turn)
        assert gate.entered.wait(WAIT)
        try:
            with caplog.at_level(logging.WARNING):
                reply = cr.get_response_group(USER, 'and this', PROMPT)
        finally:
            gate.release.set()
            t.join(WAIT)
        assert create.seen['turn'] == [ON_EXPERT, ON_EXPERT], (
            'the ordinary turn ran on the expert turn\'s agents, or switched them '
            'under it: Claude spent outside the day-cap check')
        assert _is_held(reply), reply
        assert any('another turn' in r.getMessage() and UP in r.getMessage()
                   for r in caplog.records), 'the held turn was not logged'


# ── REUSE ──────────────────────────────────────────────────────────────────

def _reuse_session(mock_flask_app, main_on=DEFAULT, time_on=DEFAULT, visual_on=DEFAULT,
                   role_on=DEFAULT, journey='UseBot'):
    g, tg = _group('', main_on), _group('Time', time_on)
    vg, rg = _group('Visual', visual_on), _group('Role', role_on)
    seen = {'turn': [], 'time': [], 'visual': [], 'crossbar': [], 'role': []}
    time_user, visual_user, multi_role, role_user = (MagicMock(), MagicMock(),
                                                     MagicMock(), MagicMock())
    time_user.initiate_chat.side_effect = _recorder(seen['time'], tg)
    visual_user.initiate_chat.side_effect = _recorder(seen['visual'], vg)
    multi_role.initiate_chat.side_effect = _recorder(seen['crossbar'], g)
    role_user.initiate_chat.side_effect = _recorder(seen['role'], rg)
    g.gc.messages[:] = [{'role': 'user', 'name': 'User', 'content': 'hello'}]
    vg.gc.messages[:] = [{'role': 'user', 'name': 'User', 'content': 'seen'}]
    rg.gc.messages[:] = [{'role': 'assistant', 'name': 'RoleAssistant',
                          'content': 'Which persona are you?'}]
    visual_group = {'visual_agent': vg.a, 'helper2': vg.h, 'visual_user': visual_user,
                    'group_chat_2': vg.gc, 'manager_2': vg.m}
    entry = (g.a, MagicMock(), g.gc, g.m, g.h, multi_role, tg.a, time_user, tg.gc, tg.m,
             MagicMock(), visual_group)
    respond = MagicMock(side_effect=lambda *a, **k: (seen['turn'].append(_models(g)), 'done')[1])
    patches = [
        patch.object(rr, 'user_agents', {UP: entry}),
        patch.object(rr, 'role_agents', {UP: (rg.a, role_user, rg.gc, rg.m, rg.h, False)}),
        patch.object(rr, 'user_journey', {UP: journey}),
        patch.object(rr, 'user_tasks', {}),
        patch.object(rr, 'llm_call_track', {UP: {'count': 0, 'original_prompt': False}}),
        patch.object(rr, 'request_id_list', {}),
        patch.object(rr, 'config_list', list(DEFAULT)),
        patch.object(rr, 'clear_action_states'),
        patch.object(rr, 'get_role', return_value='role'),
        patch.object(rr, 'get_agent_response', respond),
        patch.object(rr, '_reuse_sync_group_log'),
        patch.object(rr, 'send_message_to_user1'),
        patch.object(rr, 'get_frame', return_value='frame'),
        patch.object(helper_fun, 'get_visual_context', return_value='ctx'),
    ]
    _start(patches)
    return SimpleNamespace(app=mock_flask_app, g=g, tg=tg, vg=vg, rg=rg, seen=seen,
                           respond=respond, time_user=time_user, patches=patches)


@pytest.fixture
def reuse(mock_flask_app):
    s = _reuse_session(mock_flask_app)
    try:
        yield s
    finally:
        _stop(s.patches)


@pytest.fixture
def reuse_built_on_expert(mock_flask_app):
    """A REUSE session an expert turn built: create_agents_for_user builds all
    three groups on the override of the turn that builds them."""
    s = _reuse_session(mock_flask_app, main_on=EXPERT, time_on=EXPERT, visual_on=EXPERT)
    try:
        yield s
    finally:
        _stop(s.patches)


@pytest.fixture
def reuse_roles(mock_flask_app):
    s = _reuse_session(mock_flask_app, journey='Roles')
    try:
        yield s
    finally:
        _stop(s.patches)


class TestReuseTurnsRunOnTheirModel:

    def test_an_expert_turn_then_an_ordinary_turn(self, reuse):
        thread_local_data.set_model_config_override(list(EXPERT))
        rr.chat_agent(USER, 'go', PROMPT, None, 'req-172-r1')
        thread_local_data.clear_model_config_override()
        rr.chat_agent(USER, 'again', PROMPT, None, 'req-172-r2')
        assert reuse.seen['turn'] == [ON_EXPERT, ON_DEFAULT]

    def test_a_chat_turn_moves_only_its_own_group(self, reuse):
        thread_local_data.set_model_config_override(list(EXPERT))
        rr.chat_agent(USER, 'go', PROMPT, None, 'req-172-r3')
        assert _models(reuse.tg) == ON_DEFAULT and _models(reuse.vg) == ON_DEFAULT, (
            'the chat turn rebuilt the time and visual groups too')

    def test_the_role_group_runs_on_the_turns_model(self, reuse_roles):
        thread_local_data.set_model_config_override(list(EXPERT))
        reply = rr.chat_agent(USER, 'I am a seller', PROMPT, None, 'req-172-r4')
        assert reply == 'Which persona are you?'
        assert reuse_roles.seen['role'] == [ON_EXPERT]

    def test_a_scheduled_run_is_on_the_default(self, reuse_built_on_expert):
        rr.time_based_execution('task', USER, PROMPT, 1)
        assert reuse_built_on_expert.seen['time'] == [ON_DEFAULT]

    def test_a_visual_run_is_on_the_default(self, reuse_built_on_expert):
        rr.visual_based_execution('look', USER, PROMPT)
        assert reuse_built_on_expert.seen['visual'] == [ON_DEFAULT]

    def test_both_crossbar_legs_are_on_the_default(self, reuse_built_on_expert):
        rr.crossbar_multiagent({'user_id': USER, 'caller_user_id': USER,
                                'caller_prompt_id': PROMPT, 'caller_role': 'buyer',
                                'role': 'seller', 'message': 'is it in stock?'})
        assert reuse_built_on_expert.seen['crossbar'] == [ON_DEFAULT, ON_DEFAULT]


class TestTurnsOfDifferentGroupsLeaveEachOtherAlone:

    def test_a_running_scheduled_run_keeps_its_model_when_a_chat_turn_asks_for_the_expert(
            self, reuse):
        """P1: the chat turn rebuilt every group of the session, the time
        group included, while the scheduled run was mid-way."""
        gate = _gate()
        reuse.time_user.initiate_chat.side_effect = _recorder(reuse.seen['time'], reuse.tg, gate)
        t = _on_thread(reuse.app, lambda: rr.time_based_execution('task', USER, PROMPT, 1))
        assert gate.entered.wait(WAIT)
        try:
            thread_local_data.set_model_config_override(list(EXPERT))
            rr.chat_agent(USER, 'go', PROMPT, None, 'req-p1')
        finally:
            gate.release.set()
            t.join(WAIT)
        assert reuse.seen['time'] == [ON_DEFAULT, ON_DEFAULT], reuse.seen['time']
        assert reuse.seen['turn'] == [ON_EXPERT], 'the chat turn was held by another group'

    def test_a_reuse_turn_does_not_hold_back_a_create_expert_turn(self, create, reuse):
        """P4: CREATE and REUSE keep separate agents for one session."""
        gate = _gate()
        reuse.respond.side_effect = lambda *a, **k: (gate.entered.set(), gate.release.wait(WAIT),
                                                     'done')[2]
        t = _on_thread(reuse.app, lambda: rr.chat_agent(USER, 'go', PROMPT, None, 'req-p4'))
        assert gate.entered.wait(WAIT)
        try:
            thread_local_data.set_model_config_override(list(EXPERT))
            cr.get_response_group(USER, 'take the step', PROMPT)
        finally:
            gate.release.set()
            t.join(WAIT)
        assert create.seen['turn'] == [ON_EXPERT], create.seen['turn']

    def test_a_reuse_turn_does_not_hold_back_the_create_orphan_settle(self, create, reuse):
        """P5: the CREATE turn answered "Processing a tool now please try
        later" to an orphaned call because a REUSE turn of the same session
        was running in its own group chat."""
        create.g.gc.messages[:] = [
            {'role': 'user', 'name': 'User', 'content': 'start'},
            {'role': 'assistant', 'name': 'Assistant', 'content': 'working'},
            {'role': 'assistant', 'name': 'Assistant', 'content': None,
             'tool_calls': [{'id': 'call_orphan', 'type': 'function',
                             'function': {'name': 'probe', 'arguments': '{}'}}]},
        ]
        gate = _gate()
        reuse.respond.side_effect = lambda *a, **k: (gate.entered.set(), gate.release.wait(WAIT),
                                                     'done')[2]
        t = _on_thread(reuse.app, lambda: rr.chat_agent(USER, 'go', PROMPT, None, 'req-p5'))
        assert gate.entered.wait(WAIT)
        try:
            reply = cr.get_response_group(USER, 'take the step', PROMPT)
        finally:
            gate.release.set()
            t.join(WAIT)
        assert reply != 'Processing a tool now please try later', reply
        assert create.seen['turn'], 'the CREATE turn never ran'

    def test_a_reuse_turn_is_seen_by_its_own_group_only(self, reuse):
        seen = []

        def respond(*a, **k):
            probe = threading.Thread(target=lambda: seen.append((
                helper_fun.another_turn_in_flight(UP, 'reuse'),
                cr._another_turn_in_flight(UP))))
            probe.start()
            probe.join(WAIT)
            return 'done'
        reuse.respond.side_effect = respond
        rr.chat_agent(USER, 'go', PROMPT, None, 'req-mark')
        assert seen == [(True, False)], seen
        assert not helper_fun.another_turn_in_flight(UP, 'reuse')


class TestAReuseTurnWaitsForTheTurnHoldingItsGroup:

    def _expert_turn_running(self, reuse):
        gate = _gate()

        def respond(*a, **k):
            reuse.seen['turn'].append(_models(reuse.g))
            if not gate.entered.is_set():   # only the expert turn waits
                gate.entered.set()
                gate.release.wait(WAIT)
                reuse.seen['turn'].append(_models(reuse.g))
            return 'done'
        reuse.respond.side_effect = respond

        def expert_turn():
            thread_local_data.set_model_config_override(list(EXPERT))
            rr.chat_agent(USER, 'go', PROMPT, None, 'req-expert')
        t = _on_thread(reuse.app, expert_turn)
        assert gate.entered.wait(WAIT)
        return gate, t

    def test_an_ordinary_turn_does_not_run_while_an_expert_turn_runs(self, reuse):
        gate, t = self._expert_turn_running(reuse)
        try:
            reply = rr.chat_agent(USER, 'and this', PROMPT, None, 'req-ordinary')
        finally:
            gate.release.set()
            t.join(WAIT)
        assert reuse.respond.call_count == 1, 'the ordinary turn ran'
        assert reuse.seen['turn'] == [ON_EXPERT, ON_EXPERT], 'the expert turn was switched'
        assert _is_held(reply), reply

    def test_a_retry_storm_is_held_and_the_running_turn_keeps_its_model(self, reuse):
        """The desktop's retry storm (10-19): one request re-posted while its
        first arrival runs.  Arrival 1 asked for the expert; arrivals 2-11 are
        ordinary turns of the same session."""
        gate, t = self._expert_turn_running(reuse)
        replies, lock = [], threading.Lock()

        def arrival(i):
            reply = rr.chat_agent(USER, 'hello?', PROMPT, None, f'req-storm-{i}')
            with lock:
                replies.append(reply)
        try:
            arrivals = [_on_thread(reuse.app, lambda i=i: arrival(i)) for i in range(2, 12)]
            for a in arrivals:
                a.join(WAIT)
        finally:
            gate.release.set()
            t.join(WAIT)
        assert reuse.respond.call_count == 1, f'{reuse.respond.call_count - 1} arrivals ran'
        assert reuse.seen['turn'] == [ON_EXPERT, ON_EXPERT]
        assert len(replies) == 10 and all(_is_held(r) for r in replies), replies

    def test_turns_asking_the_same_model_both_run(self, reuse_built_on_expert):
        """Two ordinary turns marked before either switched the group (built
        on the expert): neither holds the other, so the group moves to the
        default and the turn runs."""
        s = reuse_built_on_expert
        marked, leave = threading.Event(), threading.Event()

        def other_ordinary_turn():
            with helper_fun.turn_in_flight(UP, 'reuse'):
                marked.set()
                leave.wait(WAIT)
        t = _on_thread(s.app, other_ordinary_turn)
        assert marked.wait(WAIT)
        try:
            reply = rr.chat_agent(USER, 'go', PROMPT, None, 'req-same')
        finally:
            leave.set()
            t.join(WAIT)
        assert reply == 'done'
        assert s.seen['turn'] == [ON_DEFAULT]


# ── The turn's model ends with its request (P2) ────────────────────────────

@pytest.fixture(scope='module')
def hie():
    import hart_intelligence_entry
    return hart_intelligence_entry


def _refusing_guardrail(seen):
    def before_dispatch(prompt, *a, **k):
        seen.append(thread_local_data.get_model_config_override())
        return False, 'probe', prompt
    return before_dispatch


def _raising_guardrail(seen):
    def before_dispatch(prompt, *a, **k):
        seen.append(thread_local_data.get_model_config_override())
        raise RuntimeError('probe: the turn raised')
    return before_dispatch


_CHAT_BODY = {'user_id': 'u172', 'prompt': 'hello', 'prompt_id': None,
              'request_id': 'req-172-teardown', 'preferred_lang': 'en',
              'model_config': EXPERT}


class TestATurnsModelEndsWithItsRequest:

    def test_after_a_chat_that_returned_early(self, hie, reuse, monkeypatch):
        seen = []
        with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                   side_effect=_refusing_guardrail(seen)):
            r = hie.app.test_client().post('/chat', json=dict(_CHAT_BODY))
        assert r.status_code == 403, r.get_data(as_text=True)[:200]
        assert seen and seen[0][0]['model'] == 'expert-model', 'the turn never had its model'
        assert thread_local_data.get_model_config_override() is None
        rr.time_based_execution('task', USER, PROMPT, 1)
        assert reuse.seen['time'] == [ON_DEFAULT], (
            'a scheduled run on the thread an expert /chat used ran on the expert')

    def test_after_a_chat_that_raised(self, hie, monkeypatch):
        monkeypatch.setitem(hie.app.config, 'PROPAGATE_EXCEPTIONS', True)
        seen = []
        with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                   side_effect=_raising_guardrail(seen)):
            with pytest.raises(RuntimeError):
                hie.app.test_client().post('/chat', json=dict(_CHAT_BODY))
        assert seen and seen[0][0]['model'] == 'expert-model'
        assert thread_local_data.get_model_config_override() is None

    def test_after_the_daemons_in_process_chat(self, hie):
        """dispatch._native_chat: the daemon's own thread runs the request."""
        from integrations.agent_engine import dispatch
        seen = []
        with patch('security.hive_guardrails.GuardrailEnforcer.before_dispatch',
                   side_effect=_refusing_guardrail(seen)):
            dispatch._native_chat(text='hello', user_id='u172', agent_id=None,
                                  model_config=EXPERT)
        assert seen and seen[0][0]['model'] == 'expert-model'
        assert thread_local_data.get_model_config_override() is None


# ── Every dispatcher holds a held turn for a retry ─────────────────────────

class TestAHeldTurnIsHeldForARetry:

    @pytest.fixture
    def nothing_else_transient(self, monkeypatch):
        from integrations.agent_engine import dispatch
        monkeypatch.setattr(dispatch, '_cb_is_open', lambda: False)
        monkeypatch.setattr('core.foreground.preempted_recently', lambda *a, **k: False)
        return dispatch

    def test_dispatch_goal_returns_no_response_and_a_transient_reason(
            self, nothing_else_transient):
        from tests.unit.test_failed_turn_is_not_work import _through_tier1
        dispatch = nothing_else_transient
        with _through_tier1(_held_reply()):
            assert dispatch.dispatch_goal('draft the digest', 'u1', 'g-held') is None
        assert dispatch.is_transient_deferral('g-held') is True
        assert dispatch.dispatch_failure_reason('g-held').startswith('deferred:')

    def test_a_held_expert_turn_is_a_transient_defer(self, nothing_else_transient):
        """The daemon's expert turn runs through the native in-process /chat.
        A transient defer is what the tick skips without a strike and without
        settling, so the goal keeps its escalation for the next tick instead
        of being parked as "the expert model did not finish it"
        (agent_daemon._settle_expert_turn) by a turn the expert never ran."""
        from tests.unit.test_failed_turn_is_not_work import _through_tier1
        dispatch = nothing_else_transient
        with _through_tier1('unused'), \
                patch.object(dispatch, '_native_chat',
                             return_value={'response': _held_reply()}):
            assert dispatch.dispatch_goal('post the thread', 'u1', 'g-expert-held',
                                          model_config=[EXPERT[0]]) is None
        assert dispatch.is_transient_deferral('g-expert-held') is True

    def test_the_hive_worker_defers_the_task(self):
        from integrations.distributed_agent.worker_loop import (DeferredForRetry,
                                                                DistributedWorkerLoop)
        task = SimpleNamespace(task_id='t172', description='recruit compute', context={'hop': 0})
        with patch('security.hive_guardrails.GuardrailEnforcer.after_response',
                   return_value=(True, '')):
            got = DistributedWorkerLoop()._after_response(_held_reply(), task)
        assert isinstance(got, DeferredForRetry), got

    def test_chats_first_create_dispatch_answers_the_held_turn(self, tmp_path):
        """/chat's autonomous CREATE answers recipe()'s held reply, not the
        gather output, so the dispatcher can hold it."""
        from tests.unit.test_yield_pauses_every_agent import _first_dispatch
        text, _payload = _first_dispatch(tmp_path, _held_reply())
        assert _is_held(text), text


def test_binding_keeps_tools_and_leaves_agents_without_a_model_alone():
    g = _group('')
    proxy = autogen.UserProxyAgent('User', llm_config=False, human_input_mode='NEVER',
                                   code_execution_config=False)
    thread_local_data.set_model_config_override(list(EXPERT))
    assert helper_fun.bind_agents_to_turn_model(
        (g.gc, g.m, {'user': proxy}), list(DEFAULT), 'bind_probe', 'reuse') is True
    assert _models(g) == ON_EXPERT
    assert proxy.llm_config is False and proxy.client is None
    assert [t['function']['name'] for t in g.h.llm_config['tools']] == ['probe']
    clients = [x.client for x in (g.a, g.h, g.m)]
    assert helper_fun.bind_agents_to_turn_model(
        (g.gc, g.m), list(DEFAULT), 'bind_probe', 'reuse') is True
    assert [x.client for x in (g.a, g.h, g.m)] == clients, \
        'agents already on the turn model were rebuilt again'

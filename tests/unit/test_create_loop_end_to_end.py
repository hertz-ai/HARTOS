"""The create loop, end to end, with scripted agents and no LLM (#102).

The create loop (create_recipe.recipe -> get_response_group) has been fixed one
symptom at a time for months, and each fix was tested alone.  Several fixes
loosened a guard to break a stall, and the loosened guard let the next defect
through: on central 2026-09-13/14 a finished action's verdict and TERMINATE
were credited to the action that had only just started (#101), so every real
action was followed by a phantom completion of the next one, and two actions
never got a file at all.

This file drives the REAL loop through a whole flow.  Every autogen agent
answers from a script, so no LLM is called, and every state change is recorded
together with whether that action had been posted ("Execute Action N:") yet.
It asserts the invariants the incidents broke:

- each action is posted before anything completes or terminates it;
- every saved action file was banked from that action's own tool calls;
- no action is skipped, and the flow recipe gets written;
- no recipe is requested for work that never ran, and no LLM is called.

The scripted verifier cites its receipt (evidence.message_index) and the
scripted tool is real work, not bookkeeping: since 2026-09-27 a verdict
without a receipt, or with a note saved to memory as its receipt, completes
nothing (tests/unit/test_completion_needs_real_work.py).  Before that this
flow finished only because the TERMINATE after each verdict walked the action
through COMPLETED with no receipt at all.

It also pins that a verdict settles only the action it answers.  One naming a
different action_id leaves that action's text alone, and one naming a FUTURE
action_id completes nothing before that action is posted.  Both were strict
xfails until #106 bound every verdict to the posted action.

    python -m pytest tests/unit/test_create_loop_end_to_end.py -q -p no:cacheprovider
"""
import json
import os
import re
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
for _p in (_ROOT, os.path.join(_ROOT, 'agent-ledger-opensource')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

USER_ID = 7001
PROMPT_ID = 424201
UP = f'{USER_ID}_{PROMPT_ID}'
ACTIONS = [
    'Collect the three sources on local AI costs',
    'Summarise the three sources in five lines',
    'Save the summary for the weekly report',
]
_MARKER = re.compile(r'Execute Action (\d+):')
_DONE_STATES = ('completed', 'terminated')


def _content(msg):
    c = msg.get('content') if isinstance(msg, dict) else msg
    return c if isinstance(c, str) else ''


class _Script:
    """Scripted replies for the create group.  Reads the live group list (the
    agents' own ``messages`` argument is rewritten by transforms)."""

    def __init__(self, verdict_ids=None):
        self.gc = None
        self.unexpected = []
        self.recipe_requests = []
        # action currently posted -> action_id the verifier should claim
        self.verdict_ids = dict(verdict_ids or {})
        # actions the verifier holds as needing a person
        self.pending_ids = set()
        # the tool the helper runs for each step
        self.tool = 'google_search'

    # -- helpers ---------------------------------------------------------
    def _msgs(self):
        return self.gc.messages if self.gc is not None else []

    def current(self):
        for m in reversed(self._msgs()):
            found = _MARKER.findall(_content(m))
            if found:
                return int(found[-1])
        return 0

    def _last(self):
        msgs = self._msgs()
        return msgs[-1] if msgs else {}

    # -- agents ----------------------------------------------------------
    def assistant(self, recipient, messages=None, sender=None, config=None):
        last = self._last()
        c = _content(last)
        n = self.current()
        if _MARKER.search(c):
            return True, f'Working on step {n}. @Helper please save step {n}.'
        if last.get('tool_calls'):
            tc = last['tool_calls'][0]
            return True, {
                'role': 'tool', 'content': f'saved step {n}',
                'tool_responses': [{'tool_call_id': tc.get('id'),
                                    'role': 'tool',
                                    'content': f'saved step {n}'}]}
        if last.get('role') == 'tool' or last.get('tool_responses'):
            return True, f'Step {n} done. @StatusVerifier please verify.'
        if c.startswith('@Assistant: To Get Action'):
            return True, 'TERMINATE'
        if 'Check if the current_action depends' in c:
            return True, '@StatusVerifier []'
        if 'Reflect on the sequence' in c:
            return True, json.dumps({'status': 'completed', 'dependency': [],
                                     'recipe': '', 'scheduled_tasks': []})
        self.unexpected.append(('Assistant', c[:200]))
        return True, 'TERMINATE'

    def helper(self, recipient, messages=None, sender=None, config=None):
        n = self.current()
        if '@Helper' in _content(self._last()):
            return True, {'content': '', 'tool_calls': [{
                'id': f'call_step_{n}', 'type': 'function',
                'function': {'name': self.tool,
                             'arguments': json.dumps({'query': f'e2e.step{n}'})}}]}
        self.unexpected.append(('Helper', _content(self._last())[:200]))
        return True, 'TERMINATE'

    def verifier(self, recipient, messages=None, sender=None, config=None):
        c = _content(self._last())
        n = self.current()
        if 'please verify' in c and n in self.pending_ids:
            return True, json.dumps({
                'status': 'pending', 'action': ACTIONS[n - 1], 'action_id': n,
                'message': 'a person has to confirm the sources',
                'can_perform_without_user_input': 'no'})
        if 'please verify' in c:
            claimed = self.verdict_ids.get(n, n)
            receipt = max(i for i, m in enumerate(self._msgs())
                          if isinstance(m, dict) and m.get('role') == 'tool')
            return True, json.dumps({
                'status': 'completed', 'action': ACTIONS[n - 1],
                'action_id': claimed, 'message': 'verified',
                'evidence': {'message_index': receipt, 'kind': 'tool_receipt'},
                'can_perform_without_user_input': 'yes',
                'persona_name': 'Researcher', 'fallback_action': 'retry once'})
        if c.strip().endswith('[]'):
            return True, '[]'
        if 'recipe' in c.lower():
            self.recipe_requests.append((n, c[:200]))
            return True, json.dumps({
                'status': 'done', 'action': ACTIONS[n - 1], 'action_id': n,
                'fallback_action': 'retry once', 'persona': 'Researcher',
                'recipe': [{'steps': f'search step {n}',
                            'tool_name': 'google_search',
                            'generalized_functions': ''}],
                'can_perform_without_user_input': 'yes',
                'scheduled_tasks': []})
        self.unexpected.append(('StatusVerifier', c[:200]))
        return True, 'TERMINATE'

    def executor(self, recipient, messages=None, sender=None, config=None):
        self.unexpected.append(('Executor', _content(self._last())[:200]))
        return True, 'TERMINATE'


@pytest.fixture
def create_env(tmp_path, monkeypatch):
    import faulthandler
    # A misrouted script spins inside the loop, and a blocked call never
    # returns: either way, dump every thread's stack and exit instead of
    # hanging the run (and the machine's memory) until an outer timeout.
    # Setup gets a wide budget (importing create_recipe took about four
    # minutes on a loaded machine); _run re-arms a tighter one for the loop.
    faulthandler.dump_traceback_later(900, exit=True)
    # Keep torch out of this process: autogen's text_compressors imports
    # llmlingua (which loads torch) inside try/except ImportError, so blocking
    # it is safe and spares about a gigabyte per run.
    if 'llmlingua' not in sys.modules:
        monkeypatch.setitem(sys.modules, 'llmlingua', None)
    import flask
    import autogen
    import hartos.create_recipe as cr
    import hartos.helper as h
    import hartos.lifecycle_hooks as lh
    import core.cache_loaders as cl
    from agent_ledger.backends import InMemoryBackend

    # -- files ---------------------------------------------------------------
    prompts = tmp_path / 'prompts'
    agent_data_dir = tmp_path / 'agent_data'
    prompts.mkdir()
    agent_data_dir.mkdir()
    (prompts / f'{PROMPT_ID}.json').write_text(json.dumps({
        'personas': [{'name': 'Researcher',
                      'description': 'Collects and summarises sources.'}],
        'goal': 'Summarise three sources on local AI costs',
        'agent_name': 'E2E Researcher',
        'flows': [{'persona': 'Researcher', 'sub_goal': 'weekly summary',
                   'actions': ACTIONS}],
    }), encoding='utf-8')
    for mod in (cr, h, lh, cl):
        if hasattr(mod, 'PROMPTS_DIR'):
            monkeypatch.setattr(mod, 'PROMPTS_DIR', str(prompts))
    for mod in (h, cl):
        if hasattr(mod, 'AGENT_DATA_DIR'):
            monkeypatch.setattr(mod, 'AGENT_DATA_DIR', str(agent_data_dir))
    monkeypatch.chdir(tmp_path)

    # -- caches: no disk loaders, no state from other tests ---------------------
    for name in ('agent_data', 'user_ledgers', 'recipe_for_persona',
                 'user_simplemem'):
        cache = getattr(cr, name, None)
        if cache is not None and hasattr(cache, '_loader'):
            monkeypatch.setattr(cache, '_loader', None)
    if hasattr(getattr(lh, '_ledger_registry', None), '_loader'):
        monkeypatch.setattr(lh._ledger_registry, '_loader', None)
    for name in ('user_tasks', 'user_agents', 'messages', 'user_ledgers',
                 'agent_data', 'recipe_for_persona', 'total_persona_actions',
                 'scheduler_check', 'request_id_list', 'individual_json',
                 'user_simplemem', 'task_time'):
        obj = getattr(cr, name, None)
        if obj is not None and hasattr(obj, 'clear'):
            obj.clear()
    for name in ('action_states', 'retry_tracker'):
        obj = getattr(lh, name, None)
        if obj is not None and hasattr(obj, 'clear'):
            obj.clear()
    monkeypatch.setattr(cr, 'get_production_backend',
                        lambda *a, **k: InMemoryBackend())

    # -- memory, history, personality ----------------------------------------
    monkeypatch.setattr(cr, 'HAS_SIMPLEMEM', False)
    import integrations.channels.memory.memory_graph as mg
    monkeypatch.setattr(mg, 'MemoryGraph',
                        mock.Mock(side_effect=RuntimeError('no graph in test')))
    import integrations.channels.memory.shared_history as sh
    monkeypatch.setattr(sh, '_get_persistent_history', lambda user_id: None)
    for modname, attr, value in (
            ('core.resonance_tuner', 'get_resonance_tuner', lambda: mock.Mock()),
            ('core.resonance_profile', 'get_or_create_profile',
             mock.Mock(side_effect=RuntimeError('no profile in test'))),
            ('core.agent_personality', 'load_personality', lambda *a, **k: None),
            ('core.agent_personality', 'save_personality', lambda *a, **k: None)):
        try:
            mod = __import__(modname, fromlist=[attr])
            monkeypatch.setattr(mod, attr, value)
        except (ImportError, AttributeError):
            pass
    monkeypatch.setattr(h, 'history', lambda *a, **k: None)

    # -- publishing, DB, charges ------------------------------------------------
    sent = []
    monkeypatch.setattr(cr, 'send_message_to_user1',
                        lambda *a, **k: sent.append(a))
    for modname, attr in (
            ('core.peer_link.crossbar_publish', 'publish_thinking_trace'),):
        try:
            mod = __import__(modname, fromlist=[attr])
            monkeypatch.setattr(mod, attr, lambda *a, **k: None)
        except (ImportError, AttributeError):
            pass
    monkeypatch.setattr(cr, '_announce_flow_recipe', lambda *a, **k: None)
    monkeypatch.setattr(cr, 'update_agent_creation_to_db', lambda *a, **k: None)
    try:
        import integrations.agent_engine.budget_gate as bg
        monkeypatch.setattr(bg, 'charge_goal_work_completed',
                            lambda *a, **k: None)
    except (ImportError, AttributeError):
        pass
    try:
        import security.immutable_audit_log as al
        monkeypatch.setattr(al, 'get_audit_log', lambda: mock.Mock())
    except (ImportError, AttributeError):
        pass

    # -- no network, no LLM -------------------------------------------------
    def _refuse(*a, **k):
        raise ConnectionError('e2e create-loop test: no network')
    import requests
    monkeypatch.setattr(requests.Session, 'request', _refuse)
    try:
        import httpx
        monkeypatch.setattr(httpx.Client, 'send', _refuse)
    except ImportError:
        pass
    # Every tool registration rebuilds an agent's OpenAIWrapper, and each one
    # loads the CA bundle into a fresh SSL context; across dozens of tools that
    # made create_agents crawl (the watchdog caught it in
    # load_ssl_context_verify).  Nothing connects here, so one context will do.
    try:
        import httpx._config as _hc
        _real_load_ssl = _hc.SSLConfig.load_ssl_context
        _ssl_cache = {}

        def _cached_ssl(self):
            key = (repr(self.verify), repr(self.cert), self.trust_env,
                   self.http2)
            if key not in _ssl_cache:
                _ssl_cache[key] = _real_load_ssl(self)
            return _ssl_cache[key]
        monkeypatch.setattr(_hc.SSLConfig, 'load_ssl_context', _cached_ssl)
    except (ImportError, AttributeError):
        pass
    llm_calls = []

    def _no_llm(*a, **k):
        llm_calls.append('llm')
        return ''
    monkeypatch.setattr(h, '_local_llm_complete', _no_llm, raising=False)

    def _no_wrapper(*a, **k):
        llm_calls.append('autogen')
        raise AssertionError('an LLM was called in a scripted run')
    monkeypatch.setattr(autogen.OpenAIWrapper, 'create', _no_wrapper)
    # The dev venv pairs Python 3.12.3 with pydantic 1.10.9, whose
    # evaluate_forwardref calls ForwardRef._evaluate(globalns, localns, set())
    # positionally, so autogen's register_for_llm raises "missing
    # recursive_guard" on a string annotation and no agent can be created.
    # That venv cannot be repaired from here (pip is broken, #35). Central runs
    # Python 3.10 with pydantic 2.9.2 and never takes this path. Where the call
    # fails, the harness resolves it the way pydantic 2's eval_type_lenient
    # does on central: typing._eval_type, and a name it cannot find leaves the
    # annotation as it was. A pass with the shim says the create-loop logic
    # runs; it is not a central-equivalent result. A venv where the call works
    # keeps autogen's own.
    import typing
    from autogen import function_utils as _fu

    def _lenient_eval(t, globalns, localns):
        try:
            return typing._eval_type(t, globalns, localns)
        except NameError:
            return t
    try:
        _fu.evaluate_forwardref(typing.ForwardRef('int'), {}, {})
    except TypeError:
        monkeypatch.setattr(_fu, 'evaluate_forwardref', _lenient_eval)
    monkeypatch.setattr(cr, 'get_llm_config', lambda *a, **k: {
        'cache_seed': None, 'max_tokens': 16,
        'config_list': [{'model': 'scripted', 'api_key': 'not-a-key',
                         'base_url': 'http://127.0.0.1:9/v1'}]})

    # -- scripted agents + state recorder ------------------------------------
    script = _Script()
    real_create = cr.create_agents

    def _create(user_id, task, prompt_id):
        out = real_create(user_id, task, prompt_id)
        group_chat, agents_object = out[3], out[6]
        script.gc = group_chat
        for key, fn in (('assistant', script.assistant),
                        ('helper', script.helper),
                        ('verify', script.verifier),
                        ('executor', script.executor)):
            agents_object[key].register_reply(
                [autogen.Agent, None], fn, position=0,
                remove_other_reply_funcs=True)
        return out
    monkeypatch.setattr(cr, 'create_agents', _create)

    events = []
    plans = []
    real_set = lh.set_action_state

    def _record(user_prompt, action_id, state, *a, **k):
        if user_prompt == UP:
            aid = int(action_id)
            posted = any(f'Execute Action {aid}:' in _content(m)
                         for m in (script.gc.messages if script.gc else []))
            events.append((aid, getattr(state, 'value', str(state)), posted))
            # The plan as it stood at this state change.  Once every flow is
            # done the loop replaces user_tasks[UP] with an empty Action, so
            # the plan can only be judged while the run is in progress.
            plans.append(list(getattr(cr.user_tasks.get(UP), 'actions', None) or []))
        return real_set(user_prompt, action_id, state, *a, **k)
    monkeypatch.setattr(lh, 'set_action_state', _record)

    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('daemon_e2e_create')
    app = flask.Flask('create-loop-e2e')
    yield SimpleNamespace(cr=cr, lh=lh, app=app, script=script, events=events,
                          plans=plans, llm_calls=llm_calls, prompts=prompts,
                          sent=sent)
    thread_local_data.set_request_id('')
    faulthandler.cancel_dump_traceback_later()


def _run(env, turns=3):
    import faulthandler
    # The loop itself gets five minutes; a misrouted script dumps and exits.
    faulthandler.dump_traceback_later(300, exit=True)
    replies = []
    with env.app.app_context():
        for _ in range(turns):
            replies.append(env.cr.recipe(USER_ID, 'Build the agent now',
                                         PROMPT_ID, None, 'daemon_e2e_create'))
            if env.cr.scheduler_check.get(UP):
                break
    return replies


def _action_file(env, n):
    p = env.prompts / f'{PROMPT_ID}_0_{n}.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None


def test_a_whole_flow_runs_in_order_with_no_phantom_completion(create_env):
    env = create_env
    replies = _run(env)

    posted = [int(n) for m in env.script.gc.messages
              for n in _MARKER.findall(_content(m))]
    first_post = {}
    for i, n in enumerate(posted):
        first_post.setdefault(n, i)
    assert sorted(first_post) == [1, 2, 3], (
        f'posted actions {posted!r}; replies {replies!r}')
    assert first_post[1] < first_post[2] < first_post[3], posted

    early = [(aid, state) for aid, state, was_posted in env.events
             if state in _DONE_STATES and not was_posted]
    assert not early, (
        f'actions reached {early!r} before "Execute Action N:" was posted: '
        'the phantom completion of #101')

    for n in (1, 2, 3):
        data = _action_file(env, n)
        assert data is not None, f'action {n} has no file (a gap)'
        assert data.get('recipe_source') == 'execution_trace', (n, data)
        steps = data.get('recipe') or []
        assert any(s.get('tool_name') == 'google_search'
                   and f'e2e.step{n}' in s.get('steps', '') for s in steps), (
            f'action {n} was not banked from its own tool call: {steps!r}')

    assert not env.script.recipe_requests, (
        f'a recipe was requested for work the trace already holds: '
        f'{env.script.recipe_requests!r}')
    assert not env.script.unexpected, env.script.unexpected
    assert not env.llm_calls, env.llm_calls
    assert (env.prompts / f'{PROMPT_ID}_0_recipe.json').exists(), (
        f'the flow recipe was never written; replies {replies!r}')


def test_a_finished_last_action_saves_the_flow_recipe_in_one_turn(create_env,
                                                                    caplog):
    """The last action's verdict is settled once, then the flow completes.

    Live 2026-09-25 (livetest_create_recipe_verify_01, prompt 91790350001,
    and livetest_agent_to_agent_verify_r1): the termination hook moved the
    last action COMPLETED -> TERMINATED before the verdict pickup, whose
    [ALREADY DONE] -> [LAST-ACTION] path then fell into the COMPLETION-GATE.
    The gate only accepts COMPLETED, so it `continue`d without posting
    anything, the same verdict was re-read on the next lap, and the loop ran
    [LAST-ACTION] -> [COMPLETION-GATE] ~300 times in ~3 s to max_iterations.
    No flow recipe was written and /chat answered 'Review Mode' after 448 s.

    One turn must finish the flow: the reply is the success string the /chat
    handler maps to a created agent, the flow recipe is on disk, and the
    loop never reaches its iteration cap.
    """
    env = create_env
    with caplog.at_level('INFO'):
        replies = _run(env, turns=1)
    log = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'reaching max iterations' not in log, (
        f'the create loop drained max_iterations; '
        f'{log.count("[COMPLETION-GATE]")} COMPLETION-GATE laps; '
        f'replies {replies!r}')
    assert (env.prompts / f'{PROMPT_ID}_0_recipe.json').exists(), (
        f'the flow recipe was never written; replies {replies!r}')
    assert replies == ['Agent Created Successfully'], replies
    for n in (1, 2, 3):
        assert env.lh.get_action_state(UP, n).value == 'terminated', n


def test_a_mislabelled_verdict_leaves_other_actions_alone(create_env):
    """A verdict naming another action settles the posted one; it never
    rewrites the other action's text (#106, settled_action_id)."""
    env = create_env
    env.script.verdict_ids = {2: 1}      # action 2's verdict claims action 1
    _run(env)
    # Judged at every state change of the run: reading user_tasks after the
    # flow finished found an empty plan and raised IndexError whatever the
    # code did, which the strict xfail this replaced accepted as its failure.
    seen = [p for p in env.plans if p]
    assert seen, 'no plan was recorded during the run'
    assert all(p[0] == ACTIONS[0] for p in seen), sorted({p[0] for p in seen})


def test_a_verdict_naming_a_future_action_does_not_complete_it(create_env):
    """A verdict naming a future action completes nothing before that action
    is posted (#106, settled_action_id)."""
    env = create_env
    env.script.verdict_ids = {2: 3}      # action 2's verdict claims action 3
    _run(env)
    early = [(aid, state) for aid, state, was_posted in env.events
             if state in _DONE_STATES and not was_posted]
    assert not early, early


def test_a_stuck_action_is_handed_on_not_completed(create_env, monkeypatch):
    """#106: on an autonomous run an action the agent cannot finish is held
    open and its goal is parked with the ask.  Nothing is recorded as done,
    nothing is banked for it, and the flow does not move past it."""
    env = create_env
    env.script.pending_ids = {2}          # the verifier: action 2 needs a person
    asks = []
    import integrations.agent_engine.goal_manager as gm
    monkeypatch.setattr(gm.GoalManager, 'escalate_goal', staticmethod(
        lambda db, goal_id, escalation: asks.append((goal_id, escalation))
        or {'success': True}))
    import integrations.social.models as models
    from contextlib import contextmanager

    @contextmanager
    def _no_db(commit=False):
        yield None
    monkeypatch.setattr(models, 'db_session', _no_db)
    # One turn: the daemon does not dispatch a parked goal again.
    replies = _run(env, turns=1)
    assert [(goal, e['action_id']) for goal, e in asks] == [('e2e_create', 2)], asks
    assert 'Paused for help' in str(replies), replies
    assert env.lh.get_action_state(UP, 2).value == 'pending'
    assert _action_file(env, 1) is not None, 'action 1 ran and should be banked'
    assert _action_file(env, 2) is None, 'an action that did not finish was banked'
    posted = {int(n) for m in env.script.gc.messages
              for n in _MARKER.findall(_content(m))}
    assert 3 not in posted, 'the flow moved past the stuck action'
    assert not env.script.recipe_requests, env.script.recipe_requests
    done = [s for aid, s, _ in env.events if aid == 2 and s in _DONE_STATES]
    assert not done, f'action 2 was recorded as done: {done}'


def test_notes_to_self_complete_nothing(create_env, caplog):
    """Live 2026-09-27, CREATE daemon_255bd83f: the only tool traffic was
    save_data_in_memory, the verifier cited it, and both actions went
    COMPLETED and were banked.  Here every step runs only that note-taking
    tool: no action may complete, and none may be banked.

    Nor may the refused action stall the loop: it is re-posted (bounded) and
    then given up or handed to a person, never left for the stall guard to
    break after 120 silent laps (measured while writing this fix)."""
    env = create_env
    env.script.tool = 'save_data_in_memory'
    with caplog.at_level('INFO'):
        _run(env, turns=1)
    log = ' | '.join(r.getMessage() for r in caplog.records)
    assert '[STALL-GUARD]' not in log, 'a refused action spun to the stall guard'
    completed = [(aid, s) for aid, s, _ in env.events if s == 'completed']
    assert not completed, f'an action completed on a note to self: {completed}'
    banked = [n for n in (1, 2, 3) if _action_file(env, n) is not None]
    assert not banked, f'notes were banked as the recipe of action(s) {banked}'

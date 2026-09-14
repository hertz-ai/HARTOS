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

Two known defects are pinned as strict xfails so they turn red the day they
are fixed (and the xfail marker has to come off):
- a verdict that names a different action_id overwrites that action's text;
- a verdict that names a FUTURE action_id completes that action before it is
  posted (state_transition forces COMPLETED on the claimed id).

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
                'function': {'name': 'save_data_in_memory',
                             'arguments': json.dumps({'key': f'e2e.step{n}',
                                                      'value': 'ok'})}}]}
        self.unexpected.append(('Helper', _content(self._last())[:200]))
        return True, 'TERMINATE'

    def verifier(self, recipient, messages=None, sender=None, config=None):
        c = _content(self._last())
        n = self.current()
        if 'please verify' in c:
            claimed = self.verdict_ids.get(n, n)
            return True, json.dumps({
                'status': 'completed', 'action': ACTIONS[n - 1],
                'action_id': claimed, 'message': 'verified',
                'can_perform_without_user_input': 'yes',
                'persona_name': 'Researcher', 'fallback_action': 'retry once'})
        if c.strip().endswith('[]'):
            return True, '[]'
        if 'recipe' in c.lower():
            self.recipe_requests.append((n, c[:200]))
            return True, json.dumps({
                'status': 'done', 'action': ACTIONS[n - 1], 'action_id': n,
                'fallback_action': 'retry once', 'persona': 'Researcher',
                'recipe': [{'steps': f'save step {n}',
                            'tool_name': 'save_data_in_memory',
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
    real_set = lh.set_action_state

    def _record(user_prompt, action_id, state, *a, **k):
        if user_prompt == UP:
            aid = int(action_id)
            posted = any(f'Execute Action {aid}:' in _content(m)
                         for m in (script.gc.messages if script.gc else []))
            events.append((aid, getattr(state, 'value', str(state)), posted))
        return real_set(user_prompt, action_id, state, *a, **k)
    monkeypatch.setattr(lh, 'set_action_state', _record)

    from hartos.threadlocal import thread_local_data
    thread_local_data.set_request_id('daemon_e2e_create')
    app = flask.Flask('create-loop-e2e')
    yield SimpleNamespace(cr=cr, lh=lh, app=app, script=script, events=events,
                          llm_calls=llm_calls, prompts=prompts, sent=sent)
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
        assert any(s.get('tool_name') == 'save_data_in_memory'
                   and f'e2e.step{n}' in s.get('steps', '') for s in steps), (
            f'action {n} was not banked from its own tool call: {steps!r}')

    assert not env.script.recipe_requests, (
        f'a recipe was requested for work the trace already holds: '
        f'{env.script.recipe_requests!r}')
    assert not env.script.unexpected, env.script.unexpected
    assert not env.llm_calls, env.llm_calls
    assert (env.prompts / f'{PROMPT_ID}_0_recipe.json').exists(), (
        f'the flow recipe was never written; replies {replies!r}')


@pytest.mark.xfail(strict=True, reason=(
    'known defect: state_transition overwrites actions[claimed_id - 1] with the '
    "verdict's action text, so a verdict naming the wrong action rewrites "
    'another action (create_recipe.py ~2495)'))
def test_a_mislabelled_verdict_leaves_other_actions_alone(create_env):
    env = create_env
    env.script.verdict_ids = {2: 1}      # action 2's verdict claims action 1
    _run(env)
    with env.app.app_context():
        texts = [env.cr.user_tasks[UP].get_action(i) for i in range(3)]
    assert texts[0] == ACTIONS[0], texts


@pytest.mark.xfail(strict=True, reason=(
    'known defect: a verdict naming a FUTURE action_id makes state_transition '
    'force COMPLETED on it before "Execute Action N:" is posted'))
def test_a_verdict_naming_a_future_action_does_not_complete_it(create_env):
    env = create_env
    env.script.verdict_ids = {2: 3}      # action 2's verdict claims action 3
    _run(env)
    early = [(aid, state) for aid, state, was_posted in env.events
             if state in _DONE_STATES and not was_posted]
    assert not early, early

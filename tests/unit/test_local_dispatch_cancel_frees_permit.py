"""A cancelled remote turn gives the LLM permit back: before the turn starts,
and mid-turn at its next LLM call.

Review of a4ea04651 (probe: started 0.75 s, cancelled 2.38 s, ran to
3.77 s): a cancel stopped only a QUEUED turn.  The node already aborts
background LLM work through core.llama_scheduler, the one admission every
local llama call passes (pooled_post, the httpx and urllib patches).  Its
preempt closes the SHARED background client, which would drop every other
background call, so a per-turn cancel cannot use that.  It uses the same
admission by request id instead: local_chat_dispatch binds the turn's
cancel_event to its request id (daemon_a2a_<ctx>), and the scheduler refuses
every later LLM call of that id (TurnCancelled) and wakes it if queued for a
slot.  The call already on the wire when the cancel lands finishes (one
call, not the turn); llama-server has no per-request abort other than
closing that connection.

Driven through the REAL local_chat_dispatch, the REAL semaphore, the REAL
scheduler and the REAL pooled_post; the in-process /chat callable, the
user-activity gate and the llama HTTP session are the boundary.
"""
import threading
import time

import pytest

from integrations.agent_engine import dispatch


@pytest.fixture
def chat(monkeypatch):
    ran = []

    def fake_chat(**kw):
        ran.append(kw['text'])
        return {'text': 'done'}
    monkeypatch.setattr(dispatch, '_in_process_chat', lambda *a, **k: fake_chat)
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, 'local_dispatch_provider_breaker_open',
                        lambda *a, **k: '')
    return ran


def _permit_free():
    ok = dispatch._local_llm_semaphore.acquire(timeout=0.2)
    if ok:
        dispatch._local_llm_semaphore.release()
    return ok


def test_a_cancel_while_waiting_for_the_permit_returns_at_once(chat):
    cancel = threading.Event()
    assert dispatch._local_llm_semaphore.acquire(timeout=1)
    out = {}
    try:
        t = threading.Thread(target=lambda: out.update(r=dispatch.local_chat_dispatch(
            'p', 'u', 'pid', daemon_id='a2a_x', cancel_event=cancel)))
        t.start()
        time.sleep(0.3)
        started = time.monotonic()
        cancel.set()
        t.join(timeout=5)
        waited = time.monotonic() - started
    finally:
        dispatch._local_llm_semaphore.release()
    assert out['r'] == ('cancelled', None)
    assert waited < 2, waited
    assert chat == []
    assert _permit_free()


def test_a_cancel_before_the_turn_releases_the_permit_it_took(chat):
    cancel = threading.Event()
    cancel.set()
    assert dispatch.local_chat_dispatch('p', 'u', 'pid', daemon_id='a2a_x',
                                        cancel_event=cancel) == ('cancelled', None)
    assert chat == []
    assert _permit_free()


def test_no_cancel_event_runs_the_turn_as_before(chat):
    assert dispatch.local_chat_dispatch('p', 'u', 'pid',
                                        daemon_id='a2a_x') == ('ok', 'done')
    assert chat == ['p']
    assert _permit_free()


def test_a_busy_permit_still_defers_after_the_wait(chat, monkeypatch):
    monkeypatch.setattr(dispatch, '_LOCAL_LLM_WAIT_S', 0.3)
    assert dispatch._local_llm_semaphore.acquire(timeout=1)
    try:
        r = dispatch.local_chat_dispatch('p', 'u', 'pid', daemon_id='a2a_x',
                                         cancel_event=threading.Event())
    finally:
        dispatch._local_llm_semaphore.release()
    assert r == ('deferred', None)


def test_the_dynamic_executor_hands_the_cancel_to_the_dispatch(monkeypatch):
    """The A2A executor for a trained agent passes the task's cancel_event
    through, and a cancelled dispatch fails the task, never completes it."""
    import asyncio
    import types
    from integrations.google_a2a import dynamic_agent_registry as dar
    seen = {}

    def spy(message, user_id, prompt_id, daemon_id=None, cancel_event=None):
        seen['cancel_event'] = cancel_event
        return 'cancelled', None
    monkeypatch.setattr(dispatch, 'local_chat_dispatch', spy)
    ex = dar.DynamicAgentExecutor.__new__(dar.DynamicAgentExecutor)
    agent = types.SimpleNamespace(persona='p', prompt_id='42', metadata={})
    ex.discovery = types.SimpleNamespace(get_agent_by_id=lambda a: agent)
    ev = threading.Event()
    with pytest.raises(RuntimeError, match='cancel'):
        asyncio.run(ex.execute_agent_task('42_0', 'hi', 'ctx',
                                          cancel_event=ev))
    assert seen['cancel_event'] is ev


def test_a_cancel_that_lands_as_the_permit_is_taken_gives_it_back(
        chat, monkeypatch):
    """The window between the acquire and the turn: the permit was taken,
    the cancel arrived, the turn must not start and the permit comes back."""
    real = threading.Semaphore(1)
    cancel = threading.Event()

    class _CancelOnAcquire:
        def acquire(self, timeout=None):
            got = real.acquire(timeout=timeout)
            if got:
                cancel.set()
            return got

        def release(self):
            real.release()
    monkeypatch.setattr(dispatch, '_local_llm_semaphore', _CancelOnAcquire())
    assert dispatch.local_chat_dispatch('p', 'u', 'pid', daemon_id='a2a_x',
                                        cancel_event=cancel) == ('cancelled', None)
    assert chat == []
    assert real.acquire(timeout=0.2), 'the permit was not given back'


def test_a_cancel_mid_turn_refuses_the_turns_next_llm_call(monkeypatch):
    from core import http_pool
    from hartos.threadlocal import thread_local_data
    monkeypatch.setattr(dispatch, 'is_user_recently_active', lambda: False)
    monkeypatch.setattr(dispatch, 'local_dispatch_provider_breaker_open',
                        lambda *a, **k: '')
    monkeypatch.setattr(http_pool, '_is_llama_completion_url', lambda url: True)
    on_wire, first_call, go_on = [], threading.Event(), threading.Event()

    class _Resp:
        status_code = 200

        def json(self):
            return {'choices': [{'message': {'content': 'ok'}}]}

    class _Session:
        def post(self, url, timeout=None, **kw):
            on_wire.append(kw.get('json', {}).get('user'))
            if len(on_wire) == 1:
                first_call.set()
                go_on.wait(5)
            return _Resp()
    monkeypatch.setattr(http_pool, '_llama_session_for', lambda kind: _Session())

    def turn(**kw):
        """Three LLM calls, the way a /chat turn makes them; an exception
        from the transport ends the turn, as the pipeline's does."""
        thread_local_data.set_request_id(kw['request_id'])
        for i in range(3):
            http_pool.pooled_post('http://127.0.0.1:8080/v1/chat/completions',
                                  json={'messages': [{'role': 'user',
                                                      'content': str(i)}]})
        return {'text': 'done'}
    monkeypatch.setattr(dispatch, '_in_process_chat', lambda *a, **k: turn)
    cancel = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=dispatch.local_chat_dispatch(
        'p', 'u', 'pid', daemon_id='a2a_ctx1', cancel_event=cancel)))
    t.start()
    assert first_call.wait(5)
    cancel.set()
    go_on.set()
    t.join(5)
    assert out['r'] == ('cancelled', None), out
    assert on_wire == ['daemon_a2a_ctx1'], on_wire
    assert _permit_free()


def test_a_turn_queued_for_a_slot_wakes_on_its_cancel(monkeypatch):
    from core.llama_scheduler import LlamaScheduler, TurnCancelled
    s = LlamaScheduler(n_slots=1)
    held = s.acquire('someone-else', 'daemon')
    cancel = threading.Event()
    s.bind_cancel('daemon_a2a_q', cancel)
    out = {}

    def call():
        try:
            with s.slot('daemon_a2a_q', 'daemon', timeout=30):
                out['ran'] = True
        except TurnCancelled:
            out['cancelled'] = time.monotonic()
    t = threading.Thread(target=call)
    t.start()
    time.sleep(0.3)
    started = time.monotonic()
    cancel.set()
    t.join(5)
    s.release(held)
    s.unbind_cancel('daemon_a2a_q')
    assert 'ran' not in out and out['cancelled'] - started < 2, out
    assert s.stats()['in_flight'] == 0


def test_other_turns_are_untouched_by_a_cancel(monkeypatch):
    from core.llama_scheduler import LlamaScheduler
    s = LlamaScheduler(n_slots=2)
    cancel = threading.Event()
    s.bind_cancel('daemon_a2a_a', cancel)
    cancel.set()
    with s.slot('daemon_other', 'daemon', timeout=1) as tok:
        assert tok is not None
    s.unbind_cancel('daemon_a2a_a')
    with s.slot('daemon_a2a_a', 'daemon', timeout=1) as tok:
        assert tok is not None, 'an unbound id is admitted again'


def test_a_task_cancel_while_the_permit_is_held_means_the_turn_never_starts(
        chat, monkeypatch):
    """Review of a4ea04651, finding 4: the cancel must reach the dispatch
    through the REAL registered executor (register_dynamic_agents), the
    REAL A2A handler and a REAL task/cancel; dropping the cancel_event
    anywhere on that path let the turn run once the permit came back."""
    import asyncio
    from integrations.google_a2a import dynamic_agent_registry as dar
    from integrations.google_a2a import register_dynamic_agents as rda
    from integrations.google_a2a.google_a2a_integration import (
        A2AMessageHandler, TaskState)
    agent = dar.TrainedAgent(
        agent_id='77_0', prompt_id=77, flow_id=0, persona='ops', action='a',
        recipe=[], status='done', can_perform_without_user_input='yes',
        fallback_action='', metadata={'user_id': 'u'}, recipe_file='')
    ex = dar.DynamicAgentExecutor.__new__(dar.DynamicAgentExecutor)
    ex.discovery = type('D', (), {'get_agent_by_id': lambda self, a: agent})()
    monkeypatch.setattr(rda, 'get_dynamic_executor', lambda: ex)
    handler = A2AMessageHandler(rda.create_dynamic_executor_function(agent))
    assert dispatch._local_llm_semaphore.acquire(timeout=1)
    released = False
    try:
        task = asyncio.run(handler.handle_message_send(
            {'message': {'parts': [{'kind': 'text', 'text': 'hi'}]},
             'configuration': {'blocking': False}}, caller='peer:x'))
        time.sleep(0.4)
        out = asyncio.run(handler.handle_task_cancel(
            {'taskId': task['id']}, caller='peer:x'))
        assert out.get('success'), out
        dispatch._local_llm_semaphore.release()
        released = True
        time.sleep(1.0)
    finally:
        if not released:
            dispatch._local_llm_semaphore.release()
    assert chat == [], 'the cancelled turn ran once the permit came back'
    assert handler.tasks[task['id']].state == TaskState.FAILED
    assert _permit_free()


def test_a_turn_that_swallows_the_refusal_still_reports_cancelled(chat,
                                                                  monkeypatch):
    """A pipeline that catches the refusal and returns a polite sentence must
    not have that sentence reported as the cancelled turn's answer."""
    cancel = threading.Event()

    def turn(**kw):
        cancel.set()            # the cancel lands mid-turn
        return {'text': 'Sorry, something went wrong.'}
    monkeypatch.setattr(dispatch, '_in_process_chat', lambda *a, **k: turn)
    assert dispatch.local_chat_dispatch('p', 'u', 'pid', daemon_id='a2a_s',
                                        cancel_event=cancel) == ('cancelled', None)
    assert _permit_free()


# ── review of f97b6bed8 follow-ups ──────────────────────────────────────

def test_two_tasks_in_one_context_keep_their_own_cancel(monkeypatch):
    """F4: the binding was keyed by daemon_a2a_<contextId>, which the PEER
    chooses; two tasks in one context overwrote each other's cancel.  Each
    task's turn now has its own request id."""
    import asyncio
    import types
    from integrations.google_a2a import dynamic_agent_registry as dar
    seen = []

    def spy(message, user_id, prompt_id, daemon_id=None, cancel_event=None):
        seen.append(daemon_id)
        return 'ok', 'done'
    monkeypatch.setattr(dispatch, 'local_chat_dispatch', spy)
    ex = dar.DynamicAgentExecutor.__new__(dar.DynamicAgentExecutor)
    agent = types.SimpleNamespace(persona='p', prompt_id='42', metadata={})
    ex.discovery = types.SimpleNamespace(get_agent_by_id=lambda a: agent)
    for tid in ('task-1', 'task-2'):
        asyncio.run(ex.execute_agent_task('42_0', 'hi', 'same-ctx',
                                          cancel_event=threading.Event(),
                                          task_id=tid))
    assert len(set(seen)) == 2, seen
    assert all('same-ctx' not in d for d in seen), seen


def test_a_mid_turn_cancel_is_reported_as_what_happened(monkeypatch, caplog):
    """F2: a turn cancelled mid-flight was reported 'cancelled by the caller
    before its turn started' and logged at ERROR."""
    import asyncio
    import logging
    import types
    from integrations.google_a2a import dynamic_agent_registry as dar
    from integrations.google_a2a import register_dynamic_agents as rda
    monkeypatch.setattr(dispatch, 'local_chat_dispatch',
                        lambda *a, **k: ('cancelled', None))
    agent = dar.TrainedAgent(
        agent_id='77_0', prompt_id=77, flow_id=0, persona='ops', action='a',
        recipe=[], status='done', can_perform_without_user_input='yes',
        fallback_action='', metadata={'user_id': 'u'}, recipe_file='')
    ex = dar.DynamicAgentExecutor.__new__(dar.DynamicAgentExecutor)
    ex.discovery = types.SimpleNamespace(get_agent_by_id=lambda a: agent)
    monkeypatch.setattr(rda, 'get_dynamic_executor', lambda: ex)
    run = rda.create_dynamic_executor_function(agent)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError) as err:
            asyncio.run(run('hi', 'ctx', cancel_event=threading.Event()))
    assert 'before its turn started' not in str(err.value)
    assert 'cancelled' in str(err.value)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        [r.getMessage() for r in caplog.records]


def test_the_httpx_path_refuses_a_cancelled_turn(monkeypatch):
    """The refusal holds on the httpx/openai transport too (autogen and
    langchain calls), not only pooled_post: the patched Client.send admits
    through the same scheduler slot."""
    import httpx
    from core import llm_outbound_logger as lol
    from core.llama_scheduler import TurnCancelled, get_scheduler
    from hartos.threadlocal import thread_local_data
    lol.install()
    monkeypatch.setattr(lol, '_is_target_request', lambda url, method: True)
    sent = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: sent.append(req) or httpx.Response(200, json={})))
    monkeypatch.setattr(lol, '_select_send_client', lambda self, req: self)
    rid = 'daemon_a2a_httpx_probe'
    cancel = threading.Event()
    cancel.set()
    get_scheduler().bind_cancel(rid, cancel)
    thread_local_data.set_request_id(rid)
    try:
        with pytest.raises(TurnCancelled):
            client.post('http://127.0.0.1:8080/v1/chat/completions',
                        json={'messages': [{'role': 'user', 'content': 'x'}]})
    finally:
        get_scheduler().unbind_cancel(rid)
        thread_local_data.set_request_id(None)
    assert sent == []

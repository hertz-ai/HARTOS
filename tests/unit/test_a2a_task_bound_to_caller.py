"""An A2A task answers only the caller that started it, and the task table
stays bounded.

Review finding M5 (2026-09-26): A2AMessageHandler.tasks held every task
forever (one entry per message/send, never removed), and message/get /
task/cancel served any admitted caller that named a task id: one verified
peer could read another's result or cancel its turn.  Each task is now
bound to the identity that was admitted for its message/send (the peer's
node_id, or the /chat gate's user / key / address) and get and cancel from
anyone else answer "not found" (existence is not disclosed).  Finished
tasks are evicted after HEVOLVE_A2A_TASK_TTL_S, and past
HEVOLVE_A2A_TASK_MAX the oldest finished go first; a running task is never
evicted.

The route tests drive the REAL Flask app, gate and jsonrpc view with two
real node keys (tests/unit/test_a2a_admitted_peer_runs_shared_agent.py's
harness); the handler tests call the real A2AMessageHandler.
"""
import asyncio
import threading
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import integrations.google_a2a.google_a2a_integration as gai
import security.node_integrity as ni
from integrations.google_a2a.google_a2a_integration import (
    A2AMessageHandler, TaskState)
from integrations.social.sync_engine import SyncEngine
from tests.unit.test_a2a_admitted_peer_runs_shared_agent import (  # noqa: F401
    AGENT, _admit, _env, _post, _signed, invoker, node)


def _as_second_node(monkeypatch):
    """Sign as a DIFFERENT verified node from here on."""
    priv = Ed25519PrivateKey.generate()
    monkeypatch.setattr(ni, '_private_key', priv)
    monkeypatch.setattr(ni, '_public_key', priv.public_key())
    other = f'livetest-b-{uuid.uuid4().hex[:8]}'
    from tests.unit import test_a2a_admitted_peer_runs_shared_agent as h
    monkeypatch.setattr(SyncEngine, 'canonical_node_id', staticmethod(
        lambda: {'invoker': other, 'server': h.SERVER_ID}[h._ROLE['who']]))
    _admit(other, ni.get_public_key_hex())
    return other


def _send(node, **params):
    code, body = _post(node, _signed(params={
        'message': {'messageId': uuid.uuid4().hex,
                    'parts': [{'kind': 'text', 'text': 'x'}]}, **params}))
    assert code == 200, body
    return body['result']['id']


def test_another_peer_cannot_read_or_cancel_the_task(node, invoker,
                                                     monkeypatch):
    _admit(invoker.node_id, invoker.public_key)
    task_id = _send(node)
    code, mine = _post(node, _signed(method='message/get',
                                     params={'taskId': task_id}))
    assert mine['result']['state'] == 'completed', mine
    _as_second_node(monkeypatch)
    code, theirs = _post(node, _signed(method='message/get',
                                       params={'taskId': task_id}))
    assert 'not found' in theirs['result']['error']['message'], theirs
    assert 'content' not in theirs['result']
    code, cancel = _post(node, _signed(method='task/cancel',
                                       params={'taskId': task_id}))
    assert 'not found' in cancel['result']['error']['message'], cancel


# ── the handler itself ──────────────────────────────────────────────────

async def _ok(text, ctx):
    return {'role': 'model', 'parts': [{'text': 'ok'}]}


def _run(coro):
    return asyncio.run(coro)


def _send_as(h, caller, blocking=True):
    params = {'message': {'messageId': uuid.uuid4().hex,
                          'parts': [{'kind': 'text', 'text': 'x'}]}}
    if not blocking:
        params['configuration'] = {'blocking': False}
    return _run(h.handle_message_send(params, caller=caller))['id']


def test_a_user_caller_is_bound_too():
    h = A2AMessageHandler(_ok)
    tid = _send_as(h, 'user:alice')
    assert _run(h.handle_message_get({'taskId': tid}, caller='user:alice')
                )['state'] == 'completed'
    assert 'error' in _run(h.handle_message_get({'taskId': tid},
                                                caller='user:bob'))
    assert 'error' in _run(h.handle_task_cancel({'taskId': tid},
                                                caller='user:bob'))


def test_finished_tasks_expire(monkeypatch):
    monkeypatch.setattr(gai, '_TASK_TTL_S', 0.2)
    h = A2AMessageHandler(_ok)
    old = _send_as(h, 'user:a')
    time.sleep(0.3)
    _send_as(h, 'user:a')
    assert old not in h.tasks


def test_the_table_is_capped_and_a_running_task_survives(monkeypatch):
    monkeypatch.setattr(gai, '_TASK_MAX', 5)
    release = threading.Event()
    started = threading.Event()

    async def slow(text, ctx):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {'role': 'model', 'parts': [{'text': 'late'}]}
    h = A2AMessageHandler(slow)
    running = _send_as(h, 'user:a', blocking=False)
    assert started.wait(5)
    h.agent_executor = _ok
    for _ in range(20):
        _send_as(h, 'user:a')
    assert len(h.tasks) <= 5, len(h.tasks)
    assert running in h.tasks
    assert h.tasks[running].state in (TaskState.SUBMITTED, TaskState.WORKING)
    release.set()


def test_another_callers_message_id_does_not_replace_the_task():
    """The messageId is chosen by the caller; reusing someone else's must
    not overwrite (and so read or hijack) their task."""
    h = A2AMessageHandler(_ok)
    mid = uuid.uuid4().hex
    params = {'message': {'messageId': mid,
                          'parts': [{'kind': 'text', 'text': 'x'}]}}
    _run(h.handle_message_send(params, caller='user:alice'))
    theirs = _run(h.handle_message_send(params, caller='user:mallory'))
    assert theirs['id'] != mid
    assert h.tasks[mid].owner == 'user:alice'


# ── review of 436580009: the identity must be a VERIFIED one ────────────

def _unsigned(method, params):
    return {'jsonrpc': '2.0', 'id': uuid.uuid4().hex, 'method': method,
            'params': params}


def _start(node, environ, headers=None):
    with _server():
        r = node.client.post(f'/a2a/{AGENT}/jsonrpc', json=_unsigned(
            'message/send', {'message': {'messageId': uuid.uuid4().hex,
                                         'parts': [{'kind': 'text',
                                                    'text': 'x'}]}}),
            environ_base=environ, headers=headers or {})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['result']['id']


def _read(node, task_id, environ, headers=None):
    with _server():
        r = node.client.post(f'/a2a/{AGENT}/jsonrpc', json=_unsigned(
            'message/get', {'taskId': task_id}),
            environ_base=environ, headers=headers or {})
    return r.status_code, r.get_json()


def _server():
    from tests.unit import test_a2a_admitted_peer_runs_shared_agent as h
    return h._AsServer()


def _hidden(resp):
    code, body = resp
    return code != 200 or 'not found' in (
        (body.get('result') or {}).get('error') or {}).get('message', '')


def test_an_unverified_key_header_is_not_an_identity(node, monkeypatch):
    """Finding 2: a local caller sending 'X-API-Key: bogus' read the task a
    remote holder of the REAL key started (both came out 'api_key')."""
    monkeypatch.setenv('HEVOLVE_API_KEY', 'the-real-key')
    remote = {'REMOTE_ADDR': '198.51.100.23'}
    local = {'REMOTE_ADDR': '127.0.0.1'}
    tid = _start(node, remote, {'X-API-Key': 'the-real-key'})
    assert not _hidden(_read(node, tid, remote, {'X-API-Key': 'the-real-key'}))
    assert _hidden(_read(node, tid, local, {'X-API-Key': 'bogus'}))
    # The verified key IS the identity: its holder reads from another address.
    assert not _hidden(_read(node, tid, {'REMOTE_ADDR': '198.51.100.24'},
                             {'X-API-Key': 'the-real-key'}))


def test_two_lan_addresses_are_two_callers(node, monkeypatch):
    """Finding 3: on a flat node the gate admits the LAN; each address is
    its own caller."""
    monkeypatch.delenv('NUNBA_BUNDLED', raising=False)
    monkeypatch.setenv('HEVOLVE_NODE_TIER', 'flat')
    a, b = {'REMOTE_ADDR': '10.0.0.5'}, {'REMOTE_ADDR': '10.0.0.9'}
    tid = _start(node, a)
    assert not _hidden(_read(node, tid, a))
    assert _hidden(_read(node, tid, b))
    assert _hidden(_read(node, tid, b, {'X-API-Key': 'something-else'}))


def test_two_signed_in_users_are_two_callers(node, monkeypatch):
    from unittest.mock import patch
    remote = {'REMOTE_ADDR': '198.51.100.23'}
    users = {'Bearer alice': {'user_id': 'alice'},
             'Bearer bob': {'user_id': 'bob'}}
    with patch('integrations.social.auth.decode_jwt',
               side_effect=lambda t: users.get('Bearer ' + t)):
        tid = _start(node, remote, {'Authorization': 'Bearer alice'})
        assert not _hidden(_read(node, tid, remote,
                                 {'Authorization': 'Bearer alice'}))
        assert _hidden(_read(node, tid, remote,
                             {'Authorization': 'Bearer bob'}))


def test_one_caller_cannot_hold_unbounded_open_tasks(monkeypatch):
    """Finding 4: every task running meant no eviction at all; one caller
    now gets 'busy' past its open-task cap, others are unaffected."""
    monkeypatch.setattr(gai, '_OPEN_TASKS_PER_CALLER', 3)
    release = threading.Event()

    async def slow(text, ctx):
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {'role': 'model', 'parts': [{'text': 'x'}]}
    h = A2AMessageHandler(slow)
    try:
        for _ in range(3):
            _send_as(h, 'peer:a', blocking=False)
        params = {'message': {'messageId': uuid.uuid4().hex,
                              'parts': [{'kind': 'text', 'text': 'x'}]},
                  'configuration': {'blocking': False}}
        busy = _run(h.handle_message_send(params, caller='peer:a'))
        assert 'busy' in busy['error']['message'], busy
        assert len(h.tasks) == 3
        _send_as(h, 'peer:b', blocking=False)
        assert len(h.tasks) == 4
    finally:
        release.set()


def test_in_flight_turns_are_capped_for_the_whole_node(monkeypatch):
    """Review of a4ea04651, finding 2: every non-blocking send started a
    thread and kept a task (60 sends, +60 threads).  ONE admission check
    bounds unfinished tasks per caller AND for the node; past either, the
    send answers 'busy' and starts nothing."""
    monkeypatch.setattr(gai, '_OPEN_TASKS_PER_CALLER', 2)
    monkeypatch.setattr(gai, '_OPEN_TASKS_MAX', 3)
    release = threading.Event()

    async def slow(text, ctx):
        while not release.is_set():
            await asyncio.sleep(0.02)
        return {'role': 'model', 'parts': [{'text': 'x'}]}
    h = A2AMessageHandler(slow)
    threads_before = threading.active_count()
    try:
        for who in ('peer:a', 'peer:b', 'peer:c'):
            _send_as(h, who, blocking=False)
        params = {'message': {'messageId': uuid.uuid4().hex,
                              'parts': [{'kind': 'text', 'text': 'x'}]},
                  'configuration': {'blocking': False}}
        busy = _run(h.handle_message_send(params, caller='peer:d'))
        assert 'busy' in busy['error']['message'], busy
        assert len(h.tasks) == 3
        assert threading.active_count() - threads_before <= 3
    finally:
        release.set()

"""Stop AI Control stops every screen-driving loop on the node, for local callers.

The indicator's Stop is the human's brake on the AI driving this machine's
one screen.  vlm_stop only bulk-stopped the sessions of the user_id it was
sent, and each VLM loop is registered under its agent's creator, so the
desktop owner's Stop left another creator's loop driving the mouse.  Live
2026-09-14: the loop for agent 88659566083 ran as its creator while the
signed-in owner was a different user, and agents on that node have 43
creators.  {"scope": "node"} stops every loop integrations.vlm.local_loop
tracks, and only for a caller on this machine; a token holder on another
machine keeps the per-user stop.
"""
import logging
import os
import tempfile

import pytest

LAN = {'REMOTE_ADDR': '192.168.0.50'}
TOKEN = 'node-scope-test-token'


@pytest.fixture(scope='module')
def hie():
    with pytest.MonkeyPatch.context() as mp:
        if not os.environ.get('HEVOLVE_CACHE_DIR'):
            mp.setenv('HEVOLVE_CACHE_DIR', tempfile.mkdtemp())
        import hart_intelligence_entry  # noqa: TID251 -- the route under test is on its app
        yield hart_intelligence_entry


@pytest.fixture
def loops():
    from integrations.vlm import local_loop
    pairs = [('owner-1', '101'), ('creator-2', '202')]
    for uid, pid in pairs:
        local_loop._register_session(uid, pid)
    yield local_loop, pairs
    for uid, pid in pairs:
        local_loop._unregister_session(uid, pid)


@pytest.fixture
def client(hie, monkeypatch):
    import core.auth_local as auth_local
    monkeypatch.setattr(auth_local, 'API_TOKEN', TOKEN)
    monkeypatch.delenv('NUNBA_CI', raising=False)
    monkeypatch.delenv('TRUSTED_PROXY', raising=False)
    return hie.app.test_client()


def _stopped(local_loop, pairs):
    return [local_loop._is_stop_requested(uid, pid) for uid, pid in pairs]


def test_local_node_stop_halts_every_creators_loop(client, loops):
    local_loop, pairs = loops
    resp = client.post('/api/vlm/stop', json={'scope': 'node', 'user_id': 'owner-1'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['status'] == 'stopped'
    assert sorted((s['user_id'], s['prompt_id'])
                  for s in body['stopped_sessions']) == sorted(pairs)
    assert _stopped(local_loop, pairs) == [True, True]


def test_node_stop_needs_no_user_id(client, loops):
    local_loop, pairs = loops
    resp = client.post('/api/vlm/stop', json={'scope': 'node'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _stopped(local_loop, pairs) == [True, True]


def test_node_stop_from_another_machine_is_refused_even_with_a_token(client, loops):
    local_loop, pairs = loops
    resp = client.post('/api/vlm/stop', json={'scope': 'node', 'user_id': 'owner-1'},
                       environ_overrides=LAN,
                       headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 403, resp.get_data(as_text=True)
    assert _stopped(local_loop, pairs) == [False, False]


def test_remote_token_holder_still_stops_only_their_own(client, loops):
    local_loop, pairs = loops
    resp = client.post('/api/vlm/stop', json={'user_id': 'owner-1'},
                       environ_overrides=LAN,
                       headers={'Authorization': f'Bearer {TOKEN}'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert _stopped(local_loop, pairs) == [True, False]


def test_no_user_and_no_scope_is_still_400(client, loops):
    assert client.post('/api/vlm/stop', json={}).status_code == 400


def test_node_stop_with_nothing_running(client):
    resp = client.post('/api/vlm/stop', json={'scope': 'node'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['status'] == 'no_active_session'


def test_node_stop_is_logged_with_its_count(hie, client, loops, caplog, monkeypatch):
    # Outside the bundle app.logger keeps its own handlers and does not
    # propagate (hart_intelligence_entry.py:1019-1023); caplog listens on root.
    monkeypatch.setattr(hie.app.logger, 'propagate', True)
    with caplog.at_level(logging.WARNING):
        client.post('/api/vlm/stop', json={'scope': 'node'})
    lines = [r.getMessage() for r in caplog.records if 'node-wide' in r.getMessage()]
    assert len(lines) == 1 and '2 loop(s)' in lines[0], lines

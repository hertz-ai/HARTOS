"""The idle shell's read-only state is PUSHED on the one SSE stream, not polled.

MEASURED on the box 2026-09-22: ~18 GETs per 5 s at idle, from four pollers
(system/metrics 4 s, ai-sensing 4 s, connectivity/summary 8 s, dashboard/agents
5 s) running in every shell document, while the server already kept each of
those fresh itself (the connectivity prober thread, the metrics reader, the
senses gate). The fix is the existing transport: the server samples once per
box, only while a stream is open, and pushes `shell_state` events on
/api/notifications/stream, the stream the shell already holds. The pollers in
the modules become a 30 s fallback for a stream that is down.

Behavioural, no mocks of the code under test: the REAL _ConnectivityCache with
its probes stubbed, the REAL LiquidUIService.push_shell_state / sampler /
stream generator, the REAL route, read through the same generator the browser
reads.

    python -m pytest -q -p no:cacheprovider tests/unit/test_shell_state_push.py
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from integrations.agent_engine import liquid_ui_service as lus
from integrations.agent_engine.liquid_ui_service import LiquidUIService


def _cp(rc, out=''):
    return SimpleNamespace(returncode=rc, stdout=out)


# ── the connectivity prober tells subscribers when its snapshot CHANGES ───────

def test_connectivity_cache_notifies_on_change_only(monkeypatch):
    cache = lus._ConnectivityCache()
    monkeypatch.setattr(cache, '_probe_rfkill_wifi', lambda *a, **k: 'none')
    monkeypatch.setattr(lus, '_volume_get', lambda **k: {'available': False, 'volume': None, 'muted': None})
    state = {'ssid': 'HomeNet'}

    def fake_run(cmd, *a, **k):
        if cmd[:3] == ['nmcli', 'radio', 'wifi']:
            return _cp(0, 'enabled\n')
        if cmd[:2] == ['nmcli', '-t'] and 'ACTIVE,SSID,SIGNAL' in cmd:
            return _cp(0, 'yes:%s:71\n' % state['ssid'])
        return _cp(1, '')
    monkeypatch.setattr(lus.subprocess, 'run', fake_run)

    seen = []
    cache.subscribe(seen.append)
    cache.refresh()
    assert len(seen) == 1 and seen[0]['wifi']['ssid'] == 'HomeNet', 'the first snapshot is a change'
    cache.refresh()
    assert len(seen) == 1, 'an identical snapshot must NOT notify (no idle churn on the stream)'
    state['ssid'] = 'Cafe'
    cache.refresh()
    assert len(seen) == 2 and seen[1]['wifi']['ssid'] == 'Cafe'
    seen[1]['wifi']['ssid'] = 'mutated'
    assert cache.summary()['wifi']['ssid'] == 'Cafe', 'subscribers get a copy, never the live cache'


def test_connectivity_cache_survives_a_raising_subscriber(monkeypatch):
    cache = lus._ConnectivityCache()
    monkeypatch.setattr(cache, '_probe_wifi', lambda: {'available': True})
    monkeypatch.setattr(cache, '_probe_bluetooth', lambda: {})
    monkeypatch.setattr(cache, '_probe_battery', lambda: {})
    monkeypatch.setattr(cache, '_probe_wifi_list', lambda: {'networks': [], 'connected': {}})
    monkeypatch.setattr(lus, '_volume_get', lambda **k: {})
    seen = []

    def bad(_):
        raise RuntimeError('subscriber bug')
    cache.subscribe(bad)
    cache.subscribe(seen.append)
    cache.refresh()
    assert seen, 'a raising subscriber must not stop the others, nor the prober'


# ── push: dedupe, cursor stamp, and the SSE condition wake ────────────────────

@pytest.fixture
def svc():
    s = LiquidUIService()
    yield s


def test_push_shell_state_is_stamped_deduped_and_wakes_the_stream(svc):
    before = time.time()
    assert svc.push_shell_state('senses', {'disabled': {'mic': False}}) is True
    ev = svc._shell_state['senses']
    assert ev['type'] == 'shell_state' and ev['kind'] == 'senses'
    assert ev['_ts'] >= before, 'no _ts cursor stamp: the producer would never emit it'
    assert svc.push_shell_state('senses', {'disabled': {'mic': False}}) is False, (
        'an unchanged payload must not be re-pushed (that is the idle churn the diet removes)')
    assert svc.push_shell_state('senses', {'disabled': {'mic': True}}) is True

    woke = {}
    started = threading.Event()

    def waiter():
        with svc._ui_event_cv:
            started.set()
            svc._ui_event_cv.wait(timeout=10.0)
        woke['t'] = time.monotonic()
    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    assert started.wait(2.0)
    time.sleep(0.05)
    t0 = time.monotonic()
    svc.push_shell_state('metrics', {'cpu_percent': 3.0})
    t.join(3.0)
    assert 't' in woke and woke['t'] - t0 < 0.5, 'a push must wake the SSE producer promptly'


# ── the stream: snapshot on connect, then pushes as they happen ───────────────

def _open_stream(svc, monkeypatch):
    # No hardware sampling in the unit test: the sampler thread is what the
    # stream starts, so pin its readers to instant stand-ins.
    monkeypatch.setattr(svc, '_read_shell_metrics', lambda: {'cpu_percent': 1.0, 'ram': {'percent': 2.0}, 'disk_percent': 3.0})
    monkeypatch.setattr(svc, '_read_shell_senses', lambda: {'disabled': {}})
    monkeypatch.setattr(svc, '_read_shell_agents', lambda: None)
    monkeypatch.setattr(lus, '_connectivity_cache', SimpleNamespace(
        start=lambda: None, summary=lambda: {'wifi': {'available': False}}, subscribe=lambda fn: None))
    app = svc._create_flask_app()
    ctx = app.test_request_context('/api/notifications/stream')
    ctx.push()
    resp = app.view_functions['notification_stream']()
    return ctx, resp, iter(resp.response)


def _frame(chunk):
    if isinstance(chunk, bytes):
        chunk = chunk.decode('utf-8')
    return chunk


def test_stream_sends_the_current_shell_state_right_after_the_head(svc, monkeypatch):
    svc.push_shell_state('senses', {'disabled': {'mic': True}})
    ctx, resp, stream = _open_stream(svc, monkeypatch)
    try:
        head = _frame(next(stream))
        assert head.startswith(':'), 'the priming comment still comes first (test_liquid_ui_sse_event_driven)'
        t0 = time.time()
        second = _frame(next(stream))
        assert time.time() - t0 < 2.0, 'the snapshot must not wait for a heartbeat'
        assert second.startswith('data: ')
        events = json.loads(second[len('data: '):])
        kinds = {e['kind']: e for e in events if e.get('type') == 'shell_state'}
        assert 'senses' in kinds and kinds['senses']['payload'] == {'disabled': {'mic': True}}, (
            'a freshly connected client must get the state it would otherwise have polled for')
        assert kinds['senses']['agent'] == 'shell'
    finally:
        resp.response.close()
        ctx.pop()


def test_a_push_after_connect_arrives_as_the_next_frame(svc, monkeypatch):
    svc.push_shell_state('senses', {'disabled': {'mic': False}})
    ctx, resp, stream = _open_stream(svc, monkeypatch)
    try:
        next(stream)                      # : ok
        next(stream)                      # snapshot
        time.sleep(0.02)                  # a coarse clock must not tie the cursor
        svc.push_shell_state('senses', {'disabled': {'mic': True}})
        t0 = time.time()
        frame = _frame(next(stream))
        assert time.time() - t0 < 2.0, 'the push must not wait for the 15 s heartbeat'
        events = json.loads(frame[len('data: '):])
        assert any(e.get('kind') == 'senses' and e['payload']['disabled']['mic'] is True for e in events)
    finally:
        resp.response.close()
        ctx.pop()


def test_stream_clients_are_counted_and_the_sampler_idles_at_zero(svc, monkeypatch):
    assert svc._shell_state_clients == 0
    ctx, resp, stream = _open_stream(svc, monkeypatch)
    try:
        next(stream)
        assert svc._shell_state_clients == 1, 'an open stream is a client'
    finally:
        resp.response.close()
        ctx.pop()
    assert svc._shell_state_clients == 0, 'closing the stream releases the client'
    # With nobody connected a tick samples NOTHING (no psutil, no backend GET).
    calls = []
    monkeypatch.setattr(svc, '_read_shell_metrics', lambda: calls.append('m') or {'cpu_percent': 1})
    monkeypatch.setattr(svc, '_read_shell_senses', lambda: calls.append('s') or {})
    monkeypatch.setattr(svc, '_read_shell_agents', lambda: calls.append('a') or None)
    assert svc._sample_shell_state() == [] and calls == [], 'zero clients: zero sampling'


# ── the sampler: one read per kind per tick, pushed only on change ────────────

def test_sampler_pushes_each_kind_and_only_on_change(svc, monkeypatch):
    svc._shell_state_clients = 1
    agents = {'agents': [{'name': 'Researcher', 'status': 'running'},
                         {'name': 'Idle', 'status': 'stopped'},
                         {'goal_type': 'writer-long-name-here', 'status': 'running'}]}
    monkeypatch.setattr(svc, '_read_shell_metrics', lambda: {'cpu_percent': 12.3, 'ram': {'percent': 40.0}, 'disk_percent': 55.0})
    monkeypatch.setattr(svc, '_read_shell_senses', lambda: {'disabled': {'mic': False}})
    monkeypatch.setattr(svc, '_read_shell_agents', lambda: agents)
    pushed = svc._sample_shell_state()
    assert sorted(pushed) == ['agents', 'metrics', 'senses']
    ag = svc._shell_state['agents']['payload']
    assert ag == {'count': 2, 'names': ['Researcher', 'writer-long-name']}, (
        'the agents payload is the reduced form the top bar paints: running only, 16-char names')
    assert svc._sample_shell_state() == [], 'nothing changed: nothing pushed'
    agents['agents'][0]['status'] = 'stopped'
    assert svc._sample_shell_state() == ['agents']


def test_sampler_keeps_going_when_a_reader_fails(svc, monkeypatch):
    svc._shell_state_clients = 1

    def boom():
        raise RuntimeError('psutil exploded')
    monkeypatch.setattr(svc, '_read_shell_metrics', boom)
    monkeypatch.setattr(svc, '_read_shell_senses', lambda: {'disabled': {'mic': True}})
    monkeypatch.setattr(svc, '_read_shell_agents', lambda: None)   # backend down: no push, no error
    assert svc._sample_shell_state() == ['senses']


def test_metrics_reader_is_the_route_reduced_to_what_the_widget_paints(svc):
    m = svc._read_shell_metrics()
    assert set(m) == {'cpu_percent', 'ram', 'disk_percent'}
    assert 'percent' in m['ram'], 'the widget reads m.ram.percent (test_system_metrics_widget)'


def test_metrics_route_and_reader_share_one_implementation(svc):
    """The route still serves the full shape (the fallback poll and the System
    panel read it); the sampler reads the same function, so the two cannot
    drift into different numbers for the same box."""
    full = lus.read_system_metrics()
    assert 'ram' in full and 'percent' in full['ram']
    client = svc._create_flask_app().test_client()
    r = client.get('/api/shell/system/metrics')
    assert r.status_code == 200 and 'ram' in r.get_json()


def test_agents_reader_returns_none_when_the_backend_is_unreachable(svc):
    svc.backend_port = 1   # nothing listens here
    assert svc._read_shell_agents() is None


def test_model_check_backs_off_once_the_bus_is_up():
    assert LiquidUIService._model_check_interval(False) == 10.0, 'keep the 10 s probe while the bus is not yet up'
    assert LiquidUIService._model_check_interval(True) >= 30.0, 'a bus that is up is not re-asked every 10 s'

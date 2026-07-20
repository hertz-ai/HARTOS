"""Shell observability + wifi-popover responsiveness — public-contract coverage.

Two independent fixes to integrations/agent_engine/liquid_ui_service.py:

FIX B — the shell WebView's JS console never reaches journald, so a runtime throw
(e.g. hartRenderPersonalize) or an unhandled rejection was an invisible blind
spot. A new POST /api/shell/clientlog route forwards client error records to the
module logger. It is bounded (ignores >8KB bodies), best-effort, and NEVER 500s.

FIX C — the background connectivity prober listed wifi with a bare
`nmcli ... device wifi list`, which can trigger a blocking radio rescan. The probe
now passes `--rescan no` so it reads the CACHED scan (instant, non-blocking); a
fresh scan only happens on an explicit user rescan action.

Behavioural (no grep / no source-string assertions): each test imports the real
code, mocks the boundary (the module logger, or the prober's _run), calls the real
function through the real Flask app, and asserts observable behaviour (logger call
args, HTTP status/body, the exact nmcli argv the probe builds).
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from integrations.agent_engine import liquid_ui_service as lus


# ─────────────────────────────────────────────────────────────────────────────
# FIX C — the wifi-list probe reads the cached scan (--rescan no)
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_wifi_list_passes_rescan_no():
    """_probe_wifi_list must ask nmcli for the CACHED scan (`--rescan no`) so
    popover-open never triggers a blocking radio rescan. Mock the prober's _run
    boundary and assert the exact argv it was handed."""
    cache = lus._ConnectivityCache()
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if 'SSID,SIGNAL,SECURITY,ACTIVE' in cmd:
            return SimpleNamespace(returncode=0,
                                   stdout='HomeNet:71:WPA2:yes\nCafe:30:--:no\n')
        return SimpleNamespace(returncode=1, stdout='')  # hostname -> unavailable

    with mock.patch.object(cache, '_run', side_effect=fake_run):
        result = cache._probe_wifi_list()

    # Find the nmcli wifi-list invocation and prove it carries `--rescan no`.
    wifi_list_cmds = [c for c in calls
                      if c[:1] == ['nmcli'] and 'list' in c]
    assert len(wifi_list_cmds) == 1, calls
    argv = wifi_list_cmds[0]
    assert 'device' in argv and 'wifi' in argv and 'list' in argv
    # The two tokens must be adjacent and in order (nmcli option syntax).
    assert '--rescan' in argv, argv
    assert argv[argv.index('--rescan') + 1] == 'no', argv

    # The probe still parses networks normally with the cached scan.
    assert [n['ssid'] for n in result['networks']] == ['HomeNet', 'Cafe']


# ─────────────────────────────────────────────────────────────────────────────
# FIX B — POST /api/shell/clientlog forwards client errors to the logger
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def shell_client(monkeypatch):
    """Real Flask app from the shell service; the media idle-indexer is neutralised
    so building the app spawns nothing (mirrors test_shell_connectivity_cache)."""
    monkeypatch.setattr(
        'integrations.agent_engine.media_semantic_index.register_idle_indexer',
        lambda *a, **k: False)
    svc = lus.LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True
    return app.test_client()


def test_clientlog_logs_error_and_returns_ok(shell_client, monkeypatch):
    log = mock.Mock()
    monkeypatch.setattr(lus, 'logger', log)

    r = shell_client.post('/api/shell/clientlog', json={
        'level': 'error', 'message': 'TypeError: x is undefined',
        'stack': 'at hartRenderPersonalize (hartPersonalize.js:12:3)',
        'url': '/shell/static/hartPersonalize.js', 'line': 12, 'col': 3})

    assert r.status_code == 200
    assert r.get_json() == {'ok': True}
    log.error.assert_called_once()
    logged = log.error.call_args[0][0]
    assert '[shell-client]' in logged
    assert 'TypeError: x is undefined' in logged
    assert 'hartRenderPersonalize' in logged  # stack forwarded too
    log.warning.assert_not_called()


def test_clientlog_warning_level_uses_warning(shell_client, monkeypatch):
    log = mock.Mock()
    monkeypatch.setattr(lus, 'logger', log)

    r = shell_client.post('/api/shell/clientlog',
                          json={'level': 'warning', 'message': 'deprecated call'})

    assert r.status_code == 200
    log.warning.assert_called_once()
    log.error.assert_not_called()


def test_clientlog_ignores_oversized_body(shell_client, monkeypatch):
    log = mock.Mock()
    monkeypatch.setattr(lus, 'logger', log)

    huge = {'level': 'error', 'message': 'x' * 20000}
    r = shell_client.post('/api/shell/clientlog', json=huge)

    # Bounded: an oversized body is dropped, but the route still 200s cleanly and
    # never logs (so a flooding client can't blow up the journal from one call).
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}
    log.error.assert_not_called()
    log.warning.assert_not_called()


def test_clientlog_never_500s_on_bad_body(shell_client):
    """Best-effort contract: even a non-JSON / empty body returns 200 {'ok': True},
    never a 500 that would surface as a broken shell fetch."""
    r = shell_client.post('/api/shell/clientlog', data=b'not json',
                          content_type='application/json')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}

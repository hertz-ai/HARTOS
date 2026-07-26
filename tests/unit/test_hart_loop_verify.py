"""Behavioural guards for scripts/hart_loop_verify.py (the dev-loop verify leg).

The runner parses a peer's journal bundle into the signals the fix->deploy->
verify loop keys on. These call the real functions on sample bundles and assert
the extraction + the first-run/regression semantics -- no network, no peer.
"""
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'hart_loop_verify.py')
_spec = importlib.util.spec_from_file_location('hart_loop_verify', _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_SAMPLE = """
Jul 15 09:12:04 hart-node systemd[1]: Reached target Login Prompts.
Jul 15 09:12:09 hart-node systemd[1]: Reached target Network is Online.
Jul 15 09:12:14 hart-node hart-comp: first real scanout (page-flip vblank) completed - the physical display is LIVE (#131 first-scanout beacon)
Jul 15 09:12:17 hart-node hart-llm-provision[1416]: Model download failed (offline?) - LLM stays gated until a model is provided
Jul 15 09:14:28 hart-node systemd[1]: hart-vision.service: Main process exited, code=exited, status=1/FAILURE
Jul 15 09:14:28 hart-node systemd[1]: hart-vision.service: Failed with result 'exit-code'.
Jul 15 09:14:00 hart-node systemd[1]: Failed to start HART OS GPU Scheduler.
Jul 15 09:20:01 hart-node hart-backend[1174]: [shell-client] (/shell:352:12) TypeError: cannot read property foo of undefined
Jul 15 09:20:02 hart-node hart-backend[1174]: Theme applied: aura
"""


def test_parse_extracts_failed_units_and_boot_signals():
    p = mod.parse_bundle(_SAMPLE)
    assert 'hart-vision.service' in p['failed_units']
    assert 'HART OS GPU Scheduler' in p['failed_units']
    assert p['first_scanout'] is True
    assert p['llm_gated'] is True
    assert p['reached_targets'] == 2
    assert p['active_theme'] == 'aura'


def test_parse_strips_ansi_from_serial_console_journals():
    """A serial/VM boot-console journal carries inline ANSI SGR colour codes
    (systemd colourizes [ OK ]/[FAILED]). The parser must strip them so a unit
    name extracts cleanly, not as a coloured blob. Real bug: verifying a VM
    boot.log surfaced 'hart-net-diag-token.service' wrapped in escape codes."""
    ansi = (
        "\x1b[0;1;39mhart-net-diag-token.service: Main process exited, "
        "code=exited, status=1/FAILURE\x1b[0m\n"
        "\x1b[0;1;31mhart-net-diag-token.service: Failed with result 'timeout'.\x1b[0m\n"
    )
    p = mod.parse_bundle(ansi)
    assert 'hart-net-diag-token.service' in p['failed_units']
    assert not any('\x1b' in u for u in p['failed_units']), 'raw ANSI leaked into a unit name'


def test_parse_extracts_shell_client_js_errors():
    p = mod.parse_bundle(_SAMPLE)
    assert any('TypeError' in e for e in p['client_errors'])
    # the '[shell-client]' prefix is stripped, the message kept
    assert not any(e.startswith('[shell-client]') for e in p['client_errors'])


def test_first_run_is_baseline_not_regression():
    cur = mod.parse_bundle(_SAMPLE)
    d = mod.diff(None, cur)
    assert d['first_run'] is True
    code = mod.report(cur, d)
    assert code == 0            # baseline, never a regression


def test_regression_when_a_new_failure_appears():
    prev = {'failed_units': ['hart-vision.service'], 'client_errors': [], 'active_theme': 'aura'}
    cur = {'failed_units': ['hart-vision.service', 'hart-smart-index.service'],
           'client_errors': [], 'first_scanout': True, 'llm_gated': False,
           'active_theme': 'aura', 'reached_targets': 30}
    d = mod.diff(prev, cur)
    assert d['new_failures'] == ['hart-smart-index.service']
    assert d['resolved_failures'] == []
    assert mod.report(cur, d) == 1      # a new failure = regression


def test_live_fetch_over_http_token_gated():
    """The verify leg fetches over REAL HTTP from the peer's diag contract:
    correct token -> 200 bundle (parses); wrong token -> fail-closed (raises)."""
    import http.server
    import threading
    import urllib.error
    import urllib.request

    token = 'tkn-xyz'

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            import urllib.parse
            q = urllib.parse.urlparse(self.path)
            ok = q.path == '/diag' and urllib.parse.parse_qs(q.query).get('t', [None])[0] == token
            body = _SAMPLE.encode() if ok else b'forbidden'
            self.send_response(200 if ok else 403)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(('127.0.0.1', 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # correct token -> real bundle, parseable
        text = mod.fetch_diag('127.0.0.1', token, timeout=5, port=port)
        parsed = mod.parse_bundle(text)
        assert 'hart-vision.service' in parsed['failed_units']
        assert parsed['active_theme'] == 'aura'
        # wrong token -> fail-closed (HTTP 403 raises)
        raised = False
        try:
            mod.fetch_diag('127.0.0.1', 'WRONG', timeout=5, port=port)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            raised = True
        assert raised, 'wrong token must fail-closed, not return data'
    finally:
        srv.shutdown()


def test_resolved_failure_is_not_a_regression():
    prev = {'failed_units': ['hart-vision.service', 'clamav-daemon.service'],
            'client_errors': [], 'active_theme': 'aura'}
    cur = {'failed_units': ['hart-vision.service'], 'client_errors': [],
           'first_scanout': True, 'llm_gated': False, 'active_theme': 'aura',
           'reached_targets': 31}
    d = mod.diff(prev, cur)
    assert d['resolved_failures'] == ['clamav-daemon.service']
    assert d['new_failures'] == []
    assert mod.report(cur, d) == 0      # things improved, not a regression

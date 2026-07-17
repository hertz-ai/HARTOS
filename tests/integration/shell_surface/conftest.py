"""Deployed-surface integration harness (goal 2026-07-17: integration tests for
the DEPLOYED functionality, coverage 100%).

The system under test is the REAL app the OS serves: LiquidUIService.
_create_flask_app() -- the same factory the node boots -- driven through a real
test_client. Three pillars:

1. RECORDING: an after_request hook records every (method, url_rule) the suite
   exercises into HITS. test_zz_surface_complete.py diffs HITS against the full
   url_map and FAILS listing any deployed route the suite never drove -- the
   100%% gate is enforced behaviourally (a new route shipped without a test
   breaks CI), never by grepping source.

2. HERMETIC OS BOUNDARY (FakeOS): every shell/system/desktop/installer handler
   reaches the OS via the `subprocess` module (verified: all import the module,
   so patching subprocess.run/check_output/check_call/call/Popen intercepts
   every call site). FakeOS returns canned rc-0 output (pattern-overridable per
   test) and RECORDS each invocation, so tests assert the exact command the
   deployed handler issues -- and a poweroff/format/nmcli test can never touch
   the machine running the suite.

3. SCRATCH CWD: the suite chdirs into a scratch dir so any handler that writes
   a relative path lands in the sandbox, not the repo.
"""
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 1. the suite-wide hit registry (the 100% gate's evidence) ───────────────
HITS = set()          # {(method, rule)} exercised by ANY test in this suite


def surface_rules(app):
    """Every deployed (method, rule) pair, excluding Flask's static handler and
    the auto HEAD/OPTIONS methods nobody deploys handlers for."""
    out = set()
    for r in app.url_map.iter_rules():
        if r.endpoint == 'static':
            continue
        for m in (r.methods or set()) - {'HEAD', 'OPTIONS'}:
            out.add((m, r.rule))
    return out


# ── 2. FakeOS: the hermetic subprocess boundary ─────────────────────────────
class FakeOS:
    """Pattern-matching stand-in for the subprocess module's exec surface.

    calls       : every command issued, as a list of argv-lists/strings.
    stdout_for  : map a substring (matched against the joined argv) to canned
                  stdout, e.g. fake_os.stdout_for['nmcli'] = 'wlan0:connected'.
    rc_for      : same keying, to force a non-zero exit for failure paths.
    """

    def __init__(self):
        self.calls = []
        self.stdout_for = {}
        self.rc_for = {}

    def _argv_text(self, cmd):
        if isinstance(cmd, (list, tuple)):
            return ' '.join(str(c) for c in cmd)
        return str(cmd)

    def _match(self, table, text, default):
        for key, val in table.items():
            if key in text:
                return val
        return default

    def run(self, cmd, *a, **kw):
        text = self._argv_text(cmd)
        self.calls.append(cmd)
        out = self._match(self.stdout_for, text, '')
        rc = self._match(self.rc_for, text, 0)
        if kw.get('check') and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, output=out)
        binary = 'text' not in kw and not kw.get('universal_newlines') and kw.get('encoding') is None
        stdout = out.encode() if binary else out
        stderr = b'' if binary else ''
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

    def check_output(self, cmd, *a, **kw):
        cp = self.run(cmd, *a, **kw)
        if cp.returncode != 0:
            raise subprocess.CalledProcessError(cp.returncode, cmd, output=cp.stdout)
        return cp.stdout

    def check_call(self, cmd, *a, **kw):
        cp = self.run(cmd, *a, **kw)
        if cp.returncode != 0:
            raise subprocess.CalledProcessError(cp.returncode, cmd)
        return 0

    def call(self, cmd, *a, **kw):
        return self.run(cmd, *a, **kw).returncode

    def popen(self, cmd, *a, **kw):
        text = self._argv_text(cmd)
        self.calls.append(cmd)
        out = self._match(self.stdout_for, text, '')
        rc = self._match(self.rc_for, text, 0)
        proc = types.SimpleNamespace()
        binary = 'text' not in kw and not kw.get('universal_newlines') and kw.get('encoding') is None
        payload = out.encode() if binary else out
        import io
        proc.stdout = io.BytesIO(payload) if binary else io.StringIO(payload)
        proc.stderr = io.BytesIO(b'') if binary else io.StringIO('')
        proc.stdin = io.BytesIO() if binary else io.StringIO()
        proc.pid = 424242
        proc.returncode = rc
        proc.poll = lambda: rc
        proc.wait = lambda timeout=None: rc
        proc.communicate = lambda input=None, timeout=None: (payload, b'' if binary else '')
        proc.terminate = lambda: None
        proc.kill = lambda: None
        proc.__enter__ = lambda s=proc: s
        proc.__exit__ = lambda s, *exc: None
        return proc


@pytest.fixture()
def fake_os(monkeypatch):
    """Hermetic OS boundary: nothing a handler execs ever reaches the machine.
    Function-scoped so each test gets a clean call log + its own canned
    outputs, while the app fixture stays session-scoped (routes are static)."""
    fx = FakeOS()
    monkeypatch.setattr(subprocess, 'run', fx.run)
    monkeypatch.setattr(subprocess, 'check_output', fx.check_output)
    monkeypatch.setattr(subprocess, 'check_call', fx.check_call)
    monkeypatch.setattr(subprocess, 'call', fx.call)
    monkeypatch.setattr(subprocess, 'Popen', fx.popen)
    # os.system is BANNED in this codebase (Gate 7) -- if any deployed handler
    # still reaches it, fail the test loudly instead of executing.
    monkeypatch.setattr(os, 'system',
                        lambda c: (_ for _ in ()).throw(AssertionError('os.system called (banned): %r' % c)))
    # Handlers poll/retry with time.sleep after issuing (now-faked) commands --
    # against an instant boundary the waits are pure dead time (the first
    # drive-all run crawled at ~30 s/route). No-op them; nothing in a request
    # path may DEPEND on wall-clock passing.
    import time as _time
    monkeypatch.setattr(_time, 'sleep', lambda s: None)
    return fx


@pytest.fixture(scope='session', autouse=True)
def no_network():
    """The peer-service HTTP boundary (model bus, Hevolve DB, hive endpoints)
    is ABSENT in CI just as it is here -- but a real socket attempt burns its
    full connect timeout per call. Refuse instantly instead: handlers exercise
    the exact same degrade path (ConnectionError -> controlled 503), just
    deterministically and in microseconds."""
    mp = pytest.MonkeyPatch()

    def _refuse(*a, **kw):
        import requests as _rq
        raise _rq.exceptions.ConnectionError('shell-surface suite: network boundary is faked (no peer services)')

    try:
        import requests
        mp.setattr(requests, 'request', _refuse)
        mp.setattr(requests, 'get', _refuse)
        mp.setattr(requests, 'post', _refuse)
        mp.setattr(requests, 'put', _refuse)
        mp.setattr(requests, 'delete', _refuse)
        mp.setattr(requests.Session, 'request', _refuse)
    except ImportError:
        pass
    try:
        import httpx

        def _refuse_httpx(*a, **kw):
            raise httpx.ConnectError('shell-surface suite: network boundary is faked')
        mp.setattr(httpx, 'request', _refuse_httpx, raising=False)
        mp.setattr(httpx, 'get', _refuse_httpx, raising=False)
        mp.setattr(httpx, 'post', _refuse_httpx, raising=False)
        mp.setattr(httpx.Client, 'request', _refuse_httpx, raising=False)
    except ImportError:
        pass
    yield
    mp.undo()


# ── 3. the real deployed app + recording client ────────────────────────────
@pytest.fixture(scope='session')
def surface_app(tmp_path_factory):
    """The REAL Flask app the node serves (same factory), with the recording
    hook installed and cwd sandboxed for the whole suite."""
    os.chdir(tmp_path_factory.mktemp('shell-surface-cwd'))
    from integrations.agent_engine.liquid_ui_service import LiquidUIService
    svc = LiquidUIService()
    app = svc._create_flask_app()
    app.testing = True

    @app.after_request
    def _record(resp):                                    # noqa: ANN001
        from flask import request
        if request.url_rule is not None:
            HITS.add((request.method, request.url_rule.rule))
        return resp

    return app


@pytest.fixture()
def client(surface_app, fake_os):
    """A recording test_client over the deployed app with the OS boundary
    already faked -- the default way every surface test talks to the system."""
    return surface_app.test_client()

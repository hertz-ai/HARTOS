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

   `subprocess` is NOT the only way a handler reaches the host, so faking it is
   not enough to make a test reproducible. A handler also ASKS ABOUT the host --
   over D-Bus, from sysfs, from the session environment -- and each of those
   answers differently per box, so the SAME test drives a DIFFERENT branch on a
   Windows dev box than on a Linux CI runner. That is how the login1 power
   cluster came to pass locally and fail in CI for months. The `fake_os` fixture
   therefore declares the fake machine's IDENTITY too (no D-Bus system bus, no
   inherited desktop session, a declared firmware capability); see its body.

3. SCRATCH CWD: the suite chdirs into a scratch dir so any handler that writes
   a relative path lands in the sandbox, not the repo.
"""
import io
import os
import subprocess
import sys

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
class FakeProc:
    """A `subprocess.Popen` stand-in.

    The dunders MUST live on the CLASS. Python resolves `__enter__`/`__exit__`
    on the TYPE, never on the instance, so an object that merely has them as
    attributes is not a context manager -- `with subprocess.Popen(...)` raises
    TypeError. That matters here because the callers are not all ours: the
    stdlib's own `ctypes.util.find_library` opens Popen with `with`, and `mss`
    (the screenshot handler's Python fallback) calls it on Linux.
    """

    def __init__(self, cmd, payload, rc, binary):
        self.args = cmd
        self.stdout = io.BytesIO(payload) if binary else io.StringIO(payload)
        self.stderr = io.BytesIO(b'') if binary else io.StringIO('')
        self.stdin = io.BytesIO() if binary else io.StringIO()
        self.pid = 424242
        self.returncode = rc
        self._payload = payload
        self._binary = binary

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return self._payload, b'' if self._binary else ''

    def terminate(self):
        pass

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Real Popen.__exit__ closes the pipes before waiting; mirror it so a
        # caller's `with` block never leaves a half-open stream behind.
        for stream in (self.stdout, self.stderr, self.stdin):
            try:
                stream.close()
            except Exception:
                pass
        return False


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
        # Declared machine identity (see the `fake_os` fixture). Legacy BIOS by
        # default; a test sets this True to drive the UEFI-capable branch.
        self.uefi_firmware_setup = False

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
        binary = 'text' not in kw and not kw.get('universal_newlines') and kw.get('encoding') is None
        payload = out.encode() if binary else out
        return FakeProc(cmd, payload, rc, binary)


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

    # ── the fake machine's IDENTITY ─────────────────────────────────────────
    # Faking what a handler EXECS is only half the boundary. A handler also
    # ASKS ABOUT the host, and those questions bypass `subprocess` entirely --
    # so without the three seals below the same test drives a different branch
    # on every box, and the suite silently tests the developer's laptop.

    # (a) NO D-BUS SYSTEM BUS. os_bridge.logind tries a NATIVE jeepney call
    #     BEFORE the busctl subprocess, and native D-Bus never goes near
    #     `subprocess`. On any box with a reachable system bus the hermetic
    #     promise above is therefore void: a power test issues a REAL
    #     org.freedesktop.login1.PowerOff. In CI polkit denies it and the test
    #     merely goes red; on a Linux desktop with an active local session
    #     polkit's shipped default for power-off is "yes" -- the suite would
    #     power the developer's machine off. Refuse the connection instead:
    #     that is precisely the documented "no system bus" branch
    #     (logind.py:119-122), and it puts the observable busctl argv back at
    #     the boundary FakeOS records. The NATIVE transport keeps its own
    #     coverage in tests/unit/test_os_bridge_power.py.
    from integrations.agent_engine.os_bridge import logind as _logind

    def _no_system_bus(*a, **kw):
        raise ConnectionError('shell-surface suite: no D-Bus system bus (hermetic)')

    monkeypatch.setattr(_logind, 'open_dbus_connection', _no_system_bus,
                        raising=False)

    # (b) NO DESKTOP SESSION INHERITED. _is_wayland() reads WAYLAND_DISPLAY and
    #     XDG_SESSION_TYPE, so a developer running the suite inside a Wayland
    #     session takes a different wallpaper/display branch than one on X11.
    #     Each test declares the session it means -- monkeypatch.setenv for
    #     Wayland, fake_os.rc_for['pgrep'] for the compositor-process probe.
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.delenv('XDG_SESSION_TYPE', raising=False)

    # (c) FIRMWARE CAPABILITY IS DECLARED, NOT READ OFF THE HOST.
    #     firmware_setup_supported() is a pure sysfs read (/sys/firmware/efi
    #     then an efivar), so it answers True on a UEFI Linux runner and False
    #     on the Windows dev box -- the branch flips under the test's feet.
    #     Pin it to the fake machine's declared identity (legacy BIOS unless a
    #     test says otherwise). This is the SAME seam the unit suites already
    #     patch (test_os_bridge_power.py, test_shell_firmware_setup.py), and
    #     the probe's own internals -- isdir, the efivar bit, the short-read
    #     guard -- stay covered in tests/unit/test_shell_os_apis.py, so pinning
    #     it here costs no coverage and makes BOTH branches reachable.
    from integrations.agent_engine import shell_os_apis as _shell_os_apis
    monkeypatch.setattr(_shell_os_apis, 'firmware_setup_supported',
                        lambda: fx.uefi_firmware_setup)
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
    hook installed and the suite's filesystem sandboxed."""
    sandbox = tmp_path_factory.mktemp('shell-surface-cwd')
    os.chdir(sandbox)
    # A chdir only sandboxes RELATIVE writes. Handlers that persist state
    # resolve an ABSOLUTE path from HEVOLVE_DATA_DIR, defaulting to
    # /var/lib/hart -- the node's root-owned StateDirectory. Unset, the suite
    # tries to write the REAL system state dir: permission-denied on a Linux
    # runner (a deployed handler correctly reporting it cannot persist, which
    # then reads as a red test), and an actual C:\var\lib\hart on the Windows
    # dev box. Point it into the sandbox; 22 call sites already honour it.
    mp = pytest.MonkeyPatch()
    state_dir = sandbox / 'state'
    state_dir.mkdir(exist_ok=True)
    mp.setenv('HEVOLVE_DATA_DIR', str(state_dir))
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

    yield app
    mp.undo()


@pytest.fixture()
def client(surface_app, fake_os):
    """A recording test_client over the deployed app with the OS boundary
    already faked -- the default way every surface test talks to the system."""
    return surface_app.test_client()

"""Behavioral tests for the native desktop notification daemon (hart-notify.nix).

The capability audit (2026-06-24) found the glass shell only had in-shell SSE toasts and
NO native org.freedesktop.Notifications daemon -- so foreign apps (Wine/Android),
AI-composed .hartapp surfaces, and the robot had no way to raise a desktop notification.
hart-notify.nix ships mako (the wlroots-native notification server) as a never-fail
graphical-session user service, glass-styled, with two clients: the ungated foreign-app
`notify-send` (libnotify) and the AI's privacy-gated `hart-notify-send`.

WHY THIS FILE IS BEHAVIORAL, NOT A GREP CHECK
---------------------------------------------
The previous version of this file asserted substrings like ``"default = true" in module``.
That broke the moment the module evolved its default to the smarter, variant-conditional
``default = lib.elem cfg.variant [ "desktop" "phone" ]`` -- the BEHAVIOR (default ON only
where there is a screen) was still correct, but the grep was a false failure. So here we:

  * exercise the REAL privacy gate the emitter wires its exit code to
    (`core.ai_sensing.query_authority` + the human kill-switch state machine), mocking
    only the socket boundary -- happy / regression (screen cut) / offline / missing-tool;
  * EXECUTE the actual Python gate the module bakes into ``hart-notify-send`` (extracted
    from the .nix source) as a subprocess against a controllable stub `core.ai_sensing`,
    asserting the real exit-code contract (0 == allow/paint, 77 == suppress/fail-closed);
  * EVALUATE the enable option's variant-conditional default expression (resolved truth
    per variant) instead of grepping for a literal -- this is the regression that the old
    stale assertion was reaching for.

The in-VM structural proof (the mako user service is wired, the glass config renders, the
DnD + privacy mako modes load, a live screen-cut suppresses a native toast) lives in the
companion nixosTest ``nixos/tests/notify.nix`` -- it boots a desktop VM and cannot run on
the Windows dev box. This file is the OS-agnostic, runnable-anywhere behavioral half.
"""
import os
import pathlib
import re
import subprocess
import sys
from unittest.mock import patch

import pytest

# ─── Paths / repo root (conftest also puts the root on sys.path for `import core`) ───
_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MODULE_PATH = _ROOT / "nixos" / "modules" / "hart-notify.nix"
_NOTIFY_SRC = _MODULE_PATH.read_text(encoding="utf-8")

# ─── Extract the SHIPPED emitter gate (the exact Python baked into hart-notify-send) ───
# The module embeds it as a flush-left single-quoted heredoc `... python - <<'PY' ... PY`.
# We pull the body verbatim and run it; if the module's gate shape changes structurally
# this extraction (and thus the contract tests) fails loudly -- which is the point.
_GATE_MATCH = re.search(r"<<'PY'\n(.*?)\nPY\b", _NOTIFY_SRC, re.DOTALL)
_GATE_SCRIPT = _GATE_MATCH.group(1) if _GATE_MATCH else None


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _restore_senses():
    """Snapshot + restore the global ai-sensing kill-switch state around each test
    so a screen-cut in one test never leaks into another (or the wider session)."""
    from core import ai_sensing
    with ai_sensing._lock:
        snapshot = dict(ai_sensing._state)
    try:
        yield
    finally:
        with ai_sensing._lock:
            ai_sensing._state.clear()
            ai_sensing._state.update(snapshot)


def _write_stub_core(directory: pathlib.Path):
    """Create a stub `core.ai_sensing` package whose query_authority is driven by env:
      HART_TEST_VERDICT  = allow|deny|raise   (default deny == fail-closed)
      HART_TEST_SENSOR_FILE = path the sensor arg is recorded to (proves which sense).
    """
    core_dir = directory / "core"
    core_dir.mkdir(parents=True, exist_ok=True)
    (core_dir / "__init__.py").write_text("", encoding="utf-8")
    (core_dir / "ai_sensing.py").write_text(
        "import os\n"
        "def query_authority(sensor, *a, **k):\n"
        "    f = os.environ.get('HART_TEST_SENSOR_FILE')\n"
        "    if f:\n"
        "        with open(f, 'w', encoding='utf-8') as fh:\n"
        "            fh.write(str(sensor))\n"
        "    v = os.environ.get('HART_TEST_VERDICT', 'deny')\n"
        "    if v == 'raise':\n"
        "        raise RuntimeError('authority boom')\n"
        "    return v == 'allow'\n",
        encoding="utf-8",
    )


def _run_gate(stub_dir: pathlib.Path, verdict: str, sensor_file: pathlib.Path):
    """Run the SHIPPED emitter gate Python (from the .nix) with a controllable
    `core.ai_sensing` on PYTHONPATH. Returns the subprocess CompletedProcess."""
    env = dict(os.environ)
    # Isolate import resolution to the stub dir only (so the real repo `core` cannot
    # leak in and silently make the import-error case pass for the wrong reason).
    env["PYTHONPATH"] = str(stub_dir)
    env["HART_TEST_VERDICT"] = verdict
    env["HART_TEST_SENSOR_FILE"] = str(sensor_file)
    return subprocess.run(
        [sys.executable, "-"],
        input=_GATE_SCRIPT,
        text=True,
        capture_output=True,
        cwd=str(stub_dir),
        env=env,
        timeout=30,
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. The human kill-switch state machine the whole gate rests on (in-process, real code)
# ════════════════════════════════════════════════════════════════════════════

class TestScreenKillSwitchState:
    """`core.ai_sensing` is the ONE source of truth the native emitter honours. These
    prove the human's cut genuinely flips the 'screen' sense (the privacy contract the
    module header is built on) -- no sockets, OS-agnostic."""

    def test_screen_allowed_by_default_happy(self):
        from core import ai_sensing
        ai_sensing.enable_all()
        assert ai_sensing.allowed("screen") is True

    def test_human_cut_screen_denies_regression(self):
        # REGRESSION / privacy core: the human cuts 'screen' -> the sense is denied, so
        # the AI's native toast must be withheld. A revert that ignored the cut re-opens
        # the exact hole this daemon was added to respect.
        from core import ai_sensing
        ai_sensing.set_sense("screen", True)
        assert ai_sensing.allowed("screen") is False
        # ...and only the cut sense is affected (mic stays whatever it was, not collateral).
        assert ai_sensing.is_disabled("screen") is True

    def test_disable_all_then_enable_all_round_trips(self):
        from core import ai_sensing
        ai_sensing.disable_all()
        assert ai_sensing.allowed("screen") is False
        ai_sensing.enable_all()
        assert ai_sensing.allowed("screen") is True


# ════════════════════════════════════════════════════════════════════════════
# 2. The cross-process gate the emitter consults (real query_authority, socket mocked)
# ════════════════════════════════════════════════════════════════════════════

class _FakeConn:
    """A minimal stand-in for a connected AF_UNIX socket."""

    def __init__(self, reply=b"1", connect_exc=None):
        self._reply = reply
        self._connect_exc = connect_exc
        self.sent = []
        self.closed = False

    def settimeout(self, t):
        pass

    def connect(self, addr):
        if self._connect_exc:
            raise self._connect_exc

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        return self._reply

    def close(self):
        self.closed = True


class _FakeSocketMod:
    """A fake `socket` module exposing exactly what query_authority touches."""

    AF_UNIX = 1
    SOCK_STREAM = 1

    def __init__(self, conn):
        self._conn = conn
        self.calls = 0

    def socket(self, *a, **k):
        self.calls += 1
        return self._conn


class _NoAFUnixSocketMod:
    """A fake `socket` module WITHOUT AF_UNIX (the missing-platform-tool boundary)."""

    SOCK_STREAM = 1

    def __init__(self):
        self.calls = 0

    def socket(self, *a, **k):  # must never be reached
        self.calls += 1
        raise AssertionError("socket() must not be constructed when AF_UNIX is absent")


class TestCrossProcessGate:
    """`query_authority('screen')` is the exact call the shipped emitter wires its exit
    code to. We mock only the socket boundary and assert the fail-closed semantics."""

    def test_authority_allows_screen_happy(self):
        from core import ai_sensing
        conn = _FakeConn(reply=b"1")
        with patch.object(ai_sensing, "socket", _FakeSocketMod(conn)):
            assert ai_sensing.query_authority("screen") is True
        # It actually asked the authority about the SCREEN sense (privacy contract).
        assert conn.sent == [b"screen"]

    def test_authority_denies_when_screen_cut_regression(self):
        from core import ai_sensing
        conn = _FakeConn(reply=b"0")  # authority server says 'screen' is cut
        with patch.object(ai_sensing, "socket", _FakeSocketMod(conn)):
            assert ai_sensing.query_authority("screen") is False

    def test_authority_offline_fails_closed_boundary(self):
        # Boundary (offline/error): the authority is unreachable -> the gate must DENY,
        # never paint on doubt. A fail-OPEN here would surface AI toasts on a cut screen.
        from core import ai_sensing
        conn = _FakeConn(connect_exc=ConnectionRefusedError("no authority"))
        with patch.object(ai_sensing, "socket", _FakeSocketMod(conn)):
            assert ai_sensing.query_authority("screen") is False

    def test_missing_af_unix_fails_closed_boundary(self):
        # Boundary (missing platform tool): no AF_UNIX -> deny without even constructing
        # a socket (matches the module's never-hang/never-crash promise).
        from core import ai_sensing
        fake = _NoAFUnixSocketMod()
        with patch.object(ai_sensing, "socket", fake):
            assert ai_sensing.query_authority("screen") is False
        assert fake.calls == 0

    def test_malformed_reply_fails_closed_boundary(self):
        # Boundary (garbage on the wire): anything that is not exactly b'1' is a DENY.
        from core import ai_sensing
        for junk in (b"", b"2", b"yes", b"01"):
            conn = _FakeConn(reply=junk)
            with patch.object(ai_sensing, "socket", _FakeSocketMod(conn)):
                assert ai_sensing.query_authority("screen") is False, junk


# ════════════════════════════════════════════════════════════════════════════
# 3. The SHIPPED emitter exit-code contract (run the actual Python from the .nix)
# ════════════════════════════════════════════════════════════════════════════

class TestEmitterExitContract:
    """Execute the exact gate Python the module bakes into `hart-notify-send` and assert
    its exit-code contract: 0 == gate allowed (the emitter then exec's notify-send),
    77 == suppressed/fail-closed. This proves the SHIPPED artifact, not a paraphrase."""

    def test_gate_extracted_from_module(self):
        assert _GATE_SCRIPT is not None, (
            "could not extract the `<<'PY' ... PY` emitter gate from hart-notify.nix -- "
            "the module's gate shape changed; update the extraction AND re-verify the contract.")
        # Sanity: it is the screen-sense gate wired to the 0/77 convention.
        assert "query_authority('screen')" in _GATE_SCRIPT
        assert "sys.exit(0 if ok else 77)" in _GATE_SCRIPT

    def test_allow_exits_zero_and_consults_screen_happy(self, tmp_path):
        stub = tmp_path / "allow"
        _write_stub_core(stub)
        sensor_file = tmp_path / "sensor_allow.txt"
        res = _run_gate(stub, "allow", sensor_file)
        assert res.returncode == 0, f"gate must exit 0 when authority allows, got {res.returncode}: {res.stderr}"
        # Proves the emitter consulted the SCREEN kill-switch (not some other sense / no-arg).
        assert sensor_file.read_text(encoding="utf-8") == "screen"

    def test_deny_exits_77_regression(self, tmp_path):
        # REGRESSION: human cut 'screen' (authority denies) -> the native toast is
        # SUPPRESSED via exit 77. This is the privacy core of the whole module.
        stub = tmp_path / "deny"
        _write_stub_core(stub)
        res = _run_gate(stub, "deny", tmp_path / "sensor_deny.txt")
        assert res.returncode == 77, f"gate must exit 77 when authority denies, got {res.returncode}: {res.stderr}"

    def test_authority_raises_fails_closed_77_boundary(self, tmp_path):
        # Boundary (error): query_authority blows up -> the try/except forces ok=False ->
        # exit 77. Never paint on an exception.
        stub = tmp_path / "raise"
        _write_stub_core(stub)
        res = _run_gate(stub, "raise", tmp_path / "sensor_raise.txt")
        assert res.returncode == 77, f"gate must fail-closed (77) when the gate errors, got {res.returncode}: {res.stderr}"

    def test_missing_core_module_fails_closed_77_boundary(self, tmp_path):
        # Boundary (missing tool/dep): `core.ai_sensing` is not importable at all ->
        # ImportError is caught -> exit 77. A node without the brain's python on PATH
        # must SUPPRESS, never crash and never paint.
        empty = tmp_path / "empty"   # deliberately NO core package inside
        empty.mkdir()
        res = _run_gate(empty, "allow", tmp_path / "sensor_missing.txt")
        assert res.returncode == 77, (
            f"gate must fail-closed (77) when core.ai_sensing is missing, "
            f"got {res.returncode}: {res.stderr}")


# ════════════════════════════════════════════════════════════════════════════
# 4. The enable option's variant-conditional default (resolved truth, not a grep)
# ════════════════════════════════════════════════════════════════════════════

class TestEnableDefaultSemantics:
    """Replaces the stale ``"default = true"`` grep. The module defaults the daemon ON
    only where there is a screen to paint on (desktop / phone) and OFF on the headless
    variants (server / edge). We extract the `default = lib.elem cfg.variant [ ... ]`
    expression and EVALUATE its resolved boolean per variant -- the actual semantics."""

    @staticmethod
    def _enable_default_variants():
        # Scope to the `enable = lib.mkOption { ... }` block, then pull the variant list
        # from its `default = lib.elem cfg.variant [ "..." "..." ];` (NOT defaultText).
        block = re.search(
            r"enable\s*=\s*lib\.mkOption\s*\{(.*?)\n\s*\};",
            _NOTIFY_SRC, re.DOTALL)
        assert block, "could not locate the enable = lib.mkOption { ... } block"
        m = re.search(
            r"(?<![A-Za-z])default\s*=\s*lib\.elem\s+\S+\s+\[\s*([^\]]*?)\]",
            block.group(1))
        assert m, "enable.default is not the expected `lib.elem cfg.variant [ ... ]` form"
        return set(re.findall(r'"([^"]+)"', m.group(1)))

    def test_default_on_for_screen_variants_happy(self):
        members = self._enable_default_variants()
        # `lib.elem variant members` resolves True exactly when variant in members.
        def resolves_on(variant):
            return variant in members
        assert resolves_on("desktop") is True, "native notifications must default ON for desktop"
        assert resolves_on("phone") is True, "native notifications must default ON for phone"

    def test_default_off_for_headless_variants_boundary(self):
        members = self._enable_default_variants()
        def resolves_on(variant):
            return variant in members
        assert resolves_on("server") is False, "headless server must not carry mako by default"
        assert resolves_on("edge") is False, "headless edge must not carry mako by default"

    def test_default_set_is_exactly_the_screened_variants(self):
        # Guard against accidental widening (e.g. someone adding "server") -- the default
        # set is precisely the two screened variants.
        assert self._enable_default_variants() == {"desktop", "phone"}

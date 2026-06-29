"""
Portal-available + microphone fix for the GTK4 layer-shell glass host (2026-06-29).

WHAT THIS GUARDS
  The real-HW regression: after Tier-1 login the glass host showed "Microphone
  access denied and then HANGS", VT-switch looked dead, because the first-paint fix
  ran the GTK4/WebKit host under a PRIVATE D-Bus session whose config made
  org.freedesktop.portal.Desktop NON-ACTIVATABLE. That fast-failed GtkSettings.Read
  (good: no 25s freeze) but ALSO starved the SAME portal WebKit getUserMedia needs
  (bad: mic denied + main-loop wedge). The fix in nixos/modules/hart-layer-shell-
  host.nix replaces "portal absent" with "portal AVAILABLE + RESPONSIVE":
    * start xdg-desktop-portal + the gtk backend, push the Wayland/desktop env into
      the D-Bus activation environment, then WAIT (bounded) for the portal to OWN its
      name before exec'ing python on the REAL session bus (Settings.Read is now ms);
    * DEGRADE-NOT-DIE: if the portal is not owned in time, fall back to the SAME
      portal-less private bus (noPortalBusConfig) so first-paint stays fast and the
      25s GtkSettings freeze can never recur;
    * connect the WebView 'permission-request' signal + enable media-stream so
      first-party getUserMedia is ALLOWED (the 'denied' half), gated best-effort on
      the human's AI-sensing kill-switch.

WHY BEHAVIOURAL-WHERE-POSSIBLE + SOURCE-GUARD ELSEWHERE
  The Nix wrapper cannot be booted on Windows (no Wayland/WebKitGTK/portal). But two
  pieces of the fix are PLAIN PYTHON embedded in the module — the kill-switch reader
  `_sense_cut` and the `_on_permission_request` allow/deny router — and those ARE
  exercised here for real: we extract the ACTUAL embedded source the kiosk runs and
  call it with the socket / WebKit boundaries mocked, asserting observable side-
  effects (request.allow()/deny() calls; fail-open verdicts). The bus/portal wiring
  (shell text that only runs under a compositor) is covered by clearly-labelled
  source-guards, the same acceptable class the Phase-4 suite uses.

Run (dev box, targeted):
    python -m pytest tests/unit/test_layer_shell_host_portal_mic.py -v \
        --noconftest -p no:cacheprovider
"""
import ast
import importlib.util
import os
import socket
import textwrap
import threading

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "nixos", "modules", "hart-layer-shell-host.nix")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# Reuse the canonical embedded-python extractor from the Phase-4 suite (DRY — do not
# re-implement the Nix antiquote-aware scanner). Loaded by path so it works under
# --noconftest without package-import gymnastics.
def _load_extractor():
    spec = importlib.util.spec_from_file_location(
        "_p4_layer_host", os.path.join(_HERE, "test_phase4_layer_shell_host.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._extract_python_c_body


_extract_python_c_body = _load_extractor()


def _host_python():
    """The ACTUAL python source the GTK4 host execs (antiquotes neutralized)."""
    return _extract_python_c_body(_read(MODULE))


def _func_source(py_src, func_name, classname=None):
    """Return dedented source of a top-level (or method, when classname given)
    function from py_src, so it can be exec'd in isolation."""
    tree = ast.parse(py_src)
    container = tree.body
    if classname is not None:
        cls = next((n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == classname), None)
        assert cls is not None, f"class {classname} not found in embedded host"
        container = cls.body
    fn = next((n for n in container
               if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    assert fn is not None, f"function {func_name} not found in embedded host"
    seg = ast.get_source_segment(py_src, fn)
    assert seg, f"could not slice source for {func_name}"
    return textwrap.dedent(seg)


# ═══════════════════════════════════════════════════════════════
# 1. BEHAVIOURAL — `_sense_cut`: fail-OPEN cross-process kill-switch read
#    (the load-bearing safety property: a missing authority must NOT deny
#    the first-party shell, or the 'mic denied' bug returns)
# ═══════════════════════════════════════════════════════════════

class TestSenseCutBehaviour:
    @pytest.fixture(scope="class")
    def sense_cut(self):
        ns = {"os": os}
        exec(_func_source(_host_python(), "_sense_cut"), ns)
        return ns["_sense_cut"]

    def test_unreachable_authority_fails_open(self, sense_cut, tmp_path, monkeypatch):
        # No authority listening at the path -> connect() raises -> returns False
        # (NOT cut). This is the fix's core invariant: a down/absent kill-switch must
        # never wrongly deny mic, else "Microphone access denied" recurs.
        monkeypatch.setenv("HART_AI_SENSING_SOCK", str(tmp_path / "nope.sock"))
        assert sense_cut("mic") is False

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                        reason="AF_UNIX unavailable (Windows dev box) — runs in Linux CI")
    def test_reachable_authority_reports_cut(self, sense_cut, tmp_path, monkeypatch):
        # A reachable authority that replies b'0' (human cut the sense) -> True (cut).
        sock_path = str(tmp_path / "auth.sock")
        self._serve(sock_path, b"0")
        monkeypatch.setenv("HART_AI_SENSING_SOCK", sock_path)
        assert sense_cut("mic") is True

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                        reason="AF_UNIX unavailable (Windows dev box) — runs in Linux CI")
    def test_reachable_authority_reports_allowed(self, sense_cut, tmp_path, monkeypatch):
        # A reachable authority that replies b'1' (sense on) -> False (NOT cut).
        sock_path = str(tmp_path / "auth.sock")
        self._serve(sock_path, b"1")
        monkeypatch.setenv("HART_AI_SENSING_SOCK", sock_path)
        assert sense_cut("camera") is False

    @staticmethod
    def _serve(sock_path, reply):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)

        def _accept():
            try:
                conn, _ = srv.accept()
                conn.recv(64)
                conn.sendall(reply)
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        threading.Thread(target=_accept, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# 2. BEHAVIOURAL — `_on_permission_request`: allow first-party getUserMedia,
#    deny ONLY on a definitive kill-switch cut, never raise (degrade-not-die)
# ═══════════════════════════════════════════════════════════════

class _FakeUserMediaRequest:
    def __init__(self, audio=False, video=False, raise_on_audio=False):
        self._audio = audio
        self._video = video
        self._raise_on_audio = raise_on_audio
        self.allowed = False
        self.denied = False

    def is_for_audio_device(self):
        if self._raise_on_audio:
            raise RuntimeError("boom")
        return self._audio

    def is_for_video_device(self):
        return self._video

    def allow(self):
        self.allowed = True

    def deny(self):
        self.denied = True


class _OtherRequest:
    """A non-UserMedia permission request (e.g. notification)."""


class TestPermissionRequestBehaviour:
    def _make_handler(self, sense_cut_stub):
        # Build the WebKit boundary: a namespace whose UserMediaPermissionRequest is
        # our fake class, plus the module-level _sense_cut the method calls.
        webkit = type("WebKit", (), {"UserMediaPermissionRequest": _FakeUserMediaRequest})
        ns = {"WebKit": webkit, "_sense_cut": sense_cut_stub}
        exec(_func_source(_host_python(), "_on_permission_request",
                          classname="GlassShellLayer"), ns)
        handler = ns["_on_permission_request"]
        dummy_self = object()
        return lambda request: handler(dummy_self, None, request)

    def test_audio_allowed_when_sense_not_cut(self):
        handler = self._make_handler(lambda s: False)   # nothing cut
        req = _FakeUserMediaRequest(audio=True)
        assert handler(req) is True
        assert req.allowed and not req.denied

    def test_audio_denied_when_mic_cut(self):
        handler = self._make_handler(lambda s: s == "mic")
        req = _FakeUserMediaRequest(audio=True)
        assert handler(req) is True
        assert req.denied and not req.allowed

    def test_video_denied_when_camera_cut(self):
        handler = self._make_handler(lambda s: s == "camera")
        req = _FakeUserMediaRequest(video=True)
        assert handler(req) is True
        assert req.denied and not req.allowed

    def test_video_allowed_when_only_mic_cut(self):
        # Camera request must NOT be denied just because the mic is cut.
        handler = self._make_handler(lambda s: s == "mic")
        req = _FakeUserMediaRequest(video=True)
        assert handler(req) is True
        assert req.allowed and not req.denied

    def test_non_usermedia_request_not_handled(self):
        # Other permission types fall through to WebKit default (return False); we do
        # not blanket-allow notifications/geolocation/etc.
        handler = self._make_handler(lambda s: False)
        req = _OtherRequest()
        assert handler(req) is False

    def test_error_in_device_probe_degrades_to_allow(self):
        # is_for_audio_device() raising must NOT crash/wedge the shell: the inner
        # guard swallows it, cut stays False, the first-party request is ALLOWED.
        handler = self._make_handler(lambda s: False)
        req = _FakeUserMediaRequest(audio=True, raise_on_audio=True)
        assert handler(req) is True
        assert req.allowed and not req.denied


# ═══════════════════════════════════════════════════════════════
# 3. SOURCE-GUARDS — the bus/portal wiring (shell text, compositor-only)
# ═══════════════════════════════════════════════════════════════

class TestPortalWiringSourceGuards:
    @pytest.fixture(scope="class")
    def src(self):
        return _read(MODULE)

    def test_pushes_wayland_env_into_dbus_activation(self, src):
        # An ACTIVATED portal backend must inherit WAYLAND_DISPLAY + desktop identity
        # to reach the compositor (Camera/ScreenCast) — pushed best-effort, non-fatal.
        assert "dbus-update-activation-environment --systemd" in src
        assert "XDG_CURRENT_DESKTOP" in src and "XDG_SESSION_TYPE" in src

    def test_starts_portal_frontend_and_gtk_backend(self, src):
        # The host itself brings the portal up (single cross-tier starter).
        assert "libexec/xdg-desktop-portal" in src
        assert "libexec/xdg-desktop-portal-gtk" in src

    def test_waits_for_portal_name_ownership_before_launch(self, src):
        # The bounded wait is what makes the portal AVAILABLE + RESPONSIVE so
        # GtkSettings.Read is a ms call, not a 25s activation (first-paint stays fast).
        assert "org.freedesktop.DBus.NameHasOwner" in src
        assert "org.freedesktop.portal.Desktop" in src
        assert "boolean true" in src

    def test_degrades_to_private_bus_on_timeout(self, src):
        # DEGRADE-NOT-DIE: the portal-less private bus is kept as the FALLBACK so the
        # 25s freeze can never recur — not deleted, demoted.
        assert "noPortalBusConfig" in src
        assert "dbus-run-session" in src
        assert "LAUNCH_PREFIX" in src
        low = src.lower()
        assert "fallback" in low

    def test_connects_permission_request_and_enables_media_stream(self, src):
        # The 'denied' half: without the signal connect WebKit default-denies, and
        # without enable-media-stream getUserMedia never even fires.
        assert "webview.connect('permission-request', self._on_permission_request)" in src
        assert "set_enable_media_stream" in src

    def test_gates_mic_on_ai_sensing_killswitch(self, src):
        # The human's kill-switch still governs capture (defence-in-depth).
        assert "_sense_cut" in src
        assert "'mic'" in src and "'camera'" in src

    def test_no_parallel_portal_starter_in_sway_config(self, src):
        # DRY: the sway host config must NOT also exec xdg-desktop-portal (that would
        # race the wrapper's single starter). The wrapper owns portal startup.
        assert "exec XDG_DATA_DIRS=" not in src or "xdg-desktop-portal -r" not in src

    def test_first_paint_marker_path_unchanged(self, src):
        # The hard constraint: first-paint stays fast AND the marker path is identical
        # to before (LoadEvent.FINISHED -> _signal_painted -> shell-ready), so the
        # supervisor's HUNG-tier guard behaves exactly as proven.
        assert "/run/hart/session/shell-ready" in src
        assert "WebKit.LoadEvent.FINISHED" in src
        assert "def _on_load_changed" in src
        assert "_signal_painted()" in src

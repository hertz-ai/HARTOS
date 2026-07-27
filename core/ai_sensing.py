"""AI sensory kill-switch — the human's hard cut over what the AI can sense.

A single authoritative gate the human flips from the shell (the floating eye
button). Every real-world sensor ingestion the shell controls checks this gate:
  * mic   — /api/voice refuses to transcribe when hearing is cut,
  * camera— the vision service is stopped (real, observable),
  * screen— gated for any consumer that honours it.
`status()` reports the LIVE state (e.g. the vision service really is stopped),
so the proof shown to the user cannot be faked by the AI. The AI has NO route to
flip this; only the human UI (POST /api/shell/ai-sensing) does.
This is the desktop expression of HART's "humans are always in control".

Single source of truth: import `allowed(sensor)` at every sensor ingestion point
rather than re-implementing a per-feature mute.

Cross-process authority (Phase 7): in-process `allowed()` is per-process memory.
A separate process (e.g. xdg-desktop-portal-hart's ScreenCast handler, which is
its OWN systemd unit) cannot read it — so a screencast surface could capture the
screen the human cut. `start_authority_server()` exposes the gate over a Unix
socket in the canonical state holder (the brain); `query_authority(sensor)` is
the FAIL-CLOSED client the portal MUST consult before any capture.
"""
import os
import socket
import threading

_lock = threading.RLock()
# True = that sense is DISABLED (the AI is blind/deaf to it). Default: sensing on.
_state = {'mic': False, 'camera': False, 'screen': False}

_SENSES = ('mic', 'camera', 'screen')


def is_disabled(sensor: str) -> bool:
    with _lock:
        return bool(_state.get(sensor, False))


def allowed(sensor: str) -> bool:
    """Gate for sensor ingestion. False => the human has cut this sense."""
    return not is_disabled(sensor)


def any_disabled() -> bool:
    with _lock:
        return any(_state.values())


def _stop_vision() -> None:
    """Best-effort hard-cut of the camera/vision service (observable in status)."""
    try:
        from integrations.vision.vision_service import get_vision_service
        vs = get_vision_service()
        if vs and vs.is_running():
            vs.stop()
    except Exception:
        pass


def _vision_running() -> bool:
    try:
        from integrations.vision.vision_service import get_vision_service
        vs = get_vision_service()
        return bool(vs and vs.is_running())
    except Exception:
        return False


def disable_all() -> dict:
    """Cut every sense + stop the services we can. Returns the live status."""
    with _lock:
        for k in _SENSES:
            _state[k] = True
    _stop_vision()
    return status()


def enable_all() -> dict:
    """Wake every sense (services restart lazily on next use)."""
    with _lock:
        for k in _SENSES:
            _state[k] = False
    return status()


def set_sense(sensor: str, disabled: bool) -> dict:
    with _lock:
        if sensor in _state:
            _state[sensor] = bool(disabled)
    if sensor == 'camera' and disabled:
        _stop_vision()
    return status()


def status() -> dict:
    """Live proof: the human-set gate flags + a REAL check where one exists."""
    with _lock:
        disabled = dict(_state)
    return {
        'sensing_enabled': not all(disabled.values()),   # any sense still on?
        'disabled': disabled,                            # per-sense human gate
        'proof': {
            # Observable OS-level state, not just the flag — this is the bit the
            # AI cannot fake. (mic/screen are enforced at ingestion by the flag.)
            'camera_service_running': _vision_running(),
            'mic_gated': disabled['mic'],
            'screen_gated': disabled['screen'],
            # Cross-process screencast verdict (Phase 7): when the human cuts
            # 'screen', the xdg-desktop-portal-hart ScreenCast/screencopy gate
            # REFUSES every native (Flatpak/Wine/Qt) capture too — not just the
            # in-process VLM grab. This is the un-fakeable proof the portal
            # path is shut, mirroring camera_service_running for the camera.
            # True == every screencast surface is blocked at the portal.
            'portal_screencast_blocked': disabled['screen'],
        },
    }


# ── Cross-process authority (Phase 7) ───────────────────────────────────────

def _authority_path(path: str = None) -> str:
    """Resolve the cross-process authority socket path.

    Priority: explicit arg > HART_AI_SENSING_SOCK env > $XDG_RUNTIME_DIR.
    In production only the env branch is reachable: the brain (hart-backend) is
    a SYSTEM service with no XDG_RUNTIME_DIR, and the portal is its OWN systemd
    unit, so the two would otherwise resolve different paths. hart-portal.nix
    pins both sides to /run/hart/ai-sensing.sock via this env var. A path
    mismatch does not fail-open: query_authority fail-CLOSES on connect error,
    so a mismatch denies capture. The env var only makes the happy path work."""
    return (path
            or os.environ.get('HART_AI_SENSING_SOCK')
            or os.path.join(
                os.environ.get('XDG_RUNTIME_DIR', '/tmp'), 'hart-ai-sensing.sock'))


def start_authority_server(path: str = None) -> bool:
    """Expose the sense gate over a Unix socket so a SEPARATE process (the
    screencast portal — its own systemd unit) can consult allowed(sensor) and
    fail-closed. Runs in the canonical state holder (the brain). Returns False
    where AF_UNIX is unavailable or the bind fails — the caller then keeps the
    no-native-capture invariant rather than shipping an unguarded surface."""
    if not hasattr(socket, 'AF_UNIX'):
        return False
    sock_path = _authority_path(path)
    try:
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        srv.listen(8)
    except Exception:
        return False

    def _serve():
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                break
            try:
                sensor = conn.recv(64).decode('ascii', 'replace').strip()
                conn.sendall(b'1' if allowed(sensor) else b'0')
            except Exception:
                try:
                    conn.sendall(b'0')          # fail-closed on any error
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=_serve, daemon=True,
                     name='ai-sensing-authority').start()
    return True


def query_authority(sensor: str, path: str = None, timeout: float = 1.0) -> bool:
    """Cross-process query of the sense gate. FAIL-CLOSED: returns False
    (denied) if the authority is unreachable or errors — a portal that cannot
    reach the gate must NOT capture. The portal consults THIS, never its own
    flag, so the human's cut applies in the portal process too."""
    if not hasattr(socket, 'AF_UNIX'):
        return False
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        c.connect(_authority_path(path))
        c.sendall(sensor.encode('ascii'))
        reply = c.recv(8).strip()
        c.close()
        return reply == b'1'
    except Exception:
        return False

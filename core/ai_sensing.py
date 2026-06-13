"""AI sensory kill-switch — the human's hard cut over what the AI can sense.

A single authoritative gate the human flips from the shell (the floating eye
button). Every real-world sensor ingestion the shell controls checks this gate:
  * mic   — /api/voice refuses to transcribe when hearing is cut,
  * camera— the vision service is stopped (real, observable),
  * screen— gated for any consumer that honours it.
`status()` reports the LIVE state (e.g. the vision service really is stopped),
so the proof shown to the user cannot be faked by the AI. Crucially, the AI has
NO route to flip this — only the human UI (POST /api/shell/ai-sensing) does.
This is the desktop expression of HART's "humans are always in control".

Single source of truth: import `allowed(sensor)` at every sensor ingestion point
rather than re-implementing a per-feature mute.
"""
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
        },
    }

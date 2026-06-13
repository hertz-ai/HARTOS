"""Cache the system proxy lookup so ``requests``/``urllib`` don't read the
Windows registry on EVERY HTTP call.

``requests`` calls ``getproxies()`` per request (via ``merge_environment_settings``
when ``trust_env`` is on); on Windows that runs ``getproxies_registry()``, a
documented per-call registry read. The 2026-06-13 sluggishness dig caught the
``superadmin-report`` loop burning CPU in exactly that path — and every other
``requests`` call in the app pays it too.

The proxy configuration is static within a session, so a TTL cache returns the
SAME proxy dict with zero behavioural change — the only difference is a bounded
(<= ttl) delay before a mid-session proxy edit is noticed, which is fine. This is
discovery, not live state.

Idempotent + safe to call once at startup; patches both ``urllib.request`` and
``requests.utils`` (requests binds ``getproxies`` by reference at import time, so
patching only urllib would miss it).
"""
import urllib.request

from core.ttl_cache import ttl_cached

_installed = False
_DEFAULT_TTL = 600


def install_proxy_cache(ttl_seconds: int = _DEFAULT_TTL) -> bool:
    """Wrap ``getproxies`` with a hard-TTL cache. Returns True if installed (or
    already installed), False if patching failed (never raises — a failed patch
    must not break startup)."""
    global _installed
    if _installed:
        return True
    try:
        _orig = urllib.request.getproxies
        cached = ttl_cached(ttl_seconds)(_orig)
        urllib.request.getproxies = cached
        try:
            import requests.utils as _ru
            _ru.getproxies = cached  # requests bound it by reference; repoint
        except Exception:
            pass  # requests not importable / different layout — urllib patch still helps
        _installed = True
        return True
    except Exception:
        return False


def _reset_for_test() -> None:
    """Test-only: clear the installed flag (does NOT restore the original
    symbol — tests that patch getproxies restore it themselves)."""
    global _installed
    _installed = False

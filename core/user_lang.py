"""core.user_lang — canonical read/write for the user's preferred language.

Before this module existed, the preferred-language signal leaked across
five unsynchronised readers:
  1. llama_config._read_preferred_lang()        (boot: reads JSON file)
  2. /chat request body data.get('preferred_lang')   (per-turn override)
  3. HART_USER_LANGUAGE env var                 (headless/CI override)
  4. hart_onboarding.get_onboarding_identity()  (first-run onboarding)
  5. user_context.py cloud profile               (cross-device sync)

And a single writer (hart_intelligence_entry._persist_language) with
three buggy guards that caused "hart_language.json stuck at first value
ever written" (see commit ef674b7).

This module owns BOTH sides:

  get_preferred_lang(request_override=None) -> str
      Precedence: request > hart_language.json > env > node_identity > 'en'
      Cached by mtime — ~1µs per call when file unchanged.

  set_preferred_lang(lang) -> bool
      Atomic tmp+os.replace+fsync.  Idempotent (skips write if current
      value matches).  No `!= 'en'` guard, no `not_exists` guard — just
      "if different, write it, fire listeners".

  on_lang_change(callback) -> None
      Subscribe to transition events.  Callback receives (old, new).
      Fires in a daemon thread so /chat hot path isn't stalled.

The intent each of the 5 sources served is preserved:
  - Request override  → honored by `request_override` param.
  - File (boot)       → is the persisted value; read by this module.
  - Env override      → headless deployments; still respected.
  - node_identity     → read once at onboarding; written into the file.
  - Cloud profile     → still read by user_context.py for LLM prompt
                        string (cross-device), unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from core.constants import SUPPORTED_LANG_DICT

logger = logging.getLogger(__name__)


_HART_LANG_PATH = os.path.join(
    os.path.expanduser('~'), 'Documents', 'Nunba', 'data',
    'hart_language.json',
)


# ── Read-side cache (mtime-invalidated) ─────────────────────────────

_cache: dict = {'value': None, 'mtime': 0}
_cache_lock = threading.Lock()


def _load_from_file() -> Optional[str]:
    """Read `hart_language.json` with mtime caching.  Returns None if
    file missing / unreadable / invalid — callers fall back to env or
    default."""
    try:
        st = os.stat(_HART_LANG_PATH)
    except OSError:
        return None
    with _cache_lock:
        if _cache['value'] is not None and _cache['mtime'] == st.st_mtime_ns:
            return _cache['value']
    try:
        with open(_HART_LANG_PATH, encoding='utf-8') as f:
            data = json.load(f) or {}
        lang = data.get('language')
        if not lang or lang[:2] not in SUPPORTED_LANG_DICT:
            return None
        with _cache_lock:
            _cache['value'] = lang
            _cache['mtime'] = st.st_mtime_ns
        return lang
    except Exception:
        return None


def _load_from_env() -> Optional[str]:
    v = os.environ.get('HART_USER_LANGUAGE', '').strip()
    if v and v[:2] in SUPPORTED_LANG_DICT:
        return v
    return None


def _load_from_node_identity() -> Optional[str]:
    """Last-resort read of the onboarding-time language choice.
    Best-effort — returns None on any import/file failure rather than
    exploding a chat request."""
    try:
        from hart_onboarding import get_onboarding_identity
        v = (get_onboarding_identity() or {}).get('language', '')
        if v and v[:2] in SUPPORTED_LANG_DICT:
            return v
    except Exception:
        pass
    return None


def get_preferred_lang(request_override: Optional[str] = None) -> str:
    """Resolve the user's preferred language.

    Precedence (first match wins):
      1. `request_override` — the /chat handler passes `data.get('preferred_lang')`
         here so per-turn UI selections always win.
      2. `hart_language.json` on disk — persisted across boots.
      3. `HART_USER_LANGUAGE` env var — headless / CI override.
      4. hart_onboarding node identity — first-run onboarding answer.
      5. Hard default `'en'`.

    Never raises — always returns a valid ISO 639-1 from
    SUPPORTED_LANG_DICT.
    """
    if request_override:
        code = request_override[:2] if len(request_override) >= 2 else request_override
        if code in SUPPORTED_LANG_DICT:
            return request_override
    v = _load_from_file()
    if v:
        return v
    v = _load_from_env()
    if v:
        return v
    v = _load_from_node_identity()
    if v:
        return v
    return 'en'


# ── Write-side + on-change subscriber bus ───────────────────────────

_listeners: List[Callable[[Optional[str], str], None]] = []
_listeners_lock = threading.Lock()


def on_lang_change(callback: Callable[[Optional[str], str], None]) -> None:
    """Register a callback for (old_lang, new_lang) transitions.
    Callback fires in a daemon thread; exceptions are swallowed.
    No-ops on `set_preferred_lang(x)` when x is already current."""
    with _listeners_lock:
        _listeners.append(callback)


def _fire_listeners(old: Optional[str], new: str) -> None:
    with _listeners_lock:
        snapshot = list(_listeners)

    def _run():
        for cb in snapshot:
            try:
                cb(old, new)
            except Exception as e:
                logger.warning(f"on_lang_change listener {cb!r} failed: {e}")

    threading.Thread(
        target=_run, daemon=True, name='user-lang-change',
    ).start()


def set_preferred_lang(lang: Optional[str]) -> bool:
    """Persist the user's language choice to `hart_language.json`
    atomically AND fire `on_lang_change` listeners on transition.

    Idempotent — if the current on-disk value equals `lang`, no write
    occurs, no listeners fire, returns True.

    Returns False on:
      * invalid `lang` (not in SUPPORTED_LANG_DICT)
      * write failure (disk full, permission denied) — original file
        stays intact because we write to .tmp then atomically replace.
    """
    if not lang:
        return False
    code = lang[:2] if len(lang) >= 2 else lang
    if code not in SUPPORTED_LANG_DICT:
        return False

    # Read current (skip if unchanged)
    current = _load_from_file()
    if current == lang:
        return True  # idempotent — no write, no listener fire

    tmp = _HART_LANG_PATH + '.tmp'
    try:
        os.makedirs(os.path.dirname(_HART_LANG_PATH), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'language': lang}, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, _HART_LANG_PATH)
    except OSError as e:
        # Clean up tmp and leave original file intact
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        logger.warning(f"set_preferred_lang({lang!r}) failed: {e}")
        return False

    # Invalidate cache so next get_preferred_lang sees the new value
    with _cache_lock:
        _cache['value'] = lang
        try:
            _cache['mtime'] = os.stat(_HART_LANG_PATH).st_mtime_ns
        except OSError:
            _cache['mtime'] = 0

    _fire_listeners(current, lang)
    return True


__all__ = [
    'get_preferred_lang',
    'set_preferred_lang',
    'on_lang_change',
]

"""core/user_context.py — canonical user action + profile resolver.

Single source of truth for the ``(user_details, actions)`` tuple that
was previously implemented THREE times — in ``hart_intelligence_entry``,
``create_recipe``, and ``reuse_recipe`` — with subtle drift. Consolidating
here fixes the four non-classification problems the reviewer flagged
in the 2026-04-11 "hi took 33.8s" post-mortem:

1. **Hard time budget.** Every HTTP fetch is bounded by a total budget
   (default 1.5s). If the budget blows, we return cached-or-default
   instantly and spawn a background refresh so the NEXT request has
   fresh data — the hot path never blocks more than the budget
   regardless of how slow the backend gets. This alone collapses the
   33.8s worst case to 1.5s.
2. **30-second TTL cache per user_id.** Fetching the same action
   history + profile on every chat message was pure waste. The cache
   collapses that to one fetch per 30s of activity — the second "hi"
   within 30s is microseconds.
3. **Deduplication / SRP.** The three copies lived in three modules
   with different filter lists (some skipped ``Screen Reasoning``,
   some didn't), different crash behavior on missing profile fields,
   and different use of ``pooled_request`` vs raw ``requests``. One
   canonical resolver with a ``mode`` parameter gives callers the
   create/reuse behavior they need without duplicating HTTP plumbing.
4. **Thread-safety.** Background refresh uses a bounded
   ``ThreadPoolExecutor`` and the cache is a ``TTLCache`` — the same
   primitives ``core/session_cache.py`` already uses for other per-
   user state. No new concurrency primitives, no new parallel paths.

**No Python-side classification.** An earlier draft of this module
had a ``_is_casual_greeting(query)`` regex short-circuit ("hi" /
"hello" / "hey" → skip HTTP entirely). That was reverted on operator
directive: keyword regex hacks for chat classification are exactly
the kind of drifting, locale-brittle shadow-code the "no parallel
paths" sweep is trying to eliminate. Chat intent classification is
owned by the draft 0.8B model (``speculative_dispatcher.
dispatch_draft_first``) which emits a structured envelope with
``is_casual`` / ``is_correction`` / ``is_create_agent`` flags. If a
future caller wants to skip the action-history fetch based on that
classification, it should pass the draft's already-computed
``is_casual`` flag down — not re-classify in Python with a regex.

Architecture (layered, SRP):

    get_user_context(user_id, mode, ...)            ← public entry
        │
        ├── UserContextCache.get / set              ← caching layer
        ├── _fetch_actions_raw(user_id, budget)     ← HTTP layer
        ├── _fetch_profile_raw(user_id, budget)     ← HTTP layer
        ├── _format_action_rich(...)                ← formatting layer
        ├── _format_action_simple(...)              ← formatting layer
        └── _schedule_background_refresh(...)       ← async refresh layer

Callers (``hart_intelligence_entry``, ``create_recipe``, ``reuse_recipe``)
pass ``mode='reuse'`` or ``mode='create'``. They no longer own any of
the HTTP, caching, or formatting logic.
"""
from __future__ import annotations
import atexit as _atexit

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Literal, Optional

import pytz
from dateutil.parser import parse as parse_date

from core.http_pool import pooled_get, pooled_post
from core.session_cache import TTLCache

logger = logging.getLogger('hevolve.user_context')


# ─── Module constants ─────────────────────────────────────────────────────

#: Default cache TTL. 30s is short enough that a user changing their
#: display name in settings sees the change on the next chat message,
#: but long enough to absorb a burst of messages without re-fetching.
DEFAULT_TTL_SECONDS = 30.0

#: Default hot-path time budget. The old code timed out at 5s per HTTP
#: call (10s combined); that's four draft-model replies. 1.5s total
#: gives either call a chance to return on a healthy local backend,
#: and still lets us fall through to cached-or-default if the backend
#: is GIL-starved (which was the exact 2026-04-11 incident).
DEFAULT_BUDGET_SECONDS = 1.5

#: Cap on the number of cached users. A single Nunba instance rarely
#: talks to more than a handful of concurrent users but the cache is
#: sized with headroom so a stampede of channel messages doesn't evict
#: the session that's driving the UI.
MAX_CACHED_USERS = 256

#: Actions the agent system produces as noise during training and
#: should not be echoed back to the LLM as "past user actions".
_UNWANTED_ACTIONS = frozenset([
    'Topic Cofirmation', 'Topic Confirmation', 'Topic confirmation',
    'Topic not found', 'Topic Listing',
    'Langchain', 'Assessment Ended', 'Casual Conversation',
    'Probe', 'Question Answering', 'Fallback',
])

#: IANA timezone for rendering action timestamps. Previously hardcoded
#: in every copy of the function — leaves a TODO in the old code
#: ("get, and populate timezone from client").
# TODO: read from thread-local session or user profile once the
# frontend sends that header. For now this matches the legacy behavior.
_DEFAULT_TZ = 'Asia/Kolkata'


# ─── Layer 1: caching ─────────────────────────────────────────────────────

class UserContextCache:
    """Thread-safe per-user cache for ``(user_details, actions)`` tuples.

    Wraps the shared ``TTLCache`` primitive from ``core/session_cache.py``
    — deliberately uses the existing TTL/LRU machinery instead of a
    parallel implementation. The only reason this class exists is to
    give callers a typed, single-purpose handle so the cache key naming
    (``{mode}:{user_id}``) stays in one place.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_size: int = MAX_CACHED_USERS):
        # int(ttl_seconds) because TTLCache stores seconds as int. A
        # fractional-second TTL would require rewriting TTLCache, which
        # is out of scope — 30s rounding to 30 is lossless.
        self._cache = TTLCache(
            ttl_seconds=int(ttl_seconds),
            max_size=max_size,
            name='user_context',
        )

    @staticmethod
    def _key(user_id, mode: str) -> str:
        return f"{mode}:{user_id}"

    def get(self, user_id, mode: str) -> Optional[tuple[str, str]]:
        """Return the cached ``(user_details, actions)`` tuple or None."""
        return self._cache.get(self._key(user_id, mode))

    def set(self, user_id, mode: str, value: tuple[str, str]) -> None:
        self._cache[self._key(user_id, mode)] = value

    def invalidate(self, user_id, mode: Optional[str] = None) -> None:
        """Drop one or all cached entries for a user.

        When ``mode`` is None, both create and reuse entries are
        dropped so a profile change is visible to either path on the
        next request.
        """
        if mode is None:
            for m in ('create', 'reuse'):
                try:
                    del self._cache[self._key(user_id, m)]
                except KeyError:
                    pass
        else:
            try:
                del self._cache[self._key(user_id, mode)]
            except KeyError:
                pass


#: Module-level singleton — mirrors the ``_registry`` / ``_integration``
#: pattern used by ``channels/registry.py`` and others.
_cache: Optional[UserContextCache] = None
_cache_lock = threading.Lock()


def get_user_context_cache() -> UserContextCache:
    """Return the singleton UserContextCache, constructing on first call."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = UserContextCache()
    return _cache


# ─── Layer 2: background refresh pool ─────────────────────────────────────

#: Separate thread pool for cache refreshes so the hot path never
#: blocks on pool saturation. Small (4 workers) — refreshes are cheap
#: and we don't want to steal CPU from the LLM subprocesses.
_refresh_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='uctx-refresh')

#: SHUT IT DOWN AT EXIT. ThreadPoolExecutor workers are NON-daemon, so
#: concurrent.futures' own atexit hook JOINS them before the interpreter
#: can exit — a pool that is never shut down keeps the process alive.
#: Named 'uctx-refresh_0' in CI run 30978623541, blocked in a refresh that
#: never returned, which is how five of eight test shards burned the full
#: 120-minute cap in silence (task #37). Same hazard for the BACKEND: a
#: process that will not exit turns `systemctl stop` into a timeout.
#: wait=False so shutdown never blocks the dying process; this is the
#: pattern already used by world_model_bridge and speculative_dispatcher.
_atexit.register(lambda: _refresh_pool.shutdown(wait=False))

#: Per-user lock that prevents N concurrent refreshes of the same user.
#: A stampede of messages for user 10077 still triggers exactly one
#: background refresh instead of N parallel HTTP calls.
_refresh_inflight: dict = {}
_refresh_inflight_lock = threading.Lock()


def _schedule_background_refresh(user_id, mode: str, timeout_budget_s: float) -> None:
    """Kick off a non-blocking cache refresh for a user.

    Deduped — if a refresh is already in flight for this (user_id, mode),
    this call is a no-op.
    """
    key = (user_id, mode)
    with _refresh_inflight_lock:
        if key in _refresh_inflight:
            return
        _refresh_inflight[key] = True

    def _task():
        try:
            _resolve_fresh(user_id, mode, timeout_budget_s)
        except Exception as e:
            logger.debug("background refresh failed for %s: %s", key, e)
        finally:
            with _refresh_inflight_lock:
                _refresh_inflight.pop(key, None)

    try:
        _refresh_pool.submit(_task)
    except Exception as e:
        logger.debug("refresh submit failed for %s: %s", key, e)
        with _refresh_inflight_lock:
            _refresh_inflight.pop(key, None)


# ─── Layer 3: HTTP fetchers ───────────────────────────────────────────────

def _action_api_url(user_id) -> str:
    """Resolve the action-history URL. Defers import to avoid circulars
    with ``config_cache`` at module-load time."""
    from core.config_cache import get_action_api
    return f"{get_action_api()}?user_id={user_id}"


def _student_api_url() -> str:
    from core.config_cache import get_student_api
    return get_student_api()


def _fetch_actions_raw(user_id, timeout_s: float) -> Optional[list]:
    """GET the raw action list for a user. Returns None on any failure.

    Uses ``pooled_get`` so connection reuse and the shared HTTP pool
    limits apply — raw ``requests.request`` in the legacy copies
    bypassed pooling and contributed to the 33.8s stall.
    """
    try:
        response = pooled_get(_action_api_url(user_id), timeout=timeout_s)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.debug("action fetch failed for user %s: %s", user_id, e)
    return None


def _fetch_profile_raw(user_id, timeout_s: float) -> Optional[dict]:
    """POST to the student profile API. Returns None on any failure."""
    try:
        body = json.dumps({"user_id": user_id})
        headers = {'Content-Type': 'application/json'}
        response = pooled_post(
            _student_api_url(), data=body, headers=headers, timeout=timeout_s,
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.debug("profile fetch failed for user %s: %s", user_id, e)
    return None


# ─── Layer 5: formatting ──────────────────────────────────────────────────

def _get_tz():
    try:
        return pytz.timezone(_DEFAULT_TZ)
    except Exception:
        return None


def _format_action_simple(raw_actions: list) -> str:
    """CREATE-mode action formatter.

    Mirrors the create_recipe.py version: one line per action with an
    ISO-formatted timestamp, no deduplication, no visual/screen
    context windows. The create flow is during initial agent training
    where the teacher walks through actions explicitly — dedup and
    video context would clutter the prompt.
    """
    tz = _get_tz()
    filtered = [
        obj for obj in raw_actions
        if obj.get("action") not in _UNWANTED_ACTIONS
        and obj.get("zeroshot_label") not in ('Video Reasoning',)
    ]
    texts = []
    for obj in filtered:
        action = obj.get("action", "")
        try:
            date = parse_date(obj["created_date"])
            rendered = date.astimezone(tz) if tz else date
            texts.append(f"{action} on {rendered.strftime('%Y-%m-%dT%H:%M:%S')}")
        except Exception:
            texts.append(action)
    if not texts:
        return 'user has not performed any actions yet.'
    return ", ".join(texts)


def _format_action_rich(raw_actions: list) -> str:
    """REUSE-mode action formatter with dedup + visual + screen context.

    Mirrors hart_intelligence_entry / reuse_recipe:
    - Dedup by action name with first/last occurrence dates.
    - 5-minute visual context window tagged with
      ``<Last_5_Minutes_Visual_Context_Start/End>``.
    - 2-minute screen context window tagged with
      ``<Last_2_Minutes_Screen_Context_Start/End>``.
    - Trailing current-time hint used by the LLM for "what time is it"
      style questions.
    """
    tz = _get_tz()
    now = datetime.now()

    filtered = [
        obj for obj in raw_actions
        if obj.get("action") not in _UNWANTED_ACTIONS
        and obj.get("zeroshot_label") not in ('Video Reasoning', 'Screen Reasoning')
    ]
    filtered_video = [
        obj for obj in raw_actions
        if obj.get("zeroshot_label") == 'Video Reasoning'
    ]
    filtered_screen = [
        obj for obj in raw_actions
        if obj.get("zeroshot_label") == 'Screen Reasoning'
    ]

    # Dedup: first/last date per action name.
    action_occurrences: dict = {}
    for obj in filtered:
        action = obj.get("action", "")
        try:
            date = parse_date(obj["created_date"])
        except Exception:
            continue
        existing = action_occurrences.get(action)
        if existing is None:
            action_occurrences[action] = [date, date]
        else:
            first_date, last_date = existing
            action_occurrences[action] = [min(first_date, date), max(last_date, date)]

    action_texts = []
    for action, (first_date, last_date) in action_occurrences.items():
        first_r = first_date.astimezone(tz) if tz else first_date
        action_texts.append(f"{action} on {first_r.strftime('%Y-%m-%dT%H:%M:%S')}")
        if first_date != last_date:
            last_r = last_date.astimezone(tz) if tz else last_date
            action_texts.append(f"{action} on {last_r.strftime('%Y-%m-%dT%H:%M:%S')}")

    # Visual context window (last 5 minutes).
    video_texts = []
    for obj in filtered_video:
        try:
            date = parse_date(obj["created_date"])
        except Exception:
            continue
        if obj.get("gpt3_label") == 'Visual Context':
            if (now - date.replace(tzinfo=None)) > timedelta(minutes=5):
                continue
        date_r = date.astimezone(tz) if tz else date
        video_texts.append(
            f"{obj.get('action', '')} on {date_r.strftime('%Y-%m-%dT%H:%M:%S')}")
    if video_texts:
        action_texts.append('<Last_5_Minutes_Visual_Context_Start>')
        action_texts.extend(video_texts)
        action_texts.append('<Last_5_Minutes_Visual_Context_End>')
        action_texts.append(
            "If a person is identified in Visual_Context section "
            "that's most probably the user (me) & most likely not "
            "taking any selfie.")

    # Screen context window (last 2 minutes).
    screen_texts = []
    for obj in filtered_screen:
        try:
            date = parse_date(obj["created_date"])
        except Exception:
            continue
        if (now - date.replace(tzinfo=None)) > timedelta(minutes=2):
            continue
        date_r = date.astimezone(tz) if tz else date
        screen_texts.append(
            f"{obj.get('action', '')} on {date_r.strftime('%Y-%m-%dT%H:%M:%S')}")
    if screen_texts:
        action_texts.append('<Last_2_Minutes_Screen_Context_Start>')
        action_texts.extend(screen_texts)
        action_texts.append('<Last_2_Minutes_Screen_Context_End>')
        action_texts.append(
            "Screen_Context shows what is currently displayed on the "
            "user's computer screen.")

    if not action_texts:
        action_texts = ['user has not performed any actions yet.']
    actions = ", ".join(action_texts)

    formatted_time = datetime.now(pytz.utc).astimezone(tz).strftime(
        '%Y-%m-%d %H:%M:%S') if tz else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    actions += (
        f". List of actions ends. <PREVIOUS_USER_ACTION_END> \n "
        f"Today's datetime in {_DEFAULT_TZ}is: {formatted_time} in this "
        f"format:'%Y-%m-%dT%H:%M:%S' \n Whenever user is asking about "
        f"current date or current time at particular location then use "
        f"this datetime format by asking what user's location is. Use "
        f"the previous sentence datetime info to answer current time "
        f"based questions coupled with google_search for current time "
        f"or full_history for historical conversation based answers. "
        f"Take a deep breath and think step by step.\n"
    )
    return actions


def _format_profile(user_data: Optional[dict], verbose: bool = True) -> str:
    """Render a user profile dict into the prompt-ready string.

    Uses ``.get()`` with "not specified" defaults throughout — the old
    ``reuse_recipe`` copy crashed on ``KeyError`` when a guest user had
    no cloud profile, which the hart_intelligence_entry copy already
    fixed. We keep the safer path as the only path.
    """
    if not user_data:
        return "No user details available."

    name = user_data.get("name") or user_data.get("display_name") \
        or user_data.get("username") or "User"
    gender = user_data.get("gender", "not specified")
    lang = user_data.get("preferred_language", "not specified")
    dob = user_data.get("dob", "not specified")
    eng = user_data.get("english_proficiency", "not specified")
    created = user_data.get("created_date", "unknown")
    standard = user_data.get("standard", "not specified")
    pays = user_data.get("who_pays_for_course", "not specified")

    if verbose:
        return (
            f"Below are the information about the user.\n"
            f"user_name: {name} (Call the user by this name only when "
            f"required and not always), gender: {gender}, "
            f"who_pays_for_course: {pays}(Entity Responsible for Paying "
            f"the Course Fees), preferred_language: {lang}(User's "
            f"Preferred Language), date_of_birth: {dob}, "
            f"english_proficiency: {eng}(User's English Proficiency "
            f"Level), created_date: {created}(user creation date), "
            f"standard: {standard}(User's Standard in which user studying)\n"
            f"If any of the above fields show \"not specified\", do not "
            f"ask the user for this information proactively. Only note "
            f"it when naturally relevant. The user's privacy is paramount "
            f"— store preferences locally when volunteered, never push "
            f"for personal data."
        )
    # Simple format for create-mode agent training.
    return (
        f"Below are the information about the user.\n"
        f"user_name: {name}, gender: {gender}, "
        f"preferred_language: {lang}, date_of_birth: {dob}"
    )


# ─── Layer 5: cheap-defaults (used on budget-blown fetch with empty cache) ───

#: Fallback strings used when the backend is unreachable AND nothing is
#: in the cache. These are intentionally generic — the LLM sees them
#: and understands "no profile / no action history available". They
#: used to be named with a "GREETING" prefix from the now-removed
#: regex short-circuit path; the module no longer has any classifier.
_DEFAULT_ACTIONS_EMPTY = 'user has not performed any actions yet.'
_DEFAULT_DETAILS_EMPTY = 'No user details available.'


def _cheap_defaults(mode: str) -> tuple[str, str]:
    """Return the zero-HTTP fallback tuple used when the budget is
    blown AND the cache is empty. Not used for classification — the
    only caller is :func:`get_user_context` after it gives up on a
    stalled backend and has no cached entry to fall back to."""
    if mode == 'reuse':
        tz = _get_tz()
        formatted_time = datetime.now(pytz.utc).astimezone(tz).strftime(
            '%Y-%m-%d %H:%M:%S') if tz else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        actions = (
            f"{_DEFAULT_ACTIONS_EMPTY}. <PREVIOUS_USER_ACTION_END> \n "
            f"Today's datetime in {_DEFAULT_TZ}is: {formatted_time}"
        )
    else:
        actions = _DEFAULT_ACTIONS_EMPTY
    return _DEFAULT_DETAILS_EMPTY, actions


# ─── Layer 7: orchestration ───────────────────────────────────────────────

def _resolve_fresh(user_id, mode: str, timeout_budget_s: float) -> tuple[str, str]:
    """Fetch + format fresh data from the backend, respecting a total
    time budget. Populates the cache on success.

    The budget is divided 50/50 between the two HTTP calls so a single
    slow endpoint can't consume the whole budget and starve the other.
    """
    per_call_budget = max(0.3, timeout_budget_s / 2.0)

    raw_actions = _fetch_actions_raw(user_id, per_call_budget)
    user_data = _fetch_profile_raw(user_id, per_call_budget)

    if mode == 'reuse':
        actions_text = _format_action_rich(raw_actions or [])
        details_text = _format_profile(user_data, verbose=True)
    else:
        actions_text = _format_action_simple(raw_actions or [])
        details_text = _format_profile(user_data, verbose=False)

    result = (details_text, actions_text)
    # Only cache if at least one HTTP call succeeded — caching a pure
    # default would lock the user into defaults for 30s after a
    # transient backend hiccup.
    if raw_actions is not None or user_data is not None:
        try:
            get_user_context_cache().set(user_id, mode, result)
        except Exception as e:
            logger.debug("cache set failed for user %s: %s", user_id, e)
    return result


def get_user_context(
    user_id,
    mode: Literal['create', 'reuse'] = 'reuse',
    timeout_budget_s: float = DEFAULT_BUDGET_SECONDS,
    ttl_s: float = DEFAULT_TTL_SECONDS,
) -> tuple[str, str]:
    """Canonical public entry point.

    Two decision layers stacked fast-first:

      1. Cache hit — if we fetched the same (user_id, mode) within
         the TTL, return the cached tuple in microseconds.
      2. Budget-guarded fresh fetch — submit the HTTP fetch to a
         background thread and wait on it with a hard wall-clock
         deadline. If the fetch completes in time, its result is
         cached and returned. If it blows the budget, the running
         future is LEFT RUNNING (it will populate the cache when it
         eventually lands) and we return cheap defaults immediately
         so the hot path never blocks past ``timeout_budget_s``.

    This function deliberately does NOT classify the user's chat
    message. Chat intent classification is owned by the draft 0.8B
    model in ``speculative_dispatcher.dispatch_draft_first`` with its
    augmented classifier prompt — callers that want to skip fetching
    for a casual message should consult the draft's structured
    envelope, never re-classify here with Python regex.

    Args:
        user_id: Hevolve user id (int or str — passed through to the
            backend as-is).
        mode: ``'reuse'`` for normal chat path (rich formatting,
            visual + screen context, verbose profile). ``'create'``
            for the initial agent-training path (simple formatting,
            no context windows).
        timeout_budget_s: Hard wall-clock budget for the HTTP fetch
            phase. Defaults to ``DEFAULT_BUDGET_SECONDS`` (1.5s).
        ttl_s: Cache freshness window. Reserved for future per-call
            overrides — the module-level constant applies today.

    Returns:
        ``(user_details, actions)`` — both strings, both safe to embed
        in an LLM prompt. On total failure both default to cheap
        placeholder strings, never None, never an exception.
    """
    del ttl_s  # Reserved for future use; the module-level constant applies.

    # Layer 1: cache hit.
    cache = get_user_context_cache()
    cached = cache.get(user_id, mode)
    if cached is not None:
        logger.debug("user_context: cache hit user=%s mode=%s", user_id, mode)
        return cached

    # Layer 2: budget-guarded fetch. We want the fetch to COMPLETE
    # inside the budget, not merely to start it — so we use a thread
    # with a future.result(timeout) wall.
    start = time.monotonic()
    future = _refresh_pool.submit(_resolve_fresh, user_id, mode, timeout_budget_s)
    try:
        result = future.result(timeout=timeout_budget_s)
        logger.debug(
            "user_context: fresh fetch user=%s mode=%s %.2fs",
            user_id, mode, time.monotonic() - start,
        )
        return result
    except Exception as e:
        # Timeout or fetch error. Return cheap defaults IMMEDIATELY and
        # let the already-submitted future keep running — when it lands,
        # it will populate the cache so the next request is fast.
        logger.info(
            "user_context: hot-path budget blown (%.2fs > %.2fs) user=%s: %s — "
            "returning defaults, refresh continues in background",
            time.monotonic() - start, timeout_budget_s, user_id, e,
        )
        return _cheap_defaults(mode)


# ─── Public helpers ───────────────────────────────────────────────────────

def invalidate_user_context(user_id, mode: Optional[str] = None) -> None:
    """Drop cached entries for a user. Call when the backend writes a
    profile update or when auth state changes."""
    try:
        get_user_context_cache().invalidate(user_id, mode)
    except Exception as e:
        logger.debug("invalidate failed for %s: %s", user_id, e)


__all__ = [
    'get_user_context',
    'get_user_context_cache',
    'invalidate_user_context',
    'UserContextCache',
    'DEFAULT_BUDGET_SECONDS',
    'DEFAULT_TTL_SECONDS',
]

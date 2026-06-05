"""Global httpx hook — logs every chat-completion POST to llama-server
:8082 with the full request body, and injects the HARTOS thread-local
``request_id`` as the OpenAI ``user`` field so the same correlation
key is visible to anything downstream that DOES log it.

Why this lives at the httpx layer, not autogen / langchain individually
-----------------------------------------------------------------------
The 2026-05-12 IPL-scores ctx-overflow incident exposed two gaps:

  * llama-server logs every request it processes but never the prompt
    content — only token counts (verified by Explore audit + grep of
    a 190 MB llama_server_8082.log: zero ``"user":`` matches, no
    request bodies, no timestamps until we add ``--log-timestamps``).
  * The autogen-side ``runtime_logging`` we briefly tried catches only
    the ~40 % of calls that flow through autogen.  Calls from
    langchain (``agentic_router.find_matching_agent``), the
    speculative dispatcher's raw ``requests.post`` draft path, and
    direct ``openai`` SDK use all bypass it.

Both autogen and langchain ultimately funnel HTTP through ``httpx``
(``openai`` SDK uses it internally).  Patching ``httpx.Client.send``
once at boot catches every Python-originated POST to llama-server
uniformly — single module, single log file, single grep.

Scope (intentionally narrow)
----------------------------
Only ``POST :8082/v1/chat/completions`` is touched.  Draft model on
:8081, embedding endpoints, vision frame uploads, and any non-LLM
httpx traffic pass through ``_orig_send`` unmodified.  Direct curl
probes outside the Python process aren't caught — that's a real
limitation; for those, llama-server's own log (with timestamps from
``--log-timestamps``) is the only source.

Body retention policy
---------------------
Full request body is logged by default (``HEVOLVE_LLM_OUTBOUND_BODY``
unset or ``full``).  Set to ``trim`` to keep first 2 + last 1 messages
and collapse the middle.  Set to ``off`` to keep only header fields
(model, n_messages, n_tools).  Errors during logging are swallowed —
the HTTP call must never fail because we couldn't write to disk.

Output
------
JSONL appended to ``~/Documents/Nunba/logs/llm_outbound.jsonl``.
Each line: ``{ts, request_id, source, body, response_status,
latency_ms}``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger('llm_outbound')

# 8082 is the LEGACY default this hook shipped with.  The MAIN model the
# autogen / langchain / chat path actually calls resolves via
# get_local_llm_url() (now :8080 by registry default).  Watching only 8082 made
# every main-model call invisible to this log AND silently skipped the n_ctx
# trim + the background-yield routing for it once the server moved to 8080.
# _target_ports() resolves the live endpoint port so the hook never drifts from
# the real server again.  See task #86.
_TARGET_PORT = 8082
_TARGET_PATH = '/v1/chat/completions'
_LOG_FILENAME = 'llm_outbound.jsonl'
# (ports, resolved_at) — re-resolved on a short TTL, NOT cached for the process
# lifetime.  The llama-server port is NOT fixed: Nunba assigns it dynamically
# (records it in ~/.nunba/llama_config.json server_port) and REASSIGNS on port
# conflict / restart; get_local_llm_url() follows that via its own probe-TTL.
# A permanent cache here froze the watched port at first resolution — e.g. a
# cold-boot placeholder before llama-server spawns — and silently re-blinded
# the hook the moment the server landed on a different port (the exact drift
# #86 set out to kill).  So mirror the resolver's TTL and re-resolve.
_target_ports_cache = None  # type: Optional[tuple]
_TARGET_PORTS_TTL = 30.0  # seconds — match get_local_llm_url's cache TTL

_installed = False
_install_lock = threading.Lock()
_file_handle = None  # type: Optional[Any]
_file_lock = threading.Lock()


def _get_request_id() -> str:
    """Best-effort thread-local request_id pull.  Empty string when
    we're outside an HTTP request context (e.g. daemon startup).

    Uses the canonical ``ThreadLocalData.get_request_id()`` accessor — the
    request_id lives in the thread-local's ``_local`` store (set via
    ``set_request_id`` at hart_intelligence_entry.py:8535 and read via
    ``get_request_id`` everywhere else).  A previous ``getattr(_tl,
    'request_id')`` read the INSTANCE attribute, which is never set, so the
    ``X-HARTOS-Request-ID`` header + JSONL correlation key were always empty —
    and the daemon-vs-user discriminator that now reads them would never fire."""
    try:
        from threadlocal import thread_local_data as _tl
        rid = _tl.get_request_id()
        if rid:
            return str(rid)
    except Exception:
        pass
    return ''


# ─── Origin-source tagging ────────────────────────────────────────────
# Callers set the source label via ``set_source('autogen.create')``
# before triggering an LLM call.  The httpx hook reads it and (a)
# stamps it into the JSONL record's ``source`` field, (b) adds an
# ``X-HARTOS-Source`` HTTP header so any proxy / future log scrape
# can recover the call's origin even without our JSONL.  llama.cpp
# silently ignores unknown headers — confirmed by Explore audit; no
# binary changes required.
import contextlib
import contextvars

_source_var: 'contextvars.ContextVar[str]' = contextvars.ContextVar(
    'llm_outbound_source', default='')


def set_source(name: str) -> 'contextvars.Token':
    """Set the origin label for LLM calls issued from this context.
    Returns a Token; pass to ``reset_source`` to restore the prior
    value.  Prefer the ``source_context`` ctxmgr below for safety."""
    return _source_var.set(name)


def reset_source(token: 'contextvars.Token') -> None:
    _source_var.reset(token)


@contextlib.contextmanager
def source_context(name: str):
    """``with source_context('langchain.main'): llm.invoke(prompt)``
    — automatically restores prior value on exit, even on exception."""
    token = _source_var.set(name)
    try:
        yield
    finally:
        _source_var.reset(token)


def with_source(name: str):
    """Decorator that wraps a function body in ``source_context(name)``.
    Every LLM call issued from the decorated function (or anything it
    calls transitively, modulo nested ``source_context`` overrides)
    gets ``source=name`` in the outbound log.  One-liner alternative
    to wrapping the whole function body with ``with``.

    Usage::

        from core.llm_outbound_logger import with_source

        @with_source('autogen.create')
        def recipe(user_id, text, prompt_id, file_id, request_id):
            ...
    """
    import functools

    def _deco(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            with source_context(name):
                return fn(*args, **kwargs)
        return _wrapper
    return _deco


def _get_source() -> str:
    """Read the current thread/task's origin label.  Empty string is
    legal — means the caller didn't tag (still gets logged, just with
    ``source=''``)."""
    try:
        return _source_var.get() or ''
    except Exception:
        return ''


def _get_log_path() -> str:
    try:
        from core.platform_paths import get_log_dir
        return os.path.join(get_log_dir(), _LOG_FILENAME)
    except Exception:
        return os.path.join(
            os.path.expanduser('~'), 'Documents', 'Nunba', 'logs',
            _LOG_FILENAME,
        )


def _open_log_handle():
    global _file_handle
    if _file_handle is None or getattr(_file_handle, 'closed', True):
        path = _get_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _file_handle = open(path, 'a', encoding='utf-8', buffering=1)
    return _file_handle


def _target_ports() -> set:
    """Local llama-server port(s) whose chat-completions we capture.

    Resolves the live MAIN and DRAFT model ports the SAME way the chat path
    does — from ``get_local_llm_url()`` / ``get_local_draft_url()`` (with
    ``get_port('llm')`` and the legacy 8082 as backstops).  Re-resolved every
    ``_TARGET_PORTS_TTL`` seconds rather than cached for the process lifetime,
    so the hook FOLLOWS the server when Nunba reassigns its port (cold-boot
    placeholder -> real port, port-conflict reassignment, server restart)
    instead of freezing — and re-blinding — on the first value.

    The underlying resolvers carry their own 30s probe-cache, so the per-TTL
    re-resolve is cheap and never re-probes a dead candidate on the hot path."""
    global _target_ports_cache
    now = time.time()
    if _target_ports_cache is not None:
        cached_ports, resolved_at = _target_ports_cache
        if (now - resolved_at) < _TARGET_PORTS_TTL:
            return cached_ports
    ports = {_TARGET_PORT}
    try:
        import re as _re
        import core.port_registry as _pr
        for _resolver in ('get_local_llm_url', 'get_local_draft_url'):
            try:
                m = _re.search(r':(\d+)', getattr(_pr, _resolver)() or '')
                if m:
                    ports.add(int(m.group(1)))
            except Exception:
                pass
        ports.add(int(_pr.get_port('llm')))
    except Exception:
        ports.add(8080)
    _target_ports_cache = (ports, now)
    return ports


def _is_target_request(url, method: str) -> bool:
    if method != 'POST':
        return False
    try:
        return (
            getattr(url, 'port', None) in _target_ports()
            and getattr(url, 'path', '') == _TARGET_PATH
        )
    except Exception:
        return False


# ─── Hard left-trim to fit n_ctx (zero-tolerance context overflow) ───
# Architecture note (2026-05-23): autogen and langchain both build
# their own OpenAI clients from config; we cannot route them through a
# caller-side ``llm_client.llm_call`` because their internal call sites
# live inside third-party code.  The httpx wire layer is the ONLY
# place every framework's traffic converges (autogen → openai SDK →
# httpx; langchain → openai/langchain-openai → httpx; raw requests.post
# in the dispatcher draft path bypasses httpx but is the 5 % minority).
# So the trim has to happen here too — it cannot live exclusively at
# the caller-side interface.  When we later add ``llm_client.llm_call``
# for our own code, the trim is idempotent (no-ops on already-fit
# bodies) so applying it at both layers is safe.
#
# Production evidence motivating this fix (2026-05-20 22:22-22:28):
# autogen's recipe-request retry path on ``initiate_chat`` with
# ``clear_history=False`` accumulated chat_instructor history past
# the 12288 / N_slots per-slot budget → llama-server 500 'Context
# size has been exceeded' → cascading json_repair/ast.literal_eval
# log spam + invalid FSM transitions.  Soft autogen-level token
# limiters didn't help — they cap per-message, not aggregate.
#
# Reuses canonical primitives (zero parallel paths):
#   * Token counting:   core.token_utils.count_tokens_for_messages
#                       (single tiktoken-with-fallback impl shared with
#                       budget_gate)
#   * Constants:        core.constants.LLAMA_CTX_SIZE_DEFAULT,
#                       LLAMA_SLOTS_DEFAULT,
#                       WIRE_TRIM_SAFETY_MARGIN_TOKENS,
#                       WIRE_TRIM_MARKER
#   * Multimodal text:  core.token_utils._content_to_text


def _get_budget_per_slot() -> int:
    """Per-slot input token budget.  Honors:
      * ``HEVOLVE_LLAMA_CTX_SIZE`` (default tracks
        ``core.constants.LLAMA_CTX_SIZE_DEFAULT`` = 12288,
        matches Nunba's ``llama_config.py:1527``).
      * ``HEVOLVE_LLAMA_SLOTS`` (default 1 — single-user dev box).
    """
    from core.constants import LLAMA_CTX_SIZE_DEFAULT, LLAMA_SLOTS_DEFAULT
    try:
        ctx = int(os.environ.get('HEVOLVE_LLAMA_CTX_SIZE',
                                  str(LLAMA_CTX_SIZE_DEFAULT)))
        slots = max(1, int(os.environ.get('HEVOLVE_LLAMA_SLOTS',
                                           str(LLAMA_SLOTS_DEFAULT))))
        return ctx // slots
    except Exception:
        return LLAMA_CTX_SIZE_DEFAULT


def _trim_to_budget(body: dict) -> tuple:
    """Return ``(trimmed_body, n_dropped, n_truncated_chars, est_before,
    est_after, budget)``.

    Trim policy (always-succeeds, idempotent):
      1. budget = per_slot - max_tokens - safety
      2. If under budget → return unchanged.
      3. Left-drop non-system messages (preserve index 0 if role=system)
         until the remaining set fits.  Always keep at least the system
         message + the most-recent user/assistant message.
      4. If even [system, last_message] is over budget, left-truncate
         the last message's content character-by-character until it
         fits, prefixed with ``WIRE_TRIM_MARKER`` so the LLM sees the
         truncation.

    Idempotent: calling on an already-trimmed body returns it unchanged.
    Multimodal-aware: rebuilds list-shaped content preserving image
    parts.

    Reuses ``core.token_utils`` for token counting (single source) and
    ``core.constants`` for the safety margin + marker (single source).
    """
    from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS, WIRE_TRIM_MARKER
    from core.token_utils import (
        count_tokens_for_messages, count_tokens_for_text, _content_to_text,
    )

    messages = list(body.get('messages') or [])
    if not messages:
        return body, 0, 0, 0, 0, 0

    model = body.get('model') or None
    max_tokens = int(body.get('max_tokens') or body.get('max_completion_tokens') or 2048)
    budget = _get_budget_per_slot() - max_tokens - WIRE_TRIM_SAFETY_MARGIN_TOKENS
    if budget <= 0:
        # max_tokens alone exceeds n_ctx — degrade gracefully so we
        # still send SOMETHING instead of 500-failing.
        budget = max(512, _get_budget_per_slot() // 4)

    est_before = count_tokens_for_messages(messages, model)
    if est_before <= budget:
        return body, 0, 0, est_before, est_before, budget

    has_system = bool(messages and isinstance(messages[0], dict)
                      and messages[0].get('role') == 'system')
    n_dropped = 0
    while len(messages) > (2 if has_system else 1):
        drop_idx = 1 if has_system else 0
        messages.pop(drop_idx)
        n_dropped += 1
        if count_tokens_for_messages(messages, model) <= budget:
            break

    n_truncated_chars = 0
    if count_tokens_for_messages(messages, model) > budget and messages:
        last = dict(messages[-1])
        last_text = _content_to_text(last.get('content'))
        # Reserve room for: (a) overhead of remaining non-last messages,
        # (b) the message-frame overhead of the last message itself,
        # (c) the truncation marker we'll prepend.  Previous bug: didn't
        # subtract (c), so the post-truncation message exceeded budget
        # by the marker length (~7 tokens) and the wire request still
        # tickled n_ctx.
        _TOKENS_PER_MSG = 4  # OpenAI envelope overhead per message
        marker_tokens = count_tokens_for_text(WIRE_TRIM_MARKER, model)
        overhead_tokens = (count_tokens_for_messages(messages[:-1], model)
                           + _TOKENS_PER_MSG
                           + marker_tokens)
        room_for_last = max(64, budget - overhead_tokens)
        # Use the same chars/token ratio the fallback uses (3.5).  When
        # tiktoken is available this is conservative; when it's the
        # active path, it's exact.  Either way we're cutting from the
        # left so over-cutting just means a slightly smaller payload.
        target_chars = int(room_for_last * 3.5)
        if len(last_text) > target_chars:
            n_truncated_chars = len(last_text) - target_chars
            new_text = WIRE_TRIM_MARKER + last_text[-target_chars:]
            if isinstance(last.get('content'), list):
                new_parts = []
                replaced = False
                for p in last['content']:
                    if isinstance(p, dict) and p.get('type') == 'text' and not replaced:
                        new_parts.append({**p, 'text': new_text})
                        replaced = True
                    else:
                        new_parts.append(p)
                if not replaced:
                    new_parts.insert(0, {'type': 'text', 'text': new_text})
                last['content'] = new_parts
            else:
                last['content'] = new_text
            messages[-1] = last

    new_body = dict(body)
    new_body['messages'] = messages
    return (new_body, n_dropped, n_truncated_chars,
            est_before, count_tokens_for_messages(messages, model), budget)


def _apply_trim_to_request(httpx_module, request, body: dict) -> tuple:
    """Trim ``body`` if over budget and mutate ``request`` so the wire
    bytes match.  Returns ``(maybe_new_body, was_trimmed)``.

    We mutate in-place (``_content`` + ``stream`` + ``content-length``)
    rather than rebuilding the Request — rebuilding would lose auth /
    cookies / extensions state that the caller has already attached.
    The 2026-05-12 ``LocalProtocolError`` incident (mentioned in the
    ``_annotate_request`` docstring) was caused by mutating ONLY
    ``_content`` and leaving ``stream`` pointing at the old buffer; we
    update both here.
    """
    trimmed, n_dropped, n_truncated, est_before, est_after, budget = \
        _trim_to_budget(body)
    if n_dropped == 0 and n_truncated == 0:
        return body, False

    try:
        new_bytes = json.dumps(trimmed).encode('utf-8')
        request._content = new_bytes
        try:
            request.stream = httpx_module.ByteStream(new_bytes)
        except Exception:
            # Older httpx may not expose ByteStream at top level; fall back
            # to the stream module path.  Either path covers httpx >=0.20.
            from httpx._content import ByteStream as _BS  # type: ignore
            request.stream = _BS(new_bytes)
        try:
            request.headers['content-length'] = str(len(new_bytes))
        except Exception:
            pass
        logger.warning(
            "[TRIM] left-trimmed %d msg(s) + %d char(s) — est tokens "
            "%d→%d, budget %d (n_ctx/%s slots, max_tokens=%s)",
            n_dropped, n_truncated, est_before, est_after, budget,
            os.environ.get('HEVOLVE_LLAMA_SLOTS', '1'),
            body.get('max_tokens') or body.get('max_completion_tokens') or 2048,
        )
        return trimmed, True
    except Exception as e:
        logger.warning("[TRIM] failed to apply trim, sending original: %s", e)
        return body, False


def _shape_body_for_log(body: dict) -> dict:
    """Apply the ``HEVOLVE_LLM_OUTBOUND_BODY`` policy."""
    mode = (os.environ.get('HEVOLVE_LLM_OUTBOUND_BODY', 'full')
            .lower())
    if mode == 'off':
        return {
            'model': body.get('model'),
            'n_messages': len(body.get('messages') or []),
            'n_tools': len(body.get('tools') or []),
        }
    if mode == 'trim':
        out = dict(body)
        msgs = out.get('messages') or []
        if len(msgs) > 4:
            out['messages'] = [
                msgs[0], msgs[1],
                {'role': 'collapsed',
                 'content': f'<{len(msgs) - 3} messages omitted>'},
                msgs[-1],
            ]
        return out
    return body  # 'full' (default)


def _ts() -> str:
    """ISO-ish timestamp with ms — matches frozen_debug column 0
    format so grep / log-correlation tools work without translation."""
    t = time.time()
    return (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))
            + f',{int((t % 1)*1000):03d}')


def log_outbound(body: dict, *,
                 response_status: Any = None,
                 latency_ms: Optional[float] = None,
                 source: Optional[str] = None) -> None:
    """Public hook for non-httpx callers (dispatcher's raw
    ``requests.post`` draft path).  Writes one JSONL record; never
    raises.

    ``source`` overrides whatever ``set_source`` / ``source_context``
    set on the thread-local context; pass it when the caller wants to
    label the call explicitly (e.g. ``dispatcher.draft``)."""
    try:
        record = {
            'ts': _ts(),
            'request_id': _get_request_id(),
            'source': source if source is not None else _get_source(),
            'body': _shape_body_for_log(body),
            'response_status': response_status,
            'latency_ms': latency_ms,
        }
        line = json.dumps(record, default=str, ensure_ascii=False) + '\n'
        with _file_lock:
            fh = _open_log_handle()
            fh.write(line)
    except Exception as e:
        logger.debug("log_outbound failed: %s", e)


def _annotate_request(request, body):
    """Stamp ``X-HARTOS-Source`` + ``X-HARTOS-Request-ID`` headers on
    the outgoing request so a future proxy / log scrape can recover
    origin without our JSONL.  llama.cpp ignores unknown headers —
    confirmed by Explore audit.

    We do NOT mutate the request body.  Earlier this function injected
    a ``user`` field into the JSON body, but in httpx the body lives
    in ``request.stream`` (an iterator) in addition to
    ``request._content`` (a buffer).  Rewriting only ``_content`` left
    ``stream`` pointing at the old bytes, the Content-Length stopped
    matching, and httpx raised ``LocalProtocolError`` on send.  Live
    evidence 2026-05-12 16:48: 96/98 outbound chat-completion calls
    failed this way until the body-rewrite was removed.  The
    request_id + source still flow through (a) the headers stamped
    below, and (b) the JSONL record written by ``log_outbound`` — so
    traceability is preserved with zero risk to the wire request.
    Header stamping is wrapped in try/except so a future httpx that
    makes ``request.headers`` read-only at send-time degrades to
    log-only instead of breaking the call."""
    rid = _get_request_id()
    src = _get_source()
    try:
        if rid:
            request.headers['X-HARTOS-Request-ID'] = rid
        if src:
            request.headers['X-HARTOS-Source'] = src
    except Exception as e:
        logger.debug("[outbound] header-stamp failed: %s", e)


# ─── Foreground preemption of autonomous-background LLM calls ─────────
# Every :8082 chat-completion already carries its caller identity in the
# X-HARTOS-Request-ID header (stamped by _annotate_request).  Autonomous daemon
# goal dispatches use request_id 'daemon_<goal_id>' (dispatch.py:_daemon_request
# _id); genuine user turns do not.  We reuse the CANONICAL discriminator
# (dispatch.is_genuine_user_request) so a daemon call (a) yields the local model
# to a live user turn before contending and (b) runs on the closable background
# client so it can be aborted mid-flight when the user hits enter.  The user's
# own turn is never reclassified, never delayed, never cancelled.


def _bg_yield_wait_s() -> float:
    """Max seconds a background LLM call waits for a live user turn to finish
    before proceeding (it can still be aborted mid-flight afterwards).  Tunable
    via ``HEVOLVE_BG_YIELD_WAIT_S``; default 20s — covers a typical chat turn,
    short enough that background work isn't starved if a session stays open."""
    try:
        return max(0.0, float(os.environ.get('HEVOLVE_BG_YIELD_WAIT_S', '20')))
    except Exception:
        return 20.0


def _is_background_call(request) -> bool:
    """True iff this llama call belongs to an autonomous background daemon agent
    (request_id ``daemon_*``) rather than a genuine user turn.

    Reads request_id from the ``X-HARTOS-Request-ID`` header stamped by
    ``_annotate_request`` (so it travels with the request even across threads),
    with a thread-local fallback, then applies the canonical
    ``dispatch.is_genuine_user_request`` rule — single source of truth, no
    duplicated prefix logic.  Conservative: ANY uncertainty returns False, so a
    call is treated as cancellable background work only when we are sure; a user
    turn is never cancelled."""
    try:
        rid = None
        try:
            rid = request.headers.get('X-HARTOS-Request-ID')
        except Exception:
            rid = None
        if not rid:
            rid = _get_request_id()
        if not rid:
            return False
        from integrations.agent_engine.dispatch import is_genuine_user_request
        return not is_genuine_user_request(rid)
    except Exception:
        return False


def _select_send_client(self, request):
    """Choose which httpx client executes this :8082 send.

    Autonomous daemon calls get the closable background client AFTER yielding to
    any in-flight user turn; everything else gets the caller's own client,
    unchanged.  Fully fenced — any failure falls back to the original client, so
    the foreground path can never break."""
    try:
        if not _is_background_call(request):
            return self
        from core.foreground import foreground_active, wait_until_clear
        if foreground_active():
            # A user is being served right now — yield the model to them first.
            wait_until_clear(_bg_yield_wait_s())
        from core.http_pool import get_bg_llm_http_client
        return get_bg_llm_http_client() or self
    except Exception:
        return self


def _install_sync_patch(httpx_module) -> None:
    _orig_send = httpx_module.Client.send

    def _patched_send(self, request, **kwargs):
        if not _is_target_request(request.url, request.method):
            return _orig_send(self, request, **kwargs)
        try:
            body_bytes = bytes(request.content or b'')
            body = json.loads(body_bytes.decode('utf-8')) if body_bytes else None
        except Exception:
            body = None
        if isinstance(body, dict):
            # Trim BEFORE annotating headers so content-length matches.
            body, _ = _apply_trim_to_request(httpx_module, request, body)
            _annotate_request(request, body)
        # Route autonomous-daemon calls through the closable background client
        # (after yielding to any live user turn); user turns stay on `self`.
        # _annotate_request ran above, so the X-HARTOS-Request-ID header the
        # discriminator reads is already set.
        send_client = _select_send_client(self, request)
        start = time.time()
        try:
            response = _orig_send(send_client, request, **kwargs)
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {},
                         response_status=getattr(response, 'status_code', None),
                         latency_ms=round(elapsed, 1))
            return response
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {}, source=(_get_source() or 'httpx-exc'),
                         response_status=type(e).__name__,
                         latency_ms=round(elapsed, 1))
            raise

    httpx_module.Client.send = _patched_send


def _install_async_patch(httpx_module) -> None:
    """Async path — openai's AsyncOpenAI / langchain's async invokes
    go through ``AsyncClient.send``.  Mirrors the sync patch."""
    if not hasattr(httpx_module, 'AsyncClient'):
        return
    _orig = httpx_module.AsyncClient.send

    async def _patched(self, request, **kwargs):
        if not _is_target_request(request.url, request.method):
            return await _orig(self, request, **kwargs)
        try:
            body_bytes = bytes(request.content or b'')
            body = json.loads(body_bytes.decode('utf-8')) if body_bytes else None
        except Exception:
            body = None
        if isinstance(body, dict):
            # Trim BEFORE annotating headers so content-length matches.
            body, _ = _apply_trim_to_request(httpx_module, request, body)
            _annotate_request(request, body)
        start = time.time()
        try:
            response = await _orig(self, request, **kwargs)
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {},
                         response_status=getattr(response, 'status_code', None),
                         latency_ms=round(elapsed, 1))
            return response
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {}, source=(_get_source() or 'httpx-async-exc'),
                         response_status=type(e).__name__,
                         latency_ms=round(elapsed, 1))
            raise

    httpx_module.AsyncClient.send = _patched


def install() -> bool:
    """Idempotently install the httpx Client + AsyncClient patches.
    Returns True on first install, False otherwise.

    Kill switch: set ``HEVOLVE_LLM_OUTBOUND_DISABLE=1`` (or any
    non-empty / non-'0' value) to skip installation entirely.
    Added 2026-05-12 after the body-rewrite regression that broke
    96/98 autogen calls — a deployed bundle should always have an
    env-var rollback, not require a rebuild to mitigate a bad patch.
    """
    global _installed
    _disable = os.environ.get('HEVOLVE_LLM_OUTBOUND_DISABLE', '0').strip().lower()
    if _disable and _disable != '0' and _disable != 'false':
        logger.info(
            "[outbound-hook] disabled via HEVOLVE_LLM_OUTBOUND_DISABLE=%r",
            _disable)
        return False
    with _install_lock:
        if _installed:
            return False
        try:
            import httpx
        except ImportError:
            logger.debug("httpx not importable, skipping install")
            return False
        try:
            _install_sync_patch(httpx)
            _install_async_patch(httpx)
        except Exception as e:
            logger.warning("[outbound-hook] install failed: %s", e)
            return False
        _installed = True
        logger.info(
            "[outbound-hook] httpx (sync+async) patched — every POST to "
            "ports %s path %s will be logged to %s",
            sorted(_target_ports()), _TARGET_PATH, _get_log_path(),
        )
        return True


def is_installed() -> bool:
    return _installed

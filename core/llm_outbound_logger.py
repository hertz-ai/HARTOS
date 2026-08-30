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
    """Best-effort request_id pull for the outbound correlation key AND the
    daemon-vs-user discriminator.  Empty string when neither source carries one.

    Two sources, in priority order:

      1. The thread-local ``ThreadLocalData.get_request_id()`` — set
         authoritatively by the /chat handler on the request thread
         (hart_intelligence_entry.py:6898 / 7962).  Kept FIRST so a genuine
         user turn's id is never shadowed.  (A previous ``getattr(_tl,
         'request_id')`` read the INSTANCE attribute, which is never set, so the
         header + JSONL key were always empty; the canonical accessor fixed it.)
      2. The ``_request_id_var`` contextvar fallback — the daemon path enters
         via ``hevolve_chat`` (routes.hartos_backend_adapter.chat) ->
         ``recipe`` / ``chat_agent`` on a worker thread/context the handler's
         thread-local never reached (``threadlocal`` uses ``threading.local()``,
         which does not cross the autogen worker boundary).
         ``with_llm_context`` binds the id there, and — exactly like the
         ``source`` contextvar — it DOES survive into the httpx send.  Without
         this fallback ~94% of daemon autogen calls logged request_id='' and so
         bypassed the foreground yield/abort entirely (llm_outbound.jsonl,
         2026-06-14): is_genuine_user_request('') is True, so the call was never
         routed to the closable background client and never released the single
         llama slot to a live user turn."""
    try:
        from threadlocal import thread_local_data as _tl
        rid = _tl.get_request_id()
        if rid:
            return str(rid)
    except Exception:
        pass
    try:
        rid = _request_id_var.get()
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

# Request-id contextvar — the propagation twin of ``_source_var`` above.  The
# daemon stamps 'daemon_<goal>' on its dispatch thread, but autogen issues its
# httpx send on a worker thread/context that a ``threading.local()`` cannot
# reach, so the tag was lost for ~94% of autogen calls and the foreground
# preempt could not see them as background.  A contextvar survives that boundary
# exactly the way the source label already does.  ``with_llm_context`` binds it;
# ``_get_request_id`` reads it as the fallback after the thread-local.
_request_id_var: 'contextvars.ContextVar[str]' = contextvars.ContextVar(
    'llm_outbound_request_id', default='')


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


@contextlib.contextmanager
def request_id_context(request_id: str):
    """Bind the request_id for LLM calls issued from this context — the
    contextvar twin of ``source_context``.  Restores the prior value on exit
    (even on exception) so a reused worker thread never leaks one request's id
    into the next.  ``_get_request_id`` reads it as a fallback after the
    thread-local."""
    token = _request_id_var.set(str(request_id or ''))
    try:
        yield
    finally:
        _request_id_var.reset(token)


def with_llm_context(source_name: str, request_id_arg: str = 'request_id'):
    """Decorator for the autogen entry points (``create_recipe.recipe`` /
    ``reuse_recipe.chat_agent``): set the outbound ``source`` label AND
    propagate the decorated function's ``request_id`` argument into
    ``_request_id_var`` so the daemon-vs-user discriminator survives the autogen
    worker-thread boundary the thread-local cannot cross.

    Why here and not ``set_request_id`` upstream: the daemon enters via
    ``hevolve_chat`` (routes.hartos_backend_adapter.chat), which bypasses the
    /chat handler that sets the thread-local — and even on the user path
    ``recipe`` runs on a worker thread.  This is the one place that (a) has the
    real ``request_id`` in hand and (b) wraps the whole autogen call, so the
    contextvar reaches the httpx send exactly like ``source``.

    Binds the id BY NAME via the signature, so it is robust to positional or
    keyword call sites.  Supersedes a bare ``with_source`` on those two
    functions; every other caller keeps using ``source_context`` /
    ``with_source`` unchanged."""
    import functools
    import inspect

    def _deco(fn):
        try:
            _sig = inspect.signature(fn)
        except (ValueError, TypeError):
            _sig = None

        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            rid = ''
            if _sig is not None:
                try:
                    bound = _sig.bind_partial(*args, **kwargs)
                    rid = bound.arguments.get(request_id_arg) or ''
                except (TypeError, KeyError):
                    rid = ''
            if not rid:
                # #162 diagnostic — an LLM entry point (recipe / chat_agent) bound
                # with NO request_id means every autogen call it issues will log
                # request_id='' and (pre-385504a) bypass the foreground abort. Log
                # WHO + whether the thread-local still has it, so the next build's
                # frozen_debug disambiguates the loss point that static analysis
                # cannot: a present thread_local_rid ⇒ the *caller* didn't thread
                # the arg (fix upstream at the recipe()/chat_agent call site); an
                # absent one ⇒ the daemon_/user tag was already gone before this
                # frame (fix at /chat handler ↔ payload). Low-frequency (once per
                # goal/turn, not per token), so INFO is safe.
                _tl_rid = ''
                try:
                    import threading as _t
                    from threadlocal import thread_local_data as _tl
                    _tl_rid = _tl.get_request_id() or ''
                    logger.info(
                        "LLM-CONTEXT empty request_id at %s (source=%s, thread=%s, "
                        "thread_local_rid=%r) — rid not threaded to this frame (#162)",
                        getattr(fn, '__name__', '?'), source_name,
                        _t.current_thread().name, _tl_rid)
                except Exception:
                    pass
                # #162 fix: the decorated arg didn't carry a rid, but DON'T
                # clobber an inherited one with ''.  The worker thread may hold
                # the originating rid in the thread-local (re-bound at the
                # speculative dispatcher's expert-task entry) or in a
                # propagated contextvar.  Binding that keeps the user's own
                # autogen turn FOREGROUND instead of background-and-preempted.
                if not rid:
                    try:
                        rid = _tl_rid or _request_id_var.get() or ''
                    except Exception:
                        rid = _tl_rid or ''
            with source_context(source_name), request_id_context(rid):
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


# PERF-2 (audit): this writer reached ~196MB — unbounded append + buffering=1
# (a flush syscall per line).  Bound it through ONE canonical rotation point
# (no parallel rotation path).  We deliberately KEEP the full request body — the
# forensic value the live diagnosis flow relies on — and only (a) cap the file
# and (b) drop the per-line flush.  Recent forensics survive in the live file +
# one .old backup (~2x cap).  Override the cap with HEVOLVE_LLM_OUTBOUND_MAX_MB.
def _max_outbound_log_bytes() -> int:
    try:
        mb = int(os.environ.get('HEVOLVE_LLM_OUTBOUND_MAX_MB', '') or 20)
    except ValueError:
        mb = 20
    return max(1, mb) * 1024 * 1024


def _rotate_if_oversized(path: str, max_bytes: int | None = None) -> bool:
    """Rename ``path`` → ``path + '.old'`` when it exceeds ``max_bytes``.

    Best-effort, never raises; one backup generation (prior .old overwritten).
    Returns True iff a rotation happened.  SOLE rotation impl for this writer —
    callers must not re-implement it (DRY / no parallel path)."""
    if max_bytes is None:
        max_bytes = _max_outbound_log_bytes()
    try:
        if os.path.getsize(path) <= max_bytes:
            return False
    except OSError:
        return False  # missing / unstatable → nothing to rotate
    try:
        os.replace(path, path + '.old')
        return True
    except OSError:
        return False


def _close_handle() -> None:
    """Close + drop the cached handle so the next ``_open_log_handle`` reopens
    (and rotates via ``_rotate_if_oversized`` there if oversized)."""
    global _file_handle
    try:
        if _file_handle is not None and not getattr(_file_handle, 'closed', True):
            _file_handle.close()
    except OSError:
        pass
    _file_handle = None


def _open_log_handle():
    global _file_handle
    if _file_handle is None or getattr(_file_handle, 'closed', True):
        path = _get_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate_if_oversized(path)  # PERF-2: bound before (re)open
        # Default buffering (was buffering=1 → flush per line).  A post-hoc
        # forensic log has no live readers, so per-line durability is wasted
        # syscalls; the process-exit close + OS flush preserve the tail.
        _file_handle = open(path, 'a', encoding='utf-8')
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


def _schema_tokens(body: dict, model=None) -> int:
    """Prompt-token cost of EVERY schema block on the request.

    llama-server bills the serialised schema exactly like message content.
    autogen sends BOTH ``functions`` (legacy OpenAI) and ``tools``; only
    ``tools`` was ever charged, so ``functions`` was free budget that did not
    exist.  Measured on the 2026-08-29 400: functions = 5 entries / 2,918
    chars (~729 tok) alongside tools = 70 entries (~10,181 tok).

    ONE canonical accessor so the budget maths and the post-trim acceptance
    test cannot drift apart — they are the same number by construction.
    """
    from core.token_utils import count_tokens_for_text
    total = 0
    for key in ('tools', 'functions'):
        block = body.get(key)
        if not block:
            continue
        try:
            total += count_tokens_for_text(
                json.dumps(block, ensure_ascii=False), model)
        except (TypeError, ValueError):
            # A non-serialisable block is not ours to fix, but pretending it
            # costs zero is how this bug shipped. Charge from its repr.
            logger.warning(
                "wire-trim: %s block is not JSON-serialisable; charging an "
                "approximate cost so the budget is not silently overstated",
                key)
            total += count_tokens_for_text(repr(block), model)
    return total


def _truncate_msg_content(msg: dict, target_chars: int, marker: str,
                          content_to_text) -> tuple:
    """Left-truncate one message's content to ``target_chars``, marker-prefixed.

    Returns ``(new_msg, n_cut_chars)`` — ``(msg, 0)`` when it already fits.
    Multimodal-aware: rebuilds list-shaped content preserving image parts.
    The ONE truncation implementation; both the last-message step and the
    anchor step in ``_trim_to_budget`` call it.
    """
    text = content_to_text(msg.get('content'))
    if len(text) <= target_chars:
        return msg, 0
    new_msg = dict(msg)
    new_text = marker + text[-target_chars:]
    if isinstance(new_msg.get('content'), list):
        new_parts = []
        replaced = False
        for p in new_msg['content']:
            if isinstance(p, dict) and p.get('type') == 'text' and not replaced:
                new_parts.append({**p, 'text': new_text})
                replaced = True
            else:
                new_parts.append(p)
        if not replaced:
            new_parts.insert(0, {'type': 'text', 'text': new_text})
        new_msg['content'] = new_parts
    else:
        new_msg['content'] = new_text
    return new_msg, len(text) - target_chars


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
    if 'max_tokens' not in body and 'max_completion_tokens' not in body:
        # The budget below RESERVES max_tokens out of the slot, but when the
        # producer omits the field llama-server generates unbounded and the
        # reservation is a lie.  Measured 2026-08-30 (llama rel 11.36-11.40,
        # installed build): an autogen.reuse call with no max_tokens reached
        # n_decoded=3,975, hit its 6,144-token slot ceiling AND exhausted the
        # shared batch memory, collateral-failing a concurrent request whose
        # 3,039 real tokens fit comfortably (#734).  Pin the SAME default the
        # budget math just used, so the wire enforces what it accounts for.
        body = dict(body)
        body['max_tokens'] = max_tokens

    # ─── The tool schema occupies n_ctx too — count it or the budget is a lie ───
    # llama-server bills prompt tokens for the SERIALISED TOOL SCHEMA exactly like
    # message content, but this function only ever walked body['messages'], so the
    # single largest consumer was invisible to the "zero-tolerance context overflow"
    # guard. Measured 2026-08-07 over 1,407 real requests: the 29 that carried a
    # tools block carried 67 tools ≈ 10,713 tokens — 2.6x an entire 4096 window and
    # 87% of a 12288 one — and ALL 29 overflowed. The guard reported them as fitting.
    #
    # CLAUDE.md records why nothing upstream caught it either: autogen attaches
    # system_message + tools AFTER transform_messages runs, so the frozen_debug
    # "FULL INPUT MESSAGES DEBUG" dump is a messages-only view. The wire layer is the
    # ONLY place the tools block is observable before it hits the socket, which makes
    # counting it here not an optimisation but the whole point of the layer.
    tools_tokens = _schema_tokens(body, model)

    budget = (_get_budget_per_slot() - max_tokens
              - WIRE_TRIM_SAFETY_MARGIN_TOKENS - tools_tokens)
    if budget <= 0:
        # max_tokens (and/or the tool schema) alone exceeds n_ctx — degrade
        # gracefully so we still send SOMETHING instead of 500-failing.
        #
        # Say so LOUDLY when the tools block is the cause: trimming messages cannot
        # recover a schema that does not fit, so a quiet degrade here means the
        # request goes out over-length and llama-server rejects it anyway. The fix
        # is to prune the tool list for the persona, and an operator can only know
        # that if this line names the cost.
        if tools_tokens and tools_tokens >= _get_budget_per_slot() // 2:
            logger.error(
                "wire-trim: the TOOL SCHEMA alone is %d tokens against an n_ctx of "
                "%d (%d tool(s)) — no amount of message trimming can make this fit. "
                "Prune the tool list for this agent; the request will be rejected "
                "as over-length.",
                tools_tokens, _get_budget_per_slot(), len(body.get('tools') or []))
        budget = max(512, _get_budget_per_slot() // 4)

    est_before = count_tokens_for_messages(messages, model)
    if est_before <= budget:
        return body, 0, 0, est_before, est_before, budget

    has_system = bool(messages and isinstance(messages[0], dict)
                      and messages[0].get('role') == 'system')
    # The newest user message is load-bearing: llama.cpp's Qwen3.5 chat
    # template raises "No user query found in messages." whenever a
    # role='tool' message survives with no user message anywhere, and the
    # server turns that into HTTP 500 (measured 3x on 2026-08-30, source
    # autogen.reuse — post-trim roles were [system, assistant, assistant,
    # tool, assistant]).  Left-dropping by position deleted it first,
    # because the user's task instruction is the OLDEST non-system message.
    anchor = next((m for m in reversed(messages)
                   if isinstance(m, dict) and m.get('role') == 'user'), None)
    n_dropped = 0
    floor = 2 if has_system else 1
    while len(messages) > floor:
        drop_idx = 1 if has_system else 0
        if messages[drop_idx] is anchor:
            if len(messages) <= floor + 1:
                break  # only system + anchor + newest remain
            drop_idx += 1
        messages.pop(drop_idx)
        n_dropped += 1
        if count_tokens_for_messages(messages, model) <= budget:
            break

    # (b)+(c) reserves below: the message-frame overhead of the message being
    # truncated, and the truncation marker we'll prepend.  Previous bug:
    # didn't subtract (c), so the post-truncation message exceeded budget by
    # the marker length (~7 tokens) and the wire request still tickled n_ctx.
    _TOKENS_PER_MSG = 4  # OpenAI envelope overhead per message
    marker_tokens = count_tokens_for_text(WIRE_TRIM_MARKER, model)

    n_truncated_chars = 0
    if count_tokens_for_messages(messages, model) > budget and messages:
        overhead_tokens = (count_tokens_for_messages(messages[:-1], model)
                           + _TOKENS_PER_MSG
                           + marker_tokens)
        room_for_last = max(64, budget - overhead_tokens)
        # Use the same chars/token ratio the fallback uses (3.5).  When
        # tiktoken is available this is conservative; when it's the
        # active path, it's exact.  Either way we're cutting from the
        # left so over-cutting just means a slightly smaller payload.
        target_chars = int(room_for_last * 3.5)
        new_last, n_cut = _truncate_msg_content(
            messages[-1], target_chars, WIRE_TRIM_MARKER, _content_to_text)
        if n_cut:
            n_truncated_chars += n_cut
            messages[-1] = new_last

    # The anchor (newest user message) is drop-protected, so when IT is the
    # oversized component the step above never touches it: it only truncates
    # messages[-1], and in the autogen.reuse conversations the anchor sits
    # mid-list behind assistant/tool replies.  Measured 2026-08-30 19:35-19:46
    # on the installed build: system+anchor ~5597 tok against budget 3840 —
    # every trim ended in the STILL-over error below and llama-server rejected
    # the turn, 95x in 11 minutes.  Same policy, same helper, applied to the
    # anchor.
    if (count_tokens_for_messages(messages, model) > budget
            and anchor is not None and anchor in messages
            and messages[-1] is not anchor):
        a_idx = messages.index(anchor)
        others = messages[:a_idx] + messages[a_idx + 1:]
        overhead_tokens = (count_tokens_for_messages(others, model)
                           + _TOKENS_PER_MSG
                           + marker_tokens)
        room_for_anchor = max(64, budget - overhead_tokens)
        target_chars = int(room_for_anchor * 3.5)
        new_anchor, n_cut = _truncate_msg_content(
            anchor, target_chars, WIRE_TRIM_MARKER, _content_to_text)
        if n_cut:
            n_truncated_chars += n_cut
            messages[a_idx] = new_anchor

    # LAST resort: the SYSTEM message itself.  autogen.reuse builds its
    # system prompt as persona boilerplate + the whole serialized recipe —
    # measured 2026-08-30 20:06-20:20 on the installed build: 86 of 100
    # STILL-over failures were this shape (sample: [system 28,154 chars,
    # assistant 247]), each sent doomed and rejected by llama-server.  When
    # drops + last + anchor have all run and the set is STILL over, the
    # system message is the only mass left; left-truncating it cuts the
    # boilerplate head and keeps the actionable recipe tail.  Only reached
    # when the alternative is a guaranteed reject.
    if (count_tokens_for_messages(messages, model) > budget
            and has_system and len(messages) >= 1):
        others = messages[1:]
        overhead_tokens = (count_tokens_for_messages(others, model)
                           + _TOKENS_PER_MSG + marker_tokens)
        room_for_system = max(64, budget - overhead_tokens)
        target_chars = int(room_for_system * 3.5)
        new_sys, n_cut = _truncate_msg_content(
            messages[0], target_chars, WIRE_TRIM_MARKER, _content_to_text)
        if n_cut:
            n_truncated_chars += n_cut
            messages[0] = new_sys

    # ─── Post-trim acceptance test — the trim is best-effort, so CHECK it ───
    # Trimming can be structurally unable to reach the budget: it drops and
    # truncates the LAST message, which cannot shrink the SYSTEM message.  On
    # 2026-08-29 that produced est 795 against a budget of 351 — over by 2.3x —
    # and the request was sent anyway because `we truncated something` was
    # treated as success.  llama-server then rejected it (11,236 > n_ctx 8192).
    # Say so here: a silent doomed request costs a full round trip and surfaces
    # to the user as an unexplained failure (see #591 for the caller side).
    _est_after = count_tokens_for_messages(messages, model)
    _wire_total = _est_after + tools_tokens
    _per_slot = _get_budget_per_slot()
    if _est_after > budget or _wire_total > _per_slot:
        logger.error(
            "[TRIM] trim could not reach budget — request is STILL over and "
            "will very likely be rejected: messages %d tok + schema %d tok = "
            "%d tok against n_ctx %d (budget was %d, %d msg(s) dropped, %d "
            "char(s) truncated). Trimming cannot shrink the system message; "
            "the oversized component is %s.",
            _est_after, tools_tokens, _wire_total, _per_slot, budget,
            n_dropped, n_truncated_chars,
            'the tool/function schema' if tools_tokens > _est_after
            else 'the message content')

    new_body = dict(body)
    new_body['messages'] = messages
    return (new_body, n_dropped, n_truncated_chars,
            est_before, _est_after, budget)


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
            # PERF-2: bound WITHIN a long session too (handle is opened once and
            # reused, so a pre-open-only guard wouldn't help a multi-hour desktop
            # run).  tell() is the cheap in-stream position (no extra stat).  At
            # the cap, close so the NEXT call rotates + reopens — reusing the one
            # _rotate_if_oversized in _open_log_handle (no parallel rotation).
            try:
                if fh.tell() >= _max_outbound_log_bytes():
                    _close_handle()
            except OSError:
                pass
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
    """True iff this llama call is autonomous background daemon work (request_id
    ``daemon_*`` — or, per the accepted contract, ABSENT) rather than a genuine
    user turn.

    Reads request_id from the ``X-HARTOS-Request-ID`` header stamped by
    ``_annotate_request`` (so it travels with the request across the autogen
    worker-thread boundary), with a thread-local fallback, then DELEGATES the
    decision ENTIRELY to the one canonical ``dispatch.is_genuine_user_request``
    — the SAME rule the inbound foreground gate (``_chat_request_is_genuine``)
    applies, so the two can never diverge.  No bespoke, per-caller "is user"
    logic here.

    Empty / missing rid → ``is_genuine_user_request`` returns False → background
    (abortable).  A real /chat always carries an id (the frontend sends one; the
    adapter defaults a timestamp), so an UNtagged llama call is daemon work whose
    ``daemon_`` tag was lost crossing into the autogen worker thread.  The prior
    bespoke ``if not rid: return False`` here classified those as FOREGROUND,
    which is exactly what left the daemon's empty-rid 4B calls on the
    non-closable client so a user's "hi" could never preempt them (#162); it also
    silently contradicted ``is_genuine_user_request``'s empty→background rule."""
    try:
        rid = None
        try:
            rid = request.headers.get('X-HARTOS-Request-ID')
        except Exception:
            rid = None
        if not rid:
            rid = _get_request_id()
        from integrations.agent_engine.dispatch import is_genuine_user_request
        return not is_genuine_user_request(rid)
    except Exception:
        return False


def _select_send_client(self, request):
    """Choose which httpx client executes this send.

    Autonomous daemon calls get the CLOSABLE background client so the scheduler's
    preempt (``close_bg_llm_http_client``) can abort them; everything else gets
    the caller's own client, unchanged.  The yield / priority / preempt is now
    owned by ``core.llama_scheduler`` (acquired in ``_patched_send``) — the SAME
    queue the requests/pooled_post path uses — NOT here; this only picks the
    abortable transport for a daemon call.  Fully fenced — any failure falls back
    to the original client, so the foreground path can never break."""
    try:
        if not _is_background_call(request):
            return self
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
        # Admit through the slot-aware priority scheduler — the SAME queue the
        # requests/pooled_post path uses — so this httpx (autogen/langchain/openai)
        # call is slot-aware: a user turn arriving to a full server preempts an
        # in-flight daemon; a daemon yields for a slot.  Fail-open to a no-op
        # context if the scheduler is unavailable, so the send can never be
        # blocked on a scheduler import error.
        try:
            from core.http_pool import close_bg_llm_http_client
            from core.llama_scheduler import get_scheduler
            _kind = 'daemon' if _is_background_call(request) else 'user'
            _slot_cm = get_scheduler().slot(_get_request_id(), _kind,
                                            cancel_fn=close_bg_llm_http_client,
                                            timeout=120.0)
        except Exception:
            _slot_cm = contextlib.nullcontext()
        start = time.time()
        try:
            with _slot_cm:
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


def _install_urllib_patch(urllib_request_module) -> None:
    """Patch ``urllib.request.urlopen`` — the THIRD transport that reaches
    llama-server, and the one that was escaping the gate entirely.

    Why this exists (measured live 2026-08-11): hevolveai's distillation engine
    calls llama-server from
    ``hevolveai/embodied_ai/models/qwen_llamacpp_wrapper.py:301`` via
    ``urllib.request.urlopen``.  That is neither httpx nor requests, so its
    traffic was BOTH invisible to ``llm_outbound.jsonl`` and unscheduled:
    1,166 records carried only ``autogen.create`` / ``dispatcher.draft`` /
    ``autogen.gather`` while 191 synthetic distillation queries had been
    generated and served.  Unscheduled calls consume real llama-server slots
    OUTSIDE ``core.llama_scheduler``'s accounting, so "in-flight <= --parallel"
    was unenforceable no matter how correct the scheduler itself is
    (``/props`` reported ``total_slots = 2``).

    The interception CRITERION is shared, not duplicated: ``_is_target_request``
    is duck-typed on ``.port``/``.path`` and ``urllib.parse.urlsplit`` satisfies
    both, so there is exactly ONE notion of "is this an LLM call".
    Classification likewise reuses ``_is_background_call`` — it already falls
    back to the request-id contextvar when the object has no ``.headers``, so no
    per-transport "is user" rule is introduced.

    Two deliberate scope limits, both pinned in
    ``tests/unit/test_urllib_outbound_gating.py``:

    * **No left-trim.**  ``_apply_trim_to_request`` drives httpx internals and
      is not reusable for a urllib ``Request``.
    * **No cancel_fn.**  ``close_bg_llm_http_client`` closes the httpx
      background client; handing it to a urllib admission would preempt an
      UNRELATED call while leaving this socket running.  urllib daemon calls are
      therefore slot-bounded and yielding, but not mid-flight cancellable.
    """
    _orig_urlopen = urllib_request_module.urlopen

    def _patched_urlopen(url, data=None, *args, **kwargs):
        # Resolve target-ness defensively: ``url`` is either a str or a
        # Request, and ANY failure here must fall through to the untouched
        # call — a logging hook may never break an LLM request.
        try:
            from urllib.parse import urlsplit
            _is_req = hasattr(url, 'full_url')
            full_url = url.full_url if _is_req else url
            payload = url.data if _is_req else data
            try:
                method = url.get_method() if _is_req else None
            except Exception:
                method = None
            if not method:
                method = 'POST' if payload is not None else 'GET'
            target = _is_target_request(urlsplit(str(full_url)), method)
        except Exception:
            target = False
        if not target:
            return _orig_urlopen(url, data, *args, **kwargs)

        body = None
        try:
            if payload:
                body = json.loads(bytes(payload).decode('utf-8'))
        except Exception:
            body = None
        # Stamp X-HARTOS-* before classifying, mirroring the httpx path's order
        # so the discriminator reads the same header there and here.  A urllib
        # Request.headers is a plain dict, the same mutation _annotate_request
        # performs on an httpx request.
        if _is_req:
            _annotate_request(url, body)
        try:
            from core.llama_scheduler import get_scheduler
            _kind = 'daemon' if _is_background_call(url) else 'user'
            _slot_cm = get_scheduler().slot(_get_request_id(), _kind,
                                            cancel_fn=None, timeout=120.0)
        except Exception:
            _slot_cm = contextlib.nullcontext()
        start = time.time()
        try:
            with _slot_cm:
                response = _orig_urlopen(url, data, *args, **kwargs)
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {},
                         source=(_get_source() or 'urllib'),
                         response_status=getattr(response, 'status', None),
                         latency_ms=round(elapsed, 1))
            return response
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {}, source=(_get_source() or 'urllib-exc'),
                         response_status=type(e).__name__,
                         latency_ms=round(elapsed, 1))
            raise

    urllib_request_module.urlopen = _patched_urlopen


def install() -> bool:
    """Idempotently install the httpx Client + AsyncClient + urllib patches.
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
            # NOT a bail-out any more.  urllib is stdlib and is the transport
            # hevolveai's distillation engine uses, so a missing httpx must
            # never leave the gate fully open (it previously returned False and
            # patched nothing at all).
            httpx = None
            logger.debug("httpx not importable — patching urllib only")
        patched = []
        try:
            if httpx is not None:
                _install_sync_patch(httpx)
                _install_async_patch(httpx)
                patched.append('httpx (sync+async)')
            import urllib.request as _urllib_request
            _install_urllib_patch(_urllib_request)
            patched.append('urllib.request')
        except Exception as e:
            logger.warning("[outbound-hook] install failed: %s", e)
            return False
        _installed = True
        logger.info(
            "[outbound-hook] %s patched — every POST to ports %s path %s is "
            "logged to %s and admitted through the llama slot scheduler",
            ' + '.join(patched),
            sorted(_target_ports()), _TARGET_PATH, _get_log_path(),
        )
        return True


def is_installed() -> bool:
    return _installed

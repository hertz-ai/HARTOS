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

import copy as _copy
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
        from hartos.threadlocal import thread_local_data as _tl
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


# The user an LLM call acts for, bound by ``with_llm_context`` from the
# decorated entry point's ``user_id`` argument.  Read by _elision_scope only.
_user_id_var: 'contextvars.ContextVar[str]' = contextvars.ContextVar(
    'llm_outbound_user_id', default='')


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
                    from hartos.threadlocal import thread_local_data as _tl
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
            uid = ''
            if _sig is not None:
                try:
                    uid = str(_sig.bind_partial(*args, **kwargs)
                              .arguments.get('user_id') or '')
                except (TypeError, KeyError):
                    uid = ''
            # The user the elided-text store scopes a pointer to (see
            # _elision_scope): the same contextvar hop as the request id.
            _uid_token = _user_id_var.set(uid) if uid else None
            try:
                with source_context(source_name), request_id_context(rid):
                    return fn(*args, **kwargs)
            finally:
                if _uid_token is not None:
                    _user_id_var.reset(_uid_token)
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
    from core.platform_paths import get_log_dir
    return os.path.join(get_log_dir(), _LOG_FILENAME)


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


def _is_chat_completions_post(request) -> bool:
    """A POST to any /chat/completions endpoint, on ANY host — the LLM calls
    that attest provider standing (#106b b).  Broader than _is_target_request
    (which is the LOCAL llama-server only): a HOSTED provider on :443 is the
    402/401 source and must reach the breaker feed too.  Ends-with catches
    both /v1/chat/completions and Azure-style
    /openai/deployments/<d>/chat/completions."""
    try:
        if getattr(request, 'method', '') != 'POST':
            return False
        return str(getattr(request.url, 'path', '') or '').endswith('/chat/completions')
    except Exception:
        return False


def _feed_provider_breaker(url, status) -> None:
    """Record LLM provider standing per host from a chat/completions response
    (#106b b).  401/402/403 = account refusal (a failure); 2xx = success
    (clears/half-open-closes).  Every other status and all exceptions are
    ignored: a 429 is a rate limit not a standing refusal, and a transport
    error says nothing about the account.  This is the SOLE consumer that
    resolves the breaker's half-open probe, so it must run on the real wire
    response, never at an upstream pre-flight check."""
    try:
        if status is None:
            return
        from core.circuit_breaker import llm_provider_breaker, provider_host
        host = provider_host(str(url))
        if not host:
            return
        if status in (401, 402, 403):
            llm_provider_breaker.record_failure(host)
        elif 200 <= status < 300:
            llm_provider_breaker.record_success(host)
    except Exception:
        pass


def _send_and_feed(orig_send, client, request, kwargs):
    """Run a HOSTED (non-target) chat/completions send unchanged and feed the
    provider breaker with its real status.  Byte-transparent: the response
    object is returned as-is and an exception is re-raised bare (exceptions do
    not feed the breaker)."""
    response = orig_send(client, request, **kwargs)
    _feed_provider_breaker(request.url, getattr(response, 'status_code', None))
    return response


# ─── Hard trim to fit n_ctx (zero-tolerance context overflow) ───
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


def _live_ctx_geometry():
    """``(n_ctx, total_slots)`` as the RUNNING llama-server reports them.

    Returns None when the server cannot be read — caller falls back to the
    constant, i.e. exactly the pre-2026-09-11 behaviour.

    ``/props`` carries ``default_generation_settings.n_ctx`` and a top-level
    ``total_slots``.  The n_ctx there is the PER-SLOT ceiling: llama-server
    quotes the same number when it refuses an over-length body
    (``'n_ctx': 8192, 'n_prompt_tokens': 11817``), so it is what one request
    may spend, already partitioned.  Do not divide it again.

    Deliberately NOT memoised.  #818/D53 sized this from a 117-second-old
    VRAM memo read across a llama-server teardown, pinned 4096 for a whole
    session and that is why CREATE was dead; a TTL does not help because the
    stale read happens INSIDE the window.  llama-server respawns on VRAM-tier
    changes, model switches and watchdog restarts, and the budget has to
    follow it within the same process.  The cost is one loopback GET with a
    1.5s cap on a path that is already making a multi-second LLM call.
    """
    try:
        from core.port_registry import get_local_llm_url
        from core.http_pool import pooled_get
        base = get_local_llm_url().rstrip('/')
        if base.endswith('/v1'):
            base = base[:-3]
        resp = pooled_get(base.rstrip('/') + '/props', timeout=1.5)
        if getattr(resp, 'status_code', 0) != 200:
            return None
        props = resp.json()
        n_ctx = int((props.get('default_generation_settings') or {}).get('n_ctx') or 0)
        slots = max(1, int(props.get('total_slots') or 1))
        return (n_ctx, slots) if n_ctx > 0 else None
    except Exception:
        return None


def _get_budget_per_slot() -> int:
    """Per-slot input token budget — MEASURED from the server, not declared.

    Order:
      * ``HEVOLVE_LLAMA_CTX_SIZE`` — explicit operator override, still wins.
        (Divided by ``HEVOLVE_LLAMA_SLOTS`` because that constant is a TOTAL.)
      * the running llama-server's ``/props`` — the truth.
      * ``core.constants.LLAMA_CTX_SIZE_DEFAULT`` — last resort, server down.

    WHY THE PROBE EXISTS (live 2026-09-11, installed build).  This returned
    12288 while llama-server ran 8192, so every body was over-budgeted by
    4,096 tokens; the "zero-tolerance overflow" guard passed requests the
    server then refused with ``exceed_context_size_error``, and reuse logged
    ``robust completion-advance FAILED ... the pipeline did not advance`` for
    sessions ..._18163818525 and ..._1923323102 — the agents never reached
    their goals.

    WHY THE OVERRIDE DID NOT SAVE US — CORRECTED 2026-09-11, and the earlier
    claim in ba1daf05e's message ("nothing ever sets it") is WITHDRAWN as
    measurably wrong.  Nunba's ``llama/llama_config.py:1970`` DOES set
    ``HEVOLVE_LLAMA_CTX_SIZE`` (and ``HEVOLVE_LLAMA_SLOTS``), on the line
    immediately above the ``--ctx-size`` / ``--parallel`` flags it hands the
    server, so on the SPAWN path the env is authoritative by construction and
    this probe never runs.

    The hole is the OTHER path.  Nunba adopts an already-running llama-server
    on :8080 without a geometry identity check (#756), and that path never
    reaches the spawn code, so the env stays unwritten and
    ``LLAMA_CTX_SIZE_DEFAULT`` (12288) wins against whatever the adopted
    server is actually running.  That is the measured split in the historical
    logs — 226 wire-trim lines reporting n_ctx 8192 (spawned, env written)
    against 26 reporting 12288 (adopted, constant) — and it is exactly the
    shape of memory/feedback_declaration_is_not_a_guard.md: constants.py:71
    declares the env "must match the --ctx-size cmdline" with nothing
    enforcing it on every path.  Asking the server turns that declaration into
    a measurement on BOTH paths.
    """
    from core.constants import LLAMA_CTX_SIZE_DEFAULT, LLAMA_SLOTS_DEFAULT
    try:
        _override = os.environ.get('HEVOLVE_LLAMA_CTX_SIZE')
        if _override:
            slots = max(1, int(os.environ.get('HEVOLVE_LLAMA_SLOTS',
                                               str(LLAMA_SLOTS_DEFAULT))))
            return int(_override) // slots
        live = _live_ctx_geometry()
        if live:
            return live[0]
        slots = max(1, int(os.environ.get('HEVOLVE_LLAMA_SLOTS',
                                           str(LLAMA_SLOTS_DEFAULT))))
        return LLAMA_CTX_SIZE_DEFAULT // slots
    except Exception:
        return LLAMA_CTX_SIZE_DEFAULT


def _min_message_budget(per_slot: int) -> int:
    """Tokens the MESSAGES must always be left, whatever the schema costs.

    The floor the degrade branch of :func:`_trim_to_budget` already fell back
    to; named here so the two callers cannot drift.  The second caller is
    ``core.agent_tools.fit_schema_to_ctx``, which subtracts this from the live
    n_ctx to learn how much a tool schema may spend — so the wire's floor and
    the selection path's ceiling are the same number by construction.
    """
    return max(512, int(per_slot) // 4)


def schema_token_room() -> int:
    """Tokens a request's tool schema may spend against the LIVE n_ctx.

    ``_get_budget_per_slot()`` minus :func:`_min_message_budget`.  Public
    because the SELECTION path (core.agent_tools) has to ask it before it
    offers a tool set; everything about the answer — the live ``/props`` probe,
    the ``HEVOLVE_LLAMA_CTX_SIZE`` override, the constant backstop — stays here,
    where the wire already computes it.

    PROMPT-SIDE ONLY, and deliberately so.  ``max_tokens`` and
    ``WIRE_TRIM_SAFETY_MARGIN_TOKENS`` are NOT subtracted: llama-server's 400
    is ``n_prompt_tokens >= n_ctx``, and reserving the 2,048-token generation
    budget as well would put the room at 4096-2048-2816-1024 = -1792 and prune
    the 23-tool set that measurably works.  Measured 2026-09-22 against both
    populations at n_ctx 4096 (room 3072):

        23 tools = 2489 tok -> fits    (516 such bodies returned 200)
        50 tools = 5782 tok -> prune   ( 20 such bodies returned 400)
        60 tools = 7712 tok -> prune   ( 16 such bodies returned 400)

    Generation overrun is the OTHER failure and the trim still reserves for it.
    """
    per_slot = _get_budget_per_slot()
    return per_slot - _min_message_budget(per_slot)


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


def _compact_tool_schema(body: dict, model=None) -> tuple:
    """Strip serialisation boilerplate from ``tools`` — same tools, fewer tokens.

    Returns ``(body_or_new_body, tokens_saved)``; ``(body, 0)`` when there is
    nothing to gain, so the caller's identity check still detects "unchanged".

    WHY.  Measured 2026-09-11 on the 70-tool wire body that killed agent
    18163818525 (llm_outbound.jsonl 22:34:31, source autogen.reuse): the schema
    was 8,482 tokens against an n_ctx of 8,192, so the request could not fit
    even with ZERO message content.  Anatomy of those tokens:

        parameters    5090 (60.0%)   descriptions  1519 (17.9%)
        JSON overhead 1623 (19.1%)   names          250 ( 2.9%)

    The fat is not the prose.  It is what pydantic emits for every
    ``Optional[X] = None`` argument::

        "repo": {"anyOf": [{"type": "string"}, {"type": "null"}],
                 "default": null, "description": "repo"}

    Three redundancies in one property, removed here:

      * ``anyOf: [{type: X}, {type: null}]`` -> ``{type: X}``.  Optionality is
        ALREADY carried by the property's absence from ``required``; the null
        branch also widens llama.cpp's GBNF grammar for no gain.  Applied ONLY
        to non-required properties — where the argument IS required the caller
        must pass something and null may be that something, so collapsing
        there would narrow the contract rather than normalise its spelling.
      * ``"default": null`` -> dropped.  Redundant with not-required, and
        llama-server does not act on defaults.  A NON-null default is kept:
        recall_memory's ``mode: 'hybrid'`` is the only thing telling the model
        what happens if it omits the argument.
      * ``"description": "repo"`` -> dropped.  A description equal to its own
        key is zero information; these are an artefact of #542's signature
        synthesis, which fills the description from the parameter name when the
        source function carried no per-argument docstring.  A real description
        is never touched — #787 measured 53% of tool calls emitted with empty
        arguments, and per-argument prose is what fixes that.

    Measured recovery on that exact block: -427 / -185 / -237 = **-842 tok**
    (8482 -> 7640) with tools 70 -> 70, properties 131 -> 131 and required
    args 62 -> 62.  Lossless by construction: only the SPELLING of the schema
    changes, never which tools exist, which arguments they take, or which are
    mandatory.

    HONEST SCOPE.  842 tokens does not by itself make that body fit
    (7640 + 1868 messages = 9508 > 8192).  This is the lossless half; bounding
    the tool COUNT is a separate concern.  Do not read this function as
    "the oversized body now fits".

    WHY HERE.  The schema is generated by autogen/pydantic from Python
    signatures inside third-party code, so there is no producer-side seam.  The
    wire is where every framework's body converges and where ``_schema_tokens``
    already charges the schema, so compaction and budget arithmetic are the
    same number by construction and cannot drift apart.
    """
    block = body.get('tools')
    if not isinstance(block, list) or not block:
        return body, 0
    from core.token_utils import count_tokens_for_text

    def _tok(obj):
        try:
            return count_tokens_for_text(json.dumps(obj, ensure_ascii=False),
                                         model)
        except (TypeError, ValueError):
            return 0

    before = _tok(block)
    if not before:
        return body, 0
    try:
        new_block = _copy.deepcopy(block)
    except Exception:
        # Never fail an LLM call for a token optimisation.
        return body, 0

    changed = False
    for entry in new_block:
        if not isinstance(entry, dict):
            continue
        fn = entry.get('function')
        if not isinstance(fn, dict):
            continue
        params = fn.get('parameters')
        if not isinstance(params, dict):
            continue
        props = params.get('properties')
        if not isinstance(props, dict):
            continue
        req = params.get('required')
        required = set(req) if isinstance(req, list) else set()
        for pname, spec in props.items():
            if not isinstance(spec, dict):
                continue
            if pname not in required:
                branches = spec.get('anyOf')
                if isinstance(branches, list):
                    non_null = [b for b in branches
                                if isinstance(b, dict) and b.get('type') != 'null']
                    has_null = any(isinstance(b, dict) and b.get('type') == 'null'
                                   for b in branches)
                    if has_null and len(non_null) == 1:
                        spec.pop('anyOf', None)
                        spec.update(non_null[0])
                        changed = True
                if 'default' in spec and spec.get('default') is None:
                    spec.pop('default', None)
                    changed = True
            desc = spec.get('description')
            if (isinstance(desc, str)
                    and desc.strip().strip('.').lower().replace(' ', '_')
                    == str(pname).lower()):
                spec.pop('description', None)
                changed = True

    if not changed:
        return body, 0
    saved = before - _tok(new_block)
    if saved <= 0:
        return body, 0
    new_body = dict(body)
    new_body['tools'] = new_block
    return new_body, saved


def _truncate_msg_content(msg: dict, target_chars: int, marker: str,
                          content_to_text) -> tuple:
    """Cut the MIDDLE of one message's content: keep its head and its tail,
    ``target_chars`` together, with ``marker`` where the middle was.

    Never the head.  Live 2026-09-27 (installed build, REUSE probe
    liveprobe_reuse_1): a dispatch turn reads "Perform this action -> Action
    #1:... <the user's words> follow these steps: [...]", and the head cut
    this used to make removed the marker and the words and kept the steps
    (6 of 77 calls); the reply was off-topic.  A tool result's head is where
    its status and failure text sit, and a system message's head is the
    persona, so every kind of message keeps both ends.  The head gets the
    larger half -- and never less than :func:`must_keep_head`: a REUSE
    dispatch turn keeps its marker and the user's words whole, and only the
    steps after ``ACTION_STEPS_SEPARATOR`` are elided (review of 111c458b0,
    probed: a fixed half/half split cut the words to 250 chars).

    Returns ``(new_msg, n_cut_chars)`` — ``(msg, 0)`` when it already fits.
    Multimodal-aware: the text parts are replaced by ONE part holding the cut
    text (they were joined to measure it; keeping the later ones as well
    would send their text twice), image parts are kept.
    The ONE truncation implementation; ``_trim_to_budget`` calls it for both
    of its cuts: the pass over the messages the drop could not remove, and
    the system-message last resort.
    """
    text = content_to_text(msg.get('content'))
    if len(text) <= target_chars:
        return msg, 0
    new_msg = dict(msg)
    target_chars = max(0, target_chars)
    tail_chars = target_chars // 2
    head_chars = target_chars - tail_chars
    keep = must_keep_head(text, msg.get('role'))
    if keep > head_chars:
        # The words stay whole even past target_chars: a request over its
        # budget is reported by the caller; a request without the user's
        # words is off-topic (liveprobe_reuse_1).
        head_chars = keep
        tail_chars = max(0, target_chars - head_chars)
    if head_chars + tail_chars >= len(text):
        return msg, 0  # nothing left to elide without cutting the head
    if callable(marker):
        marker = marker(text)  # a pointer to this text (see elided_pointer)
    new_text = _middle_cut(text, head_chars, tail_chars, marker)
    if isinstance(new_msg.get('content'), list):
        new_parts = []
        replaced = False
        for p in new_msg['content']:
            if isinstance(p, dict) and p.get('type') == 'text':
                if not replaced:
                    new_parts.append({**p, 'text': new_text})
                    replaced = True
            else:
                new_parts.append(p)
        if not replaced:
            new_parts.insert(0, {'type': 'text', 'text': new_text})
        new_msg['content'] = new_parts
    else:
        new_msg['content'] = new_text
    return new_msg, len(text) - len(new_text) + len(marker)


# ─── Pointers to what the trim elides ───────────────────────────────────
# Owner direction (2026-09-27): "tool results shd be saved with pointers and
# whatever is trimmed needs pointers to memory"; "the pointer design shd be
# explicitly understood by the LLM ... and a pointer shd not influence the
# context".  Whatever the trim cuts out of a message, or drops as a tool
# result, is saved whole in the agent-data store under the ``elided``
# namespace (core.cache_loaders: the store behind get_data_by_key), and the
# wire carries ``[elided:<id> <n> chars of <kind>]`` in its place.  The id is
# the first 12 hex digits of the text's sha256, so the pointer stays well
# inside the trim's 64-token floor.  ``get_data_by_key(key="elided:<id>")``
# reads the original back a page at a time.  A pointer is metadata only: no
# instruction, no summary.  What it is and how to expand it is said once, in
# the system message of a body that carries one (ELIDED_POINTER_EXPLANATION).
ELIDED_NAMESPACE = 'elided'
ELIDED_KEY_PREFIX = 'elided:'
_ELIDED_POINTER_RE = None  # compiled on first use
_ELIDED_MAX_ITEMS = 5000        # items kept on disk; the oldest go first
_ELIDED_MAX_BYTES = 64 * 1024 * 1024  # and bytes kept; the oldest go first
_ELIDED_TTL_S = 24 * 3600       # a REUSE replay reads it within the day
_ELIDED_EVICT_EVERY = 50        # writes between eviction sweeps
_elided_writes = 0
_elided_evict_lock = threading.Lock()
_ELIDED_KINDS = {'tool': 'a tool result', 'user': 'a user turn',
                 'assistant': 'an assistant turn', 'system': 'the system prompt'}
_ELIDED_LISTED_DROPS = 5        # dropped tool results named in the explanation
# Pointers are sent only when the budget is at least this many times what
# the explanation costs (~400 tokens today); below it the explanation
# would crowd out the very text it points at, so plain markers go instead.
_ELIDED_MIN_BUDGET_MULTIPLE = 4

ELIDED_POINTER_EXPLANATION = (
    "\n\nSome messages below were shortened to fit. A mark like "
    "[elided:ID N chars of KIND] stands where text was removed: the mark is "
    "not the content and is not a result. Judge only what is shown. If you "
    "need the removed text and can call tools, call get_data_by_key with "
    "key=\"elided:ID\"; it returns the text a page at a time, and each page "
    "names the offset of the next.")


def elided_pointer(pointer_id: str, n_chars: int, kind: str) -> str:
    """The ONE pointer format: ``[elided:<id> <n> chars of <kind>]``."""
    return f'[{ELIDED_KEY_PREFIX}{pointer_id} {int(n_chars)} chars of {kind}]'


def parse_elided_pointers(text: str) -> list:
    """``[(pointer_id, n_chars, kind), ...]`` for every pointer in ``text`` --
    the parser of :func:`elided_pointer`'s format, defined beside it."""
    global _ELIDED_POINTER_RE
    if _ELIDED_POINTER_RE is None:
        import re as _re
        _ELIDED_POINTER_RE = _re.compile(
            r'\[' + _re.escape(ELIDED_KEY_PREFIX)
            + r'([0-9a-f]{12}) (\d+) chars of ([a-z ]+)\]')
    return [(m.group(1), int(m.group(2)), m.group(3))
            for m in _ELIDED_POINTER_RE.finditer(str(text or ''))]


def strip_elided_pointers(text, marker_line=False):
    """``text`` with every pointer removed, and the space it leaves closed.

    THE text-for-the-user step: every send of a model's text to a person
    calls it -- both branches of CREATE's and REUSE's send_message_to_user1,
    publish_agent_message, the /chat reply (_chat_reply), the hive expert's
    publish and the channel router (tests/unit/
    test_elided_pointer_never_reaches_the_user.py guards the list).  A model
    can copy a pointer from its context into its answer, and a pointer is
    never an answer (owner ruling, 2026-09-27).  Non-text is returned as it
    is.

    ``marker_line``: the wire's own use, where each pointer follows
    WIRE_TRIM_MARKER on a line of its own; the line's newline goes with it,
    so what is left is exactly the plain marker."""
    if not isinstance(text, str) or ELIDED_KEY_PREFIX not in text:
        return text
    import re as _re
    pat = _re.compile(r'[ \t]*\[' + _re.escape(ELIDED_KEY_PREFIX)
                      + r'[0-9a-f]{12} \d+ chars of [a-z ]+\]'
                      + ('\n?' if marker_line else ''))
    return pat.sub('', text)


def _elided_id(text: str) -> str:
    import hashlib
    return hashlib.sha256(
        text.encode('utf-8', 'surrogatepass')).hexdigest()[:12]


def elision_scope(user_id=None, request_id=None) -> str:
    """Whose elided text a pointer may read: a digest of the user the LLM
    call acted for, else of its request id, else 'anon'.

    A pointer resolves only in the scope that wrote it, so one user's
    elided text is never readable through another user's get_data_by_key
    (review of f97b6bed8: the one shared store let anyone with an id read
    it).  With no argument, the scope of the current LLM call: the user id
    ``with_llm_context`` bound, else the thread-local one, else the request
    id."""
    import hashlib
    if user_id is None and request_id is None:
        user_id = _user_id_var.get() or ''
        if not user_id:
            try:
                from hartos.threadlocal import thread_local_data as _tl
                user_id = _tl.get_user_id() or ''
            except Exception:
                user_id = ''
        request_id = '' if user_id else _get_request_id()
    if user_id:
        return 'u' + hashlib.sha256(str(user_id).encode()).hexdigest()[:12]
    if request_id:
        return 'r' + hashlib.sha256(str(request_id).encode()).hexdigest()[:12]
    return 'anon'


def _elided_item(scope: str, pointer_id: str) -> str:
    """The agent-data namespace of ONE elided item: ``elided_<scope>_<id>``."""
    return f'{ELIDED_NAMESPACE}_{scope}_{pointer_id}'


def _evict_elided() -> None:
    """Remove elided items older than _ELIDED_TTL_S and, past
    _ELIDED_MAX_ITEMS items or _ELIDED_MAX_BYTES on disk, the oldest.  One
    sweep at a time; never raises."""
    if not _elided_evict_lock.acquire(blocking=False):
        return
    try:
        from core.cache_loaders import AGENT_DATA_DIR
        prefix, suffix = ELIDED_NAMESPACE + '_', '_agent_data.json'
        items = []
        for entry in os.scandir(AGENT_DATA_DIR):
            if entry.name.startswith(prefix) and entry.name.endswith(suffix):
                try:
                    st = entry.stat()
                    items.append((st.st_mtime, st.st_size, entry.path))
                except OSError as e:
                    logger.debug("wire-trim: elided item unreadable: %s", e)
        items.sort()
        now = time.time()
        excess = max(0, len(items) - _ELIDED_MAX_ITEMS)
        total = sum(size for _, size, _ in items)
        for n, (mtime, size, path) in enumerate(items):
            if (n < excess or total > _ELIDED_MAX_BYTES
                    or now - mtime > _ELIDED_TTL_S):
                try:
                    os.remove(path)
                    total -= size
                except OSError as e:
                    logger.debug("wire-trim: elided item not evicted: %s", e)
    except Exception as e:
        logger.debug("wire-trim: elided eviction skipped: %s", e)
    finally:
        _elided_evict_lock.release()


def _save_elided(records: dict) -> bool:
    """Write each record as its own item in the current elision_scope --
    one small atomic file per item, no shared file rewritten, no global lock
    on the hot path -- and sweep old items every _ELIDED_EVICT_EVERY writes.
    Never raises; False when any item could not be written (the caller then
    sends plain markers, never a pointer to nothing)."""
    global _elided_writes
    if not records:
        return True
    try:
        from core.cache_loaders import save_agent_data
        scope = elision_scope()
        ok = all(save_agent_data(_elided_item(scope, pid), rec)
                 for pid, rec in records.items())
        _elided_writes += len(records)
        if _elided_writes >= _ELIDED_EVICT_EVERY:
            _elided_writes = 0
            _evict_elided()
        return ok
    except Exception as e:
        logger.warning("wire-trim: could not save elided text: %s", e)
        return False


def read_elided(pointer_id: str, scope=None):
    """The original text a pointer names in ``scope`` (default: the current
    call's elision_scope), or None when that scope holds no such item."""
    try:
        from core.cache_loaders import load_agent_data
        pid = str(pointer_id).strip()
        if not pid.isalnum():
            return None
        entry = load_agent_data(_elided_item(scope or elision_scope(), pid))
        return entry.get('text') if isinstance(entry, dict) else None
    except Exception:
        return None


def _middle_cut(text: str, head_chars: int, tail_chars: int,
                marker: str) -> str:
    """``text``'s first ``head_chars`` and last ``tail_chars`` characters with
    ``marker`` between.  A cut never leaves half of a surrogate pair at
    either edge: json.dumps would send it as a lone surrogate escape, which
    llama.cpp refuses with a 500 (review of bb809af28)."""
    head = text[:head_chars]
    if head and 0xD800 <= ord(head[-1]) <= 0xDBFF:
        head = head[:-1]
    tail = text[-tail_chars:] if tail_chars else ''
    if tail and 0xDC00 <= ord(tail[0]) <= 0xDFFF:
        tail = tail[1:]
    return head + marker + tail


def _truncate_tool_call_arguments(msg: dict, room_tokens: int, marker,
                                  model=None) -> tuple:
    """Cut the arguments of the tool calls ``msg`` carries so they cost about
    ``room_tokens`` together AS SENT; ``(new_msg, n_cut_chars)``.

    Arguments stay one strict JSON object -- llama.cpp answers 500 "Failed
    to parse tool call arguments as JSON" to anything else -- so a cut call's
    arguments become ``{"trimmed_arguments": <head> marker <tail>}``.  Each
    call gets an equal share of the room; one under its share is left as it
    is.  Review of be96f2510: a call with 40k-char arguments kept whole with
    its protected result sent 20,243 tokens against a 7,424 budget.

    Sized on the ESCAPED result, not the raw text: json.dumps writes a quote
    or a backslash as two characters and an emoji as a 12-character
    surrogate escape, so a cut sized on the raw text stayed 10k-19k tokens
    against 7,424 (review of f97b6bed8).  The cut shrinks until the sent
    arguments fit, a few rounds at most.  ``marker`` is a string, or a
    callable given the original arguments (it names the pointer)."""
    from core.token_utils import count_tokens_for_text
    calls = msg.get('tool_calls')
    if not isinstance(calls, list) or not calls:
        return msg, 0
    share = max(1, int(room_tokens) // len(calls))
    new_calls, n_cut = [], 0
    for tc in calls:
        fn = tc.get('function') if isinstance(tc, dict) else None
        args = fn.get('arguments') if isinstance(fn, dict) else None
        if (not isinstance(args, str)
                or count_tokens_for_text(json.dumps(args), model) <= share):
            new_calls.append(tc)
            continue
        mark = marker(args) if callable(marker) else marker
        chars = _chars_for_tokens(args, share, model)
        for _ in range(8):
            tail_chars = chars // 2
            cut = _middle_cut(args, chars - tail_chars, tail_chars, mark)
            sent = json.dumps({'trimmed_arguments': cut})
            # As the body is counted and sent: the arguments are a JSON
            # string INSIDE the tool_calls JSON, so escaped once more.
            cost = count_tokens_for_text(json.dumps(sent), model)
            if cost <= share or chars <= 0:
                break
            chars = int(chars * share / cost * 0.9)
        new_calls.append({**tc, 'function': {**fn, 'arguments': sent}})
        n_cut += len(args) - len(cut) + len(mark)
    if not n_cut:
        return msg, 0
    return {**msg, 'tool_calls': new_calls}, n_cut


def _chars_for_tokens(text: str, tokens: int, model=None) -> int:
    """How many characters of ``text`` hold about ``tokens`` tokens, at the
    text's OWN chars/token -- dense JSON runs ~2.5, prose ~4, so one fixed
    ratio either over- or under-cuts.  Falls back to 3.5 for empty text."""
    from core.token_utils import count_tokens_for_text
    n = count_tokens_for_text(text, model) if text else 0
    ratio = (len(text) / n) if n else 3.5
    # 10% under: a cut's two ends tokenize a little worse than the average
    # (measured: 2-17 tokens over a 700-token room without it).
    return int(max(0, tokens) * ratio * 0.9)


def must_keep_head(text: str, role='user') -> int:
    """How many leading characters of ``text`` no cut may take: a REUSE
    dispatch turn's marker and the user's words, up to and including
    ``ACTION_STEPS_SEPARATOR``.  0 for any other message.

    The ONE rule for both places that shorten a turn: the wire trim (which
    passes the message's role) and the seats' token limiter (whose messages
    are conversation turns only: autogen adds the system prompt after the
    transforms).  Only a user turn is a dispatch turn: a system prompt that
    happens to contain the separator is cut like any other (review of
    f97b6bed8)."""
    if role != 'user' or not isinstance(text, str):
        return 0
    from core.constants import ACTION_STEPS_SEPARATOR
    at = text.find(ACTION_STEPS_SEPARATOR)
    return at + len(ACTION_STEPS_SEPARATOR) if at >= 0 else 0


def keep_head_cut(text: str, keep: int, tail_chars: int, marker: str) -> str:
    """``text``'s first ``keep`` characters whole, then ``marker``, then its
    last ``tail_chars``: the cut both the trim and the limiter make of a
    dispatch turn (must_keep_head)."""
    return _middle_cut(text, keep, max(0, tail_chars), marker)


def ensure_user_turn(messages: list) -> bool:
    """Give ``messages`` one role='user' turn if it has none; True when added.

    Both model servers refuse a conversation without a user turn, and a
    role='tool' result does not count: llama-server's Qwen3 chat template
    raises a hard 500 "No user query found in messages." (measured
    2026-09-03), and central's hosted Qwen endpoint answers a bare 400
    "invalid request" (measured 2026-09-13, task #89).  The turn carries
    WIRE_USER_SEED_TEXT and goes right after a leading system message.
    Mutates ``messages`` in place; a no-op when any user turn exists.

    One rule, two callers: the wire trim below applies it to the bodies it
    intercepts (local llama-server ports only), and
    ToolMessageHandler.validate_messages applies it on the agent path, the
    only one of the two that sees a hosted endpoint's traffic.
    """
    from core.constants import WIRE_USER_SEED_TEXT
    if not messages or any(isinstance(m, dict) and (m.get('role') or '') == 'user'
                           for m in messages):
        return False
    idx = 1 if (isinstance(messages[0], dict)
                and messages[0].get('role') == 'system') else 0
    messages.insert(idx, {'role': 'user', 'name': 'User',
                          'content': WIRE_USER_SEED_TEXT})
    # A last-resort guard, not a path: since the seats' limiters put the
    # task turn back (protected_messages), a body without a user turn means
    # it was lost upstream.  Loud and counted, so it is seen, not absorbed.
    global _user_seed_count
    with _user_seed_lock:
        _user_seed_count += 1
        n = _user_seed_count
    logger.warning(
        "wire-trim: seeded a user turn (WIRE_USER_SEED_TEXT) into a body with "
        "none -- the real task turn was lost before the wire (seed #%d this "
        "process); roles=%s", n,
        [m.get('role') for m in messages if isinstance(m, dict)][:12])
    return True


_user_seed_count = 0
_user_seed_lock = threading.Lock()


def user_seed_count() -> int:
    """How many bodies ensure_user_turn had to seed in this process."""
    return _user_seed_count


def _task_turn(messages: list):
    """The newest turn of the speaker who opened the conversation's user side.

    ``None`` when the first role='user' message carries no ``name`` -- a body
    whose speakers cannot be told apart has no task turn separate from the
    newest user turn.  NEWEST of that speaker, not its first message: a
    carried-over history can open with an earlier request from the same user,
    and that one is stale.
    """
    opener = next((m for m in messages
                   if isinstance(m, dict) and m.get('role') == 'user'), None)
    speaker = opener.get('name') if opener is not None else None
    if not speaker:
        return None
    return next((m for m in reversed(messages)
                 if isinstance(m, dict) and m.get('role') == 'user'
                 and m.get('name') == speaker), None)


def protected_messages(messages: list) -> list:
    """The messages no trimming may remove, newest-user first, deduplicated.

    THE one protected set, read by both places that shorten a conversation:
    the wire trim (``_trim_to_budget``) and the seats' context limiters
    (``hartos.helper`` history_limiter / token_limiter).  The limiters used
    to keep only the newest message, and dropped the user's task turn
    BEFORE the wire saw the body: live 2026-09-27, REUSE probe
    liveprobe_reuse_1, 99 of 113 ToolMessageHandler inputs held no message
    from User.  One set means the two can never disagree about what must
    survive.

      * the newest role='user' message: llama.cpp's Qwen3.5 template raises
        "No user query found in messages." without one (measured 3x on
        2026-08-30, source autogen.reuse);
      * the task turn (:func:`_task_turn`): in an autogen group chat every
        other agent's message reaches a seat as role='user', so the newest
        user turn is often the StatusVerifier's verdict and the user's own
        text is the oldest message (measured 2026-09-25 21:17:36);
      * the newest role='tool' message: what the current step produced, the
        thing the Assistant must use and the StatusVerifier must check (owner
        decision, delegated 2026-09-26).
    """
    anchor = next((m for m in reversed(messages)
                   if isinstance(m, dict) and m.get('role') == 'user'), None)
    newest_result = next((m for m in reversed(messages)
                          if isinstance(m, dict) and m.get('role') == 'tool'),
                         None)
    out = []
    for m in (anchor, _task_turn(messages), newest_result):
        if m is not None and not any(m is o for o in out):
            out.append(m)
    return out


def _drop_units(messages: list) -> dict:
    """``id(message) -> the messages the drop must remove along with it``.

    An assistant message that carries ``tool_calls`` and the role='tool'
    messages answering it are one unit: removing the call and keeping a
    result leaves an answer to a call the model never sees.  Measured on the
    live llama-server (b10330, Qwen3.5-4B template, 2026-09-26): such a body
    is accepted with 200 and renders the result as a bare ``<tool_response>``
    user turn, no ``<tool_call>`` before it.  A result pairs with the NEAREST
    earlier message announcing its ``tool_call_id``, since a model may reuse
    ids across turns.  Only the top-level ``tool_call_id`` is read: autogen's
    ``tool_responses`` bundle is split into one message per call before the
    wire (0 of 1,111 logged wire bodies carried it, 2026-09-26).  A message
    outside any unit is absent from the map and drops alone.
    """
    units = {}
    announcer = {}  # call id -> the nearest earlier message announcing it
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get('role') == 'assistant' and isinstance(m.get('tool_calls'), list):
            for tc in m['tool_calls']:
                if isinstance(tc, dict) and isinstance(tc.get('id'), str):
                    announcer[tc['id']] = m
        elif (m.get('role') == 'tool'
              and isinstance(m.get('tool_call_id'), str)):
            parent = announcer.get(m['tool_call_id'])
            if parent is not None:
                unit = units.setdefault(id(parent), [parent])
                unit.append(m)
                units[id(m)] = unit
    return units


def _strip_pointers(msg: dict) -> dict:
    """``msg`` with every pointer removed (the plain WIRE_TRIM_MARKER stays),
    for a body whose elided text could not be saved."""
    # The one pointer format, removed by the one stripper.
    def sub(text):
        return strip_elided_pointers(text, marker_line=True)
    out = dict(msg)
    if isinstance(out.get('content'), str):
        out['content'] = sub(out['content'])
    elif isinstance(out.get('content'), list):
        out['content'] = [{**p, 'text': sub(p['text'])}
                          if isinstance(p, dict) and isinstance(p.get('text'), str)
                          else p for p in out['content']]
    if out.get('tool_calls'):
        calls = []
        for tc in out['tool_calls']:
            fn = tc.get('function') if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get('arguments'), str):
                try:
                    obj = json.loads(fn['arguments'])
                    if isinstance(obj, dict) and isinstance(
                            obj.get('trimmed_arguments'), str):
                        obj['trimmed_arguments'] = sub(
                            obj['trimmed_arguments'])
                        tc = {**tc, 'function': {**fn, 'arguments': json.dumps(obj)}}
                except ValueError as e:
                    # Arguments that are not JSON carry no pointer to strip.
                    logger.debug("wire-trim: call arguments not JSON: %s", e)
            calls.append(tc)
        out['tool_calls'] = calls
    return out


def _trim_to_budget(body: dict, _reserve: int = 0) -> tuple:
    """Return ``(trimmed_body, n_dropped, n_truncated_chars, est_before,
    est_after, budget)``.

    Trim policy (best-effort, idempotent):
      1. budget = per_slot - max_tokens - safety - the tool schema's tokens
      2. If under budget → return unchanged.
      3. Left-drop non-system messages (preserve index 0 if role=system)
         until the remaining set fits.  Never dropped: the system message,
         the most-recent message, the newest role='user' message, the
         task turn (:func:`_task_turn`, the initiator's newest turn) and the
         newest role='tool' message.  An assistant message carrying tool_calls drops together with the
         results answering it (:func:`_drop_units`), and is kept with them
         when one of them is never dropped.
      4. If still over, cut the middle of the messages step 3 could not drop,
         one at a time, until the set fits: the most-recent message first
         when nothing protects it, then the protected ones, largest first.
         Each is cut only as far as the others at their current size
         require, never below 64 tokens nor below a dispatch turn's marker
         and words (:func:`must_keep_head`), with ``WIRE_TRIM_MARKER`` where
         its middle was so the LLM sees the truncation.
      5. If STILL over, cut the middle of the system message the same way.
      A cut keeps each message's head and tail (_truncate_msg_content).
      A body that is still over after step 5 is sent as is and logged as
      an error.

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

    # ─── Guarantee at least one role='user' turn (before any budget math) ───
    # llama-server's Qwen3 chat template raises a hard 500 "No user query
    # found in messages." whenever the body reaches it with no user turn — a
    # role='tool' result does NOT satisfy it.  Measured live 2026-09-03
    # 03:40:07 (source autogen.reuse, roles [system, assistant, tool,
    # assistant]): a reuse group-chat reply view lost its user anchor and the
    # turn died with that 500.  The anchor logic further down only PRESERVES
    # an existing user turn during trimming, and the whole trim path is
    # skipped for under-budget bodies (this one was 4 short messages ~ far
    # under budget), so a user-less small body sailed straight through the
    # `est_before <= budget` early-return untouched.  The wire is the single
    # chokepoint EVERY outbound body crosses (autogen + langchain + raw SDK),
    # which is why the guard belongs here and not in a per-agent transform:
    # ToolMessageHandler.validate_messages is registered per agent and was
    # bypassed on this reply path (no seed line logged, body still user-less).
    # Idempotent — strict no-op when a user turn already exists.
    if ensure_user_turn(messages):
        # Rebuild body so BOTH the under-budget early-return and the trim
        # path carry the seed (the early-return returns `body` as-is; a fresh
        # dict also makes `_apply_trim_to_request`'s `trimmed is body` check
        # re-serialize the wire bytes, exactly like the max_tokens pin below).
        body = dict(body)
        body['messages'] = messages
        logger.info(
            "wire-trim: seeded one user turn (body had no role='user' — would "
            "trip llama-server's Qwen3 'No user query found in messages' 500).")
    # For the one re-run with a reserve (see the end): its own copy of the
    # list, which the drop and the cut below change in place.
    _entry_body = dict(body, messages=list(messages))

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
    # Drop pydantic's Optional-boilerplate BEFORE charging the schema, so the
    # budget reflects the bytes that actually reach the socket.  Compacting
    # after this line would leave the budget pessimistic by exactly the saving
    # and still fire the degrade branch below — the drift this layer exists to
    # prevent.  Lossless (see _compact_tool_schema): same tools, same
    # arguments, same required set.
    body, _schema_saved = _compact_tool_schema(body, model)
    if _schema_saved:
        logger.debug("wire-trim: tool schema compacted, -%d tok", _schema_saved)

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
        budget = _min_message_budget(_get_budget_per_slot())

    est_before = count_tokens_for_messages(messages, model)
    if est_before <= budget:
        return body, 0, 0, est_before, est_before, budget

    # What a trim that elides adds -- the pointer explanation in the system
    # message and the dropped results it names -- is only known once the
    # trim has run.  So the trim runs aiming at the full budget and, when
    # those additions then push it over, once more with exactly that much
    # reserved (``_reserve``; see the end of this function).  A cut's own
    # pointer is reserved in ``marker_tokens`` below.  The budget RETURNED
    # is always the full one.
    _sample_pointer = elided_pointer('0' * 12, 10 ** 6, 'an assistant turn')
    _pointer_tokens = count_tokens_for_text(_sample_pointer + '\n', model)
    full_budget = budget
    budget = max(1, budget - int(_reserve))
    elided = {}          # pointer_id -> record, saved once at the end
    dropped_pointers = []

    def _remember(text, kind):
        pid = _elided_id(text)
        elided[pid] = {'text': text, 'kind': kind, 'at': time.time(),
                       'request_id': _get_request_id()}
        return elided_pointer(pid, len(text), kind)

    def _marker_for(msg):
        kind = _ELIDED_KINDS.get(msg.get('role'), 'a message')
        return lambda text: (WIRE_TRIM_MARKER + _remember(text, kind) + '\n')

    has_system = bool(messages and isinstance(messages[0], dict)
                      and messages[0].get('role') == 'system')
    # Never dropped: the newest user message, the task turn and the newest
    # tool result -- protected_messages, the one set the seats' context
    # limiters keep too.  Each was added after a measured failure; the
    # reasons live on that function.
    protected = protected_messages(messages)
    start = 1 if has_system else 0
    n_dropped = 0
    # A tool call and its results leave together or not at all (see
    # _drop_units): review of 9ddc8b92d, probed, [system, User task,
    # assistant tool_calls, StatusVerifier, tool] trimmed to [system, user,
    # user, tool] -- the result kept, the call it answers dropped.
    units = _drop_units(messages)
    must_stay = protected + messages[-1:]
    while True:
        # Leftmost message that is not the system prompt, not protected and
        # not the newest message -- the same "keep system + newest" floor --
        # taken with the rest of its unit.  A unit holding a message that
        # must stay (a call whose result is the newest message) stays whole.
        drop = next((unit for unit in (units.get(id(messages[i]), [messages[i]])
                                       for i in range(start, len(messages)))
                     if not any(m is k for m in unit for k in must_stay)),
                    None)
        if drop is None:
            break
        messages[:] = [m for m in messages if not any(m is d for d in drop)]
        n_dropped += len(drop)
        for d in drop:
            if isinstance(d, dict) and d.get('role') == 'tool':
                d_text = _content_to_text(d.get('content'))
                if d_text:
                    dropped_pointers.append(_remember(d_text, 'a tool result'))
        if count_tokens_for_messages(messages, model) <= budget:
            break

    # Each cut below sizes a message against everything else in the set plus
    # two reserves: the envelope overhead of the message being cut, and the
    # truncation marker prepended to it.  Previous bug: the marker was not
    # reserved, so the cut message exceeded budget by the marker length
    # (~7 tokens) and the wire request still tickled n_ctx.
    _TOKENS_PER_MSG = 4  # OpenAI envelope overhead per message
    marker_tokens = (count_tokens_for_text(WIRE_TRIM_MARKER, model)
                     + _pointer_tokens)

    n_truncated_chars = 0

    # The drop above never removes a protected message or the newest one (nor
    # a tool call whose result is the newest; that call is not cut), so
    # when one of those is the oversized component, cutting its content is
    # the only way to fit.  The cut once reached only messages[-1], and in the
    # autogen.reuse conversations the anchor sits mid-list behind
    # assistant/tool replies.  Measured 2026-08-30 19:35-19:46 on the
    # installed build: system+anchor ~5597 tok against budget 3840 -- every
    # trim ended in the STILL-over error below and llama-server rejected the
    # turn, 95x in 11 minutes.  So the cut covers every message the drop
    # kept: protecting a message from the drop must never make the trim
    # unable to fit it.
    #
    # LARGEST FIRST.  Each message's room is computed with the others at their
    # current size, so the order decides who is cut.  Anchor-first (the first
    # cut) sized the anchor against a still-full task, floored it at 64
    # tokens, then cut the task anyway and left budget unused: measured in the
    # StatusVerifier seat (review of bac8f91c4), the Assistant's 2.7k-char
    # result -- the thing the verifier must check -- went to ~224 chars while
    # the user's 15k-char input was what needed cutting.  Shrinking the larger
    # message first means the smaller one is only cut when the larger alone
    # cannot make room.
    #
    # The NEWEST message is in the same pass.  It used to be cut first, in a
    # separate step sized against the others at full size; in the
    # StatusVerifier seat the anchor (the Assistant's result) IS the newest
    # message, so it was floored while the 15k-char task was what needed
    # cutting (review of 520c95e28, probed: anchor 2822 -> 247 chars, task
    # cut anyway, est 569 of budget 740).  Deduplicated by identity: the
    # newest message is often the anchor itself.  Room converts to chars at
    # the message's own measured chars/token (_chars_for_tokens): a fixed 3.5
    # over-counted dense text (JSON steps run ~2.5), so the cut left the
    # message over its room.
    #
    # UNPROTECTED BEFORE PROTECTED.  When the newest message is protected by
    # nothing (an assistant reply), it is cut before any protected message,
    # whatever the sizes.  Largest-first decides only among the protected.
    # The newest tool result used to be that unprotected message, and was
    # floored at 64 tokens ahead of a whole task (review of 9ddc8b92d); it is
    # protected now, so a 15k task and a 10.5k result share the cut, the
    # larger first -- which can cut the middle of the task, the price of the result
    # keeping real content (owner decision above).
    #
    # AND THE UNITS THEY PIN.  A protected tool result keeps its whole unit
    # (_drop_units): the tool_calls message and every sibling result.  Those
    # can be neither dropped nor, until the review of be96f2510, cut: a call
    # with 40k-char arguments sent 20,243 tokens and three parallel ~16k
    # results 8,307, each against a budget of 7,424.  They are unprotected
    # candidates, so they are cut first, largest first; a call is cut in its
    # arguments (_truncate_tool_call_arguments), a sibling in its content.
    pinned = [m for m in messages
              if any(m is k for u in (units.get(id(s), [s]) for s in must_stay)
                     for k in u)]
    candidates = []
    for m in protected + messages[-1:] + pinned:
        if not any(m is c for c in candidates):
            candidates.append(m)

    def _cut_order(m):
        is_protected = any(m is p for p in protected)
        return (is_protected, -count_tokens_for_messages([m], model))

    for p in sorted(candidates, key=_cut_order):
        if count_tokens_for_messages(messages, model) <= budget:
            break
        # Always found: the drop skips every candidate (protected or newest).
        p_idx = next(i for i, m in enumerate(messages) if m is p)
        others = messages[:p_idx] + messages[p_idx + 1:]
        overhead_tokens = (count_tokens_for_messages(others, model)
                           + _TOKENS_PER_MSG
                           + marker_tokens)
        p_text = _content_to_text(p.get('content'))
        # A dispatch turn keeps its marker and words whatever its room
        # (_truncate_msg_content / must_keep_head): sized against a
        # full-size tool result it went to the 64-token floor and lost them
        # (review of 111c458b0).  The candidates after it are sized against
        # what it kept.
        room_for_p = max(64, budget - overhead_tokens)
        new_p, n_cut = p, 0
        if p.get('tool_calls'):
            # A call is cut in its arguments AND its text, each in proportion
            # to what it costs (review of f97b6bed8: a call carrying "Writing
            # the file." was cut in neither, 20,416 tokens against 7,424).
            args_tokens = count_tokens_for_text(
                json.dumps(p['tool_calls'], ensure_ascii=False), model)
            text_tokens = (count_tokens_for_text(p_text, model)
                           if p_text.strip() else 0)
            args_room = max(1, room_for_p * args_tokens
                            // max(1, args_tokens + text_tokens))
            new_p, n_cut = _truncate_tool_call_arguments(
                p, args_room,
                lambda t: (WIRE_TRIM_MARKER
                           + _remember(t, 'tool call arguments') + '\n'),
                model)
            room_for_p = max(64, room_for_p - args_room)
        if p_text.strip():
            target_chars = _chars_for_tokens(p_text, room_for_p, model)
            new_p, n_text = _truncate_msg_content(
                new_p, target_chars, _marker_for(p), _content_to_text)
            n_cut += n_text
        if n_cut:
            n_truncated_chars += n_cut
            messages[p_idx] = new_p

    # LAST resort: the SYSTEM message itself.  autogen.reuse builds its
    # system prompt as persona boilerplate + the whole serialized recipe —
    # measured 2026-08-30 20:06-20:20 on the installed build: 86 of 100
    # STILL-over failures were this shape (sample: [system 28,154 chars,
    # assistant 247]), each sent doomed and rejected by llama-server.  When
    # the drop and the cut pass have both run and the set is STILL over, the
    # system message is the only mass left; cutting its middle keeps the
    # persona head and the actionable recipe tail.  Only reached
    # when the alternative is a guaranteed reject.
    if (count_tokens_for_messages(messages, model) > budget
            and has_system and len(messages) >= 1):
        others = messages[1:]
        overhead_tokens = (count_tokens_for_messages(others, model)
                           + _TOKENS_PER_MSG + marker_tokens)
        room_for_system = max(64, budget - overhead_tokens)
        target_chars = _chars_for_tokens(
            _content_to_text(messages[0].get('content')), room_for_system,
            model)
        new_sys, n_cut = _truncate_msg_content(
            messages[0], target_chars, _marker_for(messages[0]),
            _content_to_text)
        if n_cut:
            n_truncated_chars += n_cut
            messages[0] = new_sys

    # ─── Pointers: save what was elided, then explain them -- or, when the
    # store cannot be written, send plain markers (never a pointer to
    # nothing).  A pointer that ended up cut out of a message is not saved.
    budget = full_budget
    sent = '\n'.join(_content_to_text(m.get('content')) + json.dumps(
        m.get('tool_calls') or '', ensure_ascii=False) for m in messages)
    listed = dropped_pointers[-_ELIDED_LISTED_DROPS:]
    live = {pid: rec for pid, rec in elided.items()
            if pid in sent or any(pid in lp for lp in listed)}
    _added = count_tokens_for_text(
        ELIDED_POINTER_EXPLANATION + ' Removed earlier: '
        + ' '.join(listed) + '.', model) if live else 0
    if live and _added * _ELIDED_MIN_BUDGET_MULTIPLE > full_budget:
        # A budget this small cannot spare the explanation for the text it
        # needs: plain markers, as before pointers existed.
        live = {}
    if live and not _reserve:
        if count_tokens_for_messages(messages, model) + _added > full_budget:
            return _trim_to_budget(_entry_body, _reserve=_added)
    if not live and elided:
        messages[:] = [_strip_pointers(m) for m in messages]
    elif live and _save_elided(live):
        explanation = ELIDED_POINTER_EXPLANATION
        if listed:
            explanation += (' Removed earlier: ' + ' '.join(listed) + '.')
        if has_system:
            head = dict(messages[0])
            head['content'] = (_content_to_text(head.get('content'))
                               + explanation)
            messages[0] = head
        else:
            messages.insert(0, {'role': 'system',
                                'content': explanation.strip()})
    elif live:
        messages[:] = [_strip_pointers(m) for m in messages]

    # ─── Post-trim acceptance test — the trim is best-effort, so CHECK it ───
    # Trimming can be structurally unable to reach the budget: every message
    # it cuts keeps at least 64 tokens, and a tool call kept because its
    # result is the newest message is not cut at all.  On 2026-08-29, when the trim could
    # not yet cut the system message, that produced est 795 against a budget
    # of 351 — over by 2.3x — and the request was sent anyway because `we
    # truncated something` was treated as success.  llama-server then
    # rejected it (11,236 > n_ctx 8192).  Say so here: a silent doomed
    # request costs a full round trip and surfaces to the user as an
    # unexplained failure (see #591 for the caller side).
    _est_after = count_tokens_for_messages(messages, model)
    _wire_total = _est_after + tools_tokens
    _per_slot = _get_budget_per_slot()
    if _est_after > budget or _wire_total > _per_slot:
        logger.error(
            "[TRIM] trim could not reach budget — request is STILL over and "
            "will very likely be rejected: messages %d tok + schema %d tok = "
            "%d tok against n_ctx %d (budget was %d, %d msg(s) dropped, %d "
            "char(s) truncated). Every message the trim may cut keeps at "
            "least 64 tokens; the oversized component is %s.",
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
    # `trimmed is body` iff NOTHING changed — _trim_to_budget returns the
    # original object untouched only when the body already fit AND already
    # carried max_tokens.  Gating on the drop/truncate counts alone threw
    # away the max_tokens pin on the under-budget path (small prompts — the
    # exact runaway case): measured live 21:23-21:53, 96 reuse bodies went
    # out pinned at 2048 while 87 same-source bodies went out unpinned.
    if n_dropped == 0 and n_truncated == 0 and trimmed is body:
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
        if n_dropped or n_truncated:
            logger.warning(
                "[TRIM] trimmed %d msg(s) + %d char(s) — est tokens "
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


# Per-argument-string cap.  Long enough to carry a real tool call whole
# (the longest observed live was ~380 chars), short enough that one
# runaway blob cannot eat the file's size budget (PERF-2).
_RESP_ARG_CAP = 600

# Per-error-string cap.  Long enough for llama-server's whole overflow
# sentence (~130 chars) and a hosted provider's JSON error object, short
# enough that an HTML error page cannot eat the file's size budget (PERF-2).
_RESP_ERROR_CAP = 1200


def _response_error(response, status) -> Optional[str]:
    """What the server SAID on a non-2xx, from an ALREADY-BUFFERED body.

    ``None`` when there is nothing to report — a 2xx, or a body that was never
    buffered.  Absent stays visibly different from empty, for the reason
    :func:`_response_tool_calls` keeps that distinction.

    WHY THIS EXISTS (measured 2026-09-22, this box).  Across 1,184 records in
    ``llm_outbound.jsonl`` + its ``.old`` rotation, 60 carry
    ``response_status: 400`` and every one of them stores only the status code.
    The cause — ``request (6249 tokens) exceeds the available context size
    (4096 tokens)`` — existed only in ``logs/llama_server_8080.log``, so
    attributing those 400s to the tool schema was a CORRELATION between tool
    counts (23 passing vs 50-65 failing) rather than a quotation.  This file is
    the one place that sees every framework's response; recording the refusal
    here is what makes that inference unnecessary next time.

    Same read discipline as the tool-call extractor: only ``_content``, which
    httpx sets when a non-streaming ``send`` has already buffered the body.
    Touching ``.content`` would raise on an unread response and drain the bytes
    the real caller is waiting for.

    Prefers the provider's own ``error`` object (llama-server, OpenAI and Azure
    all use it) and falls back to the raw text, so a proxy's HTML 502 is still
    an answer rather than "unparseable, therefore nothing happened".
    """
    try:
        if isinstance(status, int) and 200 <= status < 300:
            return None
        raw = getattr(response, '_content', None)
        if raw is None:
            return None
        text = bytes(raw).decode('utf-8', 'replace').strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get('error', data)
            text = (json.dumps(err, ensure_ascii=False, default=str)
                    if not isinstance(err, str) else err)
        if len(text) > _RESP_ERROR_CAP:
            text = text[:_RESP_ERROR_CAP] + '...[cut]'
        return text
    except Exception:
        # A logging hook may never fail an LLM call.
        return None


def _response_tool_calls(response) -> Optional[list]:
    """Tool-call names + RAW ``arguments`` strings from an ALREADY-BUFFERED
    response body.  ``None`` when there is nothing safely readable.

    Why the response and not just the request (#787).  The ``tool_calls``
    that appear in a later request body are autogen's re-serialisation of an
    earlier completion, so a ``{}`` there could equally mean the model
    generated ``{}`` or that the arguments were dropped in between.  Those
    have opposite fixes.  Recording the completion as it arrived is the only
    way to tell them apart, and this function is already on every LLM
    response, so it is the one place that can.

    Never consumes a stream.  Only ``_content`` is read — httpx sets it when
    a non-streaming ``send`` has already buffered the body, and leaves it
    absent for ``stream=True``.  Touching ``.content`` instead would raise on
    an unread response and, worse, drain the bytes the real caller is waiting
    for.  urllib's ``HTTPResponse`` has no ``_content`` at all, so that
    transport simply reports nothing rather than being read behind the
    caller's back.

    Returns ``[]`` for a readable completion that made no tool call — a
    distinct fact from ``None`` ("could not read"), and collapsing the two
    would turn an absent measurement into a false zero.
    """
    try:
        raw = getattr(response, '_content', None)
        if raw is None:
            return None
        data = json.loads(bytes(raw).decode('utf-8', 'replace'))
        if not isinstance(data, dict):
            return None
        out = []
        for choice in (data.get('choices') or []):
            if not isinstance(choice, dict):
                continue
            msg = choice.get('message') or choice.get('delta') or {}
            for tc in (msg.get('tool_calls') or []):
                fn = (tc or {}).get('function') or {}
                args = fn.get('arguments')
                args = args if isinstance(args, str) else json.dumps(
                    args, default=str)
                if len(args) > _RESP_ARG_CAP:
                    args = args[:_RESP_ARG_CAP] + '...[cut]'
                # `id` is the join key back to the same call's replays in later
                # request bodies.  Without it, "these arguments went missing"
                # is a name-level inference; with it, the two mechanisms
                # separate — same id with '{}' means the call object was
                # rebuilt, a different id means it is simply another call
                # instance whose generation was never captured.
                out.append({'id': tc.get('id'),
                            'name': fn.get('name'),
                            'arguments': args,
                            'finish_reason': choice.get('finish_reason')})
        return out
    except Exception:
        # A logging hook may never fail an LLM call, and an unparseable body
        # is itself a legitimate outcome (an HTML error page, a 500).
        return None


def log_outbound(body: dict, *,
                 response_status: Any = None,
                 latency_ms: Optional[float] = None,
                 source: Optional[str] = None,
                 response_tools: Optional[list] = None,
                 response_error: Optional[str] = None) -> None:
    """Public hook for non-httpx callers (dispatcher's raw
    ``requests.post`` draft path).  Writes one JSONL record; never
    raises.

    ``source`` overrides whatever ``set_source`` / ``source_context``
    set on the thread-local context; pass it when the caller wants to
    label the call explicitly (e.g. ``dispatcher.draft``).

    ``response_tools`` is ``_response_tool_calls``' output; the key is
    omitted entirely when it is ``None`` so "not readable" stays visibly
    different from "read it, no tool calls" (``[]``).

    ``response_error`` is ``_response_error``' output — what the server said
    when it refused.  Same omit-when-None rule, and never written on a 2xx, so
    grepping the field finds exactly the failures."""
    try:
        record = {
            'ts': _ts(),
            'request_id': _get_request_id(),
            'source': source if source is not None else _get_source(),
            'body': _shape_body_for_log(body),
            'response_status': response_status,
            'latency_ms': latency_ms,
        }
        if response_tools is not None:
            record['response_tool_calls'] = response_tools
        if response_error is not None:
            record['response_error'] = response_error
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
            # A HOSTED provider's chat/completions call still feeds the
            # provider breaker (#106b b); every other request is an unchanged
            # passthrough.
            if _is_chat_completions_post(request):
                return _send_and_feed(_orig_send, self, request, kwargs)
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
            # Local llama-server is a provider too (host 127.0.0.1); feed the
            # breaker the real status (#106b b) before logging.
            _status = getattr(response, 'status_code', None)
            _feed_provider_breaker(request.url, _status)
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {},
                         response_status=_status,
                         latency_ms=round(elapsed, 1),
                         response_tools=_response_tool_calls(response),
                         response_error=_response_error(response, _status))
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
            # Hosted chat/completions still feeds the provider breaker (#106b b).
            if _is_chat_completions_post(request):
                _resp = await _orig(self, request, **kwargs)
                _feed_provider_breaker(request.url, getattr(_resp, 'status_code', None))
                return _resp
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
            _status = getattr(response, 'status_code', None)
            _feed_provider_breaker(request.url, _status)
            elapsed = (time.time() - start) * 1000
            log_outbound(body or {},
                         response_status=_status,
                         latency_ms=round(elapsed, 1),
                         response_tools=_response_tool_calls(response),
                         response_error=_response_error(response, _status))
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
            # Same extractor as the httpx sites — one notion of "what did the
            # response say", not a per-transport reimplementation.  A urllib
            # HTTPResponse carries no buffered `_content`, so it reports None
            # and the key is omitted; the alternative (read it here) would
            # drain the body the caller has not read yet.
            _status = getattr(response, 'status', None)
            log_outbound(body or {},
                         source=(_get_source() or 'urllib'),
                         response_status=_status,
                         latency_ms=round(elapsed, 1),
                         response_tools=_response_tool_calls(response),
                         response_error=_response_error(response, _status))
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

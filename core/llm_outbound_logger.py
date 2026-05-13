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

_TARGET_PORT = 8082
_TARGET_PATH = '/v1/chat/completions'
_LOG_FILENAME = 'llm_outbound.jsonl'

_installed = False
_install_lock = threading.Lock()
_file_handle = None  # type: Optional[Any]
_file_lock = threading.Lock()


def _get_request_id() -> str:
    """Best-effort thread-local request_id pull.  Empty string when
    we're outside an HTTP request context (e.g. daemon startup)."""
    try:
        from threadlocal import thread_local_data as _tl
        rid = getattr(_tl, 'request_id', None)
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


def _is_target_request(url, method: str) -> bool:
    if method != 'POST':
        return False
    try:
        return (
            getattr(url, 'port', None) == _TARGET_PORT
            and getattr(url, 'path', '') == _TARGET_PATH
        )
    except Exception:
        return False


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
            _annotate_request(request, body)
        start = time.time()
        try:
            response = _orig_send(self, request, **kwargs)
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
            "[outbound-hook] httpx (sync+async) patched — every POST "
            "to :%d%s will be logged to %s",
            _TARGET_PORT, _TARGET_PATH, _get_log_path(),
        )
        return True


def is_installed() -> bool:
    return _installed

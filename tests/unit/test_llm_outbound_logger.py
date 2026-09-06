"""Tests for ``core.llm_outbound_logger``.

The module's load-bearing invariants:

  1. ``install()`` is idempotent and survives missing httpx.
  2. The patch ONLY intercepts ``POST :8082/v1/chat/completions``.
     Other ports / paths pass through unmodified.
  3. When a target POST fires, the thread-local request_id is injected
     as the ``user`` field of the JSON body BEFORE the underlying
     ``httpx.Client.send`` is called.
  4. A JSONL record is appended with the full body, response status,
     and latency.
  5. Body-retention policy honours ``HEVOLVE_LLM_OUTBOUND_BODY``.
"""
from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ─── Helpers ──────────────────────────────────────────────────────────


class _FakeURL:
    def __init__(self, host='127.0.0.1', port=8082, path='/v1/chat/completions'):
        self.host = host
        self.port = port
        self.path = path

    def __str__(self):
        return f'http://{self.host}:{self.port}{self.path}'


class _FakeHeaders(dict):
    pass


class _FakeRequest:
    def __init__(self, body: bytes = b'',
                 url: _FakeURL = None,
                 method: str = 'POST'):
        self._content = body
        self.url = url or _FakeURL()
        self.method = method
        self.headers = _FakeHeaders()
        self.headers['content-length'] = str(len(body))

    @property
    def content(self):
        return self._content


class _FakeClient:
    pass


class _FakeResponse:
    """A response whose body is ALREADY buffered — what httpx hands back
    from a non-streaming ``Client.send`` (it calls ``.read()`` internally).

    Deliberately NOT a MagicMock: a MagicMock auto-creates ``_content`` as
    another MagicMock, so a test using one cannot tell "no body to read"
    apart from "read it and found nothing".
    """

    def __init__(self, payload=None, status_code=200, buffered=True):
        self.status_code = status_code
        if buffered:
            self._content = json.dumps(payload or {}).encode('utf-8')

    @property
    def content(self):
        # Mirrors httpx: touching .content on an unread stream raises.
        if not hasattr(self, '_content'):
            raise RuntimeError(
                'Attempted to access .content on a streaming response before '
                'read() — reading it here would CONSUME the stream and starve '
                'the real caller')
        return self._content


def _build_fake_httpx_module(response_factory=None):
    """Build a minimal stand-in for the ``httpx`` module so the patch
    can be installed without polluting the real one.

    ``response_factory`` (optional) supplies the object the fake send
    returns, so a test can exercise the response-side extraction with a
    realistic completion payload instead of the default MagicMock."""
    mod = types.ModuleType('httpx')
    captured = {'send_calls': []}

    def _mk_response():
        if response_factory is not None:
            return response_factory()
        resp = MagicMock()
        resp.status_code = 200
        return resp

    def _send(self, request, **kwargs):
        captured['send_calls'].append({
            'content': bytes(request._content or b''),
            'content_length': request.headers.get('content-length'),
            'url': str(request.url),
        })
        return _mk_response()

    class Client:
        send = _send

    class AsyncClient:
        async def send(self, request, **kwargs):
            captured['send_calls'].append({
                'content': bytes(request._content or b''),
                'content_length': request.headers.get('content-length'),
                'url': str(request.url),
            })
            resp = MagicMock()
            resp.status_code = 200
            return resp

    # Real httpx exposes ByteStream at the top level; the wire-trim uses it to
    # rebuild the request body stream after a rewrite (llm_outbound_logger.py:766)
    # and only falls back to httpx._content when it is absent. Provide it so the
    # test exercises the SAME code path production takes (rewrite -> ByteStream ->
    # Content-Length update), not the fake-only import-fallback branch.
    class ByteStream:
        def __init__(self, data=b''):
            self._data = bytes(data)

    mod.ByteStream = ByteStream
    mod.Client = Client
    mod.AsyncClient = AsyncClient
    mod._captured = captured
    return mod


def _reset_module(monkeypatch, log_dir):
    """Fresh import + redirect log path to a temp dir.  We import the
    real ``core.platform_paths`` first so ``core/__init__.py`` resolves
    its required names (get_data_dir, get_db_path, etc.), then monkey-
    patch ``get_log_dir`` to return our temp dir."""
    if 'core.llm_outbound_logger' in sys.modules:
        del sys.modules['core.llm_outbound_logger']
    import core.platform_paths as _pp  # real import, picks up all names
    monkeypatch.setattr(_pp, 'get_log_dir',
                        lambda: str(log_dir), raising=False)


# ─── Tests ────────────────────────────────────────────────────────────


def test_install_idempotent_and_returns_false_second_time(
        tmp_path, monkeypatch):
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    import core.llm_outbound_logger as mod
    assert mod.install() is True
    assert mod.install() is False
    assert mod.is_installed() is True


def test_install_still_patches_urllib_when_httpx_missing(tmp_path, monkeypatch):
    """CONTRACT CHANGED 2026-08-11 — deliberately, and this is the record.

    Previously this asserted ``install() is False`` and ``is_installed() is
    False`` when httpx was unimportable: the hook patched NOTHING at all.  That
    was wrong once urllib was recognised as a real LLM transport — hevolveai's
    distillation engine reaches llama-server through
    ``urllib.request.urlopen`` (qwen_llamacpp_wrapper.py:301), and urllib is
    stdlib, so it is ALWAYS available.  Bailing out on a missing httpx left the
    gate fully open for exactly the caller that was already escaping it.

    Safe to change: the only production caller is
    ``hart_intelligence_entry.py:873``, which invokes
    ``_install_outbound_hook()`` and DISCARDS the return value — no branch
    anywhere depends on the old False.  Verified by grep over both repos.
    """
    _reset_module(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, 'httpx', None)
    import core.llm_outbound_logger as mod
    assert mod.install() is True, (
        'a missing httpx must no longer abort the install — urllib is stdlib '
        'and is itself an LLM transport, so the hook must still cover it')
    assert mod.is_installed() is True


def test_non_target_request_passes_through_untouched(
        tmp_path, monkeypatch):
    """A POST to a different port must NOT be modified and must NOT
    log a JSONL record."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    import core.llm_outbound_logger as mod
    mod.install()

    body = b'{"messages": []}'
    request = _FakeRequest(
        body=body,
        url=_FakeURL(port=8081, path='/v1/chat/completions'),  # draft port
    )
    fake_httpx.Client.send(_FakeClient(), request)

    # Body untouched
    assert fake_httpx._captured['send_calls'][0]['content'] == body
    # No JSONL written
    log = tmp_path / 'llm_outbound.jsonl'
    assert not log.exists() or log.read_text() == ''


def test_target_request_stamps_headers_does_not_mutate_body(
        tmp_path, monkeypatch):
    """The target endpoint sees: (1) ``X-HARTOS-Request-ID`` header
    stamped on the outgoing request, (2) Content-Length ALWAYS matches the body
    on the wire (the wire-trim may pin max_tokens / trim to budget, but the
    header can never drift from the bytes — that drift was the 2026-05-12 16:48
    LocalProtocolError storm), and (3) JSONL record appended with the same
    info."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    fake_tl = types.ModuleType('threadlocal')
    # Model the REAL ThreadLocalData contract: request_id is read via the
    # get_request_id() accessor (stored in _local), not a bare instance attr.
    fake_tl.thread_local_data = types.SimpleNamespace(
        get_request_id=lambda: 'req-abc-1234')
    monkeypatch.setitem(sys.modules, 'hartos.threadlocal', fake_tl)
    import core.llm_outbound_logger as mod
    mod.install()

    original_body = json.dumps({
        'model': 'qwen3.5-4b',
        'messages': [{'role': 'user', 'content': 'IPL scores?'}],
    }).encode('utf-8')
    request = _FakeRequest(body=original_body)
    fake_httpx.Client.send(_FakeClient(), request)

    sent = fake_httpx._captured['send_calls'][0]
    # CRITICAL invariant (updated 2026-08-31): the wire-trim feature
    # (181e7415 "pin max_tokens when the producer omits it", 26819d22 "the pin
    # now reaches the wire on the under-budget path") DELIBERATELY re-enabled
    # body rewrite — reversing the 2026-05-12 "drop body-rewrite" decision, but
    # SAFELY: it re-sets Content-Length whenever it re-encodes the body
    # (llm_outbound_logger.py:773 / "Trim BEFORE annotating headers so
    # content-length matches"). So the property that actually prevents the
    # LocalProtocolError storm is Content-Length CONSISTENCY, not byte
    # immutability. Assert THAT — a rewrite that forgets Content-Length is the
    # real regression this guards, and it stays well-formed + keeps the user msg.
    assert sent['content_length'] == str(len(sent['content'])), (
        f"Content-Length header {sent['content_length']!r} != body bytes "
        f"({len(sent['content'])}) on the wire — the exact 2026-05-12 drift "
        f"that caused the LocalProtocolError storm.")
    parsed = json.loads(sent['content'])
    assert parsed['model'] == 'qwen3.5-4b'
    assert parsed['messages'][-1]['content'] == 'IPL scores?'
    # Header stamped (this part stays — headers are cheap and llama.cpp ignores unknown ones)
    assert request.headers.get('X-HARTOS-Request-ID') == 'req-abc-1234'
    assert 'X-HARTOS-Source' not in request.headers

    log = tmp_path / 'llm_outbound.jsonl'
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec['request_id'] == 'req-abc-1234'
    assert rec['source'] == ''
    assert rec['response_status'] == 200
    # Body was logged from what we PARSED, not what we mutated —
    # so 'user' is absent (we never added it).
    assert 'user' not in rec['body']
    assert ',' in rec['ts']


def test_source_context_stamps_headers_and_tags_log(
        tmp_path, monkeypatch):
    """When the caller wraps an LLM call in ``source_context``, the
    JSONL record's ``source`` field carries the label AND headers
    (X-HARTOS-Source + X-HARTOS-Request-ID) are stamped on the wire
    request.  The request body BYTES are NOT mutated (see
    LocalProtocolError regression on 2026-05-12)."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    fake_tl = types.ModuleType('threadlocal')
    fake_tl.thread_local_data = types.SimpleNamespace(
        get_request_id=lambda: 'req-x')
    monkeypatch.setitem(sys.modules, 'hartos.threadlocal', fake_tl)
    import core.llm_outbound_logger as mod
    mod.install()

    original_body = json.dumps({'model': 'm', 'messages': []}).encode('utf-8')
    request = _FakeRequest(body=original_body)

    with mod.source_context('autogen.create'):
        fake_httpx.Client.send(_FakeClient(), request)

    sent = fake_httpx._captured['send_calls'][0]
    # Body untouched
    assert sent['content'] == original_body
    # Headers stamped on the actual request that went out
    assert request.headers.get('X-HARTOS-Source') == 'autogen.create'
    assert request.headers.get('X-HARTOS-Request-ID') == 'req-x'

    rec = json.loads(
        (tmp_path / 'llm_outbound.jsonl').read_text().splitlines()[0])
    assert rec['source'] == 'autogen.create'
    assert rec['request_id'] == 'req-x'


def test_source_context_restores_prior_value_on_exit(
        tmp_path, monkeypatch):
    """``with source_context('X'):`` must unset on exit so a later
    untagged call doesn't accidentally inherit the tag."""
    _reset_module(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, 'httpx', None)
    import core.llm_outbound_logger as mod
    assert mod._get_source() == ''
    with mod.source_context('langchain.main'):
        assert mod._get_source() == 'langchain.main'
    assert mod._get_source() == ''


def test_caller_supplied_user_field_preserved(tmp_path, monkeypatch):
    """If the caller already set ``user`` in their body (e.g.
    autogen llm_config ``extra_body={'user': ...}``), we don't touch
    it — body bytes are never mutated.  Logged record carries
    whatever the caller put in."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    import core.llm_outbound_logger as mod
    mod.install()

    body = json.dumps({
        'model': 'm', 'messages': [], 'user': 'caller-supplied',
    }).encode('utf-8')
    request = _FakeRequest(body=body)
    fake_httpx.Client.send(_FakeClient(), request)

    sent_body = json.loads(
        fake_httpx._captured['send_calls'][0]['content'].decode('utf-8'))
    assert sent_body['user'] == 'caller-supplied'
    rec = json.loads(
        (tmp_path / 'llm_outbound.jsonl').read_text().splitlines()[0])
    assert rec['body']['user'] == 'caller-supplied'


def test_body_retention_policy_off(tmp_path, monkeypatch):
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    monkeypatch.setenv('HEVOLVE_LLM_OUTBOUND_BODY', 'off')
    import core.llm_outbound_logger as mod
    mod.install()

    body = json.dumps({
        'model': 'm',
        'messages': [{'role': 'user', 'content': 'sensitive data ' * 50}],
        'tools': [{'name': 't1'}],
    }).encode('utf-8')
    fake_httpx.Client.send(_FakeClient(), _FakeRequest(body=body))

    log = tmp_path / 'llm_outbound.jsonl'
    rec = json.loads(log.read_text().splitlines()[0])
    # Off mode: only header-fields kept
    assert rec['body'].keys() == {'model', 'n_messages', 'n_tools'}
    assert 'sensitive data' not in json.dumps(rec)


def test_body_retention_policy_trim(tmp_path, monkeypatch):
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    monkeypatch.setenv('HEVOLVE_LLM_OUTBOUND_BODY', 'trim')
    import core.llm_outbound_logger as mod
    mod.install()

    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'u1'},
        {'role': 'assistant', 'content': 'a1'},
        {'role': 'user', 'content': 'u2'},
        {'role': 'assistant', 'content': 'a2'},
        {'role': 'user', 'content': 'final'},
    ]
    body = json.dumps({'model': 'm', 'messages': msgs}).encode('utf-8')
    fake_httpx.Client.send(_FakeClient(), _FakeRequest(body=body))

    log = tmp_path / 'llm_outbound.jsonl'
    rec = json.loads(log.read_text().splitlines()[0])
    logged_msgs = rec['body']['messages']
    # First 2 kept, middle collapsed, last 1 kept
    assert any(m.get('role') == 'collapsed' for m in logged_msgs)
    assert logged_msgs[-1]['content'] == 'final'
    assert logged_msgs[0]['role'] == 'system'


def test_log_outbound_public_helper_for_non_httpx_callers(
        tmp_path, monkeypatch):
    """Public helper for the dispatcher's ``requests.post`` path."""
    _reset_module(monkeypatch, tmp_path)
    # No httpx needed for this path.
    monkeypatch.setitem(sys.modules, 'httpx', None)
    import core.llm_outbound_logger as mod

    mod.log_outbound(
        {'model': 'draft', 'messages': [{'role': 'user', 'content': 'hi'}]},
        response_status=200,
        latency_ms=42.5,
        source='dispatcher.draft',
    )
    log = tmp_path / 'llm_outbound.jsonl'
    assert log.exists()
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec['source'] == 'dispatcher.draft'
    assert rec['body']['model'] == 'draft'
    assert rec['latency_ms'] == 42.5


def test_failure_during_log_does_not_break_request(tmp_path, monkeypatch):
    """Disk-full / write-error must NOT propagate — we never want the
    chat path to fail because logging failed."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    import core.llm_outbound_logger as mod
    mod.install()
    # Sabotage _open_log_handle to always raise
    monkeypatch.setattr(mod, '_open_log_handle',
                        lambda: (_ for _ in ()).throw(OSError('disk full')))

    body = json.dumps({'model': 'm', 'messages': []}).encode('utf-8')
    request = _FakeRequest(body=body)
    # Must not raise
    resp = fake_httpx.Client.send(_FakeClient(), request)
    assert resp.status_code == 200


# ── Port resolution (#86): capture the MAIN model port, not just legacy 8082 ──

def test_target_ports_includes_resolved_main_port_and_legacy(monkeypatch):
    """The hook must watch the MAIN model port (the one autogen actually calls
    via get_local_llm_url — default :8080) AND keep legacy :8082. Watching only
    8082 made every main-model/autogen call invisible to this log and silently
    skipped the n_ctx trim + the background-yield routing for it."""
    import core.port_registry as pr
    import core.llm_outbound_logger as mod
    monkeypatch.setattr(pr, 'get_local_llm_url',
                        lambda: 'http://127.0.0.1:8080/v1', raising=False)
    monkeypatch.setattr(pr, 'get_local_draft_url', lambda: '', raising=False)
    monkeypatch.setattr(pr, 'get_port',
                        lambda s: 8080 if s == 'llm' else 0, raising=False)
    mod._target_ports_cache = None
    try:
        ports = mod._target_ports()
        assert 8080 in ports   # main model now captured
        assert 8082 in ports   # legacy/draft still captured
    finally:
        mod._target_ports_cache = None


def test_is_target_request_matches_main_and_legacy_ports(monkeypatch):
    import types
    import core.port_registry as pr
    import core.llm_outbound_logger as mod
    monkeypatch.setattr(pr, 'get_local_llm_url',
                        lambda: 'http://127.0.0.1:8080/v1', raising=False)
    monkeypatch.setattr(pr, 'get_local_draft_url', lambda: '', raising=False)
    mod._target_ports_cache = None
    try:
        def _u(port):
            return types.SimpleNamespace(port=port, path='/v1/chat/completions')
        assert mod._is_target_request(_u(8080), 'POST') is True   # main model
        assert mod._is_target_request(_u(8082), 'POST') is True   # legacy/draft
        assert mod._is_target_request(_u(9999), 'POST') is False  # unrelated
        assert mod._is_target_request(_u(8080), 'GET') is False   # POST only
    finally:
        mod._target_ports_cache = None


def test_target_ports_follows_dynamic_port_reassignment(monkeypatch):
    """The llama-server port is NOT fixed — Nunba assigns it dynamically and
    reassigns on port-conflict/restart (get_local_llm_url follows it). The hook
    must FOLLOW the move, not freeze on the first value. A permanent cache (the
    original #86 fix) re-blinded the log the moment the server changed ports;
    this guards the regression by asserting caches-within-TTL AND follows-after.
    """
    import core.port_registry as pr
    import core.llm_outbound_logger as mod
    monkeypatch.setattr(pr, 'get_port',
                        lambda s: 8080 if s == 'llm' else 0, raising=False)
    monkeypatch.setattr(pr, 'get_local_draft_url', lambda: '', raising=False)
    mod._target_ports_cache = None
    try:
        # Server first comes up on 8090.
        monkeypatch.setattr(pr, 'get_local_llm_url',
                            lambda: 'http://127.0.0.1:8090/v1', raising=False)
        assert 8090 in mod._target_ports()

        # It moves to 8091 — within the TTL the hook keeps the cached value
        # (cheap hot path, no re-probe) and does not yet see the new port.
        monkeypatch.setattr(pr, 'get_local_llm_url',
                            lambda: 'http://127.0.0.1:8091/v1', raising=False)
        ports_within_ttl = mod._target_ports()
        assert 8090 in ports_within_ttl and 8091 not in ports_within_ttl

        # Age the cache past the TTL → the hook RE-RESOLVES and follows the move.
        cached_ports, _ = mod._target_ports_cache
        mod._target_ports_cache = (cached_ports, 0.0)  # resolved_at = epoch
        assert 8091 in mod._target_ports()
    finally:
        mod._target_ports_cache = None


# ─── Response-side tool-call capture (#787 / D21) ─────────────────────
#
# Why this exists.  On 2026-09-06 a live drive measured that 29 of 55 distinct
# tool_calls reaching the wire carried EMPTY arguments ``{}``, and 16 of the
# resulting real tool results were ``TypeError: ... missing 1 required
# positional argument``.  The schema branch was eliminated — all four tools
# correctly advertise ``required`` — which leaves exactly two candidates:
#
#   (a) the model GENERATED ``{}``, or
#   (b) arguments were STRIPPED somewhere between generation and what autogen
#       replays back into the next request body.
#
# Those two demand opposite fixes, and NOTHING on this box could tell them
# apart: llm_outbound.jsonl records request bodies only, llama_server_8080.log
# carries slot/timing metrics with no content, and server.log dumps the
# request side.  The ``tool_calls`` visible in a request body are autogen's
# RE-SERIALISATION of an earlier response — post-parse — so they are evidence
# about (b) contaminated by (a).
#
# This module is already the response chokepoint (it records response_status
# per call), so the raw completion is recorded HERE rather than in a second
# logger.  Extend, don't fork — see the module docstring's "why this lives at
# the httpx layer" argument, which applies identically to the response.


def _completion(tool_calls, finish_reason='tool_calls'):
    return {'choices': [{'finish_reason': finish_reason,
                         'message': {'role': 'assistant',
                                     'tool_calls': tool_calls}}]}


def _last_record(log_path):
    lines = [ln for ln in log_path.read_text(encoding='utf-8').splitlines() if ln]
    assert lines, 'no JSONL record was written at all'
    return json.loads(lines[-1])


def _drive_one_send(tmp_path, monkeypatch, response_factory):
    """Install the patch against a fake httpx and fire ONE target POST."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module(response_factory=response_factory)
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    import core.llm_outbound_logger as mod
    mod.install()
    request = _FakeRequest(body=b'{"messages": [{"role": "user", "content": "go"}]}')
    fake_httpx.Client.send(_FakeClient(), request)
    mod._close_handle()
    return _last_record(tmp_path / 'llm_outbound.jsonl')


def test_response_tool_call_arguments_are_recorded_verbatim(
        tmp_path, monkeypatch):
    """The RAW ``arguments`` string as the model returned it.

    Verbatim matters more than parsed: ``{}``, ``''`` and absent are three
    different generation outcomes with three different fixes, and any
    normalisation here would erase exactly the distinction #787 needs.
    """
    payload = _completion([
        {'id': 'c1', 'type': 'function',
         'function': {'name': 'execute_windows_or_android_command',
                      'arguments': '{"instructions":"open LinkedIn",'
                                   '"os_to_control":"windows"}'}},
    ])
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload))
    calls = rec.get('response_tool_calls')
    assert calls, (
        'the JSONL record must carry the response tool calls — without them '
        'there is no record anywhere of what the model actually generated, '
        'which is the whole of #787')
    assert calls[0]['name'] == 'execute_windows_or_android_command'
    assert calls[0]['arguments'] == ('{"instructions":"open LinkedIn",'
                                     '"os_to_control":"windows"}')


def test_empty_arguments_are_recorded_as_emitted_not_normalised(
        tmp_path, monkeypatch):
    """THE decisive case: an empty-argument call must survive to the log.

    If this is what the model emits, #787's fix belongs in generation
    (prompt / schema / grammar).  If the model emits real arguments and the
    request body still shows ``{}``, the fix belongs in the parse-and-replay
    path.  The log must be able to say which — so an empty ``{}`` has to be
    recorded, never dropped as "nothing interesting".
    """
    payload = _completion([
        {'id': 'c2', 'type': 'function',
         'function': {'name': 'save_data_in_memory', 'arguments': '{}'}},
    ])
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload))
    calls = rec.get('response_tool_calls')
    assert calls and len(calls) == 1
    assert calls[0]['arguments'] == '{}', (
        'an empty arguments object is the SIGNAL, not noise — it must be '
        'recorded exactly as emitted')
    assert calls[0]['finish_reason'] == 'tool_calls', (
        'finish_reason distinguishes a deliberate tool call from a "length" '
        'truncation that merely looks like one')


def test_response_without_tool_calls_records_an_empty_list(
        tmp_path, monkeypatch):
    """Readable-but-none must be distinguishable from not-readable.

    A plain prose completion records ``[]``; a response whose body was never
    buffered records no key at all.  Collapsing those two into one value is
    how a measurement quietly becomes a guess.
    """
    payload = {'choices': [{'finish_reason': 'stop',
                            'message': {'role': 'assistant',
                                        'content': 'done'}}]}
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload))
    assert rec.get('response_tool_calls') == []


def test_streaming_response_is_never_consumed_by_the_logger(
        tmp_path, monkeypatch):
    """A logging hook may not eat the caller's stream.

    ``_FakeResponse(buffered=False)`` raises on ``.content`` exactly as httpx
    does before ``read()``.  The extraction must not touch it: the send must
    still return normally, and the record must simply omit the key.
    """
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(buffered=False))
    assert 'response_tool_calls' not in rec, (
        'an unread streaming body must yield NO response_tool_calls key — '
        'reading it here would consume the stream the real caller needs')
    assert rec['response_status'] == 200, (
        'the send itself must be unaffected')


def test_extraction_failure_never_breaks_the_send(tmp_path, monkeypatch):
    """Fail-open: a malformed body is logged without tool calls, not raised."""

    class _Garbage(_FakeResponse):
        def __init__(self):
            self.status_code = 200
            self._content = b'<html>llama-server said no</html>'

    rec = _drive_one_send(tmp_path, monkeypatch, _Garbage)
    assert rec['response_status'] == 200
    assert rec.get('response_tool_calls') in (None, [])


def test_argument_string_is_capped_so_the_log_stays_bounded(
        tmp_path, monkeypatch):
    """PERF-2 discipline: this file is already capped by size; one runaway
    arguments blob must not eat that budget."""
    huge = '{"content":"' + ('x' * 5000) + '"}'
    payload = _completion([
        {'id': 'c3', 'type': 'function',
         'function': {'name': 'create_social_post', 'arguments': huge}},
    ])
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload))
    got = rec['response_tool_calls'][0]['arguments']
    import core.llm_outbound_logger as mod
    assert len(got) <= mod._RESP_ARG_CAP + 32
    assert got.startswith('{"content":"xxx')

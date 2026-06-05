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


def _build_fake_httpx_module():
    """Build a minimal stand-in for the ``httpx`` module so the patch
    can be installed without polluting the real one."""
    mod = types.ModuleType('httpx')
    captured = {'send_calls': []}

    def _send(self, request, **kwargs):
        captured['send_calls'].append({
            'content': bytes(request._content or b''),
            'content_length': request.headers.get('content-length'),
            'url': str(request.url),
        })
        resp = MagicMock()
        resp.status_code = 200
        return resp

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


def test_install_returns_false_when_httpx_missing(tmp_path, monkeypatch):
    _reset_module(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, 'httpx', None)
    import core.llm_outbound_logger as mod
    assert mod.install() is False
    assert mod.is_installed() is False


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
    stamped on the outgoing request, (2) request body BYTES UNCHANGED
    (no Content-Length drift, no LocalProtocolError — see 2026-05-12
    16:48 live regression that motivated dropping body-rewrite), and
    (3) JSONL record appended with all the same info."""
    _reset_module(monkeypatch, tmp_path)
    fake_httpx = _build_fake_httpx_module()
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    fake_tl = types.ModuleType('threadlocal')
    # Model the REAL ThreadLocalData contract: request_id is read via the
    # get_request_id() accessor (stored in _local), not a bare instance attr.
    fake_tl.thread_local_data = types.SimpleNamespace(
        get_request_id=lambda: 'req-abc-1234')
    monkeypatch.setitem(sys.modules, 'threadlocal', fake_tl)
    import core.llm_outbound_logger as mod
    mod.install()

    original_body = json.dumps({
        'model': 'qwen3.5-4b',
        'messages': [{'role': 'user', 'content': 'IPL scores?'}],
    }).encode('utf-8')
    request = _FakeRequest(body=original_body)
    fake_httpx.Client.send(_FakeClient(), request)

    sent = fake_httpx._captured['send_calls'][0]
    # CRITICAL invariant: the body bytes that went on the wire MUST
    # equal what the caller (autogen/openai-python) produced.  If we
    # ever rewrite the body again, this assertion catches the regression.
    assert sent['content'] == original_body, (
        f'body bytes were mutated! Caused LocalProtocolError storm '
        f'in 2026-05-12 16:48 live evidence.  Bytes: {sent["content"]!r}'
    )
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
    monkeypatch.setitem(sys.modules, 'threadlocal', fake_tl)
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

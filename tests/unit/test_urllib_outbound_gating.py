"""``urllib.request`` chat-completions must be logged AND slot-scheduled.

Root cause (measured live 2026-08-11): ``core.llm_outbound_logger.install()``
patched ONLY ``httpx`` (sync + async).  ``hevolveai``'s distillation engine
reaches llama-server through
``hevolveai/embodied_ai/models/qwen_llamacpp_wrapper.py:301``::

    with urllib.request.urlopen(req, timeout=self.timeout) as response:

so every one of its calls was:

  * absent from ``llm_outbound.jsonl`` — 1,166 records carried exactly three
    sources (``autogen.create`` 898, ``dispatcher.draft`` 257,
    ``autogen.gather`` 11) and ZERO hevolveai rows, despite 191 synthetic
    distillation queries having been generated and served that session;
  * never admitted through ``core.llama_scheduler`` — so it consumed a real
    llama-server slot OUTSIDE the scheduler's accounting.  ``/props`` reported
    ``total_slots = 2``; the scheduler can only bound the callers it sees, so
    actual server concurrency could exceed 2 while ``stats()`` reported 2.

The defect class is "the gate is keyed on TRANSPORT": the interceptor's
contract is *catch every outbound LLM call*, and a caller using a third HTTP
library escaped it silently.  These tests pin the contract by behaviour, not by
transport, so a fourth library added later fails loudly here.

NOTE ON SCOPE — deliberately NOT asserted:
  * No left-trim for the urllib path.  ``_apply_trim_to_request`` manipulates
    httpx internals and is not reusable here; trimming is a separate concern.
  * No cancel_fn.  ``close_bg_llm_http_client`` closes the httpx background
    client, which would NOT abort a urllib socket — passing it would preempt an
    unrelated call.  urllib daemon calls are therefore yieldable and
    slot-bounded but not mid-flight cancellable.  See the implementation note.
"""
import importlib
import json
import sys
import urllib.request

import pytest


def _fresh_module():
    """Re-import the logger so ``_installed`` starts False in every test.

    Mirrors tests/unit/test_llm_outbound_logger.py::_reset_module — the module
    keeps install state in globals, so a stale import would make install() a
    no-op and silently vacate these assertions.
    """
    if 'core.llm_outbound_logger' in sys.modules:
        del sys.modules['core.llm_outbound_logger']
    return importlib.import_module('core.llm_outbound_logger')


class _FakeResponse:
    status = 200

    def read(self):
        return b'{"choices":[]}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingScheduler:
    """Stands in for the real LlamaScheduler; records every admission."""

    def __init__(self):
        self.acquired = []

    def slot(self, rid, kind='daemon', cancel_fn=None, timeout=None):
        self.acquired.append({'rid': rid, 'kind': kind,
                              'cancel_fn': cancel_fn, 'timeout': timeout})

        class _CM:
            def __enter__(_self):
                return object()

            def __exit__(_self, *exc):
                return False

        return _CM()


TARGET_URL = 'http://127.0.0.1:8080/v1/chat/completions'
BODY = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}]}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Install the hook over a FAKE urlopen so no socket is ever opened."""
    monkeypatch.delenv('HEVOLVE_LLM_OUTBOUND_DISABLE', raising=False)
    mod = _fresh_module()

    log_path = tmp_path / 'llm_outbound.jsonl'
    monkeypatch.setattr(mod, '_get_log_path', lambda: str(log_path))
    # Pin the target port so the test never depends on a live llama-server.
    monkeypatch.setattr(mod, '_target_ports', lambda: {8080})

    seen = []

    def _fake_urlopen(url, data=None, *args, **kwargs):
        seen.append({'url': url, 'data': data,
                     'args': args, 'kwargs': kwargs})
        return _FakeResponse()

    # Replace BEFORE install() so the patch wraps the fake, not the real one.
    monkeypatch.setattr(urllib.request, 'urlopen', _fake_urlopen)

    sched = _RecordingScheduler()
    monkeypatch.setitem(
        sys.modules, 'core.llama_scheduler',
        type(sys)('core.llama_scheduler'))
    sys.modules['core.llama_scheduler'].get_scheduler = lambda: sched

    assert mod.install() is True, 'install() must report a first install'

    def _records():
        if not log_path.exists():
            return []
        return [json.loads(x) for x in
                log_path.read_text(encoding='utf-8').splitlines() if x.strip()]

    return {'mod': mod, 'seen': seen, 'sched': sched, 'records': _records}


def _post_target():
    req = urllib.request.Request(
        TARGET_URL, data=json.dumps(BODY).encode('utf-8'), method='POST')
    return urllib.request.urlopen(req, timeout=5)


def test_urllib_chat_completion_is_logged(harness):
    """RED before the fix: zero records — the call bypassed the hook entirely."""
    _post_target()

    records = harness['records']()
    assert len(records) == 1, (
        'a POST to the llama-server chat-completions endpoint via '
        'urllib.request.urlopen produced no llm_outbound.jsonl record — the '
        'interceptor is blind to this transport, exactly the gap that hid '
        "hevolveai's 191 synthetic distillation queries.")
    rec = records[0]
    assert rec['body']['messages'][0]['content'] == 'hi'
    assert rec['response_status'] == 200
    assert rec['latency_ms'] is not None
    assert rec['source'], 'record must carry a non-empty source label'


def test_urllib_chat_completion_acquires_a_scheduler_slot(harness):
    """The whole point: this traffic must be BOUNDED by --parallel, not free.

    Without this, llama-server can be handed more concurrent work than it has
    slots while core.llama_scheduler.stats() still reports <= n_slots.
    """
    _post_target()

    acquired = harness['sched'].acquired
    assert len(acquired) == 1, (
        'urllib chat-completion did not pass through '
        'core.llama_scheduler.slot() — it takes a real llama-server slot '
        "outside the scheduler's accounting, so the "
        '"in-flight <= slots" invariant is unenforceable for this caller.')
    assert acquired[0]['kind'] in ('user', 'daemon')
    assert acquired[0]['timeout'] is not None, (
        'a slot acquisition with no timeout can park a caller forever; the '
        'httpx path uses a bounded wait and this one must too')


def test_untagged_urllib_call_is_classified_daemon(harness):
    """No request-id => background, via the ONE canonical discriminator.

    ``_is_background_call`` delegates to ``dispatch.is_genuine_user_request``
    (empty rid => background).  hevolveai never sets a request-id, so its
    distillation traffic must land as 'daemon' and thus be preemptible/yielding
    rather than competing with a live user turn at equal priority.
    """
    _post_target()

    assert harness['sched'].acquired[0]['kind'] == 'daemon', (
        'an untagged urllib call was admitted as a USER turn — it would then '
        'be non-evictable (_pick_daemon_locked only evicts kind==daemon) and '
        "would outrank the real user's chat turn.")


def test_non_target_urllib_calls_are_not_logged(harness):
    """Zero-regression: /health, /v1/models, downloads must pass through.

    urllib is used all over hevolveai for health probes and model downloads
    (auto_setup.py, qwen_auto_encoder.py, qwen_benchmark.py).  Logging or
    scheduling those would both pollute the record and serialise probes behind
    generation.
    """
    urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)
    urllib.request.urlopen('http://127.0.0.1:8080/v1/models', timeout=5)
    # A POST to a DIFFERENT path on the same port is also out of scope.
    other = urllib.request.Request(
        'http://127.0.0.1:8080/v1/embeddings', data=b'{}', method='POST')
    urllib.request.urlopen(other, timeout=5)

    assert harness['records']() == [], 'non chat-completions traffic was logged'
    assert harness['sched'].acquired == [], (
        'non chat-completions traffic consumed a scheduler slot — health '
        'probes would then queue behind generation')
    assert len(harness['seen']) == 3, 'all three calls must still reach urlopen'


def test_original_urlopen_still_receives_caller_arguments(harness):
    """The wrapper must be transparent — same url object, data and timeout."""
    _post_target()

    assert len(harness['seen']) == 1
    call = harness['seen'][0]
    assert getattr(call['url'], 'full_url', None) == TARGET_URL
    # timeout was passed as a kwarg by the caller; it must survive the wrapper
    assert call['kwargs'].get('timeout') == 5 or 5 in call['args'], (
        'the caller timeout was dropped by the wrapper — a urllib call with no '
        'timeout can hang forever, violating the standing no-timeout-less-IO rule')


def test_response_is_returned_unchanged(harness):
    """Callers use the response as a context manager — do not wrap/consume it."""
    resp = _post_target()
    assert isinstance(resp, _FakeResponse)
    with resp as r:
        assert r.read() == b'{"choices":[]}'

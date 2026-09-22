"""A non-2xx LLM response must record WHAT THE SERVER SAID, not just the code.

THE OBSERVABILITY GAP, measured 2026-09-22 on this box.  Across 1,184 records
in ``llm_outbound.jsonl`` + ``.old``, 60 carry ``response_status: 400``.  Every
one of them stores exactly these keys::

    ['body', 'latency_ms', 'request_id', 'response_status', 'source', 'ts',
     'response_tool_calls']

``response_tool_calls`` is ``[]`` on all of them — correct, since a 400 has no
``choices`` — and there is no other field.  So the file records THAT the server
refused and never WHY.  The reason had to be recovered from a different file
entirely::

    logs/llama_server_8080.log
      srv send_error: task id = 213, error: request (6249 tokens) exceeds the
                      available context size (4096 tokens), try increasing it

and the attribution "these 400s are context overflow" was, until that second
file was read, a correlation between tool counts (23 passing vs 50-65 failing),
not a quoted error.  That is exactly the inference this log exists to make
unnecessary.

WHY HERE AND NOWHERE ELSE.  ``core.llm_outbound_logger`` is the single writer
of this file and already the response chokepoint — it records
``response_status`` and, since #787, the response's ``tool_calls``.  It reads
``response._content``, the buffered body httpx has already fetched, so the
error text is in hand at zero cost and with zero risk of consuming a stream the
real caller still needs.  A second logger, or an extractor at any call site,
would be a parallel path over the same bytes.

CONTRACT.
  * 2xx                          -> no ``response_error`` key at all.
  * non-2xx with a readable body -> ``response_error`` carries the server's
                                    own message, bounded.
  * non-2xx, unreadable/streaming-> no key (absent measurement stays visibly
                                    absent, never a false empty string).

RED BEFORE GREEN: against HEAD ``log_outbound`` has no ``response_error``, so
the first three tests fail.

    python -m pytest tests/unit/test_wire_records_the_error_body.py --noconftest -q
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.unit.test_llm_outbound_logger import (  # noqa: E402
    _FakeResponse, _drive_one_send,
)

# The exact shape llama-server returns for the overflow that produced all 60
# of the measured 400s.
_LLAMA_400 = {
    'error': {
        'code': 400,
        'message': ('the request exceeds the available context size, try '
                    'increasing it. n_prompt_tokens = 6249, n_ctx = 4096'),
        'type': 'exceed_context_size_error',
    }
}


def test_a_400_records_the_servers_own_message(tmp_path, monkeypatch):
    """The whole point: never infer this from a tool count again."""
    rec = _drive_one_send(
        tmp_path, monkeypatch,
        lambda: _FakeResponse(_LLAMA_400, status_code=400))
    assert rec['response_status'] == 400
    err = rec.get('response_error')
    assert err, (
        'a non-2xx record MUST carry the server\'s error text — 60 of 60 '
        'measured 400s stored only the status code, so the cause had to be '
        'recovered from llama_server_8080.log and the attribution was a '
        'correlation, not a quotation')
    assert 'exceed_context_size_error' in err
    assert 'n_ctx = 4096' in err


def test_a_500_records_it_too(tmp_path, monkeypatch):
    """20 of the measured records are 500s.  Same gap, same fix — the rule
    is non-2xx, not ``== 400``."""
    payload = {'error': {'code': 500,
                         'message': 'No user query found in messages.'}}
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload, status_code=500))
    assert 'No user query found in messages.' in (
        rec.get('response_error') or '')


def test_a_non_json_error_page_is_still_recorded(tmp_path, monkeypatch):
    """A proxy's HTML 502 is an answer too.  Falling back to the raw text is
    what stops "unparseable" being logged as "nothing happened"."""

    class _Html(_FakeResponse):
        def __init__(self):
            self.status_code = 502
            self._content = b'<html><body>upstream connect error</body></html>'

    rec = _drive_one_send(tmp_path, monkeypatch, _Html)
    assert 'upstream connect error' in (rec.get('response_error') or '')


def test_a_2xx_carries_no_error_key(tmp_path, monkeypatch):
    """Success must stay clean — an empty ``response_error`` on every good
    call would make the field useless for grepping the bad ones."""
    payload = {'choices': [{'finish_reason': 'stop',
                            'message': {'role': 'assistant',
                                        'content': 'done'}}]}
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(payload, status_code=200))
    assert 'response_error' not in rec


def test_an_unread_streaming_error_omits_the_key_rather_than_faking_one(
        tmp_path, monkeypatch):
    """Absent measurement stays visibly absent.  Reading ``.content`` here
    would consume the stream the real caller is waiting on — the same rule
    ``_response_tool_calls`` already follows."""
    rec = _drive_one_send(
        tmp_path, monkeypatch,
        lambda: _FakeResponse(status_code=400, buffered=False))
    assert rec['response_status'] == 400
    assert 'response_error' not in rec


def test_the_error_text_is_bounded(tmp_path, monkeypatch):
    """PERF-2 discipline: this file is size-capped, so one runaway error page
    may not eat the budget."""
    huge = {'error': {'message': 'x' * 20000}}
    rec = _drive_one_send(tmp_path, monkeypatch,
                          lambda: _FakeResponse(huge, status_code=400))
    import core.llm_outbound_logger as mod
    assert len(rec['response_error']) <= mod._RESP_ERROR_CAP + 32


def test_the_public_helper_accepts_it_too(tmp_path, monkeypatch):
    """``log_outbound`` is the public entry the non-httpx transports use
    (the dispatcher's raw ``requests.post`` draft path).  One record shape
    for every transport, or the field is only half there."""
    from tests.unit.test_llm_outbound_logger import _reset_module
    _reset_module(monkeypatch, tmp_path)
    import core.llm_outbound_logger as mod
    mod.log_outbound({'model': 'm'}, response_status=400,
                     response_error='boom')
    mod._close_handle()
    line = (tmp_path / 'llm_outbound.jsonl').read_text(
        encoding='utf-8').strip().splitlines()[-1]
    assert json.loads(line)['response_error'] == 'boom'

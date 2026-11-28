"""A body the wire is about to cut is measured by the server, not guessed.

MEASURED 2026-11-28 on the owner's desktop (Qwen3.5-4B, n_ctx 12,288, one
slot): 61% of live agent turns went out with the system prompt cut in the
middle (median 25,280 characters removed), while the server's own records
over 7,895 requests never passed 9,925 tokens.  The wire held 2,816 tokens
back from every body in case the server counted more than it did.  The
server answers that question exactly (/apply-template + /tokenize, 60-85 ms,
not held up by a running decode), so the reserve for a body about to be cut
is what the server says, plus the flat overhead reserve.

The real ``_trim_to_budget`` on real bodies; only the window and the
server's answer are given by the test.

    python -m pytest tests/unit/test_the_wire_asks_the_server_before_it_cuts.py -q --noconftest
"""
import pytest

import core.llm_outbound_logger as lol
from core.constants import WIRE_TRIM_SAFETY_MARGIN_TOKENS
from core.token_utils import count_tokens_for_messages

WINDOW = 12288
REPLY = 2048


class _Server:
    """Counts a body as the wire does, plus ``over`` tokens; None when down."""

    def __init__(self, monkeypatch, over=0, up=True):
        self.over, self.up, self.asked = over, up, 0
        monkeypatch.setattr(lol, '_get_budget_per_slot', lambda: WINDOW)
        monkeypatch.setattr(lol, '_server_prompt_tokens', self._count)
        monkeypatch.setattr(lol, '_save_elided', lambda live: True,
                            raising=False)

    def _count(self, body):
        self.asked += 1
        if not self.up:
            return None
        return (count_tokens_for_messages(body['messages'], body.get('model'))
                + lol._schema_tokens(body, body.get('model')) + self.over)


def _body(system_tokens, conversation_tokens=600):
    """A system prompt of about ``system_tokens`` and a short conversation."""
    return {
        'model': 'local', 'max_tokens': REPLY,
        'messages': [
            {'role': 'system', 'content': 'rule %d holds. ' * 1 % 7
             + ' '.join('rule%d' % i for i in range(system_tokens // 2))},
            {'role': 'user', 'content': 'please continue the plan '
             + ' '.join('w%d' % i for i in range(conversation_tokens // 2))},
        ],
    }


def _wire_tokens(body):
    return count_tokens_for_messages(body['messages'], body.get('model'))


def _sized_between(low, high):
    """A body whose wire count lies strictly between two budgets."""
    for system_tokens in range(4000, 14000, 50):
        body = _body(system_tokens)
        if low < _wire_tokens(body) <= high:
            return body
    raise AssertionError('no body between %d and %d' % (low, high))


FIXED_BUDGET = WINDOW - REPLY - WIRE_TRIM_SAFETY_MARGIN_TOKENS
MEASURED_BUDGET = WINDOW - REPLY - lol._PROMPT_OVERHEAD_TOKENS


def test_a_body_that_really_fits_is_not_cut(monkeypatch):
    server = _Server(monkeypatch, over=0)
    body = _sized_between(FIXED_BUDGET + 200, MEASURED_BUDGET - 200)
    sent, dropped, cut_chars, before, after, budget = lol._trim_to_budget(body)
    assert (dropped, cut_chars) == (0, 0)
    assert sent['messages'] == body['messages']
    assert budget == MEASURED_BUDGET and server.asked == 1


def test_the_same_body_is_cut_when_the_server_cannot_be_asked(monkeypatch):
    server = _Server(monkeypatch, up=False)
    body = _sized_between(FIXED_BUDGET + 200, MEASURED_BUDGET - 200)
    _sent, dropped, cut_chars, _b, _a, budget = lol._trim_to_budget(body)
    assert dropped + cut_chars > 0
    assert budget == FIXED_BUDGET and server.asked == 1


def test_a_body_too_big_either_way_loses_less(monkeypatch):
    body = _sized_between(MEASURED_BUDGET + 800, MEASURED_BUDGET + 1500)
    _Server(monkeypatch, up=False)
    _s, _d, cut_without, _b, after_without, _bud = lol._trim_to_budget(dict(body))
    _Server(monkeypatch, over=0)
    _s, _d, cut_with, _b, after_with, budget = lol._trim_to_budget(dict(body))
    assert 0 < cut_with < cut_without
    assert after_with <= budget == MEASURED_BUDGET
    assert after_with > after_without


def test_what_the_server_counts_over_the_wire_is_held_back(monkeypatch):
    _Server(monkeypatch, over=300)
    body = _sized_between(FIXED_BUDGET + 200, MEASURED_BUDGET - 500)
    _s, _d, _c, _b, _a, budget = lol._trim_to_budget(body)
    assert budget == MEASURED_BUDGET - 300


def test_a_server_far_over_the_wire_keeps_the_fixed_reserve(monkeypatch):
    """Never a smaller budget than today's: the reserve is capped at the
    constant it replaces."""
    _Server(monkeypatch, over=9000)
    body = _sized_between(FIXED_BUDGET + 200, MEASURED_BUDGET - 200)
    _s, dropped, cut_chars, _b, _a, budget = lol._trim_to_budget(body)
    assert budget == FIXED_BUDGET and dropped + cut_chars > 0


def test_a_body_that_fits_the_fixed_budget_never_asks(monkeypatch):
    server = _Server(monkeypatch)
    body = _body(2000)
    sent, dropped, cut_chars, _b, _a, budget = lol._trim_to_budget(body)
    assert (dropped, cut_chars, server.asked) == (0, 0, 0)
    assert budget == FIXED_BUDGET


def test_the_server_is_asked_once_for_one_request(monkeypatch):
    """The trim re-runs itself once to make room for what it elided; the
    re-run is handed the first answer."""
    server = _Server(monkeypatch, over=0)
    body = _sized_between(MEASURED_BUDGET + 800, MEASURED_BUDGET + 1500)
    lol._trim_to_budget(body)
    assert server.asked == 1


@pytest.mark.parametrize('answer', [
    {'status': 404},
    {'status': 200, 'template': {'prompt': ''}},
    {'status': 200, 'template': {'prompt': 'x'}, 'tokenize_status': 500},
    {'status': 200, 'template': {'prompt': 'x'}, 'tokens': []},
    {'raises': True},
])
def test_a_server_that_does_not_answer_properly_counts_as_not_asked(
        monkeypatch, answer):
    class _Resp:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload

        def json(self):
            return self._payload

    class _Session:
        def post(self, url, json=None, timeout=None):
            if answer.get('raises'):
                raise ConnectionError('refused')
            if url.endswith('/apply-template'):
                return _Resp(answer['status'], answer.get('template', {}))
            return _Resp(answer.get('tokenize_status', 200),
                         {'tokens': answer.get('tokens', [1, 2, 3])})
    import core.http_pool as pool
    import core.port_registry as ports
    monkeypatch.setattr(pool, 'get_http_session', lambda: _Session())
    monkeypatch.setattr(ports, 'get_local_llm_url',
                        lambda: 'http://127.0.0.1:1/v1')
    assert lol._server_prompt_tokens(
        {'messages': [{'role': 'user', 'content': 'hi'}]}) is None


def test_the_count_is_the_length_of_the_servers_token_list(monkeypatch):
    seen = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Session:
        def post(self, url, json=None, timeout=None):
            seen.append((url, sorted(json)))
            if url.endswith('/apply-template'):
                return _Resp({'prompt': '<|im_start|>user\nhi<|im_end|>'})
            return _Resp({'tokens': list(range(37))})
    import core.http_pool as pool
    import core.port_registry as ports
    monkeypatch.setattr(pool, 'get_http_session', lambda: _Session())
    monkeypatch.setattr(ports, 'get_local_llm_url',
                        lambda: 'http://127.0.0.1:8082/v1')
    body = {'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'type': 'function', 'function': {'name': 't'}}]}
    assert lol._server_prompt_tokens(body) == 37
    assert seen == [
        ('http://127.0.0.1:8082/apply-template', ['messages', 'tools']),
        ('http://127.0.0.1:8082/tokenize', ['content'])]

"""C277: the wearer's typed words reach the world model.

Before this, WorldModelBridge.ingest_sensor_batch skipped every 'text'
reading (only camera and audio had a branch) even though HevolveAI's
/v1/sensor/ingest has accepted modality 'text' since it shipped, and
record_interaction fed the user's prompt to the distillation queue only. So a
person typing to Nunba was never heard by the world model; only the screen
and, with consent, the mic were.

Asserted:
  (1) a 'text' reading posts modality 'text' with the words in `text`, base64
      of the same words in `data`, source 'chat', reality_signature 1.0;
  (2) record_interaction posts the REDACTED prompt as such a reading when the
      user holds data_access '*' consent, and nothing when the user does not;
  (3) HEVOLVE_CHAT_LEARNING=0 switches the leg off; a consent machinery
      error fails closed (no ingest);
  (4) the distillation queue still receives the experience either way.
"""
import base64
import sys
import types

import pytest


class _Resp:
    status_code = 200


class _Now:
    """A flush executor that runs the submitted call immediately."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)


@pytest.fixture
def bridge(monkeypatch):
    import integrations.agent_engine.world_model_bridge as wmb
    b = wmb.WorldModelBridge.__new__(wmb.WorldModelBridge)
    # the minimum ingest_sensor_batch / record_interaction touch
    b._http_disabled = False
    b._cb_is_open = lambda: False
    b._cb_record_success = lambda: None
    b._propagate_embodied_error = lambda *a, **k: None
    b._timeout_flush = 1
    b._lock = __import__('threading').Lock()
    b._stats = {'total_recorded': 0}
    b._api_url = 'http://test'
    b._in_process = True
    b._in_process_retry_done = True
    b._flush_batch_size = 10 ** 6
    b._experience_queue = __import__('collections').deque()
    b._flush_executor = _Now()
    b._persist_to_conversation_entry = lambda **k: None
    posts = []
    monkeypatch.setattr(wmb, 'pooled_post',
                        lambda url, json=None, timeout=None: posts.append((url, json)) or _Resp())
    return wmb, b, posts


def _consent(monkeypatch, value):
    """Install a fake integrations.social consent surface returning `value`
    (or raising when value is an exception)."""
    cs = types.ModuleType('integrations.social.consent_service')

    class ConsentService:
        @staticmethod
        def check_consent(db, user_id, consent_type, scope='*'):
            if isinstance(value, Exception):
                raise value
            assert (consent_type, scope) == ('data_access', '*')
            return value
    cs.ConsentService = ConsentService
    models = types.ModuleType('integrations.social.models')

    class _S:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False
    models.db_session = lambda commit=False: _S()
    monkeypatch.setitem(sys.modules, 'integrations.social.consent_service', cs)
    monkeypatch.setitem(sys.modules, 'integrations.social.models', models)


def test_a_text_reading_is_posted_as_the_text_modality(bridge):
    wmb, b, posts = bridge
    n = b.ingest_sensor_batch([{'sensor_id': 'chat_u1', 'sensor_type': 'text',
                                'data': {'text': 'the red ball', 'stream_source': 'chat'}}])
    assert n == 1 and len(posts) == 1
    url, body = posts[0]
    assert url.endswith('/v1/sensor/ingest')
    assert body['modality'] == 'text' and body['text'] == 'the red ball'
    assert base64.b64decode(body['data']).decode('utf-8') == 'the red ball'
    assert body['source'] == 'chat' and body['reality_signature'] == 1.0
    # an unknown sensor type is still skipped, as before
    assert b.ingest_sensor_batch([{'sensor_type': 'imu', 'data': {'x': 1}}]) == 0


def test_record_interaction_posts_the_prompt_under_consent(bridge, monkeypatch):
    wmb, b, posts = bridge
    monkeypatch.delenv('HEVOLVE_CHAT_LEARNING', raising=False)
    # the redactor is exercised for real if present; the prompt has no secret
    _consent(monkeypatch, True)
    b.record_interaction('u1', 'p1', 'mama gives baby milk', 'ok')
    assert len(b._experience_queue) == 1, 'distillation still receives the pair'
    assert [p[1]['text'] for p in posts] == ['mama gives baby milk']
    assert posts[0][1]['session_id'].startswith('chat_u1')

    posts.clear()
    _consent(monkeypatch, False)
    b.record_interaction('u1', 'p2', 'no consent here', 'ok')
    assert posts == [] and len(b._experience_queue) == 2


def test_flag_off_and_consent_error_both_fail_closed(bridge, monkeypatch):
    wmb, b, posts = bridge
    _consent(monkeypatch, True)
    monkeypatch.setenv('HEVOLVE_CHAT_LEARNING', '0')
    b.record_interaction('u1', 'p3', 'switched off', 'ok')
    assert posts == []
    monkeypatch.delenv('HEVOLVE_CHAT_LEARNING', raising=False)
    _consent(monkeypatch, RuntimeError('db down'))
    b.record_interaction('u1', 'p4', 'consent machinery broken', 'ok')
    assert posts == [], 'a consent error must mean no ingest'
    assert len(b._experience_queue) == 2


# ---------------------------------------------------------------------------
# C281: a failed flush re-queues, a non-2xx answer is a failure (Master 11.376)
# ---------------------------------------------------------------------------
def _flush_bridge(monkeypatch, breaker_open=False):
    import collections
    import threading
    import integrations.agent_engine.world_model_bridge as wmb
    b = wmb.WorldModelBridge.__new__(wmb.WorldModelBridge)
    b._in_process = False
    b._provider = None
    b._http_disabled = False
    b._api_url = 'http://test'
    b._timeout_flush = 1
    b._lock = threading.Lock()
    b._stats = {'total_flushed': 0}
    b._last_flush_at = None
    b._experience_queue = collections.deque(maxlen=5)
    b._is_external_target = lambda: False
    state = {'open': breaker_open, 'fail': 0, 'ok': 0}
    b._cb_is_open = lambda: state['open']
    b._cb_record_failure = lambda: state.__setitem__('fail', state['fail'] + 1)
    b._cb_record_success = lambda: state.__setitem__('ok', state['ok'] + 1)
    return wmb, b, state


def _exps(n):
    return [{'prompt': 'p%d' % i, 'response': 'r%d' % i, 'user_id': 'u', 'prompt_id': 'x%d' % i,
             'source': 'test'} for i in range(n)]


def test_C281_breaker_open_requeues_instead_of_dropping(monkeypatch):
    wmb, b, state = _flush_bridge(monkeypatch, breaker_open=True)
    posts = []
    monkeypatch.setattr(wmb, 'pooled_post', lambda *a, **k: posts.append(k) or _Resp())
    b._flush_to_world_model(_exps(3))
    assert posts == [] and [e['prompt'] for e in b._experience_queue] == ['p0', 'p1', 'p2']
    # bounded by the queue's maxlen: the overflow is counted, not silently lost
    b._flush_to_world_model(_exps(4))
    assert len(b._experience_queue) == 5 and b._stats['total_dropped'] == 2


def test_C281_a_timeout_requeues_the_rest_of_the_batch_and_says_so_once(monkeypatch):
    import requests
    wmb, b, state = _flush_bridge(monkeypatch)
    calls = []

    def post(url, json=None, timeout=None):
        calls.append(json['messages'][1]['content'])
        if len(calls) == 2:
            raise requests.Timeout('read timed out')
        return _Resp()
    monkeypatch.setattr(wmb, 'pooled_post', post)
    warned = []
    monkeypatch.setattr(wmb.logger, 'warning', lambda msg, *a, **k: warned.append(msg % a if a else msg))
    b._flush_to_world_model(_exps(4))
    assert calls == ['p0', 'p1'], 'stop at the first failure'
    assert b._stats['total_flushed'] == 1 and state['fail'] == 1
    assert [e['prompt'] for e in b._experience_queue] == ['p1', 'p2', 'p3'], (
        'the failed one and the rest go back, in order')
    assert len(warned) == 1 and 'Timeout' in warned[0] and '3 experience(s) re-queued' in warned[0]
    # a second failing run is silent; a success re-arms the warning
    b._experience_queue.clear()
    monkeypatch.setattr(wmb, 'pooled_post', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout('again')))
    b._flush_to_world_model(_exps(1))
    assert len(warned) == 1
    b._experience_queue.clear()
    monkeypatch.setattr(wmb, 'pooled_post', lambda *a, **k: _Resp())
    b._flush_to_world_model(_exps(1))
    monkeypatch.setattr(wmb, 'pooled_post', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout('after ok')))
    b._flush_to_world_model(_exps(1))
    assert len(warned) == 2


def test_C281_a_non_2xx_answer_is_a_failure_not_a_flush(monkeypatch):
    wmb, b, state = _flush_bridge(monkeypatch)

    class _Bad:
        status_code = 503
    monkeypatch.setattr(wmb, 'pooled_post', lambda *a, **k: _Bad())
    monkeypatch.setattr(wmb.logger, 'warning', lambda *a, **k: None)
    b._flush_to_world_model(_exps(2))
    assert b._stats['total_flushed'] == 0 and state['fail'] == 1 and state['ok'] == 0
    assert [e['prompt'] for e in b._experience_queue] == ['p0', 'p1']

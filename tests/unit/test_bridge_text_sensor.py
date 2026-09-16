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

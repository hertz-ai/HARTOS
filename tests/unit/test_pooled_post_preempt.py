"""Regression (#162): every LOCAL llama chat-completion issued via the
``requests``-based ``pooled_post`` is admitted through the slot-aware priority
scheduler (core.llama_scheduler) — it classifies user vs daemon, carries the
original rid on the wire, runs a daemon call on the CLOSABLE bg session (so a
user turn can preempt it), and a user call on the shared session.

Before this, ``pooled_post`` always used the single shared session with no
scheduling, so the draft classifier + the casual/main direct calls (the path a
"hi" travels) were invisible to slot/priority/cancel and could never preempt a
daemon holding the single GPU slot.

Behavioural: real ``_classify_llama_call`` + real
``dispatch.is_genuine_user_request`` + real scheduler; the rid source and the
HTTP boundary are mocked.

    python -m pytest tests/unit/test_pooled_post_preempt.py --noconftest -q
"""
import unittest
from unittest.mock import MagicMock, patch

from core import http_pool as H

_LLM = 'http://127.0.0.1:8080/v1/chat/completions'


def _fake_resp():
    r = MagicMock()
    r.json.return_value = {'choices': [{'message': {'content': 'x'}}], 'usage': {}}
    return r


class TestClassify(unittest.TestCase):
    def test_classify_daemon_user_empty(self):
        for rid, expect in [('daemon_g1', 'daemon'), ('user_1', 'user'), ('', 'daemon')]:
            with patch('core.llm_outbound_logger._get_request_id', return_value=rid):
                r, k = H._classify_llama_call()
            self.assertEqual((r, k), (rid, expect))

    def test_session_for_kind(self):
        self.assertIs(H._llama_session_for('user'), H.get_http_session())
        self.assertIs(H._llama_session_for('daemon'), H.get_bg_llm_requests_session())


class TestPooledPostScheduling(unittest.TestCase):
    def setUp(self):
        H._bg_llm_requests_session = None
        H._bg_requests_cancel_registered = False

    def test_user_call_stamps_rid_and_uses_shared_session(self):
        shared = MagicMock(); shared.post.return_value = _fake_resp()
        body = {'model': 'llama', 'messages': [{'role': 'user', 'content': 'hi'}]}
        with patch('core.http_pool.get_http_session', return_value=shared), \
             patch('core.llm_outbound_logger._get_request_id', return_value='user_1'):
            H.pooled_post(_LLM, json=body)
        shared.post.assert_called_once()
        self.assertEqual(body.get('user'), 'user_1')      # rid carried on the wire

    def test_daemon_call_uses_closable_bg_session(self):
        bg = MagicMock(); bg.post.return_value = _fake_resp()
        shared = MagicMock(); shared.post.return_value = _fake_resp()
        with patch('core.http_pool.get_bg_llm_requests_session', return_value=bg), \
             patch('core.http_pool.get_http_session', return_value=shared), \
             patch('core.llm_outbound_logger._get_request_id', return_value='daemon_g1'):
            H.pooled_post(_LLM, json={'messages': []})
        bg.post.assert_called_once()                       # daemon → closable bg session
        shared.post.assert_not_called()

    def test_non_llama_url_bypasses_scheduler_and_stamp(self):
        shared = MagicMock(); shared.post.return_value = _fake_resp()
        body = {'a': 1}
        with patch('core.http_pool.get_http_session', return_value=shared):
            H.pooled_post('http://127.0.0.1:9999/api/foo', json=body)
        shared.post.assert_called_once()
        self.assertNotIn('user', body)                    # no rid stamp on non-llama

    def test_bg_session_registered_as_cancellable(self):
        import core.foreground as F
        H.get_bg_llm_requests_session()
        self.assertIn(H.close_bg_llm_requests_session, F._cancellables)

    def test_close_clears_bg_session(self):
        H.get_bg_llm_requests_session()
        H.close_bg_llm_requests_session()
        self.assertIsNone(H._bg_llm_requests_session)


class TestMainPortScoping(unittest.TestCase):
    """Task #10 (draft-LLM scheduler bypass): the scheduler gate is scoped to
    the MAIN llama-server's port. A localhost llama URL on a DIFFERENT port
    (the draft server, own slots) must NOT contend for main slots; an unknown
    main port fails OPEN (everything local is treated as main — never
    accidentally unscheduled)."""

    def setUp(self):
        H._main_llama_port_cache = None   # re-resolve per test

    def tearDown(self):
        H._main_llama_port_cache = None

    def test_main_port_url_is_gated(self):
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8082/v1'):
            self.assertTrue(H._is_llama_completion_url(
                'http://127.0.0.1:8082/v1/chat/completions'))

    def test_other_local_port_is_not_gated(self):
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8082/v1'):
            self.assertFalse(H._is_llama_completion_url(
                'http://127.0.0.1:8081/v1/chat/completions'))

    def test_unknown_main_port_fails_open(self):
        with patch('core.port_registry.get_local_llm_url', return_value=None):
            self.assertTrue(H._is_llama_completion_url(
                'http://127.0.0.1:8081/v1/chat/completions'))

    def test_non_llama_urls_unchanged(self):
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8082/v1'):
            self.assertFalse(H._is_llama_completion_url(
                'http://127.0.0.1:6777/chat'))
            self.assertFalse(H._is_llama_completion_url(
                'https://api.openai.com/v1/chat/completions'))

    def test_main_port_call_acquires_scheduler_draft_port_does_not(self):
        """Behavioural: pooled_post to the main port goes through the
        scheduler's slot(); the draft port skips it entirely."""
        with patch('core.port_registry.get_local_llm_url',
                   return_value='http://127.0.0.1:8082/v1'):
            sched = MagicMock()
            sched.slot.return_value.__enter__ = MagicMock(return_value=None)
            sched.slot.return_value.__exit__ = MagicMock(return_value=False)
            with patch('core.llama_scheduler.get_scheduler', return_value=sched),                  patch.object(H, 'get_http_session') as gs,                  patch.object(H, 'get_bg_llm_requests_session') as gbg,                  patch('core.llm_outbound_logger._get_request_id', return_value='user_1'):
                gs.return_value.post.return_value = _fake_resp()
                gbg.return_value.post.return_value = _fake_resp()

                H.pooled_post('http://127.0.0.1:8082/v1/chat/completions',
                              json={'messages': []})
                self.assertEqual(sched.slot.call_count, 1)

                H.pooled_post('http://127.0.0.1:8081/v1/chat/completions',
                              json={'messages': []})
                self.assertEqual(sched.slot.call_count, 1)  # unchanged


if __name__ == '__main__':
    unittest.main()

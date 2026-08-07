"""Unit tests for the streaming-STT WS handler's optional ``?call_id=``
hook (UNIF-G7 / W1.7 Producer C).

The hook lets RN/web mic clients opt in to landing each final
transcript segment in the canonical per-call STT queue
(``whisper_tool.enqueue_stt_segment``) so the AgentBridgeWorker
(``agent_voice_bridge._tick``) can drain it.

Zero-regression contract:
  - Plain transcription clients (no ``?call_id=`` query) see ZERO
    behavior change — the helper short-circuits to a no-op.
  - Producer-side errors (queue cap, unknown call) are swallowed
    inside the WS handler hot path; they never bubble up.

These tests exercise the SMALL helpers in isolation so we don't have
to spin up the actual whisper subprocess + websockets server.
"""
from __future__ import annotations

import ast
import unittest


class WsPathParserTest(unittest.TestCase):

    def test_websockets_11_request_path(self):
        """websockets 11+ exposes the path via ``ws.request.path``."""
        from integrations.service_tools.whisper_tool import _ws_path

        class _Req:
            path = '/?call_id=room-1&user_id=alice'

        class _WS:
            request = _Req()

        self.assertEqual(_ws_path(_WS()),
                         '/?call_id=room-1&user_id=alice')

    def test_websockets_10_path_attr(self):
        """websockets 10.x exposed it as ``ws.path``."""
        from integrations.service_tools.whisper_tool import _ws_path

        class _WS:
            path = '/?call_id=room-2'

        self.assertEqual(_ws_path(_WS()), '/?call_id=room-2')

    def test_no_path_returns_empty(self):
        from integrations.service_tools.whisper_tool import _ws_path

        class _WS:
            pass

        self.assertEqual(_ws_path(_WS()), '')

    def test_garbage_value_returns_empty(self):
        """Non-string path → empty (defensive)."""
        from integrations.service_tools.whisper_tool import _ws_path

        class _WS:
            path = 12345

        self.assertEqual(_ws_path(_WS()), '')


class CallContextParserTest(unittest.TestCase):

    def test_extracts_call_id_and_user_id(self):
        from integrations.service_tools.whisper_tool import _parse_call_context
        cid, uid = _parse_call_context('/?call_id=room-1&user_id=alice')
        self.assertEqual(cid, 'room-1')
        self.assertEqual(uid, 'alice')

    def test_call_id_only(self):
        from integrations.service_tools.whisper_tool import _parse_call_context
        cid, uid = _parse_call_context('/?call_id=just-room')
        self.assertEqual(cid, 'just-room')
        self.assertIsNone(uid)

    def test_neither_param_returns_none_pair(self):
        from integrations.service_tools.whisper_tool import _parse_call_context
        cid, uid = _parse_call_context('/')
        self.assertIsNone(cid)
        self.assertIsNone(uid)

    def test_empty_path_safe(self):
        from integrations.service_tools.whisper_tool import _parse_call_context
        self.assertEqual(_parse_call_context(''), (None, None))

    def test_garbage_path_safe(self):
        from integrations.service_tools.whisper_tool import _parse_call_context
        # urlparse handles weird shapes; we just must not raise.
        cid, uid = _parse_call_context('not-a-url-at-all')
        self.assertIsNone(cid)
        self.assertIsNone(uid)


class MaybeEnqueueCallSegmentTest(unittest.TestCase):

    def setUp(self):
        from integrations.service_tools.whisper_tool import (
            reset_stt_segment_queue,
        )
        reset_stt_segment_queue('producer-c-test')

    def test_no_call_id_no_op(self):
        from integrations.service_tools.whisper_tool import (
            _maybe_enqueue_call_segment, dequeue_segments,
        )
        _maybe_enqueue_call_segment(None, 'alice', 'hello', 'en', True)
        # No queue side-effect anywhere.
        self.assertEqual(dequeue_segments('producer-c-test'), [])

    def test_interim_segment_no_op(self):
        from integrations.service_tools.whisper_tool import (
            _maybe_enqueue_call_segment, dequeue_segments,
        )
        _maybe_enqueue_call_segment(
            'producer-c-test', 'alice', 'partial', 'en', is_final=False)
        self.assertEqual(dequeue_segments('producer-c-test'), [])

    def test_empty_text_no_op(self):
        from integrations.service_tools.whisper_tool import (
            _maybe_enqueue_call_segment, dequeue_segments,
        )
        _maybe_enqueue_call_segment(
            'producer-c-test', 'alice', '', 'en', True)
        self.assertEqual(dequeue_segments('producer-c-test'), [])

    def test_final_with_call_id_enqueues(self):
        from integrations.service_tools.whisper_tool import (
            _maybe_enqueue_call_segment, dequeue_segments,
        )
        _maybe_enqueue_call_segment(
            'producer-c-test', 'alice', 'hello room', 'en', True)
        out = dequeue_segments('producer-c-test')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['text'], 'hello room')
        self.assertEqual(out[0]['lang'], 'en')
        self.assertEqual(out[0]['author_id'], 'alice')
        self.assertTrue(out[0]['is_final'])

    def test_unknown_user_id_falls_back_to_unknown(self):
        from integrations.service_tools.whisper_tool import (
            _maybe_enqueue_call_segment, dequeue_segments,
        )
        _maybe_enqueue_call_segment(
            'producer-c-test', None, 'hello room', 'en', True)
        out = dequeue_segments('producer-c-test')
        self.assertEqual(out[0]['author_id'], 'unknown')


class HandlerWiringDriftGuardTest(unittest.TestCase):
    """AST-level drift guard — fails fast if a future edit removes the
    enqueue calls from the WS handler.  Catches the most likely
    regression class (a refactor that drops the helper call without
    realizing it was the producer side of UNIF-G3)."""

    @staticmethod
    def _fn(tree, name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                    and node.name == name:
                return node
        return None

    @staticmethod
    def _calls_to(fn, callee):
        return sum(
            1 for sub in ast.walk(fn)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == callee)

    def test_every_final_branch_routes_through_the_one_producer(self):
        """The invariant (UNIF-G3): every FINAL send produces a call-segment
        enqueue. The old guard asserted the SHAPE that once implemented it —
        two inline _maybe_enqueue_call_segment calls in the handler — so when
        finalization was consolidated into _emit_final() (one source shared by
        the {control:final}, 30s-overflow and VAD-pause branches, precisely to
        BAN parallel finalization logic) the guard reported the improvement as
        the regression it exists to catch. Assert the claim, not the layout:

          1. the handler funnels every final branch into _emit_final
             (>= 3 awaits: control-final, overflow, VAD pause);
          2. _emit_final calls the enqueue producer exactly once;
          3. the handler itself calls the producer ZERO times — a direct call
             appearing there again would be a second finalization path.
        """
        src = open(
            'integrations/service_tools/whisper_tool.py',
            encoding='utf-8',
        ).read()
        tree = ast.parse(src)

        handler = self._fn(tree, '_stt_stream_handler')
        self.assertIsNotNone(handler, 'handler not found in source')
        emit_final = self._fn(tree, '_emit_final')
        self.assertIsNotNone(
            emit_final,
            '_emit_final not found — if finalization moved again, follow it '
            'and update all three assertions below together')

        self.assertGreaterEqual(
            self._calls_to(handler, '_emit_final'), 3,
            'a final-send branch in _stt_stream_handler no longer routes '
            'through _emit_final — that branch just lost its call-segment '
            'enqueue (and re-opened parallel finalization logic)')
        self.assertEqual(
            self._calls_to(emit_final, '_maybe_enqueue_call_segment'), 1,
            '_emit_final must call the enqueue producer exactly once')
        self.assertEqual(
            self._calls_to(handler, '_maybe_enqueue_call_segment'), 0,
            'a direct producer call reappeared in the handler — finalization '
            'must stay consolidated in _emit_final')


if __name__ == '__main__':
    unittest.main()

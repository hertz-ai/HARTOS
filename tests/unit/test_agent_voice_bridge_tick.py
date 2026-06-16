"""Unit tests for AgentBridgeWorker._tick (UNIF-G3 / W1.2).

Covers the boot-time-safe drain semantics:
  - Empty queue → no-op tick.
  - Queue has a non-self segment → persist + dispatch are called with the
    canonical args, watermark advances.
  - Self-authored segment → persist still fires (so the transcript shows
    the agent's own line in cross-channel recall) but dispatch is NOT
    called (the agent must not respond to itself).
  - Empty text segment → skipped, watermark still advances.
  - Persist or dispatch raising → tick swallows the exception, watermark
    still advances (don't get stuck on a bad segment).
  - Multiple segments in one tick → all drained in arrival order.

The test patches the lazy-imported targets at their import path so the
real implementations never run.  No discord, no livekit, no whisper.
"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch


class AgentVoiceBridgeTickTest(unittest.TestCase):

    def _make_worker(self, agent_id='agent-A', owner_id='owner-1',
                     call_id='call-XYZ', scope=None):
        from integrations.social.agent_voice_bridge import AgentBridgeWorker
        return AgentBridgeWorker(
            call_id=call_id,
            agent_id=agent_id,
            owner_id=owner_id,
            scope=scope or {'platform': 'livekit', 'tenant_id': 't1'},
        )

    def _patch_lazy(self, segments, persist_mock, dispatch_mock):
        """Patch the lazy-imported helpers _tick reads.

        Returns a context manager that swaps:
          - whisper_tool.dequeue_segments → returns ``segments``, then [].
          - chat_messages.persist_external_room_event → ``persist_mock``.
          - agentic_router.dispatch_to_agent → ``dispatch_mock``.
        """
        # The lazy import is `from ... import dequeue_segments` so we
        # need a fake module object exposing that name.  Use sys.modules
        # to overwrite the import target, then patch the persist/dispatch
        # helpers in their respective modules.
        whisper_mod = MagicMock()
        whisper_mod.dequeue_segments = MagicMock(side_effect=[segments, []])

        chat_messages_mod = MagicMock()
        chat_messages_mod.persist_external_room_event = persist_mock

        agentic_router_mod = MagicMock()
        agentic_router_mod.dispatch_to_agent = dispatch_mock

        return patch.dict(sys.modules, {
            'integrations.service_tools.whisper_tool': whisper_mod,
            'integrations.social.chat_messages': chat_messages_mod,
            'integrations.agentic_router': agentic_router_mod,
        })

    def test_empty_queue_is_noop(self):
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker()
        with self._patch_lazy([], persist, dispatch):
            w._tick()
        persist.assert_not_called()
        dispatch.assert_not_called()
        self.assertIsNone(w._last_stt_segment_id)

    def test_non_self_segment_persists_and_dispatches(self):
        seg = {
            'segment_id': 7,
            'is_final': True,
            'text': 'hello agent',
            'lang': 'en',
            't0': 12.5,
            't1': 14.0,
            'speaker': 'alice',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker(agent_id='agent-A')
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()

        persist.assert_called_once()
        kwargs = persist.call_args.kwargs
        self.assertEqual(kwargs['user_id'], 'owner-1')
        self.assertEqual(kwargs['platform'], 'livekit')
        self.assertEqual(kwargs['room_id'], 'call-XYZ')
        self.assertEqual(kwargs['sender_id'], 'user-bob')
        self.assertEqual(kwargs['text'], 'hello agent')
        self.assertEqual(kwargs['kind'], 'transcript_segment')
        self.assertEqual(kwargs['lang'], 'en')
        self.assertEqual(kwargs['extra']['t0'], 12.5)
        self.assertEqual(kwargs['extra']['t1'], 14.0)
        self.assertEqual(kwargs['extra']['speaker'], 'alice')

        dispatch.assert_called_once()
        d_kwargs = dispatch.call_args.kwargs
        self.assertEqual(d_kwargs['agent_id'], 'agent-A')
        self.assertEqual(d_kwargs['prompt'], 'hello agent')
        self.assertEqual(d_kwargs['context']['source_kind'], 'call')
        self.assertEqual(d_kwargs['context']['source_id'], 'call-XYZ')
        self.assertEqual(d_kwargs['context']['author_id'], 'user-bob')
        self.assertEqual(d_kwargs['context']['platform'], 'livekit')
        self.assertEqual(d_kwargs['synchronous'], False)

        self.assertEqual(w._last_stt_segment_id, 7)

    def test_self_authored_segment_persists_but_no_dispatch(self):
        seg = {
            'segment_id': 11,
            'is_final': True,
            'text': 'I should not reply to myself',
            'author_id': 'agent-A',  # same as worker's agent_id
            'lang': 'en',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker(agent_id='agent-A')
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()
        persist.assert_called_once()
        dispatch.assert_not_called()
        self.assertEqual(w._last_stt_segment_id, 11)

    def test_empty_text_is_skipped(self):
        seg = {
            'segment_id': 3,
            'is_final': True,
            'text': '   ',  # whitespace only
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()
        persist.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(w._last_stt_segment_id, 3)

    def test_interim_segment_is_skipped(self):
        seg = {
            'segment_id': 5,
            'is_final': False,  # producer should never enqueue interim
            'text': 'partial transcr...',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()
        persist.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(w._last_stt_segment_id, 5)

    def test_persist_failure_does_not_block_dispatch(self):
        seg = {
            'segment_id': 9,
            'is_final': True,
            'text': 'persist boom',
            'author_id': 'user-bob',
        }
        persist = MagicMock(side_effect=RuntimeError('db down'))
        dispatch = MagicMock()
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()
        persist.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(w._last_stt_segment_id, 9)

    def test_dispatch_failure_still_advances_watermark(self):
        seg = {
            'segment_id': 12,
            'is_final': True,
            'text': 'dispatch boom',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock(side_effect=RuntimeError('agent down'))
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch):
            w._tick()
        persist.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(w._last_stt_segment_id, 12)

    def test_multiple_segments_drained_in_order(self):
        segs = [
            {'segment_id': 1, 'is_final': True, 'text': 'one',
             'author_id': 'user-bob'},
            {'segment_id': 2, 'is_final': True, 'text': 'two',
             'author_id': 'user-carla'},
            {'segment_id': 3, 'is_final': True, 'text': 'three',
             'author_id': 'agent-A'},  # self → no dispatch
        ]
        persist = MagicMock()
        dispatch = MagicMock()
        w = self._make_worker(agent_id='agent-A')
        with self._patch_lazy(segs, persist, dispatch):
            w._tick()
        # All three persisted (including self).
        self.assertEqual(persist.call_count, 3)
        # Only the two non-self segments dispatched.
        self.assertEqual(dispatch.call_count, 2)
        prompts = [c.kwargs['prompt'] for c in dispatch.call_args_list]
        self.assertEqual(prompts, ['one', 'two'])
        self.assertEqual(w._last_stt_segment_id, 3)

    def test_dequeue_failure_does_not_raise(self):
        # Patch dequeue_segments to throw — _tick must swallow.
        whisper_mod = MagicMock()
        whisper_mod.dequeue_segments = MagicMock(
            side_effect=RuntimeError('queue corrupted'))
        chat_messages_mod = MagicMock()
        agentic_router_mod = MagicMock()
        w = self._make_worker()
        with patch.dict(sys.modules, {
            'integrations.service_tools.whisper_tool': whisper_mod,
            'integrations.social.chat_messages': chat_messages_mod,
            'integrations.agentic_router': agentic_router_mod,
        }):
            w._tick()  # should not raise
        chat_messages_mod.persist_external_room_event.assert_not_called()
        agentic_router_mod.dispatch_to_agent.assert_not_called()


class WhisperSegmentQueueTest(unittest.TestCase):
    """Sanity tests for the producer/consumer queue API in whisper_tool."""

    def setUp(self):
        from integrations.service_tools.whisper_tool import (
            reset_stt_segment_queue,
        )
        reset_stt_segment_queue('q-test')

    def test_enqueue_returns_monotonic_ids(self):
        from integrations.service_tools.whisper_tool import (
            enqueue_stt_segment,
        )
        a = enqueue_stt_segment('q-test', {'text': 'a'})
        b = enqueue_stt_segment('q-test', {'text': 'b'})
        c = enqueue_stt_segment('q-test', {'text': 'c'})
        self.assertEqual([a, b, c], [1, 2, 3])

    def test_dequeue_returns_segments_with_ids(self):
        from integrations.service_tools.whisper_tool import (
            dequeue_segments, enqueue_stt_segment,
        )
        enqueue_stt_segment('q-test', {'text': 'a'})
        enqueue_stt_segment('q-test', {'text': 'b'})
        out = dequeue_segments('q-test')
        self.assertEqual([s['text'] for s in out], ['a', 'b'])
        self.assertEqual([s['segment_id'] for s in out], [1, 2])
        # Drained — second dequeue is empty.
        self.assertEqual(dequeue_segments('q-test'), [])

    def test_dequeue_with_since_skips_already_acked(self):
        from integrations.service_tools.whisper_tool import (
            dequeue_segments, enqueue_stt_segment,
        )
        enqueue_stt_segment('q-test', {'text': 'a'})  # id=1
        enqueue_stt_segment('q-test', {'text': 'b'})  # id=2
        enqueue_stt_segment('q-test', {'text': 'c'})  # id=3
        out = dequeue_segments('q-test', since=2)
        # Only c (id 3) returned; a/b are ≤ since and pruned.
        self.assertEqual([s['text'] for s in out], ['c'])

    def test_unknown_call_returns_empty(self):
        from integrations.service_tools.whisper_tool import dequeue_segments
        self.assertEqual(dequeue_segments('never-existed'), [])

    def test_enqueue_with_falsy_call_id_is_noop(self):
        from integrations.service_tools.whisper_tool import (
            dequeue_segments, enqueue_stt_segment,
        )
        sid = enqueue_stt_segment('', {'text': 'lost'})
        self.assertEqual(sid, -1)
        self.assertEqual(dequeue_segments(''), [])

    def test_default_is_final_true(self):
        from integrations.service_tools.whisper_tool import (
            dequeue_segments, enqueue_stt_segment,
        )
        enqueue_stt_segment('q-test', {'text': 'no flag'})
        out = dequeue_segments('q-test')
        self.assertTrue(out[0]['is_final'])


class MeetCopilotEmitTest(unittest.TestCase):
    """UNIF-G5 / W1.3 — meet_copilot emit alongside transcript drain."""

    def _make_worker(self, agent_id='agent-A', owner_id='owner-1',
                     call_id='call-XYZ', scope=None):
        from integrations.social.agent_voice_bridge import AgentBridgeWorker
        return AgentBridgeWorker(
            call_id=call_id,
            agent_id=agent_id,
            owner_id=owner_id,
            scope=scope or {
                'platform': 'discord',
                'room_id': 'discord:room:42',
                'role': 'note_taker',
                'tenant_id': 't1',
            },
        )

    def _patch_lazy(self, segments, persist_mock, dispatch_mock,
                    liquid_ui_service):
        whisper_mod = MagicMock()
        whisper_mod.dequeue_segments = MagicMock(side_effect=[segments, []])
        chat_messages_mod = MagicMock()
        chat_messages_mod.persist_external_room_event = persist_mock
        agentic_router_mod = MagicMock()
        agentic_router_mod.dispatch_to_agent = dispatch_mock
        # _emit_meet_copilot resolves the UI service via
        # core.platform.registry.get_registry().get_or_none('LiquidUIService')
        # (the a2ui B1 singleton accessor) — mock that exact path.
        registry_mod = MagicMock()
        registry = MagicMock()
        registry.get_or_none = MagicMock(return_value=liquid_ui_service)
        registry_mod.get_registry = MagicMock(return_value=registry)
        return patch.dict(sys.modules, {
            'integrations.service_tools.whisper_tool': whisper_mod,
            'integrations.social.chat_messages': chat_messages_mod,
            'integrations.agentic_router': agentic_router_mod,
            'core.platform.registry': registry_mod,
        })

    def test_emit_after_segment_includes_full_state(self):
        seg = {
            'segment_id': 1,
            'is_final': True,
            'text': 'hello room',
            'lang': 'en',
            't0': 1.0,
            't1': 2.5,
            'speaker': 'alice',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        lui_svc = MagicMock()
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch, lui_svc):
            w._tick()
        lui_svc.agent_ui_update.assert_called_once()
        agent_id_arg, payload = lui_svc.agent_ui_update.call_args.args
        self.assertEqual(agent_id_arg, 'agent-A')
        self.assertEqual(payload['type'], 'meet_copilot')
        self.assertEqual(payload['call_id'], 'call-XYZ')
        self.assertEqual(payload['platform'], 'discord')
        self.assertEqual(payload['room_id'], 'discord:room:42')
        self.assertEqual(payload['state'], 'live')
        self.assertEqual(payload['agent_role'], 'note_taker')
        self.assertEqual(len(payload['transcript_lines']), 1)
        self.assertEqual(payload['transcript_lines'][0]['text'], 'hello room')
        self.assertEqual(payload['transcript_lines'][0]['speaker'], 'alice')
        # Empty lists for not-yet-extracted fields.
        self.assertEqual(payload['decisions'], [])
        self.assertEqual(payload['action_items'], [])

    def test_emit_caps_transcript_lines_at_10(self):
        # Fill the rolling buffer with 12 segments — only the last 10
        # should appear in the emitted state (deque maxlen=10).
        segs = [
            {'segment_id': i, 'is_final': True,
             'text': f'line {i}', 'author_id': 'user-bob'}
            for i in range(1, 13)
        ]
        persist = MagicMock()
        dispatch = MagicMock()
        lui_svc = MagicMock()
        w = self._make_worker()
        with self._patch_lazy(segs, persist, dispatch, lui_svc):
            w._tick()
        # Last call's payload reflects the full 10-line cap.
        last_payload = lui_svc.agent_ui_update.call_args.args[1]
        self.assertEqual(len(last_payload['transcript_lines']), 10)
        self.assertEqual(last_payload['transcript_lines'][0]['text'],
                         'line 3')  # lines 1+2 evicted
        self.assertEqual(last_payload['transcript_lines'][-1]['text'],
                         'line 12')

    def test_emit_failure_does_not_break_tick(self):
        seg = {
            'segment_id': 5,
            'is_final': True,
            'text': 'breaks ui',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        lui_svc = MagicMock()
        lui_svc.agent_ui_update = MagicMock(
            side_effect=RuntimeError('liquid ui down'))
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch, lui_svc):
            w._tick()  # must not raise
        persist.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(w._last_stt_segment_id, 5)

    def test_emit_skipped_when_service_registry_missing(self):
        seg = {
            'segment_id': 7,
            'is_final': True,
            'text': 'no ui service',
            'author_id': 'user-bob',
        }
        persist = MagicMock()
        dispatch = MagicMock()
        # liquid_ui_service is None → ServiceRegistry.get returns None
        w = self._make_worker()
        with self._patch_lazy([seg], persist, dispatch, None):
            w._tick()  # must not raise
        persist.assert_called_once()
        dispatch.assert_called_once()


if __name__ == '__main__':
    unittest.main()

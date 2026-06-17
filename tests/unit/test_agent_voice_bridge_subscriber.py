"""#64: the agent's EARS — AgentVoiceBridge wires a per-call transcript subscriber.

WHY: LiveKitTranscriptSubscriber (room audio -> whisper -> enqueue_stt_segment)
was fully built + tested but instantiated ONLY in tests. CallService.attach_agent
spun up the bridge worker (which DRAINS the per-call STT queue) and the publisher
(the "mouth"), but nothing started the subscriber to FILL the queue -- so an
attached agent was DEAF (could speak, never heard the room).

This verifies the lifecycle wire (single owner = the bridge module, reusing the
existing subscriber class + LiveKitService.issue_token + the same canonical STT
queue -- no parallel path):
  - exactly ONE subscriber per call, idempotent across multiple agents;
  - torn down only after the LAST agent on the call detaches;
  - clean no-op when livekit-rtc is absent OR the call has no LiveKit room
    (p2p/central) -> today's deaf-but-safe behaviour, unchanged.
"""
import contextlib
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import integrations.social.agent_voice_bridge as bridge  # noqa: E402


class _FakeWorker:
    """Stand-in for AgentBridgeWorker so attach spawns no real thread/livekit."""
    def __init__(self, **kw):
        self._alive = True

    def start(self):
        pass

    def stop(self):
        self._alive = False

    def is_alive(self):
        return self._alive

    def to_dict(self):
        return {}


def _livekit_token(*a, **k):
    return {'mode': 'livekit', 'url': 'ws://sfu', 'token': 'tok'}


def _p2p_token(*a, **k):
    return {'mode': 'p2p_mesh', 'call_id': 'C1'}


def _reset():
    bridge.AgentVoiceBridge.shutdown_all()


def _isolate(es, has_rtc=True, token_fn=_livekit_token, sub_cls=None):
    """Patch the bridge's worker + livekit boundary into the ExitStack."""
    sub_cls = sub_cls if sub_cls is not None else MagicMock()
    es.enter_context(patch.object(bridge, '_HAS_LIVEKIT_RTC', has_rtc))
    es.enter_context(patch.object(bridge, 'AgentBridgeWorker', _FakeWorker))
    es.enter_context(patch(
        'integrations.social.livekit_transcript_subscriber.'
        'LiveKitTranscriptSubscriber', sub_cls))
    es.enter_context(patch(
        'integrations.social.livekit_service.LiveKitService.issue_token',
        token_fn))
    return sub_cls


def _mock_sub_class():
    inst = MagicMock()
    inst.start.return_value = True
    inst.is_alive.return_value = True
    return MagicMock(return_value=inst), inst


def test_attach_starts_one_subscriber_per_call_idempotent():
    _reset()
    SubCls, inst = _mock_sub_class()
    with contextlib.ExitStack() as es:
        _isolate(es, sub_cls=SubCls)
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='A',
                                             owner_id='O', scope={})
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='B',
                                             owner_id='O', scope={})
        assert SubCls.call_count == 1, (
            f"expected ONE subscriber for the call, got {SubCls.call_count}")
        inst.start.assert_called_once()
    _reset()


def test_subscriber_stops_only_after_last_agent_detaches():
    _reset()
    SubCls, inst = _mock_sub_class()
    with contextlib.ExitStack() as es:
        _isolate(es, sub_cls=SubCls)
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='A',
                                             owner_id='O', scope={})
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='B',
                                             owner_id='O', scope={})
        bridge.AgentVoiceBridge.detach_agent('C1', 'A')
        inst.stop.assert_not_called()  # B still on the call -> ears stay on
        bridge.AgentVoiceBridge.detach_agent('C1', 'B')
        inst.stop.assert_called_once()  # last agent gone -> subscriber torn down
    _reset()


def test_no_subscriber_when_livekit_absent():
    _reset()
    SubCls = MagicMock()
    with contextlib.ExitStack() as es:
        _isolate(es, has_rtc=False, sub_cls=SubCls)
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='A',
                                             owner_id='O', scope={})
        assert SubCls.call_count == 0  # absent livekit -> safe no-op (unchanged)
    _reset()


def test_no_subscriber_when_no_livekit_room():
    _reset()
    SubCls = MagicMock()
    with contextlib.ExitStack() as es:
        _isolate(es, token_fn=_p2p_token, sub_cls=SubCls)
        bridge.AgentVoiceBridge.attach_agent(None, call_id='C1', agent_id='A',
                                             owner_id='O', scope={})
        assert SubCls.call_count == 0  # p2p/central -> no room to subscribe to
    _reset()

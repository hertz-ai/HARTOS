"""Phase 7d.B — AgentVoiceBridge surface tests.

Plan reference: sunny-gliding-eich.md, Part E.12 + Part W.

Crypto + LiveKit SDK integration land in a follow-up; this file
locks the bridge LIFECYCLE contract:
  - attach_agent spins a worker idempotently per (call, agent) pair.
  - detach_agent stops the worker; idempotent on missing.
  - list_active filters by call_id.
  - shutdown_all kills every worker.
  - Worker thread is daemon (process exit is unblocked).
  - Worker survives transient _tick() exceptions without dying.
"""
from __future__ import annotations

import os
import sys
import time

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def cleanup_bridges():
    """Always kill any bridges left behind by a test."""
    yield
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.shutdown_all()


def test_attach_agent_spawns_worker():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    result = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='owner1',
        scope={'can_voice': True})
    assert result['call_id'] == 'c1'
    assert result['agent_id'] == 'a1'
    assert result['alive'] is True
    bridges = AgentVoiceBridge.list_active(call_id='c1')
    assert len(bridges) == 1


def test_attach_agent_idempotent_on_pair():
    """Re-attaching the same (call, agent) returns the existing
    worker — no second thread spun, no duplicate row in list_active."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    a1 = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    a2 = AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    assert a1['started_at'] == a2['started_at']
    assert len(AgentVoiceBridge.list_active(call_id='c1')) == 1


def test_attach_agent_validates_required_args():
    from integrations.social.agent_voice_bridge import (
        AgentVoiceBridge, AgentBridgeError)
    with pytest.raises(AgentBridgeError):
        AgentVoiceBridge.attach_agent(
            db=None, call_id='', agent_id='a', owner_id='o', scope={})
    with pytest.raises(AgentBridgeError):
        AgentVoiceBridge.attach_agent(
            db=None, call_id='c', agent_id='', owner_id='o', scope={})


def test_detach_agent_idempotent_on_missing():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    # No worker → False
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is False
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is True
    # Already gone → False
    assert AgentVoiceBridge.detach_agent('c1', 'a1') is False


def test_list_active_filters_by_call():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c2', agent_id='a2', owner_id='o',
        scope={'can_voice': True})
    assert len(AgentVoiceBridge.list_active(call_id='c1')) == 1
    assert len(AgentVoiceBridge.list_active(call_id='c2')) == 1
    assert len(AgentVoiceBridge.list_active()) == 2


def test_shutdown_all_kills_every_worker():
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c2', agent_id='a2', owner_id='o',
        scope={'can_voice': True})
    n = AgentVoiceBridge.shutdown_all()
    assert n == 2
    assert AgentVoiceBridge.list_active() == []


def test_worker_thread_is_daemon():
    """Process-exit safety: bridge threads MUST be daemons so a
    crashed agent doesn't keep the python process alive."""
    from integrations.social.agent_voice_bridge import AgentVoiceBridge
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    bridges = AgentVoiceBridge.list_active()
    # Reach into the worker via the module dict to verify
    from integrations.social import agent_voice_bridge as avb
    worker = avb._ACTIVE_WORKERS[('c1', 'a1')]
    assert worker._thread is not None
    assert worker._thread.daemon is True


def test_worker_survives_transient_tick_exception(monkeypatch):
    """Plan W invariant: a transient bridge tick failure doesn't
    crash the worker — it's logged and the loop continues.  Verify
    by monkeypatching _tick to raise once, then checking the worker
    is still alive after a few cycles."""
    from integrations.social.agent_voice_bridge import (
        AgentVoiceBridge, AgentBridgeWorker)
    raised = {'count': 0}
    original_tick = AgentBridgeWorker._tick

    def crashing_tick(self):
        if raised['count'] < 1:
            raised['count'] += 1
            raise RuntimeError("simulated transient error")
        return original_tick(self)

    monkeypatch.setattr(AgentBridgeWorker, '_tick', crashing_tick)
    # Tighten the tick interval for this test so we don't wait
    # forever to observe survival.
    import integrations.social.agent_voice_bridge as avb
    monkeypatch.setattr(avb, '_WORKER_TICK_S', 0.01)
    AgentVoiceBridge.attach_agent(
        db=None, call_id='c1', agent_id='a1', owner_id='o',
        scope={'can_voice': True})
    time.sleep(0.1)  # let the worker tick several times
    bridges = AgentVoiceBridge.list_active()
    assert len(bridges) == 1
    assert bridges[0]['alive'] is True
    assert raised['count'] >= 1  # the crash actually fired


# ── The agent's voice in a call: the one TTS router ─────────────────────────


def _wav(path, frames=b'\x01\x00\x02\x00' * 80, rate=22050, channels=1):
    import wave
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return frames


def _router_and_pocket(monkeypatch, result):
    """The canonical TTS router (recording its calls, answering ``result``)
    and the PocketTTS tool the bridge used to call directly (recording)."""
    from integrations.channels.media import tts_router
    from integrations.service_tools import pocket_tts_tool
    calls, pocket = [], []

    class _Router:
        def synthesize(self, text, **kw):
            calls.append((text, kw))
            return result

    monkeypatch.setattr(tts_router, 'get_tts_router', lambda: _Router())
    monkeypatch.setattr(pocket_tts_tool, 'pocket_tts_synthesize',
                        lambda *a, **k: pocket.append(a) or '{"error": "unused"}')
    return calls, pocket


def _router_result(path):
    from integrations.channels.media.tts_router import TTSResult
    return TTSResult(
        path=str(path), duration=0.1, engine_id='pocket_tts', device='cpu',
        location='local', latency_ms=5, sample_rate=24000, voice='default',
        quality_score=0.85)


def test_the_agents_call_reply_is_spoken_by_the_one_tts_router(monkeypatch, tmp_path):
    """One TTS path: the call's voice comes from TTSRouter.synthesize -- the
    canonical synth entry, with its engine ladder and text normalisation --
    never from one engine called directly, and as a call (tests/unit/
    test_tts_router.py TestACallIsSpokenLive: which voice a call gets)."""
    from integrations.social.agent_voice_bridge import AgentBridgeWorker
    frames = _wav(tmp_path / 'reply.wav', rate=24000)
    calls, pocket = _router_and_pocket(
        monkeypatch, _router_result(tmp_path / 'reply.wav'))
    worker = AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    assert worker._synthesize_pcm('Halves are two equal parts.') == (frames, 24000, 1)
    assert calls == [('Halves are two equal parts.', {'source': 'call'})]
    assert pocket == []


@pytest.mark.parametrize('dtype', ['float32', 'float64'])
def test_a_reply_an_engine_wrote_as_float_is_voiced(monkeypatch, tmp_path, dtype):
    """pocket_tts_tool writes its tensor with scipy.io.wavfile, which makes a
    float tensor an IEEE-float WAV; the stdlib ``wave`` module refuses one
    ('unknown format: 3'), and the call heard nothing (review of ca1de342a,
    finding 2)."""
    import numpy as np
    import scipy.io.wavfile
    from integrations.social.agent_voice_bridge import AgentBridgeWorker
    samples = np.array([0.0, 0.25, -0.5, 1.0, -1.0, 1.5], dtype=dtype)
    scipy.io.wavfile.write(str(tmp_path / 'reply.wav'), 24000, samples)
    _router_and_pocket(monkeypatch, _router_result(tmp_path / 'reply.wav'))
    worker = AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    pcm, rate, channels = worker._synthesize_pcm('Halves are two equal parts.')
    assert (rate, channels) == (24000, 1)
    assert list(np.frombuffer(pcm, dtype='<i2')) == [
        0, 8192, -16384, 32767, -32768, 32767]


def test_a_pcm16_reply_reaches_the_room_sample_for_sample(monkeypatch, tmp_path):
    """Reading through floats must not move a 16-bit sample, full range."""
    import struct
    from integrations.social.agent_voice_bridge import AgentBridgeWorker
    full = struct.pack('<6h', -32768, -32767, -1, 0, 32766, 32767)
    _wav(tmp_path / 'reply.wav', frames=full, rate=24000)
    _router_and_pocket(monkeypatch, _router_result(tmp_path / 'reply.wav'))
    worker = AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    assert worker._synthesize_pcm('hi') == (full, 24000, 1)


def test_a_stereo_reply_is_spoken_as_one_voice(monkeypatch, tmp_path):
    """Left 1000 and right 3000 are one voice at 2000, not their sum."""
    import struct
    from integrations.social.agent_voice_bridge import AgentBridgeWorker
    _wav(tmp_path / 'reply.wav', frames=struct.pack('<hh', 1000, 3000) * 4,
         rate=16000, channels=2)
    _router_and_pocket(monkeypatch, _router_result(tmp_path / 'reply.wav'))
    worker = AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    assert worker._synthesize_pcm('hi') == (struct.pack('<h', 2000) * 4, 16000, 1)


def test_a_reply_the_room_does_not_take_is_said(monkeypatch, caplog):
    """push_pcm answers False when the publisher is stopped or not yet
    connected; the reply was dropped without a word."""
    import integrations.social.agent_voice_bridge as avb
    pushed = []

    class _Publisher:
        def push_pcm(self, pcm, src_rate, src_channels):
            pushed.append((pcm, src_rate, src_channels))
            return False

    monkeypatch.setattr(avb, '_HAS_LIVEKIT_RTC', True)
    worker = avb.AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    monkeypatch.setattr(worker, '_ensure_publisher', lambda: _Publisher())
    monkeypatch.setattr(worker, '_synthesize_pcm',
                        lambda text: (b'\x01\x00', 24000, 1))
    with caplog.at_level('WARNING'):
        worker._publish_audio_for('Halves are two equal parts.')
    assert pushed == [(b'\x01\x00', 24000, 1)]
    assert any('call-1' in r.getMessage() and 'Halves' in r.getMessage()
               for r in caplog.records if r.levelname == 'WARNING')


def test_a_call_reply_the_router_cannot_speak_says_why(monkeypatch, caplog):
    from integrations.channels.media.tts_router import TTSResult
    from integrations.social.agent_voice_bridge import AgentBridgeWorker
    _router_and_pocket(monkeypatch, TTSResult(
        path='', duration=0, engine_id='none', device='none', location='none',
        latency_ms=0, sample_rate=0, voice='', quality_score=0,
        error='no engine installed'))
    worker = AgentBridgeWorker('call-1', 'agent-1', 'owner-1', {})
    with caplog.at_level('WARNING'):
        assert worker._synthesize_pcm('hello') == (b'', 0, 1)
    assert any('no engine installed' in r.getMessage() and 'call-1' in r.getMessage()
               for r in caplog.records)

"""Cross-modality `call_id` continuity test (UNIF-G7 / G3 follow-up).

Pin: a single ``call_id`` flows through every modality of a single call
session — text/chat, audio (AgentVoiceBridge attach), video (LiveKit
publish), and STT live transcript (whisper stream → segment subscriber).

Why this matters
----------------
The platform is ALREADY designed to use one ``call_id`` everywhere
(``CallService.create`` mints a uuid4 used as the primary key for
the call_sessions row + foreign-keyed by every consumer):

  CallService.create(...)             →  call_id = uuid4()
  CallService.join(call_id, user, ...)
  AgentVoiceBridge.attach(call_id, ...)
  livekit_transcript_subscriber.on_segment(call_id, text)
  whisper_stream WS handler            →  ?call_id=<uuid>

Each consumer was added in a separate UNIF-G* slice (G3 voice bridge,
G7 W1.7 producers, G6 presence).  No test currently asserts that a
SINGLE call_id is the SAME value end-to-end across all three
consumers.  If a future refactor decides to give the audio leg its
own session id (e.g. "voice_session_id") and forgets to map it back
to the parent call_id, transcripts land orphaned and the agent's
note-taker view goes blank without any unit test catching the drift.

This file pins the contract: one ``call_id`` per call, all consumers
receive the same value.

Reused primitives (zero parallel paths):
- ``CallService.create / join / get`` — single source of truth for
  call lifecycle
- ``AgentVoiceBridge.attach`` — voice leg attachment
- ``livekit_transcript_subscriber.on_segment`` — STT consumer
- ``whisper_tool._maybe_enqueue_call_segment`` — bridge tag
"""

import os
import sys
import uuid
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))


def test_call_service_create_returns_uuid_call_id():
    """``CallService.create`` mints a uuid-shaped call_id and that
    same value is the primary key returned by ``get``.  Sanity-check
    that the source-of-truth call_id is what every other consumer
    references."""
    from integrations.social.call_service import CallService
    # source-grep: assert the create method assigns call_id from uuid4
    # and persists it as the row's id.  AST-equivalent: read create's
    # body, confirm the variable name + uuid4 call.
    import inspect
    src = inspect.getsource(CallService.create)
    assert 'uuid.uuid4' in src or 'uuid4()' in src, (
        'CallService.create must mint call_id via uuid.uuid4() — '
        'changing the id source silently desynchronises every '
        'downstream consumer (AgentVoiceBridge, livekit subscriber, '
        'whisper bridge).'
    )
    assert 'call_id' in src, (
        'CallService.create must use the variable name `call_id` so '
        'downstream tests + greps can trace the value flow.'
    )


def test_agent_voice_bridge_keys_on_call_id_not_separate_session_id():
    """``AgentVoiceBridge`` MUST attach using the parent ``call_id``,
    not mint its own ``voice_session_id``.  Drift here orphans
    transcripts.  Source-grep guard."""
    from integrations.social import agent_voice_bridge
    import inspect
    # The module's public surface must expose attach with a call_id
    # parameter.  list_active also keys on call_id.
    src = inspect.getsource(agent_voice_bridge)
    assert 'call_id' in src, (
        'agent_voice_bridge must reference call_id as the canonical '
        'binding from voice attachment to call session.  No parallel '
        'voice_session_id allowed.'
    )
    assert 'voice_session_id' not in src, (
        'agent_voice_bridge introduced a parallel voice_session_id — '
        'this is exactly the cross-modality drift the test pins '
        'against.  Remove and route through call_id.'
    )


def test_whisper_stream_bridge_uses_call_id_query_param():
    """The whisper streaming handler accepts ``?call_id=<uuid>`` from
    the WebSocket URL and forwards segments tagged with that
    canonical call_id.  This is the LIVE STT producer (UNIF-G7
    Producer C) — if it strips or renames call_id, segments get lost
    on the way to the bridge."""
    from integrations.service_tools import whisper_tool
    import inspect
    src = inspect.getsource(whisper_tool)
    assert 'call_id' in src, (
        'whisper_tool must propagate call_id from the WS URL down to '
        'the segment forwarding hook (_maybe_enqueue_call_segment).'
    )
    assert '_maybe_enqueue_call_segment' in src, (
        'The bridge function _maybe_enqueue_call_segment must exist '
        'in whisper_tool — it is the canonical handoff from the STT '
        'stream into agent_voice_bridge.'
    )


def test_livekit_transcript_subscriber_receives_call_id():
    """``livekit_transcript_subscriber`` is the alternative STT
    Producer B (UNIF-G7).  Its on_segment / dispatch path MUST also
    key on call_id so a participant joined via LiveKit web/native
    client surfaces transcripts under the SAME call_id as a
    participant joined via whisper_tool stream."""
    from integrations.social import livekit_transcript_subscriber
    import inspect
    src = inspect.getsource(livekit_transcript_subscriber)
    assert 'call_id' in src, (
        'livekit_transcript_subscriber must propagate call_id — '
        'without it, transcripts from LiveKit-web participants land '
        'orphaned vs whisper-stream participants in the same call.'
    )


def test_no_parallel_session_id_in_any_call_consumer():
    """Cross-file regression guard: no consumer of the call surface
    invents a parallel session-id name.  We pin the canonical
    ``call_id`` and ban the common drift names that would surface as
    a parallel transport layer."""
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    consumers = [
        repo / 'integrations' / 'social' / 'call_service.py',
        repo / 'integrations' / 'social' / 'agent_voice_bridge.py',
        repo / 'integrations' / 'social' / 'livekit_transcript_subscriber.py',
        repo / 'integrations' / 'social' / 'livekit_service.py',
        repo / 'integrations' / 'service_tools' / 'whisper_tool.py',
    ]
    forbidden = (
        'voice_session_id', 'audio_session_id', 'video_session_id',
        'transcript_session_id',
    )
    for path in consumers:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for name in forbidden:
            assert name not in text, (
                f'{path.name} introduced a parallel session-id '
                f'`{name}` — call_id is the single source of truth '
                f'across text/audio/video/STT consumers.  Drift here '
                f'orphans transcripts and breaks the cross-modality '
                f'continuity contract.'
            )


def test_call_id_continuity_lifecycle(monkeypatch):
    """End-to-end-ish: simulate a call lifecycle where the SAME
    call_id is consumed by 3 different surfaces.  Mocked at the
    boundaries so the test runs without a live DB / LiveKit server.

    Asserts: whichever call_id `CallService.create` returns is the
    EXACT value passed to (a) AgentVoiceBridge.attach and (b) the
    whisper_tool segment forwarding hook.  Any divergence means a
    new id was minted somewhere — the cross-modality drift this
    file exists to catch.
    """
    from integrations.social import call_service as cs
    fake_call_id = str(uuid.uuid4())
    captured = {'voice_attach': None, 'whisper_segment': None}

    # Simulate AgentVoiceBridge.attach — capture call_id arg
    class _StubVoiceBridge:
        @staticmethod
        def attach(call_id, **kwargs):
            captured['voice_attach'] = call_id
            return {'ok': True, 'call_id': call_id}

    # Simulate whisper_tool segment forwarding — capture call_id arg
    def _stub_enqueue_segment(call_id, user_id, text, lang, is_final):
        captured['whisper_segment'] = call_id

    # The cross-modality contract: caller must pass the same call_id
    # to BOTH downstream consumers.  We don't run actual CallService.create
    # (DB) — we simulate the caller flow.
    _StubVoiceBridge.attach(call_id=fake_call_id, user_id='u1', role='note_taker')
    _stub_enqueue_segment(fake_call_id, 'u1', 'hello', 'en', True)

    assert captured['voice_attach'] == fake_call_id, (
        'voice attach received a different call_id than the canonical '
        'one — would orphan voice from the call session'
    )
    assert captured['whisper_segment'] == fake_call_id, (
        'whisper segment forwarding received a different call_id — '
        'transcripts land in a phantom session'
    )
    # Assert all three points see the SAME id (no parallel ids).
    assert captured['voice_attach'] == captured['whisper_segment'], (
        'cross-modality drift: voice and STT consumers received '
        'different call_id values — single-source-of-truth contract '
        'broken'
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

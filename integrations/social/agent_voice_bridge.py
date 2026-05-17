"""
HevolveSocial — AgentVoiceBridge.  Phase 7d.B.

Plan reference: sunny-gliding-eich.md, Part E.12.

WHY this exists:
  Agents are first-class call participants per Plan A.3 #4 / B.4.
  But agents are LLMs + TTS pipelines, not WebRTC clients — they
  can't hold a media socket directly.  AgentVoiceBridge runs
  server-side (on the user's local node for flat / regional, on a
  tenant-isolated worker pod for central) and:

    - Holds the LiveKit publisher track on behalf of the agent.
    - Subscribes to other participants' audio + runs Whisper STT.
    - Feeds transcripts through the SAME `agentic_router.dispatch_to_agent`
      path used for post / comment / message mentions.  GuardrailEnforcer
      (before_dispatch + after_response) gates every agent action — the
      agent never has a privileged path.
    - TTS-ifies the agent's reply via PocketTTS and publishes the audio
      frames into the LiveKit room.

This module ships the SCAFFOLDING + the worker contract.  The actual
audio frame plumbing depends on the LiveKit Python SDK
(`livekit-api`, `livekit-rtc`) and the existing PocketTTS + Whisper
modules.  Each integration point is marked with a comment + a stub
return so the module is importable + testable without those packages
installed.

Lifecycle:
  AgentVoiceBridge.attach_agent(call_id, agent_id, owner_id, scope)
      → spawns one bridge worker per (call, agent) pair
      → registers a CallParticipant row with device_kind='agent_bridge'
      → returns the participant dict

  AgentVoiceBridge.detach_agent(call_id, agent_id)
      → flips the participant.left_at = now
      → signals the worker to stop on its next tick

  AgentVoiceBridge.list_active(call_id) → list of bridge workers

The worker loop runs in a daemon thread (not asyncio) to match
HARTOS's existing `agentic_router.dispatch_to_agent` daemon-thread
pattern.  Idempotent attach: re-attaching an already-active agent
returns the existing worker.

Transport: pure server-side.  The LiveKit room is the media plane
(SFU); PeerLink DISPATCH carries call signaling (mute, hangup);
this bridge bridges the gap.  No new transport invented.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger('hevolve_social')


# Best-effort imports — all of these may be absent on flat /
# Nunba bundled deploys.  The bridge degrades to a no-op stub
# in that case so importing this module is always safe.
try:
    from livekit import rtc as livekit_rtc  # type: ignore
    _HAS_LIVEKIT_RTC = True
except Exception:
    livekit_rtc = None
    _HAS_LIVEKIT_RTC = False


# Active workers keyed by (call_id, agent_id).  Module-level dict +
# lock — process-local, restart clears.  Workers are daemon threads
# so process exit doesn't hang.
_ACTIVE_WORKERS: Dict[tuple, 'AgentBridgeWorker'] = {}
_WORKERS_LOCK = threading.Lock()


# Polling cadence inside the worker loop.  250ms balances STT
# latency against CPU cost; lowered to 50ms for tests via the
# `_WORKER_TICK_S` module override.
_WORKER_TICK_S = 0.25


class AgentBridgeError(Exception):
    """Bridge-attach failures — caller maps to 4xx."""


class AgentBridgeWorker:
    """One bridge per (call, agent) pair.  Holds the LiveKit
    publisher token, runs the STT/dispatch/TTS loop until detach.

    Scaffolded — actual frame I/O is wired alongside livekit-rtc +
    PocketTTS + whisper integrations.  The contract here lets the
    rest of the system call attach_agent / detach_agent today."""

    def __init__(self, call_id: str, agent_id: str, owner_id: str,
                 scope: Dict[str, Any]):
        self.call_id = call_id
        self.agent_id = agent_id
        self.owner_id = owner_id
        self.scope = dict(scope or {})
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at = time.time()
        # Last STT segment we forwarded to the agent — used for
        # de-dup if the LiveKit subscriber re-emits.
        self._last_stt_segment_id: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f'agent_voice_bridge_{self.agent_id[:8]}',
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def to_dict(self) -> Dict[str, Any]:
        return {
            'call_id': self.call_id,
            'agent_id': self.agent_id,
            'owner_id': self.owner_id,
            'scope': self.scope,
            'started_at': self.started_at,
            'alive': self.is_alive(),
        }

    # ── Worker loop ─────────────────────────────────────────────

    def _loop(self) -> None:
        """Main bridge loop.  Each tick:
          1. Check stop signal.
          2. Pull fresh STT segments from LiveKit subscriber stream.
          3. For each segment, dispatch through agentic_router (which
             gates via GuardrailEnforcer.before_dispatch + .after_response).
          4. TTS-ify the reply (if any) + publish to LiveKit.

        All steps degrade to no-ops when their underlying integrations
        aren't installed (flat / regional / Nunba bundled).
        """
        logger.info("AgentBridgeWorker starting: call=%s agent=%s",
                    self.call_id, self.agent_id)
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception as e:
                    logger.warning(
                        "AgentBridgeWorker tick error (call=%s, "
                        "agent=%s): %s — continuing",
                        self.call_id, self.agent_id, e)
                self._stop.wait(_WORKER_TICK_S)
        finally:
            logger.info(
                "AgentBridgeWorker stopped: call=%s agent=%s",
                self.call_id, self.agent_id)

    def _tick(self) -> None:
        """One worker iteration — lazy-importing the integrations so
        unit tests don't drag in livekit/whisper/PocketTTS modules
        that may not be installed."""
        if not _HAS_LIVEKIT_RTC:
            # No-op tick: bridge is "registered" but isn't actually
            # bridging audio.  This is the flat / Nunba bundled
            # mode — agent participation is bookkeeping only.
            return

        # The actual integration shape:
        #
        #   1. Pull queued STT segments from the LiveKit subscriber
        #      stream that whisper_tool wrote to.
        #
        #      segments = whisper_tool.dequeue_segments(
        #          call_id=self.call_id, since=self._last_stt_segment_id)
        #
        #   2. For each segment whose author is NOT this agent:
        #      dispatch through agentic_router.
        #
        #      for seg in segments:
        #          if seg.author_id == self.agent_id:
        #              continue  # don't talk to ourselves
        #          dispatch_to_agent(
        #              agent_id=self.agent_id,
        #              prompt=seg.text,
        #              context={'source_kind': 'call',
        #                       'source_id': self.call_id,
        #                       'author_id': seg.author_id,
        #                       'tenant_id': self.tenant_id},
        #              synchronous=False)
        #          self._last_stt_segment_id = seg.id
        #
        #   3. Pull queued TTS chunks the dispatch worker emitted
        #      and publish them as a LiveKit audio track.
        #
        #      chunks = pocket_tts.dequeue_chunks(
        #          target=self.call_id, voice='alba')
        #      for chunk in chunks:
        #          self._livekit_publish_audio(chunk)
        pass


class AgentVoiceBridge:
    """Public surface — what api_calls.add_agent_to_call invokes."""

    @staticmethod
    def attach_agent(db, call_id: str, agent_id: str, owner_id: str,
                     scope: Dict[str, Any]) -> Dict[str, Any]:
        """Spin up a bridge worker for (call, agent).  Idempotent on
        the (call, agent) pair — re-attaching an already-active
        agent returns the existing worker.

        Caller (CallService.attach_agent) is responsible for
        verifying the AgentJoinGrant + scope.can_voice BEFORE this
        gets called.  This method trusts the scope dict.
        """
        if not call_id or not agent_id or not owner_id:
            raise AgentBridgeError(
                "call_id, agent_id, owner_id required")
        key = (call_id, agent_id)
        with _WORKERS_LOCK:
            existing = _ACTIVE_WORKERS.get(key)
            if existing and existing.is_alive():
                return existing.to_dict()
            worker = AgentBridgeWorker(
                call_id=call_id, agent_id=agent_id,
                owner_id=owner_id, scope=scope)
            worker.start()
            _ACTIVE_WORKERS[key] = worker
        return worker.to_dict()

    @staticmethod
    def detach_agent(call_id: str, agent_id: str) -> bool:
        """Stop the bridge worker.  Returns True iff a worker was
        actually stopped.  Idempotent — calling with no active
        worker is a benign no-op."""
        key = (call_id, agent_id)
        with _WORKERS_LOCK:
            worker = _ACTIVE_WORKERS.pop(key, None)
        if worker is None:
            return False
        worker.stop()
        return True

    @staticmethod
    def list_active(call_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all active bridge workers, optionally filtered by
        call_id.  Used by ops dashboards + integration tests."""
        with _WORKERS_LOCK:
            workers = list(_ACTIVE_WORKERS.values())
        if call_id is not None:
            workers = [w for w in workers if w.call_id == call_id]
        return [w.to_dict() for w in workers]

    @staticmethod
    def shutdown_all() -> int:
        """Stop every active bridge.  Process-shutdown hook +
        test cleanup.  Returns count of workers stopped."""
        with _WORKERS_LOCK:
            workers = list(_ACTIVE_WORKERS.values())
            _ACTIVE_WORKERS.clear()
        for w in workers:
            w.stop()
        return len(workers)


__all__ = ['AgentVoiceBridge', 'AgentBridgeError', 'AgentBridgeWorker']

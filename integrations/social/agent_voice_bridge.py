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
from collections import deque
from typing import Any, Deque, Dict, List, Optional

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

# The agent's EARS: exactly ONE LiveKitTranscriptSubscriber per call_id (a single
# room→STT transcription serves every agent on that call).  Lives in the same
# bridge module — and under the SAME _WORKERS_LOCK — as the per-(call,agent)
# worker + per-worker publisher, so "agent presence in a call" has one owner, not
# a parallel lifecycle registry in a second file.  Keyed by call_id.
_ACTIVE_SUBSCRIBERS: Dict[str, 'LiveKitTranscriptSubscriber'] = {}


# ── TTS outbox: agent reply text → audio publisher ──────────────────
#
# This is the symmetric counterpart of whisper_tool's STT-segment
# queue.  agentic_router._post_agent_reply for source_kind='call'
# enqueues the agent's reply text here; the audio publisher (the
# bridge worker's TTS half OR an external adapter — LiveKit-rtc
# producer / Discord voice client) drains it, synthesizes via
# PocketTTS, and publishes audio frames into the room.
#
# Single canonical home for outbound agent-text-on-calls.  No parallel
# queues elsewhere — every producer pushes here, every consumer reads
# here, just like the STT side in whisper_tool.
#
# Bounded per (call, agent) so a stalled audio adapter can't grow
# unbounded.  Drops oldest with WARN if cap exceeded.
_TTS_OUTBOX: Dict[tuple, deque] = {}
_TTS_LOCK = threading.Lock()
_TTS_OUTBOX_CAP = 64  # chunks per (call, agent)


# Polling cadence inside the worker loop.  250ms balances STT
# latency against CPU cost; lowered to 50ms for tests via the
# `_WORKER_TICK_S` module override.
_WORKER_TICK_S = 0.25


def enqueue_tts_text(call_id: str, agent_id: str, text: str) -> bool:
    """Append agent reply text to the (call, agent) TTS outbox.

    Producer-side: agentic_router._post_agent_reply for source_kind
    ='call' calls this with the LLM reply text after GuardrailEnforcer
    .after_response has already approved it.

    Returns True iff enqueued (False on empty text or invalid keys).
    Best-effort: never raises.
    """
    if not call_id or not agent_id:
        return False
    if not text or not text.strip():
        return False
    key = (call_id, agent_id)
    with _TTS_LOCK:
        q = _TTS_OUTBOX.setdefault(key, deque())
        q.append({'text': text.strip(), 'enqueued_at': time.time()})
        while len(q) > _TTS_OUTBOX_CAP:
            evicted = q.popleft()
            logger.warning(
                "AgentVoiceBridge.enqueue_tts_text: cap %d exceeded "
                "for call=%s agent=%s; evicted text=%r",
                _TTS_OUTBOX_CAP, call_id, agent_id,
                evicted['text'][:80])
    return True


def dequeue_tts_text(call_id: str, agent_id: str,
                     limit: int = 4) -> List[Dict[str, Any]]:
    """Drain up to `limit` reply chunks for (call, agent).  Destructive
    — once popped, gone.  Bridge worker calls per tick."""
    if not call_id or not agent_id:
        return []
    key = (call_id, agent_id)
    out: List[Dict[str, Any]] = []
    with _TTS_LOCK:
        q = _TTS_OUTBOX.get(key)
        if not q:
            return []
        while q and len(out) < limit:
            out.append(q.popleft())
        if not q:
            _TTS_OUTBOX.pop(key, None)
    return out


def tts_outbox_depth(call_id: str, agent_id: str) -> int:
    """For /health + tests."""
    with _TTS_LOCK:
        q = _TTS_OUTBOX.get((call_id, agent_id))
        return len(q) if q else 0


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
        # Last STT segment id we drained from
        # ``whisper_tool.dequeue_segments`` — monotonic int per call.
        # Passed back as the ``since`` watermark next tick to avoid
        # re-processing the same segment twice if a producer re-emits.
        self._last_stt_segment_id: Optional[int] = None
        # Rolling state for the Liquid UI meet_copilot card (UNIF-G5).
        # Capped to keep the card lightweight on every emit; the card
        # only shows the most-recent N lines anyway.  Decisions and
        # action items are appended by future LLM-driven extraction
        # (out of W1.3 scope); kept here so the card is forward-
        # compatible.  Participants list is populated by adapter
        # ``list_room_members`` calls in a follow-up.
        self._transcript_lines: Deque[Dict[str, Any]] = deque(maxlen=10)
        self._decisions: List[str] = []
        self._action_items: List[str] = []
        # The agent's "mouth": a LiveKitAudioPublisher created lazily on the
        # first reply that needs voicing (so attach is cheap + no room is joined
        # for a silent agent).  None until then / when no LiveKit room exists.
        self._publisher: Any = None

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
        # Emit the initial meet_copilot card so the user sees the
        # presence indicator immediately on attach (UNIF-G5) — empty
        # transcript_lines is fine; subsequent ticks fill it.
        self._emit_meet_copilot(state='live')
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
            # Disconnect the audio publisher (leaves the LiveKit room) so a
            # detached agent doesn't keep a phantom track in the call.
            if self._publisher is not None:
                try:
                    self._publisher.stop()
                except Exception:
                    pass
                self._publisher = None
            logger.info(
                "AgentBridgeWorker stopped: call=%s agent=%s",
                self.call_id, self.agent_id)
            # Flip state to 'ended' so the renderer can dismiss /
            # collapse the card.  Idempotent overwrite — same canonical
            # emit path.
            try:
                self._emit_meet_copilot(state='ended')
            except Exception:
                pass

    def _tick(self) -> None:
        """One worker iteration — lazy-importing the integrations so
        unit tests don't drag in livekit/whisper/PocketTTS modules
        that may not be installed.

        Drains finalized STT segments from
        ``whisper_tool.dequeue_segments`` (the canonical per-call queue
        that audio-adapter producers fill — LiveKit subscriber, Discord
        voice receiver, RN mic stream, etc.).  For each segment whose
        author is NOT this agent:
          (a) Persists it as a ``ConversationEntry`` row via
              ``chat_messages.persist_external_room_event`` with
              ``kind='transcript_segment'`` so cross-channel recall
              sees the call transcript chronologically alongside
              external-room messages (UNIF-G3).
          (b) Dispatches the transcript text through
              ``agentic_router.dispatch_to_agent`` so the agent can
              respond (existing canonical agent dispatch path).

        Self-authored segments are skipped — the agent should not
        talk to itself.  Interim (non-final) segments never enter the
        queue (producers should only enqueue ``is_final=True``); a
        defensive ``is_final`` check stays here as belt-and-braces.

        TTS publish-back to the room is the producer-adapter's
        concern (LiveKit publish track / Discord voice client send),
        not the bridge's.  This loop is one-way: room → agent.

        Whichever audio source isn't installed (LiveKit RTC, discord.py
        voice receive lib, etc.) just means the producer never enqueues
        for this call_id — the worker tick is then a no-op drain.
        Always safe to run; never raises out of the loop.
        """
        # Lazy imports — avoids hard dependency on whisper / agentic_router
        # at module-import time (unit tests of attach/detach don't need
        # to drag in the LLM dispatch graph or audio toolchain).
        try:
            from integrations.service_tools.whisper_tool import (
                dequeue_segments,
            )
            from integrations.social.chat_messages import (
                persist_external_room_event,
            )
        except Exception as e:  # pragma: no cover — import-only failure
            logger.debug(
                "AgentBridgeWorker._tick: lazy imports unavailable "
                "(%s) — skipping drain", e)
            return

        # Drain the queue since our last watermark.  whisper_tool returns
        # only segments with id > since AND prunes them from the queue;
        # no double-processing.
        try:
            segments = dequeue_segments(
                call_id=self.call_id,
                since=self._last_stt_segment_id,
            )
        except Exception as e:
            logger.warning(
                "AgentBridgeWorker._tick: dequeue_segments failed "
                "(call=%s): %s", self.call_id, e)
            return

        if not segments:
            return

        for seg in segments:
            seg_id = seg.get('segment_id')
            if not seg.get('is_final', True):
                # Defensive: producers should not enqueue interim, but
                # if they do, we just skip + keep advancing the watermark.
                if seg_id is not None:
                    self._last_stt_segment_id = seg_id
                continue

            author_id = seg.get('author_id')
            text = (seg.get('text') or '').strip()
            if not text:
                if seg_id is not None:
                    self._last_stt_segment_id = seg_id
                continue

            # Persist for cross-channel recall (UNIF-G3) — same canonical
            # writer used by adapter message handlers.  Voice transcripts
            # carry kind='transcript_segment' + extra={t0, t1, speaker}
            # per the persist_external_room_event contract.
            try:
                persist_external_room_event(
                    user_id=str(self.owner_id),
                    platform=str(self.scope.get('platform') or 'livekit'),
                    room_id=str(self.call_id),
                    sender_id=str(author_id or 'unknown'),
                    text=text,
                    kind='transcript_segment',
                    timestamp=seg.get('t1') or seg.get('t0'),
                    lang=seg.get('lang'),
                    extra={
                        't0': seg.get('t0'),
                        't1': seg.get('t1'),
                        'speaker': seg.get('speaker'),
                    },
                )
            except Exception as e:
                logger.warning(
                    "AgentBridgeWorker._tick: persist failed "
                    "(call=%s, seg=%s): %s",
                    self.call_id, seg_id, e)

            # Append to rolling transcript and emit the meet_copilot
            # Liquid UI card update (UNIF-G5).  Idempotent overwrite —
            # the renderer (web AgentOverlay.jsx + RN LiquidOverlay.js +
            # iOS shared JS) treats each emit as the canonical full
            # state, so stragglers / re-orders self-correct on the
            # next tick.  Best-effort: emit failures don't block.
            self._transcript_lines.append({
                'speaker': seg.get('speaker') or author_id,
                'text': text,
                't0': seg.get('t0'),
                't1': seg.get('t1'),
            })
            self._emit_meet_copilot(state='live')

            # Skip self-authored — the agent shouldn't dispatch on its
            # own previous TTS-back-into-the-room.
            if author_id and str(author_id) == str(self.agent_id):
                if seg_id is not None:
                    self._last_stt_segment_id = seg_id
                continue

            # Dispatch through the canonical agent router so existing
            # GuardrailEnforcer.before_dispatch / .after_response gates
            # apply identically to call-driven prompts as to chat-driven.
            try:
                from integrations.agentic_router import dispatch_to_agent
                dispatch_to_agent(
                    agent_id=self.agent_id,
                    prompt=text,
                    context={
                        'source_kind': 'call',
                        'source_id': self.call_id,
                        'author_id': author_id,
                        'owner_id': self.owner_id,
                        'tenant_id': self.scope.get('tenant_id'),
                        'platform': self.scope.get('platform') or 'livekit',
                        'lang': seg.get('lang'),
                    },
                    synchronous=False,
                )
            except Exception as e:
                logger.warning(
                    "AgentBridgeWorker._tick: dispatch_to_agent failed "
                    "(call=%s, agent=%s, seg=%s): %s",
                    self.call_id, self.agent_id, seg_id, e)

            if seg_id is not None:
                self._last_stt_segment_id = seg_id

        # ── Half B: TTS outbox → audio publisher ────────────────────
        # Drain any reply text that agentic_router._post_agent_reply
        # enqueued for this (call, agent) and hand it to the audio
        # publisher.  When livekit-rtc is absent (flat / Nunba bundled)
        # we log the reply text so the call audit trail still reflects
        # the agent's contribution; the audio side stays no-op.
        try:
            replies = dequeue_tts_text(self.call_id, self.agent_id,
                                       limit=2)
        except Exception as e:
            replies = []
            logger.warning(
                "AgentBridgeWorker._tick: dequeue_tts_text failed "
                "(call=%s, agent=%s): %s",
                self.call_id, self.agent_id, e)
        for r in replies:
            text = r.get('text', '')
            if not text:
                continue
            try:
                self._publish_audio_for(text)
            except Exception as e:
                logger.warning(
                    "AgentBridgeWorker._tick: _publish_audio_for "
                    "failed (call=%s, agent=%s): %s",
                    self.call_id, self.agent_id, e)

    def _publish_audio_for(self, text: str) -> None:
        """Synthesize ``text`` via PocketTTS and publish the audio frames into
        the LiveKit room through this worker's ``LiveKitAudioPublisher`` (the
        agent's "mouth").

        Degrades cleanly: when the realtime SDK is absent (flat / Nunba bundled)
        OR the call has no LiveKit room (p2p mesh / central), we log the reply at
        INFO so the call audit trail still records the agent's spoken
        contribution; no audio is published.  Best-effort — never raises out of
        the tick loop.
        """
        if not text:
            return
        if not _HAS_LIVEKIT_RTC:
            logger.info(
                "AgentBridgeWorker._publish_audio_for: livekit (rtc) "
                "absent; reply queued but not voiced — "
                "call=%s agent=%s text=%r",
                self.call_id, self.agent_id, text[:120])
            return
        pub = self._ensure_publisher()
        if pub is None:
            logger.info(
                "AgentBridgeWorker._publish_audio_for: no LiveKit room for "
                "this call (p2p/central) — reply not voiced — "
                "call=%s agent=%s text=%r",
                self.call_id, self.agent_id, text[:120])
            return
        pcm, rate, channels = self._synthesize_pcm(text)
        if not pcm:
            return
        pub.push_pcm(pcm, src_rate=rate, src_channels=channels)

    def _ensure_publisher(self):
        """Return this worker's LiveKitAudioPublisher, creating + starting it on
        first use.  Returns None when there is no LiveKit room to publish into
        (issue_token returns a non-'livekit' mode) or the SDK/connect fails — the
        caller then logs the reply text instead of voicing it."""
        if self._publisher is not None:
            return self._publisher if self._publisher.is_alive() else None
        try:
            from integrations.social.livekit_service import LiveKitService
            from integrations.social.livekit_audio_publisher import (
                LiveKitAudioPublisher,
            )
        except Exception as e:  # pragma: no cover — import-only failure
            logger.debug(
                "AgentBridgeWorker._ensure_publisher: imports unavailable "
                "(%s)", e)
            return None
        # Mint the agent's publisher token via the SAME issuer humans use; the
        # agent joins the room as identity=agent_id with publish rights.
        try:
            tok = LiveKitService.issue_token(
                self.call_id, self.agent_id,
                can_publish=True, is_agent=True)
        except Exception as e:
            logger.warning(
                "AgentBridgeWorker._ensure_publisher: issue_token failed "
                "(call=%s): %s", self.call_id, e)
            return None
        if (tok.get('mode') != 'livekit' or not tok.get('token')
                or not tok.get('url')):
            # p2p mesh / livekit_pending / central — nothing to publish into.
            return None
        pub = LiveKitAudioPublisher(self.call_id, tok['url'], tok['token'])
        if not pub.start():
            return None
        self._publisher = pub
        return pub

    def _synthesize_pcm(self, text: str):
        """``text`` → (pcm_bytes, sample_rate, channels) via PocketTTS.

        PocketTTS writes a .wav (``pocket_tts_synthesize`` → JSON ``{path}``);
        we read it back as PCM16 with the stdlib ``wave`` module.  The publisher
        resamples to the LiveKit publish format, so we pass the wav's native
        rate/channels straight through.  Returns ``(b'', 0, 1)`` on any failure
        (TTS error, missing file, unreadable wav) — never raises."""
        try:
            import json
            import wave
            from integrations.service_tools.pocket_tts_tool import (
                pocket_tts_synthesize,
            )
            res = json.loads(pocket_tts_synthesize(text))
            path = res.get('path')
            if not path:
                logger.warning(
                    "AgentBridgeWorker._synthesize_pcm: TTS produced no audio "
                    "(call=%s): %s", self.call_id, res.get('error', res))
                return b'', 0, 1
            with wave.open(path, 'rb') as wf:
                channels = wf.getnchannels()
                rate = wf.getframerate()
                pcm = wf.readframes(wf.getnframes())
            return pcm, rate, channels
        except Exception as e:
            logger.warning(
                "AgentBridgeWorker._synthesize_pcm failed (call=%s): %s",
                self.call_id, e)
            return b'', 0, 1

    def _emit_meet_copilot(self, state: str = 'live') -> None:
        """Push the rolling meet_copilot card state to the user's Liquid UI
        surface (UNIF-G5).

        Single canonical emit site for the meet_copilot component-type
        defined in ``liquid_ui_service.py:COMPONENT_TYPES`` and rendered
        by web ``AgentOverlay.jsx`` + RN ``LiquidOverlay.js`` + iOS
        shared JS.  Idempotent overwrite — every emit is the full state
        the renderer should display, so dropped / re-ordered events
        self-correct on the next tick.

        Best-effort: never raises out of the bridge loop.  If the
        LiquidUIService isn't registered (Nunba bundled mode without
        the agent engine), the emit is a logged no-op.
        """
        try:
            from core.platform.service_registry import ServiceRegistry
            svc = ServiceRegistry.get('LiquidUIService')
        except Exception as e:
            logger.debug(
                "AgentBridgeWorker._emit_meet_copilot: ServiceRegistry "
                "unavailable (%s) — skipping emit", e)
            return
        if svc is None:
            return
        try:
            svc.agent_ui_update(
                str(self.agent_id),
                {
                    'type': 'meet_copilot',
                    'call_id': str(self.call_id),
                    'platform': str(
                        self.scope.get('platform') or 'livekit'),
                    'room_id': str(
                        self.scope.get('room_id') or self.call_id),
                    'state': state,
                    'transcript_lines': list(self._transcript_lines),
                    'decisions': list(self._decisions),
                    'action_items': list(self._action_items),
                    'participants': list(
                        self.scope.get('participants') or []),
                    'agent_role': str(
                        self.scope.get('role') or 'co_pilot'),
                },
            )
        except Exception as e:
            logger.warning(
                "AgentBridgeWorker._emit_meet_copilot: agent_ui_update "
                "failed (call=%s, agent=%s): %s",
                self.call_id, self.agent_id, e)


def _ensure_call_subscriber(call_id: str) -> None:
    """Start ONE LiveKitTranscriptSubscriber (the room→STT "ears") per call,
    idempotently, so the per-(call,agent) bridge workers actually have transcript
    segments to drain.  Without this the worker drains a queue nobody fills and
    the agent is deaf — the subscriber class shipped but was never wired into the
    attach flow.

    Reuses the existing subscriber class + ``LiveKitService.issue_token`` and the
    same canonical per-call STT queue the worker drains — no parallel path.  No-op
    when livekit-rtc is absent or the call has no LiveKit room (p2p/central): the
    worker then drains an empty queue exactly as before.  Best-effort; never
    raises out to the caller."""
    if not _HAS_LIVEKIT_RTC:
        return
    with _WORKERS_LOCK:
        sub = _ACTIVE_SUBSCRIBERS.get(call_id)
        if sub is not None and sub.is_alive():
            return
    try:
        from integrations.social.livekit_service import LiveKitService
        from integrations.social.livekit_transcript_subscriber import (
            LiveKitTranscriptSubscriber,
        )
    except Exception as e:  # pragma: no cover — import-only failure
        logger.debug("AgentVoiceBridge._ensure_call_subscriber: imports "
                     "unavailable (%s)", e)
        return
    # Subscribe-only token (can_publish=False) for the room's single transcriber
    # identity — the SAME issuer humans + the publisher use.
    try:
        tok = LiveKitService.issue_token(
            call_id, 'transcript-bot', can_publish=False, is_agent=True)
    except Exception as e:
        logger.warning("AgentVoiceBridge._ensure_call_subscriber: issue_token "
                       "failed (call=%s): %s", call_id, e)
        return
    if (tok.get('mode') != 'livekit' or not tok.get('token')
            or not tok.get('url')):
        return  # p2p mesh / central / pending — no room to subscribe to
    sub = LiveKitTranscriptSubscriber(call_id, tok['url'], tok['token'])
    if not sub.start():
        return
    with _WORKERS_LOCK:
        existing = _ACTIVE_SUBSCRIBERS.get(call_id)
        if existing is not None and existing.is_alive():
            sub.stop()  # lost a race — keep the already-running one
            return
        _ACTIVE_SUBSCRIBERS[call_id] = sub


def _maybe_stop_call_subscriber(call_id: str) -> None:
    """Stop + drop the call's shared subscriber once NO bridge workers remain for
    that call (the last agent detached).  Best-effort; never raises."""
    with _WORKERS_LOCK:
        still_serving = any(k[0] == call_id for k in _ACTIVE_WORKERS)
        sub = None if still_serving else _ACTIVE_SUBSCRIBERS.pop(call_id, None)
    if sub is not None:
        try:
            sub.stop()
        except Exception:
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
                result = existing.to_dict()
            else:
                worker = AgentBridgeWorker(
                    call_id=call_id, agent_id=agent_id,
                    owner_id=owner_id, scope=scope)
                worker.start()
                _ACTIVE_WORKERS[key] = worker
                result = worker.to_dict()
        # Ears: ensure the per-call transcript subscriber (room→STT queue) is
        # running so the worker's _tick has segments to drain.  Idempotent and
        # called OUTSIDE _WORKERS_LOCK (the helper takes the lock itself).
        _ensure_call_subscriber(call_id)
        return result

    @staticmethod
    def detach_agent(call_id: str, agent_id: str) -> bool:
        """Stop the bridge worker.  Returns True iff a worker was
        actually stopped.  Idempotent — calling with no active
        worker is a benign no-op."""
        key = (call_id, agent_id)
        with _WORKERS_LOCK:
            worker = _ACTIVE_WORKERS.pop(key, None)
        # Drop any pending TTS chunks for this (call, agent) so a
        # later same-key attach doesn't replay stale audio.
        with _TTS_LOCK:
            _TTS_OUTBOX.pop(key, None)
        if worker is not None:
            worker.stop()
        # Ears: tear down the call's shared subscriber once the last agent on
        # this call has detached (no-op while other agents remain).
        _maybe_stop_call_subscriber(call_id)
        return worker is not None

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
            subs = list(_ACTIVE_SUBSCRIBERS.values())
            _ACTIVE_SUBSCRIBERS.clear()
        with _TTS_LOCK:
            _TTS_OUTBOX.clear()
        for w in workers:
            w.stop()
        for s in subs:
            try:
                s.stop()
            except Exception:
                pass
        return len(workers)


__all__ = [
    'AgentVoiceBridge',
    'AgentBridgeError',
    'AgentBridgeWorker',
    'enqueue_tts_text',
    'dequeue_tts_text',
    'tts_outbox_depth',
]

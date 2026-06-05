"""
livekit_audio_publisher — the PUBLISH (TTS → room) half, symmetric to
``livekit_transcript_subscriber`` (the room → STT half).  Phase 7d.B / #64.

An ``AgentBridgeWorker`` (``agent_voice_bridge``) holds the agent's seat in a
call.  When the agent replies, ``agentic_router`` enqueues the reply text and the
worker drains it (Half-B of ``_tick``) → synthesizes via PocketTTS → and this
publishes the audio frames into the LiveKit room: connect as the agent
participant, publish ONE audio track, stream PCM into it.

Shares the daemon-thread / asyncio-loop / connect / teardown / resample
machinery with the subscriber via ``_LiveKitRoomThread`` (``_livekit_room.py``);
only the publish-specific ``_async_main`` + the PCM queue + 10ms framing live
here.  Lib-gated through the base: no SDK → ``start()`` is a no-op and
``agent_voice_bridge`` keeps logging the reply text for the call audit trail.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional, Callable

from integrations.social._livekit_room import (
    _LiveKitRoomThread,
    HAS_LIVEKIT_RTC,
    livekit_rtc,
    resample_pcm16,
)

logger = logging.getLogger('hevolve_social')


# LiveKit's default capture format: 48kHz s16le mono — publish at this rate so
# the SFU never has to resample our track.
_PUBLISH_RATE = 48000
_PUBLISH_CHANNELS = 1
_SAMPLE_WIDTH = 2  # s16le
_FRAME_MS = 10     # LiveKit's native frame granularity


def _iter_frames(pcm: bytes, rate: int = _PUBLISH_RATE,
                 channels: int = _PUBLISH_CHANNELS, frame_ms: int = _FRAME_MS):
    """Yield fixed-size ``frame_ms`` PCM16 chunks from ``pcm``; the trailing
    partial chunk is zero-padded so ``samples_per_channel`` is exact.  Pure —
    unit-testable."""
    bytes_per_frame = int(rate * frame_ms / 1000) * channels * _SAMPLE_WIDTH
    if bytes_per_frame <= 0:
        return
    for i in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[i:i + bytes_per_frame]
        if len(chunk) < bytes_per_frame:
            chunk = chunk + b'\x00' * (bytes_per_frame - len(chunk))
        yield chunk


class LiveKitAudioPublisher(_LiveKitRoomThread):
    """One publisher per (call_id, agent).  Connects as the agent participant,
    publishes a single audio track, streams PCM frames pushed via ``push_pcm``.

    ``source_factory`` is the publish-side test seam (returns an AudioSource-like
    object), alongside the base's ``room_factory``.
    """

    _THREAD_PREFIX = 'livekit-audio-pub'

    def __init__(self, call_id: str, livekit_url: str, token: str,
                 room_factory: Optional[Callable[[], Any]] = None,
                 source_factory: Optional[Callable[[int, int], Any]] = None,
                 sample_rate: int = _PUBLISH_RATE,
                 num_channels: int = _PUBLISH_CHANNELS):
        super().__init__(call_id, livekit_url, token, room_factory)
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self._source_factory = source_factory
        self._ready = threading.Event()  # set once the track is published
        self._queue: Any = None          # asyncio.Queue, created on the loop
        self._source: Any = None

    # ── Public API (publish-specific) ───────────────────────────

    def push_pcm(self, pcm: bytes, src_rate: int = 24000,
                 src_channels: int = 1) -> bool:
        """Resample one PCM16 blob (a TTS utterance) to the publish format and
        hand it to the asyncio loop for framed playout.  Sync — called from the
        bridge worker's thread.  Best-effort; a no-op (False) when the loop isn't
        ready (SDK absent, not connected, or stopped)."""
        if self._stop_evt.is_set() or not pcm:
            return False
        out = resample_pcm16(pcm, src_rate, src_channels,
                             self.sample_rate, self.num_channels)
        if not out:
            return False
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None:
            return False
        try:
            import asyncio
            asyncio.run_coroutine_threadsafe(queue.put(out), loop)
            return True
        except Exception as e:
            logger.debug('livekit_audio_publisher: enqueue failed (call=%s): %s',
                         self.call_id, e)
            return False

    # ── Base hooks ──────────────────────────────────────────────

    def _on_loop_start(self) -> None:
        import asyncio
        self._queue = asyncio.Queue()

    def _on_stop(self) -> None:
        # Wake the drain loop promptly with a sentinel (the 0.5s get() timeout
        # is the backstop).  Best-effort on a possibly-closing loop.
        loop = self._loop
        queue = self._queue
        if loop is not None and queue is not None:
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            except Exception:
                pass

    def _make_source(self) -> Any:
        if self._source_factory is not None:
            return self._source_factory(self.sample_rate, self.num_channels)
        return livekit_rtc.AudioSource(  # type: ignore[union-attr]
            self.sample_rate, self.num_channels)

    async def _async_main(self) -> None:
        import asyncio
        room = self._make_room()
        if room is None:
            return
        self._room = room
        try:
            await room.connect(self.livekit_url, self.token)
        except Exception as e:
            logger.warning('livekit_audio_publisher: room connect failed '
                           '(call=%s): %s', self.call_id, e)
            return
        try:
            source = self._make_source()
            track = livekit_rtc.LocalAudioTrack.create_audio_track(  # type: ignore[union-attr]
                'agent-voice', source)
            options = livekit_rtc.TrackPublishOptions(  # type: ignore[union-attr]
                source=livekit_rtc.TrackSource.SOURCE_MICROPHONE)
            await room.local_participant.publish_track(track, options)
            self._source = source
            self._ready.set()
        except Exception as e:
            logger.warning('livekit_audio_publisher: publish_track failed '
                           '(call=%s): %s', self.call_id, e)
            return
        # Drain: each queued utterance → 10ms frames → capture_frame (paces
        # playout to real time because capture_frame awaits buffer room).
        while not self._stop_evt.is_set():
            try:
                blob = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if blob is None:  # stop() sentinel
                break
            for frame_bytes in _iter_frames(blob, self.sample_rate,
                                            self.num_channels, _FRAME_MS):
                if self._stop_evt.is_set():
                    break
                try:
                    samples = len(frame_bytes) // (_SAMPLE_WIDTH * self.num_channels)
                    frame = livekit_rtc.AudioFrame(  # type: ignore[union-attr]
                        data=frame_bytes,
                        sample_rate=self.sample_rate,
                        num_channels=self.num_channels,
                        samples_per_channel=samples)
                    await self._source.capture_frame(frame)
                except Exception as e:
                    logger.debug('livekit_audio_publisher: capture_frame failed '
                                 '(call=%s): %s', self.call_id, e)
                    break


__all__ = ['LiveKitAudioPublisher', 'HAS_LIVEKIT_RTC', 'resample_pcm16',
           '_iter_frames']

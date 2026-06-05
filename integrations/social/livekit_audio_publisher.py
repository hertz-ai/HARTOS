"""
livekit_audio_publisher — the PUBLISH (TTS → room) half, symmetric to
``livekit_transcript_subscriber`` (the room → STT half).  Phase 7d.B / #64.

An ``AgentBridgeWorker`` (``agent_voice_bridge``) holds the agent's seat in a
call.  When the agent replies, ``agentic_router`` enqueues the reply text and the
worker drains it (Half-B of ``_tick``) → synthesizes via PocketTTS → and must
PUBLISH the audio frames into the LiveKit room.  This module owns that publish:
it connects to the room as the agent participant, publishes ONE audio track, and
streams PCM frames into it.

Single canonical publisher — there is no other livekit-publish path.  This is the
exact mirror of the subscriber, on purpose (same threading model, same lib gate,
same ``audioop`` resampling, same ``room_factory`` test seam): the two together
are the agent's ears (subscriber) and mouth (this) in a call.

Lib gate:
    Importable even when the LiveKit realtime SDK (the ``livekit`` package, which
    provides ``livekit.rtc``) is NOT installed.  ``HAS_LIVEKIT_RTC`` is False
    then and ``start()`` is a no-op, so flat / Nunba-bundled deploys without the
    SDK keep today's log-only behaviour (``agent_voice_bridge`` already logs the
    reply text for the call audit trail).

Threading model:
    ``livekit.rtc`` is asyncio-based.  We run one event loop in a daemon thread
    (mirrors ``livekit_transcript_subscriber`` and ``whisper_tool``).  Connect +
    publish happen on that loop; the worker's sync thread feeds PCM via
    ``push_pcm`` which hands frames to the loop with
    ``run_coroutine_threadsafe``.  ``stop()`` tears the loop + room down cleanly.

Resampling + framing:
    PocketTTS emits whatever rate its model uses (often 24kHz mono); the LiveKit
    ``AudioSource`` is created at a fixed (sample_rate, num_channels).  We convert
    with stdlib ``audioop.ratecv`` (same helper shape as the subscriber) and chunk
    to 10ms frames so ``capture_frame`` paces playout to real time.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger('hevolve_social')


try:
    from livekit import rtc as livekit_rtc  # type: ignore
    HAS_LIVEKIT_RTC = True
except Exception:
    livekit_rtc = None  # type: ignore
    HAS_LIVEKIT_RTC = False


# LiveKit's default capture format: 48kHz s16le mono.  We publish at this rate so
# the SFU never has to resample our track.
_PUBLISH_RATE = 48000
_PUBLISH_CHANNELS = 1
_SAMPLE_WIDTH = 2  # s16le
_FRAME_MS = 10     # 10ms frames — LiveKit's native granularity


def _resample_pcm(pcm: bytes, src_rate: int, src_channels: int,
                  dst_rate: int = _PUBLISH_RATE,
                  dst_channels: int = _PUBLISH_CHANNELS) -> bytes:
    """Convert arbitrary PCM16 to (dst_rate, dst_channels).  Stdlib audioop,
    no new dep.  Returns ``b''`` on any conversion error.  Pure — unit-testable
    without a LiveKit room."""
    if not pcm:
        return b''
    if src_rate == dst_rate and src_channels == dst_channels:
        return pcm
    try:
        import audioop
        if src_channels > 1 and dst_channels == 1:
            pcm = audioop.tomono(pcm, _SAMPLE_WIDTH, 1.0, 1.0)
        if src_rate != dst_rate:
            pcm, _ = audioop.ratecv(
                pcm, _SAMPLE_WIDTH, dst_channels, src_rate, dst_rate, None)
        return pcm
    except Exception as e:
        logger.debug('livekit_audio_publisher: resample failed: %s', e)
        return b''


def _iter_frames(pcm: bytes, rate: int = _PUBLISH_RATE,
                 channels: int = _PUBLISH_CHANNELS, frame_ms: int = _FRAME_MS):
    """Yield fixed-size ``frame_ms`` PCM16 chunks from ``pcm``.  The trailing
    partial chunk is zero-padded to a full frame so ``samples_per_channel`` is
    exact.  Pure — unit-testable."""
    bytes_per_frame = int(rate * frame_ms / 1000) * channels * _SAMPLE_WIDTH
    if bytes_per_frame <= 0:
        return
    for i in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[i:i + bytes_per_frame]
        if len(chunk) < bytes_per_frame:
            chunk = chunk + b'\x00' * (bytes_per_frame - len(chunk))
        yield chunk


class LiveKitAudioPublisher:
    """One publisher per (call_id, agent).  Connects as the agent participant,
    publishes a single audio track, streams PCM frames pushed via ``push_pcm``.

    Constructor seams allow tests to inject:
      - ``room_factory``   : callable returning a Room-like object instead of
                             importing livekit.
      - ``source_factory`` : callable(rate, channels) returning an
                             AudioSource-like object (so ``push_pcm`` framing +
                             ``capture_frame`` can be tested without the SDK).
    """

    def __init__(self, call_id: str, livekit_url: str, token: str,
                 room_factory: Optional[Callable[[], Any]] = None,
                 source_factory: Optional[Callable[[int, int], Any]] = None,
                 sample_rate: int = _PUBLISH_RATE,
                 num_channels: int = _PUBLISH_CHANNELS):
        self.call_id = str(call_id) if call_id is not None else ''
        self.livekit_url = livekit_url
        self.token = token
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self._room_factory = room_factory
        self._source_factory = source_factory
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._ready = threading.Event()  # set once track is published
        self._loop: Any = None           # asyncio loop, set in thread
        self._queue: Any = None          # asyncio.Queue, created in thread
        self._room: Any = None
        self._source: Any = None

    # ── Public API ──────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the daemon thread + asyncio loop and connect/publish.  No-op
        (returns False) when the SDK isn't installed or required fields are
        missing.  Returns True iff a thread was started."""
        if not HAS_LIVEKIT_RTC and self._room_factory is None:
            logger.info(
                'livekit_audio_publisher: livekit (rtc) not installed — '
                'start() is a no-op (call_id=%s)', self.call_id)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.call_id or not self.livekit_url or not self.token:
            logger.warning(
                'livekit_audio_publisher: missing required fields '
                '(call_id=%r url=%r token=%r)',
                self.call_id, self.livekit_url, bool(self.token))
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'livekit-audio-pub-{self.call_id[:12]}',
        )
        self._thread.start()
        return True

    def push_pcm(self, pcm: bytes, src_rate: int = 24000,
                 src_channels: int = 1) -> bool:
        """Resample one PCM16 blob (a TTS utterance) to the publish format and
        hand it to the asyncio loop for framed playout.  Sync — called from the
        bridge worker's thread.  Returns True iff the audio was enqueued.

        Best-effort: never raises.  A no-op (False) when the loop isn't ready
        (SDK absent, not yet connected, or stopped) — the caller already logs
        the reply text for the audit trail."""
        if self._stop_evt.is_set() or not pcm:
            return False
        out = _resample_pcm(pcm, src_rate, src_channels,
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
            logger.debug('livekit_audio_publisher: enqueue failed '
                         '(call=%s): %s', self.call_id, e)
            return False

    def stop(self) -> None:
        """Signal teardown + disconnect the room.  Idempotent."""
        self._stop_evt.set()
        loop = self._loop
        queue = self._queue
        # Wake the drain loop so it observes the stop flag promptly.
        if loop is not None and queue is not None:
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            except Exception:
                pass
        room = self._room
        if loop is not None and room is not None:
            try:
                import asyncio
                fut = asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(room), loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── asyncio-side connect + publish + drain ──────────────────

    def _run(self) -> None:
        try:
            import asyncio
        except Exception:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.warning('livekit_audio_publisher: loop crashed: %s', e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _make_room(self) -> Any:
        if self._room_factory is not None:
            try:
                return self._room_factory()
            except Exception as e:
                logger.warning('livekit_audio_publisher: room_factory '
                               'failed: %s', e)
                return None
        if not HAS_LIVEKIT_RTC:
            return None
        try:
            return livekit_rtc.Room()  # type: ignore[union-attr]
        except Exception as e:
            logger.warning('livekit_audio_publisher: Room() failed: %s', e)
            return None

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
        # Publish one audio track backed by an AudioSource we push frames into.
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
                    logger.debug('livekit_audio_publisher: capture_frame '
                                 'failed (call=%s): %s', self.call_id, e)
                    break

    async def _async_disconnect(self, room) -> None:
        try:
            disconnect = getattr(room, 'disconnect', None)
            if disconnect is None:
                return
            res = disconnect()
            if hasattr(res, '__await__'):
                await res
        except Exception:
            pass


__all__ = ['LiveKitAudioPublisher', 'HAS_LIVEKIT_RTC',
           '_resample_pcm', '_iter_frames']

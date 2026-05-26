"""
livekit_transcript_subscriber — UNIF-G7 / W1.7 Producer B.

Subscribes to remote audio tracks in a LiveKit room, resamples each
participant's PCM frames, and pushes them through the canonical
streaming-STT WebSocket so Producer C's ``?call_id=&user_id=`` hook
lands the final transcripts in
``whisper_tool.enqueue_stt_segment(call_id, ...)``.

Single canonical sink: every audio source in HARTOS — Discord voice
recv (Producer A), LiveKit RTC (this module), RN mic stream (Producer
C's direct caller) — funnels into the SAME per-call STT queue that
``agent_voice_bridge._tick`` drains.  No parallel paths.

Lib gate:
    The module is importable even when ``livekit-rtc`` is NOT
    installed.  ``HAS_LIVEKIT_RTC`` is False in that case and
    ``LiveKitTranscriptSubscriber.start()`` becomes a synchronous
    no-op so today's bridge-worker behavior is preserved.

Threading model:
    ``livekit-rtc`` is asyncio-based.  We run an asyncio event loop
    in a daemon thread (mirrors the existing ``start_stt_stream_
    server`` pattern in ``whisper_tool``).  Each subscribed
    ``RemoteAudioTrack`` spawns a per-track frame-consumer task that
    pushes resampled PCM through a per-participant sync ``websockets``
    client back into the local STT WS.  No new event loops in the
    request hot path; ``stop()`` cleanly tears down both.

Resampling:
    LiveKit publishes 48kHz s16le mono per audio frame (default RTC
    config).  Some senders may publish at other rates — we use stdlib
    ``audioop.ratecv`` to convert to 16kHz mono if needed.  When the
    frame is already 16kHz mono we forward it as-is (zero-copy).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


try:
    from livekit import rtc as livekit_rtc  # type: ignore
    HAS_LIVEKIT_RTC = True
except Exception:
    livekit_rtc = None  # type: ignore
    HAS_LIVEKIT_RTC = False


# Target STT-server format.
_TARGET_RATE = 16000
_TARGET_CHANNELS = 1
_SAMPLE_WIDTH = 2  # s16le


def _resample_to_16k_mono(pcm: bytes, src_rate: int,
                          src_channels: int) -> bytes:
    """Convert arbitrary PCM16 to 16kHz mono.  Stdlib audioop, no
    new dep.  Returns ``b''`` on any conversion error.
    """
    if not pcm:
        return b''
    if src_rate == _TARGET_RATE and src_channels == _TARGET_CHANNELS:
        return pcm
    try:
        import audioop
        if src_channels > 1:
            pcm = audioop.tomono(pcm, _SAMPLE_WIDTH, 1.0, 1.0)
        if src_rate != _TARGET_RATE:
            pcm, _ = audioop.ratecv(
                pcm, _SAMPLE_WIDTH, _TARGET_CHANNELS,
                src_rate, _TARGET_RATE, None)
        return pcm
    except Exception as e:
        logger.debug('livekit_transcript_subscriber: resample failed: %s', e)
        return b''


class LiveKitTranscriptSubscriber:
    """One subscriber per (call_id, livekit_room).  Connects, listens,
    pipes PCM through the local STT WS, tears down on ``stop()``.

    Constructor seams allow tests to inject:
      - ``room_factory``    : callable returning an awaitable Room-like
                              object instead of importing livekit.
      - ``ws_connect``      : sync WS connect callable; defaults to
                              ``websockets.sync.client.connect``.
      - ``stt_port_provider``: returns the local STT WS port; defaults
                              to ``whisper_tool.get_stt_stream_port``.
    """

    def __init__(self, call_id: str, livekit_url: str, token: str,
                 room_factory: Optional[Callable[[], Any]] = None,
                 ws_connect: Optional[Callable[..., Any]] = None,
                 stt_port_provider: Optional[Callable[[], Optional[int]]] = None):
        self.call_id = str(call_id) if call_id is not None else ''
        self.livekit_url = livekit_url
        self.token = token
        self._room_factory = room_factory
        self._ws_connect = ws_connect
        self._stt_port_provider = stt_port_provider
        # Per-participant WS clients keyed by participant identity.
        self._ws_per_participant: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._loop: Any = None  # asyncio.AbstractEventLoop, set in thread
        self._room: Any = None

    # ── Public API ──────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the daemon thread + asyncio loop.  No-op (returns
        False) when livekit-rtc isn't installed.  Returns True iff a
        thread was started."""
        if not HAS_LIVEKIT_RTC and self._room_factory is None:
            logger.info(
                'livekit_transcript_subscriber: livekit-rtc not '
                'installed — start() is a no-op (call_id=%s)',
                self.call_id)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.call_id or not self.livekit_url or not self.token:
            logger.warning(
                'livekit_transcript_subscriber: missing required '
                'fields (call_id=%r url=%r token=%r)',
                self.call_id, self.livekit_url, bool(self.token))
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'livekit-transcript-{self.call_id[:12]}',
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Signal the thread to tear down.  Idempotent."""
        self._stop_evt.set()
        # Schedule the room disconnect on the asyncio loop.
        loop = self._loop
        room = self._room
        if loop is not None and room is not None:
            try:
                import asyncio as _asyncio
                fut = _asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(room), loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass
        # Close any per-participant WS clients.
        with self._lock:
            wss = list(self._ws_per_participant.values())
            self._ws_per_participant.clear()
        for ws in wss:
            try:
                ws.close()
            except Exception:
                pass

    # ── Producer C bridge per participant ───────────────────────

    def _resolve_stt_port(self) -> Optional[int]:
        if self._stt_port_provider is not None:
            try:
                return self._stt_port_provider()
            except Exception:
                return None
        try:
            from integrations.service_tools.whisper_tool import (
                get_stt_stream_port, start_stt_stream_server,
            )
            return get_stt_stream_port() or start_stt_stream_server()
        except Exception:
            return None

    def _resolve_ws_connect(self) -> Optional[Callable[..., Any]]:
        if self._ws_connect is not None:
            return self._ws_connect
        try:
            from websockets.sync.client import connect  # type: ignore
            return connect
        except Exception:
            return None

    def _get_ws_for(self, participant_identity: str) -> Any:
        with self._lock:
            existing = self._ws_per_participant.get(participant_identity)
            if existing is not None:
                return existing
            if self._stop_evt.is_set():
                return None
        port = self._resolve_stt_port()
        if not port:
            return None
        connect = self._resolve_ws_connect()
        if connect is None:
            return None
        url = (f'ws://127.0.0.1:{port}/?call_id={self.call_id}'
               f'&user_id={participant_identity}')
        try:
            ws = connect(url, max_size=2 * 1024 * 1024)
        except Exception as e:
            logger.warning(
                'livekit_transcript_subscriber: WS connect failed '
                '(%s): %s', url, e)
            return None
        with self._lock:
            if self._stop_evt.is_set():
                try:
                    ws.close()
                except Exception:
                    pass
                return None
            self._ws_per_participant[participant_identity] = ws
        # Drain inbound responses so the WS keeps reading; Producer C
        # handles the enqueue server-side.
        threading.Thread(
            target=self._drain_loop, args=(ws,), daemon=True,
            name=f'lk-drain-{participant_identity[:12]}',
        ).start()
        return ws

    def _drain_loop(self, ws) -> None:
        try:
            for _msg in ws:
                if self._stop_evt.is_set():
                    return
        except Exception:
            return

    def push_frame(self, participant_identity: str, pcm: bytes,
                   src_rate: int = 48000, src_channels: int = 1) -> bool:
        """Resample + forward one PCM frame for a participant.

        Public seam — tests can drive this directly without a real
        LiveKit Room.  Returns True iff the frame was sent through
        the WS (False on no-op / error).
        """
        if self._stop_evt.is_set():
            return False
        if not participant_identity or not pcm:
            return False
        out = _resample_to_16k_mono(pcm, src_rate, src_channels)
        if not out:
            return False
        ws = self._get_ws_for(participant_identity)
        if ws is None:
            return False
        try:
            ws.send(out)
            return True
        except Exception as e:
            logger.debug(
                'livekit_transcript_subscriber: send failed '
                '(participant=%s): %s', participant_identity, e)
            with self._lock:
                self._ws_per_participant.pop(participant_identity, None)
            try:
                ws.close()
            except Exception:
                pass
            return False

    # ── asyncio-side LiveKit subscription ──────────────────────

    def _run(self) -> None:
        try:
            import asyncio
        except Exception:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.warning(
                'livekit_transcript_subscriber: loop crashed: %s', e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _async_main(self) -> None:
        room = self._make_room()
        if room is None:
            return
        self._room = room
        # Wire participant-track callbacks BEFORE connecting so we
        # don't miss the join-time tracks.
        try:
            room.on('track_subscribed', self._on_track_subscribed)
        except Exception as e:
            logger.debug(
                'livekit_transcript_subscriber: on() unavailable: %s',
                e)
        try:
            await room.connect(self.livekit_url, self.token)
        except Exception as e:
            logger.warning(
                'livekit_transcript_subscriber: room connect '
                'failed (call=%s): %s', self.call_id, e)
            return
        # Wait until stop() is signaled.
        import asyncio as _asyncio
        while not self._stop_evt.is_set():
            await _asyncio.sleep(0.25)

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

    def _make_room(self) -> Any:
        if self._room_factory is not None:
            try:
                return self._room_factory()
            except Exception as e:
                logger.warning(
                    'livekit_transcript_subscriber: room_factory '
                    'failed: %s', e)
                return None
        if not HAS_LIVEKIT_RTC:
            return None
        try:
            return livekit_rtc.Room()  # type: ignore[union-attr]
        except Exception as e:
            logger.warning(
                'livekit_transcript_subscriber: livekit_rtc.Room() '
                'failed: %s', e)
            return None

    def _on_track_subscribed(self, track, publication, participant) -> None:
        """LiveKit fires this synchronously on track-subscribe.  We
        spawn a per-track frame consumer on the loop."""
        # Only audio tracks are interesting.
        kind = getattr(track, 'kind', None)
        try:
            audio_kind = livekit_rtc.TrackKind.KIND_AUDIO  # type: ignore
        except Exception:
            audio_kind = None
        if audio_kind is not None and kind != audio_kind:
            return
        identity = (getattr(participant, 'identity', None)
                    or getattr(participant, 'sid', None) or 'unknown')
        try:
            import asyncio as _asyncio
            _asyncio.create_task(self._consume_track(track, str(identity)))
        except Exception as e:
            logger.debug(
                'livekit_transcript_subscriber: cannot spawn consumer '
                '(%s): %s', identity, e)

    async def _consume_track(self, track, participant_identity: str) -> None:
        """Iterate over audio frames from a LiveKit RemoteAudioTrack.

        livekit-rtc 0.x exposes ``AudioStream(track)`` whose async
        iteration yields ``AudioFrameEvent`` with a ``.frame`` carrying
        ``data`` (PCM bytes), ``sample_rate``, ``num_channels``.  Older
        signatures vary; we read defensively.
        """
        try:
            stream = livekit_rtc.AudioStream(track)  # type: ignore
        except Exception as e:
            logger.debug(
                'livekit_transcript_subscriber: AudioStream init '
                'failed: %s', e)
            return
        try:
            async for evt in stream:
                if self._stop_evt.is_set():
                    return
                frame = getattr(evt, 'frame', None) or evt
                pcm = getattr(frame, 'data', None) or getattr(
                    frame, 'pcm', None)
                rate = (getattr(frame, 'sample_rate', None)
                        or getattr(frame, 'rate', None) or 48000)
                channels = (getattr(frame, 'num_channels', None)
                            or getattr(frame, 'channels', None) or 1)
                if pcm is None:
                    continue
                # ``pcm`` may be bytes OR a memoryview / np buffer; force
                # to bytes for the WS hand-off.
                if not isinstance(pcm, (bytes, bytearray)):
                    try:
                        pcm = bytes(pcm)
                    except Exception:
                        continue
                self.push_frame(
                    participant_identity, bytes(pcm),
                    src_rate=int(rate), src_channels=int(channels))
        except Exception as e:
            logger.debug(
                'livekit_transcript_subscriber: track stream ended '
                '(%s): %s', participant_identity, e)

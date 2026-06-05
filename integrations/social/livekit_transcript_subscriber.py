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

Lib gate + threading:
    Shares the daemon-thread / asyncio-loop / connect / teardown / resample
    machinery with the publish half via ``_LiveKitRoomThread``
    (``_livekit_room.py``).  ``HAS_LIVEKIT_RTC`` is False when ``livekit`` isn't
    installed and ``start()`` becomes a no-op, so today's bridge-worker
    behaviour is preserved.  Only the subscribe-specific bits live here: the
    per-participant WS forwarders, the track-subscribe handler, and the
    frame-consumer.

Resampling:
    LiveKit publishes 48kHz s16le mono per audio frame (default RTC
    config).  Some senders may publish at other rates — ``_resample_to_16k_mono``
    converts to 16kHz mono (via the shared ``resample_pcm16``) for the STT WS.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from integrations.social._livekit_room import (
    _LiveKitRoomThread,
    HAS_LIVEKIT_RTC,
    livekit_rtc,
    resample_pcm16,
)

logger = logging.getLogger(__name__)


# Target STT-server format.
_TARGET_RATE = 16000
_TARGET_CHANNELS = 1


def _resample_to_16k_mono(pcm: bytes, src_rate: int,
                          src_channels: int) -> bytes:
    """Convert arbitrary PCM16 to 16kHz mono for the STT WS — the shared
    ``resample_pcm16`` with this module's fixed target.  Kept as a named
    wrapper because callers + tests reference it directly."""
    return resample_pcm16(pcm, src_rate, src_channels,
                          _TARGET_RATE, _TARGET_CHANNELS)


class LiveKitTranscriptSubscriber(_LiveKitRoomThread):
    """One subscriber per (call_id, livekit_room).  Connects, listens,
    pipes PCM through the local STT WS, tears down on ``stop()``.

    Constructor seams allow tests to inject:
      - ``room_factory``    : (base) callable returning a Room-like object.
      - ``ws_connect``      : sync WS connect callable; defaults to
                              ``websockets.sync.client.connect``.
      - ``stt_port_provider``: returns the local STT WS port; defaults
                              to ``whisper_tool.get_stt_stream_port``.
    """

    _THREAD_PREFIX = 'livekit-transcript'

    def __init__(self, call_id: str, livekit_url: str, token: str,
                 room_factory: Optional[Callable[[], Any]] = None,
                 ws_connect: Optional[Callable[..., Any]] = None,
                 stt_port_provider: Optional[Callable[[], Optional[int]]] = None):
        super().__init__(call_id, livekit_url, token, room_factory)
        self._ws_connect = ws_connect
        self._stt_port_provider = stt_port_provider
        # Per-participant WS clients keyed by participant identity.
        self._ws_per_participant: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ── base hook: close per-participant WS clients on teardown ──

    def _on_stop(self) -> None:
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

    # ── asyncio-side LiveKit subscription (override) ────────────

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
                'livekit_transcript_subscriber: on() unavailable: %s', e)
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


__all__ = ['LiveKitTranscriptSubscriber', 'HAS_LIVEKIT_RTC',
           '_resample_to_16k_mono']

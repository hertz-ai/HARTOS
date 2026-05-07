"""
discord_voice_recv_sink — UNIF-G7 / W1.7 Producer A.

Optional Discord voice-receive bridge.  When the
``discord-ext-voice-recv`` community library is installed,
``DiscordAdapter.join_room`` attaches a ``HevolveStreamingSink`` to
the connected ``VoiceClient`` so per-speaker PCM frames are piped
through the canonical streaming-STT WebSocket and final segments
land in ``whisper_tool.enqueue_stt_segment`` via Producer C's
``?call_id=`` hook.

Single canonical sink — every audio source (Discord voice, LiveKit
RTC, RN mic, future others) lands segments in the SAME per-call
queue, drained by ``agent_voice_bridge._tick``.  No parallel paths.

Lib gate:
    The module is importable even when ``discord-ext-voice-recv``
    is NOT installed.  ``HAS_VOICE_RECV`` is False in that case
    and ``maybe_attach_recv_sink`` becomes a no-op so today's
    voice-room presence-only behavior is preserved.

Threading model:
    discord-ext-voice-recv calls ``AudioSink.write`` from its own
    audio receiver thread (NOT the asyncio event loop).  We open a
    sync ``websockets`` client per speaker on first frame, push the
    resampled PCM, and rely on Producer C's server-side ``?call_id=``
    hook to enqueue the final transcript.  The WS response stream
    is drained in a daemon thread so back-pressure never blocks the
    audio-write path.

Resampling:
    Discord delivers 48kHz s16le stereo per voice packet.  The STT
    server expects 16kHz s16le mono.  We use ``audioop.tomono`` +
    ``audioop.ratecv`` (Python stdlib) — no new dependency.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# Optional dependency — the entire feature is gated on this.
try:
    from discord_ext_voice_recv import (  # type: ignore
        AudioSink, VoiceRecvClient,
    )
    HAS_VOICE_RECV = True
except Exception:
    AudioSink = object  # type: ignore
    VoiceRecvClient = None  # type: ignore
    HAS_VOICE_RECV = False


# Discord audio frame format.
_DISCORD_RATE = 48000
_DISCORD_CHANNELS = 2
_TARGET_RATE = 16000
_TARGET_CHANNELS = 1
_SAMPLE_WIDTH = 2  # s16le


def _resample_48k_stereo_to_16k_mono(pcm: bytes) -> bytes:
    """Discord 48kHz stereo s16le → STT-server-expected 16kHz mono s16le.

    Uses stdlib ``audioop`` so no new dependency.  Returns ``b''`` on
    any conversion error (sink will skip that packet — caller never
    sees an exception).
    """
    if not pcm:
        return b''
    try:
        import audioop
        mono = audioop.tomono(pcm, _SAMPLE_WIDTH, 1.0, 1.0)
        downsampled, _ = audioop.ratecv(
            mono, _SAMPLE_WIDTH, _TARGET_CHANNELS,
            _DISCORD_RATE, _TARGET_RATE, None)
        return downsampled
    except Exception as e:
        logger.debug('discord_voice_recv_sink: resample failed: %s', e)
        return b''


class HevolveStreamingSink(AudioSink):
    """One per Discord voice channel join.  Forwards PCM frames per
    speaker to the canonical streaming-STT WS server.

    See module docstring for design notes.
    """

    def __init__(self, call_id: str, bot_user_id: Optional[int] = None,
                 ws_connect: Optional[Callable[..., Any]] = None,
                 stt_port_provider: Optional[Callable[[], Optional[int]]] = None):
        # ``ws_connect`` and ``stt_port_provider`` are dependency-
        # injection seams for tests — production passes None and we
        # resolve them lazily.
        if HAS_VOICE_RECV:
            try:
                super().__init__()
            except Exception:
                pass
        self.call_id = str(call_id) if call_id is not None else ''
        self.bot_user_id = bot_user_id
        self._ws_connect = ws_connect
        self._stt_port_provider = stt_port_provider
        # Per-speaker WS client + the daemon-thread that drains it.
        self._ws_per_user: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._closed = False

    # ── discord-ext-voice-recv contract ──────────────────────────

    def wants_opus(self) -> bool:
        """Receive raw PCM, not Opus packets."""
        return False

    def write(self, user, data) -> None:
        """Called per voice packet, on the recv lib's audio thread."""
        if self._closed or not self.call_id:
            return
        if user is None or data is None:
            return
        user_id = getattr(user, 'id', None)
        if user_id is None:
            return
        if self.bot_user_id is not None and user_id == self.bot_user_id:
            return  # don't echo bot back
        pcm = getattr(data, 'pcm', None) or getattr(data, 'data', None)
        if not pcm:
            return
        resampled = _resample_48k_stereo_to_16k_mono(pcm)
        if not resampled:
            return
        ws = self._get_ws(str(user_id))
        if ws is None:
            return
        try:
            ws.send(resampled)
        except Exception as e:
            logger.debug(
                'discord_voice_recv_sink: send failed for user=%s: %s',
                user_id, e)
            self._reset_ws(str(user_id))

    def cleanup(self) -> None:
        """Called by the recv lib on disconnect.  Closes all per-speaker
        WS clients."""
        with self._lock:
            self._closed = True
            wss = list(self._ws_per_user.values())
            self._ws_per_user.clear()
        for ws in wss:
            try:
                ws.close()
            except Exception:
                pass

    # ── Internals ────────────────────────────────────────────────

    def _get_ws(self, speaker_id: str) -> Any:
        with self._lock:
            existing = self._ws_per_user.get(speaker_id)
            if existing is not None:
                return existing
            if self._closed:
                return None
        # Resolve port + connector lazily — not under the lock.
        port = self._resolve_stt_port()
        if not port:
            return None
        connect = self._resolve_ws_connect()
        if connect is None:
            return None
        url = (f'ws://127.0.0.1:{port}/?call_id={self.call_id}'
               f'&user_id={speaker_id}')
        try:
            ws = connect(url, max_size=2 * 1024 * 1024)
        except Exception as e:
            logger.warning(
                'discord_voice_recv_sink: WS connect failed (%s): %s',
                url, e)
            return None
        with self._lock:
            if self._closed:
                # Race — sink closed between check and connect.
                try:
                    ws.close()
                except Exception:
                    pass
                return None
            self._ws_per_user[speaker_id] = ws
        # Drain inbound responses so back-pressure never blocks send().
        # Producer C does the enqueue server-side; we discard the
        # client-side echo.
        threading.Thread(
            target=self._drain_loop, args=(ws,), daemon=True,
            name=f'hevolve-discord-recv-drain-{speaker_id[:8]}',
        ).start()
        return ws

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
        except Exception as e:
            logger.debug(
                'discord_voice_recv_sink: cannot resolve STT port: %s', e)
            return None

    def _resolve_ws_connect(self) -> Optional[Callable[..., Any]]:
        if self._ws_connect is not None:
            return self._ws_connect
        try:
            from websockets.sync.client import connect  # type: ignore
            return connect
        except Exception as e:
            logger.debug(
                'discord_voice_recv_sink: websockets.sync.client '
                'unavailable: %s', e)
            return None

    def _drain_loop(self, ws) -> None:
        try:
            for _msg in ws:
                if self._closed:
                    return
        except Exception:
            return

    def _reset_ws(self, speaker_id: str) -> None:
        with self._lock:
            ws = self._ws_per_user.pop(speaker_id, None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def maybe_attach_recv_sink(voice_client, call_id: str,
                           bot_user_id: Optional[int] = None) -> bool:
    """Attach a ``HevolveStreamingSink`` to a connected ``VoiceClient``
    if (a) the recv lib is installed AND (b) the voice_client supports
    listening (i.e. it's actually a ``VoiceRecvClient``).

    Returns True iff a sink was attached.  False is the no-op fallback
    that preserves today's "presence only" voice-room behavior.

    Best-effort: any failure logs at debug + returns False so the
    caller can continue.
    """
    if not HAS_VOICE_RECV or voice_client is None:
        return False
    listen = getattr(voice_client, 'listen', None)
    if not callable(listen):
        return False
    try:
        sink = HevolveStreamingSink(
            call_id=str(call_id), bot_user_id=bot_user_id)
        listen(sink)
        logger.info(
            'discord_voice_recv_sink: attached for call_id=%s '
            '(bot_user_id=%s)', call_id, bot_user_id)
        return True
    except Exception as e:
        logger.warning(
            'discord_voice_recv_sink: maybe_attach_recv_sink failed: %s',
            e)
        return False

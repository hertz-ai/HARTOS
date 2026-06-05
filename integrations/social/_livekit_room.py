"""Shared LiveKit room-thread machinery for the two halves of an agent's
presence in a call: ``livekit_transcript_subscriber`` (ears: room → STT) and
``livekit_audio_publisher`` (mouth: TTS → room).

Both are "one daemon thread running an asyncio loop connected to a LiveKit room",
so that lifecycle lives ONCE here as ``_LiveKitRoomThread`` (start / run / stop /
make-room / disconnect + the lib-gate + the test ``room_factory`` seam), plus the
shared PCM16 resampler.  Subclasses implement only their role-specific
``_async_main`` (subscribe vs publish) and the small ``_on_loop_start`` /
``_on_stop`` hooks.  This is the extracted base for the duplication that the
publisher first copied from the subscriber — keep new room roles as subclasses,
never as a third paste.
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


_SAMPLE_WIDTH = 2  # s16le


def resample_pcm16(pcm: bytes, src_rate: int, src_channels: int,
                   dst_rate: int, dst_channels: int) -> bytes:
    """Convert PCM16 to (dst_rate, dst_channels) via stdlib ``audioop`` — no new
    dep.  Returns ``b''`` on any conversion error.  Pure; the single resampler
    both the 16k-mono STT path and the 48k publish path call."""
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
        logger.debug('resample_pcm16 failed: %s', e)
        return b''


class _LiveKitRoomThread:
    """One daemon thread + asyncio loop connected to a LiveKit room.

    Lib-gated: ``start()`` is a no-op (returns False) when the realtime SDK
    isn't installed and no test ``room_factory`` is injected, so flat / bundled
    deploys keep today's behaviour.  Subclasses MUST implement ``_async_main``;
    they MAY override ``_on_loop_start`` (e.g. create an ``asyncio.Queue`` on the
    loop thread) and ``_on_stop`` (e.g. close sockets / push a queue sentinel).
    """

    # Overridden per subclass so thread names stay legible in py-spy dumps.
    _THREAD_PREFIX = 'livekit-room'

    def __init__(self, call_id: str, livekit_url: str, token: str,
                 room_factory: Optional[Callable[[], Any]] = None):
        self.call_id = str(call_id) if call_id is not None else ''
        self.livekit_url = livekit_url
        self.token = token
        self._room_factory = room_factory
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._loop: Any = None   # asyncio loop, set in the thread
        self._room: Any = None

    # ── Public lifecycle (shared) ───────────────────────────────

    def start(self) -> bool:
        """Spawn the daemon thread + asyncio loop.  No-op without the SDK (and
        no injected room_factory).  Returns True iff a thread was started."""
        if not HAS_LIVEKIT_RTC and self._room_factory is None:
            logger.info('%s: livekit (rtc) not installed — start() no-op '
                        '(call_id=%s)', self._THREAD_PREFIX, self.call_id)
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.call_id or not self.livekit_url or not self.token:
            logger.warning('%s: missing required fields (call_id=%r url=%r '
                           'token=%r)', self._THREAD_PREFIX, self.call_id,
                           self.livekit_url, bool(self.token))
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f'{self._THREAD_PREFIX}-{self.call_id[:12]}')
        self._thread.start()
        return True

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stop(self) -> None:
        """Signal teardown, disconnect the room on the loop, then run any
        subclass teardown.  Idempotent + safe before start()."""
        self._stop_evt.set()
        loop = self._loop
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
        try:
            self._on_stop()
        except Exception:
            pass

    # ── asyncio-side scaffolding (shared) ───────────────────────

    def _run(self) -> None:
        try:
            import asyncio
        except Exception:
            return
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._on_loop_start()
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.warning('%s: loop crashed: %s', self._THREAD_PREFIX, e)
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
                logger.warning('%s: room_factory failed: %s',
                               self._THREAD_PREFIX, e)
                return None
        if not HAS_LIVEKIT_RTC:
            return None
        try:
            return livekit_rtc.Room()  # type: ignore[union-attr]
        except Exception as e:
            logger.warning('%s: Room() failed: %s', self._THREAD_PREFIX, e)
            return None

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

    # ── Subclass hooks ──────────────────────────────────────────

    def _on_loop_start(self) -> None:
        """Runs on the loop thread before ``_async_main`` (default no-op)."""

    def _on_stop(self) -> None:
        """Subclass teardown after disconnect (default no-op)."""

    async def _async_main(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


__all__ = ['_LiveKitRoomThread', 'HAS_LIVEKIT_RTC', 'livekit_rtc',
           'resample_pcm16']

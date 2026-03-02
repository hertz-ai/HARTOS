"""
Window Session Manager — Multi-window streaming sessions for tab-detach.

Each remote application window can be streamed as an independent session.
The viewer sees separate "tabs" for each window, and can detach them
into standalone viewers.

Reuses:
  - window_capture.py: WindowEnumerator, WindowCapture, WindowInfo
  - session_manager.py: SessionManager for auth/lifecycle
  - transport.py: TransportChannel for frame delivery
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


@dataclass
class WindowSession:
    """One streaming session bound to one OS window."""
    session_id: str
    window_hwnd: int
    window_title: str
    process_name: str
    started_at: float
    viewer_count: int = 0
    capture_thread: Optional[threading.Thread] = field(default=None, repr=False)
    running: bool = False

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'window_hwnd': self.window_hwnd,
            'window_title': self.window_title,
            'process_name': self.process_name,
            'started_at': self.started_at,
            'viewer_count': self.viewer_count,
            'running': self.running,
        }


class WindowSessionManager:
    """Manages multiple concurrent window capture sessions.

    Integrates with existing SessionManager for auth/lifecycle but adds
    window-level granularity. Each window session has its own capture thread
    and transport channel.
    """

    def __init__(self):
        self._sessions: Dict[str, WindowSession] = {}
        self._captures: Dict[str, object] = {}  # session_id → WindowCapture
        self._transports: Dict[str, object] = {}  # session_id → TransportChannel
        self._lock = threading.Lock()
        self._frame_callbacks: Dict[str, Callable] = {}

    def list_available_windows(self) -> List[dict]:
        """Enumerate host windows available for streaming.

        Returns list of WindowInfo dicts (excludes system/shell windows).
        """
        try:
            from integrations.remote_desktop.window_capture import WindowEnumerator
            enum = WindowEnumerator()
            windows = enum.list_windows(include_minimized=False)
            # Filter out trivial system windows
            filtered = []
            for w in windows:
                # Skip windows with very small dimensions
                _, _, width, height = w.rect
                if width < 50 or height < 50:
                    continue
                filtered.append(w.to_dict())
            return filtered
        except Exception as e:
            logger.warning(f"Window enumeration failed: {e}")
            return []

    def start_window_session(self, window_hwnd: int,
                              window_title: str = '',
                              process_name: str = '',
                              transport=None,
                              user_id: Optional[str] = None) -> dict:
        """Start streaming a specific window.

        Args:
            window_hwnd: OS window handle to capture.
            window_title: Window title (for display).
            process_name: Process name (for display).
            transport: TransportChannel to send frames on.
            user_id: User starting the session.

        Returns:
            {session_id, status, window_title, ...}
        """
        session_id = f"win-{uuid.uuid4().hex[:12]}"

        # Create WindowCapture
        try:
            from integrations.remote_desktop.window_capture import (
                WindowInfo, WindowCapture, WindowCaptureConfig,
            )
            winfo = WindowInfo(
                hwnd=window_hwnd,
                title=window_title,
                process_name=process_name,
                pid=0,
                rect=(0, 0, 0, 0),
            )
            # Try to get real rect via refresh
            from integrations.remote_desktop.window_capture import WindowEnumerator
            enum = WindowEnumerator()
            refreshed = enum.refresh_window_info(winfo)
            if refreshed:
                winfo = refreshed

            capture = WindowCapture(winfo, WindowCaptureConfig())
        except Exception as e:
            return {'status': 'error', 'error': f'Window capture init failed: {e}'}

        ws = WindowSession(
            session_id=session_id,
            window_hwnd=window_hwnd,
            window_title=winfo.title or window_title,
            process_name=winfo.process_name or process_name,
            started_at=time.time(),
            running=True,
        )

        with self._lock:
            self._sessions[session_id] = ws
            self._captures[session_id] = capture

        if transport:
            self._transports[session_id] = transport

        # Start capture thread
        thread = threading.Thread(
            target=self._capture_loop,
            args=(session_id,),
            daemon=True,
            name=f'wincap-{session_id[:8]}',
        )
        ws.capture_thread = thread
        thread.start()

        logger.info(f"Window session started: {ws.window_title} "
                    f"({session_id[:8]})")

        return {
            'status': 'streaming',
            'session_id': session_id,
            'window_title': ws.window_title,
            'process_name': ws.process_name,
            'window_hwnd': window_hwnd,
        }

    def stop_window_session(self, session_id: str) -> bool:
        """Stop a window streaming session."""
        with self._lock:
            ws = self._sessions.get(session_id)
            if not ws:
                return False
            ws.running = False

        # Wait for capture thread to finish
        if ws.capture_thread:
            ws.capture_thread.join(timeout=5)

        # Cleanup
        capture = self._captures.pop(session_id, None)
        if capture:
            capture.stop()
        self._transports.pop(session_id, None)
        self._frame_callbacks.pop(session_id, None)

        with self._lock:
            self._sessions.pop(session_id, None)

        logger.info(f"Window session stopped: {session_id[:8]}")
        return True

    def get_active_window_sessions(self) -> List[dict]:
        """Get all active window sessions as dicts."""
        with self._lock:
            return [ws.to_dict() for ws in self._sessions.values()
                    if ws.running]

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get a specific session."""
        with self._lock:
            ws = self._sessions.get(session_id)
            return ws.to_dict() if ws else None

    def on_frame(self, session_id: str,
                 callback: Callable[[str, bytes], None]) -> None:
        """Register a callback for frames from a window session.

        callback(session_id, jpeg_bytes)
        """
        self._frame_callbacks[session_id] = callback

    def detach_window(self, session_id: str) -> dict:
        """'Tab detach' — return connection info for a standalone viewer.

        The window session continues running; this just provides the info
        needed for a separate viewer instance to connect to it.
        """
        with self._lock:
            ws = self._sessions.get(session_id)
            if not ws:
                return {'status': 'error', 'error': 'Session not found'}
            if not ws.running:
                return {'status': 'error', 'error': 'Session not running'}

        return {
            'status': 'detached',
            'session_id': session_id,
            'window_title': ws.window_title,
            'process_name': ws.process_name,
            'window_hwnd': ws.window_hwnd,
        }

    def stop_all(self) -> int:
        """Stop all window sessions. Returns count stopped."""
        with self._lock:
            session_ids = list(self._sessions.keys())
        count = 0
        for sid in session_ids:
            if self.stop_window_session(sid):
                count += 1
        return count

    def _capture_loop(self, session_id: str) -> None:
        """Capture loop for a window session — sends frames via transport/callback."""
        capture = self._captures.get(session_id)
        if not capture:
            return

        ws = self._sessions.get(session_id)
        if not ws:
            return

        logger.debug(f"Window capture loop started: {session_id[:8]}")
        for frame in capture.capture_loop():
            if not ws.running:
                break

            # Send via transport if available
            transport = self._transports.get(session_id)
            if transport and hasattr(transport, 'send_frame'):
                try:
                    transport.send_frame(frame)
                except Exception:
                    pass

            # Notify callback if registered
            callback = self._frame_callbacks.get(session_id)
            if callback:
                try:
                    callback(session_id, frame)
                except Exception:
                    pass

        logger.debug(f"Window capture loop ended: {session_id[:8]}")


# ── Singleton ────────────────────────────────────────────────

_window_session_manager: Optional[WindowSessionManager] = None


def get_window_session_manager() -> WindowSessionManager:
    """Get or create the singleton WindowSessionManager."""
    global _window_session_manager
    if _window_session_manager is None:
        _window_session_manager = WindowSessionManager()
    return _window_session_manager

"""
Host Service — Native screen sharing host for HARTOS Remote Desktop.

Only used when engine_selector picks NATIVE (no RustDesk/Sunshine installed).
RustDesk/Sunshine handle their own hosting — this is the pure-Python fallback.

Captures screen via frame_capture.py, streams over transport.py,
receives input events via input_handler.py. Supports multi-viewer.

Reuses:
  - frame_capture.py: FrameCapture (mss/dxcam/pyautogui circuit breaker)
  - input_handler.py: InputHandler (pynput/pyautogui with security gating)
  - transport.py: DirectWebSocketTransport for LAN, WAMP for WAN
  - session_manager.py: Session lifecycle
  - security/node_watchdog.py: Heartbeat monitoring
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional  # noqa: F401

logger = logging.getLogger('hevolve.remote_desktop')


class HostService:
    """Native host: captures screen, streams frames, receives input.

    Lifecycle:
      start() → capture_loop sends frames → handle_viewer() dispatches input → stop()
    """

    def __init__(self):
        self._running = False
        self._capture = None
        self._input_handler = None
        self._transport = None
        self._capture_thread: Optional[threading.Thread] = None
        self._viewers: Dict[str, dict] = {}  # session_id → viewer info
        self._lock = threading.Lock()
        self._device_id: Optional[str] = None
        self._password: Optional[str] = None
        self._config: dict = {}

    def start(self, config: Optional[dict] = None) -> dict:
        """Start hosting — begin screen capture and transport server.

        Args:
            config: Optional FrameConfig overrides (max_fps, quality, etc.)

        Returns:
            {device_id, password, lan_port, status}
        """
        if self._running:
            return {'status': 'already_running', 'device_id': self._device_id}

        self._config = config or {}

        # Get device ID
        try:
            from integrations.remote_desktop.device_id import get_device_id, format_device_id
            self._device_id = get_device_id()
            formatted_id = format_device_id(self._device_id)
        except Exception as e:
            return {'status': 'error', 'error': f'Device ID unavailable: {e}'}

        # Generate password
        try:
            from integrations.remote_desktop.session_manager import get_session_manager
            sm = get_session_manager()
            self._password = sm.generate_otp(self._device_id)
        except Exception as e:
            return {'status': 'error', 'error': f'Password generation failed: {e}'}

        # Initialize frame capture
        try:
            from integrations.remote_desktop.frame_capture import FrameCapture, FrameConfig
            fc = FrameConfig(
                max_fps=self._config.get('max_fps', 30),
                quality=self._config.get('quality', 80),
            )
            self._capture = FrameCapture(config=fc)
        except Exception as e:
            return {'status': 'error', 'error': f'Frame capture init failed: {e}'}

        # Initialize input handler
        try:
            from integrations.remote_desktop.input_handler import InputHandler
            allow_control = self._config.get('allow_control', True)
            self._input_handler = InputHandler(allow_control=allow_control)
        except Exception as e:
            logger.warning(f"Input handler unavailable: {e}")

        # Start transport server
        lan_port = 0
        try:
            from integrations.remote_desktop.transport import DirectWebSocketTransport
            self._transport = DirectWebSocketTransport(host_mode=True)
            lan_port = self._transport.start_server()

            # Register event handler for input
            self._transport.on_event(self._on_viewer_event)
        except Exception as e:
            logger.warning(f"WebSocket transport unavailable: {e}")

        # Register with NodeWatchdog
        self._register_watchdog()

        # Start capture loop
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name='host-capture-loop',
        )
        self._capture_thread.start()

        logger.info(f"Native host started: {formatted_id} on port {lan_port}")
        return {
            'status': 'hosting',
            'device_id': self._device_id,
            'formatted_id': formatted_id,
            'password': self._password,
            'lan_port': lan_port,
        }

    def stop(self) -> None:
        """Stop hosting — disconnect all viewers, stop capture."""
        self._running = False

        if self._capture_thread:
            self._capture_thread.join(timeout=5)
            self._capture_thread = None

        if self._capture:
            self._capture.stop()
            self._capture = None

        if self._transport:
            self._transport.close()
            self._transport = None

        self._input_handler = None
        self._viewers.clear()
        logger.info("Native host stopped")

    def handle_viewer(self, session_id: str, viewer_device_id: str) -> None:
        """Register a new viewer for this host session."""
        with self._lock:
            self._viewers[session_id] = {
                'viewer_device_id': viewer_device_id,
                'connected_at': time.time(),
            }
        logger.info(f"Viewer connected: {viewer_device_id[:12]} "
                    f"(session {session_id[:8]})")

    def remove_viewer(self, session_id: str) -> None:
        """Remove a viewer."""
        with self._lock:
            self._viewers.pop(session_id, None)

    def _capture_loop(self) -> None:
        """Main capture loop — sends frames to all viewers via transport."""
        if not self._capture:
            return

        logger.debug("Capture loop started")
        for frame in self._capture.capture_loop():
            if not self._running:
                break

            # Send frame to all connected viewers via transport
            if self._transport and self._transport.connected:
                self._transport.send_frame(frame)

        logger.debug("Capture loop ended")

    def _on_viewer_event(self, event: dict) -> None:
        """Handle incoming event from viewer (input, clipboard, file, control)."""
        event_type = event.get('type', '')

        if event_type in ('click', 'rightclick', 'doubleclick', 'middleclick',
                          'move', 'mouse_move', 'drag', 'scroll',
                          'mouse_down', 'mouse_up', 'key', 'type', 'hotkey'):
            # Input event → dispatch to InputHandler
            if self._input_handler:
                result = self._input_handler.handle_input_event(event)
                if not result.get('success'):
                    logger.debug(f"Input event rejected: {result.get('error')}")

        elif event_type == 'clipboard':
            # Remote clipboard → apply locally
            content = event.get('content', '')
            if content:
                try:
                    from integrations.remote_desktop.clipboard_sync import ClipboardSync
                    sync = ClipboardSync()
                    sync.apply_remote_clipboard(content)
                except Exception:
                    pass

        elif event_type == 'file_ctrl':
            # File transfer control
            try:
                from integrations.remote_desktop.file_transfer import FileTransfer
                ft = FileTransfer()
                ft.handle_event(event)
            except Exception:
                pass

        elif event_type == 'drag_drop':
            # Cross-device drag-and-drop
            try:
                from integrations.remote_desktop.drag_drop import DragDropBridge
                bridge = DragDropBridge(
                    transport=self._transport,
                    input_handler=self._input_handler,
                )
                bridge.handle_file_drop_on_host(event)
            except Exception as e:
                logger.debug(f"Drag-drop event handling failed: {e}")

        elif event_type == 'file_drop':
            # File drop simulation at cursor position
            if self._input_handler:
                self._input_handler.handle_input_event(event)

        elif event_type == 'list_windows':
            # Window enumeration request → return available windows
            windows = self.get_available_windows()
            if self._transport and self._transport.connected:
                self._transport.send_event({
                    'type': 'window_list',
                    'windows': windows,
                })

        elif event_type == 'stream_window':
            # Start streaming a specific window
            hwnd = event.get('hwnd', 0)
            title = event.get('title', '')
            result = self.start_window_stream(hwnd, title)
            if self._transport and self._transport.connected:
                self._transport.send_event({
                    'type': 'window_stream_result',
                    'result': result,
                })

        elif event_type == 'stop_window_stream':
            window_session_id = event.get('window_session_id', '')
            self.stop_window_stream(window_session_id)

        elif event_type == 'control':
            action = event.get('action', '')
            if action == 'disconnect':
                session_id = event.get('session_id', '')
                self.remove_viewer(session_id)

    def get_available_windows(self) -> List[dict]:
        """Return windows available for per-window streaming."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().list_available_windows()
        except Exception as e:
            logger.debug(f"Window enumeration failed: {e}")
            return []

    def start_window_stream(self, hwnd: int, title: str = '') -> dict:
        """Start streaming a specific window."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            wsm = get_window_session_manager()
            return wsm.start_window_session(
                window_hwnd=hwnd,
                window_title=title,
                transport=self._transport,
            )
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def stop_window_stream(self, window_session_id: str) -> bool:
        """Stop a window stream."""
        try:
            from integrations.remote_desktop.window_session import (
                get_window_session_manager,
            )
            return get_window_session_manager().stop_window_session(
                window_session_id)
        except Exception:
            return False

    def _register_watchdog(self) -> None:
        """Register with NodeWatchdog for auto-restart."""
        try:
            from security.node_watchdog import get_watchdog
            watchdog = get_watchdog()
            if watchdog:
                watchdog.register(
                    'remote_desktop_host',
                    expected_interval=30,
                    restart_fn=lambda: self.start(self._config),
                    stop_fn=self.stop,
                )
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict:
        """Get host service status."""
        with self._lock:
            viewer_count = len(self._viewers)
        return {
            'running': self._running,
            'device_id': self._device_id,
            'password': self._password,
            'viewers': viewer_count,
            'transport_connected': self._transport.connected if self._transport else False,
            'capture_stats': self._capture.get_stats() if self._capture else None,
        }


# ── Singleton ────────────────────────────────────────────────

_host_service: Optional[HostService] = None


def get_host_service() -> HostService:
    """Get or create the singleton HostService."""
    global _host_service
    if _host_service is None:
        _host_service = HostService()
    return _host_service

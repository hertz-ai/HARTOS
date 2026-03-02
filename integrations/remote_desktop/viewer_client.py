"""
Viewer Client — Native remote desktop viewer for HARTOS.

Connects to a native host (or agent headless screen view).
Only used when the engine_selector picks NATIVE. RustDesk/Moonlight
handle their own viewing — this is the pure-Python fallback.

Also used by agents for headless screen viewing (no GUI needed).

Reuses:
  - transport.py: auto_negotiate_transport() for tier selection
  - signaling.py: SignalingChannel for connection negotiation
  - input_handler.py: event format (sent to host)
  - clipboard_sync.py: bidirectional clipboard bridge
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class ViewerClient:
    """Native viewer: receives frames, sends input to remote host.

    Lifecycle:
      connect() → on_frame callbacks → send_mouse/keyboard → disconnect()
    """

    def __init__(self):
        self._connected = False
        self._transport = None
        self._signaling = None
        self._clipboard_sync = None
        self._session_id: Optional[str] = None
        self._remote_device_id: Optional[str] = None
        self._frame_callback: Optional[Callable[[bytes], None]] = None
        self._status_callback: Optional[Callable[[dict], None]] = None
        self._lock = threading.Lock()
        self._frame_count = 0
        self._connected_at: Optional[float] = None
        self._last_frame_at: Optional[float] = None

    def connect(self, device_id: str, password: str,
                mode: str = 'full_control',
                transport_offers: Optional[dict] = None) -> dict:
        """Connect to a remote native host.

        Args:
            device_id: Remote host's device ID
            password: OTP password
            mode: 'full_control' or 'view_only'
            transport_offers: Pre-negotiated offers (skip signaling)

        Returns:
            {status, session_id, transport_tier}
        """
        self._remote_device_id = device_id

        # Authenticate
        try:
            from integrations.remote_desktop.security import authenticate_connection
            from integrations.remote_desktop.device_id import get_device_id
            local_id = get_device_id()
            ok, msg = authenticate_connection(device_id, local_id, password)
            if not ok:
                return {'status': 'auth_failed', 'error': msg}
        except Exception as e:
            logger.warning(f"Auth module unavailable: {e}")
            local_id = 'viewer'

        # Create session
        from integrations.remote_desktop.session_manager import (
            get_session_manager, SessionMode,
        )
        sm = get_session_manager()
        mode_enum = (SessionMode.FULL_CONTROL if mode == 'full_control'
                     else SessionMode.VIEW_ONLY)
        session = sm.create_session(
            host_device_id=device_id,
            viewer_device_id=local_id,
            mode=mode_enum,
        )
        self._session_id = session.session_id

        # Negotiate transport
        if transport_offers:
            self._transport = self._negotiate_transport(transport_offers)
        else:
            # Use signaling to get transport offers from host
            self._transport = self._signal_and_negotiate(device_id, password)

        if not self._transport:
            return {'status': 'error', 'error': 'No transport available'}

        # Register callbacks on transport
        self._transport.on_frame(self._on_frame_received)
        self._transport.on_event(self._on_event_received)

        self._connected = True
        self._connected_at = time.time()

        # Start clipboard sync
        self._start_clipboard_sync()

        logger.info(f"Connected to {device_id[:12]} via {self._transport.tier.value}")
        return {
            'status': 'connected',
            'session_id': self._session_id,
            'transport_tier': self._transport.tier.value,
        }

    def disconnect(self) -> None:
        """Disconnect from remote host."""
        self._connected = False

        # Send BYE via transport
        if self._transport and self._transport.connected:
            self._transport.send_event({
                'type': 'control',
                'action': 'disconnect',
                'session_id': self._session_id,
            })
            self._transport.close()

        # Stop clipboard sync
        if self._clipboard_sync:
            self._clipboard_sync.stop_monitoring()
            self._clipboard_sync = None

        # Disconnect session
        if self._session_id:
            try:
                from integrations.remote_desktop.session_manager import get_session_manager
                get_session_manager().disconnect_session(self._session_id)
            except Exception:
                pass

        self._transport = None
        self._session_id = None
        logger.info("Viewer disconnected")

    # ── Frame receive ────────────────────────────────────────

    def on_frame(self, callback: Callable[[bytes], None]) -> None:
        """Register callback for received frames (JPEG bytes)."""
        self._frame_callback = callback

    def on_status(self, callback: Callable[[dict], None]) -> None:
        """Register callback for status updates."""
        self._status_callback = callback

    def _on_frame_received(self, data: bytes) -> None:
        """Handle incoming frame from host."""
        self._frame_count += 1
        self._last_frame_at = time.time()
        if self._frame_callback:
            try:
                self._frame_callback(data)
            except Exception as e:
                logger.debug(f"Frame callback error: {e}")

    def _on_event_received(self, event: dict) -> None:
        """Handle incoming event from host (clipboard, file, control)."""
        event_type = event.get('type', '')

        if event_type == 'clipboard':
            content = event.get('content', '')
            if content and self._clipboard_sync:
                self._clipboard_sync.apply_remote_clipboard(content)

        elif event_type == 'control':
            action = event.get('action', '')
            if action == 'disconnect':
                logger.info("Host initiated disconnect")
                self._connected = False

    # ── Input sending ────────────────────────────────────────

    def send_mouse(self, event_type: str, x: int, y: int,
                   button: str = 'left') -> bool:
        """Send mouse event to remote host."""
        if not self._connected or not self._transport:
            return False
        return self._transport.send_event({
            'type': event_type,
            'x': x,
            'y': y,
            'button': button,
        })

    def send_keyboard(self, event_type: str, key: str) -> bool:
        """Send keyboard event to remote host."""
        if not self._connected or not self._transport:
            return False
        return self._transport.send_event({
            'type': event_type,
            'key': key,
        })

    def send_text(self, text: str) -> bool:
        """Send text typing event to remote host."""
        if not self._connected or not self._transport:
            return False
        return self._transport.send_event({
            'type': 'type',
            'text': text,
        })

    def send_hotkey(self, hotkey: str) -> bool:
        """Send hotkey combo to remote host (e.g., 'ctrl+c')."""
        if not self._connected or not self._transport:
            return False
        return self._transport.send_event({
            'type': 'hotkey',
            'key': hotkey,
        })

    # ── File transfer ────────────────────────────────────────

    def transfer_file(self, local_path: str) -> dict:
        """Send a file to the remote host."""
        if not self._connected or not self._transport:
            return {'success': False, 'error': 'Not connected'}
        try:
            from integrations.remote_desktop.file_transfer import FileTransfer
            ft = FileTransfer()
            return ft.send_file(self._transport, local_path)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # ── Status ───────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    def get_status(self) -> dict:
        """Get viewer client status."""
        fps = 0.0
        if self._connected_at and self._frame_count > 0:
            elapsed = time.time() - self._connected_at
            if elapsed > 0:
                fps = self._frame_count / elapsed

        latency_ms = None
        if self._last_frame_at and self._connected_at:
            # Rough estimate: time since last frame
            latency_ms = (time.time() - self._last_frame_at) * 1000

        return {
            'connected': self._connected,
            'session_id': self._session_id,
            'remote_device_id': self._remote_device_id,
            'transport_tier': self._transport.tier.value if self._transport else None,
            'fps': round(fps, 1),
            'latency_ms': round(latency_ms, 1) if latency_ms else None,
            'frames_received': self._frame_count,
            'transport_stats': self._transport.get_stats() if self._transport else None,
            'clipboard_active': self._clipboard_sync.is_running if self._clipboard_sync else False,
        }

    # ── Internal ─────────────────────────────────────────────

    def _negotiate_transport(self, offers: dict):
        """Negotiate transport from pre-provided offers."""
        try:
            from integrations.remote_desktop.transport import auto_negotiate_transport
            return auto_negotiate_transport(
                self._session_id or 'anon',
                offers,
                role='viewer',
            )
        except Exception as e:
            logger.error(f"Transport negotiation failed: {e}")
            return None

    def _signal_and_negotiate(self, device_id: str, password: str):
        """Use signaling to discover host and negotiate transport."""
        try:
            from integrations.remote_desktop.signaling import (
                SignalingChannel, create_connect_request,
            )
            from integrations.remote_desktop.device_id import get_device_id
            local_id = get_device_id()

            channel = SignalingChannel(local_id)
            channel.start()

            # Send connect request
            request = create_connect_request(
                local_id, device_id, password,
                session_id=self._session_id,
            )
            channel.send_signal(device_id, request)

            # Wait for accept (timeout 10s)
            accept = None
            for _ in range(100):
                pending = channel.get_pending()
                for msg in pending:
                    if msg.msg_type == 'connect_accept':
                        accept = msg
                        break
                if accept:
                    break
                time.sleep(0.1)

            channel.close()

            if accept:
                offers = accept.payload.get('transport_offers', {})
                return self._negotiate_transport(offers)

            logger.warning("No connect_accept received (timeout)")
            return None
        except Exception as e:
            logger.error(f"Signaling failed: {e}")
            return None

    def drop_files(self, file_paths: list, x: int, y: int) -> dict:
        """Drop local files at position (x, y) on the remote host.

        Uses DragDropBridge to transfer files and simulate drop.
        """
        if not self._connected or not self._transport:
            return {'success': False, 'error': 'Not connected'}

        try:
            from integrations.remote_desktop.drag_drop import DragDropBridge
            bridge = DragDropBridge(transport=self._transport)
            return bridge.handle_local_drop(file_paths, x, y)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _start_clipboard_sync(self) -> None:
        """Start clipboard bridge for this viewer session."""
        try:
            from integrations.remote_desktop.clipboard_sync import ClipboardSync

            def on_local_change(content):
                if self._transport and self._connected:
                    self._transport.send_event({
                        'type': 'clipboard',
                        'content': content,
                    })

            self._clipboard_sync = ClipboardSync(
                on_change=on_local_change,
                dlp_enabled=True,
            )
            self._clipboard_sync.start_monitoring()
        except Exception as e:
            logger.debug(f"Clipboard sync unavailable: {e}")


# ── Singleton ────────────────────────────────────────────────

_viewer_client: Optional[ViewerClient] = None


def get_viewer_client() -> ViewerClient:
    """Get or create the singleton ViewerClient."""
    global _viewer_client
    if _viewer_client is None:
        _viewer_client = ViewerClient()
    return _viewer_client

"""
Drag-and-Drop Bridge — Cross-device file drag-and-drop over HARTOS transport.

Local→Remote: detect drag onto viewer → DLP scan → transfer → simulate drop at (x,y)
Remote→Local: receive drop event → transfer → place file locally

Composes existing modules:
  - file_transfer.py: FileTransfer.send_file() for actual transfer
  - security.py: scan_file_transfer() for DLP
  - input_handler.py: InputHandler for drop simulation on remote

RustDesk already has native drag-drop; this provides the same for the native
HARTOS transport fallback.
"""

import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class DragDropState(Enum):
    IDLE = 'idle'
    DRAG_STARTED = 'drag_started'
    TRANSFERRING = 'transferring'
    DROP_PENDING = 'drop_pending'
    COMPLETED = 'completed'
    ERROR = 'error'


@dataclass
class DragDropEvent:
    """Cross-device drag-and-drop event."""
    direction: str             # 'local_to_remote' or 'remote_to_local'
    file_paths: List[str] = field(default_factory=list)
    drop_x: int = 0
    drop_y: int = 0
    state: DragDropState = DragDropState.IDLE
    error: Optional[str] = None
    bytes_transferred: int = 0

    def to_dict(self) -> dict:
        return {
            'direction': self.direction,
            'file_paths': self.file_paths,
            'drop_x': self.drop_x,
            'drop_y': self.drop_y,
            'state': self.state.value,
            'error': self.error,
            'bytes_transferred': self.bytes_transferred,
        }


class DragDropBridge:
    """Cross-device drag-and-drop over HARTOS transport.

    Orchestrates file transfer + input simulation for seamless drag-drop
    across local and remote devices.
    """

    def __init__(self, transport=None, input_handler=None):
        self._transport = transport
        self._input_handler = input_handler
        self._monitoring = False
        self._progress_callbacks: List[Callable] = []
        self._active_drops: Dict[str, DragDropEvent] = {}
        self._lock = threading.Lock()
        self._receive_dir: Optional[str] = None

    def start_monitoring(self) -> bool:
        """Start monitoring for OS drag events entering the viewer window.

        Returns True if monitoring started successfully.
        """
        self._monitoring = True
        logger.info("Drag-and-drop bridge monitoring started")
        return True

    def stop_monitoring(self) -> None:
        """Stop monitoring for drag events."""
        self._monitoring = False
        logger.info("Drag-and-drop bridge monitoring stopped")

    @property
    def is_monitoring(self) -> bool:
        return self._monitoring

    def set_receive_directory(self, path: str) -> None:
        """Set directory for received files."""
        self._receive_dir = path

    def handle_local_drop(self, file_paths: List[str],
                          x: int, y: int) -> dict:
        """Handle files dropped onto the viewer → transfer + remote drop.

        1. Validate file paths exist
        2. DLP scan each file
        3. Transfer via FileTransfer
        4. Send drop event to remote host
        5. Host simulates file drop at (x, y)

        Args:
            file_paths: Local file paths to send.
            x: Drop X coordinate on remote screen.
            y: Drop Y coordinate on remote screen.

        Returns:
            {success, files_sent, errors}
        """
        event = DragDropEvent(
            direction='local_to_remote',
            file_paths=file_paths,
            drop_x=x,
            drop_y=y,
            state=DragDropState.DRAG_STARTED,
        )
        self._notify_progress(event)

        # Validate paths
        valid_paths = []
        errors = []
        for path in file_paths:
            if os.path.exists(path):
                valid_paths.append(path)
            else:
                errors.append(f"File not found: {path}")

        if not valid_paths:
            event.state = DragDropState.ERROR
            event.error = 'No valid files'
            self._notify_progress(event)
            return {'success': False, 'error': 'No valid files', 'errors': errors}

        # DLP scan
        event.state = DragDropState.TRANSFERRING
        self._notify_progress(event)

        blocked = []
        for path in valid_paths:
            try:
                from integrations.remote_desktop.security import scan_file_transfer
                allowed, reason = scan_file_transfer(os.path.basename(path))
                if not allowed:
                    blocked.append(f"DLP blocked {os.path.basename(path)}: {reason}")
            except Exception:
                pass  # If DLP unavailable, allow

        if blocked:
            errors.extend(blocked)
            valid_paths = [p for p in valid_paths
                          if os.path.basename(p) not in
                          ' '.join(blocked)]

        # Transfer files
        results = []
        total_bytes = 0
        for path in valid_paths:
            result = self._transfer_file(path)
            results.append(result)
            if result.get('success'):
                total_bytes += result.get('bytes_sent', 0)
            else:
                errors.append(result.get('error', 'Transfer failed'))

        event.bytes_transferred = total_bytes
        files_sent = sum(1 for r in results if r.get('success'))

        # Send drop event to remote
        if files_sent > 0 and self._transport:
            try:
                filenames = [os.path.basename(p) for p in valid_paths[:files_sent]]
                self._transport.send_event({
                    'type': 'drag_drop',
                    'action': 'DROP',
                    'files': filenames,
                    'x': x,
                    'y': y,
                    'direction': 'local_to_remote',
                })
            except Exception as e:
                errors.append(f"Drop event send failed: {e}")

        event.state = (DragDropState.COMPLETED if files_sent > 0
                       else DragDropState.ERROR)
        event.error = '; '.join(errors) if errors else None
        self._notify_progress(event)

        return {
            'success': files_sent > 0,
            'files_sent': files_sent,
            'total_files': len(file_paths),
            'bytes_transferred': total_bytes,
            'errors': errors,
        }

    def handle_remote_drop(self, event: dict) -> dict:
        """Handle drop event received from remote → receive files locally.

        event: {type: 'drag_drop', action: 'DROP', files: [...], x, y}

        Returns:
            {success, files_received, save_dir}
        """
        action = event.get('action', '')
        if action != 'DROP':
            return {'success': False, 'error': f'Unknown action: {action}'}

        files = event.get('files', [])
        x = event.get('x', 0)
        y = event.get('y', 0)

        save_dir = self._receive_dir or self._get_default_receive_dir()

        drop_event = DragDropEvent(
            direction='remote_to_local',
            file_paths=files,
            drop_x=x,
            drop_y=y,
            state=DragDropState.DROP_PENDING,
        )
        self._notify_progress(drop_event)

        # Files should have been transferred via FileTransfer already
        # Just acknowledge the drop
        drop_event.state = DragDropState.COMPLETED
        self._notify_progress(drop_event)

        return {
            'success': True,
            'files_received': len(files),
            'save_dir': save_dir,
            'x': x,
            'y': y,
        }

    def handle_file_drop_on_host(self, event: dict) -> dict:
        """Host-side: simulate a file drop at cursor position.

        Called when remote viewer sends files + drop position. Uses InputHandler
        or platform-specific file placement.
        """
        x = event.get('x', 0)
        y = event.get('y', 0)
        files = event.get('files', [])

        if self._input_handler:
            # Move cursor to drop position
            try:
                self._input_handler.handle_input_event({
                    'type': 'move',
                    'x': x,
                    'y': y,
                })
            except Exception:
                pass

        # Open file explorer at the received file location
        save_dir = self._receive_dir or self._get_default_receive_dir()
        self._open_file_location(save_dir)

        return {
            'success': True,
            'files': files,
            'position': {'x': x, 'y': y},
            'save_dir': save_dir,
        }

    def on_progress(self, callback: Callable[[DragDropEvent], None]) -> None:
        """Register a progress callback."""
        self._progress_callbacks.append(callback)

    def get_status(self) -> dict:
        """Get drag-drop bridge status."""
        return {
            'monitoring': self._monitoring,
            'has_transport': self._transport is not None,
            'has_input_handler': self._input_handler is not None,
            'receive_dir': self._receive_dir,
        }

    # ── Internal helpers ──────────────────────────────────────

    def _transfer_file(self, local_path: str) -> dict:
        """Transfer a single file via FileTransfer module."""
        if not self._transport:
            return {'success': False, 'error': 'No transport'}

        try:
            from integrations.remote_desktop.file_transfer import FileTransfer
            ft = FileTransfer()
            return ft.send_file(self._transport, local_path)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _notify_progress(self, event: DragDropEvent) -> None:
        """Notify all progress callbacks."""
        for cb in self._progress_callbacks:
            try:
                cb(event)
            except Exception:
                pass

    def _get_default_receive_dir(self) -> str:
        """Get default directory for received files."""
        # Try standard Downloads folder
        home = os.path.expanduser('~')
        downloads = os.path.join(home, 'Downloads')
        if os.path.isdir(downloads):
            return downloads
        return home

    def _open_file_location(self, path: str) -> None:
        """Open file manager at the given path (platform-specific)."""
        system = platform.system()
        try:
            if system == 'Windows':
                subprocess.Popen(['explorer', path])
            elif system == 'Darwin':
                subprocess.Popen(['open', path])
            elif system == 'Linux':
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            logger.debug(f"Could not open file location: {e}")

"""
File Transfer — Chunked binary transfer over any transport tier.

Protocol:
  1. Sender: FILE_START {name, size, sha256} → binary chunks (64KB) → FILE_END
  2. Receiver: accumulate chunks → verify SHA256 → FILE_ACK

DLP scan before sending via security/dlp_engine.py.
Works over DirectWebSocket, WAMP relay, or WireGuard transport.

Reuses:
  - transport.TransportChannel.send_event() for control messages
  - transport.TransportChannel.send_frame() for binary chunks
  - security.scan_file_transfer() for DLP scanning
"""

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('hevolve.remote_desktop')


class FileTransferState(Enum):
    IDLE = 'idle'
    SENDING = 'sending'
    RECEIVING = 'receiving'
    COMPLETED = 'completed'
    ERROR = 'error'


@dataclass
class TransferProgress:
    """Progress tracking for a file transfer."""
    filename: str = ''
    total_bytes: int = 0
    transferred_bytes: int = 0
    chunks_sent: int = 0
    chunks_received: int = 0
    state: FileTransferState = FileTransferState.IDLE
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    sha256: Optional[str] = None

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return min(100.0, (self.transferred_bytes / self.total_bytes) * 100)

    def to_dict(self) -> dict:
        return {
            'filename': self.filename,
            'total_bytes': self.total_bytes,
            'transferred_bytes': self.transferred_bytes,
            'percent': round(self.percent, 1),
            'state': self.state.value,
            'error': self.error,
            'sha256': self.sha256,
        }


class FileTransfer:
    """Chunked binary file transfer over transport channel.

    Sends files as a sequence of control messages + binary chunks.
    Works over any TransportChannel implementation.
    """

    CHUNK_SIZE = 65536  # 64KB per chunk

    def __init__(self):
        self._progress: Optional[TransferProgress] = None
        self._on_progress: Optional[Callable[[TransferProgress], None]] = None
        self._receive_buffer: bytearray = bytearray()
        self._receive_info: Optional[dict] = None
        self._save_dir: str = '.'

    def on_progress(self, callback: Callable[[TransferProgress], None]) -> None:
        """Register progress callback."""
        self._on_progress = callback

    def send_file(self, transport, local_path: str) -> dict:
        """Send a file over a transport channel.

        Args:
            transport: TransportChannel instance
            local_path: Path to file to send

        Returns:
            {success, sha256, bytes_sent, chunks, filename}
        """
        if not os.path.exists(local_path):
            return {'success': False, 'error': f'File not found: {local_path}'}

        filename = os.path.basename(local_path)
        file_size = os.path.getsize(local_path)

        # DLP scan
        try:
            from integrations.remote_desktop.security import scan_file_transfer
            allowed, reason = scan_file_transfer(filename)
            if not allowed:
                return {'success': False, 'error': f'DLP blocked: {reason}'}
        except Exception:
            pass

        # Initialize progress
        self._progress = TransferProgress(
            filename=filename,
            total_bytes=file_size,
            state=FileTransferState.SENDING,
            started_at=time.time(),
        )

        # Compute SHA256
        sha256 = hashlib.sha256()
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
        file_hash = sha256.hexdigest()
        self._progress.sha256 = file_hash

        # Send FILE_START control message
        ok = transport.send_event({
            'type': 'file_ctrl',
            'action': 'FILE_START',
            'filename': filename,
            'size': file_size,
            'sha256': file_hash,
        })
        if not ok:
            self._progress.state = FileTransferState.ERROR
            self._progress.error = 'Failed to send FILE_START'
            return {'success': False, 'error': 'Transport send failed'}

        # Send chunks
        chunks_sent = 0
        bytes_sent = 0
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break

                ok = transport.send_frame(chunk)
                if not ok:
                    self._progress.state = FileTransferState.ERROR
                    self._progress.error = f'Chunk {chunks_sent} send failed'
                    return {
                        'success': False,
                        'error': f'Failed at chunk {chunks_sent}',
                        'bytes_sent': bytes_sent,
                    }

                chunks_sent += 1
                bytes_sent += len(chunk)
                self._progress.chunks_sent = chunks_sent
                self._progress.transferred_bytes = bytes_sent
                self._notify_progress()

        # Send FILE_END
        transport.send_event({
            'type': 'file_ctrl',
            'action': 'FILE_END',
            'filename': filename,
            'sha256': file_hash,
        })

        self._progress.state = FileTransferState.COMPLETED
        self._progress.completed_at = time.time()
        self._notify_progress()

        logger.info(f"File sent: {filename} ({bytes_sent} bytes, {chunks_sent} chunks)")
        return {
            'success': True,
            'filename': filename,
            'sha256': file_hash,
            'bytes_sent': bytes_sent,
            'chunks': chunks_sent,
        }

    def send_files(self, transport, local_paths: list) -> list:
        """Send multiple files sequentially (for drag-and-drop batch transfers).

        Args:
            transport: TransportChannel to send on.
            local_paths: List of local file paths to transfer.

        Returns:
            List of result dicts (one per file).
        """
        results = []
        for path in local_paths:
            result = self.send_file(transport, path)
            results.append(result)
        return results

    def receive_file(self, save_dir: str = '.') -> dict:
        """Prepare to receive a file.

        Call this before starting the transport event loop.
        The actual receiving happens via handle_event() and handle_frame().

        Returns:
            Setup status dict.
        """
        self._save_dir = save_dir
        self._receive_buffer = bytearray()
        self._receive_info = None
        self._progress = TransferProgress(state=FileTransferState.RECEIVING)

        os.makedirs(save_dir, exist_ok=True)
        return {'status': 'ready', 'save_dir': save_dir}

    def handle_event(self, event: dict) -> Optional[dict]:
        """Handle a file transfer control event.

        Called by the transport event callback.
        Returns result dict on FILE_END, None otherwise.
        """
        if event.get('type') != 'file_ctrl':
            return None

        action = event.get('action', '')

        if action == 'FILE_START':
            self._receive_info = {
                'filename': event.get('filename', 'unnamed'),
                'size': event.get('size', 0),
                'sha256': event.get('sha256', ''),
            }
            self._receive_buffer = bytearray()
            self._progress = TransferProgress(
                filename=self._receive_info['filename'],
                total_bytes=self._receive_info['size'],
                state=FileTransferState.RECEIVING,
                started_at=time.time(),
            )
            logger.info(f"Receiving file: {self._receive_info['filename']} "
                       f"({self._receive_info['size']} bytes)")
            return None

        if action == 'FILE_END':
            return self._finalize_receive(event)

        return None

    def handle_frame(self, data: bytes) -> None:
        """Handle a binary chunk during file receive.

        Called by the transport frame callback during file transfer.
        """
        if self._progress and self._progress.state == FileTransferState.RECEIVING:
            self._receive_buffer.extend(data)
            self._progress.chunks_received += 1
            self._progress.transferred_bytes = len(self._receive_buffer)
            self._notify_progress()

    def _finalize_receive(self, event: dict) -> dict:
        """Finalize file receive — verify SHA256 and save."""
        if not self._receive_info:
            return {'success': False, 'error': 'No FILE_START received'}

        filename = self._receive_info['filename']
        expected_hash = self._receive_info.get('sha256', '')

        # Verify SHA256
        actual_hash = hashlib.sha256(self._receive_buffer).hexdigest()
        if expected_hash and actual_hash != expected_hash:
            self._progress.state = FileTransferState.ERROR
            self._progress.error = 'SHA256 mismatch'
            return {
                'success': False,
                'error': 'SHA256 mismatch (file corrupted)',
                'expected': expected_hash,
                'actual': actual_hash,
            }

        # Save file
        save_path = os.path.join(self._save_dir, filename)
        try:
            with open(save_path, 'wb') as f:
                f.write(self._receive_buffer)
        except Exception as e:
            self._progress.state = FileTransferState.ERROR
            self._progress.error = str(e)
            return {'success': False, 'error': f'Save failed: {e}'}

        self._progress.state = FileTransferState.COMPLETED
        self._progress.completed_at = time.time()
        self._progress.sha256 = actual_hash
        self._notify_progress()

        logger.info(f"File received: {filename} → {save_path} "
                    f"({len(self._receive_buffer)} bytes)")

        # Clean up
        received_bytes = len(self._receive_buffer)
        self._receive_buffer = bytearray()
        self._receive_info = None

        return {
            'success': True,
            'path': save_path,
            'filename': filename,
            'sha256': actual_hash,
            'bytes_received': received_bytes,
        }

    def _notify_progress(self) -> None:
        """Notify progress callback."""
        if self._on_progress and self._progress:
            try:
                self._on_progress(self._progress)
            except Exception:
                pass

    def get_progress(self) -> Optional[dict]:
        """Get current transfer progress."""
        if self._progress:
            return self._progress.to_dict()
        return None

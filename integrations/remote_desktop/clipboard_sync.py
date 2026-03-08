"""
Clipboard Sync — Bidirectional clipboard sharing between host and viewer.

Reuses pyperclip (already in codebase via local_computer_tool.py:28).
DLP scan on outbound clipboard via security/dlp_engine.py.
"""

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger('hevolve.remote_desktop')

# ── Optional dependency ─────────────────────────────────────────

_pyperclip = None
try:
    import pyperclip as _pyperclip_module
    _pyperclip = _pyperclip_module
except ImportError:
    pass


class ClipboardSync:
    """Bidirectional clipboard synchronization.

    Monitors local clipboard for changes and sends to remote.
    Receives remote clipboard content and applies locally.
    """

    POLL_INTERVAL = 0.5  # 500ms

    def __init__(self, on_change: Optional[Callable[[str], None]] = None,
                 dlp_enabled: bool = True):
        """
        Args:
            on_change: Callback when local clipboard changes (sends to remote)
            dlp_enabled: Whether to DLP-scan outbound clipboard
        """
        self._on_change = on_change
        self._dlp_enabled = dlp_enabled
        self._last_content: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._paused = False

    def start_monitoring(self) -> bool:
        """Start background clipboard monitoring thread.

        Returns:
            True if started, False if pyperclip unavailable.
        """
        if not _pyperclip:
            logger.warning("Clipboard sync unavailable: pyperclip not installed")
            return False
        if self._running:
            return True

        self._running = True
        try:
            self._last_content = _pyperclip.paste()
        except Exception:
            self._last_content = ''

        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name='clipboard-sync',
        )
        self._thread.start()
        logger.info("Clipboard monitoring started")
        return True

    def stop_monitoring(self) -> None:
        """Stop clipboard monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("Clipboard monitoring stopped")

    def pause(self) -> None:
        """Pause monitoring (e.g., while applying remote clipboard)."""
        self._paused = True

    def resume(self) -> None:
        """Resume monitoring."""
        self._paused = False

    def apply_remote_clipboard(self, content: str) -> bool:
        """Set local clipboard from remote content.

        Pauses monitoring to avoid echo loop.

        Returns:
            True if clipboard was set successfully.
        """
        if not _pyperclip:
            return False

        with self._lock:
            self.pause()
            try:
                _pyperclip.copy(content)
                self._last_content = content
                logger.debug(f"Remote clipboard applied ({len(content)} chars)")
                return True
            except Exception as e:
                logger.warning(f"Failed to set clipboard: {e}")
                return False
            finally:
                self.resume()

    def get_current(self) -> Optional[str]:
        """Get current clipboard content."""
        if not _pyperclip:
            return None
        try:
            return _pyperclip.paste()
        except Exception:
            return None

    def _monitor_loop(self) -> None:
        """Background loop: detect clipboard changes and notify."""
        while self._running:
            try:
                if not self._paused:
                    current = _pyperclip.paste()
                    if current != self._last_content:
                        self._last_content = current
                        self._handle_change(current)
            except Exception as e:
                logger.debug(f"Clipboard poll error: {e}")

            time.sleep(self.POLL_INTERVAL)

    def _handle_change(self, content: str) -> None:
        """Process clipboard change — DLP scan + notify."""
        if not content:
            return

        # DLP scan outbound clipboard
        if self._dlp_enabled:
            try:
                from integrations.remote_desktop.security import scan_clipboard
                allowed, reason = scan_clipboard(content)
                if not allowed:
                    logger.warning(f"Clipboard blocked by DLP: {reason}")
                    return
            except Exception:
                pass

        # Notify callback
        if self._on_change:
            try:
                self._on_change(content)
            except Exception as e:
                logger.error(f"Clipboard change callback error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running

    def get_stats(self) -> dict:
        return {
            'running': self._running,
            'paused': self._paused,
            'dlp_enabled': self._dlp_enabled,
            'pyperclip_available': _pyperclip is not None,
        }

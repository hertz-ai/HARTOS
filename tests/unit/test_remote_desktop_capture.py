"""Tests for Remote Desktop Phase 2 — Frame Capture, Input Handler, Clipboard Sync."""
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.remote_desktop.frame_capture import (
    FrameCapture, FrameConfig, _CaptureCircuitBreaker,
)
from integrations.remote_desktop.input_handler import InputHandler
from integrations.remote_desktop.clipboard_sync import ClipboardSync


# ═══════════════════════════════════════════════════════════════
# Frame Capture Tests
# ═══════════════════════════════════════════════════════════════

class TestFrameConfig(unittest.TestCase):
    """FrameConfig defaults and customization."""

    def test_default_config(self):
        config = FrameConfig()
        self.assertEqual(config.max_fps, 30)
        self.assertEqual(config.quality, 80)
        self.assertEqual(config.scale_factor, 1.0)
        self.assertAlmostEqual(config.min_change_threshold, 0.01)

    def test_custom_config(self):
        config = FrameConfig(max_fps=60, quality=50, scale_factor=0.5)
        self.assertEqual(config.max_fps, 60)
        self.assertEqual(config.quality, 50)
        self.assertEqual(config.scale_factor, 0.5)


class TestCircuitBreaker(unittest.TestCase):
    """Circuit breaker for capture backends."""

    def test_initially_closed(self):
        cb = _CaptureCircuitBreaker(threshold=3)
        self.assertFalse(cb.is_open('mss'))

    def test_opens_after_threshold(self):
        cb = _CaptureCircuitBreaker(threshold=3)
        cb.record_failure('mss')
        cb.record_failure('mss')
        self.assertFalse(cb.is_open('mss'))
        cb.record_failure('mss')
        self.assertTrue(cb.is_open('mss'))

    def test_success_resets_count(self):
        cb = _CaptureCircuitBreaker(threshold=3)
        cb.record_failure('mss')
        cb.record_failure('mss')
        cb.record_success('mss')
        cb.record_failure('mss')
        cb.record_failure('mss')
        self.assertFalse(cb.is_open('mss'))

    def test_independent_backends(self):
        cb = _CaptureCircuitBreaker(threshold=2)
        cb.record_failure('mss')
        cb.record_failure('mss')
        self.assertTrue(cb.is_open('mss'))
        self.assertFalse(cb.is_open('pyautogui'))

    def test_reset(self):
        cb = _CaptureCircuitBreaker(threshold=2)
        cb.record_failure('mss')
        cb.record_failure('mss')
        self.assertTrue(cb.is_open('mss'))
        cb.reset('mss')
        self.assertFalse(cb.is_open('mss'))


class TestFrameCapture(unittest.TestCase):
    """Frame capture with mocked backends."""

    def test_get_screen_size_fallback(self):
        """Returns default (1920, 1080) when no backends available."""
        fc = FrameCapture()
        # Patch out all backends
        import integrations.remote_desktop.frame_capture as fc_mod
        orig_mss = fc_mod._mss
        orig_pyautogui = fc_mod._pyautogui
        fc_mod._mss = None
        fc_mod._pyautogui = None
        try:
            w, h = fc.get_screen_size()
            self.assertEqual((w, h), (1920, 1080))
        finally:
            fc_mod._mss = orig_mss
            fc_mod._pyautogui = orig_pyautogui

    def test_capture_frame_returns_none_when_all_fail(self):
        """Returns None when all backends are circuit-broken."""
        fc = FrameCapture()
        fc._circuit._open = {'dxcam', 'mss', 'pyautogui'}
        result = fc.capture_frame()
        self.assertIsNone(result)

    def test_capture_frame_with_mock_pyautogui(self):
        """Captures frame via pyautogui when mss/dxcam unavailable."""
        import integrations.remote_desktop.frame_capture as fc_mod
        orig_mss = fc_mod._mss
        orig_dxcam = fc_mod._dxcam
        orig_pyautogui = fc_mod._pyautogui
        orig_pil = fc_mod._PIL_Image

        try:
            fc_mod._mss = None
            fc_mod._dxcam = None

            # Mock pyautogui screenshot
            mock_img = MagicMock()
            mock_buf = MagicMock()
            mock_img.save = MagicMock(side_effect=lambda buf, **kw: buf.write(b'fake_jpeg'))
            mock_pyautogui = MagicMock()
            mock_pyautogui.screenshot.return_value = mock_img
            fc_mod._pyautogui = mock_pyautogui

            # Mock PIL Image
            if orig_pil is None:
                mock_pil = MagicMock()
                fc_mod._PIL_Image = mock_pil

            fc = FrameCapture()
            frame = fc.capture_frame()
            # Should have called pyautogui.screenshot
            mock_pyautogui.screenshot.assert_called_once()
        finally:
            fc_mod._mss = orig_mss
            fc_mod._dxcam = orig_dxcam
            fc_mod._pyautogui = orig_pyautogui
            fc_mod._PIL_Image = orig_pil

    def test_get_stats(self):
        fc = FrameCapture()
        stats = fc.get_stats()
        self.assertIn('running', stats)
        self.assertIn('frame_count', stats)
        self.assertIn('config', stats)
        self.assertIn('backends', stats)
        self.assertEqual(stats['frame_count'], 0)

    def test_stop(self):
        fc = FrameCapture()
        fc._running = True
        fc.stop()
        self.assertFalse(fc.is_running())


# ═══════════════════════════════════════════════════════════════
# Input Handler Tests
# ═══════════════════════════════════════════════════════════════

class TestInputHandler(unittest.TestCase):
    """Input handler with mocked backends."""

    def test_view_only_blocks_input(self):
        handler = InputHandler(allow_control=False)
        result = handler.handle_input_event({'type': 'click', 'x': 100, 'y': 200})
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'view_only_mode')

    def test_toggle_control_mode(self):
        handler = InputHandler(allow_control=True)
        self.assertTrue(handler.control_enabled)
        handler.set_control_mode(False)
        self.assertFalse(handler.control_enabled)

    def test_destructive_input_blocked(self):
        handler = InputHandler(allow_control=True)
        result = handler.handle_input_event({'type': 'hotkey', 'hotkey': 'alt+f4'})
        self.assertFalse(result['success'])
        self.assertEqual(result['error'], 'destructive_action_blocked')
        self.assertEqual(result['classification'], 'destructive')

    def test_unknown_event_type(self):
        handler = InputHandler(allow_control=True)
        result = handler.handle_input_event({'type': 'nonexistent_type'})
        self.assertFalse(result['success'])
        self.assertIn('unknown_event_type', result['error'])

    def test_click_event_classified_safe(self):
        """Click events are classified as safe."""
        handler = InputHandler(allow_control=True)
        # Mock the actual click execution (no real mouse control in tests)
        handler._handle_click = MagicMock()
        result = handler.handle_input_event({'type': 'click', 'x': 50, 'y': 50})
        self.assertTrue(result['success'])
        self.assertEqual(result['classification'], 'safe')
        handler._handle_click.assert_called_once()

    def test_type_event(self):
        handler = InputHandler(allow_control=True)
        handler._handle_type = MagicMock()
        result = handler.handle_input_event({'type': 'type', 'text': 'hello'})
        self.assertTrue(result['success'])
        handler._handle_type.assert_called_once()

    def test_get_stats(self):
        handler = InputHandler()
        stats = handler.get_stats()
        self.assertIn('control_enabled', stats)
        self.assertIn('event_count', stats)
        self.assertIn('backends', stats)

    def test_event_count_increments(self):
        handler = InputHandler(allow_control=True)
        handler._handle_click = MagicMock()
        handler.handle_input_event({'type': 'click', 'x': 0, 'y': 0})
        handler.handle_input_event({'type': 'click', 'x': 0, 'y': 0})
        self.assertEqual(handler._event_count, 2)


# ═══════════════════════════════════════════════════════════════
# Clipboard Sync Tests
# ═══════════════════════════════════════════════════════════════

class TestClipboardSync(unittest.TestCase):
    """Clipboard synchronization."""

    def test_init_defaults(self):
        cs = ClipboardSync()
        self.assertFalse(cs.is_running)
        self.assertTrue(cs._dlp_enabled)

    def test_start_without_pyperclip(self):
        """Graceful failure when pyperclip not available."""
        import integrations.remote_desktop.clipboard_sync as cs_mod
        orig = cs_mod._pyperclip
        cs_mod._pyperclip = None
        try:
            cs = ClipboardSync()
            result = cs.start_monitoring()
            self.assertFalse(result)
            self.assertFalse(cs.is_running)
        finally:
            cs_mod._pyperclip = orig

    def test_apply_remote_clipboard_without_pyperclip(self):
        import integrations.remote_desktop.clipboard_sync as cs_mod
        orig = cs_mod._pyperclip
        cs_mod._pyperclip = None
        try:
            cs = ClipboardSync()
            result = cs.apply_remote_clipboard('hello')
            self.assertFalse(result)
        finally:
            cs_mod._pyperclip = orig

    def test_apply_remote_clipboard_with_mock(self):
        import integrations.remote_desktop.clipboard_sync as cs_mod
        mock_pyperclip = MagicMock()
        orig = cs_mod._pyperclip
        cs_mod._pyperclip = mock_pyperclip
        try:
            cs = ClipboardSync()
            result = cs.apply_remote_clipboard('test content')
            self.assertTrue(result)
            mock_pyperclip.copy.assert_called_once_with('test content')
        finally:
            cs_mod._pyperclip = orig

    def test_pause_resume(self):
        cs = ClipboardSync()
        self.assertFalse(cs._paused)
        cs.pause()
        self.assertTrue(cs._paused)
        cs.resume()
        self.assertFalse(cs._paused)

    def test_get_stats(self):
        cs = ClipboardSync()
        stats = cs.get_stats()
        self.assertIn('running', stats)
        self.assertIn('paused', stats)
        self.assertIn('dlp_enabled', stats)
        self.assertIn('pyperclip_available', stats)

    def test_on_change_callback(self):
        changes = []
        cs = ClipboardSync(on_change=lambda c: changes.append(c))
        cs._dlp_enabled = False  # Skip DLP for unit test
        cs._handle_change('new clipboard content')
        self.assertEqual(changes, ['new clipboard content'])

    def test_empty_change_ignored(self):
        changes = []
        cs = ClipboardSync(on_change=lambda c: changes.append(c))
        cs._handle_change('')
        self.assertEqual(changes, [])


if __name__ == '__main__':
    unittest.main()

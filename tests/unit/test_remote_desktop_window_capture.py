"""
Tests for window-level capture + multi-session (tab detach).

Covers: window_capture.py, window_session.py
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock


class TestWindowInfo(unittest.TestCase):
    """Tests for WindowInfo dataclass."""

    def test_window_info_creation(self):
        from integrations.remote_desktop.window_capture import WindowInfo
        w = WindowInfo(
            hwnd=12345, title='Notepad', process_name='notepad.exe',
            pid=1000, rect=(0, 0, 800, 600), visible=True, minimized=False,
        )
        self.assertEqual(w.hwnd, 12345)
        self.assertEqual(w.title, 'Notepad')
        self.assertEqual(w.process_name, 'notepad.exe')
        self.assertTrue(w.visible)

    def test_window_info_to_dict(self):
        from integrations.remote_desktop.window_capture import WindowInfo
        w = WindowInfo(
            hwnd=1, title='Test', process_name='test.exe',
            pid=42, rect=(10, 20, 300, 200), visible=True, minimized=False,
        )
        d = w.to_dict()
        self.assertEqual(d['hwnd'], 1)
        self.assertEqual(d['title'], 'Test')
        self.assertEqual(list(d['rect']), [10, 20, 300, 200])

    def test_window_info_from_dict(self):
        from integrations.remote_desktop.window_capture import WindowInfo
        d = {
            'hwnd': 99, 'title': 'CMD', 'process_name': 'cmd.exe',
            'pid': 500, 'rect': [0, 0, 640, 480],
            'visible': True, 'minimized': False,
        }
        w = WindowInfo.from_dict(d)
        self.assertEqual(w.hwnd, 99)
        self.assertEqual(w.title, 'CMD')

    def test_window_info_defaults(self):
        from integrations.remote_desktop.window_capture import WindowInfo
        w = WindowInfo(
            hwnd=0, title='', process_name='',
            pid=0, rect=(0, 0, 0, 0),
        )
        self.assertTrue(w.visible)  # default
        self.assertFalse(w.minimized)  # default


class TestWindowEnumerator(unittest.TestCase):
    """Tests for WindowEnumerator."""

    def test_enumerator_creation(self):
        from integrations.remote_desktop.window_capture import WindowEnumerator
        e = WindowEnumerator()
        self.assertIsNotNone(e)

    def test_list_windows_returns_list(self):
        from integrations.remote_desktop.window_capture import WindowEnumerator
        e = WindowEnumerator()
        result = e.list_windows()
        self.assertIsInstance(result, list)

    @patch('platform.system', return_value='Linux')
    def test_list_windows_linux_fallback(self, mock_sys):
        from integrations.remote_desktop.window_capture import WindowEnumerator
        e = WindowEnumerator()
        # Should return empty list on Linux without X11
        result = e.list_windows()
        self.assertIsInstance(result, list)

    def test_get_window_by_title_not_found(self):
        from integrations.remote_desktop.window_capture import WindowEnumerator
        e = WindowEnumerator()
        # Search for nonexistent window
        result = e.get_window_by_title('NONEXISTENT_WINDOW_XYZ_12345')
        self.assertIsNone(result)

    def test_get_window_by_pid_not_found(self):
        from integrations.remote_desktop.window_capture import WindowEnumerator
        e = WindowEnumerator()
        result = e.get_window_by_pid(999999999)
        self.assertIsNone(result)

    def test_get_window_by_title_case_insensitive(self):
        """Title matching should be case-insensitive."""
        from integrations.remote_desktop.window_capture import (
            WindowEnumerator, WindowInfo,
        )
        e = WindowEnumerator()
        # Mock list_windows to return a test window
        e.list_windows = lambda **kw: [
            WindowInfo(hwnd=1, title='My Notepad', process_name='notepad.exe',
                       pid=100, rect=(0, 0, 800, 600)),
        ]
        result = e.get_window_by_title('notepad')
        self.assertIsNotNone(result)
        self.assertEqual(result.hwnd, 1)


class TestWindowCaptureConfig(unittest.TestCase):
    """Tests for WindowCaptureConfig."""

    def test_default_config(self):
        from integrations.remote_desktop.window_capture import WindowCaptureConfig
        cfg = WindowCaptureConfig()
        self.assertGreater(cfg.quality, 0)
        self.assertGreater(cfg.max_fps, 0)

    def test_custom_config(self):
        from integrations.remote_desktop.window_capture import WindowCaptureConfig
        cfg = WindowCaptureConfig(quality=50, max_fps=15, scale_factor=0.5)
        self.assertEqual(cfg.quality, 50)
        self.assertEqual(cfg.max_fps, 15)
        self.assertEqual(cfg.scale_factor, 0.5)


class TestWindowCapture(unittest.TestCase):
    """Tests for WindowCapture."""

    def _make_window_info(self):
        from integrations.remote_desktop.window_capture import WindowInfo
        return WindowInfo(
            hwnd=12345, title='Test', process_name='test.exe',
            pid=100, rect=(0, 0, 800, 600),
        )

    def test_window_capture_creation(self):
        from integrations.remote_desktop.window_capture import WindowCapture
        winfo = self._make_window_info()
        cap = WindowCapture(winfo)
        self.assertIsNotNone(cap)

    def test_capture_frame_returns_bytes_or_none(self):
        from integrations.remote_desktop.window_capture import WindowCapture
        winfo = self._make_window_info()
        cap = WindowCapture(winfo)
        frame = cap.capture_frame()
        # May return None if no display/capture backend available
        self.assertTrue(frame is None or isinstance(frame, bytes))

    def test_get_window_info(self):
        from integrations.remote_desktop.window_capture import WindowCapture
        winfo = self._make_window_info()
        cap = WindowCapture(winfo)
        result = cap.get_window_info()
        self.assertEqual(result.hwnd, 12345)

    def test_window_capture_with_config(self):
        from integrations.remote_desktop.window_capture import (
            WindowCapture, WindowCaptureConfig,
        )
        winfo = self._make_window_info()
        cfg = WindowCaptureConfig(quality=30, max_fps=10)
        cap = WindowCapture(winfo, config=cfg)
        self.assertIsNotNone(cap)


class TestWindowSession(unittest.TestCase):
    """Tests for WindowSession dataclass."""

    def test_window_session_creation(self):
        from integrations.remote_desktop.window_session import WindowSession
        s = WindowSession(
            session_id='ws-001',
            window_hwnd=12345,
            window_title='Notepad',
            process_name='notepad.exe',
            started_at=1000.0,
        )
        self.assertEqual(s.session_id, 'ws-001')
        self.assertEqual(s.window_hwnd, 12345)

    def test_window_session_to_dict(self):
        from integrations.remote_desktop.window_session import WindowSession
        s = WindowSession(
            session_id='ws-002',
            window_hwnd=1,
            window_title='CMD',
            process_name='cmd.exe',
            started_at=2000.0,
        )
        d = s.to_dict()
        self.assertEqual(d['session_id'], 'ws-002')
        self.assertIn('window_title', d)


class TestWindowSessionManager(unittest.TestCase):
    """Tests for WindowSessionManager."""

    def test_manager_creation(self):
        from integrations.remote_desktop.window_session import WindowSessionManager
        m = WindowSessionManager()
        self.assertIsNotNone(m)

    def test_list_available_windows(self):
        from integrations.remote_desktop.window_session import WindowSessionManager
        m = WindowSessionManager()
        result = m.list_available_windows()
        self.assertIsInstance(result, list)

    def test_get_active_sessions_empty(self):
        from integrations.remote_desktop.window_session import WindowSessionManager
        m = WindowSessionManager()
        sessions = m.get_active_window_sessions()
        self.assertIsInstance(sessions, list)
        self.assertEqual(len(sessions), 0)

    def test_stop_nonexistent_session(self):
        from integrations.remote_desktop.window_session import WindowSessionManager
        m = WindowSessionManager()
        result = m.stop_window_session('nonexistent-id')
        self.assertFalse(result)

    def test_stop_all_empty(self):
        from integrations.remote_desktop.window_session import WindowSessionManager
        m = WindowSessionManager()
        m.stop_all()  # Should not raise

    def test_singleton(self):
        import integrations.remote_desktop.window_session as ws_mod
        ws_mod._window_session_manager = None
        m1 = ws_mod.get_window_session_manager()
        m2 = ws_mod.get_window_session_manager()
        self.assertIs(m1, m2)
        ws_mod._window_session_manager = None  # cleanup


class TestWindowOrchestratorIntegration(unittest.TestCase):
    """Tests for window methods on orchestrator."""

    def test_list_remote_windows(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.list_remote_windows()
        self.assertIsInstance(result, list)

    def test_get_window_sessions_empty(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.get_window_sessions()
        self.assertIsInstance(result, list)

    def test_stop_window_stream_nonexistent(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.stop_window_stream('fake-id')
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()

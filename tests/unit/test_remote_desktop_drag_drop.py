"""
Tests for cross-device drag-and-drop.

Covers: drag_drop.py, file_transfer.py (send_files), input_handler.py (file_drop)
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class TestDragDropState(unittest.TestCase):
    """Tests for DragDropState enum."""

    def test_states_exist(self):
        from integrations.remote_desktop.drag_drop import DragDropState
        self.assertEqual(DragDropState.IDLE.value, 'idle')
        self.assertEqual(DragDropState.TRANSFERRING.value, 'transferring')
        self.assertEqual(DragDropState.COMPLETED.value, 'completed')
        self.assertEqual(DragDropState.ERROR.value, 'error')


class TestDragDropEvent(unittest.TestCase):
    """Tests for DragDropEvent dataclass."""

    def test_event_creation(self):
        from integrations.remote_desktop.drag_drop import DragDropEvent
        ev = DragDropEvent(
            direction='local_to_remote',
            file_paths=['/tmp/test.txt'],
            drop_x=100, drop_y=200,
        )
        self.assertEqual(ev.direction, 'local_to_remote')
        self.assertEqual(len(ev.file_paths), 1)
        self.assertEqual(ev.drop_x, 100)

    def test_event_to_dict(self):
        from integrations.remote_desktop.drag_drop import DragDropEvent
        ev = DragDropEvent(
            direction='remote_to_local',
            file_paths=['/tmp/a.txt', '/tmp/b.txt'],
            drop_x=50, drop_y=75,
        )
        d = ev.to_dict()
        self.assertEqual(d['direction'], 'remote_to_local')
        self.assertEqual(len(d['file_paths']), 2)


class TestDragDropBridge(unittest.TestCase):
    """Tests for DragDropBridge."""

    def _make_bridge(self):
        from integrations.remote_desktop.drag_drop import DragDropBridge
        transport = MagicMock()
        return DragDropBridge(transport=transport)

    def test_bridge_creation(self):
        bridge = self._make_bridge()
        self.assertIsNotNone(bridge)

    def test_initial_state_not_monitoring(self):
        bridge = self._make_bridge()
        self.assertFalse(bridge._monitoring)

    def test_handle_local_drop_no_files(self):
        bridge = self._make_bridge()
        result = bridge.handle_local_drop([], 100, 100)
        self.assertFalse(result.get('success', True))

    def test_handle_local_drop_file_not_found(self):
        bridge = self._make_bridge()
        result = bridge.handle_local_drop(
            ['/nonexistent/file.xyz'], 100, 100)
        self.assertFalse(result.get('success', True))

    def test_handle_local_drop_with_real_file(self):
        bridge = self._make_bridge()
        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as f:
            f.write(b'test data')
            path = f.name
        try:
            result = bridge.handle_local_drop([path], 200, 300)
            # May succeed or fail depending on transport mock
            self.assertIn('success', result)
        finally:
            os.unlink(path)

    def test_handle_remote_drop(self):
        bridge = self._make_bridge()
        event = {
            'direction': 'remote_to_local',
            'file_paths': ['/remote/file.txt'],
            'drop_x': 50, 'drop_y': 50,
        }
        result = bridge.handle_remote_drop(event)
        self.assertIsInstance(result, dict)

    def test_on_progress_callback(self):
        bridge = self._make_bridge()
        cb = MagicMock()
        bridge.on_progress(cb)
        self.assertIn(cb, bridge._progress_callbacks)

    def test_stop_monitoring(self):
        bridge = self._make_bridge()
        bridge.stop_monitoring()  # Should not raise


class TestFileTransferBatch(unittest.TestCase):
    """Tests for file_transfer.py send_files batch method."""

    def test_send_files_empty(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        transport = MagicMock()
        result = ft.send_files(transport, [])
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_send_files_nonexistent(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        transport = MagicMock()
        result = ft.send_files(transport, ['/nonexistent/file.xyz'])
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0].get('success', True))

    def test_send_files_real_file(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        transport = MagicMock()
        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as f:
            f.write(b'test content')
            path = f.name
        try:
            result = ft.send_files(transport, [path])
            self.assertEqual(len(result), 1)
        finally:
            os.unlink(path)


class TestInputHandlerFileDrop(unittest.TestCase):
    """Tests for input_handler.py file_drop handler."""

    def test_file_drop_handler_exists(self):
        from integrations.remote_desktop.input_handler import InputHandler
        handler = InputHandler()
        # Verify file_drop is a handled event type (via handle_input_event)
        self.assertTrue(hasattr(handler, '_handle_file_drop'))

    def test_file_drop_event(self):
        from integrations.remote_desktop.input_handler import InputHandler
        handler = InputHandler()
        event = {
            'type': 'file_drop',
            'x': 100, 'y': 200,
            'file_paths': ['/tmp/test.txt'],
        }
        result = handler.handle_input_event(event)
        self.assertIsInstance(result, dict)


class TestDragDropAgentTool(unittest.TestCase):
    """Tests for drag-drop related agent tools (indirectly via orchestrator)."""

    def test_orchestrator_has_no_drag_drop_crash(self):
        """Orchestrator shouldn't crash when drag-drop modules imported."""
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        status = orch.get_status()
        self.assertIn('started', status)


if __name__ == '__main__':
    unittest.main()

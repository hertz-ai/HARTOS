"""Tests for Remote Desktop Signaling, File Transfer, Host Service, and Viewer Client."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


# ═══════════════════════════════════════════════════════════════
# Signaling Tests
# ═══════════════════════════════════════════════════════════════

class TestSignalingMessage(unittest.TestCase):
    """Test SignalingMessage dataclass."""

    def test_create_message(self):
        from integrations.remote_desktop.signaling import SignalingMessage
        msg = SignalingMessage(
            msg_type='connect_request',
            sender_device_id='dev-1',
            target_device_id='dev-2',
            payload={'password': 'abc123'},
        )
        self.assertEqual(msg.msg_type, 'connect_request')
        self.assertEqual(msg.sender_device_id, 'dev-1')
        self.assertIn('password', msg.payload)

    def test_to_dict_roundtrip(self):
        from integrations.remote_desktop.signaling import SignalingMessage
        msg = SignalingMessage(
            msg_type='connect_accept',
            sender_device_id='dev-1',
            target_device_id='dev-2',
            payload={'session_id': 's1'},
        )
        d = msg.to_dict()
        msg2 = SignalingMessage.from_dict(d)
        self.assertEqual(msg.msg_type, msg2.msg_type)
        self.assertEqual(msg.sender_device_id, msg2.sender_device_id)
        self.assertEqual(msg.payload, msg2.payload)


class TestSignalingChannel(unittest.TestCase):
    """Test SignalingChannel (WAMP + HTTP fallback)."""

    def test_start_without_wamp(self):
        """Should start in HTTP fallback mode when WAMP unavailable."""
        from integrations.remote_desktop.signaling import SignalingChannel
        with patch.dict('sys.modules', {'crossbar_server': None}):
            channel = SignalingChannel('dev-1')
            result = channel.start()
            self.assertTrue(result)
            self.assertTrue(channel._connected)

    def test_on_signal_callback(self):
        from integrations.remote_desktop.signaling import SignalingChannel, SignalingMessage
        channel = SignalingChannel('dev-1')
        received = []
        channel.on_signal(lambda msg: received.append(msg))

        # Simulate incoming signal
        msg = SignalingMessage(
            msg_type='connect_request',
            sender_device_id='dev-2',
            target_device_id='dev-1',
        )
        import json
        channel._on_wamp_signal(json.dumps(msg.to_dict()))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].msg_type, 'connect_request')

    def test_pending_signals_when_no_callback(self):
        from integrations.remote_desktop.signaling import SignalingChannel, SignalingMessage
        channel = SignalingChannel('dev-1')
        # No callback registered
        import json
        msg = SignalingMessage(
            msg_type='bye',
            sender_device_id='dev-2',
            target_device_id='dev-1',
        )
        channel._on_wamp_signal(json.dumps(msg.to_dict()))
        pending = channel.get_pending()
        self.assertEqual(len(pending), 1)
        # Cleared after get_pending
        self.assertEqual(len(channel.get_pending()), 0)

    def test_close(self):
        from integrations.remote_desktop.signaling import SignalingChannel
        channel = SignalingChannel('dev-1')
        channel._connected = True
        channel.close()
        self.assertFalse(channel._connected)


class TestSignalingHelpers(unittest.TestCase):
    """Test convenience functions for creating signaling messages."""

    def test_create_connect_request(self):
        from integrations.remote_desktop.signaling import create_connect_request
        msg = create_connect_request('dev-1', 'dev-2', 'pw123', 'full_control')
        self.assertEqual(msg.msg_type, 'connect_request')
        self.assertEqual(msg.payload['password'], 'pw123')
        self.assertEqual(msg.payload['mode'], 'full_control')

    def test_create_connect_accept(self):
        from integrations.remote_desktop.signaling import create_connect_accept
        with patch('integrations.remote_desktop.transport.get_local_ip',
                   return_value='192.168.1.5'):
            msg = create_connect_accept('dev-1', 'dev-2', 'sess-1')
            self.assertEqual(msg.msg_type, 'connect_accept')
            self.assertEqual(msg.payload['session_id'], 'sess-1')
            self.assertEqual(
                msg.payload['transport_offers']['lan_ip'], '192.168.1.5')

    def test_create_connect_reject(self):
        from integrations.remote_desktop.signaling import create_connect_reject
        msg = create_connect_reject('dev-1', 'dev-2', 'wrong password')
        self.assertEqual(msg.msg_type, 'connect_reject')
        self.assertEqual(msg.payload['reason'], 'wrong password')

    def test_create_bye(self):
        from integrations.remote_desktop.signaling import create_bye
        msg = create_bye('dev-1', 'dev-2', 'sess-1')
        self.assertEqual(msg.msg_type, 'bye')
        self.assertEqual(msg.payload['session_id'], 'sess-1')


# ═══════════════════════════════════════════════════════════════
# File Transfer Tests
# ═══════════════════════════════════════════════════════════════

class TestFileTransfer(unittest.TestCase):
    """Test chunked file transfer."""

    def test_send_file_not_found(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        result = ft.send_file(MagicMock(), '/nonexistent/file.txt')
        self.assertFalse(result['success'])
        self.assertIn('not found', result['error'])

    def test_send_file_success(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        mock_transport = MagicMock()
        mock_transport.send_event.return_value = True
        mock_transport.send_frame.return_value = True

        with tempfile.NamedTemporaryFile(delete=False, suffix='.txt') as tmp:
            tmp.write(b'Hello, remote desktop!')
            tmp.flush()
            tmp_path = tmp.name

        try:
            with patch('integrations.remote_desktop.security.scan_file_transfer',
                       return_value=(True, 'ok')):
                result = ft.send_file(mock_transport, tmp_path)
                self.assertTrue(result['success'])
                self.assertEqual(result['filename'], os.path.basename(tmp_path))
                self.assertIn('sha256', result)
                self.assertGreater(result['bytes_sent'], 0)
        finally:
            os.unlink(tmp_path)

    def test_receive_file_round_trip(self):
        """Test FILE_START → chunks → FILE_END → verify."""
        import hashlib
        from integrations.remote_desktop.file_transfer import FileTransfer

        content = b'Test file content for remote desktop transfer'
        sha256 = hashlib.sha256(content).hexdigest()

        ft = FileTransfer()
        with tempfile.TemporaryDirectory() as tmpdir:
            ft.receive_file(tmpdir)

            # Simulate FILE_START
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'test.dat',
                'size': len(content),
                'sha256': sha256,
            })

            # Simulate chunk
            ft.handle_frame(content)

            # Simulate FILE_END
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': 'test.dat',
                'sha256': sha256,
            })

            self.assertIsNotNone(result)
            self.assertTrue(result['success'])
            self.assertEqual(result['sha256'], sha256)
            self.assertTrue(os.path.exists(result['path']))

    def test_receive_sha256_mismatch(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        with tempfile.TemporaryDirectory() as tmpdir:
            ft.receive_file(tmpdir)
            ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_START',
                'filename': 'bad.dat',
                'size': 5,
                'sha256': 'aaaa',
            })
            ft.handle_frame(b'hello')
            result = ft.handle_event({
                'type': 'file_ctrl',
                'action': 'FILE_END',
                'filename': 'bad.dat',
                'sha256': 'aaaa',
            })
            self.assertFalse(result['success'])
            self.assertIn('mismatch', result['error'])

    def test_progress_tracking(self):
        from integrations.remote_desktop.file_transfer import TransferProgress, FileTransferState
        p = TransferProgress(filename='test.bin', total_bytes=1000,
                             transferred_bytes=500,
                             state=FileTransferState.SENDING)
        self.assertAlmostEqual(p.percent, 50.0)
        d = p.to_dict()
        self.assertEqual(d['filename'], 'test.bin')
        self.assertEqual(d['percent'], 50.0)

    def test_progress_callback(self):
        from integrations.remote_desktop.file_transfer import FileTransfer
        ft = FileTransfer()
        progress_updates = []
        ft.on_progress(lambda p: progress_updates.append(p.percent))

        mock_transport = MagicMock()
        mock_transport.send_event.return_value = True
        mock_transport.send_frame.return_value = True

        with tempfile.NamedTemporaryFile(delete=False, suffix='.bin') as tmp:
            tmp.write(b'x' * 100)
            tmp.flush()
            tmp_path = tmp.name

        try:
            with patch('integrations.remote_desktop.security.scan_file_transfer',
                       return_value=(True, 'ok')):
                ft.send_file(mock_transport, tmp_path)
                self.assertGreater(len(progress_updates), 0)
        finally:
            os.unlink(tmp_path)


# ═══════════════════════════════════════════════════════════════
# Host Service Tests
# ═══════════════════════════════════════════════════════════════

class TestHostService(unittest.TestCase):
    """Test native host service."""

    @patch('integrations.remote_desktop.device_id.get_device_id', return_value='host-dev-1')
    @patch('integrations.remote_desktop.device_id.format_device_id', return_value='hos-tde-v1')
    @patch('integrations.remote_desktop.session_manager.get_session_manager')
    @patch('integrations.remote_desktop.frame_capture.FrameCapture')
    @patch('integrations.remote_desktop.input_handler.InputHandler')
    def test_start_host(self, mock_ih, mock_fc, mock_sm_fn, mock_fmt, mock_did):
        from integrations.remote_desktop.host_service import HostService
        host = HostService()

        mock_sm = MagicMock()
        mock_sm.generate_otp.return_value = 'pw1234'
        mock_sm_fn.return_value = mock_sm

        # Mock transport to avoid WebSocket dependency
        with patch('integrations.remote_desktop.transport.DirectWebSocketTransport') as mock_ws:
            mock_ws_inst = MagicMock()
            mock_ws_inst.start_server.return_value = 5678
            mock_ws.return_value = mock_ws_inst
            with patch.object(host, '_register_watchdog'):
                result = host.start()
                self.assertEqual(result['status'], 'hosting')
                self.assertEqual(result['device_id'], 'host-dev-1')
                self.assertEqual(result['password'], 'pw1234')
                self.assertTrue(host.is_running)
                host.stop()

    def test_singleton(self):
        import integrations.remote_desktop.host_service as hs_mod
        hs_mod._host_service = None
        h1 = hs_mod.get_host_service()
        h2 = hs_mod.get_host_service()
        self.assertIs(h1, h2)
        hs_mod._host_service = None

    def test_handle_viewer(self):
        from integrations.remote_desktop.host_service import HostService
        host = HostService()
        host.handle_viewer('sess-1', 'viewer-dev')
        self.assertIn('sess-1', host._viewers)
        host.remove_viewer('sess-1')
        self.assertNotIn('sess-1', host._viewers)


# ═══════════════════════════════════════════════════════════════
# Viewer Client Tests
# ═══════════════════════════════════════════════════════════════

class TestViewerClient(unittest.TestCase):
    """Test native viewer client."""

    def test_singleton(self):
        import integrations.remote_desktop.viewer_client as vc_mod
        vc_mod._viewer_client = None
        v1 = vc_mod.get_viewer_client()
        v2 = vc_mod.get_viewer_client()
        self.assertIs(v1, v2)
        vc_mod._viewer_client = None

    def test_send_without_connection(self):
        from integrations.remote_desktop.viewer_client import ViewerClient
        vc = ViewerClient()
        self.assertFalse(vc.send_mouse('click', 100, 200))
        self.assertFalse(vc.send_keyboard('key', 'a'))
        self.assertFalse(vc.send_text('hello'))
        self.assertFalse(vc.send_hotkey('ctrl+c'))

    def test_get_status_disconnected(self):
        from integrations.remote_desktop.viewer_client import ViewerClient
        vc = ViewerClient()
        status = vc.get_status()
        self.assertFalse(status['connected'])
        self.assertIsNone(status['session_id'])
        self.assertEqual(status['frames_received'], 0)

    def test_frame_callback(self):
        from integrations.remote_desktop.viewer_client import ViewerClient
        vc = ViewerClient()
        frames = []
        vc.on_frame(lambda data: frames.append(data))
        vc._on_frame_received(b'fake-jpeg-data')
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], b'fake-jpeg-data')
        self.assertEqual(vc._frame_count, 1)

    def test_file_transfer_not_connected(self):
        from integrations.remote_desktop.viewer_client import ViewerClient
        vc = ViewerClient()
        result = vc.transfer_file('/some/file.txt')
        self.assertFalse(result['success'])


# ═══════════════════════════════════════════════════════════════
# Engine Selector Enhancement Tests
# ═══════════════════════════════════════════════════════════════

class TestRecommendEngineSwitch(unittest.TestCase):
    """Test AI-native engine switch recommendations."""

    def test_recommend_rustdesk_for_file_transfer(self):
        from integrations.remote_desktop.engine_selector import recommend_engine_switch
        with patch('integrations.remote_desktop.engine_selector._detect_engines',
                   return_value={'rustdesk': True, 'moonlight': True, 'native': True}):
            result = recommend_engine_switch('moonlight',
                                             context={'mode': 'file_transfer'})
            self.assertIsNotNone(result)
            self.assertEqual(result['recommend'], 'rustdesk')

    def test_recommend_moonlight_for_gaming(self):
        from integrations.remote_desktop.engine_selector import recommend_engine_switch
        with patch('integrations.remote_desktop.engine_selector._detect_engines',
                   return_value={'rustdesk': True, 'moonlight': True, 'native': True}):
            result = recommend_engine_switch('rustdesk',
                                             context={'use_case': 'gaming'})
            self.assertIsNotNone(result)
            self.assertEqual(result['recommend'], 'moonlight')

    def test_no_recommendation_when_optimal(self):
        from integrations.remote_desktop.engine_selector import recommend_engine_switch
        with patch('integrations.remote_desktop.engine_selector._detect_engines',
                   return_value={'rustdesk': True, 'native': True}):
            result = recommend_engine_switch('rustdesk',
                                             context={'mode': 'full_control'})
            self.assertIsNone(result)

    def test_recommend_upgrade_from_native(self):
        from integrations.remote_desktop.engine_selector import recommend_engine_switch
        with patch('integrations.remote_desktop.engine_selector._detect_engines',
                   return_value={'rustdesk': True, 'native': True}):
            result = recommend_engine_switch('native')
            self.assertIsNotNone(result)
            self.assertEqual(result['recommend'], 'rustdesk')


if __name__ == '__main__':
    unittest.main()

"""
Tests for DLNA bridge (screen casting to smart TVs).

Covers: dlna_bridge.py (DLNARenderer, DLNABridge, MJPEGStreamServer)
"""

import socket
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


class TestDLNARenderer(unittest.TestCase):
    """Tests for DLNARenderer dataclass."""

    def test_renderer_creation(self):
        from integrations.remote_desktop.dlna_bridge import DLNARenderer
        r = DLNARenderer(
            device_id='uuid:12345',
            friendly_name='Living Room TV',
            ip='192.168.1.100',
            port=8080,
        )
        self.assertEqual(r.device_id, 'uuid:12345')
        self.assertEqual(r.friendly_name, 'Living Room TV')
        self.assertTrue(r.supports_video)

    def test_renderer_to_dict(self):
        from integrations.remote_desktop.dlna_bridge import DLNARenderer
        r = DLNARenderer(
            device_id='uuid:abc',
            friendly_name='Bedroom TV',
            ip='10.0.0.5',
            port=1234,
            manufacturer='Samsung',
            model='Smart TV 2024',
        )
        d = r.to_dict()
        self.assertEqual(d['device_id'], 'uuid:abc')
        self.assertEqual(d['friendly_name'], 'Bedroom TV')
        self.assertEqual(d['manufacturer'], 'Samsung')

    def test_renderer_defaults(self):
        from integrations.remote_desktop.dlna_bridge import DLNARenderer
        r = DLNARenderer(
            device_id='test', friendly_name='Test',
            ip='127.0.0.1', port=80,
        )
        self.assertTrue(r.supports_video)
        self.assertTrue(r.supports_audio)
        self.assertEqual(r.manufacturer, '')


class TestCastSession(unittest.TestCase):
    """Tests for CastSession dataclass."""

    def test_cast_session_creation(self):
        from integrations.remote_desktop.dlna_bridge import (
            CastSession, DLNARenderer,
        )
        renderer = DLNARenderer(
            device_id='uuid:tv1', friendly_name='TV',
            ip='192.168.1.10', port=8080,
        )
        session = CastSession(
            cast_session_id='cast-001',
            renderer=renderer,
            source_session_id='session-abc',
            stream_url='http://192.168.1.5:8554/stream.mjpeg',
            started_at=time.time(),
        )
        self.assertEqual(session.cast_session_id, 'cast-001')
        self.assertTrue(session.active)


class TestMJPEGStreamServer(unittest.TestCase):
    """Tests for MJPEGStreamServer."""

    def test_server_creation(self):
        from integrations.remote_desktop.dlna_bridge import MJPEGStreamServer
        server = MJPEGStreamServer()
        self.assertIsNotNone(server)
        self.assertFalse(server.is_running)

    def test_start_and_stop(self):
        from integrations.remote_desktop.dlna_bridge import MJPEGStreamServer
        server = MJPEGStreamServer()

        # Frame source that returns a tiny JPEG-like blob
        def frame_source():
            return b'\xff\xd8\xff\xe0' + b'\x00' * 100

        url = server.start(frame_source, port=0)
        self.assertTrue(url.startswith('http://'))
        self.assertIn('/stream.mjpeg', url)
        self.assertTrue(server.is_running)

        server.stop()
        self.assertFalse(server.is_running)

    def test_start_auto_port(self):
        from integrations.remote_desktop.dlna_bridge import MJPEGStreamServer
        server = MJPEGStreamServer()
        url = server.start(lambda: None, port=0)
        self.assertIn('stream.mjpeg', url)
        server.stop()

    def test_get_local_ip(self):
        from integrations.remote_desktop.dlna_bridge import MJPEGStreamServer
        server = MJPEGStreamServer()
        ip = server._get_local_ip()
        self.assertIsInstance(ip, str)
        # Should be a valid IP
        parts = ip.split('.')
        self.assertEqual(len(parts), 4)


class TestDLNABridge(unittest.TestCase):
    """Tests for DLNABridge."""

    def test_bridge_creation(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        self.assertIsNotNone(bridge)

    def test_discover_renderers_returns_list(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        # Short timeout since no actual DLNA devices on test network
        result = bridge.discover_renderers(timeout=0.5)
        self.assertIsInstance(result, list)

    def test_get_cached_renderers_empty(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        cached = bridge.get_cached_renderers()
        self.assertIsInstance(cached, list)

    def test_cast_session_renderer_not_found(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        result = bridge.cast_session('session-1', 'nonexistent-renderer')
        self.assertFalse(result['success'])
        self.assertIn('not found', result['error'])

    def test_stop_cast_nonexistent(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        result = bridge.stop_cast('nonexistent-cast')
        self.assertFalse(result)

    def test_stop_all_empty(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        bridge.stop_all()  # Should not raise

    def test_get_cast_status_empty(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        status = bridge.get_cast_status()
        self.assertIsInstance(status, list)
        self.assertEqual(len(status), 0)

    def test_parse_header(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        response = (
            'HTTP/1.1 200 OK\r\n'
            'LOCATION: http://192.168.1.10:8080/desc.xml\r\n'
            'ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n'
            '\r\n'
        )
        loc = bridge._parse_header(response, 'LOCATION')
        self.assertEqual(loc, 'http://192.168.1.10:8080/desc.xml')

    def test_parse_header_not_found(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        result = bridge._parse_header('HTTP/1.1 200 OK\r\n', 'MISSING')
        self.assertIsNone(result)

    def test_soap_post_invalid_url(self):
        from integrations.remote_desktop.dlna_bridge import DLNABridge
        bridge = DLNABridge()
        result = bridge._soap_post(
            'http://invalid-host-xyz:9999/control',
            'urn:test#Action',
            '<xml/>',
        )
        self.assertFalse(result)

    def test_singleton(self):
        import integrations.remote_desktop.dlna_bridge as dlna_mod
        dlna_mod._dlna_bridge = None
        b1 = dlna_mod.get_dlna_bridge()
        b2 = dlna_mod.get_dlna_bridge()
        self.assertIs(b1, b2)
        dlna_mod._dlna_bridge = None  # cleanup


class TestDLNAOrchestratorIntegration(unittest.TestCase):
    """Tests for DLNA methods on orchestrator."""

    def test_discover_cast_targets(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.discover_cast_targets(timeout=0.5)
        self.assertIsInstance(result, list)

    def test_stop_cast_nonexistent(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.stop_cast('fake-cast-id')
        self.assertFalse(result)

    def test_get_cast_status_empty(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        result = orch.get_cast_status()
        self.assertIsInstance(result, list)

    def test_get_status_includes_casts(self):
        from integrations.remote_desktop.orchestrator import RemoteDesktopOrchestrator
        orch = RemoteDesktopOrchestrator()
        status = orch.get_status()
        self.assertIn('casts', status)
        self.assertIn('cast_count', status)


class TestDLNAAgentTools(unittest.TestCase):
    """Tests for DLNA-related agent tools."""

    def test_agent_tools_include_cast(self):
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools,
        )
        tools = build_remote_desktop_tools({'user_id': 'test'})
        tool_names = [t[0] for t in tools]
        self.assertIn('discover_cast_targets', tool_names)
        self.assertIn('cast_to_tv', tool_names)

    def test_agent_tools_include_peripherals(self):
        from integrations.remote_desktop.agent_tools import (
            build_remote_desktop_tools,
        )
        tools = build_remote_desktop_tools({'user_id': 'test'})
        tool_names = [t[0] for t in tools]
        self.assertIn('list_peripherals', tool_names)
        self.assertIn('forward_peripheral', tool_names)


if __name__ == '__main__':
    unittest.main()

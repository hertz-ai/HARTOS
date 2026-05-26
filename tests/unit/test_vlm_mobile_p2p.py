"""Tests for Phases 8, 9, 10 of memory/vlm_best_of_all_worlds_plan.md

  Phase 8: Android client surface (integrations.vlm.mobile)
  Phase 9: iOS unsupported envelope
  Phase 10: P2P inference resolver (Qwen3VLBackend.dispatch_inference)
"""
import os
import unittest
from unittest.mock import patch, MagicMock


class TestPlatformDetection(unittest.TestCase):
    """_detect_mobile_platform identifies android / ios / desktop."""

    def test_force_android_via_env(self):
        from integrations.vlm.mobile import _detect_mobile_platform
        with patch.dict(os.environ, {'HEVOLVE_FORCE_PLATFORM': 'android'}):
            self.assertEqual(_detect_mobile_platform(), 'android')

    def test_force_ios_via_env(self):
        from integrations.vlm.mobile import _detect_mobile_platform
        with patch.dict(os.environ, {'HEVOLVE_FORCE_PLATFORM': 'ios'}):
            self.assertEqual(_detect_mobile_platform(), 'ios')

    def test_desktop_default(self):
        from integrations.vlm.mobile import _detect_mobile_platform
        # No ANDROID_ARGUMENT, no iP machine, no force → desktop ('').
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_FORCE_PLATFORM', None)
            os.environ.pop('ANDROID_ARGUMENT', None)
            with patch('integrations.vlm.mobile.platform.machine',
                       return_value='AMD64'), \
                 patch('integrations.vlm.mobile.sys.platform', 'win32'):
                self.assertEqual(_detect_mobile_platform(), '')


class TestIosStubs(unittest.TestCase):
    """Phase 9: iOS sandbox forbids cross-app capture/dispatch.
    Functions return platform_unsupported envelope instead of raising."""

    def setUp(self):
        self._orig_force = os.environ.get('HEVOLVE_FORCE_PLATFORM')
        os.environ['HEVOLVE_FORCE_PLATFORM'] = 'ios'

    def tearDown(self):
        if self._orig_force is None:
            os.environ.pop('HEVOLVE_FORCE_PLATFORM', None)
        else:
            os.environ['HEVOLVE_FORCE_PLATFORM'] = self._orig_force

    def test_list_windows_returns_unsupported_marker(self):
        from integrations.vlm.mobile import list_android_windows
        result = list_android_windows()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['platform'], 'ios')
        self.assertIn('sandbox', result[0]['reason'].lower())

    def test_capture_returns_none(self):
        from integrations.vlm.mobile import capture_android_window
        self.assertIsNone(capture_android_window('any-id'))

    def test_get_node_tree_returns_unsupported(self):
        from integrations.vlm.mobile import get_android_node_tree
        result = get_android_node_tree()
        self.assertEqual(result['platform'], 'ios')

    def test_dispatch_action_returns_unsupported(self):
        from integrations.vlm.mobile import dispatch_android_action
        result = dispatch_android_action({'action': 'left_click'})
        self.assertEqual(result['platform'], 'ios')


class TestAndroidClient(unittest.TestCase):
    """Phase 8: Android client uses peer_dispatch when supplied,
    falls back to local UNIX socket on the device, returns empty
    when no companion is reachable."""

    def test_no_peer_dispatch_on_desktop_returns_empty(self):
        """Desktop → no Android socket, no peer_dispatch → empty list."""
        from integrations.vlm.mobile import list_android_windows
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('HEVOLVE_FORCE_PLATFORM', None)
            self.assertEqual(list_android_windows(), [])

    def test_peer_dispatch_called_with_compute_channel(self):
        """When peer_dispatch is supplied, it gets called with
        channel='compute' + the wire-protocol payload."""
        from integrations.vlm.mobile import list_android_windows
        peer = MagicMock(return_value={
            'status': 'ok',
            'data': {'windows': [
                {'window_id': 'a', 'package': 'com.spotify.music'},
                {'window_id': 'b', 'package': 'com.android.chrome'},
            ]},
        })
        result = list_android_windows(peer_dispatch=peer)
        peer.assert_called_once()
        args, kwargs = peer.call_args
        self.assertEqual(args[0], 'compute')
        payload = args[1]
        self.assertEqual(payload['type'], 'android_list_windows')
        self.assertIn('request_id', payload)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['package'], 'com.spotify.music')

    def test_peer_dispatch_failure_returns_empty(self):
        from integrations.vlm.mobile import list_android_windows
        peer = MagicMock(side_effect=Exception('peer down'))
        self.assertEqual(list_android_windows(peer_dispatch=peer), [])

    def test_capture_decodes_base64_response(self):
        from integrations.vlm.mobile import capture_android_window
        import base64
        peer = MagicMock(return_value={
            'status': 'ok',
            'data': {'jpeg_base64':
                     base64.b64encode(b'fake-jpeg').decode('ascii')},
        })
        result = capture_android_window('w1', peer_dispatch=peer)
        self.assertEqual(result, b'fake-jpeg')

    def test_dispatch_action_payload_shape(self):
        from integrations.vlm.mobile import dispatch_android_action
        peer = MagicMock(return_value={'status': 'ok'})
        action = {'action': 'left_click', 'coordinate': [100, 200]}
        dispatch_android_action(action, peer_dispatch=peer)
        payload = peer.call_args[0][1]
        self.assertEqual(payload['type'], 'android_dispatch_action')
        self.assertEqual(payload['action'], action)


class TestDispatchInference(unittest.TestCase):
    """Phase 10: Qwen3VLBackend.dispatch_inference picks tier based
    on intelligence_preference + reachability."""

    def setUp(self):
        from integrations.vlm.qwen3vl_backend import Qwen3VLBackend
        self.backend = Qwen3VLBackend()

    def test_local_only_uses_local_when_available(self):
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=True), \
             patch.object(self.backend, '_dispatch_local',
                          return_value={'action': 'left_click'}) as m_local:
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act',
                 'screenshot_b64': 'x', 'task': 'click X'},
                intelligence_preference='local_only')
        m_local.assert_called_once()
        self.assertEqual(result['tier'], 'local')

    def test_local_only_no_route_when_local_down(self):
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=False):
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act',
                 'screenshot_b64': 'x', 'task': 'click X'},
                intelligence_preference='local_only')
        self.assertEqual(result['tier'], 'no_route')

    def test_hybrid_falls_back_to_peer_when_local_down(self):
        peer = MagicMock(return_value={
            'type': 'vlm_grounding_result',
            'action': 'left_click', 'screen_x': 100, 'screen_y': 200,
        })
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=False):
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act',
                 'screenshot_b64': 'x', 'task': 'click X'},
                peer_dispatch=peer,
                intelligence_preference='hybrid')
        self.assertEqual(result['tier'], 'paired_peer')
        # Verify it was the compute channel (per plan §10).
        peer.assert_called()
        self.assertEqual(peer.call_args[0][0], 'compute')

    def test_hive_preference_uses_paired_peer_first(self):
        peer = MagicMock(return_value={
            'type': 'vlm_grounding_result', 'action': 'left_click',
        })
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=True):
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act',
                 'screenshot_b64': 'x', 'task': 'click X'},
                peer_dispatch=peer,
                intelligence_preference='hive')
        self.assertEqual(result['tier'], 'paired_peer')

    def test_no_peer_no_local_no_cloud_returns_no_route(self):
        # cloud tier needs WorldModelBridge import - block it so the
        # tier returns None and we fall through to no_route.
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=False), \
             patch.object(self.backend, '_dispatch_cloud',
                          return_value=None):
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act', 'screenshot_b64': 'x',
                 'task': 'click X'},
                intelligence_preference='hybrid')
        self.assertEqual(result['tier'], 'no_route')

    def test_hybrid_includes_cloud_when_local_available(self):
        """Reviewer fix: 'hybrid' must enumerate all 4 tiers, not
        exclude cloud when local is reachable.  Verifies the tier
        list includes 'cloud' as a final fallback."""
        peer = MagicMock(side_effect=Exception('peer down'))
        with patch.object(self.backend, '_is_local_vlm_available',
                          return_value=True), \
             patch.object(self.backend, '_dispatch_local',
                          side_effect=Exception('local crashed')), \
             patch.object(self.backend, '_dispatch_cloud',
                          return_value={'action': 'left_click',
                                        'reasoning': 'cloud win'}) as m_cloud:
            result = self.backend.dispatch_inference(
                {'method': 'point_and_act', 'screenshot_b64': 'x',
                 'task': 'click X'},
                peer_dispatch=peer,
                intelligence_preference='hybrid')
        m_cloud.assert_called_once()
        self.assertEqual(result['tier'], 'cloud')


if __name__ == '__main__':
    unittest.main()

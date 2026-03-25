"""
Tests for WorldModelBridge embodied interaction extensions +
action_model + control_loop.

These test the agentic orchestration layer's routing to HevolveAI,
NOT HevolveAI's native intelligence (sensor fusion, PID, SLAM).

Covers:
  - WorldModelBridge.send_action() — safety check + routing
  - WorldModelBridge.ingest_sensor_batch() — batch routing
  - WorldModelBridge.get_learning_feedback() — feedback polling
  - WorldModelBridge.record_embodied_interaction() — experience recording
  - WorldModelBridge.emergency_stop() — bridge-level E-stop
  - RobotAction data model
  - ControlLoopBridge timing
"""
import os
import time
import threading
import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_env():
    os.environ.pop('HEVOLVE_HALTED', None)
    os.environ.pop('HEVOLVE_HALT_REASON', None)
    yield
    os.environ.pop('HEVOLVE_HALTED', None)
    os.environ.pop('HEVOLVE_HALT_REASON', None)


@pytest.fixture
def bridge():
    """Fresh WorldModelBridge for each test (not singleton)."""
    from integrations.agent_engine.world_model_bridge import WorldModelBridge
    b = WorldModelBridge()
    b._in_process = False  # Force HTTP mode for testing
    b._http_disabled = False  # Allow HTTP calls in tests
    return b


# ── RobotAction Tests ───────────────────────────────────────────

class TestRobotAction:
    def test_create_motor_action(self):
        from integrations.robotics.action_model import RobotAction
        a = RobotAction(
            action_type='motor_velocity', target='left_wheel',
            params={'velocity': 0.5},
        )
        assert a.action_type == 'motor_velocity'
        assert a.target == 'left_wheel'
        assert a.params['velocity'] == 0.5

    def test_to_dict(self):
        from integrations.robotics.action_model import RobotAction
        a = RobotAction(
            action_type='servo_position', target='pan_servo',
            params={'angle_deg': 90}, priority=5,
        )
        d = a.to_dict()
        assert d['type'] == 'servo_position'
        assert d['priority'] == 5

    def test_from_dict(self):
        from integrations.robotics.action_model import RobotAction
        d = {'type': 'gpio_output', 'target': 'led_0',
             'params': {'value': 'on'}, 'source': 'recipe'}
        a = RobotAction.from_dict(d)
        assert a.action_type == 'gpio_output'
        assert a.source == 'recipe'

    def test_emergency_stop_action(self):
        from integrations.robotics.action_model import RobotAction
        a = RobotAction.emergency_stop_action()
        assert a.action_type == 'emergency_stop'
        assert a.target == '*'
        assert a.priority == 999
        assert a.source == 'safety'


# ── WorldModelBridge.send_action() Tests ─────────────────────────

class TestSendAction:
    def test_send_action_blocked_by_estop(self, bridge):
        from integrations.robotics.safety_monitor import SafetyMonitor
        monitor = SafetyMonitor()
        monitor.trigger_estop('test block', source='test')

        # Patch get_safety_monitor to return our estopped monitor
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=monitor):
            result = bridge.send_action({'type': 'motor', 'target': 'wheel', 'params': {}})
        # Since monitor.is_estopped is True, send_action returns False
        assert result is False

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_send_action_http_success(self, mock_post, bridge):
        mock_post.return_value = MagicMock(status_code=200)
        os.environ.pop('HEVOLVE_HALTED', None)

        # Mock safety monitor to return safe
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        mock_monitor.check_position_safe.return_value = True
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = bridge.send_action({'type': 'motor', 'target': 'wheel',
                                          'params': {'velocity': 0.5}})
        assert result is True
        assert bridge._stats.get('total_actions_sent', 0) >= 1

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_send_action_http_failure(self, mock_post, bridge):
        mock_post.return_value = MagicMock(status_code=500)
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = bridge.send_action({'type': 'motor', 'target': 'w', 'params': {}})
        assert result is False

    def test_send_action_circuit_breaker_open(self, bridge):
        bridge._circuit_breaker._failures = bridge._circuit_breaker.threshold
        bridge._circuit_breaker._opened_at = time.time()
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = bridge.send_action({'type': 'motor', 'target': 'w', 'params': {}})
        assert result is False


# ── WorldModelBridge.ingest_sensor_batch() Tests ─────────────────

class TestIngestSensorBatch:
    def test_empty_batch(self, bridge):
        result = bridge.ingest_sensor_batch([])
        assert result == 0

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_batch_success(self, mock_post, bridge):
        mock_post.return_value = MagicMock(status_code=200)
        readings = [
            {'sensor_id': 'imu_0', 'sensor_type': 'imu', 'data': {'accel_x': 1.0}},
            {'sensor_id': 'gps_0', 'sensor_type': 'gps', 'data': {'latitude': 37.0, 'longitude': -122.0}},
        ]
        result = bridge.ingest_sensor_batch(readings)
        assert result == 2
        assert bridge._stats.get('total_sensor_readings', 0) >= 2

    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_batch_failure(self, mock_post, bridge):
        mock_post.return_value = MagicMock(status_code=500)
        result = bridge.ingest_sensor_batch([{'sensor_id': 'x', 'data': {}}])
        assert result == 0

    def test_circuit_breaker_blocks_batch(self, bridge):
        bridge._circuit_breaker._failures = bridge._circuit_breaker.threshold
        bridge._circuit_breaker._opened_at = time.time()
        result = bridge.ingest_sensor_batch([{'sensor_id': 'x', 'data': {}}])
        assert result == 0


# ── WorldModelBridge.get_learning_feedback() Tests ───────────────

class TestGetLearningFeedback:
    @patch('integrations.agent_engine.world_model_bridge.pooled_get')
    def test_http_feedback_success(self, mock_get, bridge):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'correction': 'adjust trajectory left 0.1m'},
        )
        result = bridge.get_learning_feedback()
        assert result is not None
        assert 'correction' in result

    @patch('integrations.agent_engine.world_model_bridge.pooled_get')
    def test_http_feedback_failure(self, mock_get, bridge):
        mock_get.return_value = MagicMock(status_code=500)
        result = bridge.get_learning_feedback()
        assert result is None

    def test_circuit_breaker_blocks_feedback(self, bridge):
        bridge._circuit_breaker._failures = bridge._circuit_breaker.threshold
        bridge._circuit_breaker._opened_at = time.time()
        result = bridge.get_learning_feedback()
        assert result is None


# ── WorldModelBridge.record_embodied_interaction() Tests ────────

class TestRecordEmbodiedInteraction:
    def test_records_to_experience_queue(self, bridge):
        initial_recorded = bridge._stats.get('total_recorded', 0)
        bridge.record_embodied_interaction(
            action={'type': 'motor', 'target': 'wheel', 'params': {'velocity': 0.5}},
            sensor_context={'imu': {'accel_x': 0.1}},
            outcome={'reached_target': True, 'distance_error': 0.02},
        )
        assert len(bridge._experience_queue) >= 1
        latest = bridge._experience_queue[-1]
        assert latest['type'] == 'embodied_interaction'
        assert latest['action']['type'] == 'motor'
        assert bridge._stats['total_recorded'] == initial_recorded + 1


# ── WorldModelBridge.emergency_stop() Tests ─────────────────────

class TestEmergencyStop:
    @patch('integrations.agent_engine.world_model_bridge.pooled_post')
    def test_http_estop(self, mock_post, bridge):
        mock_post.return_value = MagicMock(status_code=200)
        result = bridge.emergency_stop()
        assert result is True
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert '/v1/actions/estop' in args[0][0]


# ── ControlLoopBridge Tests ─────────────────────────────────────

class TestControlLoopBridge:
    def test_register_and_start(self):
        from integrations.robotics.control_loop import ControlLoopBridge
        loop = ControlLoopBridge()
        call_count = {'n': 0}

        def callback():
            call_count['n'] += 1

        loop.register_callback('test', callback, hz=100)
        loop.start()
        time.sleep(0.15)  # ~15 calls at 100Hz
        loop.stop()
        assert call_count['n'] >= 5  # Should get at least 5 calls

    def test_stats_tracking(self):
        from integrations.robotics.control_loop import ControlLoopBridge
        loop = ControlLoopBridge()
        loop.register_callback('stat_test', lambda: None, hz=100)
        loop.start()
        time.sleep(0.1)
        loop.stop()
        stats = loop.get_stats()
        assert 'stat_test' in stats
        assert stats['stat_test']['calls'] >= 1
        assert stats['stat_test']['target_hz'] == 100

    def test_unregister_callback(self):
        from integrations.robotics.control_loop import ControlLoopBridge
        loop = ControlLoopBridge()
        loop.register_callback('temp', lambda: None, hz=10)
        loop.unregister_callback('temp')
        stats = loop.get_stats()
        # After unregister, stats may still exist but callback is removed
        assert 'temp' not in loop._callbacks

    def test_multiple_callbacks(self):
        from integrations.robotics.control_loop import ControlLoopBridge
        loop = ControlLoopBridge()
        counts = {'a': 0, 'b': 0}

        def inc_a():
            counts['a'] += 1

        def inc_b():
            counts['b'] += 1

        loop.register_callback('a', inc_a, hz=50)
        loop.register_callback('b', inc_b, hz=100)
        loop.start()
        time.sleep(0.12)
        loop.stop()
        assert counts['a'] >= 2
        assert counts['b'] >= 2


# ── Integration: Action → Safety → Bridge ───────────────────────

class TestActionSafetyIntegration:
    def test_action_model_to_bridge_dict(self):
        """RobotAction.to_dict() produces valid input for send_action()."""
        from integrations.robotics.action_model import RobotAction
        a = RobotAction(
            action_type='navigate_to', target='waypoint_1',
            params={'x': 0.5, 'y': 0.3, 'z': 0.0},
        )
        d = a.to_dict()
        assert 'type' in d
        assert 'params' in d
        assert d['params']['x'] == 0.5


# ── Boundary: No ML training primitives in robotics ──────────────

class TestNoMLTrainingCode:
    """This repo is the agentic orchestration layer.

    Tensors, gradients, weights, layers, pytorch belong in HevolveAI
    (the native intelligence repo), NEVER here.  These are training
    primitives.  This repo routes data — it does not train models.
    """

    # Terms that must NEVER appear in robotics source code
    FORBIDDEN = [
        'tensor', 'gradient', 'weight', 'layer',
        'pytorch', 'import torch', 'from torch',
    ]

    # Strings that contain a forbidden word but are legitimate
    ALLOWED_CONTEXTS = [
        # "layer" inside "orchestration layer", "agentic layer", etc. in comments/docstrings
        'agentic layer', 'orchestration layer', 'native layer',
        'embodiment layer', 'learning layer', 'safety layer',
        'security layer', 'routing layer',
        # "gradient" in module name reference (federated_gradient_protocol)
        'gradient_protocol', 'gradient_sync', 'gradient_service',
        'gradient_tools', 'embedding_delta',
        # "weight" in "log-scale weighting", "aggregation weights" (federation docs)
        'weighting',
    ]

    def _scan_source(self, source: str) -> list:
        """Return list of (line_no, line, term) for forbidden terms."""
        hits = []
        for i, line in enumerate(source.splitlines(), 1):
            low = line.lower().strip()
            # Skip empty / comment-only lines that are just docstrings
            for term in self.FORBIDDEN:
                if term in low:
                    # Check allowed contexts
                    if any(ctx in low for ctx in self.ALLOWED_CONTEXTS):
                        continue
                    hits.append((i, line.strip(), term))
        return hits

    def test_robotics_package_no_ml(self):
        """integrations/robotics/ must not contain ML training primitives."""
        import importlib, inspect
        import integrations.robotics.action_model as m1
        import integrations.robotics.control_loop as m2
        import integrations.robotics.safety_monitor as m3
        import integrations.robotics.sensor_model as m4
        import integrations.robotics.sensor_store as m5
        import integrations.robotics.capability_advertiser as m6
        import integrations.robotics.robot_tools as m7
        import integrations.robotics.recipe_adapter as m8
        import integrations.robotics.robot_boot as m9

        for mod in [m1, m2, m3, m4, m5, m6, m7, m8, m9]:
            source = inspect.getsource(mod)
            hits = self._scan_source(source)
            assert hits == [], (
                f"{mod.__name__} contains ML training primitives:\n"
                + '\n'.join(f"  line {n}: '{line}' (matched: {t})"
                            for n, line, t in hits)
            )

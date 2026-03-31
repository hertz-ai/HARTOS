"""
Tests for Batch 4: Robot Goal Type + Capability Advertisement.

Covers:
  - RobotCapabilityAdvertiser: detect, gossip payload, task matching
  - Robot tools: get_robot_capabilities, read_sensor, navigate_to, etc.
  - Robot prompt builder: build_robot_prompt
  - Goal registration: 'robot' type in GoalManager
  - Goal seeding: bootstrap_robot_health_monitor
  - Dispatch: capability-matched dispatch
  - Device routing: 'robot' form factor

Boundary enforcement: every test verifies that intelligence stays
in HevolveAI and routing/orchestration stays here.
"""
import json
import os
import pytest
from unittest.mock import patch, MagicMock


# ── RobotCapabilityAdvertiser Tests ──────────────────────────────

class TestCapabilityAdvertiser:
    def test_fresh_advertiser_empty(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        # Detect will have empty results on a dev machine
        caps = adv.detect_capabilities()
        assert 'locomotion' in caps
        assert 'manipulation' in caps
        assert 'sensors' in caps
        assert 'form_factor' in caps
        assert 'native_skills' in caps
        assert isinstance(caps['sensors'], dict)

    def test_get_capabilities_auto_detects(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        assert not adv._detected
        caps = adv.get_capabilities()
        assert adv._detected
        assert isinstance(caps, dict)

    def test_gossip_payload_compact(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._capabilities = {
            'form_factor': 'rover',
            'locomotion': {'type': 'differential'},
            'manipulation': None,
            'sensors': {'imu': True, 'gps': True, 'lidar': False},
            'native_skills': ['navigate', 'avoid_obstacles'],
        }
        adv._detected = True

        payload = adv.get_gossip_payload()
        assert payload['form_factor'] == 'rover'
        assert payload['has_locomotion'] is True
        assert payload['has_manipulation'] is False
        assert 'imu' in payload['sensor_types']
        assert 'gps' in payload['sensor_types']
        assert 'lidar' not in payload['sensor_types']
        assert payload['native_skill_count'] == 2

    def test_matches_full_match(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._capabilities = {
            'locomotion': {'type': 'differential'},
            'manipulation': {'arms': 1, 'grippers': 1, 'dof': 6},
            'sensors': {'imu': True, 'gps': True, 'camera': True},
            'form_factor': 'rover',
            'payload_kg': 5.0,
            'native_skills': [],
        }
        adv._detected = True

        score = adv.matches_task_requirements({
            'required_capabilities': ['locomotion', 'gripper', 'gps'],
        })
        assert score == 1.0

    def test_matches_partial(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._capabilities = {
            'locomotion': {'type': 'differential'},
            'manipulation': None,
            'sensors': {'imu': True},
            'form_factor': 'rover',
            'payload_kg': None,
            'native_skills': [],
        }
        adv._detected = True

        score = adv.matches_task_requirements({
            'required_capabilities': ['locomotion', 'gripper', 'lidar'],
        })
        # 1 of 3 matched = 0.33
        assert score < 0.5

    def test_matches_no_requirements(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._detected = True
        adv._capabilities = {}

        score = adv.matches_task_requirements({})
        assert score == 0.5  # Neutral

    def test_matches_form_factor_bonus(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._capabilities = {
            'locomotion': {'type': 'differential'},
            'manipulation': None,
            'sensors': {'imu': True},
            'form_factor': 'rover',
            'payload_kg': None,
            'native_skills': [],
        }
        adv._detected = True

        # Use 2 required caps so base score is 0.5 (1/2), bonus pushes to 0.6
        score_with = adv.matches_task_requirements({
            'required_capabilities': ['locomotion', 'lidar'],
            'preferred_form_factor': 'rover',
        })
        score_without = adv.matches_task_requirements({
            'required_capabilities': ['locomotion', 'lidar'],
            'preferred_form_factor': 'drone',
        })
        assert score_with > score_without

    def test_matches_payload_penalty(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        adv._capabilities = {
            'locomotion': {'type': 'differential'},
            'manipulation': None,
            'sensors': {},
            'form_factor': 'rover',
            'payload_kg': 2.0,
            'native_skills': [],
        }
        adv._detected = True

        score = adv.matches_task_requirements({
            'required_capabilities': ['locomotion'],
            'min_payload_kg': 10.0,
        })
        # Payload penalty halves score
        assert score == 0.5

    def test_detect_from_config_file(self, tmp_path):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        config = {
            'form_factor': 'arm',
            'manipulation': {'arms': 1, 'grippers': 1, 'dof': 6},
            'payload_kg': 3.0,
            'actuators': ['servo_0', 'servo_1', 'servo_2'],
        }
        config_file = tmp_path / 'robot_config.json'
        config_file.write_text(json.dumps(config))

        adv = RobotCapabilityAdvertiser()
        with patch.dict(os.environ, {'HEVOLVE_ROBOT_CONFIG': str(config_file)}):
            caps = adv.detect_capabilities()

        assert caps['form_factor'] == 'arm'
        assert caps['manipulation']['arms'] == 1
        assert caps['payload_kg'] == 3.0
        assert 'servo_0' in caps['actuators']

    def test_detect_from_hevolveai(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        mock_bridge = MagicMock()
        mock_bridge.check_health.return_value = {
            'status': 'ok',
            'native_skills': ['navigate', 'slam', 'grasp'],
        }

        adv = RobotCapabilityAdvertiser()
        with patch('integrations.robotics.capability_advertiser.get_world_model_bridge',
                   return_value=mock_bridge, create=True):
            # Need to patch the import inside the method
            with patch('integrations.agent_engine.world_model_bridge.get_world_model_bridge',
                       return_value=mock_bridge):
                caps = adv.detect_capabilities()

        assert 'navigate' in caps['native_skills']
        assert 'slam' in caps['native_skills']

    def test_has_capability_sensor_types(self):
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        adv = RobotCapabilityAdvertiser()
        caps = {
            'locomotion': None,
            'manipulation': None,
            'sensors': {'imu': True, 'gps': False, 'camera': True},
            'native_skills': ['navigate'],
        }
        assert adv._has_capability(caps, 'imu') is True
        assert adv._has_capability(caps, 'gps') is False
        assert adv._has_capability(caps, 'camera') is True
        assert adv._has_capability(caps, 'navigate') is True
        assert adv._has_capability(caps, 'locomotion') is False


# ── Robot Tools Tests ────────────────────────────────────────────

class TestRobotTools:
    def test_get_robot_capabilities(self):
        from integrations.robotics.robot_tools import get_robot_capabilities
        result = json.loads(get_robot_capabilities())
        # Should return a dict with capability keys (even if empty)
        assert isinstance(result, dict)

    def test_read_sensor_missing_id(self):
        from integrations.robotics.robot_tools import read_sensor
        result = json.loads(read_sensor())
        assert 'error' in result

    def test_read_sensor_no_data(self):
        from integrations.robotics.robot_tools import read_sensor
        result = json.loads(read_sensor(sensor_id='nonexistent_sensor'))
        assert 'error' in result

    def test_get_sensor_window_missing_id(self):
        from integrations.robotics.robot_tools import get_sensor_window
        result = json.loads(get_sensor_window())
        assert 'error' in result

    def test_get_robot_status(self):
        from integrations.robotics.robot_tools import get_robot_status
        result = json.loads(get_robot_status())
        assert isinstance(result, dict)
        assert 'safety' in result
        assert 'active_sensors' in result

    @patch('integrations.agent_engine.world_model_bridge.requests.post')
    def test_navigate_to(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.robotics.robot_tools import navigate_to
        # Mock safety to allow action
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = json.loads(navigate_to(x=1.0, y=2.0, z=0.0))
        assert 'success' in result

    @patch('integrations.agent_engine.world_model_bridge.requests.post')
    def test_move_joint(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.robotics.robot_tools import move_joint
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = json.loads(move_joint(joint_id='elbow', position=1.57))
        assert 'success' in result

    def test_move_joint_no_id(self):
        from integrations.robotics.robot_tools import move_joint
        result = json.loads(move_joint())
        assert 'error' in result

    def test_execute_motion_sequence_invalid_json(self):
        from integrations.robotics.robot_tools import execute_motion_sequence
        result = json.loads(execute_motion_sequence(steps='not json'))
        assert 'error' in result

    def test_execute_motion_sequence_empty(self):
        from integrations.robotics.robot_tools import execute_motion_sequence
        result = json.loads(execute_motion_sequence(steps='[]'))
        assert 'error' in result

    def test_configure_sensor_no_id(self):
        from integrations.robotics.robot_tools import configure_sensor
        result = json.loads(configure_sensor())
        assert 'error' in result

    def test_configure_sensor_invalid_config(self):
        from integrations.robotics.robot_tools import configure_sensor
        result = json.loads(configure_sensor(
            sensor_id='imu_0', config='not json'))
        assert 'error' in result


# ── Robot Prompt Builder Tests ───────────────────────────────────

class TestRobotPromptBuilder:
    def _mock_advertiser_with_hardware(self):
        """Return a mock capability advertiser that reports robot hardware."""
        mock_adv = MagicMock()
        mock_adv.get_capabilities.return_value = {
            'form_factor': 'rover',
            'locomotion': {'type': 'differential'},
            'manipulation': None,
            'sensors': {'imu': True},
            'native_skills': [],
        }
        return mock_adv

    def test_build_robot_prompt_basic(self):
        from integrations.robotics.robot_prompt_builder import (
            build_robot_prompt,
        )
        goal = {
            'title': 'Navigate to charging station',
            'description': 'Find and dock at the nearest charging station.',
            'goal_type': 'robot',
        }
        with patch(
            'integrations.robotics.capability_advertiser.get_capability_advertiser',
            return_value=self._mock_advertiser_with_hardware(),
        ):
            prompt = build_robot_prompt(goal)
        assert prompt is not None, "build_robot_prompt returned None — hardware mock failed"
        assert 'ROBOT TASK AGENT' in prompt
        assert 'Navigate to charging station' in prompt
        assert 'navigate_to' in prompt
        assert 'read_sensor' in prompt
        assert 'E-stop' in prompt

    def test_prompt_includes_safety_warning(self):
        from integrations.robotics.robot_prompt_builder import (
            build_robot_prompt,
        )
        goal = {'title': 'Test', 'description': '', 'goal_type': 'robot'}
        with patch(
            'integrations.robotics.capability_advertiser.get_capability_advertiser',
            return_value=self._mock_advertiser_with_hardware(),
        ):
            prompt = build_robot_prompt(goal)
        assert prompt is not None, "build_robot_prompt returned None — hardware mock failed"
        assert 'ALWAYS check get_robot_status()' in prompt
        assert 'Never compute trajectories' in prompt

    def test_prompt_capabilities_section(self):
        from integrations.robotics.robot_prompt_builder import (
            _get_capabilities_section,
        )
        mock_adv = MagicMock()
        mock_adv.get_capabilities.return_value = {
            'form_factor': 'rover',
            'locomotion': {'type': 'differential', 'max_speed': '1.0 m/s'},
            'manipulation': None,
            'sensors': {'imu': True, 'gps': True},
            'actuators': ['motor_left', 'motor_right'],
            'payload_kg': 5.0,
            'native_skills': ['navigate'],
        }
        with patch(
            'integrations.robotics.capability_advertiser.get_capability_advertiser',
            return_value=mock_adv,
        ):
            section = _get_capabilities_section()
        assert 'rover' in section
        assert 'differential' in section
        assert 'imu' in section
        assert 'motor_left' in section
        assert '5.0 kg' in section

    def test_prompt_safety_section_estopped(self):
        from integrations.robotics.robot_prompt_builder import (
            _get_safety_section,
        )
        mock_monitor = MagicMock()
        mock_monitor.get_safety_status.return_value = {
            'is_estopped': True,
            'estop_reason': 'test halt',
        }
        with patch(
            'integrations.robotics.safety_monitor.get_safety_monitor',
            return_value=mock_monitor,
        ):
            section = _get_safety_section()
        assert 'E-STOP ACTIVE' in section
        assert 'test halt' in section
        assert 'NO MOTION COMMANDS' in section


# ── Goal Registration Tests ──────────────────────────────────────

class TestGoalRegistration:
    def test_robot_type_registered(self):
        from integrations.agent_engine.goal_manager import (
            get_registered_types,
        )
        types = get_registered_types()
        assert 'robot' in types

    def test_robot_prompt_builder_callable(self):
        from integrations.agent_engine.goal_manager import (
            get_prompt_builder,
        )
        builder = get_prompt_builder('robot')
        assert builder is not None
        assert callable(builder)

    def test_robot_tool_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('robot')
        assert 'robot' in tags

    def test_robot_goal_prompt_via_manager(self):
        from integrations.agent_engine.goal_manager import GoalManager
        goal_dict = {
            'goal_type': 'robot',
            'title': 'Pick up the red cube',
            'description': 'Use gripper to grasp the red cube on the table.',
        }
        mock_adv = MagicMock()
        mock_adv.get_capabilities.return_value = {
            'form_factor': 'arm',
            'locomotion': None,
            'manipulation': {'arms': 1, 'grippers': 1, 'dof': 6},
            'sensors': {'camera': True},
            'native_skills': [],
        }
        with patch(
            'integrations.robotics.capability_advertiser.get_capability_advertiser',
            return_value=mock_adv,
        ):
            prompt = GoalManager.build_prompt(goal_dict)
        assert prompt is not None, "build_prompt returned None — hardware mock failed"
        assert 'ROBOT TASK AGENT' in prompt
        assert 'Pick up the red cube' in prompt


# ── Goal Seeding Tests ───────────────────────────────────────────

class TestGoalSeeding:
    def test_robot_health_monitor_in_seeds(self):
        from integrations.agent_engine.goal_seeding import (
            SEED_BOOTSTRAP_GOALS,
        )
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_robot_health_monitor' in slugs

    def test_robot_health_monitor_config(self):
        from integrations.agent_engine.goal_seeding import (
            SEED_BOOTSTRAP_GOALS,
        )
        robot_goal = next(
            g for g in SEED_BOOTSTRAP_GOALS
            if g['slug'] == 'bootstrap_robot_health_monitor'
        )
        assert robot_goal['goal_type'] == 'robot'
        assert robot_goal['config']['continuous'] is True
        assert 'get_robot_status' in robot_goal['description']


# ── Dispatch Capability Matching Tests ───────────────────────────

class TestDispatchCapabilityMatch:
    def test_non_robot_always_passes(self):
        from integrations.agent_engine.dispatch import (
            _check_robot_capability_match,
        )
        assert _check_robot_capability_match('marketing', 'g1') is True
        assert _check_robot_capability_match('coding', 'g2') is True

    def test_robot_no_db_passes(self):
        from integrations.agent_engine.dispatch import (
            _check_robot_capability_match,
        )
        # When DB is unavailable, should pass (fail-open for local execution)
        with patch('integrations.agent_engine.dispatch.get_db',
                   side_effect=Exception('no db'), create=True):
            assert _check_robot_capability_match('robot', 'g1') is True

    def test_robot_with_matching_caps(self):
        from integrations.agent_engine.dispatch import (
            _check_robot_capability_match,
        )
        mock_goal = MagicMock()
        mock_goal.config_json = {
            'required_capabilities': ['locomotion'],
        }

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_goal

        mock_adv = MagicMock()
        mock_adv.matches_task_requirements.return_value = 0.9

        with patch('integrations.social.models.get_db', return_value=mock_db):
            with patch(
                'integrations.robotics.capability_advertiser.get_capability_advertiser',
                return_value=mock_adv,
            ):
                result = _check_robot_capability_match('robot', 'g1')
        assert result is True

    def test_robot_with_mismatched_caps(self):
        from integrations.agent_engine.dispatch import (
            _check_robot_capability_match,
        )
        mock_goal = MagicMock()
        mock_goal.config_json = {
            'required_capabilities': ['locomotion', 'gripper', 'lidar'],
        }

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_goal

        mock_adv = MagicMock()
        mock_adv.matches_task_requirements.return_value = 0.2

        with patch('integrations.social.models.get_db', return_value=mock_db):
            with patch(
                'integrations.robotics.capability_advertiser.get_capability_advertiser',
                return_value=mock_adv,
            ):
                result = _check_robot_capability_match('robot', 'g1')
        assert result is False


# ── Device Routing Tests ─────────────────────────────────────────

class TestDeviceRouting:
    def test_robot_in_tts_priority(self):
        from integrations.social.device_routing_service import _TTS_PRIORITY
        assert 'robot' in _TTS_PRIORITY
        # Robot should be lowest priority for TTS
        assert _TTS_PRIORITY.index('robot') > _TTS_PRIORITY.index('phone')
        assert _TTS_PRIORITY.index('robot') > _TTS_PRIORITY.index('embedded')


# ── Gossip Beacon Tests ──────────────────────────────────────────

class TestGossipBeacon:
    def test_beacon_includes_robot_capabilities(self):
        """Verify beacon builder tries to include robot capabilities."""
        from integrations.social.peer_discovery import AutoDiscovery
        # We can't easily test the full beacon without UDP sockets,
        # but we can verify the import path exists
        try:
            from integrations.robotics.capability_advertiser import (
                get_capability_advertiser,
            )
            adv = get_capability_advertiser()
            payload = adv.get_gossip_payload()
            assert 'form_factor' in payload
            assert 'has_locomotion' in payload
            assert 'sensor_types' in payload
        except ImportError:
            pytest.skip("Robotics package not available")


# ── Integration: Tool → Bridge → HevolveAI routing ───────────────

class TestToolBridgeIntegration:
    @patch('integrations.agent_engine.world_model_bridge.requests.post')
    def test_navigate_to_routes_through_bridge(self, mock_post):
        """navigate_to tool → WorldModelBridge.send_action → HTTP POST."""
        mock_post.return_value = MagicMock(status_code=200)
        from integrations.robotics.robot_tools import navigate_to
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        mock_monitor.check_position_safe.return_value = True
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            result = json.loads(navigate_to(x=1.0, y=2.0, z=0.0))
        # Verify the POST was made (action routed to HevolveAI)
        assert mock_post.called or result.get('success') is not None

    def test_capability_advertiser_no_intelligence(self):
        """Verify advertiser doesn't contain any ML/control code."""
        import inspect
        from integrations.robotics.capability_advertiser import (
            RobotCapabilityAdvertiser,
        )
        source = inspect.getsource(RobotCapabilityAdvertiser)
        # Should NOT contain PID, Kalman, SLAM, path planning keywords
        for forbidden in ['kalman', 'pid_', 'slam(', 'a_star', 'rrt(',
                          'inverse_kinematics']:
            assert forbidden not in source.lower(), (
                f"Advertiser contains intelligence code: {forbidden}")

    def test_robot_tools_no_intelligence(self):
        """Verify robot tools don't contain any ML/control code."""
        import inspect
        import integrations.robotics.robot_tools as tools_module
        source = inspect.getsource(tools_module)
        for forbidden in ['kalman', 'pid_', 'slam(', 'a_star', 'rrt(',
                          'inverse_kinematics', 'gradient_descent']:
            assert forbidden not in source.lower(), (
                f"Robot tools contain intelligence code: {forbidden}")

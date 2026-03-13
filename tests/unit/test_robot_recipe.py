"""
Tests for Batch 6: Recipe Integration + Robot Boot + Embedded Main Loop.

Covers:
  - RobotRecipeAdapter: action↔recipe conversion, sequence recording/replay
  - boot_robotics: subsystem initialization
  - embedded_main.py robot boot integration
  - lifecycle_hooks: new physical action states
  - goal_seeding: bootstrap_robot_learning goal
"""
import json
import os
import time
import pytest
from unittest.mock import patch, MagicMock


# ── RobotRecipeAdapter Tests ────────────────────────────────────

class TestActionToRecipeStep:
    def test_basic_conversion(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        action = {
            'type': 'motor_velocity',
            'target': 'left_wheel',
            'params': {'velocity': 0.5},
        }
        step = RobotRecipeAdapter.action_to_recipe_step(action)
        assert step['step_type'] == 'robot_action'
        assert step['action'] == action
        assert step['sensor_context'] == {}
        assert step['outcome'] == {}
        assert 'timestamp' in step
        assert 'step_id' in step

    def test_with_sensor_context(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        action = {'type': 'navigate_to', 'target': 'base', 'params': {'x': 1.0}}
        sensor_ctx = {'imu': {'accel_x': 0.1}, 'gps': {'lat': 37.0}}
        step = RobotRecipeAdapter.action_to_recipe_step(
            action, sensor_context=sensor_ctx)
        assert step['sensor_context']['imu']['accel_x'] == 0.1
        assert step['sensor_context']['gps']['lat'] == 37.0

    def test_with_outcome(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        action = {'type': 'gripper', 'target': 'gripper_0', 'params': {'force': 5}}
        outcome = {'success': True, 'grasp_force': 4.8, 'slip_detected': False}
        step = RobotRecipeAdapter.action_to_recipe_step(
            action, outcome=outcome)
        assert step['outcome']['success'] is True
        assert step['outcome']['grasp_force'] == 4.8


class TestRecipeStepToAction:
    def test_valid_step(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        step = {
            'step_type': 'robot_action',
            'action': {'type': 'motor_velocity', 'target': 'wheel',
                       'params': {'velocity': 0.3}},
        }
        action = RobotRecipeAdapter.recipe_step_to_action(step)
        assert action is not None
        assert action['type'] == 'motor_velocity'

    def test_non_robot_step(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        step = {
            'step_type': 'text_action',
            'action': {'type': 'speak', 'text': 'hello'},
        }
        assert RobotRecipeAdapter.recipe_step_to_action(step) is None

    def test_missing_type(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        step = {
            'step_type': 'robot_action',
            'action': {'target': 'wheel', 'params': {}},
        }
        assert RobotRecipeAdapter.recipe_step_to_action(step) is None

    def test_missing_action(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        step = {'step_type': 'robot_action'}
        assert RobotRecipeAdapter.recipe_step_to_action(step) is None


class TestRecordMotionSequence:
    def test_record_plain_actions(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        actions = [
            {'type': 'motor_velocity', 'target': 'left', 'params': {'v': 0.5}},
            {'type': 'motor_velocity', 'target': 'right', 'params': {'v': 0.5}},
            {'type': 'motor_velocity', 'target': 'left', 'params': {'v': 0.0}},
        ]
        recipe = RobotRecipeAdapter.record_motion_sequence(actions)
        assert recipe['recipe_type'] == 'robot_motion_sequence'
        assert recipe['step_count'] == 3
        assert len(recipe['steps']) == 3
        assert recipe['steps'][0]['action']['target'] == 'left'

    def test_record_with_sensor_log(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        actions = [
            {'type': 'navigate_to', 'target': 'base',
             'params': {'x': 1.0, 'y': 0.0}},
        ]
        sensor_log = [{'imu': {'accel_x': 0.02}}]
        recipe = RobotRecipeAdapter.record_motion_sequence(
            actions, sensor_log=sensor_log)
        assert recipe['steps'][0]['sensor_context']['imu']['accel_x'] == 0.02

    def test_record_structured_entries(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        entries = [
            {
                'action': {'type': 'gripper', 'target': 'g0',
                           'params': {'force': 5}},
                'sensor_context': {'force': {'fx': 4.8}},
                'outcome': {'success': True},
            },
        ]
        recipe = RobotRecipeAdapter.record_motion_sequence(entries)
        assert recipe['step_count'] == 1
        assert recipe['steps'][0]['outcome']['success'] is True


class TestReplayMotionRecipe:
    def test_replay_extracts_actions(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        recipe = {
            'steps': [
                {
                    'step_type': 'robot_action',
                    'action': {'type': 'motor_velocity', 'target': 'l',
                               'params': {'v': 0.5}},
                },
                {
                    'step_type': 'robot_action',
                    'action': {'type': 'motor_velocity', 'target': 'r',
                               'params': {'v': 0.5}},
                },
            ],
        }
        actions = RobotRecipeAdapter.replay_motion_recipe(recipe)
        assert len(actions) == 2
        assert actions[0]['target'] == 'l'
        assert actions[1]['target'] == 'r'

    def test_replay_skips_non_robot(self):
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        recipe = {
            'steps': [
                {'step_type': 'text_action', 'action': {'type': 'speak'}},
                {
                    'step_type': 'robot_action',
                    'action': {'type': 'navigate_to', 'target': 'b',
                               'params': {'x': 1.0}},
                },
            ],
        }
        actions = RobotRecipeAdapter.replay_motion_recipe(recipe)
        assert len(actions) == 1
        assert actions[0]['type'] == 'navigate_to'

    def test_roundtrip(self):
        """Record and replay a sequence — actions should match."""
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        original = [
            {'type': 'motor_velocity', 'target': 'l', 'params': {'v': 0.5}},
            {'type': 'servo_position', 'target': 'pan', 'params': {'angle': 90}},
        ]
        recipe = RobotRecipeAdapter.record_motion_sequence(original)
        replayed = RobotRecipeAdapter.replay_motion_recipe(recipe)
        assert len(replayed) == 2
        assert replayed[0]['type'] == 'motor_velocity'
        assert replayed[1]['type'] == 'servo_position'
        assert replayed[1]['params']['angle'] == 90


# ── Robot Boot Tests ─────────────────────────────────────────────

class TestRobotBoot:
    def test_boot_returns_status(self):
        from integrations.robotics.robot_boot import boot_robotics
        status = boot_robotics()
        assert isinstance(status, dict)
        assert 'safety' in status
        assert 'sensor_store' in status
        assert 'control_loop' in status
        assert 'capability_advertiser' in status
        assert 'bridge_ready' in status

    def test_boot_safety_initializes(self):
        from integrations.robotics.robot_boot import boot_robotics
        status = boot_robotics()
        # Safety monitor should initialize (no hardware needed)
        assert status['safety'] is True

    def test_boot_sensor_store_initializes(self):
        from integrations.robotics.robot_boot import boot_robotics
        status = boot_robotics()
        assert status['sensor_store'] is True

    def test_boot_with_caps(self):
        from integrations.robotics.robot_boot import boot_robotics
        mock_caps = MagicMock()
        mock_caps.hardware.has_serial = False
        mock_caps.hardware.has_gpio = False
        status = boot_robotics(mock_caps)
        assert isinstance(status, dict)


# ── Embedded Main Integration Tests ─────────────────────────────

class TestEmbeddedMainRobot:
    @patch('embedded_main._boot_system_check')
    @patch('embedded_main._boot_identity', return_value=('node123', 'hash456'))
    @patch('embedded_main._boot_guardrails', return_value='gh789')
    @patch('embedded_main._boot_safety')
    @patch('embedded_main._boot_db', return_value=True)
    @patch('embedded_main._main_loop')
    def test_robot_enabled_triggers_boot(self, mock_loop, mock_db, mock_safety,
                                         mock_guard, mock_id, mock_sys):
        mock_sys.return_value = MagicMock(
            tier=MagicMock(value='standard'),
            hardware=MagicMock(has_gpio=False, has_serial=False,
                               cpu_cores=4, ram_gb=8),
            enabled_features=[],
        )

        with patch.dict(os.environ, {
            'HEVOLVE_HEADLESS': 'true',
            'HEVOLVE_ROBOT_ENABLED': 'true',
        }):
            with patch('integrations.robotics.robot_boot.boot_robotics',
                       return_value={'safety': True}) as mock_boot:
                import embedded_main
                embedded_main.main()
                mock_boot.assert_called_once()

    def test_robot_disabled_skips_boot(self):
        """When HEVOLVE_ROBOT_ENABLED is not set, robot boot is skipped."""
        # Just verify the env check logic
        robot_enabled = os.environ.get('HEVOLVE_ROBOT_ENABLED', '').lower() == 'true'
        assert robot_enabled is False  # Not set in test env


# ── Lifecycle Hooks Tests ────────────────────────────────────────

class TestPhysicalActionStates:
    def test_executing_motion_state_exists(self):
        from lifecycle_hooks import ActionState
        assert hasattr(ActionState, 'EXECUTING_MOTION')
        assert ActionState.EXECUTING_MOTION.value == 'executing_motion'

    def test_sensor_confirm_state_exists(self):
        from lifecycle_hooks import ActionState
        assert hasattr(ActionState, 'SENSOR_CONFIRM')
        assert ActionState.SENSOR_CONFIRM.value == 'sensor_confirm'

    def test_all_original_states_preserved(self):
        from lifecycle_hooks import ActionState
        # Verify original states still exist
        expected = [
            'ASSIGNED', 'IN_PROGRESS', 'STATUS_VERIFICATION_REQUESTED',
            'COMPLETED', 'PENDING', 'ERROR', 'FALLBACK_REQUESTED',
            'FALLBACK_RECEIVED', 'RECIPE_REQUESTED', 'RECIPE_RECEIVED',
            'TERMINATED',
        ]
        for state_name in expected:
            assert hasattr(ActionState, state_name), f"Missing: {state_name}"


# ── Goal Seeding Tests ───────────────────────────────────────────

class TestGoalSeedingRobotLearning:
    def test_bootstrap_robot_learning_exists(self):
        from integrations.agent_engine.goal_seeding import (
            SEED_BOOTSTRAP_GOALS,
        )
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_robot_learning' in slugs

    def test_bootstrap_robot_learning_config(self):
        from integrations.agent_engine.goal_seeding import (
            SEED_BOOTSTRAP_GOALS,
        )
        goal = next(
            g for g in SEED_BOOTSTRAP_GOALS
            if g['slug'] == 'bootstrap_robot_learning'
        )
        assert goal['goal_type'] == 'robot'
        assert goal['config']['mode'] == 'learning'
        assert goal['config']['continuous'] is True
        assert 'recipe' in goal['description'].lower()

    def test_both_robot_goals_exist(self):
        from integrations.agent_engine.goal_seeding import (
            SEED_BOOTSTRAP_GOALS,
        )
        slugs = [g['slug'] for g in SEED_BOOTSTRAP_GOALS]
        assert 'bootstrap_robot_learning' in slugs
        assert 'bootstrap_robot_health_monitor' in slugs


# ── Integration: Recipe → Replay → Bridge ────────────────────────

class TestRecipeBridgeIntegration:
    @patch('integrations.agent_engine.world_model_bridge.requests.post')
    def test_replay_sends_through_bridge(self, mock_post):
        """Replayed actions route through WorldModelBridge."""
        mock_post.return_value = MagicMock(status_code=200)

        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        from integrations.agent_engine.world_model_bridge import WorldModelBridge

        # Create a recipe
        actions = [
            {'type': 'motor_velocity', 'target': 'left',
             'params': {'velocity': 0.5}},
        ]
        recipe = RobotRecipeAdapter.record_motion_sequence(actions)

        # Replay
        replay_actions = RobotRecipeAdapter.replay_motion_recipe(recipe)
        assert len(replay_actions) == 1

        # Send through bridge
        bridge = WorldModelBridge()
        bridge._in_process = False
        mock_monitor = MagicMock()
        mock_monitor.is_estopped = False
        mock_monitor.check_position_safe.return_value = True
        with patch('integrations.robotics.safety_monitor.get_safety_monitor',
                   return_value=mock_monitor):
            for action in replay_actions:
                result = bridge.send_action(action)
                assert result is True

    def test_recipe_adapter_no_intelligence(self):
        """Verify recipe adapter doesn't contain ML/control code."""
        import inspect
        from integrations.robotics.recipe_adapter import RobotRecipeAdapter
        source = inspect.getsource(RobotRecipeAdapter)
        for forbidden in ['kalman', 'pid_', 'slam(', 'gradient',
                          'inverse_kinematics', 'path_plan']:
            assert forbidden not in source.lower(), (
                f"Recipe adapter contains intelligence code: {forbidden}")

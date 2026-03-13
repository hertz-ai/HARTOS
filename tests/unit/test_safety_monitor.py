"""
Tests for integrations.robotics.safety_monitor — E-Stop + Workspace Limits.

Covers:
  - E-stop trigger and clear lifecycle
  - Human-only clear enforcement (agents rejected)
  - Workspace limit enforcement (Cartesian + joint)
  - GPIO/serial E-stop source registration
  - Fleet command integration (estop/estop_clear)
  - Safety status reporting
  - Audit trail
  - Monitor thread start/stop
  - HiveCircuitBreaker.local_halt() integration
  - Safety tools (AutoGen)
"""
import json
import os
import threading
import time
import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_env():
    """Ensure halt env vars are clean before/after each test."""
    os.environ.pop('HEVOLVE_HALTED', None)
    os.environ.pop('HEVOLVE_HALT_REASON', None)
    yield
    os.environ.pop('HEVOLVE_HALTED', None)
    os.environ.pop('HEVOLVE_HALT_REASON', None)


@pytest.fixture
def monitor():
    """Fresh SafetyMonitor for each test (not singleton)."""
    from integrations.robotics.safety_monitor import SafetyMonitor
    m = SafetyMonitor()
    yield m
    m.stop()


# ── E-Stop Trigger Tests ────────────────────────────────────────

class TestEStopTrigger:
    def test_trigger_sets_estop_active(self, monitor):
        assert not monitor.is_estopped
        monitor.trigger_estop('test reason', source='test')
        assert monitor.is_estopped

    def test_trigger_sets_halt_env(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert os.environ.get('HEVOLVE_HALTED') == 'true'
        assert 'E-STOP' in os.environ.get('HEVOLVE_HALT_REASON', '')

    def test_trigger_records_reason_and_source(self, monitor):
        monitor.trigger_estop('motor overtemp', source='gpio_17')
        status = monitor.get_safety_status()
        assert status['estop_reason'] == 'motor overtemp'
        assert status['estop_source'] == 'gpio_17'

    def test_trigger_records_timestamp(self, monitor):
        before = time.time()
        monitor.trigger_estop('test', source='test')
        after = time.time()
        status = monitor.get_safety_status()
        assert before <= status['estop_timestamp'] <= after

    def test_double_trigger_is_idempotent(self, monitor):
        monitor.trigger_estop('first', source='test')
        monitor.trigger_estop('second', source='test')
        status = monitor.get_safety_status()
        assert status['estop_reason'] == 'first'  # First reason preserved

    def test_trigger_fires_callbacks(self, monitor):
        callback_args = []
        monitor.on_estop(lambda reason, source: callback_args.append((reason, source)))
        monitor.trigger_estop('cb test', source='cb_source')
        assert len(callback_args) == 1
        assert callback_args[0] == ('cb test', 'cb_source')

    def test_trigger_calls_local_halt(self, monitor):
        """Verify trigger_estop calls HiveCircuitBreaker.local_halt."""
        with patch('security.hive_guardrails.HiveCircuitBreaker.local_halt') as mock_halt:
            monitor.trigger_estop('halt test', source='test')
            mock_halt.assert_called_once()
            assert 'halt test' in mock_halt.call_args[0][0]
        assert monitor.is_estopped

    def test_callback_error_does_not_block_estop(self, monitor):
        def bad_callback(reason, source):
            raise RuntimeError("callback error")
        monitor.on_estop(bad_callback)
        monitor.trigger_estop('error test', source='test')
        assert monitor.is_estopped  # E-stop still activated


# ── E-Stop Clear Tests ──────────────────────────────────────────

class TestEStopClear:
    def test_clear_by_human_operator(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert monitor.is_estopped
        result = monitor.clear_estop('john_operator')
        assert result is True
        assert not monitor.is_estopped

    def test_clear_removes_halt_env(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert os.environ.get('HEVOLVE_HALTED') == 'true'
        monitor.clear_estop('operator_jane')
        assert os.environ.get('HEVOLVE_HALTED') is None

    def test_clear_rejected_for_empty_operator(self, monitor):
        monitor.trigger_estop('test', source='test')
        result = monitor.clear_estop('')
        assert result is False
        assert monitor.is_estopped

    def test_clear_rejected_for_agent(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert monitor.clear_estop('agent_planner') is False
        assert monitor.is_estopped

    def test_clear_rejected_for_bot(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert monitor.clear_estop('bot_coordinator') is False
        assert monitor.is_estopped

    def test_clear_rejected_for_system(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert monitor.clear_estop('system_daemon') is False
        assert monitor.is_estopped

    def test_clear_rejected_for_auto(self, monitor):
        monitor.trigger_estop('test', source='test')
        assert monitor.clear_estop('auto_recovery') is False
        assert monitor.is_estopped

    def test_clear_records_operator(self, monitor):
        monitor.trigger_estop('test', source='test')
        monitor.clear_estop('operator_bob')
        status = monitor.get_safety_status()
        assert status['cleared_by'] == 'operator_bob'

    def test_clear_when_not_estopped_returns_true(self, monitor):
        assert not monitor.is_estopped
        result = monitor.clear_estop('operator')
        assert result is True


# ── Workspace Limits Tests ──────────────────────────────────────

class TestWorkspaceLimits:
    def test_position_within_limits(self, monitor):
        monitor.register_workspace_limits({
            'x': (-1.0, 1.0), 'y': (-0.5, 0.5), 'z': (0.0, 1.2),
        })
        assert monitor.check_position_safe({'x': 0.5, 'y': 0.2, 'z': 0.8})

    def test_position_outside_x_limit(self, monitor):
        monitor.register_workspace_limits({'x': (-1.0, 1.0)})
        assert not monitor.check_position_safe({'x': 1.5})

    def test_position_below_min(self, monitor):
        monitor.register_workspace_limits({'z': (0.0, 1.2)})
        assert not monitor.check_position_safe({'z': -0.1})

    def test_position_at_boundary_is_safe(self, monitor):
        monitor.register_workspace_limits({'x': (-1.0, 1.0)})
        assert monitor.check_position_safe({'x': 1.0})
        assert monitor.check_position_safe({'x': -1.0})

    def test_unconfigured_axis_is_safe(self, monitor):
        monitor.register_workspace_limits({'x': (-1.0, 1.0)})
        assert monitor.check_position_safe({'y': 999.0})  # y not configured

    def test_joint_limits(self, monitor):
        monitor.register_workspace_limits({
            'joint_limits': {'joint_0': (-90, 90), 'joint_1': (0, 180)},
        })
        assert monitor.check_position_safe({'joint_0': 45.0, 'joint_1': 90.0})
        assert not monitor.check_position_safe({'joint_0': 100.0})

    def test_estop_blocks_all_positions(self, monitor):
        monitor.register_workspace_limits({'x': (-1.0, 1.0)})
        monitor.trigger_estop('test', source='test')
        assert not monitor.check_position_safe({'x': 0.0})  # Even valid position


# ── Source Registration Tests ───────────────────────────────────

class TestSourceRegistration:
    def test_register_gpio_pin(self, monitor):
        monitor.register_estop_pin(17)
        status = monitor.get_safety_status()
        assert 17 in status['estop_gpio_pins']

    def test_register_duplicate_gpio_pin(self, monitor):
        monitor.register_estop_pin(17)
        monitor.register_estop_pin(17)
        status = monitor.get_safety_status()
        assert status['estop_gpio_pins'].count(17) == 1

    def test_register_serial_port(self, monitor):
        monitor.register_estop_serial('/dev/ttyUSB0', 'STOP!')
        status = monitor.get_safety_status()
        assert len(status['estop_serial_ports']) == 1
        assert status['estop_serial_ports'][0]['port'] == '/dev/ttyUSB0'
        assert status['estop_serial_ports'][0]['pattern'] == 'STOP!'


# ── Monitor Thread Tests ────────────────────────────────────────

class TestMonitorThread:
    def test_start_without_sources_does_not_start(self, monitor):
        monitor.start()
        assert not monitor._running

    def test_start_with_gpio_pin_starts_thread(self, monitor):
        monitor.register_estop_pin(17)
        monitor.start()
        assert monitor._running
        assert monitor._monitor_thread is not None
        assert monitor._monitor_thread.is_alive()
        monitor.stop()

    def test_stop_halts_thread(self, monitor):
        monitor.register_estop_pin(17)
        monitor.start()
        assert monitor._running
        monitor.stop()
        assert not monitor._running


# ── Safety Status Tests ─────────────────────────────────────────

class TestSafetyStatus:
    def test_initial_status(self, monitor):
        status = monitor.get_safety_status()
        assert status['estop_active'] is False
        assert status['estop_reason'] == ''
        assert status['workspace_limits'] == {}
        assert status['joint_limits'] == {}
        assert status['monitor_running'] is False

    def test_status_after_estop(self, monitor):
        monitor.trigger_estop('overheating', source='thermal_sensor')
        status = monitor.get_safety_status()
        assert status['estop_active'] is True
        assert status['estop_reason'] == 'overheating'
        assert status['estop_source'] == 'thermal_sensor'


# ── Audit Trail Tests ───────────────────────────────────────────

class TestAuditTrail:
    def test_trigger_adds_audit_entry(self, monitor):
        monitor.trigger_estop('audit test', source='test')
        status = monitor.get_safety_status()
        assert len(status['audit_trail']) >= 1
        assert status['audit_trail'][-1]['event'] == 'estop_triggered'

    def test_clear_adds_audit_entry(self, monitor):
        monitor.trigger_estop('test', source='test')
        monitor.clear_estop('operator_audit')
        status = monitor.get_safety_status()
        assert any(e['event'] == 'estop_cleared' for e in status['audit_trail'])

    def test_audit_trail_bounded(self, monitor):
        monitor._max_audit = 5
        for i in range(10):
            monitor._estop_active = False  # Reset to allow re-trigger
            monitor.trigger_estop(f'test {i}', source='test')
        # Audit should be bounded
        assert len(monitor._audit_trail) <= 5


# ── Fleet Command Integration Tests ────────────────────────────

class TestFleetCommandIntegration:
    def test_valid_command_types_include_estop(self):
        from integrations.social.fleet_command import VALID_COMMAND_TYPES
        assert 'estop' in VALID_COMMAND_TYPES
        assert 'estop_clear' in VALID_COMMAND_TYPES

    def test_execute_estop_command(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('estop', {'reason': 'fleet test'})
        assert result['success'] is True
        assert monitor_is_estopped_or_halt_flag()

    def test_execute_estop_clear_requires_operator(self):
        from integrations.social.fleet_command import FleetCommandService
        result = FleetCommandService.execute_command('estop_clear', {})
        assert result['success'] is False
        assert 'operator_id' in result['message']

    def test_execute_estop_clear_with_operator(self):
        from integrations.social.fleet_command import FleetCommandService
        # First trigger
        FleetCommandService.execute_command('estop', {'reason': 'test'})
        # Then clear with human operator
        result = FleetCommandService.execute_command(
            'estop_clear', {'operator_id': 'human_operator_1'}
        )
        assert result['success'] is True


def monitor_is_estopped_or_halt_flag():
    """Check if E-stop is active via monitor or env flag."""
    if os.environ.get('HEVOLVE_HALTED', '').lower() == 'true':
        return True
    try:
        from integrations.robotics.safety_monitor import get_safety_monitor
        return get_safety_monitor().is_estopped
    except ImportError:
        return False


# ── HiveCircuitBreaker.local_halt() Tests ───────────────────────

class TestLocalHalt:
    def test_local_halt_sets_halted(self):
        from security.hive_guardrails import HiveCircuitBreaker
        # Reset state
        HiveCircuitBreaker._halted = False
        HiveCircuitBreaker._halt_reason = ''
        HiveCircuitBreaker._halt_timestamp = None

        result = HiveCircuitBreaker.local_halt('safety test')
        assert result is True
        assert HiveCircuitBreaker.is_halted()
        assert 'safety test' in HiveCircuitBreaker.get_status()['reason']

        # Cleanup
        HiveCircuitBreaker._halted = False
        HiveCircuitBreaker._halt_reason = ''
        HiveCircuitBreaker._halt_timestamp = None

    def test_local_halt_does_not_require_master_key(self):
        from security.hive_guardrails import HiveCircuitBreaker
        HiveCircuitBreaker._halted = False
        # local_halt should succeed without any master key signature
        result = HiveCircuitBreaker.local_halt('no key needed')
        assert result is True

        # Cleanup
        HiveCircuitBreaker._halted = False
        HiveCircuitBreaker._halt_reason = ''
        HiveCircuitBreaker._halt_timestamp = None


# ── Safety Tools Tests ──────────────────────────────────────────

class TestSafetyTools:
    def test_configure_workspace_limits_tool(self):
        from integrations.robotics.safety_tools import configure_workspace_limits
        result = configure_workspace_limits('{"x": [-1, 1], "y": [-0.5, 0.5]}')
        assert 'configured' in result.lower() or 'x' in result

    def test_configure_workspace_limits_invalid_json(self):
        from integrations.robotics.safety_tools import configure_workspace_limits
        result = configure_workspace_limits('not json')
        assert 'error' in result.lower()

    def test_get_safety_status_tool(self):
        from integrations.robotics.safety_tools import get_safety_status
        result = get_safety_status()
        status = json.loads(result)
        assert 'estop_active' in status

    def test_test_estop_tool_requires_confirm(self):
        from integrations.robotics.safety_tools import test_estop
        result = test_estop('false')
        assert 'not executed' in result.lower()

    def test_test_estop_tool_executes(self):
        from integrations.robotics.safety_tools import test_estop
        result = test_estop('true')
        assert 'operational' in result.lower() or 'triggered' in result.lower()

    def test_configure_estop_sources_tool(self):
        from integrations.robotics.safety_tools import configure_estop_sources
        result = configure_estop_sources('{"gpio_pins": [17]}')
        assert 'GPIO pin 17' in result


# ── Embedded Main Boot Safety Tests ─────────────────────────────

class TestEmbeddedBootSafety:
    def test_boot_safety_no_env_vars(self):
        """When no E-stop env vars set, _boot_safety should not crash."""
        os.environ.pop('HEVOLVE_ESTOP_PINS', None)
        os.environ.pop('HEVOLVE_ESTOP_SERIAL', None)

        from embedded_main import _boot_safety
        caps = MagicMock()
        caps.hardware.has_gpio = False
        caps.hardware.has_serial = False
        _boot_safety(caps)  # Should not raise

    def test_boot_safety_with_gpio_pins(self):
        """When HEVOLVE_ESTOP_PINS is set, should register pins."""
        os.environ['HEVOLVE_ESTOP_PINS'] = '17,27'
        os.environ.pop('HEVOLVE_ESTOP_SERIAL', None)

        from embedded_main import _boot_safety
        caps = MagicMock()
        caps.hardware.has_gpio = True
        caps.hardware.has_serial = False
        _boot_safety(caps)

        # Verify pins were registered via singleton
        from integrations.robotics.safety_monitor import get_safety_monitor
        status = get_safety_monitor().get_safety_status()
        assert 17 in status['estop_gpio_pins'] or 27 in status['estop_gpio_pins']

        os.environ.pop('HEVOLVE_ESTOP_PINS', None)

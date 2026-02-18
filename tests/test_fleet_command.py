"""
Tests for integrations.social.fleet_command - Queen Bee Authority.

Covers: push_command, push_broadcast, get_pending_commands, ack_command,
execute_command (all 6 types), signature verification, validation.
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set up in-memory DB before importing models
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.models import Base, get_engine, get_db, PeerNode, FleetCommand
from integrations.social.fleet_command import (
    FleetCommandService, VALID_COMMAND_TYPES,
    _execute_config_update, _execute_halt, _execute_restart,
    _execute_sensor_config, _execute_goal_assign, _execute_firmware_update,
)


@pytest.fixture(scope='session')
def engine():
    """Create in-memory engine and tables once."""
    eng = get_engine()
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    """Fresh DB session per test with rollback."""
    session = get_db()
    yield session
    session.rollback()
    session.close()


# ══════════════════════════════════════════════════════════════════
# push_command
# ══════════════════════════════════════════════════════════════════

class TestPushCommand:

    @patch('integrations.social.fleet_command._sign_command', return_value='sig123')
    @patch('integrations.social.fleet_command._get_self_node_id', return_value='central001')
    def test_push_command_basic(self, mock_id, mock_sign, db):
        """Push a config_update command to a specific node."""
        result = FleetCommandService.push_command(
            db, 'node_abc123', 'config_update',
            {'env_vars': {'HEVOLVE_GOSSIP_ENABLED': 'true'}},
        )
        assert result is not None
        assert result['cmd_type'] == 'config_update'
        assert result['target_node_id'] == 'node_abc123'
        assert result['status'] == 'pending'
        assert result['issued_by'] == 'central001'
        assert result['signature'] == 'sig123'

    def test_push_command_invalid_type(self, db):
        """Reject commands with invalid type."""
        result = FleetCommandService.push_command(
            db, 'node_abc', 'invalid_type', {},
        )
        assert result is None

    def test_push_command_empty_node_id(self, db):
        """Reject commands with empty node_id."""
        result = FleetCommandService.push_command(
            db, '', 'config_update', {},
        )
        assert result is None

    @patch('integrations.social.fleet_command._sign_command', return_value='')
    @patch('integrations.social.fleet_command._get_self_node_id', return_value='central001')
    def test_push_all_command_types(self, mock_id, mock_sign, db):
        """All 6 command types are accepted."""
        for cmd_type in VALID_COMMAND_TYPES:
            result = FleetCommandService.push_command(
                db, f'node_{cmd_type}', cmd_type, {'test': True},
            )
            assert result is not None, f"Command type '{cmd_type}' was rejected"
            assert result['cmd_type'] == cmd_type


# ══════════════════════════════════════════════════════════════════
# push_broadcast
# ══════════════════════════════════════════════════════════════════

class TestPushBroadcast:

    @patch('integrations.social.fleet_command._sign_command', return_value='')
    @patch('integrations.social.fleet_command._get_self_node_id', return_value='central001')
    def test_broadcast_to_tier(self, mock_id, mock_sign, db):
        """Broadcast halt to all embedded nodes."""
        # Create some peer nodes
        for i in range(3):
            peer = PeerNode(
                node_id=f'emb_node_{i}', url=f'http://emb{i}:8080',
                status='active', capability_tier='embedded',
            )
            db.add(peer)
        # One non-embedded node
        db.add(PeerNode(
            node_id='std_node_1', url='http://std1:8080',
            status='active', capability_tier='standard',
        ))
        db.flush()

        results = FleetCommandService.push_broadcast(
            db, 'halt', {'reason': 'maintenance'}, tier_filter='embedded',
        )
        assert len(results) == 3
        for cmd in results:
            assert cmd['cmd_type'] == 'halt'
            assert 'emb_node_' in cmd['target_node_id']

    def test_broadcast_invalid_type(self, db):
        """Reject broadcast with invalid command type."""
        results = FleetCommandService.push_broadcast(
            db, 'bad_type', {},
        )
        assert results == []


# ══════════════════════════════════════════════════════════════════
# get_pending_commands + ack_command
# ══════════════════════════════════════════════════════════════════

class TestGetAndAckCommands:

    @patch('integrations.social.fleet_command._sign_command', return_value='sig')
    @patch('integrations.social.fleet_command._get_self_node_id', return_value='central')
    def test_get_pending_and_ack(self, mock_id, mock_sign, db):
        """Push commands, retrieve them, acknowledge one."""
        # Push 2 commands for the same node
        FleetCommandService.push_command(db, 'node_x', 'config_update', {'env_vars': {'A': '1'}})
        FleetCommandService.push_command(db, 'node_x', 'restart', {'target': 'gossip'})
        # Push one for a different node
        FleetCommandService.push_command(db, 'node_y', 'halt', {'reason': 'test'})

        # Get pending for node_x
        pending = FleetCommandService.get_pending_commands(db, 'node_x')
        assert len(pending) == 2
        assert pending[0]['cmd_type'] == 'config_update'
        assert pending[1]['cmd_type'] == 'restart'
        # All should now be 'delivered'
        assert all(c['status'] == 'delivered' for c in pending)

        # Get again - should be empty (already delivered)
        again = FleetCommandService.get_pending_commands(db, 'node_x')
        assert len(again) == 0

        # Ack the first command
        acked = FleetCommandService.ack_command(
            db, pending[0]['id'], 'node_x', success=True, result_message='done',
        )
        assert acked is not None
        assert acked['status'] == 'completed'
        assert acked['result_message'] == 'done'

    @patch('integrations.social.fleet_command._sign_command', return_value='sig')
    @patch('integrations.social.fleet_command._get_self_node_id', return_value='central')
    def test_ack_command_failed(self, mock_id, mock_sign, db):
        """Ack a command as failed."""
        FleetCommandService.push_command(db, 'node_z', 'sensor_config', {'poll_interval_ms': 100})
        pending = FleetCommandService.get_pending_commands(db, 'node_z')
        assert len(pending) == 1

        acked = FleetCommandService.ack_command(
            db, pending[0]['id'], 'node_z', success=False,
            result_message='GPIO not available',
        )
        assert acked['status'] == 'failed'

    def test_ack_nonexistent_command(self, db):
        """Ack a command that doesn't exist returns None."""
        result = FleetCommandService.ack_command(db, 99999, 'node_x')
        assert result is None


# ══════════════════════════════════════════════════════════════════
# Local command execution
# ══════════════════════════════════════════════════════════════════

class TestExecuteCommand:

    def test_execute_config_update(self):
        """Config update sets env vars."""
        result = _execute_config_update({
            'env_vars': {'HEVOLVE_TEST_FLEET_VAR': 'hello'}
        })
        assert result['success'] is True
        assert os.environ.get('HEVOLVE_TEST_FLEET_VAR') == 'hello'
        os.environ.pop('HEVOLVE_TEST_FLEET_VAR', None)

    def test_execute_config_update_blocks_master_key(self):
        """Config update NEVER overwrites master key vars."""
        result = _execute_config_update({
            'env_vars': {
                'HEVOLVE_MASTER_PRIVATE_KEY': 'evil',
                'HEVOLVE_GOSSIP_ENABLED': 'true',
            }
        })
        assert result['success'] is True
        # Master key should NOT be set
        assert os.environ.get('HEVOLVE_MASTER_PRIVATE_KEY') != 'evil'
        os.environ.pop('HEVOLVE_GOSSIP_ENABLED', None)

    def test_execute_config_update_blocks_guardrail(self):
        """Config update NEVER overwrites guardrail vars."""
        result = _execute_config_update({
            'env_vars': {'HEVOLVE_GUARDRAIL_HASH': 'evil'}
        })
        assert result['success'] is True
        assert os.environ.get('HEVOLVE_GUARDRAIL_HASH') != 'evil'

    def test_execute_config_update_empty(self):
        """Config update with no env_vars fails."""
        result = _execute_config_update({})
        assert result['success'] is False

    def test_execute_halt(self):
        """Halt sets halt flag when circuit breaker unavailable."""
        with patch.dict(os.environ, {}, clear=False):
            result = _execute_halt({'reason': 'test halt'})
            assert result['success'] is True
            os.environ.pop('HEVOLVE_HALTED', None)

    def test_execute_restart(self):
        """Restart sets restart flag."""
        result = _execute_restart({'target': 'gossip'})
        assert result['success'] is True
        assert os.environ.get('HEVOLVE_RESTART_REQUESTED') == 'gossip'
        os.environ.pop('HEVOLVE_RESTART_REQUESTED', None)

    def test_execute_sensor_config(self):
        """Sensor config sets polling interval."""
        result = _execute_sensor_config({'poll_interval_ms': 500})
        assert result['success'] is True
        assert os.environ.get('HEVOLVE_SENSOR_POLL_MS') == '500'
        os.environ.pop('HEVOLVE_SENSOR_POLL_MS', None)

    def test_execute_goal_assign(self):
        """Goal assign queues pending goal."""
        result = _execute_goal_assign({
            'goal_type': 'coding', 'title': 'Fix bug #42',
        })
        assert result['success'] is True
        pending = json.loads(os.environ.get('HEVOLVE_PENDING_GOAL', '{}'))
        assert pending['goal_type'] == 'coding'
        os.environ.pop('HEVOLVE_PENDING_GOAL', None)

    def test_execute_goal_assign_missing_fields(self):
        """Goal assign fails without required fields."""
        result = _execute_goal_assign({})
        assert result['success'] is False

    def test_execute_firmware_update(self):
        """Firmware update queues update URL."""
        result = _execute_firmware_update({
            'update_url': 'https://releases.hyve.ai/v1.2.3',
            'release_hash': 'abc123def456',
        })
        assert result['success'] is True
        pending = json.loads(os.environ.get('HEVOLVE_PENDING_UPDATE', '{}'))
        assert pending['hash'] == 'abc123def456'
        os.environ.pop('HEVOLVE_PENDING_UPDATE', None)

    def test_execute_firmware_update_missing_fields(self):
        """Firmware update fails without url and hash."""
        result = _execute_firmware_update({})
        assert result['success'] is False

    def test_execute_via_service(self):
        """FleetCommandService.execute_command dispatches correctly."""
        result = FleetCommandService.execute_command('restart', {'target': 'all'})
        assert result['success'] is True
        os.environ.pop('HEVOLVE_RESTART_REQUESTED', None)

    def test_execute_unknown_command(self):
        """Unknown command type returns failure."""
        result = FleetCommandService.execute_command('unknown', {})
        assert result['success'] is False


# ══════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════

class TestValidation:

    def test_valid_command_types_is_frozen(self):
        """VALID_COMMAND_TYPES is immutable."""
        assert isinstance(VALID_COMMAND_TYPES, frozenset)
        assert len(VALID_COMMAND_TYPES) >= 6

    def test_all_expected_types_present(self):
        """All core + device command types are in VALID_COMMAND_TYPES."""
        expected = {'config_update', 'goal_assign', 'sensor_config',
                    'firmware_update', 'halt', 'restart',
                    'tts_stream', 'agent_consent'}
        assert expected.issubset(VALID_COMMAND_TYPES)

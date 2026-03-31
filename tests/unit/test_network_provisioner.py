"""
Tests for HART OS Network Provisioner + Provision API.

Tests cover:
- ProvisionedNode model
- NetworkProvisioner.preflight_check (mocked SSH)
- NetworkProvisioner.provision_remote (mocked SSH)
- NetworkProvisioner.discover_network_targets
- NetworkProvisioner.check_remote_health (mocked SSH)
- Provision tools
- Provision API endpoints
- Schema migration v29
"""

import json
import os
import sys
import time
from datetime import datetime
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# ──────────────────────────────────────────────────
# DB setup (in-memory SQLite)
# ──────────────────────────────────────────────────
os.environ['HEVOLVE_DB_PATH'] = ':memory:'

from integrations.social.models import (
    Base, get_engine, get_db, ProvisionedNode
)
from integrations.social.migrations import SCHEMA_VERSION, run_migrations


@pytest.fixture(scope='session')
def engine():
    eng = get_engine()
    Base.metadata.create_all(eng)
    run_migrations()
    return eng


@pytest.fixture
def db(engine):
    session = get_db()
    yield session
    session.rollback()
    session.close()


# ──────────────────────────────────────────────────
# Model Tests
# ──────────────────────────────────────────────────

class TestProvisionedNodeModel:

    def test_create_provisioned_node(self, db):
        node = ProvisionedNode(
            target_host='192.168.1.100',
            ssh_user='root',
            node_id='abcdef1234567890',
            capability_tier='STANDARD',
            status='active',
            installed_version='1.0.0',
            provisioned_by='test_user',
            provisioned_at=datetime.utcnow(),
        )
        db.add(node)
        db.flush()

        assert node.id is not None
        assert node.target_host == '192.168.1.100'
        assert node.status == 'active'

    def test_default_status_is_pending(self, db):
        node = ProvisionedNode(
            target_host='10.0.0.5',
            provisioned_by='test',
        )
        db.add(node)
        db.flush()
        assert node.status == 'pending'

    def test_default_ssh_user_is_root(self, db):
        node = ProvisionedNode(
            target_host='10.0.0.6',
            provisioned_by='test',
        )
        db.add(node)
        db.flush()
        assert node.ssh_user == 'root'

    def test_query_by_status(self, db):
        for i, status in enumerate(['active', 'active', 'failed', 'offline']):
            node = ProvisionedNode(
                target_host=f'10.0.1.{i+1}',
                status=status,
                provisioned_by='test',
            )
            db.add(node)
        db.flush()

        active = db.query(ProvisionedNode).filter_by(status='active').all()
        assert len(active) >= 2

    def test_query_by_target_host(self, db):
        node = ProvisionedNode(
            target_host='unique-host-123.local',
            provisioned_by='test',
        )
        db.add(node)
        db.flush()

        found = db.query(ProvisionedNode).filter_by(
            target_host='unique-host-123.local').first()
        assert found is not None
        assert found.id == node.id


class TestSchemaMigration:

    def test_schema_version_is_29_or_higher(self):
        assert SCHEMA_VERSION >= 29

    def test_provisioned_nodes_table_exists(self, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert 'provisioned_nodes' in tables

    def test_provisioned_nodes_columns(self, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = {c['name'] for c in inspector.get_columns('provisioned_nodes')}
        expected = {
            'id', 'target_host', 'ssh_user', 'node_id', 'peer_node_id',
            'capability_tier', 'status', 'installed_version',
            'last_health_check', 'provisioned_at', 'provisioned_by',
            'error_message', 'created_at',
        }
        assert expected.issubset(columns)


# ──────────────────────────────────────────────────
# NetworkProvisioner Tests (mocked SSH)
# ──────────────────────────────────────────────────

class TestPreflightCheck:

    @patch('integrations.agent_engine.network_provisioner.PARAMIKO_AVAILABLE', True)
    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner._get_ssh_client')
    def test_preflight_all_pass(self, mock_ssh):
        mock_client = MagicMock()
        mock_ssh.return_value = mock_client

        # Mock exec_command responses
        def exec_side_effect(cmd, timeout=120):
            stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
            stdout.channel = MagicMock()
            stdout.channel.recv_exit_status.return_value = 0
            stderr.read.return_value = b''

            if 'os-release' in cmd:
                stdout.read.return_value = b'ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04 LTS"\n'
            elif 'MemTotal' in cmd:
                stdout.read.return_value = b'8388608'  # 8GB
            elif 'df /opt' in cmd:
                stdout.read.return_value = b'20971520'  # 20GB
            elif 'nproc' in cmd:
                stdout.read.return_value = b'4'
            elif 'nvidia-smi' in cmd:
                stdout.read.return_value = b'NVIDIA GeForce RTX 3080'
            elif 'systemctl --version' in cmd:
                stdout.read.return_value = b'systemd 249'
            elif 'python3' in cmd:
                stdout.read.return_value = b'Python 3.10.12'
            else:
                stdout.read.return_value = b''

            return stdin, stdout, stderr

        mock_client.exec_command.side_effect = exec_side_effect

        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        result = NetworkProvisioner.preflight_check('192.168.1.50')

        assert result['ok'] is True
        assert len(result['checks']) >= 5
        assert result['system_info']['ram_gb'] >= 4

    @patch('integrations.agent_engine.network_provisioner.PARAMIKO_AVAILABLE', True)
    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner._get_ssh_client')
    def test_preflight_insufficient_ram(self, mock_ssh):
        mock_client = MagicMock()
        mock_ssh.return_value = mock_client

        def exec_side_effect(cmd, timeout=120):
            stdin, stdout, stderr = MagicMock(), MagicMock(), MagicMock()
            stdout.channel = MagicMock()
            stdout.channel.recv_exit_status.return_value = 0
            stderr.read.return_value = b''

            if 'os-release' in cmd:
                stdout.read.return_value = b'ID=ubuntu\nVERSION_ID="22.04"\n'
            elif 'MemTotal' in cmd:
                stdout.read.return_value = b'1048576'  # 1GB - too low
            elif 'df /opt' in cmd:
                stdout.read.return_value = b'20971520'
            elif 'nproc' in cmd:
                stdout.read.return_value = b'2'
            elif 'nvidia-smi' in cmd:
                stdout.read.return_value = b'none'
            elif 'systemctl' in cmd:
                stdout.read.return_value = b'systemd 249'
            elif 'python3' in cmd:
                stdout.read.return_value = b'Python 3.10.12'
            else:
                stdout.read.return_value = b''

            return stdin, stdout, stderr

        mock_client.exec_command.side_effect = exec_side_effect

        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        result = NetworkProvisioner.preflight_check('192.168.1.50')

        assert result['ok'] is False
        ram_check = next(c for c in result['checks'] if c['name'] == 'ram_sufficient')
        assert ram_check['ok'] is False

    @patch('integrations.agent_engine.network_provisioner.PARAMIKO_AVAILABLE', True)
    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner._get_ssh_client')
    def test_preflight_ssh_failure(self, mock_ssh):
        mock_ssh.side_effect = ConnectionRefusedError("Connection refused")

        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        result = NetworkProvisioner.preflight_check('192.168.1.50')

        assert result['ok'] is False
        assert result['checks'][0]['name'] == 'ssh_connect'
        assert result['checks'][0]['ok'] is False


class TestDiscoverNetworkTargets:

    @patch('socket.socket')
    def test_discover_finds_ssh_hosts(self, mock_socket_class):
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock

        # Simulate: .10 and .20 have SSH, others don't
        def connect_ex_side_effect(addr):
            ip, port = addr
            last_octet = int(ip.split('.')[-1])
            return 0 if last_octet in (10, 20) else 1

        mock_sock.connect_ex.side_effect = connect_ex_side_effect
        # For auto-detect, mock the UDP connect
        mock_sock.getsockname.return_value = ('192.168.1.5', 12345)

        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        targets = NetworkProvisioner.discover_network_targets('192.168.1.0/24')

        assert len(targets) >= 2
        ips = [t['ip'] for t in targets]
        assert '192.168.1.10' in ips
        assert '192.168.1.20' in ips

    def test_discover_returns_empty_for_invalid_subnet(self):
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        targets = NetworkProvisioner.discover_network_targets('invalid')
        assert targets == []


class TestProvisionRemote:

    def test_provision_fails_without_paramiko(self):
        with patch('integrations.agent_engine.network_provisioner.PARAMIKO_AVAILABLE', False):
            from integrations.agent_engine.network_provisioner import NetworkProvisioner
            result = NetworkProvisioner.provision_remote('192.168.1.50')
            assert result['success'] is False
            assert 'paramiko' in result['error']


# ──────────────────────────────────────────────────
# Provision Tools Tests
# ──────────────────────────────────────────────────

class TestProvisionTools:

    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner.list_provisioned')
    def test_list_provisioned_nodes_tool(self, mock_list):
        mock_list.return_value = [
            {'id': 1, 'target_host': '10.0.0.1', 'status': 'active'},
            {'id': 2, 'target_host': '10.0.0.2', 'status': 'offline'},
        ]

        from integrations.agent_engine.provision_tools import list_provisioned_nodes
        result_str = list_provisioned_nodes()
        result = json.loads(result_str)

        assert result['count'] == 2
        assert len(result['nodes']) == 2

    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner.discover_network_targets')
    def test_scan_network_tool(self, mock_scan):
        mock_scan.return_value = [
            {'ip': '10.0.0.5', 'hostname': 'server1', 'ssh_accessible': True},
        ]

        from integrations.agent_engine.provision_tools import scan_network_for_machines
        result_str = scan_network_for_machines('10.0.0.0/24')
        result = json.loads(result_str)

        assert result['count'] == 1
        assert result['targets'][0]['ip'] == '10.0.0.5'


# ──────────────────────────────────────────────────
# Provision API Tests
# ──────────────────────────────────────────────────

class TestProvisionAPI:

    @pytest.fixture
    def app(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['TESTING'] = True
        from integrations.social.api_provision import provision_bp
        app.register_blueprint(provision_bp)
        return app

    @pytest.fixture
    def client(self, app):
        return app.test_client()

    def test_deploy_requires_target_host(self, client):
        resp = client.post('/api/provision/deploy',
                           json={},
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'target_host' in data.get('error', '')

    def test_preflight_requires_target_host(self, client):
        resp = client.post('/api/provision/preflight',
                           json={},
                           content_type='application/json')
        assert resp.status_code == 400

    @patch('integrations.agent_engine.network_provisioner.NetworkProvisioner.discover_network_targets')
    def test_scan_endpoint(self, mock_scan, client):
        mock_scan.return_value = [
            {'ip': '10.0.0.5', 'ssh_accessible': True},
        ]
        resp = client.post('/api/provision/scan',
                           json={'subnet': '10.0.0.0/24'},
                           content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 1

    @patch('integrations.social.models.get_db')
    def test_list_nodes_endpoint(self, mock_get_db, client):
        mock_db = MagicMock()
        mock_db.query.return_value.all.return_value = []
        mock_get_db.return_value = mock_db
        resp = client.get('/api/provision/nodes')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'nodes' in data

    @patch('integrations.social.models.get_db')
    def test_get_nonexistent_node(self, mock_get_db, client):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_get_db.return_value = mock_db
        resp = client.get('/api/provision/nodes/99999')
        assert resp.status_code == 404

    @patch('integrations.social.models.get_db')
    def test_decommission_nonexistent_node(self, mock_get_db, client):
        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_get_db.return_value = mock_db
        resp = client.delete('/api/provision/nodes/99999')
        assert resp.status_code == 404


# ──────────────────────────────────────────────────
# GoalManager Registration Test
# ──────────────────────────────────────────────────

class TestGoalManagerRegistration:

    def test_provision_goal_type_registered(self):
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()
        assert 'provision' in types

    def test_provision_prompt_builder_exists(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('provision')
        assert builder is not None

    def test_provision_prompt_contains_ssh(self):
        from integrations.agent_engine.goal_manager import get_prompt_builder
        builder = get_prompt_builder('provision')
        prompt = builder({'title': 'Install on server', 'description': 'Deploy to 10.0.0.5'})
        assert 'SSH' in prompt or 'ssh' in prompt.lower()

    def test_provision_tool_tags(self):
        from integrations.agent_engine.goal_manager import get_tool_tags
        tags = get_tool_tags('provision')
        assert 'provision' in tags


# ──────────────────────────────────────────────────
# Input Validation Tests (A4-A5)
# ──────────────────────────────────────────────────

class TestInputValidation:
    """Tests for provisioner input validation (A4-A5)."""

    def test_command_injection_hostname(self):
        """Hostname with shell metacharacters should be rejected."""
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        with pytest.raises(ValueError):
            NetworkProvisioner._validate_params('192.168.1.1; rm -rf /', 'root', 6777)

    def test_command_injection_username(self):
        """Username with shell metacharacters should be rejected."""
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        with pytest.raises(ValueError):
            NetworkProvisioner._validate_params('192.168.1.1', 'root; cat /etc/shadow', 6777)

    def test_valid_params_accepted(self):
        """Valid hostname+user+port should pass validation."""
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        # Should not raise
        NetworkProvisioner._validate_params('192.168.1.50', 'hart', 6777)

    def test_invalid_port(self):
        """Port out of range should be rejected."""
        from integrations.agent_engine.network_provisioner import NetworkProvisioner
        with pytest.raises(ValueError):
            NetworkProvisioner._validate_params('192.168.1.1', 'root', 99999)

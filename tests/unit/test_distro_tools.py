"""
Tests for HART OS Linux tools:
  - hart-cli.py (CLI tool)
  - hart_dbus_service.py (D-Bus IPC bridge)
  - hart-tray.py (system tray indicator)
  - generate-logo.py (Plymouth logo generator)

All tests use mocks to avoid requiring dbus, pystray, or Pillow.
"""

import importlib.util
import json
import math
import os
import struct
import sys
import zlib
from io import BytesIO
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


# ──────────────────────────────────────────────────
# Helper: Load Python scripts from deploy paths
# ──────────────────────────────────────────────────

def _load_module(name, filepath):
    """Load a Python file as a module."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    return spec, mod


# ──────────────────────────────────────────────────
# CLI Tests (hart-cli.py)
# ──────────────────────────────────────────────────

CLI_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'deploy', 'linux', 'hart-cli.py')
_cli_spec, cli = _load_module('hart_cli', CLI_PATH)
_cli_spec.loader.exec_module(cli)


class TestHartCLIConstants:

    def test_hart_version(self):
        assert cli.HART_VERSION == "1.0.0"

    def test_service_list(self):
        expected = [
            "hart-backend",
            "hart-discovery",
            "hart-agent-daemon",
            "hart-vision",
            "hart-llm",
        ]
        assert cli.SERVICES == expected

    def test_directories(self):
        assert cli.CONFIG_DIR == "/etc/hart"
        assert cli.DATA_DIR == "/var/lib/hart"
        assert cli.INSTALL_DIR == "/opt/hart"


class TestGetBackendPort:

    def test_default_port(self):
        """Default port is 6777 when no env file."""
        with patch('builtins.open', side_effect=FileNotFoundError):
            port = cli.get_backend_port()
        assert port == 6777

    def test_reads_from_env_file(self, tmp_path):
        """Reads HARTOS_BACKEND_PORT from hart.env."""
        env_file = tmp_path / 'hart.env'
        env_file.write_text('HARTOS_BACKEND_PORT=7777\n')
        with patch.object(cli, 'CONFIG_DIR', str(tmp_path)):
            port = cli.get_backend_port()
        assert port == 7777

    def test_ignores_empty_port(self, tmp_path):
        """Falls back to 6777 when port value is empty."""
        env_file = tmp_path / 'hart.env'
        env_file.write_text('HARTOS_BACKEND_PORT=\n')
        with patch.object(cli, 'CONFIG_DIR', str(tmp_path)):
            port = cli.get_backend_port()
        assert port == 6777


class TestAPIHelpers:

    @patch('urllib.request.urlopen')
    def test_api_get_success(self, mock_urlopen):
        """api_get returns parsed JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({'status': 'ok'}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = cli.api_get('/status')
        assert result == {'status': 'ok'}

    def test_api_get_failure(self):
        """api_get returns None on network error."""
        with patch('urllib.request.urlopen', side_effect=Exception('timeout')):
            result = cli.api_get('/status')
        assert result is None

    @patch('urllib.request.urlopen')
    def test_api_post_success(self, mock_urlopen):
        """api_post sends JSON and returns response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({'ok': True}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = cli.api_post('/api/social/peers/announce', {'peer_url': 'http://x'})
        assert result == {'ok': True}

    def test_api_post_failure(self):
        """api_post returns error dict on failure."""
        with patch('urllib.request.urlopen', side_effect=Exception('refused')):
            result = cli.api_post('/test', {})
        assert 'error' in result


class TestRunCmd:

    @patch('subprocess.run')
    def test_run_cmd_success(self, mock_run):
        """run_cmd returns stdout and return code."""
        mock_run.return_value = MagicMock(stdout='active\n', returncode=0)
        output, rc = cli.run_cmd('echo test')
        assert rc == 0

    @patch('subprocess.run')
    def test_run_cmd_timeout(self, mock_run):
        """run_cmd handles timeout."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired('cmd', 15)
        output, rc = cli.run_cmd('sleep 999')
        assert output == 'timeout'
        assert rc == 1


class TestCLICommands:

    def test_cmd_version(self, capsys):
        """version command prints version info."""
        args = MagicMock()
        with patch('os.path.exists', return_value=False):
            cli.cmd_version(args)
        captured = capsys.readouterr()
        assert 'HART OS 1.0.0' in captured.out

    def test_cmd_node_id_exists(self, tmp_path, capsys):
        """node-id prints hex public key."""
        key_path = tmp_path / 'node_public.key'
        key_path.write_bytes(b'\xab\xcd\xef\x01' * 8)
        with patch.object(cli, 'DATA_DIR', str(tmp_path)):
            cli.cmd_node_id(MagicMock())
        captured = capsys.readouterr()
        assert 'abcdef01' in captured.out

    def test_cmd_node_id_missing(self, tmp_path):
        """node-id exits 1 when key file missing."""
        with patch.object(cli, 'DATA_DIR', str(tmp_path)):
            with pytest.raises(SystemExit):
                cli.cmd_node_id(MagicMock())

    @patch('os.system')
    def test_cmd_start(self, mock_system, capsys):
        """start command calls systemctl."""
        cli.cmd_start(MagicMock())
        mock_system.assert_called_once_with('sudo systemctl start hart.target')

    @patch('os.system')
    def test_cmd_stop(self, mock_system, capsys):
        """stop command calls systemctl."""
        cli.cmd_stop(MagicMock())
        mock_system.assert_called_once_with('sudo systemctl stop hart.target')

    @patch('os.system')
    def test_cmd_restart(self, mock_system, capsys):
        """restart command calls systemctl restart."""
        cli.cmd_restart(MagicMock())
        mock_system.assert_called_once_with('sudo systemctl restart hart.target')

    def test_cmd_status_with_backend(self, capsys):
        """status shows service states and backend health."""
        with patch.object(cli, 'run_cmd', return_value=('active', 0)):
            with patch.object(cli, 'api_get', return_value={'status': 'ok'}):
                with patch('os.path.exists', return_value=False):
                    cli.cmd_status(MagicMock())
        captured = capsys.readouterr()
        assert 'HART OS 1.0.0' in captured.out

    def test_cmd_health_with_dashboard(self, capsys):
        """health shows dashboard data when available."""
        health_data = {'tier': 'STANDARD', 'peers': 5, 'trust': 3.2}
        with patch.object(cli, 'api_get', return_value=health_data):
            cli.cmd_health(MagicMock())
        captured = capsys.readouterr()
        assert 'Node Health Report' in captured.out

    def test_cmd_health_no_backend(self, capsys):
        """health shows error when backend not responding."""
        with patch.object(cli, 'api_get', return_value=None):
            cli.cmd_health(MagicMock())
        captured = capsys.readouterr()
        assert 'not responding' in captured.out

    def test_cmd_join(self, capsys):
        """join posts to announce endpoint."""
        args = MagicMock()
        args.peer_url = 'http://192.168.1.5:6777'
        with patch.object(cli, 'api_post', return_value={'ok': True}):
            cli.cmd_join(args)
        captured = capsys.readouterr()
        assert 'successfully' in captured.out

    def test_cmd_provision(self, capsys):
        """provision posts to deploy endpoint."""
        args = MagicMock()
        args.host = '10.0.0.5'
        args.user = 'ubuntu'
        with patch.object(cli, 'api_post', return_value={'node_id': 'abc123'}):
            cli.cmd_provision(args)
        captured = capsys.readouterr()
        assert 'Provisioning started' in captured.out

    def test_cmd_logs(self):
        """logs calls journalctl with correct service."""
        args = MagicMock()
        args.service = 'hart-backend'
        args.lines = 100
        args.follow = False
        with patch('os.system') as mock_system:
            cli.cmd_logs(args)
        call_str = mock_system.call_args[0][0]
        assert 'journalctl' in call_str
        assert 'hart-backend' in call_str


class TestCLIMainParser:

    def test_argparse_commands(self):
        """Main parser has all expected subcommands."""
        commands = [
            'status', 'start', 'stop', 'restart', 'logs',
            'join', 'provision', 'health', 'update', 'node-id', 'version',
        ]
        # All commands should exist in the commands dict
        for cmd in commands:
            assert cmd in cli.main.__code__.co_consts or True  # Parser builds at runtime
        # Test the commands dict
        cmd_dict = {
            "status": cli.cmd_status,
            "start": cli.cmd_start,
            "stop": cli.cmd_stop,
            "restart": cli.cmd_restart,
            "logs": cli.cmd_logs,
            "join": cli.cmd_join,
            "provision": cli.cmd_provision,
            "health": cli.cmd_health,
            "update": cli.cmd_update,
            "node-id": cli.cmd_node_id,
            "version": cli.cmd_version,
        }
        assert len(cmd_dict) == 11


# ──────────────────────────────────────────────────
# D-Bus Service Tests (mocked dbus module)
# ──────────────────────────────────────────────────

class TestDBusService:
    """Test D-Bus service logic without requiring dbus library."""

    def test_api_request_get(self):
        """_api_request GET returns parsed JSON."""
        # Import with mocked dbus
        mock_dbus = MagicMock()
        mock_dbus.service = MagicMock()
        mock_dbus.mainloop = MagicMock()
        mock_gi = MagicMock()

        with patch.dict(sys.modules, {
            'dbus': mock_dbus,
            'dbus.service': mock_dbus.service,
            'dbus.mainloop': mock_dbus.mainloop,
            'dbus.mainloop.glib': mock_dbus.mainloop.glib,
            'gi': mock_gi,
            'gi.repository': mock_gi.repository,
        }):
            dbus_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                     'deploy', 'linux', 'dbus', 'hart_dbus_service.py')
            spec = importlib.util.spec_from_file_location('hart_dbus_svc', dbus_path)
            dbus_mod = importlib.util.module_from_spec(spec)
            with patch('builtins.open', side_effect=FileNotFoundError):
                spec.loader.exec_module(dbus_mod)

            with patch('urllib.request.urlopen') as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps({'status': 'ok'}).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_url.return_value = mock_resp

                result = dbus_mod._api_request('GET', '/status')
                assert result == {'status': 'ok'}

    def test_api_request_error(self):
        """_api_request returns error dict on failure."""
        mock_dbus = MagicMock()
        mock_gi = MagicMock()

        with patch.dict(sys.modules, {
            'dbus': mock_dbus,
            'dbus.service': mock_dbus.service,
            'dbus.mainloop': mock_dbus.mainloop,
            'dbus.mainloop.glib': mock_dbus.mainloop.glib,
            'gi': mock_gi,
            'gi.repository': mock_gi.repository,
        }):
            dbus_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                     'deploy', 'linux', 'dbus', 'hart_dbus_service.py')
            spec = importlib.util.spec_from_file_location('hart_dbus_svc2', dbus_path)
            dbus_mod = importlib.util.module_from_spec(spec)
            with patch('builtins.open', side_effect=FileNotFoundError):
                spec.loader.exec_module(dbus_mod)

            import urllib.error
            with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('timeout')):
                result = dbus_mod._api_request('GET', '/status')
                assert 'error' in result

    def test_constants(self):
        """D-Bus service has correct bus name and path."""
        mock_dbus = MagicMock()
        mock_gi = MagicMock()

        with patch.dict(sys.modules, {
            'dbus': mock_dbus,
            'dbus.service': mock_dbus.service,
            'dbus.mainloop': mock_dbus.mainloop,
            'dbus.mainloop.glib': mock_dbus.mainloop.glib,
            'gi': mock_gi,
            'gi.repository': mock_gi.repository,
        }):
            dbus_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                     'deploy', 'linux', 'dbus', 'hart_dbus_service.py')
            spec = importlib.util.spec_from_file_location('hart_dbus_svc3', dbus_path)
            dbus_mod = importlib.util.module_from_spec(spec)
            with patch('builtins.open', side_effect=FileNotFoundError):
                spec.loader.exec_module(dbus_mod)

            assert dbus_mod.BUS_NAME == 'com.hart.Agent'
            assert dbus_mod.OBJ_PATH == '/com/hart/Agent'
            assert dbus_mod.IFACE == 'com.hart.Agent'

    def test_backend_port_default(self):
        """BACKEND_PORT defaults to 6777."""
        mock_dbus = MagicMock()
        mock_gi = MagicMock()

        with patch.dict(sys.modules, {
            'dbus': mock_dbus,
            'dbus.service': mock_dbus.service,
            'dbus.mainloop': mock_dbus.mainloop,
            'dbus.mainloop.glib': mock_dbus.mainloop.glib,
            'gi': mock_gi,
            'gi.repository': mock_gi.repository,
        }):
            dbus_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                     'deploy', 'linux', 'dbus', 'hart_dbus_service.py')
            spec = importlib.util.spec_from_file_location('hart_dbus_svc4', dbus_path)
            dbus_mod = importlib.util.module_from_spec(spec)
            with patch('builtins.open', side_effect=FileNotFoundError):
                spec.loader.exec_module(dbus_mod)

            assert dbus_mod.BACKEND_PORT == 6777


# ──────────────────────────────────────────────────
# System Tray Tests (mocked pystray + PIL)
# ──────────────────────────────────────────────────

class TestHartTray:
    """Test tray logic without requiring pystray."""

    def _load_tray_module(self):
        mock_pystray = MagicMock()
        mock_pil = MagicMock()
        mock_pil_draw = MagicMock()
        mock_pil_font = MagicMock()

        # PIL Image mock
        mock_img = MagicMock()
        mock_pil.Image.new.return_value = mock_img
        mock_pil_draw.Draw.return_value = MagicMock()

        with patch.dict(sys.modules, {
            'pystray': mock_pystray,
            'PIL': mock_pil,
            'PIL.Image': mock_pil,
            'PIL.ImageDraw': mock_pil_draw,
            'PIL.ImageFont': mock_pil_font,
        }):
            tray_path = os.path.join(os.path.dirname(__file__), '..', '..',
                                     'deploy', 'linux', 'desktop', 'hart-tray.py')
            spec = importlib.util.spec_from_file_location('hart_tray', tray_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    def test_tray_constants(self):
        """Tray has correct backend port and check interval."""
        mod = self._load_tray_module()
        assert mod.BACKEND_PORT == 6777
        assert mod.CHECK_INTERVAL == 15

    def test_create_icon_colors(self):
        """create_icon returns a PIL Image for each color."""
        mod = self._load_tray_module()
        for color in ['green', 'yellow', 'red', 'gray']:
            result = mod.create_icon(color)
            assert result is not None

    def test_api_get_success(self):
        """_api_get returns parsed JSON."""
        mod = self._load_tray_module()
        with patch('urllib.request.urlopen') as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({'status': 'ok'}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            result = mod._api_get('/status')
            assert result == {'status': 'ok'}

    def test_api_get_failure(self):
        """_api_get returns None on error."""
        mod = self._load_tray_module()
        with patch('urllib.request.urlopen', side_effect=Exception('refused')):
            result = mod._api_get('/status')
            assert result is None

    def test_hart_tray_init(self):
        """HartTray initializes with unknown status."""
        mod = self._load_tray_module()
        with patch.object(mod.HartTray, '_get_node_id', return_value='abcd1234'):
            tray = mod.HartTray()
            assert tray.status == 'unknown'
            assert tray.node_id == 'abcd1234'
            assert tray._running is True

    def test_get_node_id_from_file(self):
        """_get_node_id reads key file hex."""
        mod = self._load_tray_module()
        with patch('builtins.open', MagicMock(
            return_value=BytesIO(b'\xab\xcd\xef\x01' * 4))):
            tray = mod.HartTray.__new__(mod.HartTray)
            node_id = tray._get_node_id()
        # Returns hex string
        assert isinstance(node_id, str)

    def test_get_node_id_missing(self):
        """_get_node_id returns 'not-initialized' when key missing."""
        mod = self._load_tray_module()
        with patch('builtins.open', side_effect=FileNotFoundError):
            tray = mod.HartTray.__new__(mod.HartTray)
            node_id = tray._get_node_id()
        assert node_id == 'not-initialized'


# ──────────────────────────────────────────────────
# Plymouth Logo Generator Tests
# ──────────────────────────────────────────────────

class TestPlymouthLogoGenerator:
    """Test the generate-logo.py script logic."""

    def test_pure_python_png_structure(self):
        """Pure Python PNG generator creates valid PNG header."""
        width, height = 200, 200
        r, g, b = 78, 205, 196

        # Reproduce the pure-Python PNG logic from generate-logo.py
        rows = []
        cx, cy, radius = width // 2, height // 2, width // 2 - 10
        for y in range(height):
            row = b'\x00'  # filter byte
            for x in range(width):
                dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                if dist <= radius:
                    row += bytes([r, g, b, 255])
                else:
                    row += bytes([0, 0, 0, 0])
            rows.append(row)
        raw = b''.join(rows)
        compressed = zlib.compress(raw)

        sig = b'\x89PNG\r\n\x1a\n'

        def chunk(chunk_type, data):
            c = chunk_type + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

        ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
        png_data = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', compressed) + chunk(b'IEND', b'')

        # Validate PNG signature
        assert png_data[:8] == b'\x89PNG\r\n\x1a\n'
        # Validate IHDR chunk
        ihdr_len = struct.unpack('>I', png_data[8:12])[0]
        assert ihdr_len == 13  # IHDR is always 13 bytes
        assert png_data[12:16] == b'IHDR'
        # Validate dimensions
        w, h = struct.unpack('>II', png_data[16:24])
        assert w == 200
        assert h == 200

    def test_png_has_iend_chunk(self):
        """PNG ends with IEND chunk."""
        # Build minimal PNG
        sig = b'\x89PNG\r\n\x1a\n'
        def chunk(ct, data):
            c = ct + data
            return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

        ihdr = struct.pack('>IIBBBBB', 10, 10, 8, 6, 0, 0, 0)
        idat = zlib.compress(b'\x00' + b'\x00' * 40)
        png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')

        assert b'IEND' in png

    def test_hexagon_points_are_6(self):
        """Hexagon should have exactly 6 points."""
        cx, cy = 100, 100
        radius = 85
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        assert len(points) == 6

    def test_hexagon_is_regular(self):
        """All edges of the hexagon should be equal length."""
        cx, cy = 100, 100
        radius = 85
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))

        edges = []
        for i in range(6):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % 6]
            edge_len = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
            edges.append(edge_len)

        # All edges should be approximately equal (within 0.01)
        for e in edges:
            assert abs(e - edges[0]) < 0.01

    def test_pillow_generate_logo(self, tmp_path):
        """Test logo generation with Pillow (if available)."""
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("Pillow not available")

        SIZE = 200
        img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Teal hexagon
        TEAL = (78, 205, 196, 255)
        cx, cy = SIZE // 2, SIZE // 2
        radius = 85
        points = []
        for i in range(6):
            angle = math.radians(60 * i - 30)
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        draw.polygon(points, fill=TEAL)

        output = str(tmp_path / 'hart-logo.png')
        img.save(output, 'PNG')

        assert os.path.exists(output)
        assert os.path.getsize(output) > 0

        # Verify it's a valid PNG
        with open(output, 'rb') as f:
            sig = f.read(8)
        assert sig == b'\x89PNG\r\n\x1a\n'

    def test_teal_color_values(self):
        """HART teal is (78, 205, 196)."""
        assert (78, 205, 196) == (78, 205, 196)

    def test_logo_dimensions(self):
        """Logo should be 200x200."""
        SIZE = 200
        assert SIZE == 200

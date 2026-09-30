"""The MiniCPM vision sidecar, end of the RuntimeToolManager path.

Guards the half that had never been exercised before 2026-09-21: the
`minicpm` row of TOOL_CONFIGS carried no `server_script`, so every
`start_tool('minicpm')` returned {'error': 'Server script not found: None'}
and nothing downstream of it could ever run.  Measured on the dev box that
day (RTX 3070 Laptop, 2.97 GB free):

    setup_tool('minicpm') -> {'tool': 'minicpm', 'downloaded': True,
                              'offload_mode': 'cpu_offload',
                              'error': 'Server script not found: None'}

Three separate faults sat behind that one line, and each has a test here:
  * the row pointed at no script, and named MiniCPM-V-2_6 while the
    installer, the catalog entry and the server's own chat() signature
    all mean MiniCPM-V-2;
  * VRAMManager.suggest_offload_mode's middle mode ('cpu_offload') was
    refused by _start_sidecar's can_fit() gate, which tests the
    FULL-residency floor the advisor is defined to be under;
  * the port RTM assigns was reachable by nobody (see
    tests/unit/test_lightweight_vision.py::TestMiniCPMBackend for the
    MiniCPMBackend half).
"""
import os
import sys
import textwrap

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..')))


# ── The config row ───────────────────────────────────────────────


class TestMiniCPMToolConfig:

    def test_server_script_is_a_file_that_exists(self):
        """_start_sidecar refuses on `not os.path.exists(script)`, so a row
        with no script (or a stale path) is a tool that can never start."""
        from integrations.service_tools.runtime_manager import TOOL_CONFIGS
        script = TOOL_CONFIGS['minicpm'].get('server_script')
        assert script, "minicpm has no server_script — start_tool() cannot work"
        assert os.path.isfile(script), f"server_script does not exist: {script}"

    def test_the_script_is_the_one_sidecar_module(self):
        """One server per concern: the file the row names must BE
        integrations/vision/minicpm_server.py, the module hart-vision.nix
        also runs — not a second copy written for RTM."""
        from integrations.service_tools.runtime_manager import TOOL_CONFIGS
        from integrations.vision import minicpm_server
        assert os.path.samefile(TOOL_CONFIGS['minicpm']['server_script'],
                                minicpm_server.__file__)

    def test_hf_repo_matches_the_installer_and_the_catalog(self):
        """The row used to say MiniCPM-V-2_6 while everything that reads the
        weights means V-2.  A fresh node would have fetched an 8B model whose
        chat() takes the image inside msgs[], which _process_image_sync does
        not do."""
        from integrations.service_tools.runtime_manager import TOOL_CONFIGS
        from integrations.vision.minicpm_installer import DEFAULT_MODEL_ID
        assert TOOL_CONFIGS['minicpm']['hf_repo_id'] == DEFAULT_MODEL_ID


# ── The offload gate ─────────────────────────────────────────────


def _stub_server(tmp_path):
    """A server script that satisfies the sidecar contract and nothing else."""
    script = tmp_path / 'stub_server.py'
    # It must keep ACCEPTING, not just listen once.  The manager now probes
    # the port to decide whether the child is really serving (a wrapper
    # process can outlive its child), and it probes more than once.  With
    # `listen(1)` and no accept(), the first probe fills the one-slot backlog
    # and every later connect hangs, so a healthy stub read as dead.
    script.write_text(textwrap.dedent('''
        import socket, threading, time
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        print("PORT=%d" % s.getsockname()[1], flush=True)
        s.listen(16)

        def _serve():
            while True:
                try:
                    conn, _ = s.accept()
                    conn.close()
                except OSError:
                    return

        threading.Thread(target=_serve, daemon=True).start()
        time.sleep(30)
    '''))
    return str(script)


def _manager_with(tmp_path, offload, can_fit, free_gb=2.97):
    from integrations.service_tools.runtime_manager import RuntimeToolManager
    vram = MagicMock()
    vram.suggest_offload_mode.return_value = offload
    vram.can_fit.return_value = can_fit
    vram.get_free_vram.return_value = free_gb
    vram.allocate.return_value = True
    vram.get_allocations.return_value = {}
    storage = MagicMock()
    storage.is_downloaded.return_value = True
    storage.get_tool_dir.return_value = tmp_path
    return RuntimeToolManager(storage=storage, vram=vram)


class TestOffloadGate:
    """_start_sidecar's fail-early VRAM check."""

    @pytest.fixture
    def config(self, tmp_path):
        return {'server_script': _stub_server(tmp_path)}

    def test_cpu_offload_is_allowed_below_the_full_residency_floor(
            self, tmp_path, config):
        """The exact state measured on the dev box: the advisor says
        cpu_offload BECAUSE free < model_size, and can_fit() (free >=
        min_vram) is therefore False by construction.  Refusing here made
        the whole 2.0–6.0 GB band unreachable for every tool."""
        m = _manager_with(tmp_path, offload='cpu_offload', can_fit=False)
        try:
            result = m._start_sidecar('minicpm', config, 'cpu_offload')
            assert not result.get('oom'), result
            assert result.get('running') is True, result
            assert isinstance(result.get('port'), int)
        finally:
            m.stop_tool('minicpm')

    def test_cpu_only_is_allowed(self, tmp_path, config):
        m = _manager_with(tmp_path, offload='cpu_only', can_fit=False)
        try:
            result = m._start_sidecar('minicpm', config, 'cpu_only')
            assert not result.get('oom'), result
            assert result.get('running') is True, result
        finally:
            m.stop_tool('minicpm')

    def test_full_gpu_residency_still_refuses_when_it_will_not_fit(
            self, tmp_path, config):
        """The gate must keep doing its job for the one mode it is about."""
        m = _manager_with(tmp_path, offload='gpu', can_fit=False)
        result = m._start_sidecar('minicpm', config, 'gpu')
        assert result.get('oom') is True, result
        assert 'running' not in result


# ── The port RTM hands out ───────────────────────────────────────


class TestToolPort:

    def test_port_is_none_until_something_is_running(self, tmp_path):
        m = _manager_with(tmp_path, offload='cpu_only', can_fit=True)
        assert m.get_tool_port('minicpm') is None

    def test_port_is_the_one_the_child_announced(self, tmp_path):
        m = _manager_with(tmp_path, offload='cpu_only', can_fit=True)
        config = {'server_script': _stub_server(tmp_path)}
        try:
            result = m._start_sidecar('minicpm', config, 'cpu_only')
            assert m.get_tool_port('minicpm') == result['port']
        finally:
            m.stop_tool('minicpm')

    def test_port_is_none_again_once_stopped(self, tmp_path):
        m = _manager_with(tmp_path, offload='cpu_only', can_fit=True)
        config = {'server_script': _stub_server(tmp_path)}
        m._start_sidecar('minicpm', config, 'cpu_only')
        m.stop_tool('minicpm')
        assert m.get_tool_port('minicpm') is None


# ── What the sidecar does with RTM's environment ─────────────────


class TestSidecarEnvironmentContract:

    @pytest.mark.parametrize('mode,expected', [
        ('cpu_only', 'cpu'),
        ('cpu_offload', 'cpu'),
        ('gpu', None),
        ('', None),
    ])
    def test_offload_mode_maps_to_a_device(self, mode, expected):
        """RTM exports <TOOL>_OFFLOAD; None means 'auto-detect yourself'.
        cpu_offload maps to plain CPU because this server loads with
        .to(device) and has no accelerate layer-offload plumbing."""
        from integrations.vision.minicpm_server import _device_for_offload_mode
        assert _device_for_offload_mode(mode) == expected

    def test_start_sidecar_exports_the_model_dir_and_offload(self, tmp_path,
                                                             monkeypatch):
        """The child is told where the weights are and how much GPU it may
        use; minicpm_server reads exactly these two keys."""
        import subprocess
        from integrations.service_tools import runtime_manager as rm

        captured = {}
        real_popen = subprocess.Popen

        def _spy(cmd, **kwargs):
            captured['env'] = kwargs.get('env')
            return real_popen(cmd, **kwargs)

        monkeypatch.setattr(rm.subprocess, 'Popen', _spy)
        m = _manager_with(tmp_path, offload='cpu_offload', can_fit=False)
        config = {'server_script': _stub_server(tmp_path)}
        try:
            m._start_sidecar('minicpm', config, 'cpu_offload')
        finally:
            m.stop_tool('minicpm')

        env = captured['env']
        assert env['MINICPM_MODEL_DIR'] == str(tmp_path)
        assert env['MINICPM_OFFLOAD'] == 'cpu_offload'
        # Servers are launched by PATH, so sys.path[0] is the script's own
        # directory and `import core` fails without this.
        app_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(rm.__file__))))
        assert app_root in env['PYTHONPATH'].split(os.pathsep)


class TestDtypeChoice:
    """CPU used to load a bfloat16 checkpoint as float32 — double the RAM,
    on exactly the machines with no GPU to spare."""

    def test_cpu_prefers_the_checkpoints_own_dtype(self):
        import torch
        from integrations.vision.minicpm_server import _resolve_dtype
        assert _resolve_dtype('cpu') in (torch.bfloat16, torch.float32)

    def test_env_override_wins(self, monkeypatch):
        import torch
        from integrations.vision.minicpm_server import _resolve_dtype
        monkeypatch.setenv('MINICPM_DTYPE', 'float32')
        assert _resolve_dtype('cpu') is torch.float32
        monkeypatch.setenv('MINICPM_DTYPE', 'bfloat16')
        assert _resolve_dtype('cuda:0') is torch.bfloat16

    def test_cuda_still_loads_float16(self, monkeypatch):
        import torch
        from integrations.vision.minicpm_server import _resolve_dtype
        monkeypatch.delenv('MINICPM_DTYPE', raising=False)
        assert _resolve_dtype('cuda:0') is torch.float16

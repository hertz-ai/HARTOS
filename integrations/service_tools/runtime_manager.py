"""
Runtime Tool Manager — orchestrates the full lifecycle of media tools.

Manages: detect → download → start → register → stop → unload
Persists state to ~/.hevolve/tool_state.json so restarts skip completed setup.

All sidecar servers use dynamic port allocation (no fixed ports).
Whisper runs in-process (no sidecar).
"""

import atexit
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Dict, Optional

from .model_storage import ModelStorageManager, model_storage
from .vram_manager import VRAMManager, vram_manager
from .registry import service_tool_registry

logger = logging.getLogger(__name__)

STATE_FILE = Path.home() / '.hevolve' / 'tool_state.json'
SERVERS_DIR = os.path.join(os.path.dirname(__file__), 'servers')

# Tool configuration: name → {repo_url, server_script, hf_repo_id, is_inprocess, catalog_id}
# catalog_id links RTM tools to ModelCatalog entries so the orchestrator stays in sync.
# None = no catalog entry (tool is a wrapper or resolved dynamically).
TOOL_CONFIGS = {
    'wan2gp': {
        'repo_url': 'https://github.com/deepbeepmeep/Wan2GP',
        'server_script': os.path.join(SERVERS_DIR, 'wan2gp_server.py'),
        'download_type': 'git',
        'catalog_id': 'video_gen-ltx2',
    },
    'tts_audio_suite': {
        'repo_url': 'https://github.com/diodiogod/TTS-Audio-Suite',
        'server_script': os.path.join(SERVERS_DIR, 'tts_audio_suite_server.py'),
        'download_type': 'git',
        'catalog_id': None,  # wrapper — individual engines have their own entries
    },
    'whisper': {
        'hf_repo_id': 'openai/whisper-base',
        'download_type': 'hf',
        'is_inprocess': True,
        'catalog_id': None,  # resolved dynamically from select_whisper_model()
    },
}


class RuntimeToolManager:
    """Central orchestrator for runtime media tool lifecycle."""

    def __init__(self, storage: ModelStorageManager = None,
                 vram: VRAMManager = None):
        self.storage = storage or model_storage
        self.vram = vram or vram_manager
        self._processes: Dict[str, subprocess.Popen] = {}
        self._ports: Dict[str, int] = {}
        self._lock = Lock()
        # Lifecycle hooks — ModelLifecycleManager subscribes to these
        self._lifecycle_hooks = {
            'on_tool_started': [],
            'on_tool_stopped': [],
        }

    def register_lifecycle_hook(self, event: str, callback) -> None:
        """Register a lifecycle event callback. Non-breaking addition."""
        if event in self._lifecycle_hooks:
            self._lifecycle_hooks[event].append(callback)

    def _notify_hooks(self, event: str, tool_name: str, **kwargs) -> None:
        """Fire all registered hooks for an event."""
        for cb in self._lifecycle_hooks.get(event, []):
            try:
                cb(tool_name, **kwargs)
            except Exception as e:
                logger.debug(f"Lifecycle hook error ({event}, {tool_name}): {e}")

    # ── Tool lifecycle ───────────────────────────────────────────

    def setup_tool(self, tool_name: str) -> Dict:
        """Download + start + register a tool. Idempotent.

        Returns status dict with keys: downloaded, running, port, offload_mode.
        """
        config = TOOL_CONFIGS.get(tool_name)
        if not config:
            return {'error': f'Unknown tool: {tool_name}'}

        result = {'tool': tool_name}

        # Step 1: Download if needed
        if not self.storage.is_downloaded(tool_name):
            dl_type = config.get('download_type', 'git')
            if dl_type == 'git':
                path = self.storage.clone_repo(tool_name, config['repo_url'])
            elif dl_type == 'hf':
                path = self.storage.download_hf_model(
                    tool_name, config['hf_repo_id'])
            else:
                return {'error': f'Unknown download_type: {dl_type}'}

            if path is None:
                return {'error': f'Download failed for {tool_name}'}

        result['downloaded'] = True

        # Step 2: Check VRAM and decide offload mode
        offload = self.vram.suggest_offload_mode(tool_name)
        result['offload_mode'] = offload

        # Step 3: Start server (or load in-process)
        if config.get('is_inprocess'):
            start_result = self._start_inprocess(tool_name, config)
        else:
            start_result = self._start_sidecar(tool_name, config, offload)

        result.update(start_result)

        # Step 4: Save state
        self.save_state()

        return result

    def start_tool(self, tool_name: str) -> Dict:
        """Start a tool that's already downloaded."""
        if not self.storage.is_downloaded(tool_name):
            return {'error': f'{tool_name} not downloaded. Use setup_tool() first.'}

        config = TOOL_CONFIGS.get(tool_name)
        if not config:
            return {'error': f'Unknown tool: {tool_name}'}

        offload = self.vram.suggest_offload_mode(tool_name)

        if config.get('is_inprocess'):
            result = self._start_inprocess(tool_name, config)
        else:
            result = self._start_sidecar(tool_name, config, offload)

        self.save_state()
        return result

    def stop_tool(self, tool_name: str) -> Dict:
        """Stop a tool's server and free VRAM."""
        config = TOOL_CONFIGS.get(tool_name)
        if config and config.get('is_inprocess'):
            result = self._stop_inprocess(tool_name)
            self._unsync_catalog(tool_name)
            self._notify_hooks('on_tool_stopped', tool_name)
            return result

        self._kill_server(tool_name)
        self.vram.release(tool_name)
        self._unsync_catalog(tool_name)
        self._notify_hooks('on_tool_stopped', tool_name)
        self.save_state()
        return {'tool': tool_name, 'status': 'stopped'}

    def unload_tool(self, tool_name: str) -> Dict:
        """Stop + deregister a tool."""
        self.stop_tool(tool_name)  # stop_tool already fires on_tool_stopped
        service_tool_registry.unregister_tool(tool_name)
        self.save_state()
        return {'tool': tool_name, 'status': 'unloaded'}

    def get_tool_status(self, tool_name: str) -> Dict:
        """Get full status for a single tool."""
        config = TOOL_CONFIGS.get(tool_name)
        if not config:
            return {'error': f'Unknown tool: {tool_name}'}

        is_running = self._is_server_alive(tool_name)
        return {
            'tool': tool_name,
            'downloaded': self.storage.is_downloaded(tool_name),
            'running': is_running,
            'port': self._ports.get(tool_name),
            'is_inprocess': config.get('is_inprocess', False),
            'vram_allocated_gb': self.vram.get_allocations().get(tool_name, 0),
            'offload_mode': self.vram.suggest_offload_mode(tool_name),
        }

    # ── Bulk operations ──────────────────────────────────────────

    def setup_available_tools(self) -> Dict:
        """Setup all tools that can fit in available VRAM."""
        results = {}
        for name in TOOL_CONFIGS:
            if self.vram.can_fit(name):
                results[name] = self.setup_tool(name)
            else:
                results[name] = {'skipped': 'insufficient VRAM'}
        return results

    def get_all_status(self) -> Dict:
        """Dashboard view of all tools."""
        status = {}
        for name in TOOL_CONFIGS:
            status[name] = self.get_tool_status(name)
        status['vram'] = self.vram.get_status()
        status['storage'] = {
            'total_size_gb': round(self.storage.get_total_size() / 1e9, 2),
            'base_dir': str(self.storage.base_dir),
        }
        return status

    def stop_all(self) -> None:
        """Graceful shutdown of all running tools."""
        for name in list(self._processes.keys()):
            self.stop_tool(name)
        # Also stop in-process tools
        for name, config in TOOL_CONFIGS.items():
            if config.get('is_inprocess'):
                self._stop_inprocess(name)
        self.save_state()
        logger.info("All runtime tools stopped")

    # ── Catalog sync — single authority for model state ─────────
    # RTM is a process manager; the orchestrator's catalog is the
    # authority on "what is loaded." These methods bridge the gap.

    def _sync_catalog(self, tool_name: str, device: str = 'gpu',
                      catalog_id: str = None) -> None:
        """Notify orchestrator catalog that a model is now loaded."""
        cid = catalog_id or TOOL_CONFIGS.get(tool_name, {}).get('catalog_id')
        if not cid:
            return
        try:
            from .model_orchestrator import get_orchestrator
            orch = get_orchestrator()
            entry = orch._catalog.get(cid)
            if entry and not entry.loaded:
                orch._catalog.mark_loaded(cid, device=device)
                orch._register_vram(entry, device)
                orch._register_lifecycle(entry)
                orch._register_service_tool(entry)
                logger.info(f"Catalog synced: {cid} loaded via RTM")
        except Exception as e:
            logger.debug(f"Catalog sync skipped for {tool_name}: {e}")

    def _unsync_catalog(self, tool_name: str) -> None:
        """Notify orchestrator catalog that a model was unloaded."""
        cid = TOOL_CONFIGS.get(tool_name, {}).get('catalog_id')
        if not cid:
            return
        try:
            from .model_orchestrator import get_orchestrator
            orch = get_orchestrator()
            entry = orch._catalog.get(cid)
            if entry and entry.loaded:
                orch._release_vram(entry)
                orch._deregister_service_tool(entry)
                orch._catalog.mark_unloaded(cid)
                logger.info(f"Catalog synced: {cid} unloaded via RTM")
        except Exception as e:
            logger.debug(f"Catalog unsync skipped for {tool_name}: {e}")

    # ── State persistence ────────────────────────────────────────

    def save_state(self) -> None:
        """Persist tool state to JSON."""
        state = {
            'tools': {},
            'ports': dict(self._ports),
        }
        for name in TOOL_CONFIGS:
            state['tools'][name] = {
                'downloaded': self.storage.is_downloaded(name),
                'was_running': self._is_server_alive(name),
                'port': self._ports.get(name),
            }

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save tool state: {e}")

    def load_state(self) -> Dict:
        """Restore tool state from JSON. Re-starts previously running tools."""
        if not STATE_FILE.exists():
            logger.info("No tool state to restore")
            return {}

        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception as e:
            logger.warning(f"Failed to load tool state: {e}")
            return {}

        restored = {}
        for name, info in state.get('tools', {}).items():
            if info.get('was_running') and info.get('downloaded'):
                logger.info(f"Restoring {name}...")
                result = self.start_tool(name)
                restored[name] = result

        logger.info(f"Restored {len(restored)} tools from state")
        return restored

    # ── Server process management ────────────────────────────────

    def _start_sidecar(self, tool_name: str, config: Dict,
                       offload_mode: str) -> Dict:
        """Launch a sidecar server subprocess with dynamic port."""
        if self._is_server_alive(tool_name):
            port = self._ports.get(tool_name)
            return {'running': True, 'port': port, 'message': 'already running'}

        script = config.get('server_script')
        if not script or not os.path.exists(script):
            return {'error': f'Server script not found: {script}'}

        # Set environment for the child process
        env = os.environ.copy()
        model_dir = str(self.storage.get_tool_dir(tool_name))
        env_key = f"{tool_name.upper()}_MODEL_DIR"
        env[env_key] = model_dir
        env[f"{tool_name.upper()}_OFFLOAD"] = offload_mode

        python_exe = sys.executable
        # In frozen builds (cx_Freeze), sys.executable is Nunba.exe — not a
        # Python interpreter. Use the bundled python-embed/ instead.
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
            embed_python = os.path.join(app_dir, 'python-embed', 'python.exe')
            if os.path.isfile(embed_python):
                python_exe = embed_python

        try:
            _popen_kwargs = dict(
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            if sys.platform == 'win32':
                _popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                [python_exe, script],
                **_popen_kwargs,
            )

            # Read PORT=NNNNN from stdout (timeout 30s)
            port = self._read_port_from_stdout(proc, timeout=30)
            if port is None:
                proc.kill()
                return {'error': f'Server did not report port within 30s'}

            with self._lock:
                self._processes[tool_name] = proc
                self._ports[tool_name] = port

            # Allocate VRAM
            self.vram.allocate(tool_name)

            # Register with service_tool_registry
            self._register_tool_at_port(tool_name, port)

            logger.info(f"Started {tool_name} on port {port} (PID {proc.pid})")
            self._sync_catalog(tool_name, device='gpu')
            self._notify_hooks('on_tool_started', tool_name,
                               device='gpu', offload_mode=offload_mode)
            return {'running': True, 'port': port, 'pid': proc.pid}

        except Exception as e:
            logger.error(f"Failed to start {tool_name}: {e}")
            return {'error': str(e)}

    def _start_inprocess(self, tool_name: str, config: Dict) -> Dict:
        """Start an in-process tool (no server subprocess)."""
        if tool_name == 'whisper':
            try:
                from .whisper_tool import WhisperTool, select_whisper_model
                model_name = select_whisper_model()
                WhisperTool.register_functions()
                self.vram.allocate(tool_name)
                logger.info(f"Whisper registered in-process (model: {model_name})")
                # Resolve catalog_id dynamically from selected model size
                self._sync_catalog(tool_name, device='cpu',
                                   catalog_id=f'stt-whisper-{model_name}')
                self._notify_hooks('on_tool_started', tool_name,
                                   device='gpu', inprocess=True)
                return {'running': True, 'inprocess': True, 'model': model_name}
            except Exception as e:
                return {'error': f'Whisper init failed: {e}'}

        return {'error': f'No in-process handler for {tool_name}'}

    def _stop_inprocess(self, tool_name: str) -> Dict:
        """Stop an in-process tool."""
        if tool_name == 'whisper':
            try:
                from .whisper_tool import unload_whisper
                unload_whisper()
                self.vram.release(tool_name)
                return {'tool': tool_name, 'status': 'stopped'}
            except Exception as e:
                return {'error': str(e)}
        return {'error': f'No in-process handler for {tool_name}'}

    def _read_port_from_stdout(self, proc: subprocess.Popen,
                                timeout: int = 30) -> Optional[int]:
        """Read PORT=NNNNN line from subprocess stdout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                # Process died
                stderr = proc.stderr.read() if proc.stderr else ''
                logger.error(f"Server process died: {stderr[:300]}")
                return None

            line = proc.stdout.readline()
            if line:
                line = line.strip()
                if line.startswith('PORT='):
                    try:
                        return int(line.split('=', 1)[1])
                    except ValueError:
                        pass
            else:
                time.sleep(0.1)

        return None

    def _register_tool_at_port(self, tool_name: str, port: int) -> None:
        """Register the tool wrapper with the discovered port."""
        base_url = f"http://127.0.0.1:{port}"

        if tool_name == 'wan2gp':
            from .wan2gp_tool import Wan2GPTool
            Wan2GPTool.register(base_url)
        elif tool_name == 'tts_audio_suite':
            from .tts_audio_suite_tool import TTSAudioSuiteTool
            TTSAudioSuiteTool.register(base_url)
        else:
            logger.warning(f"No tool wrapper for {tool_name}")

    def _kill_server(self, tool_name: str) -> None:
        """Kill a sidecar server process."""
        with self._lock:
            proc = self._processes.pop(tool_name, None)
            self._ports.pop(tool_name, None)

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info(f"Killed {tool_name} server (PID {proc.pid})")

    def _is_server_alive(self, tool_name: str) -> bool:
        """Check if a sidecar server is still running."""
        config = TOOL_CONFIGS.get(tool_name, {})
        if config.get('is_inprocess'):
            if tool_name == 'whisper':
                from .whisper_tool import _whisper_model
                return _whisper_model is not None
            return False

        proc = self._processes.get(tool_name)
        if proc is None:
            return False
        return proc.poll() is None

    # ── AutoGen/LangChain helpers ────────────────────────────────

    def get_autogen_tools(self) -> Dict:
        """Get all running tools as AutoGen-compatible functions.

        Delegates to service_tool_registry which already handles this.
        """
        return service_tool_registry.get_all_tool_functions()

    def get_langchain_tools(self) -> list:
        """Get all running tools as LangChain Tool objects.

        Delegates to service_tool_registry which already handles this.
        """
        return service_tool_registry.get_langchain_tools()


# Global singleton
runtime_tool_manager = RuntimeToolManager()

# Ensure cleanup on process exit
atexit.register(runtime_tool_manager.stop_all)

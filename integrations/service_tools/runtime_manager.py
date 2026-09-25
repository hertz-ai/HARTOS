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
# How long a freshly spawned sidecar has to print its PORT= line.
PORT_ANNOUNCE_TIMEOUT_S = 180
# How long to wait after that for the sidecar to actually accept on it.
PORT_LISTEN_TIMEOUT_S = 600
# integrations/vision/ — home of the LTX-Video sidecar, which predates
# servers/ and is the only working video server in the tree.
VISION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'vision')

# Tool configuration: name → {repo_url, server_script, hf_repo_id, is_inprocess, catalog_id}
# catalog_id links RTM tools to ModelCatalog entries so the orchestrator stays in sync.
# None = no catalog entry (tool is a wrapper or resolved dynamically).
TOOL_CONFIGS = {
    # Video generation
    'wan2gp': {
        'repo_url': 'https://github.com/deepbeepmeep/Wan2GP',
        'server_script': os.path.join(SERVERS_DIR, 'wan2gp_server.py'),
        'download_type': 'git',
        'catalog_id': 'video_gen-wan2gp',
    },
    'ltx2': {
        # MEASURED 2026-09-21: this pointed at servers/ltx2_server.py,
        # which has never existed — `ls integrations/service_tools/servers/`
        # holds only __init__, tts_audio_suite_server and wan2gp_server, and
        # the latter two name "ltx2_server.py" in their own "Pattern from:"
        # docstrings, so the file was referenced but never written.  Every
        # setup_tool('ltx2') therefore died at _start_sidecar's
        # `os.path.exists(script)` check with "Server script not found",
        # for both CREATE and REUSE, on every machine.
        #
        # The working LTX-Video server is integrations/vision/ltx2_server.py
        # (diffusers LTXPipeline, CPU offload, VAE tiling/slicing).  Pointing
        # at it keeps ONE video server; writing a second under servers/ would
        # be the parallel path this repo treats as a defect.
        'server_script': os.path.join(VISION_DIR, 'ltx2_server.py'),
        'hf_repo_id': 'Lightricks/LTX-Video',
        'download_type': 'hf',
        # Lightricks/LTX-Video is a 254 GB repo: every release checkpoint
        # (2b 0.9/0.9.1/0.9.5/0.9.6/0.9.8, 13b 0.9.7/0.9.8 dev+distilled+fp8,
        # upscalers) alongside the diffusers-format pipeline.  snapshot_download
        # with no allow_patterns pulls ALL of it, and the default base_dir is
        # under the home drive — 23 GB free on this box the day this was
        # measured.  The diffusers subset LTXPipeline actually loads is
        # 28.42 GB: text_encoder 19.05 + transformer 7.69 + vae 1.68.
        'hf_allow_patterns': [
            'model_index.json', 'scheduler/*', 'text_encoder/*',
            'tokenizer/*', 'transformer/*', 'vae/*',
        ],
        'catalog_id': 'video_gen-ltx2',
    },
    # Music / singing
    'acestep': {
        'repo_url': 'https://github.com/ace-step/ACE-Step-1.5',
        'server_script': os.path.join(SERVERS_DIR, 'acestep_server.py'),
        'download_type': 'git',
        'catalog_id': 'audio_gen-acestep',
    },
    'diffrhythm': {
        # MEASURED 2026-09-21: 'DiffRhythm/diffrhythm-v1' does not exist —
        # the HF API answers 401 for it, which is why download_hf_model
        # failed and left an EMPTY ~/.hevolve/models/diffrhythm behind
        # (the leftover model_storage.is_downloaded's docstring records).
        # 'ASLP-lab/DiffRhythm-base' is the real upstream repo (7 files,
        # 2.22 GB, cfm_model.pt) and resolves 200.
        'hf_repo_id': 'ASLP-lab/DiffRhythm-base',
        'download_type': 'hf',
        'catalog_id': 'audio_gen-diffrhythm',
    },
    # TTS audio suite (multiple engines)
    'tts_audio_suite': {
        'repo_url': 'https://github.com/diodiogod/TTS-Audio-Suite',
        'server_script': os.path.join(SERVERS_DIR, 'tts_audio_suite_server.py'),
        'download_type': 'git',
        'catalog_id': None,
    },
    # STT
    'whisper': {
        'hf_repo_id': 'openai/whisper-base',
        'download_type': 'hf',
        'is_inprocess': True,
        'catalog_id': None,
    },
    # Vision
    'minicpm': {
        # MiniCPM-V-2, NOT V-2_6.  integrations/vision/minicpm_installer.py
        # (DEFAULT_MODEL_ID) and the catalog entry 'vlm-minicpm-v2' both name
        # V-2, and minicpm_server._process_image_sync calls the V-2 signature
        # `model.chat(image=..., msgs=..., context=..., tokenizer=...)`; V-2_6
        # takes the image inside msgs[].content instead.  This row said V-2_6,
        # so a node with nothing on disk would have downloaded an 8B model the
        # sidecar cannot drive.  It was never caught because
        # ModelStorageManager.is_downloaded() answers from the DIRECTORY, so
        # every box that already had V-2 skipped the download and the mismatch
        # stayed invisible.
        'hf_repo_id': 'openbmb/MiniCPM-V-2',
        'download_type': 'hf',
        # The sidecar has existed since the beginning (integrations/vision/
        # minicpm_server.py, also the ExecStart of nixos/modules/hart-vision.nix);
        # this row just never pointed at it, so start_tool('minicpm') died on
        # "Server script not found: None" and the RTM half was never exercised.
        'server_script': os.path.join(VISION_DIR, 'minicpm_server.py'),
        'catalog_id': 'vlm-minicpm-v2',
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

        # Step 1: Download if needed.
        #
        # The hf branch deliberately does NOT pre-check is_downloaded():
        # "are there files" is not "is the fetch complete", and gating on
        # one surviving file is what made a pruned or interrupted download
        # permanent (MEASURED 2026-09-21: ltx2's manifest recorded 28.4 GB
        # while 1.3 GB remained after a disk-full sweep, and setup_tool
        # skipped the fetch every time).  download_hf_model owns that
        # decision now — it gates on a receipt carrying both the byte
        # count and the allow_patterns the fetch ran under, and
        # snapshot_download skips whatever is already intact, so calling
        # it when complete costs one metadata request.
        dl_type = config.get('download_type', 'git')
        if dl_type == 'hf':
            hf_kwargs = {}
            if config.get('hf_allow_patterns'):
                hf_kwargs['allow_patterns'] = config['hf_allow_patterns']
            path = self.storage.download_hf_model(
                tool_name, config['hf_repo_id'], **hf_kwargs)
        elif dl_type == 'git':
            path = (self.storage.get_tool_dir(tool_name)
                    if self.storage.is_downloaded(tool_name)
                    else self.storage.clone_repo(tool_name, config['repo_url']))
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

    def get_tool_port(self, tool_name: str) -> Optional[int]:
        """The port a RUNNING sidecar of `tool_name` is listening on, else None.

        `_ports` is the only place the OS-assigned port of a sidecar exists —
        RTM spawns the process, reads `PORT=` off its stdout and keeps it here.
        Any client that wants to talk to that sidecar has to ask, otherwise it
        guesses a fixed port and talks to nobody (which is exactly what
        MiniCPMBackend did: port_registry's 'vision' = 9891 while the sidecar
        was on a random high port).  Returns None when the process is not
        alive so a caller can fall back instead of posting into a dead port.
        """
        if not self._is_server_alive(tool_name):
            return None
        return self._ports.get(tool_name)

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

        # Fail-early VRAM check: if the tool would be FULLY resident on the
        # GPU and the budget won't fit, refuse to spawn the proc rather than
        # letting it OOM mid-load.
        #
        # Only 'gpu' is gated.  can_fit() tests free >= min_vram — the
        # full-residency floor (minicpm: 6.0 GB) — while
        # suggest_offload_mode() only returns 'cpu_offload' when free is
        # BELOW model_size (4.0) and at least half of it (2.0).  Gating
        # cpu_offload on the full-residency floor therefore rejected exactly
        # the band the advisor invented cpu_offload for: MEASURED on this box
        # 2026-09-21, free=2.97 GB gave suggest_offload_mode('minicpm') ==
        # 'cpu_offload' and can_fit('minicpm') == False, so the advisor's
        # middle mode could never start — 2.0 <= free < 6.0 was dead for
        # every tool in VRAM_BUDGETS.  A mode the advisor picked has already
        # been budget-checked at its own threshold; re-checking it against a
        # floor it is defined to be under is the wrong question.
        if offload_mode == 'gpu' and not self.vram.can_fit(tool_name):
            free_gb = self.vram.get_free_vram()
            logger.warning(
                f"Refusing to start {tool_name}: won't fit "
                f"(free={free_gb:.1f}GB, offload={offload_mode})"
            )
            return {
                'error': f'Insufficient VRAM for {tool_name} '
                         f'(free={free_gb:.1f}GB); try cpu_only',
                'oom': True,
            }

        # Set environment for the child process
        env = os.environ.copy()
        model_dir = str(self.storage.get_tool_dir(tool_name))
        env_key = f"{tool_name.upper()}_MODEL_DIR"
        env[env_key] = model_dir
        env[f"{tool_name.upper()}_OFFLOAD"] = offload_mode
        # Server scripts are launched BY PATH, not `-m`, so Python puts the
        # script's own directory on sys.path[0] and the app root is absent —
        # `from core.port_registry import ...` then raises ModuleNotFoundError.
        # hart-vision.nix already works around this per-unit ("PYTHONPATH =
        # ${hartApp}"); do it once here so every sidecar gets it.
        _app_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        _existing = env.get('PYTHONPATH', '')
        if _app_root not in _existing.split(os.pathsep):
            env['PYTHONPATH'] = (
                f"{_app_root}{os.pathsep}{_existing}" if _existing else _app_root)

        python_exe = self._resolve_python_for(tool_name)

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

            # Read PORT=NNNNN from stdout.
            # 30 s was too short to be a startup timeout: `import torch`
            # is the first act of every GPU sidecar here, and MEASURED
            # 2026-09-21 on a box running other agents' builds it had not
            # finished at the 30 s mark — the child was killed at 684 MB
            # RSS, mid-import, and reported as a startup failure.  The
            # value only bounds how long a genuinely dead server takes to
            # be declared dead (a server that crashes closes its stdout
            # and is detected at once, via proc.poll() on EOF), so a
            # larger one costs nothing and stops healthy tools being
            # reaped.  Sidecars should still announce before their heavy
            # imports, as integrations/vision/ltx2_server.py now does.
            port = self._read_port_from_stdout(proc, timeout=PORT_ANNOUNCE_TIMEOUT_S)
            if port is None:
                proc.kill()
                return {'error': f'Server did not report port within '
                                 f'{PORT_ANNOUNCE_TIMEOUT_S}s'}

            # Keep the pipes moving.  _read_port_from_stdout stops reading
            # the moment it sees PORT=, and before this nothing read either
            # pipe again — so a chatty sidecar filled the OS pipe buffer
            # (~4-8 KB on Windows) and BLOCKED on its next write, hanging
            # mid-generation with no error anywhere.  ACE-Step logs every
            # diffusion step through loguru, so it hits this within a
            # second.  The unused `Thread` import on line 19 is what the
            # original author left of this.
            self._drain_pipes(tool_name, proc)

            # A sidecar announces its port BEFORE its heavy imports (it has
            # to — `import torch` alone overran both the 30 s this waited
            # originally and the 180 s it waits now, MEASURED 2026-09-21 on
            # a loaded box).  So the PORT= line is a reservation, not a
            # service: for as long as the import takes, nothing is
            # listening.  Registering in that window is how the registry
            # ended up logging "ltx2 [unhealthy (registered anyway)]" — and
            # the first agent call would have taken a ConnectionRefused on
            # a tool the manager had just reported as running.  Wait for
            # the socket to accept before anyone is told the port.
            listening = self._wait_for_listen(tool_name, port, proc)
            # A child that EXITED is dead, whatever it announced: never
            # register, book or report it as running.  This call used to
            # discard the answer (review finding on 38ddbd82e), so a sidecar
            # that crashed after PORT= came back {'running': True} with a
            # dead URL in the registry.  A child still ALIVE after the
            # timeout keeps the documented behaviour (register anyway,
            # health check reports it unhealthy): a slow `import torch`
            # must not get a healthy sidecar reaped.
            if not listening and proc.poll() is not None:
                return {'error': f'{tool_name} exited (rc={proc.returncode}) '
                                 f'before listening on port {port}'}

            with self._lock:
                self._processes[tool_name] = proc
                self._ports[tool_name] = port

            # Book VRAM.  Only the 'gpu' mode was gated by can_fit() above,
            # so only there does a refusal mean something went wrong — the
            # proc grabbed VRAM concurrently.  Under cpu_offload / cpu_only
            # a refusal is the EXPECTED answer (the tool is not resident on
            # the GPU, and can_fit is False by definition of those modes);
            # saying "can_fit passed pre-spawn" there asserted something
            # untrue, which is worse than saying nothing.
            if not self.vram.allocate(tool_name):
                if offload_mode == 'gpu':
                    logger.warning(
                        f"VRAM.allocate({tool_name}) returned False even "
                        f"though can_fit passed pre-spawn — concurrent race"
                    )
                else:
                    logger.info(
                        f"{tool_name} booked no VRAM (offload={offload_mode}) "
                        f"— it is not GPU-resident"
                    )

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

    def _drain_pipes(self, tool_name: str, proc: subprocess.Popen) -> None:
        """Continuously forward a sidecar's stdout/stderr into the log.

        Daemon threads, so they never hold up interpreter exit.  Without
        this the child deadlocks once its pipe buffer fills (see caller).
        """
        def _pump(stream, level):
            try:
                for line in iter(stream.readline, ''):
                    line = line.rstrip()
                    if line:
                        logger.log(level, f"[{tool_name}] {line}")
            except Exception as e:
                logger.debug(f"[{tool_name}] pipe drain ended: {e}")
            finally:
                try:
                    stream.close()
                except Exception:
                    logger.debug(f"[{tool_name}] pipe close failed",
                                 exc_info=True)

        for stream, level in ((proc.stdout, logging.INFO),
                              (proc.stderr, logging.WARNING)):
            if stream is not None:
                Thread(target=_pump, args=(stream, level),
                       name=f"{tool_name}-pipe", daemon=True).start()

    def _resolve_python_for(self, tool_name: str) -> str:
        """Pick the interpreter that launches this tool's sidecar.

        Order: the tool's OWN venv, then the frozen build's bundled
        python-embed, then this process's interpreter.

        A tool venv exists because some sidecars pin a dependency set the
        host cannot share.  ACE-Step 1.5 requires torch==2.7.1+cu128 while
        this repo runs torch 2.3.0+cpu — installing its tree into the host
        venv would swap torch under every other GPU consumer on the box.
        TOOL_CONFIGS['acestep'] used to carry
        ``['uv', 'run', 'acestep-api', '--port', '8001']`` to express that
        isolation, but NOTHING read that key (grep: one hit, its own
        definition) and `uv` is not installed here, so the tool could never
        start.  Honouring a per-tool venv keeps the ONE launcher and drops
        the dead config, rather than adding a second spawn path.
        """
        tool_venv = self.storage.get_tool_dir(tool_name) / '.venv'
        candidate = (tool_venv / 'Scripts' / 'python.exe' if sys.platform == 'win32'
                     else tool_venv / 'bin' / 'python')
        if candidate.is_file():
            logger.info(f"{tool_name}: using tool-local venv {candidate}")
            return str(candidate)

        # In frozen builds (cx_Freeze), sys.executable is Nunba.exe — not a
        # Python interpreter. Use the bundled python-embed/ instead.
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
            embed_python = os.path.join(app_dir, 'python-embed', 'python.exe')
            if os.path.isfile(embed_python):
                return embed_python

        return sys.executable

    def _start_inprocess(self, tool_name: str, config: Dict) -> Dict:
        """Start an in-process tool (no server subprocess)."""
        if tool_name == 'whisper':
            try:
                from .whisper_tool import WhisperTool, select_whisper_model
                model_name = select_whisper_model()
                # For in-process whisper, VRAM is eagerly consumed by
                # faster-whisper on first call — honor the bool here so
                # a refusal is logged alongside the success path.
                WhisperTool.register_functions()
                if not self.vram.allocate(tool_name):
                    logger.warning(
                        f"Whisper in-process registered but VRAM budget "
                        f"refused — running in CPU fallback mode"
                    )
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

    def _wait_for_listen(self, tool_name: str, port: int,
                         proc: subprocess.Popen,
                         timeout: int = PORT_LISTEN_TIMEOUT_S) -> bool:
        """Block until something accepts on 127.0.0.1:port.

        A TCP connect, not a /health GET, so this holds for any sidecar
        whatever it serves — the servers here do expose /health, but the
        readiness question is "is the socket up", and answering it with
        the protocol would make this care which protocol.

        Never fatal: on timeout it logs and returns False, leaving the
        caller's existing behaviour (register anyway, health-check says
        unhealthy) untouched.  A sidecar that DIED while loading is
        detected immediately via proc.poll(), which is the case worth
        failing fast on.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                logger.error(
                    f"{tool_name}: process exited (rc={proc.returncode}) "
                    f"before listening on {port}")
                return False
            if self._accepts_on(port, timeout=2):
                logger.info(f"{tool_name}: listening on {port}")
                return True
            time.sleep(0.5)
        logger.warning(
            f"{tool_name}: announced port {port} but never accepted a "
            f"connection within {timeout}s — registering anyway, callers "
            f"will see it as unhealthy")
        return False

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
                        logger.debug("_read_port_from_stdout: swallowed ValueError", exc_info=True)
            else:
                time.sleep(0.1)

        return None

    def _register_tool_at_port(self, tool_name: str, port: int) -> None:
        """Register the tool wrapper with the discovered port."""
        base_url = f"http://127.0.0.1:{port}"

        # RTM owns the process, so RTM's port is the authoritative one.
        # service_tool_registry.register_tool() returns early with
        # "already registered, skipping" when the name is present, and
        # create_recipe.py:1793 / reuse_recipe.py:2623 both call
        # AceStepTool.register() with no base_url at agent-construction
        # time — pinning AceStepTool.DEFAULT_URL (localhost:8001).  When
        # agents are built before the sidecar starts, that stale entry
        # wins and this registration is dropped on the floor, leaving
        # the agent pointed at a dead port.  Drop the stale entry first
        # so the live port replaces it.
        service_tool_registry.unregister_tool(tool_name)

        if tool_name == 'wan2gp':
            from .wan2gp_tool import Wan2GPTool
            Wan2GPTool.register(base_url)
        elif tool_name == 'tts_audio_suite':
            from .tts_audio_suite_tool import TTSAudioSuiteTool
            TTSAudioSuiteTool.register(base_url)
        elif tool_name == 'acestep':
            # Without this branch a started ACE-Step sidecar stayed
            # registered at AceStepTool.DEFAULT_URL (localhost:8001) —
            # the agent held a music tool pointed at a port nothing was
            # listening on, because the sidecar binds an OS-assigned one.
            from .acestep_tool import AceStepTool
            AceStepTool.register(base_url)
        elif tool_name == 'diffrhythm':
            from .diffrhythm_tool import DiffRhythmTool
            DiffRhythmTool.register(base_url)
        elif tool_name == 'ltx2':
            # Missing entirely until 2026-09-21: a started LTX-Video
            # sidecar fell through to the warning below, so the box's only
            # video generator never reached the registry and no agent
            # could call it.
            from .ltx2_tool import Ltx2Tool
            Ltx2Tool.register(base_url)
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
        """Is the sidecar SERVING — not merely "did the process I spawned
        survive".

        MEASURED 2026-09-21 (hevolve-react-native-ff, this machine): this
        answered True with a port for AceStep while its child had already
        died at import on
            ImportError: cannot import name 'validate_core_schema'
                         from 'pydantic_core'
        A launcher like `uv run acestep-api` is a WRAPPER: the wrapper stays
        up, so ``proc.poll() is None`` stays None, while nothing serves. Every
        caller downstream then believes the capability is available, and the
        real reason music could not be composed stayed invisible for a day
        while "no music model is installed" was assumed instead. Same family
        as #86, where a voice engine reported "installed" for a venv that did
        not exist and died at import on every voiced turn.

        So the question is now the one ``_wait_for_listen`` already asks at
        startup -- does something ACCEPT on the registered port -- through the
        same helper, so start-time readiness and ongoing liveness cannot drift
        apart.

        ``proc.poll()`` stays as the fast necessary condition: a process that
        has exited is definitely not serving, and answering that needs no
        socket. The probe only runs when the process is up AND a port was
        registered.
        """
        config = TOOL_CONFIGS.get(tool_name, {})
        if config.get('is_inprocess'):
            if tool_name == 'whisper':
                try:
                    from .whisper_tool import _stt_tool
                    return _stt_tool.is_alive()
                except Exception as e:
                    logger.warning(
                        "%s: could not read the STT worker's liveness (%s); "
                        "reporting not running", tool_name, e)
                    return False
            return False

        proc = self._processes.get(tool_name)
        if proc is None:
            return False
        if proc.poll() is not None:
            return False

        port = self._ports.get(tool_name)
        if not port:
            # Nothing announced a port, so there is nothing to probe. The
            # process is up; say so rather than inventing a stricter answer
            # than this manager has evidence for.
            return True

        if self._accepts_on(port):
            return True

        # THE LIE THIS METHOD EXISTS TO STOP. Say it plainly: a live wrapper
        # with a dead child is the hardest state to diagnose from downstream,
        # because every symptom appears somewhere else.
        logger.warning(
            "%s: process is alive but NOTHING accepts on port %s -- the "
            "server died after launch (a wrapper can outlive its child). "
            "Reporting not running.", tool_name, port)
        return False

    def _accepts_on(self, port: int, timeout: float = 0.5) -> bool:
        """Does something accept a TCP connection on 127.0.0.1:port?

        The ONE socket question this module asks, used by both
        ``_wait_for_listen`` (is it up yet) and ``_is_server_alive`` (is it
        still up). A TCP connect rather than a /health GET for the reason
        already recorded on _wait_for_listen: the readiness question is "is
        the socket up", and answering it with the protocol would make this
        care which protocol each sidecar speaks.

        Short timeout on purpose: this is loopback, and get_all_status calls
        it once per tool for a dashboard.
        """
        import socket
        try:
            with socket.create_connection(('127.0.0.1', int(port)),
                                          timeout=timeout):
                return True
        except OSError:
            return False
        except Exception as e:
            # Not a connection failure -- a bad port value, say. Do not let
            # it read as "dead"; log and treat as unknown-but-up so a
            # malformed record cannot silently disable a working tool.
            logger.warning("probe of port %r failed oddly (%s); not treating "
                           "it as a dead server", port, e)
            return True

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

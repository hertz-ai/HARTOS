"""
llama.cpp Server Manager -- lifecycle management for local LLM inference.

Manages llama-server (or llama-cpp-python) processes:
  - Auto-downloads llama.cpp release binaries if not found
  - Starts server with optimal settings for detected hardware
  - Health monitoring and auto-restart
  - Model hot-swap (stop -> load new GGUF -> start)
  - Graceful shutdown

Standalone mode: HARTOS manages its own llama.cpp (not waiting for Nunba).
Bundled mode: Defers to Nunba's llama.cpp server.

Usage:
    from integrations.service_tools.llamacpp_manager import get_llamacpp_manager

    mgr = get_llamacpp_manager()
    mgr.start('/path/to/model.gguf')
    print(mgr.health())
    mgr.swap_model('/path/to/other.gguf')
    mgr.stop()
"""

import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default directories
_HEVOLVE_HOME = Path.home() / '.hevolve'
_BIN_DIR = _HEVOLVE_HOME / 'bin'
_MODELS_DIR = _HEVOLVE_HOME / 'models'

# Health check timing
_HEALTH_START_TIMEOUT = 30       # Max seconds to wait for server on start
_HEALTH_POLL_INTERVAL = 0.5      # Initial poll interval (seconds)
_HEALTH_POLL_MAX_INTERVAL = 2.0  # Max poll interval (exponential backoff cap)
_HEALTH_CHECK_TIMEOUT = 3        # HTTP timeout for a single health check (seconds)

# Process shutdown
_STOP_GRACE_PERIOD = 5  # Seconds to wait after terminate() before kill()

# GitHub release
_GITHUB_RELEASE_API = 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest'

# Platform binary name patterns for GitHub release assets
_PLATFORM_ASSET_PATTERNS = {
    ('Windows', 'AMD64'):  'win-amd64',
    ('Windows', 'x86_64'): 'win-amd64',
    ('Linux', 'x86_64'):   'ubuntu-x64',
    ('Linux', 'aarch64'):  'ubuntu-arm64',
    ('Darwin', 'x86_64'):  'macos-x64',
    ('Darwin', 'arm64'):   'macos-arm64',
}


def _get_platform_key() -> str:
    """Return the platform asset key for the current system."""
    system = platform.system()
    machine = platform.machine()
    return _PLATFORM_ASSET_PATTERNS.get((system, machine), '')


def _server_binary_name() -> str:
    """Return the expected server binary filename for this OS."""
    if sys.platform == 'win32':
        return 'llama-server.exe'
    return 'llama-server'


def _http_get(url: str, timeout: int = _HEALTH_CHECK_TIMEOUT) -> Any:
    """Perform an HTTP GET, preferring pooled session, falling back to urllib.

    Returns the parsed JSON body on success, or None on failure.
    """
    # Try pooled session first (avoids new TCP connection)
    try:
        from core.http_pool import pooled_get
        resp = pooled_get(url, timeout=(timeout, timeout))
        resp.raise_for_status()
        return resp.json()
    except Exception:
        pass

    # Fallback: stdlib urllib (zero dependencies)
    try:
        import json
        import urllib.request
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def _http_get_raw(url: str, timeout: int = 30) -> Optional[bytes]:
    """Download raw bytes from a URL. Returns bytes or None."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            'Accept': 'application/octet-stream',
            'User-Agent': 'HARTOS-LlamaCppManager/1.0',
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as exc:
        logger.error(f"Download failed for {url}: {exc}")
        return None


def _http_get_json(url: str, timeout: int = 15) -> Optional[Dict]:
    """Fetch JSON from a URL using urllib (for GitHub API). Returns dict or None."""
    try:
        import json
        import urllib.request
        req = urllib.request.Request(url, headers={
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'HARTOS-LlamaCppManager/1.0',
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        logger.error(f"GitHub API request failed: {exc}")
        return None


class LlamaCppManager:
    """Manages a llama-server process for local LLM inference.

    Thread-safe: all mutating operations (start/stop/swap) are guarded by a Lock.
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._current_model: Optional[str] = None
        self._port: int = 8080
        self._lock = threading.Lock()
        self._server_binary: Optional[Path] = None

    # ── Public API ───────────────────────────────────────────────

    def start(self, model_path: str, port: int = 8080, **kwargs) -> bool:
        """Start llama-server with the given GGUF model.

        Auto-detects hardware and selects optimal parameters (GPU layers,
        context size, thread count, flash attention).

        Args:
            model_path: Absolute path to a .gguf model file.
            port: Port to listen on (default 8080, from port_registry 'llm').
            **kwargs: Additional overrides for server params (n_gpu_layers,
                      ctx_size, threads, flash_attn, etc.).

        Returns:
            True if server started and health check passed, False otherwise.
        """
        with self._lock:
            return self._start_locked(model_path, port, **kwargs)

    def stop(self) -> bool:
        """Gracefully stop the managed llama-server process.

        Sends terminate signal, waits up to 5 seconds, then force-kills
        if the process has not exited.

        Returns:
            True if the process was stopped (or was not running), False on error.
        """
        with self._lock:
            return self._stop_locked()

    def is_running(self) -> bool:
        """Check if the managed server process is alive AND responding to health checks.

        Returns:
            True if the process is running and /health returns successfully.
        """
        if self._process is None:
            return False
        if self._process.poll() is not None:
            # Process has exited
            logger.warning(
                f"llama-server process exited with code {self._process.returncode}")
            self._process = None
            return False
        # Process alive -- verify health endpoint
        return self._check_health()

    def health(self) -> Dict:
        """Query the llama-server /health endpoint.

        Returns:
            Parsed JSON from /health on success, or an error dict.
        """
        if self._process is None:
            return {'status': 'not_running', 'error': 'No managed server process'}

        url = f'http://127.0.0.1:{self._port}/health'
        result = _http_get(url, timeout=_HEALTH_CHECK_TIMEOUT)
        if result is not None:
            return result
        return {
            'status': 'error',
            'error': 'Health endpoint did not respond',
            'port': self._port,
            'model': self._current_model,
        }

    def swap_model(self, new_model_path: str, **kwargs) -> bool:
        """Hot-swap: stop the current model and start with a new one.

        Args:
            new_model_path: Path to the new .gguf model file.
            **kwargs: Additional server param overrides.

        Returns:
            True if the new model started successfully.
        """
        with self._lock:
            port = self._port
            logger.info(
                f"Swapping model: {self._current_model} -> {new_model_path}")
            self._stop_locked()
            return self._start_locked(new_model_path, port, **kwargs)

    def get_server_binary(self) -> Optional[Path]:
        """Locate the llama-server binary on this system.

        Search order:
          1. Cached result from a previous call
          2. System PATH (llama-server, llama-cpp-server)
          3. ~/.hevolve/bin/llama-server[.exe]

        Returns:
            Path to the binary, or None if not found.
        """
        if self._server_binary and self._server_binary.exists():
            return self._server_binary

        binary_name = _server_binary_name()

        # 1. Check PATH
        for name in ('llama-server', 'llama-cpp-server'):
            if sys.platform == 'win32':
                name += '.exe'
            found = shutil.which(name)
            if found:
                self._server_binary = Path(found)
                logger.info(f"Found llama-server on PATH: {self._server_binary}")
                return self._server_binary

        # 2. Check ~/.hevolve/bin/
        local_bin = _BIN_DIR / binary_name
        if local_bin.exists():
            self._server_binary = local_bin
            logger.info(f"Found llama-server at: {self._server_binary}")
            return self._server_binary

        logger.info("llama-server binary not found on this system")
        return None

    def download_server(self) -> Optional[Path]:
        """Download the latest llama.cpp release binary from GitHub.

        Detects the current platform, downloads the appropriate archive,
        extracts llama-server to ~/.hevolve/bin/, and makes it executable.

        Returns:
            Path to the downloaded binary, or None on failure.
        """
        platform_key = _get_platform_key()
        if not platform_key:
            logger.error(
                f"Unsupported platform: {platform.system()} {platform.machine()}")
            return None

        # Fetch latest release metadata
        logger.info("Fetching latest llama.cpp release from GitHub...")
        release = _http_get_json(_GITHUB_RELEASE_API)
        if not release:
            logger.error("Failed to fetch release info from GitHub")
            return None

        tag = release.get('tag_name', 'unknown')
        assets = release.get('assets', [])
        logger.info(f"Latest release: {tag} ({len(assets)} assets)")

        # Find matching asset
        target_asset = None
        for asset in assets:
            name = asset.get('name', '')
            # Match pattern: llama-{tag}-bin-{platform_key}.zip
            if platform_key in name and name.endswith('.zip'):
                target_asset = asset
                break

        if not target_asset:
            # Broader search: any zip containing the platform key
            for asset in assets:
                name = asset.get('name', '')
                if platform_key in name and ('.zip' in name or '.tar.gz' in name):
                    target_asset = asset
                    break

        if not target_asset:
            logger.error(
                f"No matching asset found for platform '{platform_key}' "
                f"in release {tag}. Available: "
                f"{[a['name'] for a in assets[:10]]}")
            return None

        download_url = target_asset.get('browser_download_url', '')
        asset_name = target_asset.get('name', '')
        asset_size = target_asset.get('size', 0)
        logger.info(
            f"Downloading: {asset_name} ({asset_size / 1024 / 1024:.1f} MB)")

        # Download
        data = _http_get_raw(download_url, timeout=300)
        if not data:
            return None

        # Extract
        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        archive_path = _BIN_DIR / asset_name

        try:
            archive_path.write_bytes(data)
            binary_name = _server_binary_name()
            extracted_binary = None

            if asset_name.endswith('.zip'):
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    # Find llama-server in the archive
                    for entry in zf.namelist():
                        basename = Path(entry).name
                        if basename == binary_name:
                            # Extract this single file to _BIN_DIR
                            source = zf.open(entry)
                            target = _BIN_DIR / binary_name
                            target.write_bytes(source.read())
                            source.close()
                            extracted_binary = target
                            break

                    if not extracted_binary:
                        # Extract all, then look for the binary
                        zf.extractall(_BIN_DIR)
                        for p in _BIN_DIR.rglob(binary_name):
                            extracted_binary = p
                            break
            else:
                # .tar.gz
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tf:
                    for member in tf.getmembers():
                        if Path(member.name).name == binary_name:
                            tf.extract(member, _BIN_DIR)
                            extracted_binary = _BIN_DIR / member.name
                            break
                    if not extracted_binary:
                        tf.extractall(_BIN_DIR)
                        for p in _BIN_DIR.rglob(binary_name):
                            extracted_binary = p
                            break

            # Clean up archive
            archive_path.unlink(missing_ok=True)

            if not extracted_binary or not extracted_binary.exists():
                logger.error(
                    f"Could not find {binary_name} in downloaded archive")
                return None

            # Move to canonical location if nested
            canonical = _BIN_DIR / binary_name
            if extracted_binary != canonical:
                shutil.move(str(extracted_binary), str(canonical))
                extracted_binary = canonical

            # Make executable (Unix)
            if sys.platform != 'win32':
                extracted_binary.chmod(
                    extracted_binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            self._server_binary = extracted_binary
            logger.info(f"llama-server installed at: {extracted_binary}")
            return extracted_binary

        except Exception as exc:
            logger.error(f"Failed to extract llama-server: {exc}")
            archive_path.unlink(missing_ok=True)
            return None

    def get_optimal_params(self, model_path: str) -> Dict[str, Any]:
        """Calculate optimal llama-server parameters based on hardware.

        Examines the GGUF file size as a proxy for model weight size, then
        checks available VRAM via vram_manager to decide GPU offload depth,
        context length, and threading.

        Args:
            model_path: Path to the .gguf model file.

        Returns:
            Dict with keys: n_gpu_layers, ctx_size, threads, flash_attn,
            host, port, and any additional flags.
        """
        params: Dict[str, Any] = {
            'n_gpu_layers': 0,
            'ctx_size': 4096,
            'threads': max(1, (os.cpu_count() or 4) // 2),
            'flash_attn': False,
            'host': '127.0.0.1',
            'port': self._port,
        }

        # Estimate model size from file
        model_size_gb = 0.0
        try:
            model_size_gb = os.path.getsize(model_path) / (1024 ** 3)
            logger.info(f"Model file size: {model_size_gb:.2f} GB")
        except OSError:
            logger.warning(f"Cannot stat model file: {model_path}")

        # Query GPU via vram_manager
        gpu_info = self._get_gpu_info()
        free_vram = gpu_info.get('free_gb', 0.0)
        cuda_available = gpu_info.get('cuda_available', False)
        total_vram = gpu_info.get('total_gb', 0.0)

        if cuda_available and free_vram > 0:
            if model_size_gb > 0 and free_vram >= model_size_gb * 1.1:
                # Enough VRAM to fit the entire model + overhead
                params['n_gpu_layers'] = -1  # All layers on GPU
                logger.info(
                    f"Full GPU offload: {free_vram:.1f} GB free >= "
                    f"{model_size_gb:.1f} GB model")
            elif model_size_gb > 0:
                # Partial offload: estimate fraction of layers that fit
                # Typical GGUF has ~32-80 layers; use ratio as heuristic
                ratio = free_vram / model_size_gb
                # Clamp to reasonable range
                estimated_layers = max(1, int(ratio * 40))  # assume ~40 layers
                params['n_gpu_layers'] = estimated_layers
                logger.info(
                    f"Partial GPU offload: {estimated_layers} layers "
                    f"({free_vram:.1f} GB free / {model_size_gb:.1f} GB model)")
            else:
                # Unknown model size, try full offload
                params['n_gpu_layers'] = -1

            # Context size: 8192 if ample VRAM, else 4096
            # Context memory is roughly (ctx * layers * hidden_dim * 2 * dtype_bytes)
            # Simplified heuristic: 8K context needs ~1-2 GB extra
            vram_after_model = free_vram - model_size_gb
            if vram_after_model >= 2.0:
                params['ctx_size'] = 8192
            else:
                params['ctx_size'] = 4096

            # Flash attention: available on modern NVIDIA GPUs (Ampere+)
            # Heuristic: if GPU name contains known architectures
            gpu_name = (gpu_info.get('name') or '').lower()
            # Ampere: RTX 30xx, A100, etc. Hopper: H100. Ada: RTX 40xx
            flash_capable_keywords = [
                'rtx 30', 'rtx 40', 'rtx 50', 'a100', 'a10', 'h100',
                'l40', 'rtx a', 'geforce 30', 'geforce 40',
            ]
            if any(kw in gpu_name for kw in flash_capable_keywords):
                params['flash_attn'] = True
                logger.info(f"Enabling flash attention for {gpu_info.get('name')}")

        else:
            # CPU-only mode
            params['n_gpu_layers'] = 0
            params['ctx_size'] = 2048  # Conservative for CPU
            # Use more threads on CPU-only
            params['threads'] = max(1, (os.cpu_count() or 4) - 1)
            logger.info("CPU-only mode: no GPU available")

        # ── ResourceGovernor cap: leave headroom for the rest of the OS ──
        # Never use ALL cores — reserve 25% for foreground apps.
        total_cores = os.cpu_count() or 4
        max_threads = max(1, int(total_cores * 0.75))
        if params['threads'] > max_threads:
            logger.info("Capping threads %d → %d (75%% of %d cores)",
                        params['threads'], max_threads, total_cores)
            params['threads'] = max_threads

        # Cap context size based on available RAM (avoid low-memory warnings)
        try:
            import psutil
            avail_gb = psutil.virtual_memory().available / (1024**3)
            if avail_gb < 4.0 and params['ctx_size'] > 4096:
                params['ctx_size'] = 4096
                logger.info("Capping ctx_size to 4096 (only %.1fGB RAM available)", avail_gb)
            elif avail_gb < 2.0 and params['ctx_size'] > 2048:
                params['ctx_size'] = 2048
                logger.info("Capping ctx_size to 2048 (only %.1fGB RAM available)", avail_gb)
        except ImportError:
            pass

        return params

    @property
    def current_model(self) -> Optional[str]:
        """Return the path of the currently loaded model, or None."""
        return self._current_model

    @property
    def port(self) -> int:
        """Return the port the server is (or will be) running on."""
        return self._port

    # ── Private Implementation ───────────────────────────────────

    def _start_locked(self, model_path: str, port: int, **kwargs) -> bool:
        """Start the server (caller must hold self._lock)."""
        if self._process is not None and self._process.poll() is None:
            logger.warning(
                "Server already running (PID %d) -- stop first or use swap_model()",
                self._process.pid)
            return False

        # Validate model file
        if not os.path.isfile(model_path):
            logger.error(f"Model file not found: {model_path}")
            return False

        # Find or download binary
        binary = self.get_server_binary()
        if binary is None:
            logger.info("llama-server not found, attempting download...")
            binary = self.download_server()
            if binary is None:
                logger.error(
                    "Cannot start: llama-server binary not available. "
                    "Install manually or check network.")
                return False

        self._port = port

        # Calculate params
        params = self.get_optimal_params(model_path)
        # Apply user overrides
        params.update(kwargs)
        params['port'] = port

        # Build command
        cmd = [
            str(binary),
            '--model', str(model_path),
            '--host', str(params.get('host', '0.0.0.0')),
            '--port', str(params['port']),
            '--ctx-size', str(params.get('ctx_size', 4096)),
            '--threads', str(params.get('threads', 2)),
            '--n-gpu-layers', str(params.get('n_gpu_layers', 0)),
        ]

        if params.get('flash_attn'):
            cmd.append('--flash-attn')

        # Pass through any extra CLI flags
        extra_args = params.get('extra_args', [])
        if extra_args:
            cmd.extend(extra_args)

        logger.info(f"Starting llama-server: {' '.join(cmd)}")

        # Platform-specific subprocess options
        popen_kwargs: Dict[str, Any] = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
        }

        if sys.platform == 'win32':
            # Hide the console window on Windows
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            popen_kwargs['startupinfo'] = si
            popen_kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(cmd, **popen_kwargs)
            logger.info(f"llama-server started (PID {self._process.pid})")
        except FileNotFoundError:
            logger.error(f"Binary not found or not executable: {binary}")
            self._process = None
            return False
        except PermissionError:
            logger.error(f"Permission denied executing: {binary}")
            self._process = None
            return False
        except OSError as exc:
            logger.error(f"Failed to start llama-server: {exc}")
            self._process = None
            return False

        # Wait for health endpoint with exponential backoff
        self._current_model = model_path
        if self._wait_for_health():
            logger.info(
                f"llama-server ready on port {port} "
                f"(model: {os.path.basename(model_path)})")
            return True
        else:
            logger.error(
                f"llama-server health check failed after {_HEALTH_START_TIMEOUT}s "
                "-- stopping process")
            self._stop_locked()
            return False

    def _stop_locked(self) -> bool:
        """Stop the server (caller must hold self._lock)."""
        if self._process is None:
            logger.debug("No server process to stop")
            return True

        pid = self._process.pid
        logger.info(f"Stopping llama-server (PID {pid})...")

        try:
            # Graceful shutdown: terminate (SIGTERM on Unix, TerminateProcess on Windows)
            self._process.terminate()

            try:
                self._process.wait(timeout=_STOP_GRACE_PERIOD)
                logger.info(f"llama-server (PID {pid}) terminated gracefully")
            except subprocess.TimeoutExpired:
                # Force kill
                logger.warning(
                    f"llama-server (PID {pid}) did not exit in "
                    f"{_STOP_GRACE_PERIOD}s -- force killing")
                self._process.kill()
                self._process.wait(timeout=5)
                logger.info(f"llama-server (PID {pid}) killed")

        except ProcessLookupError:
            logger.debug(f"Process {pid} already exited")
        except OSError as exc:
            logger.error(f"Error stopping llama-server (PID {pid}): {exc}")
            return False
        finally:
            self._process = None
            self._current_model = None

        return True

    def _check_health(self) -> bool:
        """Single health check against /health endpoint."""
        url = f'http://127.0.0.1:{self._port}/health'
        result = _http_get(url, timeout=_HEALTH_CHECK_TIMEOUT)
        return result is not None

    def _wait_for_health(self) -> bool:
        """Wait for the server health endpoint with exponential backoff.

        Polls GET /health up to _HEALTH_START_TIMEOUT seconds.
        """
        deadline = time.monotonic() + _HEALTH_START_TIMEOUT
        interval = _HEALTH_POLL_INTERVAL

        while time.monotonic() < deadline:
            # Check if process died
            if self._process is not None and self._process.poll() is not None:
                rc = self._process.returncode
                logger.error(f"llama-server exited prematurely (code {rc})")
                # Try to read stderr for diagnostics
                try:
                    stderr = self._process.stderr.read().decode('utf-8', errors='replace')
                    if stderr:
                        logger.error(f"llama-server stderr: {stderr[:1000]}")
                except Exception:
                    pass
                return False

            if self._check_health():
                return True

            time.sleep(interval)
            interval = min(interval * 1.5, _HEALTH_POLL_MAX_INTERVAL)

        return False

    @staticmethod
    def _get_gpu_info() -> Dict:
        """Query GPU info via vram_manager singleton."""
        try:
            from .vram_manager import vram_manager
            return vram_manager.detect_gpu()
        except Exception as exc:
            logger.debug(f"vram_manager unavailable: {exc}")
            return {
                'name': None,
                'total_gb': 0.0,
                'free_gb': 0.0,
                'cuda_available': False,
            }


# ── Module-level Singleton ───────────────────────────────────────

_manager: Optional[LlamaCppManager] = None
_manager_lock = threading.Lock()


def get_llamacpp_manager() -> LlamaCppManager:
    """Return the global LlamaCppManager singleton (thread-safe)."""
    global _manager
    if _manager is not None:
        return _manager

    with _manager_lock:
        if _manager is not None:
            return _manager
        _manager = LlamaCppManager()
        return _manager

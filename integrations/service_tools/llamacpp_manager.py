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
# How long a SWAP waits for the newcomer to finish loading.  llama-server
# answers /health 503 until its weights are in, and a 21.19 GiB model measured
# ~7 minutes off an external drive; the 30 s start window killed every such
# swap mid-load.  During a swap the incumbent keeps serving, so waiting costs
# the user nothing -- only a cold start() has a reason to give up early.
_SWAP_LOAD_TIMEOUT = 900

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
        logger.exception("_http_get: swallowed Exception")

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
        """Replace the running model without the node ever losing its LLM.

        MAKE-BEFORE-BREAK.  This used to be ``_stop_locked()`` followed by
        ``_start_locked()`` on the SAME port: stop, then hope.  If the
        newcomer failed to start, :8080 was empty and the node had no local
        model at all — and a 21.19 GiB model measured ~7 MINUTES to become
        ready off an external drive, so the window was minutes even when it
        worked.  Owner directive 2026-09-22: "at any point in time some LLM
        shd be running for the nunba to work locally except central".

        The ordering lives in ``llm_swap.swap_main_llm`` because Nunba's
        ``LlamaConfig`` owns the same problem on the desktop and the two
        had already drifted into different failure modes.  What is HERE is
        only what is specific to a process THIS manager owns.

        OWNERSHIP IS THE PRECONDITION.  We swap only a server we started.
        A server we did not start is someone else's (on the desktop it is
        Nunba's), and for that one, both halves are wrong: we cannot stop
        it — ``_stop_locked`` works off ``self._process`` and would silently
        no-op — and launching a second main server beside it is the
        2026-09-13 incident, where a second :8080 server with a 4096 ctx
        turned every agent call into an HTTP 400.  So we refuse and say who
        to ask instead.  This is the same rule ``_start_locked``'s adopt
        probe enforces, applied one level up where the decision belongs:
        the probe cannot tell "a server is running" from "a server I own",
        and that distinction is the whole difference between a swap and a
        duplicate.

        NOT EVICTION.  ``model_lifecycle.request_swap`` excludes the main
        LLM on purpose and is not involved.  In particular the incumbent's
        reclaim is NOT part of the budget below: it is still serving and
        must keep serving, so what it holds is not available.

        Args:
            new_model_path: Path to the new .gguf model file.
            **kwargs: Additional server param overrides.

        Returns:
            True only if the new model is SERVING and is now the endpoint.
        """
        from core.port_registry import find_free_port

        from .llm_swap import swap_main_llm

        with self._lock:
            incumbent = self._process
            if incumbent is None or incumbent.poll() is not None:
                logger.info(
                    "not swapping %s: this manager does not own a running "
                    "llama-server.  Whoever owns the serving model owns the "
                    "swap — on the desktop that is Nunba's model settings "
                    "(LlamaConfig.switch_model).  Stopping a process we did "
                    "not start is not possible here, and starting a second "
                    "main server beside it is the 2026-09-13 duplicate.",
                    os.path.basename(new_model_path))
                return False

            logger.info(
                f"Swapping model: {self._current_model} -> {new_model_path}")

            def _spawn(port: int):
                return self._spawn_locked(new_model_path, port, **kwargs)

            def _serves(handle, port: int) -> bool:
                proc, _ctx, _slots = handle
                return self._wait_for_health(proc, port,
                                             timeout=_SWAP_LOAD_TIMEOUT)

            def _repoint(handle, port: int) -> None:
                proc, ctx_size, slots = handle
                # Move the CANONICAL endpoint, not just this object's field.
                # The newcomer is on an ephemeral port that no resolver
                # candidate lists; set_local_llm_url writes the first
                # candidate get_local_llm_url consults and drops its cache.
                # If this raises, swap_main_llm abandons the newcomer and the
                # incumbent keeps serving on the endpoint it still owns.
                from core.port_registry import set_local_llm_url
                set_local_llm_url(f'http://127.0.0.1:{port}')
                self._process = proc
                self._port = port
                self._current_model = new_model_path
                # Announce the geometry only now that this server is both
                # serving AND the endpoint — same rule as _start_locked.
                from core.llama_geometry import publish_geometry
                publish_geometry(ctx_size, slots)

            outcome = swap_main_llm(
                label=os.path.basename(new_model_path),
                **self._admission_facts(new_model_path),
                pick_port=find_free_port,
                spawn=_spawn,
                serves=_serves,
                repoint=_repoint,
                retire=lambda: self._terminate(incumbent),
                abandon=lambda handle: self._terminate(handle[0]),
            )
            return outcome.ok

    #: What ``llm_swap.swap_main_llm`` admits on -- exactly its keyword
    #: parameters, because ``swap_model`` splats ``_admission_facts`` into it.
    _ADMISSION_KEYS = ('free_vram_gb', 'free_ram_gb', 'gpu_available', 'moe',
                       'vram_need_gb', 'ram_need_gb', 'whole_need_gb')

    def _gguf_footprint(self, model_path: str) -> Dict[str, Any]:
        """Everything a placement decision about ``model_path`` can know, read ONCE.

        Two decisions consume this, and until 2026-09-23 each gathered its
        own copy.  The swap's admission (``_admission_facts`` -- "does it fit
        BESIDE the incumbent?") probed the GPU, psutil, the GGUF header and
        the catalog; ``get_optimal_params`` probed the GPU and stat'd the
        file again, then judged the fit with a hand-written
        ``free_vram >= size * 1.1`` -- a fourth fit rule beside
        ``model_catalog.gguf_fits_gpu``, and invisible to every refactor that
        collapsed the other three, because it referenced no shared symbol a
        grep could find.  One gatherer, so the swap and the spawn judge the
        same file on the same card from the same numbers; one rule
        (``gguf_fits_gpu``) judges it.

        Pure gathering, no decisions.  Each source fails on its own -- an
        unreadable catalog does not lose the file size, an absent psutil
        does not lose the GPU reading -- and what could not be read is None,
        so each consumer applies its own policy to "unknown": the swap
        refuses, the RAM clamp leaves the window alone, the spawn keeps its
        pre-existing "size unknown: try full offload".

        The footprint is stated at the best knowledge level available, which
        is the level ``gguf_fits_gpu`` is then asked at:

          MEASURED   ``vram_need_gb`` / ``ram_need_gb`` -- ``record_residency``
                     banked what this exact weight file cost on this card
                     last time it loaded.  Quant-aware, so a row re-pointed
                     from Q4 to Q8 reads as unknown rather than as the old
                     number.
          ESTIMATED  ``whole_need_gb`` -- never loaded here, so only the file
                     size is knowable, through the one sizing helper.
                     Deliberately more conservative.
          UNKNOWN    neither -- the file could not even be sized.

        Keys: free_vram_gb, gpu_available, gpu_name, free_ram_gb (None when
        unknown), size_gb (None when unknown), moe, block_count (None when
        the header does not say), vram_need_gb, ram_need_gb, whole_need_gb.
        """
        gpu = self._get_gpu_info()
        fp: Dict[str, Any] = {
            'free_vram_gb': float(gpu.get('free_gb') or 0.0),
            'gpu_available': bool(gpu.get('cuda_available')),
            'gpu_name': gpu.get('name'),
            'free_ram_gb': None,
            'size_gb': None,
            'moe': False,
            'block_count': None,
            'vram_need_gb': None,
            'ram_need_gb': None,
            'whole_need_gb': None,
        }

        try:
            import psutil
            fp['free_ram_gb'] = psutil.virtual_memory().available / (1024 ** 3)
        except ImportError:
            logger.debug("_gguf_footprint: psutil unavailable; RAM unknown")

        try:
            from .model_catalog import (get_catalog,
                                        llama_gguf_compute_requirements,
                                        read_gguf_facts)
        except ImportError:
            logger.exception(
                "_gguf_footprint: model_catalog unavailable; %s cannot be "
                "sized and is reported as unknown", model_path)
            return fp

        # The file's own statement of what it is.  {} for anything
        # unreadable -- "not known", never "not MoE".
        facts = read_gguf_facts(model_path)
        fp['moe'] = bool(facts.get('moe'))
        fp['block_count'] = facts.get('block_count') or None

        # read_gguf_facts already stat'd the file; stat again only for a
        # file it could not parse.
        try:
            weight_bytes = (facts.get('weight_bytes')
                            or os.path.getsize(model_path))
            fp['size_gb'] = weight_bytes / (1024 ** 3)
        except OSError:
            logger.warning(f"Cannot stat model file: {model_path}")

        try:
            catalog = get_catalog()
            entry = catalog.get_by_weight_file(model_path)
            banked = catalog.residency(entry.id) if entry else None
            if banked and banked.get('vram_gb') is not None:
                fp['vram_need_gb'] = banked.get('vram_gb')
                fp['ram_need_gb'] = banked.get('ram_gb')
        except Exception:
            logger.exception(
                "_gguf_footprint: catalog unreadable for %s; sizing from "
                "the file alone", model_path)

        if fp['vram_need_gb'] is None and fp['size_gb'] is not None:
            # The same conservative figure the install path uses, from the
            # one helper, rather than another copy of "x 1.35".
            vram_est, ram_est = llama_gguf_compute_requirements(fp['size_gb'])
            fp['whole_need_gb'] = vram_est
            # The RAM-only arm (no CUDA) needs its own estimate; the GPU arm
            # ignores ram_need_gb unless the split was measured.
            if fp['ram_need_gb'] is None:
                fp['ram_need_gb'] = ram_est
        return fp

    def _admission_facts(self, model_path: str) -> Dict[str, Any]:
        """What the swap needs to know to decide "does it fit BESIDE?".

        The ``_ADMISSION_KEYS`` projection of :meth:`_gguf_footprint`: the
        live readings taken with the incumbent resident, plus the newcomer's
        footprint at the best knowledge level available (MEASURED /
        ESTIMATED / UNKNOWN, as that method describes).  With neither
        footprint known, ``gguf_fits_gpu`` returns False and the swap is
        refused -- an unknown footprint is not a free model: refusing costs
        the user a swap, guessing costs them the node.

        RAM that could not be read counts as none.  A MoE's experts have to
        be RESIDENT (0.95 tok/s measured when they were not), so "unknown"
        must not admit them.
        """
        fp = self._gguf_footprint(model_path)
        facts = {key: fp[key] for key in self._ADMISSION_KEYS}
        if facts['free_ram_gb'] is None:
            facts['free_ram_gb'] = 0.0
        return facts

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

        Reads the box and the file once (``_gguf_footprint``, shared with
        the swap's admission), asks ``model_catalog.gguf_fits_gpu`` whether
        the model fits with attention resident on the GPU, and places it:
        full offload (plus ``--cpu-moe`` for a mixture of experts whose
        experts belong in system RAM), or a partial offload sized against
        the file's own block count.  Context length comes from
        ``core.llama_geometry``; threads from the core count.

        Args:
            model_path: Path to the .gguf model file.

        Returns:
            Dict with keys: n_gpu_layers, ctx_size, threads, flash_attn,
            host, port, and any additional flags (``extra_args``).
        """
        from core.llama_geometry import (clamp_ctx_for_ram, ctx_for_role,
                                         derive_ctx_size)
        from .model_catalog import (LAYER_COUNT_FALLBACK, gguf_fits_gpu,
                                    gguf_partial_offload_layers,
                                    moe_offload_args)

        params: Dict[str, Any] = {
            'n_gpu_layers': 0,
            # The unmeasured default, named once: core.llama_geometry
            # .CTX_FALLBACK.  Every branch below overwrites it; it only
            # survives if the GPU probe itself raises.
            'ctx_size': ctx_for_role('main'),
            'threads': max(1, (os.cpu_count() or 4) // 2),
            'flash_attn': False,
            'host': '127.0.0.1',
            'port': self._port,
        }

        # ONE reading of the box and the file, the same one the swap's
        # admission uses -- see _gguf_footprint for why it is not gathered
        # here a second time.
        fp = self._gguf_footprint(model_path)
        free_vram = fp['free_vram_gb']
        size_gb = fp['size_gb']
        free_ram = fp['free_ram_gb'] if fp['free_ram_gb'] is not None else 0.0
        if size_gb is not None:
            logger.info(f"Model file size: {size_gb:.2f} GB")

        if fp['gpu_available'] and free_vram > 0:
            if not size_gb:
                # Unknown (or empty) model size: try full offload, as before.
                params['n_gpu_layers'] = -1
            elif gguf_fits_gpu(free_vram, free_ram, gpu_available=True,
                               moe=fp['moe'],
                               vram_need_gb=fp['vram_need_gb'],
                               ram_need_gb=fp['ram_need_gb'],
                               whole_need_gb=fp['whole_need_gb']):
                # Fits with attention resident on the GPU -- the ONE rule,
                # model_catalog.gguf_fits_gpu, asked at the same knowledge
                # level the swap asks it at.  This used to be a private
                # `free_vram >= model_size_gb * 1.1`, which judged a mixture
                # of experts by its whole file: a 21 GiB MoE that runs in
                # 2.87 GiB of VRAM under --cpu-moe was refused full offload
                # on the very card that had already served it.
                params['n_gpu_layers'] = -1
                placement = ''
                # For a MoE, "fits" may mean fits WITH its experts in system
                # RAM, and llama.cpp has to be told so.  moe_offload_args is
                # the one answer to that -- the same call Nunba's main and
                # caption spawns and model_lifecycle's restart make -- and
                # this spawn was the one of the four that never made it.  It
                # returns [] for a MoE that fits whole (experts are faster
                # on the GPU) and for a dense model, which is never asked.
                if fp['moe']:
                    moe_args = moe_offload_args(model_path, free_vram)
                    params['extra_args'] = (
                        list(params.get('extra_args') or []) + moe_args)
                    placement = (' (MoE: ' + (' '.join(moe_args)
                                              or 'experts on the GPU') + ')')
                logger.info(
                    f"Full GPU offload: {free_vram:.1f} GB VRAM free, "
                    f"{size_gb:.1f} GB model{placement}")
            else:
                # Does not fit resident: partial offload, the pre-existing
                # fallback.  How many layers is sized against the file's own
                # block count when the header states it; LAYER_COUNT_FALLBACK
                # (40) only for a file the reader could not parse.  A MoE the
                # rule refused is placed like a dense model here: the MoE arm
                # of gguf_fits_gpu already accounted for the RAM split, so a
                # "no" from it means the experts do not fit anywhere either.
                n_layers = fp['block_count'] or LAYER_COUNT_FALLBACK
                params['n_gpu_layers'] = gguf_partial_offload_layers(
                    free_vram, size_gb, fp['block_count'])
                logger.info(
                    f"Partial GPU offload: {params['n_gpu_layers']} of "
                    f"{n_layers} layers"
                    f"{'' if fp['block_count'] else ' (block count unknown, assumed)'}"
                    f" ({free_vram:.1f} GB free / {size_gb:.1f} GB model)")

            # Context size: the ONE tier table, core.llama_geometry.CTX_TIERS.
            #
            # This used to be a private ladder (10240 / 8192 / 4096 / 2048)
            # against `free - model - 3.0`, where the 3.0 was a hardcoded "TTS
            # reserve".  Two defects, both invisible from inside this file:
            #
            #   1. It was the THIRD independent answer to "how big is n_ctx",
            #      and it could not agree with the one the wire trimmer budgets
            #      against.  10240 is not even a value any other site can
            #      produce, so a model onboarded here served a window no other
            #      component believed in.
            #   2. The TTS reserve was double-counted.  CTX_TIERS already
            #      encodes headroom for the rest of the GPU stack (TTS, the
            #      0.8B draft, KV buffers) IN its thresholds rather than as a
            #      subtraction — that is what "≥2 GiB remaining → 8192,
            #      <2 → 4096 (preserves VRAM for TTS/STT)" means.  Subtracting
            #      3.0 first and then applying a ladder reserves for TTS twice.
            #
            # So: pass the raw headroom and let the shared table decide.
            params['ctx_size'] = derive_ctx_size(free_vram, size_gb or 0.0)

            # Flash attention: available on modern NVIDIA GPUs (Ampere+)
            # Heuristic: if GPU name contains known architectures
            gpu_name = (fp['gpu_name'] or '').lower()
            # Ampere: RTX 30xx, A100, etc. Hopper: H100. Ada: RTX 40xx
            flash_capable_keywords = [
                'rtx 30', 'rtx 40', 'rtx 50', 'a100', 'a10', 'h100',
                'l40', 'rtx a', 'geforce 30', 'geforce 40',
            ]
            if any(kw in gpu_name for kw in flash_capable_keywords):
                params['flash_attn'] = True
                logger.info(f"Enabling flash attention for {fp['gpu_name']}")

        else:
            # CPU-only mode — core.llama_geometry.CPU_ONLY_CTX (still 2048;
            # the number moved, it did not change).
            params['n_gpu_layers'] = 0
            params['ctx_size'] = ctx_for_role('main', on_gpu=False)
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

        # Cap context size based on available RAM (avoid low-memory warnings).
        #
        # core.llama_geometry.RAM_CLAMPS — a SEPARATE constraint from the tier
        # table, not a competing one: the tiers ask whether the GPU has
        # headroom, this asks whether the host survives the allocation.
        #
        # The if/elif this replaces had a dead branch: any box under 2.0 GiB is
        # also under 4.0 GiB, so it matched the first arm and the 2048 clamp
        # was unreachable.  A 1.5 GiB box got 4096 while the code read as
        # though it promised 2048.  As a table every row can fire.
        #
        # The RAM figure is the one reading the fit decision above used.  RAM
        # that could not be read (no psutil) leaves the window alone, as it
        # always has: guessing downward would silently shrink a correctly
        # derived window.
        if fp['free_ram_gb'] is not None:
            _before = params['ctx_size']
            params['ctx_size'] = clamp_ctx_for_ram(_before, fp['free_ram_gb'])
            if params['ctx_size'] != _before:
                logger.info("Capping ctx_size %d -> %d (only %.1fGB RAM available)",
                            _before, params['ctx_size'], fp['free_ram_gb'])
        else:
            logger.debug("get_optimal_params: RAM unknown; ctx_size not clamped")

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

        # ADOPT, never duplicate — owner ruling 2026-09-13
        # (memory/feedback_one_llama_server_single_chokepoint.md): ONE
        # llama-server, reached through one chokepoint.
        #
        # The `self._process` check above only knows about servers THIS manager
        # started.  The live reachable caller is model_onboarding.switch_model
        # (exposed as the `switch_model` MCP tool and POST /api/models/switch),
        # and unlike its sibling `onboard` it carries no _is_nunba_bundled()
        # guard — so on the desktop an agent calling that tool would have
        # launched a SECOND main llama-server on the port Nunba's is already
        # serving.  That is precisely the 2026-09-13 incident: a second server
        # took :8080 with a 4096 ctx and every agent call returned HTTP 400
        # ("TOOL SCHEMA alone is 4745 tokens against an n_ctx of 4096").
        #
        # Same primitive model_lifecycle._launch_llama_server_direct already
        # uses, so there is one definition of "is a main engine serving".
        try:
            from core.health_probe import probe_llm
            if (probe_llm() or {}).get('status') == 'up':
                logger.info(
                    "a main llama-server is already serving — not launching a "
                    "second one.  Change models through the owner of that "
                    "server (Nunba's model settings / LlamaConfig.switch_model)")
                return False
        except ImportError:
            pass          # no canonical prober here; fall through to launching
        except Exception as exc:
            logger.debug("adopt-probe skipped: %r", exc)

        spawned = self._spawn_locked(model_path, port, **kwargs)
        if spawned is None:
            return False
        proc, ctx_size, slots = spawned

        # Adopt it as OUR server before waiting: _stop_locked() on a failed
        # health check works off self._process, and is_running() must see the
        # process we just launched.
        self._process = proc
        self._port = port
        self._current_model = model_path

        if self._wait_for_health(proc, port):
            logger.info(
                f"llama-server ready on port {port} "
                f"(model: {os.path.basename(model_path)})")
            # Publish only now that a server is CONFIRMED serving this
            # geometry.  HARTOS's wire trimmer
            # (core.llm_outbound_logger._get_budget_per_slot) prefers the
            # published env over its live /props probe, so announcing a spawn
            # that then failed health would leave every request budgeted
            # against a window nothing is serving — the 2026-09-11 shape
            # (trimmer believed 12288, the server ran 8192) with the sign
            # flipped.  Announce what exists, not what was attempted.
            #
            # The G3 fallback in model_lifecycle deliberately does NOT publish:
            # it passes no --parallel, so it has no honest slot count to state.
            from core.llama_geometry import publish_geometry
            publish_geometry(ctx_size, slots)
            return True

        logger.error(
            f"llama-server health check failed after {_HEALTH_START_TIMEOUT}s "
            "-- stopping process")
        self._stop_locked()
        return False

    def _spawn_locked(self, model_path: str, port: int,
                      **kwargs) -> Optional[tuple]:
        """Launch a llama-server process.  Caller must hold self._lock.

        LAUNCH ONLY — it does not wait for health, and it does not touch
        ``self._process`` / ``self._port`` / ``self._current_model``.  Both
        of those are the caller's business, and they have to be, because a
        SWAP runs two servers at once: the newcomer must come up without
        displacing the handle for the incumbent that is still serving.

        Split out of ``_start_locked`` on 2026-09-22 so that ``start()`` and
        ``swap_model()`` share ONE spawn rather than growing a second copy
        that drifts (the two main-LLM swaps in this codebase had already
        drifted into two different failure modes before this).

        Returns ``(process, ctx_size, slots)``, or None if it did not launch.
        The geometry is returned rather than published here: it must be
        announced only when this server becomes THE endpoint, which for a
        swap is later.
        """
        # Validate model file
        if not os.path.isfile(model_path):
            logger.error(f"Model file not found: {model_path}")
            return None

        # Find or download binary
        binary = self.get_server_binary()
        if binary is None:
            logger.info("llama-server not found, attempting download...")
            binary = self.download_server()
            if binary is None:
                logger.error(
                    "Cannot start: llama-server binary not available. "
                    "Install manually or check network.")
                return None

        # Calculate params
        params = self.get_optimal_params(model_path)
        # Apply user overrides
        params.update(kwargs)
        params['port'] = port

        # Build command.  ctx_size is always present (get_optimal_params seeds
        # it from core.llama_geometry before any branch), so the `.get` default
        # here was a FOURTH place a context size could be written — and being a
        # default that never fires, nothing would ever have caught it drifting.
        from core.llama_geometry import ctx_for_role, slots_from_env
        ctx_size = int(params.get('ctx_size') or ctx_for_role('main'))
        # core.constants.LLAMA_SLOTS_DEFAULT (1) unless an operator has
        # published otherwise — one source for the slot count too.
        slots = int(params.get('parallel') or slots_from_env())
        cmd = [
            str(binary),
            '--model', str(model_path),
            '--host', str(params.get('host', '0.0.0.0')),
            '--port', str(params['port']),
            '--ctx-size', str(ctx_size),
            '--threads', str(params.get('threads', 2)),
            '--n-gpu-layers', str(params.get('n_gpu_layers', 0)),
            # Explicit slot count, for the same reason Nunba's spawn pins it.
            # Leaving --parallel off does NOT mean one slot: llama-server
            # defaults it to "auto" and picked 4 on the reference box
            # (llama_server_8080.log:8, "n_parallel is set to auto, using
            # n_parallel = 4 and kv_unified = true").  Under kv_unified the
            # whole n_ctx is ONE shared KV pool across all slots, so 4 slots
            # over-subscribe it and the server logs "failed to find free space
            # in the KV cache" -> truncation + HTTP 503.  Pinning it also makes
            # the publish below a measurement rather than a guess.
            '--parallel', str(slots),
        ]

        if params.get('flash_attn'):
            cmd.append('--flash-attn')

        # Pass through any extra CLI flags
        extra_args = params.get('extra_args', [])
        if extra_args:
            cmd.extend(extra_args)

        # Multi-token prediction, switched on by the MODEL (its MTP head)
        # and only for a binary that accepts it: model_catalog.mtp_spec_args,
        # the same call every spawn makes.  Placement-independent -- it
        # applies to a full, partial or CPU offload alike.
        try:
            from .model_catalog import mtp_spec_args
            cmd.extend(mtp_spec_args(str(model_path), str(binary)))
        except Exception as e:
            logger.info("MTP probe skipped (%s); launching without it", e)

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
            proc = subprocess.Popen(cmd, **popen_kwargs)
            logger.info(f"llama-server started (PID {proc.pid}) on port {port}")
        except FileNotFoundError:
            logger.error(f"Binary not found or not executable: {binary}")
            return None
        except PermissionError:
            logger.error(f"Permission denied executing: {binary}")
            return None
        except OSError as exc:
            logger.error(f"Failed to start llama-server: {exc}")
            return None

        return proc, ctx_size, slots

    def _stop_locked(self) -> bool:
        """Stop OUR server and forget it (caller must hold self._lock)."""
        if self._process is None:
            logger.debug("No server process to stop")
            return True

        try:
            return self._terminate(self._process)
        finally:
            # Forget it even if the kill raised (TimeoutExpired after kill()
            # escapes _terminate); the pre-split code had this finally.
            self._process = None
            self._current_model = None

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> bool:
        """Stop one llama-server process.  Owns no instance state.

        Separated from ``_stop_locked`` because a swap has TWO processes to
        end in different circumstances -- retiring the incumbent once the
        newcomer serves, or abandoning a newcomer that never did -- and
        neither is "stop the server this manager currently points at".
        """
        pid = proc.pid
        logger.info(f"Stopping llama-server (PID {pid})...")

        try:
            # Graceful shutdown: terminate (SIGTERM on Unix, TerminateProcess on Windows)
            proc.terminate()

            try:
                proc.wait(timeout=_STOP_GRACE_PERIOD)
                logger.info(f"llama-server (PID {pid}) terminated gracefully")
            except subprocess.TimeoutExpired:
                # Force kill
                logger.warning(
                    f"llama-server (PID {pid}) did not exit in "
                    f"{_STOP_GRACE_PERIOD}s -- force killing")
                proc.kill()
                proc.wait(timeout=5)
                logger.info(f"llama-server (PID {pid}) killed")

        except ProcessLookupError:
            logger.debug(f"Process {pid} already exited")
        except OSError as exc:
            logger.error(f"Error stopping llama-server (PID {pid}): {exc}")
            return False

        return True

    def _check_health(self, port: Optional[int] = None) -> bool:
        """Single health check against /health endpoint.

        ``port`` defaults to this manager's own server.  A SWAP asks about a
        newcomer on a DIFFERENT port while the incumbent still holds
        ``self._port``, so the port has to be an argument rather than
        instance state.
        """
        url = f'http://127.0.0.1:{port or self._port}/health'
        result = _http_get(url, timeout=_HEALTH_CHECK_TIMEOUT)
        return result is not None

    def _wait_for_health(self, proc: Optional[subprocess.Popen] = None,
                         port: Optional[int] = None,
                         timeout: Optional[float] = None) -> bool:
        """Wait until a server SERVES, with exponential backoff.

        Polls GET /health up to _HEALTH_START_TIMEOUT seconds.  This is the
        capability probe, not a liveness check: a process that starts and
        never answers is exactly the case that turns break-before-make into
        an outage (#99).

        ``proc``/``port`` default to this manager's own server so existing
        callers are unchanged; the swap passes the newcomer's.
        """
        proc = proc if proc is not None else self._process
        # ``timeout``: a cold start gives up at _HEALTH_START_TIMEOUT; a swap
        # passes _SWAP_LOAD_TIMEOUT because its incumbent is still serving.
        deadline = time.monotonic() + (
            _HEALTH_START_TIMEOUT if timeout is None else timeout)
        interval = _HEALTH_POLL_INTERVAL

        while time.monotonic() < deadline:
            # Check if process died
            if proc is not None and proc.poll() is not None:
                rc = proc.returncode
                logger.error(f"llama-server exited prematurely (code {rc})")
                # Try to read stderr for diagnostics
                try:
                    stderr = proc.stderr.read().decode('utf-8', errors='replace')
                    if stderr:
                        logger.error(f"llama-server stderr: {stderr[:1000]}")
                except Exception:
                    logger.exception("_wait_for_health: swallowed Exception")
                return False

            if self._check_health(port):
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

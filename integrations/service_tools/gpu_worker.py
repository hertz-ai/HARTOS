"""
gpu_worker.py — Persistent GPU subprocess worker for crash isolation.

Problem: PyTorch / CUDA OOM and DLL segfaults are C-level aborts that
Python's try/except CANNOT catch. When an in-process model crashes,
it takes the entire parent process with it. This is especially bad
for long-running services (Nunba, HARTOS backend) where one TTS
crash kills chat, STT, agents, and everything else.

Solution: run GPU models in a dedicated subprocess. When it crashes,
only the subprocess dies. The parent catches the exit code and falls
back to a CPU engine (Piper) or retries on the next request.

Architecture:
    Parent (Nunba/HARTOS)
        │
        │  JSON request (stdin)
        ▼
    Worker subprocess (python-embed or sys.executable)
        │  ├── load model once on startup
        │  ├── serve forever via stdin/stdout JSON lines
        │  └── exit non-zero on fatal error
        │
        │  JSON response (stdout) OR non-zero exit code
        ▼
    Parent catches failure → fallback

Usage:

    # Parent side
    worker = GPUWorker(
        name='f5_tts',
        module='integrations.service_tools.f5_tts_worker',
        startup_timeout=60,      # F5 takes ~40s to load
        request_timeout=120,     # longest inference we tolerate
    )
    worker.start()  # spawns subprocess, waits for READY handshake
    try:
        result = worker.call({
            'text': 'hello',
            'language': 'en',
            'voice': None,
            'output_path': '/tmp/out.wav',
        })
    except WorkerCrash:
        # Subprocess died — fall back to Piper
        pass

    # Worker side (in f5_tts_worker.py)
    from integrations.service_tools.gpu_worker import run_worker

    def load_model():
        from f5_tts.api import F5TTS
        return F5TTS()

    def synthesize(model, req):
        wav, sr, _ = model.infer(
            ref_file=req.get('voice') or '',
            ref_text='',
            gen_text=req['text'],
        )
        import soundfile as sf
        sf.write(req['output_path'], wav, sr)
        return {
            'path': req['output_path'],
            'duration': len(wav) / sr,
            'sample_rate': sr,
        }

    if __name__ == '__main__':
        run_worker(name='f5_tts', load=load_model, handle=synthesize)
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Cross-worker VRAM eviction registry
# ═══════════════════════════════════════════════════════════════════
#
# Every ToolWorker instance registers itself here so that when a new
# worker needs GPU memory and the VRAM manager reports insufficient
# headroom, we can evict the least-recently-used OTHER worker(s) to
# make room. Without this, spawning F5 on top of a loaded Chatterbox
# ML would OOM the subprocess — correct crash isolation, but wasted
# time because nobody told Chatterbox to step aside.

_REGISTRY_LOCK = threading.Lock()
_ALL_WORKERS: "List[weakref.ref['ToolWorker']]" = []


def _register_tool_worker(tw: "ToolWorker") -> None:
    """Register a ToolWorker so the cross-worker eviction sees it.

    Uses weakref so dropping a ToolWorker doesn't keep it alive.
    """
    with _REGISTRY_LOCK:
        # Prune dead refs opportunistically
        _ALL_WORKERS[:] = [r for r in _ALL_WORKERS if r() is not None]
        _ALL_WORKERS.append(weakref.ref(tw))


def _live_tool_workers() -> "List[ToolWorker]":
    """Return currently-alive ToolWorker instances (worker subprocess up)."""
    with _REGISTRY_LOCK:
        out = []
        for r in _ALL_WORKERS:
            tw = r()
            if tw is not None and tw.is_alive():
                out.append(tw)
        return out


def try_free_vram(needed_gb: float, exclude_tool: Optional[str] = None) -> bool:
    """Stop LRU workers until `needed_gb` GB of GPU VRAM is free.

    Called BEFORE spawning a heavy worker when the VRAM manager reports
    insufficient free VRAM. Iterates live workers sorted by last-used
    ascending (oldest first), skipping the worker identified by
    `exclude_tool`, and stops each one until free VRAM meets the
    threshold — or the registry runs out.

    Returns True if the required headroom was reached, False otherwise.
    """
    try:
        from integrations.service_tools.vram_manager import vram_manager
    except ImportError:
        return False

    free_gb = vram_manager.get_free_vram()
    if free_gb >= needed_gb:
        return True

    # Sort LRU first (smallest _last_used = longest idle)
    candidates = sorted(
        (tw for tw in _live_tool_workers() if tw.tool_name != exclude_tool),
        key=lambda w: w._last_used or 0.0,
    )

    for tw in candidates:
        logger.info(
            f"VRAM eviction: stopping {tw.tool_name} to free memory for "
            f"{exclude_tool or 'new worker'} "
            f"(need {needed_gb:.1f}GB, have {vram_manager.get_free_vram():.1f}GB)"
        )
        try:
            tw.stop()
        except Exception as e:
            logger.warning(f"eviction: failed to stop {tw.tool_name}: {e}")
            continue

        free_gb = vram_manager.get_free_vram()
        if free_gb >= needed_gb:
            logger.info(
                f"VRAM eviction: freed enough ({free_gb:.1f}GB) for "
                f"{exclude_tool or 'new worker'}"
            )
            return True

    logger.warning(
        f"VRAM eviction: couldn't free enough ({vram_manager.get_free_vram():.1f}GB "
        f"free, need {needed_gb:.1f}GB)"
    )
    return False


# ═══════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════

class WorkerError(Exception):
    """Base for worker errors."""


class WorkerCrash(WorkerError):
    """Subprocess died (exit code != 0 or stdout closed)."""


class WorkerTimeout(WorkerError):
    """Worker did not respond within the timeout."""


class WorkerNotReady(WorkerError):
    """Worker has not completed startup handshake."""


# ═══════════════════════════════════════════════════════════════════
# Parent-side: spawns and talks to the worker
# ═══════════════════════════════════════════════════════════════════

class GPUWorker:
    """Persistent GPU subprocess worker.

    Thread-safe: one lock serializes requests (GPU inference is not
    parallelizable on a single device anyway).
    """

    # Signal line the worker prints to stdout when ready to serve.
    READY_MARKER = '__WORKER_READY__'

    def __init__(
        self,
        name: str,
        module: str,
        startup_timeout: float = 60.0,
        request_timeout: float = 120.0,
        python_exe: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        args: Optional[list] = None,
    ):
        """
        Args:
            name: Worker name (for logs).
            module: Dotted module path to run via -m (e.g. 'tts.f5_worker').
            startup_timeout: Seconds to wait for READY marker.
            request_timeout: Seconds to wait for each request response.
            python_exe: Python to use. Defaults to python-embed if present,
                       else sys.executable.
            env: Extra env vars to pass to the subprocess.
            args: Extra CLI args appended after `-m module` (for variant
                  dispatch in modules with multiple worker entry points).
        """
        self.name = name
        self.module = module
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.python_exe = python_exe or _resolve_python_exe()
        self.env = env or {}
        self.args = list(args) if args else []

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ready = False
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stdout_queue: queue.Queue = queue.Queue()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the subprocess and wait for READY handshake."""
        with self._lock:
            if self._proc and self._proc.poll() is None and self._ready:
                return  # already running

            self._spawn()
            self._wait_ready()

    def is_alive(self) -> bool:
        """True if subprocess is running and READY."""
        return (
            self._proc is not None
            and self._proc.poll() is None
            and self._ready
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Gracefully stop the worker. Falls back to kill after timeout."""
        with self._lock:
            if not self._proc:
                return
            if self._proc.poll() is None:
                try:
                    # Send shutdown request. Worker exits on its own.
                    self._write_line(json.dumps({'op': 'shutdown'}))
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logger.warning(f"{self.name}: shutdown timeout, killing")
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            self._proc = None
            self._ready = False

    # ── Request/response ───────────────────────────────────────────

    def call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request to the worker and return the response.

        Raises:
            WorkerCrash: subprocess died or closed stdout.
            WorkerTimeout: no response within request_timeout.
            WorkerNotReady: worker not started or crashed during startup.
        """
        with self._lock:
            if not self.is_alive():
                raise WorkerNotReady(f"{self.name}: worker is not running")

            # Send request
            payload = json.dumps(request)
            try:
                self._write_line(payload)
            except (BrokenPipeError, OSError) as e:
                self._reap()
                raise WorkerCrash(f"{self.name}: write failed: {e}")

            # Poll loop: wake up every 250ms to check if process died.
            # This lets us distinguish "timeout" (worker stuck) from
            # "crash" (worker died) without waiting the full request_timeout
            # when the subprocess has already exited.
            deadline = time.monotonic() + self.request_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error(f"{self.name}: request timeout, killing stuck worker")
                    self._reap(force=True)
                    raise WorkerTimeout(
                        f"{self.name}: no response within {self.request_timeout}s"
                    )

                line = self._read_line_with_timeout(min(0.25, remaining))
                if line is not None:
                    break  # got a response

                # No line yet — is the process still alive?
                if self._proc is None or self._proc.poll() is not None:
                    code = self._proc.returncode if self._proc else -1
                    self._reap()
                    raise WorkerCrash(
                        f"{self.name}: subprocess died (exit={code})"
                    )

            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                raise WorkerError(
                    f"{self.name}: invalid JSON response: {e}: {line[:200]}"
                )

    # ── Internals ──────────────────────────────────────────────────

    def _spawn(self) -> None:
        """Launch the subprocess.

        IMPORTANT: propagates the parent process's current sys.path to the
        child via PYTHONPATH. Before the subprocess-isolation refactor, TTS
        engines ran in-process and inherited any runtime sys.path mutations
        automatically (e.g. Nunba's app.py prepends ~/.nunba/site-packages/
        where CUDA torch, regex, transformers, parler_tts actually live).
        Spawned subprocesses boot fresh from the python binary's default
        sys.path and can't see those entries — every transformers-based
        worker then crashes on `import regex` / `import transformers`.
        Fixing at the spawn layer means every worker (TTS, STT, VLM, any
        future engine) benefits without each one re-implementing the same
        path plumbing.
        """
        env = os.environ.copy()
        env.update(self.env)
        # Unbuffered so stdout/stderr come through immediately
        env['PYTHONUNBUFFERED'] = '1'

        # Propagate parent sys.path via PYTHONPATH so the child inherits
        # runtime-added package dirs (e.g. ~/.nunba/site-packages for CUDA
        # torch + TTS deps). Filter to existing dirs only — empty / stale
        # entries can mask imports. Preserve an existing PYTHONPATH in env
        # by appending our paths to the front (caller-set overrides last).
        _extra_paths = [
            p for p in sys.path
            if p and os.path.isdir(p)
        ]
        if _extra_paths:
            _existing = env.get('PYTHONPATH', '')
            _joined = os.pathsep.join(_extra_paths)
            if _existing:
                env['PYTHONPATH'] = _joined + os.pathsep + _existing
            else:
                env['PYTHONPATH'] = _joined

        # Windows: don't pop a console window
        creationflags = 0
        if sys.platform == 'win32':
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        cmd = [self.python_exe, '-u', '-m', self.module, *self.args]
        logger.info(f"{self.name}: spawning {' '.join(cmd)}")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creationflags,
            bufsize=1,                     # line-buffered
            text=True,
            encoding='utf-8',
            errors='replace',
        )

        # Drain stderr in a thread so it doesn't fill the pipe buffer.
        # Also forward worker log lines to our logger.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
        )
        self._stderr_thread.start()

        # Single persistent stdout reader thread that pumps lines into
        # a queue. Only ONE thread ever reads from proc.stdout — this
        # is essential because creating a new reader per-poll leaves
        # blocked readline() threads that corrupt subsequent reads.
        self._stdout_queue = queue.Queue()
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            daemon=True,
        )
        self._stdout_thread.start()
        self._ready = False

    def _drain_stderr(self) -> None:
        """Background thread: read worker stderr and log it."""
        if not self._proc or not self._proc.stderr:
            return
        try:
            for line in self._proc.stderr:
                line = line.rstrip()
                if line:
                    logger.info(f"[{self.name}] {line}")
        except Exception:
            pass  # pipe closed, process dead

    def _drain_stdout(self) -> None:
        """Background thread: read worker stdout into the line queue.

        Single reader thread guarantees no interleaved reads. Sentinel
        value None marks EOF so the main thread knows to stop waiting.
        """
        if not self._proc or not self._proc.stdout:
            return
        try:
            for line in self._proc.stdout:
                self._stdout_queue.put(line.rstrip('\n'))
        except Exception:
            pass  # pipe closed
        finally:
            self._stdout_queue.put(None)  # EOF sentinel

    def _wait_ready(self) -> None:
        """Block until worker prints READY marker or exits."""
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                code = self._proc.returncode if self._proc else -1
                raise WorkerCrash(f"{self.name}: died during startup (exit={code})")

            line = self._read_line_with_timeout(0.5)
            if line is None:
                continue
            if line.strip() == self.READY_MARKER:
                self._ready = True
                logger.info(f"{self.name}: worker ready")
                return
            # Ignore any other startup chatter
            logger.debug(f"[{self.name}] startup: {line}")

        # Timeout
        self._reap(force=True)
        raise WorkerTimeout(f"{self.name}: startup timeout ({self.startup_timeout}s)")

    def _write_line(self, line: str) -> None:
        if not self._proc or not self._proc.stdin:
            raise WorkerCrash(f"{self.name}: stdin closed")
        self._proc.stdin.write(line + '\n')
        self._proc.stdin.flush()

    def _read_line_with_timeout(self, timeout: float) -> Optional[str]:
        """Read one line from stdout with a timeout.

        Reads from the stdout queue populated by `_drain_stdout`. The
        queue handles the timeout natively and there's only ever one
        reader thread touching proc.stdout, so no corruption.

        Returns:
            The line (without trailing newline), or None if timeout or EOF.
        """
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        # None is the EOF sentinel from _drain_stdout
        if line is None:
            return None
        return line

    def _reap(self, force: bool = False) -> None:
        """Clean up after a crashed/stuck worker."""
        if not self._proc:
            return
        if self._proc.poll() is None:
            if force:
                self._proc.kill()
            else:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._ready = False


# ═══════════════════════════════════════════════════════════════════
# Worker-side: helper to run a model in a loop
# ═══════════════════════════════════════════════════════════════════

def run_worker(
    name: str,
    load: Callable[[], Any],
    handle: Callable[[Any, Dict[str, Any]], Dict[str, Any]],
) -> None:
    """Main loop for a worker subprocess.

    Args:
        name: Worker name (for logs written to stderr).
        load: Called once to load the model. Return anything — it's
              passed to `handle` as the first arg.
        handle: Called once per request. Receives (model, request_dict)
                and must return a JSON-serializable dict.

    Protocol:
        1. Worker loads the model.
        2. Worker writes READY marker to the protected protocol channel.
        3. For each stdin line (JSON request):
             - If op == 'shutdown', exit cleanly.
             - Otherwise call handle(model, request), write JSON response.
        4. Load failure → exit code 2.
        5. Recoverable handler error → error JSON on protocol channel, continue.
        6. stdin EOF → exit code 0.
        7. Unexpected fatal error → exit code 3.

    Stdout isolation:
        Before loading the model, we duplicate fd 1 to a private file
        handle (_protocol) and redirect fd 1 + sys.stdout to stderr.
        This means ANY library inside the worker (torch, F5, HF,
        transformers, CUDA init messages) that writes to stdout is
        safely rerouted to stderr, where the parent drains it into a
        log stream. The protocol channel stays 100% clean for JSON.
    """
    # ── Stdout isolation (C5 fix) ──────────────────────────────────
    # Save a private writer pointing at the ORIGINAL fd 1, then
    # redirect fd 1 and sys.stdout to stderr so no stray prints
    # corrupt the JSON protocol.
    _protocol_fd = os.dup(1)
    _protocol = os.fdopen(_protocol_fd, 'w', buffering=1, encoding='utf-8')
    try:
        os.dup2(2, 1)  # fd 1 -> fd 2 (stderr) — catches C-level writes too
    except OSError:
        pass  # unusual platforms / closed stderr
    sys.stdout = sys.stderr  # catches Python-level print()

    def _emit(obj) -> None:
        """Write one protocol message (JSON or marker string)."""
        if isinstance(obj, str):
            _protocol.write(obj + '\n')
        else:
            _protocol.write(json.dumps(obj) + '\n')
        _protocol.flush()

    # Route all logging to stderr to keep the protocol channel clean.
    # Explicit handler setup (not basicConfig) because other modules
    # imported by the worker may have already called basicConfig, which
    # would make ours a silent no-op.
    _root_logger = logging.getLogger()
    for h in list(_root_logger.handlers):
        _root_logger.removeHandler(h)
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter(f'[{name}] %(message)s'))
    _root_logger.addHandler(_h)
    _root_logger.setLevel(logging.INFO)
    worker_log = logging.getLogger(f'worker.{name}')

    # ── Phase 1: load model ────────────────────────────────────────
    try:
        worker_log.info('loading model...')
        t0 = time.time()
        model = load()
        worker_log.info(f'loaded in {time.time() - t0:.1f}s')
    except Exception as e:
        worker_log.exception(f'load failed: {e}')
        sys.exit(2)

    # ── Phase 2: announce ready ────────────────────────────────────
    _emit(GPUWorker.READY_MARKER)

    # ── Phase 3: serve requests ────────────────────────────────────
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                _emit({'error': f'invalid JSON: {e}'})
                continue

            if req.get('op') == 'shutdown':
                worker_log.info('shutdown requested')
                sys.exit(0)

            try:
                t0 = time.time()
                result = handle(model, req)
                if isinstance(result, dict):
                    result.setdefault('latency_ms', round((time.time() - t0) * 1000, 1))
                else:
                    result = {'result': result}
            except Exception as e:
                worker_log.exception('handler failed')
                result = {'error': f'{type(e).__name__}: {e}'}

            # Serialize defensively — a non-JSON-serializable field in the
            # result would otherwise raise inside this try and take down the
            # worker. Catch that and return a structured error.
            try:
                _emit(result)
            except (TypeError, ValueError) as e:
                _emit({'error': f'response serialization failed: {e}'})

    except KeyboardInterrupt:
        pass
    except Exception as e:
        worker_log.exception(f'fatal: {e}')
        sys.exit(3)

    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════
# Python resolver: prefer python-embed in frozen builds
# ═══════════════════════════════════════════════════════════════════

def _resolve_python_exe() -> str:
    """Return the Python executable to use for workers.

    Preference:
      1. $HARTOS_WORKER_PYTHON env var (explicit override)
      2. python-embed next to the frozen exe (Nunba bundled build)
      3. sys.executable (dev mode)
    """
    override = os.environ.get('HARTOS_WORKER_PYTHON')
    if override and os.path.isfile(override):
        return override

    # Frozen build: python-embed sibling to Nunba.exe
    if getattr(sys, 'frozen', False):
        app_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidate = os.path.join(app_dir, 'python-embed', 'python.exe')
        if os.path.isfile(candidate):
            return candidate

    return sys.executable


# ═══════════════════════════════════════════════════════════════════
# High-level helper: one-call tool wrapper
# ═══════════════════════════════════════════════════════════════════
#
# Each GPU tool (F5, Chatterbox, CosyVoice, Indic Parler, Whisper, ...)
# needs the same parent-side boilerplate:
#   - Lazy singleton GPUWorker
#   - Thread-safe get-or-start
#   - Call → catch crash → return transient error
#   - VRAM budget allocation
#   - Output path auto-generation
#   - Uniform result JSON shape
#
# `ToolWorker` encapsulates ALL of it. Per-tool modules only specify:
#   - worker module name
#   - VRAM tool name (for budget allocation)
#   - output subdir (for auto-generated paths)
#   - engine display name
#
# Single responsibility: GPUWorker = IPC. ToolWorker = tool lifecycle.

class ToolWorker:
    """Reusable parent-side wrapper for GPU tools.

    Holds a lazily-started GPUWorker and exposes a single `synthesize()`
    method that handles: worker start, crash recovery, VRAM budget,
    output path generation, and result JSON shaping.

    One instance per tool, module-level singleton is typical.
    """

    # Central entry module — every worker spawns through here
    _DISPATCHER = 'integrations.service_tools.gpu_worker'

    def __init__(
        self,
        *,
        tool_name: str,
        tool_module: Optional[str] = None,
        vram_budget: str,
        output_subdir: str,
        engine: str,
        variant: Optional[str] = None,
        startup_timeout: float = 90.0,
        request_timeout: float = 120.0,
        idle_timeout: float = 300.0,
        # Back-compat aliases — callers on the old API still work
        worker_module: Optional[str] = None,
        worker_args: Optional[list] = None,
    ):
        """
        Args:
            tool_name: Logical tool name (e.g. 'f5_tts').
            tool_module: Dotted path to the library module that defines
                         `_load` / `_synthesize` (and variant-suffixed
                         versions). The parent spawns the centralized
                         dispatcher which imports this module in the
                         subprocess. No `__main__` block needed in the
                         tool module itself.
            vram_budget: Key in VRAM_BUDGETS (e.g. 'tts_f5').
            output_subdir: Subdir under ~/.hevolve/models for auto outputs.
            engine: Display name for the result JSON (e.g. 'f5-tts').
            variant: Optional variant suffix (e.g. 'turbo', 'ml'). When
                     set, the dispatcher picks `_load_<variant>` and
                     `_synthesize_<variant>` instead of the plain names.
            startup_timeout: Seconds to wait for worker READY handshake.
            request_timeout: Max seconds for a single request.
            idle_timeout: Seconds of inactivity after which the worker is
                          auto-stopped to free VRAM. Default 5 min.
                          Set to 0 to disable auto-stop.

            worker_module / worker_args: DEPRECATED — legacy aliases. If
                `tool_module` is not given, we fall back to `worker_module`.
                If `worker_args=['<variant>']` is given, we fall back to
                that for `variant`. New callers should use `tool_module`
                + `variant` directly.
        """
        # Resolve the library module the worker should import
        self.tool_module = tool_module or worker_module
        if self.tool_module is None:
            raise ValueError(
                f'{tool_name}: ToolWorker needs tool_module (library to run)'
            )

        # Resolve the variant — prefer explicit, fall back to legacy args
        if variant is None and worker_args:
            variant = worker_args[0] if worker_args else None
        self.variant = variant

        self.tool_name = tool_name
        self.vram_budget = vram_budget
        self.output_subdir = output_subdir
        self.engine = engine
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.idle_timeout = idle_timeout

        self._worker: Optional[GPUWorker] = None
        self._lock = threading.Lock()
        # Init to NOW so a brand-new worker isn't LRU-evicted immediately.
        # The old 0.0 default made fresh workers look like the oldest idle
        # in the LRU sort, causing premature eviction before first use.
        self._last_used: float = time.monotonic()
        self._idle_timer: Optional[threading.Timer] = None
        # State change observers. Listeners receive (tool_name, event)
        # where event is 'spawned' | 'stopped' | 'crashed'. Fired AFTER
        # the state transition is complete so observers can probe
        # is_alive() and get the new state.
        self._observers: list = []

        # Register with the cross-worker registry so the LRU eviction
        # policy can see this worker when other tools need VRAM.
        _register_tool_worker(self)

    # Back-compat shims for properties the tests still read
    @property
    def worker_module(self) -> str:
        """Compat alias — old tests read this."""
        return self.tool_module

    @property
    def worker_args(self) -> list:
        """Compat alias — old tests read this as ['<variant>'] or []."""
        return [self.variant] if self.variant else []

    # ── Observer API ──────────────────────────────────────────────

    def add_observer(self, callback: Callable[[str, str], None]) -> None:
        """Register a state change listener.

        The callback will be invoked with (tool_name, event) on every
        state transition: 'spawned' when the worker subprocess becomes
        READY, 'stopped' when it's cleanly stopped (user action or
        idle auto-stop), 'crashed' when the subprocess dies mid-call.

        Called after the transition so listeners can call .is_alive()
        and see ground truth. Exceptions in listeners are swallowed —
        a broken observer must never take down the worker.
        """
        self._observers.append(callback)

    def remove_observer(self, callback: Callable[[str, str], None]) -> None:
        """Unregister a previously-added observer (no-op if not present)."""
        try:
            self._observers.remove(callback)
        except ValueError:
            pass

    def _notify(self, event: str) -> None:
        """Fire a state-change event to every registered observer."""
        for cb in list(self._observers):
            try:
                cb(self.tool_name, event)
            except Exception as e:
                logger.debug(f"observer {cb} failed for {event}: {e}")

    # ── Public API ────────────────────────────────────────────────

    def call(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a request to the worker. Handles start, crash, timeout.

        Returns the worker's raw response dict (with an 'error' key on
        transient failure). Does NOT shape the result — callers that
        want uniform JSON output should use `synthesize()` instead.
        """
        # VRAM allocation moved INSIDE _get_or_start (after the _worker is
        # None check) to prevent double-counting when two concurrent call()
        # invocations both allocate before either enters the lock. See T138.
        try:
            worker = self._get_or_start()
        except (WorkerCrash, WorkerTimeout, WorkerError) as e:
            return {'error': f'{self.tool_name} worker startup failed: {e}'}

        try:
            result = worker.call(request)
        except (WorkerCrash, WorkerTimeout) as e:
            # Subprocess died. Worker will respawn on next call.
            logger.warning(f"{self.tool_name}: worker crash: {e}")
            self._notify('crashed')
            return {'error': f'{self.tool_name} crashed: {e}', 'transient': True}
        except WorkerError as e:
            return {'error': f'{self.tool_name} worker error: {e}'}

        # Request succeeded — reset idle timer
        self._touch_idle()
        return result

    def synthesize(
        self,
        *,
        text: str,
        language: str = 'en',
        voice: Optional[str] = None,
        output_path: Optional[str] = None,
        default_sample_rate: int = 24000,
        extra_request: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Complete TTS synthesis with uniform JSON output.

        Handles all the per-tool boilerplate (text validation, output
        path generation, result shaping) so the tool module is ~10 lines.

        Returns a JSON string matching the original in-process tool
        output shape for drop-in compatibility.
        """
        if not text or not text.strip():
            return json.dumps({'error': 'Text is required'})

        if output_path is None:
            output_path = str(
                self._get_output_dir() / f'{self.tool_name}_{int(time.time() * 1000)}.wav'
            )

        request = {
            'text': text,
            'language': language,
            'voice': voice,
            'output_path': output_path,
        }
        if extra_request:
            request.update(extra_request)

        t0 = time.time()
        result = self.call(request)
        elapsed = time.time() - t0

        if 'error' in result:
            return json.dumps(result)

        duration = result.get('duration', 0)
        return json.dumps({
            'path': result.get('path', output_path),
            'duration': duration,
            'engine': result.get('engine', self.engine),
            'device': result.get('device', 'cuda'),
            'sample_rate': result.get('sample_rate', default_sample_rate),
            'voice': voice or 'default',
            'language': language,
            'latency_ms': round(elapsed * 1000, 1),
            'rtf': round(elapsed / duration, 4) if duration > 0 else 0,
        })

    def is_alive(self) -> bool:
        """True if the worker subprocess is running and READY."""
        w = self._worker
        return w is not None and w.is_alive()

    def set_idle_timeout(self, seconds: float) -> None:
        """Update the idle auto-stop threshold.

        Called by the model loader when the catalog entry's
        idle_timeout_s changes, so admin-UI edits take effect without
        restarting the worker. If a worker is currently running and
        the timeout was previously disabled, arms the timer now.
        """
        self.idle_timeout = max(0.0, float(seconds))
        if self.idle_timeout > 0 and self.is_alive():
            # Re-arm timer with the new deadline
            self._touch_idle()
        elif self.idle_timeout <= 0 and self._idle_timer is not None:
            with self._lock:
                self._idle_timer.cancel()
                self._idle_timer = None

    def stop(self) -> None:
        """Stop the worker and release VRAM."""
        was_running = False
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if self._worker is not None:
                was_running = True
                try:
                    self._worker.stop()
                except Exception:
                    pass
                self._worker = None
        self._release_vram()
        logger.info(f"{self.tool_name}: worker stopped")
        if was_running:
            self._notify('stopped')

    # ── Idle timeout ─────────────────────────────────────────────

    def _touch_idle(self) -> None:
        """Record that the worker was just used; (re)arm the idle timer."""
        if self.idle_timeout <= 0:
            return
        with self._lock:
            self._last_used = time.monotonic()
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = threading.Timer(self.idle_timeout, self._on_idle)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _on_idle(self) -> None:
        """Fired when idle_timeout elapses with no requests.

        TOCTOU fix (T138): the elapsed-time check AND the stop decision
        both run INSIDE the lock. The old code checked elapsed under the
        lock, then called stop() OUTSIDE it — a concurrent call() could
        update _last_used between the check and the stop, killing a
        worker that was actively serving a request.
        """
        if self.idle_timeout <= 0:
            return
        with self._lock:
            elapsed = time.monotonic() - self._last_used
            if elapsed + 0.5 < self.idle_timeout:
                return  # someone used it recently, skip
            # Stop INSIDE the lock so no concurrent call() can slip in
            # between the elapsed check and the actual termination.
            if self._worker is not None and self._worker.is_alive():
                logger.info(
                    f"{self.tool_name}: idle for {self.idle_timeout:.0f}s, "
                    f"stopping worker to free VRAM"
                )
                try:
                    self._worker.stop()
                except Exception:
                    pass
                self._worker = None
                self._release_vram()
        # Notify OUTSIDE the lock (observers may call back into us)
        self._notify('stopped')

    # ── Internals ─────────────────────────────────────────────────

    def _get_or_start(self) -> GPUWorker:
        """Lazy singleton: start the worker on first call or after a crash.

        Spawns the centralized dispatcher (`gpu_worker` module) with the
        target tool module (+ optional variant) as CLI args. Tool modules
        do NOT need their own `if __name__ == '__main__':` blocks — the
        dispatcher imports them and picks up `_load` / `_synthesize`.
        """
        spawned = False
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                # Allocate VRAM INSIDE the lock so concurrent call()
                # invocations don't double-count. T138 fix (c).
                # If allocate() returns False (GPU full), try evicting
                # LRU workers first, then retry. If still False after
                # eviction, the spawn will proceed but on CPU fallback
                # path (subprocess self-detects VRAM at _load time).
                allocated = self._allocate_vram()

                # Cross-worker VRAM eviction: if our budget won't fit
                # in current free VRAM, stop LRU other workers to make
                # room BEFORE spawning. Prevents a predictable OOM in
                # the new subprocess while older idle workers hold
                # memory they're not actively using.
                self._ensure_vram_headroom()

                # Retry allocate after eviction so the budget is tracked.
                if not allocated:
                    self._allocate_vram()

                cli_args = [self.tool_module]
                if self.variant:
                    cli_args.append(self.variant)
                self._worker = GPUWorker(
                    name=self.tool_name,
                    module=self._DISPATCHER,
                    startup_timeout=self.startup_timeout,
                    request_timeout=self.request_timeout,
                    args=cli_args,
                )
                self._worker.start()
                spawned = True
            worker = self._worker
        if spawned:
            # Fire observer event OUTSIDE the lock so callbacks can
            # safely call back into this ToolWorker without deadlocking.
            self._notify('spawned')
        return worker

    def _ensure_vram_headroom(self) -> None:
        """Evict LRU workers if VRAM is too tight for this tool to fit.

        Looks up this tool's declared VRAM budget from vram_manager.
        If current free VRAM is below that budget, calls try_free_vram
        to stop other workers (oldest-idle first). Silent no-op when
        the tool has no registered budget or vram_manager is unavailable.
        """
        try:
            from integrations.service_tools.vram_manager import (
                vram_manager, VRAM_BUDGETS,
            )
        except ImportError:
            return
        budget = VRAM_BUDGETS.get(self.vram_budget)
        if not budget:
            return
        _min_vram, model_gb = budget
        free_gb = vram_manager.get_free_vram()
        if free_gb >= model_gb:
            return
        try_free_vram(needed_gb=model_gb, exclude_tool=self.tool_name)

    def _get_output_dir(self) -> Path:
        d = Path(os.environ.get(
            'HEVOLVE_MODEL_DIR',
            os.path.expanduser('~/.hevolve/models'),
        )) / self.output_subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _allocate_vram(self) -> bool:
        """Attempt to reserve VRAM. Returns True on success, False if
        the allocation was refused (caller may fall back to CPU).

        This honors the vram_manager.allocate contract: False means
        'won't fit', not 'error'. Exceptions during import/allocation
        are treated as 'unknown state -> allow', matching prior
        default-allow behavior while still surfacing real refusals.
        """
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            return bool(get_vram_manager().allocate(self.vram_budget))
        except ImportError:
            return True
        except Exception:
            return True

    def _release_vram(self) -> None:
        try:
            from integrations.service_tools.vram_manager import get_vram_manager
            get_vram_manager().release(self.vram_budget)
        except (ImportError, Exception):
            pass


# ═══════════════════════════════════════════════════════════════════
# Centralized worker entry point
# ═══════════════════════════════════════════════════════════════════
#
# ONE `if __name__ == '__main__':` block for ALL GPU workers. When the
# parent spawns `python -m integrations.service_tools.gpu_worker
# <tool_module> [variant]`, this dispatcher:
#
#   1. Dynamically imports the tool module.
#   2. Picks the right load/handle callbacks via convention:
#        - no variant      → module._load,  module._synthesize
#        - variant='turbo' → module._load_turbo, module._synthesize_turbo
#        - variant='<v>'   → module._load_<v>,   module._synthesize_<v>
#      (Fallback: if the variant-specific name doesn't exist, use the
#       plain _load / _synthesize.)
#   3. Calls run_worker(...) — the infinite request loop.
#
# Tool modules do NOT need their own `if __name__ == '__main__':` block.
# They just define `_load` and `_synthesize` (plus variants if needed).
# This keeps the "entry point" concern in exactly one place.

def _dispatch_and_run(tool_module_name: str, variant: Optional[str] = None) -> None:
    """Import a tool module and run its worker loop.

    Picks `_load_<variant>` + `_synthesize_<variant>` when variant is
    set, else `_load` + `_synthesize`. Worker name is the module's
    last segment plus the variant suffix.
    """
    import importlib

    try:
        mod = importlib.import_module(tool_module_name)
    except Exception as e:
        # Log to stderr and exit non-zero so parent sees WorkerCrash
        print(f'[gpu_worker] import failed for {tool_module_name}: {e}', file=sys.stderr)
        sys.exit(2)

    suffix = f'_{variant}' if variant else ''
    load_name = f'_load{suffix}'
    handle_name = f'_synthesize{suffix}'

    load = getattr(mod, load_name, None) or getattr(mod, '_load', None)
    handle = getattr(mod, handle_name, None) or getattr(mod, '_synthesize', None)

    if load is None or handle is None:
        missing = []
        if load is None:
            missing.append(load_name if variant else '_load')
        if handle is None:
            missing.append(handle_name if variant else '_synthesize')
        print(
            f'[gpu_worker] {tool_module_name} missing callbacks: {missing}',
            file=sys.stderr,
        )
        sys.exit(2)

    base_name = tool_module_name.rsplit('.', 1)[-1]
    worker_name = f'{base_name}{suffix}' if variant else base_name
    run_worker(name=worker_name, load=load, handle=handle)


if __name__ == '__main__':
    # Usage:  python -m integrations.service_tools.gpu_worker <module> [variant]
    if len(sys.argv) < 2:
        print(
            'usage: python -m integrations.service_tools.gpu_worker '
            '<tool.module.path> [variant]',
            file=sys.stderr,
        )
        sys.exit(2)
    _tool_module = sys.argv[1]
    _variant = sys.argv[2] if len(sys.argv) > 2 else None
    _dispatch_and_run(_tool_module, _variant)

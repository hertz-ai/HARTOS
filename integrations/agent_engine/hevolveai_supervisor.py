"""
hevolveai supervisor -- owns the HevolveAI API-server subprocess.

PURPOSE
-------
Nunba (bundled HARTOS) wants to talk to the HevolveAI brain over the
WorldModelBridge.  In bundled mode the bridge can run either:

  (A) in-process -- direct Python calls when an IntegratedRealtimeAgent
      lives in the same interpreter; OR
  (B) HTTP fallback -- when HevolveAI is a separate process exposing the
      OpenAI-compatible API on http://localhost:8000 .

Path (B) is the production shape for end-user installs: heavy ML imports
stay out of the Nunba interpreter, and the bridge stays decoupled from
HevolveAI's startup spike.  This module is the launcher for that
subprocess.

LIFECYCLE BINDING (Windows)
---------------------------
The child MUST die when Nunba dies.  Otherwise a stale uvicorn keeps
port 8000 bound across reboots and the next Nunba startup races against
its own zombie.  We use a Windows Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (0x2000): when the parent process
exits -- cleanly, via Task Manager, or via crash -- the Job handle is
released by the kernel, which kills every process assigned to the job.

To make this work the child is spawned with ``CREATE_BREAKAWAY_FROM_JOB``
so it doesn't inherit any pre-existing job the parent is in (HARTOS
already assigns the parent to its own resource-governor job for CPU/RAM
caps; nested jobs would otherwise conflict on older Windows).  We then
``AssignProcessToJobObject`` immediately after Popen returns -- small
race window measured in microseconds; if Nunba crashes inside it the
child becomes a one-off orphan but never accumulates.  NOTE: if the Job
Object cannot be created OR the assign call fails, the child is NOT
lifecycle-bound on Windows and relies on the POSIX-style atexit fallback
only -- a hard parent crash in that degraded state can leak one uvicorn.
Both failures are logged at WARNING so the degraded mode is visible.

POSIX FALLBACK
--------------
Linux/macOS use ``start_new_session`` + an atexit terminate hook.  Less
robust than Windows Job Object but the primary deployment target is
Nunba on Windows, where Job Object semantics are exact.

RESOURCE GOVERNOR ACCOUNTING
----------------------------
The spawned PID is registered with ``core.resource_governor.get_governor()
.register_subprocess(name='hevolveai', pid=...)`` so HARTOS's monitor
sees it in stats and future mode transitions can broadcast to it.

INTENT
------
This file is the COUNTERPART to ``_init_agent_engine_subsystem`` in
``hartos_bootstrap.py``: spawn HevolveAI BEFORE the WorldModelBridge
eager-init runs, set ``HEVOLVEAI_API_URL`` in the parent env so the
bridge picks up HTTP mode, then let the bridge attach to the WAMP
hivemind 0x05 channel as a federation participant.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger('hevolve_agent_engine')


# -- Windows Job Object constants -------------------------------------
# (Mirrors the values used in core/resource_governor.py for the parent
# process's own resource cage.  Defined inline here to avoid coupling
# this module to the governor's private structs.)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK      = 0x0800
_JOBOBJECT_EXTENDED_LIMIT_INFO_CLS  = 9    # JobObjectExtendedLimitInformation
_JOBOBJECT_CPU_RATE_CONTROL_INFO_CLS = 15  # JobObjectCpuRateControlInformation
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4

_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_CREATE_NO_WINDOW          = 0x08000000

# Governor-mode -> CPU fraction the child is allowed to use of one
# logical core's worth of CPU.  These match the GOVERNOR's intent for
# the parent process so the child tightens / loosens in lockstep:
#   ACTIVE -- user is busy, child stays out of the way (5%)
#   IDLE   -- user is away, child can train hard (50%)
#   SLEEP  -- battery / system pressure, child essentially paused (1%)
# Values express percent-of-one-core; multiply by 100 for the Job
# Object's CpuRate field (units of 1/100th of a percent).
_MODE_CPU_FRACTION = {
    'active': 0.05,
    'idle':   0.50,
    'sleep':  0.01,
}


# -- Configuration ----------------------------------------------------
def _hevolveai_api_url() -> str:
    """Default URL the parent bridge will read after we spawn the child.

    An explicit ``HEVOLVEAI_API_URL`` always wins.  Otherwise the URL is
    DERIVED from ``HEVOLVEAI_PORT`` so that overriding only the port
    (e.g. ``HEVOLVEAI_PORT=9000``) keeps the bridge and the spawned child
    in agreement -- a bare ``localhost:8000`` default would otherwise make
    the bridge talk to 8000 while the child binds 9000.
    """
    explicit = os.environ.get('HEVOLVEAI_API_URL')
    if explicit:
        return explicit
    return f'http://localhost:{_hevolveai_port()}'


def _hevolveai_port() -> int:
    """Port the spawned uvicorn binds.  Defaults to 8000."""
    try:
        return int(os.environ.get('HEVOLVEAI_PORT', '8000'))
    except ValueError:
        return 8000


def _hevolveai_available() -> bool:
    """True when the child interpreter will be able to ``import
    hevolveai`` -- either it's importable in THIS process (frozen Nunba
    runs the child under the same python-embed site-packages, so a
    parent find_spec is representative) OR a dev PYTHONPATH resolves to
    the sibling source repo (which we surface to the child).

    This is the missing pre-spawn detection that the sibling
    supervisors already have (LiveKit checks the binary exists,
    WhatsApp breaks on FileNotFoundError).  Without it, on any box
    where hevolveai is not installed AND no dev sibling repo exists,
    the supervisor spawned a child that exits rc=1 in <1s and looped
    at the 60s backoff cap forever (live log: 889 'exited rc=1' lines,
    plus 14 spurious AssignProcessToJobObject err=0 from binding a
    process that already died).
    """
    try:
        import importlib.util as _ilu
        if _ilu.find_spec('hevolveai') is not None:
            return True
    except Exception:
        pass
    # Dev fallback: the sibling repo path that gets put on the child's
    # PYTHONPATH so it can import hevolveai even when not pip-installed.
    return _resolve_hevolveai_pythonpath() is not None


def supervisor_should_run() -> bool:
    """Skip the spawn when an operator has opted out OR hevolveai is
    not installed/available on this box.

    Disabled when:
      * ``HEVOLVE_SKIP_HEVOLVEAI_SPAWN=1`` (force the bridge into HTTP
        no-op mode; useful when HevolveAI is hosted on a different box).
      * ``HEVOLVEAI_API_URL`` points to a non-localhost host (we never
        spawn a remote target's server locally).
      * hevolveai is neither importable nor resolvable via a dev
        sibling repo -- spawning would crash-loop (see
        ``_hevolveai_available``).
    """
    if os.environ.get('HEVOLVE_SKIP_HEVOLVEAI_SPAWN') == '1':
        return False
    url = os.environ.get('HEVOLVEAI_API_URL', '')
    if url and ('localhost' not in url) and ('127.0.0.1' not in url):
        return False
    if not _hevolveai_available():
        return False
    return True


def _resolve_python_exe() -> str:
    """Pick the interpreter the child should run under.

    Frozen Nunba: ``<app_dir>/python-embed/python.exe``.  Using
    ``sys.executable`` directly would launch a new Nunba GUI instance
    instead of starting Python.  Mirrors the resolution in
    ``integrations/audio/diarization_service.py``.

    Dev mode (PyCharm): plain ``sys.executable`` is the active venv's
    python -- exactly what the developer expects.
    """
    if getattr(sys, 'frozen', False):
        app_dir = os.path.dirname(sys.executable)
        embed_python = os.path.join(app_dir, 'python-embed', 'python.exe')
        if os.path.isfile(embed_python):
            return embed_python
        logger.warning(
            "hevolveai_supervisor: python-embed/python.exe not found at %s; "
            "falling back to sys.executable", embed_python)
    return sys.executable


def _resolve_hevolveai_pythonpath() -> Optional[str]:
    """Locate the HevolveAI source root for dev-mode spawns.

    In frozen Nunba HevolveAI is installed under
    ``python-embed/Lib/site-packages/hevolveai`` and is importable
    natively -- no PYTHONPATH munging needed.

    In dev mode the sibling repo is at
    ``~/PycharmProjects/Hevolveai/src``; we surface that on PYTHONPATH
    so the spawned interpreter can ``import hevolveai`` even when the
    package is not pip-installed into its venv.  Override via
    ``HEVOLVEAI_HOME``.
    """
    override = os.environ.get('HEVOLVEAI_HOME', '').strip()
    if override:
        candidate = os.path.join(override, 'src')
        if os.path.isdir(os.path.join(candidate, 'hevolveai')):
            return candidate
        if os.path.isdir(os.path.join(override, 'hevolveai')):
            return override

    if getattr(sys, 'frozen', False):
        return None  # bundled -- hevolveai already on sys.path

    # Dev fallback: sibling repo next to hartos.
    here = Path(__file__).resolve()
    # hartos/integrations/agent_engine/hevolveai_supervisor.py -> hartos/
    hartos_root = here.parent.parent.parent
    candidate = hartos_root.parent / 'Hevolveai' / 'src'
    if (candidate / 'hevolveai').is_dir():
        return str(candidate)
    return None


# Canonical armored-import snippet for the spawned hevolveai server.
#
# Read the bundle dir + key file that app.py exports (HEVOLVE_ARMORED_DIR /
# HEVOLVE_ARMOR_KEY_FILE) and install the Hevolvearmor import hook BEFORE
# importing hevolveai — so the server loads the encrypted .enc modules instead
# of any (possibly stale) .pyd.
#
# Loader API: ``hevolvearmor._loader.install_loader(dir, raw_key)`` — the
# RAW-KEY entry.  Our producer (scripts/armor_hevolveai.py) encrypts with a
# random 32-byte key stored in _key.bin, so the raw-key loader is the matching
# decryptor.  (The Rust ``hevolvearmor.install`` takes a *passphrase* string, not
# a raw key, so it cannot open these bundles — verified 2026-06-01.)  package
# names auto-detect from the bundle's subdirs (hevolveai, embodied_ai).
#
# ONE mechanism, presence-gated, NO flag: same env vars the in-process loader
# uses, activated purely by the bundle being staged.  When the env vars / bundle
# / hevolvearmor package are absent (dev), it is a silent no-op and the import
# that follows loads the plain on-disk package — byte-identical in effect to the
# pre-armor boot.  test_supervisor_armored_spawn.py pins the env-var contract +
# round-trips the snippet against a real bundle so it cannot silently rot.
_ARMOR_INSTALL_SNIPPET = (
    "import os\n"
    "try:\n"
    "    _ad = os.environ.get('HEVOLVE_ARMORED_DIR', '')\n"
    "    _kf = os.environ.get('HEVOLVE_ARMOR_KEY_FILE', '')\n"
    "    if _ad and os.path.isdir(_ad) and _kf and os.path.isfile(_kf):\n"
    "        from hevolvearmor._loader import install_loader\n"
    "        _raw = open(_kf, 'rb').read().strip()\n"
    "        _k = _raw if len(_raw) == 32 else bytes.fromhex(_raw.decode('ascii'))\n"
    "        install_loader(_ad, _k)\n"
    "except Exception:\n"
    "    pass\n"
)


def _port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(('127.0.0.1', port)) == 0
    except OSError:
        return False


# -- Job Object helpers -----------------------------------------------
class _JobHandle:
    """Owns a Windows Job Object handle with KILL_ON_JOB_CLOSE set.

    Keep one instance alive in module scope for the lifetime of the
    parent process -- releasing the last reference closes the handle,
    which kills every assigned process.  That is the intended lifecycle
    binding; do NOT call ``.close()`` from supervisor restart code.
    """

    def __init__(self) -> None:
        self.handle: Optional[int] = None
        self._kernel32 = None

    def create(self) -> Optional[int]:
        """Create the Job Object and set the kill-on-close + breakaway-ok
        limit flags.  Returns the handle (a Windows HANDLE as int) or
        None if anything fails -- caller falls back to no-binding spawn.
        """
        if sys.platform != 'win32':
            return None
        try:
            # use_last_error=True so ctypes captures the per-call Win32
            # error into get_last_error() (windll.kernel32 does NOT, so
            # the error code logged on Assign failure would be stale).
            self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)  # type: ignore[attr-defined]
            # Declare argtypes/restype so 64-bit HANDLEs round-trip without
            # truncation. Without this, ctypes treats the HANDLE return as a
            # 32-bit int and a handle above 0x7FFFFFFF gets sign-corrupted,
            # silently breaking the kill-on-close binding this file exists for.
            k = self._kernel32
            k.CreateJobObjectW.restype = ctypes.c_void_p
            k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k.SetInformationJobObject.restype = ctypes.c_bool
            k.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
            k.AssignProcessToJobObject.restype = ctypes.c_bool
            k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self.handle = self._kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                logger.warning(
                    "hevolveai_supervisor: CreateJobObjectW failed; "
                    "child will NOT be killed on parent exit")
                self.handle = None
                return None
            if not self._set_limits():
                # SetInformationJobObject failed.  Leave the handle open;
                # AssignProcessToJobObject still works, just no kill-on-close.
                logger.warning(
                    "hevolveai_supervisor: SetInformationJobObject failed; "
                    "Job Object created without KILL_ON_JOB_CLOSE")
            return self.handle
        except Exception as e:  # pragma: no cover -- defensive
            logger.warning(
                "hevolveai_supervisor: Job Object setup failed: %s", e)
            self.handle = None
            return None

    def _set_limits(self) -> bool:
        """Apply KILL_ON_JOB_CLOSE + BREAKAWAY_OK to the job."""
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (matches resource_governor.py)
        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [('_' + str(i), ctypes.c_ulonglong) for i in range(6)]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', ctypes.c_ulong),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', ctypes.c_ulong),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', ctypes.c_ulong),
                ('SchedulingClass', ctypes.c_ulong),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', _IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        ok = self._kernel32.SetInformationJobObject(
            self.handle,
            _JOBOBJECT_EXTENDED_LIMIT_INFO_CLS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return bool(ok)

    def assign(self, proc_handle: int) -> bool:
        """Assign a child process to this job.  proc_handle must be a
        Windows HANDLE (subprocess.Popen._handle is one)."""
        if self.handle is None or self._kernel32 is None:
            return False
        try:
            ok = self._kernel32.AssignProcessToJobObject(
                self.handle, int(proc_handle))
            if not ok:
                err = ctypes.get_last_error()
                logger.warning(
                    "hevolveai_supervisor: AssignProcessToJobObject "
                    "failed (err=%d); child WILL outlive parent on crash",
                    err)
            return bool(ok)
        except Exception as e:  # pragma: no cover
            logger.warning(
                "hevolveai_supervisor: AssignProcessToJobObject "
                "exception: %s", e)
            return False

    def set_cpu_rate(self, cpu_fraction: float) -> bool:
        """Live-update the job's CPU rate cap.

        Reuses the existing Job Object so the running child immediately
        sees the new cap -- no restart required.  ``cpu_fraction`` is a
        fraction of ONE logical core (0.05 = 5% of one core).  Windows
        clamps the rate field at 1 (0.01%); we floor at 100 (1%) so
        SLEEP mode doesn't drop to literal zero and starve the child
        out of even emitting its shutdown logs.
        """
        if self.handle is None or self._kernel32 is None:
            return False
        try:
            class _RATE(ctypes.Structure):
                _fields_ = [
                    ('ControlFlags', ctypes.c_ulong),
                    ('Value', ctypes.c_ulong),
                ]
            info = _RATE()
            info.ControlFlags = (
                _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            )
            info.Value = max(100, int(cpu_fraction * 10000))
            ok = self._kernel32.SetInformationJobObject(
                self.handle,
                _JOBOBJECT_CPU_RATE_CONTROL_INFO_CLS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            return bool(ok)
        except Exception as e:  # pragma: no cover
            logger.debug(
                "hevolveai_supervisor: SetInformationJobObject "
                "CpuRate failed: %s", e)
            return False


_JOB = _JobHandle()


# -- Supervisor lifecycle ---------------------------------------------
from core.process_supervisor import ProcessSupervisor


class _Supervisor(ProcessSupervisor):
    """Owns the HevolveAI uvicorn subprocess for the lifetime of HARTOS.

    Daemon thread; the OS-level Job Object KILL_ON_JOB_CLOSE flag is the
    primary kill mechanism on Windows.  ``stop()`` is the graceful
    secondary path for clean HARTOS shutdown.  The spawn/stream/backoff loop
    lives in core.process_supervisor.ProcessSupervisor (#110); the #59
    fast-fail/unhealthy circuit breakers + Windows Job-Object bind + governor
    registration live in the _on_started / _on_child_exit hooks below.
    """

    name = 'hevolveai'
    # #59: reset the backoff after a healthy run so a transient crash after
    # hours of uptime respawns promptly (the base resets at this uptime).
    reset_backoff_after = 60.0

    def __init__(self) -> None:
        super().__init__()
        self.python_exe: str = _resolve_python_exe()
        self.pythonpath: Optional[str] = _resolve_hevolveai_pythonpath()
        self.port: int = _hevolveai_port()
        self.api_url: str = _hevolveai_api_url()
        # Circuit-breaker state — persisted across respawns (see _on_child_exit).
        self._consecutive_fast_fails = 0
        self._consecutive_unhealthy = 0
        self._current_pid: Optional[int] = None

    def start(self) -> Dict[str, Any]:
        """Idempotent -- call once from HARTOS bootstrap."""
        if self.thread is not None and self.thread.is_alive():
            return self.info()

        if _port_in_use(self.port):
            self.last_error = (
                f'port {self.port} already in use; assuming an '
                f'operator-managed hevolveai server is running and '
                f'skipping spawn')
            logger.info("hevolveai_supervisor: %s", self.last_error)
            # Still expose the URL so the bridge picks HTTP mode.
            os.environ['HEVOLVEAI_API_URL'] = self.api_url
            return self.info()

        # Surface the URL to the parent env BEFORE the bridge constructs
        # -- otherwise WorldModelBridge.__init__ sees an empty
        # HEVOLVEAI_API_URL and locks itself into http_disabled=True
        # (see world_model_bridge.py L109-115).
        os.environ['HEVOLVEAI_API_URL'] = self.api_url

        # Prime the Job Object once (idempotent on retries via _JOB
        # being module-scope).  Skipped silently on POSIX.
        if sys.platform == 'win32' and _JOB.handle is None:
            _JOB.create()

        self._spawn_thread()

        # Best-effort atexit terminate so dev-mode (no Job Object) also
        # cleans up.  Windows already has KILL_ON_JOB_CLOSE; the duplicate
        # terminate() is harmless because Popen.poll() will already report
        # the process as gone.
        atexit.register(self._atexit_terminate)

        # Subscribe to governor mode transitions so the child's CPU cap
        # tightens/loosens in lockstep with the parent's resource policy.
        # Best-effort -- never aborts startup if the EventBus isn't up yet
        # (the governor itself starts later in some boot orders).
        self._subscribe_governor_modes()
        return self.info()

    def stop(self) -> None:
        """Graceful stop -- used on clean HARTOS shutdown."""
        self.stop_event.set()
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass

    def _atexit_terminate(self) -> None:
        """atexit hook -- fallback for POSIX or dev-mode runs where the
        Windows Job Object kill-on-close isn't in effect."""
        try:
            self.stop()
        except Exception:
            pass

    # -- Spawn & supervise loop ----------------------------------------
    def _build_cmd(self) -> list:
        """Launch the API server via an explicit import + uvicorn.run.

        We deliberately do NOT use ``-m hevolveai.server.api_server``.
        In the bundled Nunba build the hevolveai package is Cython-compiled
        and source-stripped (api_server.cp312-win_amd64.pyd, no .py), and
        ``python -m pkg.mod`` fails there with
            "No code object available for hevolveai.server.api_server"
        because runpy needs a code object that a .pyd extension module does
        not expose. Importing the compiled module works (that is the whole
        point of Cython); we then run uvicorn ourselves on HEVOLVEAI_PORT.

        This single command works in BOTH bundled (.pyd) and dev (.py via
        the PYTHONPATH the supervisor exports) -- one launch path, no fork.
        The module's FastAPI lifespan startup (background services, proof
        monitor) still fires when uvicorn starts the app, identical to the
        old ``if __name__ == '__main__'`` path minus the banner.

        ARMORED IMPORT (canonical, presence-gated, NO flag): the boot first runs
        ``_ARMOR_INSTALL_SNIPPET``, which installs the Hevolvearmor import hook
        IFF the bundle + key staged by the build — and exported by app.py via
        HEVOLVE_ARMORED_DIR / HEVOLVE_ARMOR_KEY_FILE — are present.  This is the
        SAME mechanism the in-process loader (security/native_hive_loader) uses,
        so there is ONE armor path, not two.  When the bundle is absent (dev),
        the snippet is a silent no-op and the import below loads the plain
        on-disk package exactly as before — zero behaviour change without a
        bundle, armored .enc (never a stale .pyd) with one.
        """
        boot = (
            _ARMOR_INSTALL_SNIPPET +
            "import uvicorn\n"
            "from hevolveai.server.api_server import app\n"
            "uvicorn.run(app, host='0.0.0.0',"
            " port=int(os.environ.get('HEVOLVEAI_PORT', '8000')),"
            " log_level='info')\n"
        )
        return [self.python_exe, '-c', boot]

    def _build_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env['HEVOLVEAI_API_URL'] = self.api_url
        env['HEVOLVEAI_PORT'] = str(self.port)
        # Tell HevolveAI it is the child of a HARTOS bundle so its own
        # startup can shorten any unnecessary banners / readiness pings.
        env['HEVOLVE_LAUNCHED_BY'] = 'hartos'
        # CPU-only convergence is the unified design; never let the
        # subprocess silently flip to CUDA just because the host has it.
        # (Qwen-VL still uses GPU INTERNALLY via qwen_llamacpp_wrapper's
        # auto-upgrade -- that path is independent of this hint.)
        env.setdefault('HEVOLVE_DEVICE', 'cpu')
        if self.pythonpath:
            existing = env.get('PYTHONPATH', '')
            env['PYTHONPATH'] = (
                self.pythonpath + os.pathsep + existing if existing
                else self.pythonpath
            )
        return env

    def _popen_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if sys.platform == 'win32':
            # CREATE_BREAKAWAY_FROM_JOB lets the child escape any
            # pre-existing parent Job Object (e.g. the resource-governor
            # cage) so we can attach it to OUR job with the
            # KILL_ON_JOB_CLOSE flag.  Combine with CREATE_NO_WINDOW so
            # no flicker console pops up in bundled GUI mode.
            kw['creationflags'] = (
                _CREATE_BREAKAWAY_FROM_JOB | _CREATE_NO_WINDOW
            )
        else:
            # POSIX: own session group so SIGINT to parent doesn't
            # propagate; atexit() handles graceful stop.
            kw['start_new_session'] = True
        return kw

    def _register_with_governor(self, pid: int) -> None:
        """Notify the resource governor about the new managed PID so it
        appears in monitor stats.  Best-effort -- never aborts spawn."""
        try:
            from core.resource_governor import get_governor
            governor = get_governor()
            register = getattr(governor, 'register_subprocess', None)
            if callable(register):
                register('hevolveai', pid)
                logger.info(
                    "hevolveai_supervisor: registered pid=%d with "
                    "ResourceGovernor", pid)
            else:
                logger.debug(
                    "hevolveai_supervisor: ResourceGovernor lacks "
                    "register_subprocess; skipping PID tracking")
        except Exception as e:
            logger.debug(
                "hevolveai_supervisor: governor registration "
                "skipped: %s", e)

    def _unregister_from_governor(self, pid: int) -> None:
        try:
            from core.resource_governor import get_governor
            governor = get_governor()
            unregister = getattr(governor, 'unregister_subprocess', None)
            if callable(unregister):
                unregister('hevolveai', pid)
        except Exception:
            pass

    # -- Mode-following: child CPU cap tracks governor mode -----------
    def _subscribe_governor_modes(self) -> None:
        """Subscribe to ``resource.mode_changed`` on the platform
        EventBus so the child's Job Object CPU cap follows the parent's
        governor mode (ACTIVE/IDLE/SLEEP).

        Best-effort -- the EventBus is registry-backed and may not yet
        exist during early bootstrap; the governor will re-emit on the
        next mode transition.
        """
        if sys.platform != 'win32' or _JOB.handle is None:
            return
        try:
            from core.platform.registry import get_registry
            registry = get_registry()
            if not registry.has('events'):
                logger.debug(
                    "hevolveai_supervisor: EventBus not yet registered; "
                    "child CPU cap will sync on next mode transition")
                return
            bus = registry.get('events')
            bus.on('resource.mode_changed', self._on_governor_mode_changed)
            logger.info(
                "hevolveai_supervisor: subscribed to "
                "resource.mode_changed")
        except Exception as e:
            logger.debug(
                "hevolveai_supervisor: governor mode subscription "
                "skipped: %s", e)

    def _on_governor_mode_changed(self, topic: str, data: Any) -> None:
        """Apply the governor's new mode to the child's Job Object.

        This is the symmetric tightening: governor caps the PARENT via
        its own job, here we cap the CHILD via ours.  Without this hook
        a SLEEP transition would leave the hevolveai subprocess running
        at full speed even though the parent throttled to near-zero.
        """
        try:
            new_mode = (data or {}).get('new_mode', '').lower()
            fraction = _MODE_CPU_FRACTION.get(new_mode)
            if fraction is None:
                return
            ok = _JOB.set_cpu_rate(fraction)
            if ok:
                logger.info(
                    "hevolveai_supervisor: child CPU cap -> %.1f%% "
                    "(governor mode=%s)", fraction * 100, new_mode)
            else:
                logger.debug(
                    "hevolveai_supervisor: failed to apply CPU cap "
                    "for mode=%s", new_mode)
        except Exception as e:
            logger.debug(
                "hevolveai_supervisor: mode-changed handler error: %s", e)

    def _build_popen(self):
        cmd = self._build_cmd()
        kw = self._popen_kwargs()
        kw['env'] = self._build_env()
        return cmd, kw

    def _on_started(self, proc) -> None:
        self._current_pid = proc.pid
        # Bind to the Job Object FIRST -- even a few ms of unbound runtime is
        # enough for a Nunba crash to orphan the child.  proc._handle is the
        # canonical Windows process HANDLE AssignProcessToJobObject needs.
        if sys.platform == 'win32' and _JOB.handle is not None:
            _JOB.assign(getattr(proc, '_handle', 0))
        self._register_with_governor(self._current_pid)

    def _format_stdout_line(self, line: str) -> None:
        logger.info('hevolveai: %s', line)

    def _on_child_exit(self, rc, uptime) -> bool:
        """#59 circuit breakers. The pre-spawn _hevolveai_available check is the
        primary guard; these catch a child that imports fine but crashes on
        every start. FAST: exits faster than _FAST_FAIL_S = instant misconfig.
        SLOW: a child that loads its model (eating VRAM), runs 10-30s, then
        crashes every time churns VRAM/CPU without tripping the fast breaker
        (live 2026-06-01: 14-22s uptime, 8x, free VRAM 7.8->3.0GB starved the
        flywheel). Disable after N consecutive unhealthy exits in a row. (The
        backoff reset after a healthy run is the base's reset_backoff_after=60.)
        """
        if self._current_pid is not None:
            self._unregister_from_governor(self._current_pid)
        _FAST_FAIL_S, _FAST_FAIL_LIMIT = 5.0, 5
        _UNHEALTHY_S, _UNHEALTHY_LIMIT = 60.0, 6
        if uptime < _FAST_FAIL_S:
            self._consecutive_fast_fails += 1
        else:
            self._consecutive_fast_fails = 0
        if uptime < _UNHEALTHY_S:
            self._consecutive_unhealthy += 1
        else:
            self._consecutive_unhealthy = 0
        if (self._consecutive_fast_fails >= _FAST_FAIL_LIMIT
                or self._consecutive_unhealthy >= _UNHEALTHY_LIMIT):
            _n = max(self._consecutive_fast_fails, self._consecutive_unhealthy)
            _window = (_FAST_FAIL_S
                       if self._consecutive_fast_fails >= _FAST_FAIL_LIMIT
                       else _UNHEALTHY_S)
            self.last_error = (
                f'hevolveai exited rc={rc} unhealthily {_n}x in a row '
                f'(uptime <{_window:.0f}s each) -- DISABLING supervisor '
                f'(broken install / crashing child / missing weights). '
                f'Restart Nunba or fix the install to re-enable.')
            logger.error("hevolveai_supervisor: %s", self.last_error)
            return True
        return False

    def info(self) -> Dict[str, Any]:
        running = (
            self.proc is not None
            and self.proc.poll() is None
            and self.thread is not None
            and self.thread.is_alive()
        )
        return {
            'should_run': supervisor_should_run(),
            'python_exe': self.python_exe,
            'pythonpath': self.pythonpath,
            'port': self.port,
            'api_url': self.api_url,
            'running': running,
            'pid': self.proc.pid if (self.proc and running) else None,
            'restart_count': self.restart_count,
            'last_started': self.last_started,
            'last_error': self.last_error,
            'job_object_bound': bool(_JOB.handle),
        }


_INSTANCE: Optional[_Supervisor] = None
_INSTANCE_LOCK = threading.Lock()


def start_supervisor() -> Dict[str, Any]:
    """Idempotent entrypoint -- called from hartos_bootstrap.

    No-op when ``HEVOLVE_SKIP_HEVOLVEAI_SPAWN=1`` or when
    ``HEVOLVEAI_API_URL`` points to a remote host.
    """
    global _INSTANCE
    if not supervisor_should_run():
        # Distinguish the skip reasons so operators know whether it was
        # opt-out, a remote target, or hevolveai simply not installed.
        if os.environ.get('HEVOLVE_SKIP_HEVOLVEAI_SPAWN') == '1':
            _reason = 'HEVOLVE_SKIP_HEVOLVEAI_SPAWN=1 -- opted out'
        elif not _hevolveai_available():
            _reason = ('hevolveai not installed/importable and no dev '
                       'sibling repo found -- supervisor disabled (set '
                       'HEVOLVEAI_HOME or pip install hevolveai to enable)')
        else:
            _reason = 'remote HEVOLVEAI_API_URL -- supervisor not started'
        return {'should_run': False, 'reason': _reason}
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = _Supervisor()
        return _INSTANCE.start()


def stop_supervisor() -> None:
    """Process-shutdown hook.  Safe to call when supervisor never ran."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            _INSTANCE.stop()
            _INSTANCE = None


def supervisor_info() -> Dict[str, Any]:
    """Read-only state -- useful for /health, debug pages, tests."""
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            return {
                'should_run': supervisor_should_run(),
                'running': False,
            }
        return _INSTANCE.info()

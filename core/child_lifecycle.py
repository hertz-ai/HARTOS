"""core.child_lifecycle -- children that die with this process.

WHY THIS EXISTS
---------------
Nunba quits from the tray with ``os._exit(0)``: no atexit, no cleanup.  On
Windows a child outlives its parent, so every llama-server and GPU worker
Nunba had started kept running -- llama-server at 2+ GB and full CPU -- and
the next launch found the old server on :8080 and reused it.  Measured on a
12 GB CPU-only laptop, 2026-10-01: 1.4 GB free, chat at 60-200 s per reply,
the whisper worker never reaching READY.

A Windows Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` fixes that
exactly: when this process exits -- tray quit, Task Manager, crash -- the
kernel closes the job handle and kills every process assigned to it.

NESTING
-------
``bind_to_parent`` assigns the child as it was spawned; it does NOT ask for
``CREATE_BREAKAWAY_FROM_JOB``.  If this process sits in the resource
governor's job (core/resource_governor.py), the child is in it too, and the
governor's memory ceiling keeps counting it -- on purpose: llama-server is
the biggest consumer it governs.  Windows 8+ nests the lifecycle job under
that one.  A process that needs a job of its own (hevolveai_supervisor: it
also caps the child's CPU rate job-wide) creates its own ``JobHandle`` and
spawns with ``CREATE_BREAKAWAY_FROM_JOB``.

Windows only.  Elsewhere ``bind_to_parent`` returns False and does nothing.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800
JOBOBJECT_EXTENDED_LIMIT_INFO_CLS = 9    # JobObjectExtendedLimitInformation
JOBOBJECT_CPU_RATE_CONTROL_INFO_CLS = 15  # JobObjectCpuRateControlInformation
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class JobHandle:
    """Owns a Windows Job Object handle with KILL_ON_JOB_CLOSE set.

    Keep the instance alive for the lifetime of the parent process --
    releasing the last reference closes the handle, which kills every
    assigned process.  That is the intended lifecycle binding.
    """

    def __init__(self, owner: str = 'child_lifecycle') -> None:
        self.owner = owner
        self.handle: Optional[int] = None
        self._kernel32 = None

    def create(self) -> Optional[int]:
        """Create the Job Object and set the kill-on-close + breakaway-ok
        limit flags.  Returns the handle (a Windows HANDLE as int) or
        None if anything fails -- the caller then spawns without binding.
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
            # silently breaking the kill-on-close binding.
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
                    "%s: CreateJobObjectW failed; children will NOT be "
                    "killed on parent exit", self.owner)
                self.handle = None
                return None
            if not self._set_limits():
                # Leave the handle open; AssignProcessToJobObject still
                # works, just without kill-on-close.
                logger.warning(
                    "%s: SetInformationJobObject failed; Job Object created "
                    "without KILL_ON_JOB_CLOSE", self.owner)
            return self.handle
        except Exception as e:  # pragma: no cover -- defensive
            logger.warning("%s: Job Object setup failed: %s", self.owner, e)
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
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        ok = self._kernel32.SetInformationJobObject(
            self.handle,
            JOBOBJECT_EXTENDED_LIMIT_INFO_CLS,
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
                    "%s: AssignProcessToJobObject failed (err=%d); child "
                    "WILL outlive parent", self.owner, err)
            return bool(ok)
        except Exception as e:  # pragma: no cover
            logger.warning(
                "%s: AssignProcessToJobObject exception: %s", self.owner, e)
            return False

    def set_cpu_rate(self, cpu_fraction: float) -> bool:
        """Live-update the job's CPU rate cap (job-wide).

        ``cpu_fraction`` is a fraction of ONE logical core (0.05 = 5%).
        Windows clamps the rate field at 1 (0.01%); we floor at 100 (1%)
        so a near-zero cap doesn't starve a child out of even emitting its
        shutdown logs.  Never call this on the shared lifecycle job: the
        cap would apply to every child bound to it.
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
                JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            )
            info.Value = max(100, int(cpu_fraction * 10000))
            ok = self._kernel32.SetInformationJobObject(
                self.handle,
                JOBOBJECT_CPU_RATE_CONTROL_INFO_CLS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            return bool(ok)
        except Exception as e:  # pragma: no cover
            logger.debug("%s: SetInformationJobObject CpuRate failed: %s",
                         self.owner, e)
            return False


_LIFECYCLE_JOB: Optional[JobHandle] = None
_LIFECYCLE_LOCK = threading.Lock()


def bind_to_parent(proc) -> bool:
    """Make ``proc`` (a subprocess.Popen) die when this process exits.

    Call right after Popen returns.  Returns True when the child is bound;
    False off Windows or if the Job Object could not be set up (logged).
    Never raises: a failed binding must not fail the spawn.
    """
    if sys.platform != 'win32':
        return False
    handle = getattr(proc, '_handle', None)
    if not handle:
        return False
    global _LIFECYCLE_JOB
    with _LIFECYCLE_LOCK:
        if _LIFECYCLE_JOB is None:
            job = JobHandle(owner='child_lifecycle')
            if job.create() is None:
                return False
            _LIFECYCLE_JOB = job
        return _LIFECYCLE_JOB.assign(handle)

"""NFT — detect_gpu() must not hang when child stalls past timeout.

Root-cause class: Python's subprocess.run(..., capture_output=True,
text=True, timeout=N) relies on _readerthread daemons to drain
stdout/stderr.  On Windows, if the child is killed mid-init and the
pipes stay open, those readers block forever in fh.read() → the
whole run() call wedges.  Observed in tests/journey/ setup: 5+ min
hang on nvidia-smi, 27 min hang on wmic (2026-04-15).

The fix (core.subprocess_safe.run_bounded) explicitly closes the
parent-side stdout/stderr after killing the child, which releases
the OS handles the reader threads are blocked on.  This test
simulates the exact wedge and asserts detect_gpu() returns within
7 seconds (5s nvidia-smi timeout + 5s rocm-smi timeout gives a
10s ceiling on real hardware; we allow 7s per single wedge).

Regression guard for: fix(vram): Popen reader-thread orphan on
                     subprocess kill
"""
from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import patch


class _WedgedPopen:
    """Fake Popen whose communicate() always times out.

    Simulates nvidia-smi mid-driver-probe where the child is spawned
    but its stdout/stderr pipes are open with no bytes flowing — the
    real failure mode that wedges _readerthread on Windows.
    """

    def __init__(self, cmd, **kwargs):
        self.args = cmd
        self.returncode = None
        self._killed = False
        self._waited = False
        # Fake pipe handles that track close().  .closed is False
        # until close() is called — exactly like a real pipe handle.
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()

    def communicate(self, timeout=None):
        # Simulate the wedge: wait up to timeout, then raise.  We
        # don't actually sleep the full `timeout` — the contract is
        # "raise TimeoutExpired if exceeded", which the real Popen
        # does internally via a reader-thread join with timeout.
        raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout)

    def kill(self):
        self._killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self._waited = True
        return self.returncode or 0

    def poll(self):
        return self.returncode


def _tools_present(name, *a, **kw):
    """Pretend nvidia-smi / rocm-smi are on PATH.

    _probe_gpu (2026-08-12) stats PATH first and caches "no vendor tools" as
    a permanent negative, skipping the probes outright, so on a box without
    the tools no Popen is ever spawned and the wedge these tests simulate can
    never occur. The wedge is only reachable when the tool EXISTS and hangs,
    which is the real-hardware case the fix was written for."""
    return "/usr/bin/" + name


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def read(self):
        return ""


class DetectGPUBoundedByWatchdogTests(unittest.TestCase):
    """Wedged nvidia-smi / rocm-smi must not hang detect_gpu()."""

    def _fresh_manager(self):
        from integrations.service_tools.vram_manager import VRAMManager
        return VRAMManager()

    def test_detect_gpu_returns_within_watchdog_budget(self):
        """detect_gpu() must return even when both smi tools wedge.

        The envelope shape may reflect a PyTorch fallback on dev
        boxes with a real GPU; this test deliberately only asserts
        on timing (the regression axis), not on cuda_available,
        because torch may legitimately report a GPU here.
        """
        mgr = self._fresh_manager()
        with patch("shutil.which", _tools_present),              patch("subprocess.Popen", _WedgedPopen):
            t0 = time.monotonic()
            info = mgr.detect_gpu()
            elapsed = time.monotonic() - t0

        # Two smi calls × 5s = 10s theoretical; real Popen wedge test
        # would also include wait_after_kill (2s each).  Our fake
        # .communicate() raises immediately, so elapsed should be
        # sub-second.  Ceiling 7s catches any regression where the
        # reader-thread orphan resurrects.
        self.assertLess(
            elapsed, 7.0,
            f"detect_gpu() must return in <7s even when child wedges; "
            f"took {elapsed:.2f}s — reader-thread orphan regression?",
        )
        # Envelope shape must always be present, regardless of which
        # detection path wins.
        for k in ("name", "total_gb", "free_gb", "cuda_available"):
            self.assertIn(k, info)

    # test_detect_gpu_closes_pipes_on_timeout used to live here and asserted the
    # OPPOSITE of the current contract: that run_bounded close()s the child's
    # stdout/stderr after a timeout. Closing them is exactly what deadlocked
    # (D36, measured 2026-09-09: fh.close() blocks on the file lock a parked
    # _readerthread holds inside fh.read()), so _safe_kill_and_close now leaves
    # the pipes to their reader threads by design. The deliberately arranged
    # test for that contract is tests/unit/test_subprocess_safe.py
    # (TestKillReachesDescendants); this file keeps only the half that is still
    # true, that detect_gpu() RETURNS within the watchdog budget when a probe
    # wedges.


class RunBoundedUnitTests(unittest.TestCase):
    """Direct tests on core.subprocess_safe.run_bounded()."""

    def test_timeout_returns_timed_out_result(self):
        from core.subprocess_safe import run_bounded
        with patch("subprocess.Popen", _WedgedPopen):
            result = run_bounded(["fake-tool"], timeout=0.1)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, -1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_success_returns_completed_result(self):
        from core.subprocess_safe import run_bounded

        class _OKPopen(_WedgedPopen):
            def communicate(self, timeout=None):
                self.returncode = 0
                return ("ok\n", "")

        with patch("subprocess.Popen", _OKPopen):
            result = run_bounded(["fake-tool"], timeout=1)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")

    def test_file_not_found_propagates(self):
        """FileNotFoundError must propagate so caller can `except` it.

        The subprocess.run callers we replaced all do `except
        FileNotFoundError: pass` to handle "tool not on PATH" — the
        helper must preserve that semantics.
        """
        from core.subprocess_safe import run_bounded

        def _missing(*a, **k):
            raise FileNotFoundError(2, "No such file", a[0][0] if a else "")

        with patch("subprocess.Popen", _missing):
            with self.assertRaises(FileNotFoundError):
                run_bounded(["definitely-not-a-tool"], timeout=1)


if __name__ == "__main__":
    unittest.main()

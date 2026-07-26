"""
Behavioural tests for the boot-time MEMORY-HEALTH snapshot probe
(nixos/modules/hart-memory-health.sh).

These run the REAL script - the exact bytes hart-memory.nix ships via
runCommand(readFile ...) - against a FIXTURE /proc/meminfo (HART_MEMINFO_FILE), a
fake zram device node (HART_ZRAM_GLOB), and STUB `zramctl` / `systemctl` on PATH.
Each test pins the memory state, so every assertion checks the OBSERVABLE
key=value snapshot the probe writes, NOT the source text (Gate-5 behavioural, the
/proc + tool boundary mocked at the process boundary).

Coverage (the #157 memory snapshot + the never-brick degrade contract):
  * meminfo present     -> ok=1 + mem/swap totals parsed off /proc/meminfo
  * zram node + zramctl -> zram_present=1 + zram_algorithm=<algo>
  * no zram node        -> zram_present=0 + empty algorithm (honest)
  * systemd-oomd active -> oomd_active=1 ; inactive -> oomd_active=0
  * meminfo unreadable  -> ok=0 + reason, exit 0 (degrade, no snapshot)
  * the probe CHANGES no kernel state + ALWAYS exits 0

[Linux/POSIX] SKIPS on a host without `sh` (the bare Windows dev box); runs for
real in CI and on any POSIX box.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

_SH = shutil.which("sh") or shutil.which("bash")

_SCRIPT = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "nixos", "modules", "hart-memory-health.sh"))

_MEMINFO = (
    "MemTotal:       16307200 kB\n"
    "MemFree:         2048000 kB\n"
    "MemAvailable:   10231044 kB\n"
    "SwapTotal:       8388604 kB\n"
    "SwapFree:        8388604 kB\n"
)


def _write_exec(path, body):
    with open(path, "w", newline="\n") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_SH, "no POSIX sh on PATH (Windows dev box) - runs in CI")
@unittest.skipUnless(os.path.exists(_SCRIPT), "hart-memory-health.sh not found")
class HartMemoryHealthTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hart-mem-health-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.status = os.path.join(self.tmp, "memory-health")
        self.meminfo = os.path.join(self.tmp, "meminfo")
        with open(self.meminfo, "w", newline="\n") as f:
            f.write(_MEMINFO)
        # zram glob: a RELATIVE prefix resolved against cwd=self.tmp in _run (an
        # absolute Windows C:\ path does not glob under Git Bash; relative is
        # portable on every sh). Create a node to mark "present", omit for "absent".
        self.zram_glob = "zram"
        self.zram_node = os.path.join(self.tmp, "zram0")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_zram_node(self):
        with open(self.zram_node, "w") as f:
            f.write("")

    def _stub_zramctl(self, algorithm):
        body = (
            "#!/bin/sh\n"
            'printf "%s\\n"\n' % algorithm
        )
        _write_exec(os.path.join(self.bindir, "zramctl"), body)

    def _stub_systemctl(self, active):
        rc = "0" if active else "3"
        body = "#!/bin/sh\nexit %s\n" % rc
        _write_exec(os.path.join(self.bindir, "systemctl"), body)

    def _run(self, meminfo=None):
        env = dict(os.environ)
        env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        env["HART_MEMORY_HEALTH_FILE"] = self.status
        env["HART_MEMINFO_FILE"] = meminfo if meminfo is not None else self.meminfo
        # Relative glob resolved against cwd=self.tmp (portable on every sh).
        env["HART_ZRAM_GLOB"] = self.zram_glob
        proc = subprocess.run(
            [_SH, _SCRIPT], env=env, cwd=self.tmp,
            capture_output=True, text=True, timeout=60)
        out = ""
        if os.path.exists(self.status):
            with open(self.status) as f:
                out = f.read()
        return proc.returncode, out, proc.stderr

    # ── tests ───────────────────────────────────────────────────────────────
    def test_full_readout(self):
        self._make_zram_node()
        self._stub_zramctl("zstd")
        self._stub_systemctl(active=True)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0, "probe must always exit 0")
        self.assertIn("ok=1", out)
        self.assertIn("mem_total_kb=16307200", out)
        self.assertIn("mem_available_kb=10231044", out)
        self.assertIn("swap_total_kb=8388604", out)
        self.assertIn("zram_present=1", out)
        self.assertIn("zram_algorithm=zstd", out)
        self.assertIn("oomd_active=1", out)

    def test_no_zram_node_is_absent(self):
        # No zram node created -> present=0, empty algorithm (zramctl not consulted).
        self._stub_systemctl(active=True)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("zram_present=0", out)
        self.assertIn("zram_algorithm=\n", out + "\n")

    def test_oomd_inactive(self):
        self._make_zram_node()
        self._stub_zramctl("lz4")
        self._stub_systemctl(active=False)
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("oomd_active=0", out)

    def test_meminfo_unreadable_degrades_ok0(self):
        """A missing/unreadable /proc/meminfo -> honest ok=0 + reason, exit 0."""
        missing = os.path.join(self.tmp, "does-not-exist")
        rc, out, _ = self._run(meminfo=missing)
        self.assertEqual(rc, 0)
        self.assertIn("ok=0", out)
        self.assertIn("meminfo-unreadable", out)


if __name__ == "__main__":
    unittest.main()

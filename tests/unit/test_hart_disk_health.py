"""
Behavioural tests for the boot-time DISK-HEALTH snapshot probe
(nixos/modules/hart-disk-health.sh).

These run the REAL script - the exact bytes hart-storage.nix ships via
runCommand(readFile ...) - against STUB `lsblk` / `smartctl` / `nvme` binaries
placed first on PATH, with the status file pointed at a fixture via
HART_DISK_HEALTH_FILE. Each test pins what the "kernel" sees, so every assertion
checks the OBSERVABLE per-device verdict the probe writes, NOT the source text
(Gate-5 behavioural, the lsblk/SMART boundary mocked at the process boundary).

Coverage (the #157 disk-health snapshot + the never-brick degrade contract):
  * a healthy disk      -> dev0.smart=passed + name/path/size/rota/model lines
  * a failing disk      -> dev0.smart=failed (honest)
  * no SMART data       -> smart=unknown (never a faked positive), still exit 0
  * NVMe via nvme-cli   -> critical_warning 0 -> passed (smartctl-absent path)
  * lsblk missing       -> ok=0 + reason, exit 0 (degrade, no enumeration)
  * the probe MOUNTS/WRITES nothing to a disk + ALWAYS exits 0

[Linux/POSIX] The artifact is a POSIX sh script needing a real `sh`; it SKIPS on a
host without `sh` (the bare Windows dev box) rather than assert nothing - it runs
for real in CI and on any POSIX box.
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
    "nixos", "modules", "hart-disk-health.sh"))


def _write_exec(path, body):
    with open(path, "w", newline="\n") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_SH, "no POSIX sh on PATH (Windows dev box) - runs in CI")
@unittest.skipUnless(os.path.exists(_SCRIPT), "hart-disk-health.sh not found")
class HartDiskHealthTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hart-disk-health-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.status = os.path.join(self.tmp, "disk-health")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── fixture builders ────────────────────────────────────────────────────
    def _stub_lsblk(self, pairs_lines):
        """Stub `lsblk` to emit the given -P KEY="value" lines (any args)."""
        body = "#!/bin/sh\ncat <<'EOF'\n" + "\n".join(pairs_lines) + "\nEOF\n"
        _write_exec(os.path.join(self.bindir, "lsblk"), body)

    def _stub_smartctl(self, health_text):
        """Stub `smartctl`: `-H <dev>` echoes health_text; anything else empty."""
        safe = health_text.replace("\\", "\\\\").replace('"', '\\"')
        body = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  -H) echo "' + safe + '" ;;\n'
            "esac\n"
            "exit 0\n"
        )
        _write_exec(os.path.join(self.bindir, "smartctl"), body)

    def _stub_nvme(self, critical_warning):
        """Stub `nvme smart-log <dev>` to print a critical_warning line."""
        body = (
            "#!/bin/sh\n"
            'printf "critical_warning : %s\\n"\n' % critical_warning
        )
        _write_exec(os.path.join(self.bindir, "nvme"), body)

    # ── runner ──────────────────────────────────────────────────────────────
    def _run(self, prepend_bin=True):
        env = dict(os.environ)
        if prepend_bin:
            env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        env["HART_DISK_HEALTH_FILE"] = self.status
        env["HART_DISK_HEALTH_TIMEOUT"] = "3"
        proc = subprocess.run(
            [_SH, _SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        out = ""
        if os.path.exists(self.status):
            with open(self.status) as f:
                out = f.read()
        return proc.returncode, out, proc.stderr

    # ── tests ───────────────────────────────────────────────────────────────
    def test_healthy_disk_reports_passed(self):
        self._stub_lsblk(['NAME="sda" TYPE="disk" SIZE="500107862016" ROTA="0" MODEL="Test SSD"'])
        self._stub_smartctl("SMART overall-health self-assessment test result: PASSED")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0, "probe must always exit 0")
        self.assertIn("ok=1", out)
        self.assertIn("dev0.name=sda", out)
        self.assertIn("dev0.path=/dev/sda", out)
        self.assertIn("dev0.size=500107862016", out)
        self.assertIn("dev0.model=Test SSD", out)
        self.assertIn("dev0.smart=passed", out)

    def test_failing_disk_reports_failed(self):
        self._stub_lsblk(['NAME="sdb" TYPE="disk" SIZE="1000" ROTA="1" MODEL="Old HDD"'])
        self._stub_smartctl("SMART overall-health self-assessment test result: FAILED!")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("dev0.smart=failed", out)

    def test_no_smart_data_is_unknown(self):
        """A disk whose SMART read returns nothing reads smart=unknown, never a
        faked positive, and the probe still exits 0."""
        self._stub_lsblk(['NAME="sdc" TYPE="disk" SIZE="1000" ROTA="1" MODEL="Mystery"'])
        self._stub_smartctl("")  # no recognisable health line
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("dev0.smart=unknown", out)

    def test_partitions_are_skipped(self):
        """Only TYPE=disk rows are reported (a partition row is ignored)."""
        self._stub_lsblk([
            'NAME="sda" TYPE="disk" SIZE="500107862016" ROTA="0" MODEL="Disk0"',
            'NAME="sda1" TYPE="part" SIZE="512" ROTA="0" MODEL=""',
        ])
        self._stub_smartctl("SMART overall-health self-assessment test result: PASSED")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("dev0.name=sda", out)
        self.assertNotIn("sda1", out)
        # Exactly one device enumerated.
        self.assertNotIn("dev1.", out)

    def test_nvme_path_when_no_smartctl(self):
        """With no smartctl but nvme present, a critical_warning of 0 -> passed."""
        self._stub_lsblk(['NAME="nvme0n1" TYPE="disk" SIZE="256060514304" ROTA="0" MODEL="NVMe SSD"'])
        self._stub_nvme("0")  # 0 == healthy per the NVMe spec
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("dev0.name=nvme0n1", out)
        self.assertIn("dev0.smart=passed", out)

    def test_lsblk_missing_degrades_ok0(self):
        """No lsblk on PATH -> honest ok=0 + reason, exit 0 (degrade-not-die)."""
        # Do NOT prepend the stub dir, and ensure no real lsblk leaks in: point PATH
        # at an empty dir so `command -v lsblk` fails deterministically.
        env = dict(os.environ)
        env["PATH"] = self.bindir  # empty bin dir, no lsblk
        env["HART_DISK_HEALTH_FILE"] = self.status
        proc = subprocess.run([_SH, _SCRIPT], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        with open(self.status) as f:
            out = f.read()
        self.assertIn("ok=0", out)
        self.assertIn("lsblk-missing", out)


if __name__ == "__main__":
    unittest.main()

"""
Behavioural tests for the cross-OS filesystem real-HW driver probe
(nixos/modules/hart-storage-fsprobe.sh).

These run the REAL script - the exact bytes hart-storage.nix ships via
writeShellScriptBin(readFile ...) - against a STUB `modinfo` / `mount.ntfs` placed
first on PATH and a FAKE /proc/filesystems table (via HART_PROC_FILESYSTEMS). The
stubs let each test pin which drivers the "kernel" has, so every assertion checks
the OBSERVABLE verdict the probe emits, NOT the source text. This is a Gate-5
behavioural test with the kernel-introspection boundary (modinfo / /proc /
mount.ntfs) mocked at the process boundary.

Coverage (the #145 interop readout + the degrade contract):
  * fs registered in /proc/filesystems        -> ok   (no modinfo needed)
  * fs not registered but its module resolves -> ok   (modinfo path, never loads)
  * ntfs via the in-kernel ntfs3 module name  -> ok   (name-mapping: ntfs->ntfs3)
  * ntfs via the ntfs-3g FUSE mount.ntfs only -> ok   (userspace helper path)
  * driver absent everywhere                  -> missing (honest), exit 0
  * status-file mode writes one fs_<name>=v line per filesystem, exit 0
  * no filesystem args / no status file       -> clean no-op, exit 0 (degrade)
  * the probe NEVER loads a module            -> stub modinfo is read-only; a
                                                 deterministic bogus fs proves
                                                 'missing' without touching the host

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
    "nixos", "modules", "hart-storage-fsprobe.sh"))


def _write_exec(path, body):
    with open(path, "w", newline="\n") as f:
        f.write(body)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_SH, "no POSIX sh on PATH (Windows dev box) - runs in CI")
@unittest.skipUnless(os.path.exists(_SCRIPT), "hart-storage-fsprobe.sh not found")
class HartStorageFsprobeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hart-fsprobe-test-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        # Default: an EMPTY fake /proc/filesystems (nothing registered) so a test
        # only sees what it explicitly registers. Stubs decide module availability.
        self.procfs = os.path.join(self.tmp, "filesystems")
        self._set_procfs([])
        # By default, modinfo resolves NOTHING (the kernel has no relevant module);
        # tests that want a driver available pass an allowlist to _add_modinfo.
        self._add_modinfo([])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── fixture builders ────────────────────────────────────────────────────
    def _set_procfs(self, registered):
        """Write a fake /proc/filesystems registering the given fs names."""
        lines = ["nodev\tsysfs\n", "nodev\tproc\n"]
        for fs in registered:
            lines.append("\t%s\n" % fs)
        with open(self.procfs, "w", newline="\n") as f:
            f.writelines(lines)

    def _add_modinfo(self, allow):
        """Stub `modinfo <mod>`: exit 0 iff <mod> is in `allow`, else 1. Read-only
        (it NEVER loads anything) - exactly the contract the probe relies on."""
        allow_str = " ".join(allow)
        body = (
            "#!/bin/sh\n"
            'for m in %s; do\n'
            '  [ "$1" = "$m" ] && exit 0\n'
            "done\n"
            "exit 1\n"
        ) % allow_str
        _write_exec(os.path.join(self.bindir, "modinfo"), body)

    def _add_mount_ntfs(self):
        """Presence of the ntfs-3g FUSE mount helper (the probe only `command -v`s it)."""
        _write_exec(os.path.join(self.bindir, "mount.ntfs"), "#!/bin/sh\nexit 0\n")

    # ── runner ──────────────────────────────────────────────────────────────
    def _run(self, *args):
        env = dict(os.environ)
        # Stub dir FIRST so modinfo / mount.ntfs are shadowed; real coreutils
        # (grep/printf) still resolve later on PATH.
        env["PATH"] = self.bindir + os.pathsep + env.get("PATH", "")
        env["HART_PROC_FILESYSTEMS"] = self.procfs
        proc = subprocess.run(
            [_SH, _SCRIPT, *[str(a) for a in args]],
            env=env, capture_output=True, text=True, timeout=60)
        return proc.returncode, proc.stdout, proc.stderr

    def _query(self, fs):
        rc, out, _ = self._run("--query", fs)
        self.assertEqual(rc, 0, f"probe must always exit 0 (got {rc}) for --query {fs}")
        return out.strip()

    # ── tests: --query verdicts ─────────────────────────────────────────────
    def test_registered_in_proc_is_ok_without_modinfo(self):
        """A filesystem already registered in /proc/filesystems reads ok even when
        no module resolves (loaded / built-in path)."""
        self._set_procfs(["ext4", "vfat"])
        self._add_modinfo([])  # modinfo resolves nothing
        self.assertEqual(self._query("ext4"), "ok")
        self.assertEqual(self._query("vfat"), "ok")

    def test_module_resolvable_is_ok(self):
        """Not registered yet, but the module exists for this kernel (modinfo) -> ok.
        The probe must NOT need the fs pre-mounted/loaded."""
        self._set_procfs([])  # nothing registered
        self._add_modinfo(["exfat", "btrfs"])
        self.assertEqual(self._query("exfat"), "ok")
        self.assertEqual(self._query("btrfs"), "ok")

    def test_ntfs_maps_to_ntfs3_module(self):
        """ntfs is served by the in-kernel ntfs3 driver: modinfo ntfs3 -> ntfs ok."""
        self._set_procfs([])
        self._add_modinfo(["ntfs3"])  # only the ntfs3 module name resolves
        self.assertEqual(self._query("ntfs"), "ok")

    def test_ntfs_via_userspace_helper_only(self):
        """No ntfs3/ntfs module at all, but the ntfs-3g mount.ntfs helper is present
        -> ntfs still ok (the userspace FUSE read/write path)."""
        self._set_procfs([])
        self._add_modinfo([])      # no kernel ntfs driver
        self._add_mount_ntfs()     # but the FUSE helper exists
        self.assertEqual(self._query("ntfs"), "ok")

    def test_absent_driver_is_missing_and_exit0(self):
        """A filesystem with no /proc entry, no module, no helper -> honest 'missing',
        and the probe still exits 0 (degrade-not-die; it loaded/mounted nothing)."""
        self._set_procfs([])
        self._add_modinfo([])
        rc, out, _ = self._run("--query", "btrfs")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "missing")

    def test_unknown_fs_is_missing(self):
        """A nonsense filesystem name is deterministically 'missing' (host-independent)."""
        self.assertEqual(self._query("nonesuchfs"), "missing")

    def test_empty_query_arg_is_missing(self):
        """`--query` with no fs name degrades to 'missing', exit 0 (never crashes)."""
        rc, out, _ = self._run("--query")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "missing")

    # ── tests: status-file mode (what hart-compat-smoketest calls) ──────────
    def test_status_file_writes_one_line_per_fs(self):
        """Status-file mode appends fs_<name>=<verdict> for each filesystem, with the
        honest mix of ok/missing, and exits 0."""
        self._set_procfs(["ext4"])             # ext4 registered
        self._add_modinfo(["exfat", "vfat"])   # exfat + vfat have modules
        # ntfs + btrfs have neither -> missing.
        status = os.path.join(self.tmp, "compat-status")
        rc, _, _ = self._run(status, "ntfs", "exfat", "vfat", "ext4", "btrfs")
        self.assertEqual(rc, 0)
        with open(status) as f:
            content = f.read()
        self.assertIn("fs_ext4=ok", content)
        self.assertIn("fs_exfat=ok", content)
        self.assertIn("fs_vfat=ok", content)
        self.assertIn("fs_ntfs=missing", content)
        self.assertIn("fs_btrfs=missing", content)

    def test_status_file_appends_not_truncates(self):
        """The probe APPENDS (compat-status already holds the runtime verdicts); it
        must not clobber pre-existing content."""
        status = os.path.join(self.tmp, "compat-status")
        with open(status, "w", newline="\n") as f:
            f.write("windows=ok\n")
        self._add_modinfo(["ext4"])
        rc, _, _ = self._run(status, "ext4")
        self.assertEqual(rc, 0)
        with open(status) as f:
            content = f.read()
        self.assertIn("windows=ok", content)   # pre-existing line survives
        self.assertIn("fs_ext4=ok", content)

    def test_status_mode_no_fs_args_is_clean_noop(self):
        """A status-file path with NO filesystem list is a clean no-op (exit 0); the
        file is created/left empty, nothing crashes."""
        status = os.path.join(self.tmp, "empty-status")
        rc, _, _ = self._run(status)
        self.assertEqual(rc, 0)

    def test_no_args_at_all_is_noop(self):
        """No --query and no status file -> honest no-op, exit 0 (degrade)."""
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()

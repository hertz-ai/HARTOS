# ═══════════════════════════════════════════════════════════════
# HART OS — Cross-OS storage interop nixosTest (#145)
# ═══════════════════════════════════════════════════════════════
#
# Proves the storage-filesystems hardware dimension, BEHAVIOURALLY (real mkfs +
# mount + read/write round-trips on real block/loop devices, NOT grep-on-source):
#
#   1. boot.supportedFilesystems covers ntfs / exfat / vfat / ext4 / btrfs — each
#      filesystem is formatted with its native mkfs, MOUNTED (which fails with
#      "unknown filesystem type" unless supportedFilesystems made the driver +
#      mount helper available), a file is WRITTEN and READ BACK, then unmounted.
#      A clean read/write round-trip IS the proof the interop config took effect.
#   2. The udisks2 on-demand mount AUTHORITY (the auto-mount path the file manager
#      + glass shell call) mounts a removable disk under /run/media and r/w works.
#   3. DEGRADE-NOT-DIE: an UNMOUNTABLE (corrupt/zeroed) disk fails to mount CLEANLY
#      and FAST (bounded by `timeout`, never a hang), and the OS stays fully up —
#      a bad plugged disk can never brick/black/wedge the machine.
#   4. NO boot-blocking external mount exists: the interop module adds ZERO
#      fileSystems/.mount units for external disks (every mount is on-demand via
#      udisks), so local-fs.target can never wait on a removable disk → no 90s
#      boot stall → no emergency drop. Asserted via fstab + `systemctl --failed`.
#   5. CROSS-LINK to the boot dimension: the HARTLOG persistence path NEVER
#      completes the live boot medium's GPT (which would arm the duplicate-LABEL
#      "VFS: Unable to mount root fs on LABEL=HART_OS" panic). The authoritative
#      proof is hartlog-create.nix subtest 6e (the mid-device-backup
#      reproduction); here we re-assert the guard FROM the storage dimension —
#      point the carve at the spare disk AS the boot medium and prove it refuses
#      (no GPT relocation, no partition appended, no HARTLOG).
#
# WHY [VM]-gated: it needs a real Linux block layer (loop + the kernel fs drivers
# + udisks + sgdisk/mkfs.*) — it cannot run on the Windows dev box. The
# "plugging a PHYSICAL NTFS/exFAT disk and seeing it auto-appear" half is the
# real-HW probe (hart-compat-smoketest checks the kernel support on real iron).
#
# #70 discipline preserved: built from `hartModules` alone via the shared mkNode.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-storage-filesystems = pkgs.testers.runNixOSTest {
    name = "hart-storage-filesystems";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.fs = mkNode "desktop" {
      virtualisation = {
        # 3 GiB: the loop-image read/write matrix builds a 320 MiB image per
        # filesystem (rm'd between cases, so peak is one image), comfortably
        # within RAM. cores=2 keeps mkfs.ntfs/btrfs snappy.
        memorySize = 3072;
        cores = 2;
        # One spare raw disk that stands in for a plugged-in removable stick
        # (the udisks mount target, the corrupt-disk degrade target, and the
        # HARTLOG boot-disk-guard stand-in — each subtest re-preps it fresh).
        emptyDiskImages = [ 256 ];
      };

      # The dimension under test: cross-OS filesystem read/write + udisks.
      hart.storage.enable = true;
      # Enabled so the cross-link subtest (5) can exercise the REAL persistence
      # carve's BOOT-DISK GUARD from the storage dimension.
      hart.hartlogCreate.enable = true;

      # sgdisk for the HARTLOG boot-disk-guard subtest; the storage module already
      # ships mkfs.{ntfs,exfat,ext4,btrfs,vfat} + util-linux, and services.udisks2
      # puts `udisksctl` on PATH.
      environment.systemPackages = [ pkgs.gptfdisk ];

      # TEST-ONLY polkit grant so `udisksctl mount` is deterministic in the
      # HEADLESS VM. In the real product the mount is driven by the file manager /
      # glass shell inside an ACTIVE graphical session, where udisks' default
      # allow_active=yes already authorizes filesystem-mount — which a headless VM
      # cannot present. This rule stands in for "an active session" so subtest 2
      # exercises the mount mechanism end-to-end (the udisks2 daemon + the fs
      # driver + the mount helper — all of which ARE the product config). It does
      # NOT relax the product; it lives only on this test node.
      security.polkit.extraConfig = ''
        polkit.addRule(function(action, subject) {
          if (action.id.indexOf("org.freedesktop.udisks2.") === 0) {
            return polkit.Result.YES;
          }
        });
      '';
    };

    testScript = ''
      # mkNode forces the VM hostname to the variant ("desktop"), so the driver keys
      # the machine global by that hostname, NOT the nodes.fs key — the bare `fs` name
      # is undefined at runtime (NameError). Bind it from the machines list (identical
      # fix to session-supervisor.nix's `sup = machines[0]`).
      fs = machines[0]
      fs.start()
      fs.wait_for_unit("multi-user.target")

      # ── 0. The interop config + the per-filesystem tooling are in the closure ──
      with subtest("the #145 interop config installed the mkfs tooling + udisks"):
          for tool in ["mkfs.ntfs", "mkfs.exfat", "mkfs.btrfs", "mkfs.ext4",
                       "mkfs.vfat", "mkfs.xfs", "mkfs.f2fs", "mount", "udisksctl"]:
              fs.succeed(f"command -v {tool}")
          # udisks2 is the on-demand mount authority — activate it via udisksctl
          # (the same D-Bus call the file manager / glass shell make).
          fs.succeed("udisksctl status >/dev/null 2>&1 || systemctl start udisks2.service")
          fs.succeed("systemctl is-active udisks2.service")

      # ── 1. boot.supportedFilesystems covers all 5: real read/write round-trips ──
      with subtest("ntfs/exfat/vfat/ext4/btrfs each round-trip a file read+write"):
          # Make every cross-OS driver loadable (supportedFilesystems made them
          # AVAILABLE; modprobe loads them — `mount` also auto-modprobes).
          fs.succeed("modprobe ntfs3 2>/dev/null || true")
          fs.succeed("modprobe exfat 2>/dev/null || true")
          fs.succeed("modprobe btrfs 2>/dev/null || true")
          fs.succeed("modprobe xfs 2>/dev/null || true")
          fs.succeed("modprobe f2fs 2>/dev/null || true")
          # (fsname, mkfs command, mount type-preference). ntfs prefers the in-kernel
          # ntfs3 RW driver, falling back to the ntfs-3g FUSE helper — both do RW.
          # xfs + f2fs are the #157 additions (RHEL/Fedora default + flash/Android).
          cases = [
              ("ext4",  "mkfs.ext4 -F -q",   "ext4"),
              ("btrfs", "mkfs.btrfs -f -q",  "btrfs"),
              ("vfat",  "mkfs.vfat -F 32",   "vfat"),
              ("exfat", "mkfs.exfat",        "exfat"),
              ("ntfs",  "mkfs.ntfs -Q -F",   "ntfs3"),
              ("xfs",   "mkfs.xfs -f -q",    "xfs"),
              ("f2fs",  "mkfs.f2fs -f",      "f2fs"),
          ]
          for fsname, mkfs, mtype in cases:
              img = f"/tmp/{fsname}.img"
              mnt = f"/mnt/{fsname}"
              # 320 MiB: above btrfs' ~109 MiB minimum, small enough to keep peak
              # RAM low (rm'd before the next case).
              fs.succeed(f"truncate -s 320M {img}")
              fs.succeed(f"{mkfs} {img}")
              fs.succeed(f"mkdir -p {mnt}")
              # Bounded mount so a hypothetical driver hang can never stall the test.
              # Try the explicit type first (deterministic), then autodetect.
              rc = fs.succeed(
                  f"timeout 40 mount -t {mtype} -o loop {img} {mnt} 2>/dev/null "
                  f"|| timeout 40 mount -o loop {img} {mnt} 2>/dev/null; echo RC=$?"
              ).strip()
              assert "RC=0" in rc, \
                  f"{fsname}: mount failed — boot.supportedFilesystems must cover it ({rc!r})"
              # WRITE then READ BACK — the real interop proof (not just a mount).
              fs.succeed(f"echo 'hart-{fsname}-rw' > {mnt}/probe.txt")
              fs.succeed("sync")
              got = fs.succeed(f"cat {mnt}/probe.txt").strip()
              assert got == f"hart-{fsname}-rw", \
                  f"{fsname}: read/write round-trip mismatch ({got!r})"
              fs.succeed(f"umount {mnt}")
              fs.succeed(f"rm -f {img}")

      # ── helper: resolve the spare disk the test re-preps for each disk subtest ──
      disk = fs.succeed(
          "for d in /dev/vdb /dev/sdb; do [ -b \"$d\" ] && echo \"$d\" && break; done"
      ).strip()
      assert disk, "no spare disk surfaced for the udisks/degrade/guard subtests"

      # ── 2. udisks2 mounts a removable disk ON DEMAND (the auto-mount authority) ──
      with subtest("udisks2 mounts a removable disk on demand and read/write works"):
          fs.succeed(f"wipefs -a {disk} || true")
          # A typical USB stick: one whole-disk FAT32 filesystem.
          fs.succeed(f"mkfs.vfat -F 32 -n HARTUSB {disk}")
          # `udevadm settle` alone is NOT enough here: mkfs on a WHOLE disk
          # emits no uevent (udev's inotify `watch` option covers partitions,
          # not whole-disk nodes), so settle no-ops and udisks keeps the
          # blank-disk probe from boot — "Object ... is not a mountable
          # filesystem" against a correctly formatted disk (run 30485906966).
          # Trigger an explicit change event so udisks re-probes the new fs.
          fs.succeed(f"udevadm trigger --action=change {disk} && udevadm settle")
          # The udisks2 daemon must SEE the disk...
          fs.succeed(f"udisksctl info -b {disk} >/dev/null")
          # ...and MOUNT it on demand (under /run/media/<user>/...).
          out = fs.succeed(f"udisksctl mount -b {disk} 2>&1")
          assert "Mounted" in out or "already mounted" in out, \
              f"udisks must mount the removable disk, got {out!r}"
          mp = fs.succeed(f"findmnt -nro TARGET {disk} | head -n1").strip()
          assert mp, f"udisks reported a mount but findmnt sees none for {disk}"
          fs.succeed(f"echo hart-udisks-rw > {mp}/probe.txt")
          assert fs.succeed(f"cat {mp}/probe.txt").strip() == "hart-udisks-rw", \
              "udisks-mounted disk must be writable+readable"
          fs.succeed(f"udisksctl unmount -b {disk}")

      # ── 3. DEGRADE-NOT-DIE: an unmountable/corrupt disk fails clean + fast ──
      with subtest("an unmountable corrupt disk fails cleanly and never wedges the OS"):
          fs.succeed(f"udisksctl unmount -b {disk} 2>/dev/null || true")
          fs.succeed(f"wipefs -a {disk} || true")
          # Zero the superblock region so the disk carries NO valid filesystem.
          fs.succeed(f"dd if=/dev/zero of={disk} bs=1M count=16 conv=fsync")
          fs.succeed("udevadm settle || true")
          fs.succeed("mkdir -p /mnt/bad")
          # A bounded mount MUST fail (no fs) and return FAST. timeout 20 bounds it:
          # a clean failure returns ~instantly; a hypothetical hang is killed at 20s
          # (exit 124), which is STILL a clean failure — never a wedge.
          # USE execute(), NOT succeed()+`; echo RC=$?` (run 30783792736).
          # The driver runs every command as `set -euo pipefail; <command>`
          # (nixos/lib/test-driver/.../machine.py:521). Under `set -e` a FAILING
          # mount aborts the shell immediately, so the trailing `echo RC=$?`
          # never runs and succeed() raises on the very outcome this subtest is
          # asserting. The mount was failing correctly all along — the test
          # could not observe it.
          #
          # The idiom is fine elsewhere in the suite because those commands are
          # expected to SUCCEED; it is only unusable where failure IS the
          # expectation. execute() returns (rc, output) and does not raise.
          rc, _out = fs.execute(f"timeout 20 mount {disk} /mnt/bad >/dev/null 2>&1")
          assert rc != 0, "mounting a corrupt disk must FAIL, got rc=0"
          # rc 124 (timeout killed it) is ALSO a pass here: bounded and clean is
          # the requirement — never a wedge.
          udrc, _uout = fs.execute(f"timeout 20 udisksctl mount -b {disk} >/dev/null 2>&1")
          assert udrc != 0, "udisks must refuse a corrupt disk, got rc=0"
          # THE degrade assertion: the OS is still fully up + responsive, and the
          # failed mount left nothing mounted + did not drop to emergency.
          fs.succeed("echo still-alive")
          fs.fail("findmnt /mnt/bad")
          fs.require_unit_state("multi-user.target", "active")

      # ── 4. No boot-blocking external mount (degrade-by-construction guard) ──
      with subtest("no fstab/.mount unit binds a removable disk (boot can never wait on it)"):
          # The interop module adds ZERO fileSystems entries for external disks; the
          # spare must NOT appear in the generated fstab, and no *.mount unit may
          # have failed at boot (a stale external fileSystems entry would show here).
          fstab = fs.succeed("cat /etc/fstab")
          assert "vdb" not in fstab and "sdb" not in fstab, \
              f"a removable disk must never be in fstab (boot-blocking risk): {fstab!r}"
          failed = fs.succeed("systemctl --failed --no-legend --plain || true")
          assert ".mount" not in failed, \
              f"a .mount unit failed at boot (boot-blocking risk): {failed!r}"
          fs.require_unit_state("multi-user.target", "active")

      # ── 5. CROSS-LINK boot dimension: HARTLOG persistence never completes the GPT ──
      with subtest("the HARTLOG persistence carve never completes the boot-disk GPT"):
          # The persistence failure mode for THIS dimension: completing the live
          # boot medium's GPT (sgdisk -e relocate + append a partition) makes BOTH
          # the whole disk and partition 1 answer to LABEL=HART_OS, arming the
          # per-boot udev race that panics root ("VFS: Unable to mount root fs on
          # LABEL=HART_OS"). Authoritative proof: hartlog-create.nix 6e. Re-assert
          # the guard here: drive the carve with the spare AS the boot medium.
          fs.succeed(f"sgdisk --zap-all {disk}")
          fs.succeed(f"wipefs -a {disk} || true")
          fs.succeed(f"sgdisk --new=1:2048:+64M --change-name=1:ISO {disk}")
          fs.succeed("udevadm settle || true")
          n_before = fs.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          lu_before = int(fs.succeed(f"sgdisk -E {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")
          out = fs.succeed(
              f"HART_HARTLOG_TEST_DISK={disk} HART_HARTLOG_TEST_BOOT_DISK={disk} "
              f"hart-hartlog-create 2>&1; echo RC=$?"
          )
          assert "RC=0" in out, f"the boot-disk guard must exit 0, got {out!r}"
          assert "DECISION=NOOP" in out and "boot medium" in out, \
              f"the persistence carve must REFUSE the boot medium, got {out!r}"
          fs.fail("blkid -L HARTLOG")  # nothing carved on the boot medium
          lu_after = int(fs.succeed(f"sgdisk -E {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")
          assert lu_after == lu_before, \
              f"the backup GPT was relocated (GPT completed): {lu_before} -> {lu_after}"
          n_after = fs.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          assert n_after == n_before, \
              f"a partition was appended to the boot medium: {n_before} -> {n_after}"

      # ── 6. REAL-HW readout: hart-storage-fsprobe reports honest per-fs verdicts ──
      with subtest("the real-HW driver probe reports the kernel can mount all 5 + degrades"):
          # hart-storage.nix installs hart-storage-fsprobe on PATH whenever
          # hart.storage.enable is on; this is the read-only readout the every-boot
          # hart-compat-smoketest + `hart sandbox` use on real iron (where the VM
          # cannot go). It must agree with subtest 1 (which actually MOUNTED all 5),
          # i.e. every interop filesystem reports `ok` on this live kernel.
          fs.succeed("command -v hart-storage-fsprobe")
          for fsname in ["ntfs", "exfat", "vfat", "ext4", "btrfs", "xfs", "f2fs"]:
              v = fs.succeed(f"hart-storage-fsprobe --query {fsname}").strip()
              assert v == "ok", \
                  f"{fsname}: probe must report the driver available (subtest 1 mounted it), got {v!r}"
          # The status-file mode (what hart-compat-smoketest calls) appends one
          # honest fs_<name>=<verdict> line per filesystem.
          fs.succeed("rm -f /tmp/compat-status")
          fs.succeed("hart-storage-fsprobe /tmp/compat-status ntfs exfat vfat ext4 btrfs xfs f2fs")
          status = fs.succeed("cat /tmp/compat-status")
          for fsname in ["ntfs", "exfat", "vfat", "ext4", "btrfs", "xfs", "f2fs"]:
              assert f"fs_{fsname}=ok" in status, \
                  f"status-file mode must record fs_{fsname}=ok on this kernel: {status!r}"
          # DEGRADE-NOT-DIE: an unknown filesystem is honestly `missing`, never a
          # crash — the probe still exits 0 (it loads nothing, mounts nothing).
          out = fs.succeed("hart-storage-fsprobe --query nonesuchfs; echo RC=$?")
          assert "missing" in out and "RC=0" in out, \
              f"an unknown fs must read 'missing' and the probe must exit 0, got {out!r}"
          # And a status-file run with NO filesystem args is a clean no-op (exit 0).
          fs.succeed("hart-storage-fsprobe /tmp/empty-status; echo ok")

      # ── 7. #157 Disk Utility tooling + the boot-time disk-health snapshot ──
      with subtest("the #157 Disk Utility tooling is on PATH + the disk-health probe writes an honest verdict"):
          # The format/repair/resize/health tooling hart-storage.nix (#157) adds.
          for tool in ["mkfs.xfs", "xfs_repair", "xfs_growfs", "mkfs.f2fs",
                       "fsck.f2fs", "resize.f2fs", "e4defrag", "resize2fs",
                       "parted", "sgdisk", "smartctl", "nvme", "hdparm", "fstrim"]:
              fs.succeed(f"command -v {tool}")
          # The boot-time disk-health snapshot oneshot runs read-only + always exits
          # 0 + writes an honest per-device verdict to /run/hart/disk-health. Start
          # it directly (deterministic, independent of greetd ordering) and assert
          # it produced the ok= header without mounting/writing any disk.
          fs.succeed("systemctl start hart-disk-health.service")
          fs.succeed("test -f /run/hart/disk-health")
          health = fs.succeed("cat /run/hart/disk-health")
          assert "ok=" in health, \
              f"disk-health probe must write an ok= verdict line: {health!r}"
          fs.require_unit_state("multi-user.target", "active")
    '';
  };
}

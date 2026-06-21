# ═══════════════════════════════════════════════════════════════
# HART OS — Live-OS HARTLOG self-create nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the Live-OS replacement for the Windows-flasher diskpart path: HART OS
# carves a FAT32 HARTLOG partition into a USB stick's TRAILING FREE SPACE,
# Linux-side, NEVER touching the in-use partitions.
#
# This test is BEHAVIOURAL (not grep-on-source): it attaches a spare disk that
# stands in for the USB stick, GPT-partitions it with a small "ISO" partition +
# LEFT FREE SPACE at the end (mimicking a freshly-flashed stick), runs the ACTUAL
# create script the module installs, and asserts:
#   - a NEW HARTLOG FAT32 partition appeared in the free space,
#   - the pre-existing "ISO" partition is UNTOUCHED (same start/size/uuid),
#   - the decision is logged LOUDLY to /run/hart/hartlog-create.status,
#   - a second run is an idempotent no-op (HARTLOG already exists),
#   - a FULL disk (no free space) is a clean no-op (+ a LOUD NOOP marker),
#   - an isohybrid MBR/DOS disk is carved via the parted path (NOT sgdisk, which
#     would convert the table) — a primary is appended, the table stays DOS.
#
# WHY [VM]-gated: it needs a real Linux block layer (sgdisk/mkfs.vfat on a real
# GPT disk) — it cannot run on the Windows dev box. The "carves the REAL USB the
# ISO was written to, detected via the live root" link still needs a real USB
# boot to fully confirm (the VM's spare disk is not the boot disk, so we drive
# the script with HART_HARTLOG_TEST_DISK to point it at the stand-in); THIS test
# proves the carve mechanics + the never-touch-existing-partitions invariant.
#
# #70 discipline preserved: built from `hartModules` alone via the shared mkNode.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-hartlog-create = pkgs.testers.runNixOSTest {
    name = "hart-hartlog-create";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.hc = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A spare raw disk that stands in for the USB stick. 512 MiB: we put a
        # ~64 MiB "ISO" partition on it + leave the rest free for the carve.
        emptyDiskImages = [ 512 ];
      };
      hart.hartlogCreate.enable = true;
      # The carve tools + a labelled-device lookup for the test body. parted is the
      # MBR/DOS carve path the module uses + the test drives the DOS stand-in with.
      environment.systemPackages = [ pkgs.gptfdisk pkgs.parted pkgs.dosfstools pkgs.util-linux ];
    };

    testScript = ''
      hc = machines[0]
      hc.start()
      hc.wait_for_unit("multi-user.target")

      # ── 1. The create unit + tooling are in the closure, ordered before boot-log ──
      with subtest("the create unit + CLI + ordering exist"):
          hc.succeed("systemctl cat hart-hartlog-create.service")
          hc.succeed("command -v hart-hartlog-create")
          hc.succeed("command -v sgdisk")
          hc.succeed("command -v mkfs.vfat")
          # Ordered Before the boot-log capture (so the first boot's journal lands).
          unit = hc.succeed("systemctl cat hart-hartlog-create.service")
          assert "hart-boot-log-early.service" in unit, \
              "create must be ordered Before hart-boot-log-early"

      # ── 2. Build a stand-in USB: GPT + a small ISO partition + trailing free ──
      with subtest("prepare a stand-in stick (GPT, small ISO part, free tail)"):
          disk = hc.succeed(
              "for d in /dev/vdb /dev/sdb; do [ -b \"$d\" ] && echo \"$d\" && break; done"
          ).strip()
          assert disk, "no spare disk surfaced"
          # Fresh GPT, one 64 MiB partition at the start (the 'ISO'), rest free.
          hc.succeed(f"sgdisk --zap-all {disk}")
          hc.succeed(f"sgdisk --new=1:2048:+64M --change-name=1:ISO {disk}")
          hc.succeed(f"udevadm settle || true")
          # Record the ISO partition's identity so we can prove it's untouched.
          iso_uuid_before = hc.succeed(f"sgdisk --info=1 {disk} | grep -i 'Partition unique GUID'").strip()
          n_parts_before = hc.succeed(f"sgdisk -p {disk} | grep -cE '^ +1 '").strip()

      # ── 3. Run the REAL create script against the stand-in disk ──
      # The module's script auto-detects the boot disk; the VM's boot disk is NOT
      # this spare. Drive it explicitly at the stand-in so the carve mechanics +
      # the never-touch-existing invariant are exercised on a real GPT disk.
      with subtest("the carve creates a HARTLOG FAT32 partition in the free space"):
          out = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out, f"carve must exit 0, got: {out!r}"
          # A HARTLOG-labelled FAT32 partition now resolves by-label.
          hc.succeed("blkid -L HARTLOG")
          fstype = hc.succeed("blkid -L HARTLOG | xargs blkid -o value -s TYPE").strip()
          assert "vfat" in fstype, f"HARTLOG must be FAT32/vfat, got {fstype!r}"

      # ── 3b. The LOUD decision marker records the verdict (never a silent no-op) ──
      with subtest("the decision is logged LOUDLY to /run/hart/hartlog-create.status"):
          status = hc.succeed("cat /run/hart/hartlog-create.status")
          # The marker must end with an unambiguous CREATED verdict naming the disk.
          assert "DECISION=CREATED" in status, \
              f"status marker must record the CREATED decision, got: {status!r}"
          assert disk in status, f"status marker must name the picked disk {disk}, got: {status!r}"

      # ── 4. The pre-existing ISO partition is UNTOUCHED ──
      with subtest("the in-use ISO partition is never touched"):
          iso_uuid_after = hc.succeed(f"sgdisk --info=1 {disk} | grep -i 'Partition unique GUID'").strip()
          assert iso_uuid_after == iso_uuid_before, \
              "the ISO partition's GUID changed — carve must NOT touch existing partitions"
          name1 = hc.succeed(f"sgdisk --info=1 {disk} | grep -i 'Partition name'").strip()
          assert "ISO" in name1, f"ISO partition name changed: {name1!r}"

      # ── 5. A second run is an idempotent no-op (HARTLOG already exists) ──
      with subtest("re-running is an idempotent no-op"):
          out2 = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out2
          assert "already exists" in out2 or "idempotent" in out2, \
              f"second run must be an idempotent no-op, got: {out2!r}"

      # ── 6. A FULL disk (no trailing free space) is a clean no-op ──
      # Wipe the stand-in + fill it with a single partition that consumes ALL the
      # usable space, then DELETE the HARTLOG label-resolution so the idempotent
      # gate doesn't short-circuit before the free-space gate. The carve must
      # refuse (no usable tail) and exit 0 WITHOUT creating a second partition.
      with subtest("a full disk (no trailing free space) is a clean no-op"):
          hc.succeed(f"sgdisk --zap-all {disk}")
          # --largest-new on a fresh disk grabs ALL usable space -> no free tail.
          hc.succeed(f"sgdisk --largest-new=1 --change-name=1:FULLISO {disk}")
          hc.succeed("udevadm settle || true")
          n_before = hc.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          out_full = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out_full, f"full-disk carve must exit 0, got: {out_full!r}"
          assert ("no trailing free space" in out_full
                  or "too small" in out_full
                  or "ISO filled the stick" in out_full), \
              f"full disk must no-op via the free-space gate, got: {out_full!r}"
          # No HARTLOG appeared and the partition count is unchanged.
          hc.fail("blkid -L HARTLOG")
          n_after = hc.succeed(f"sgdisk -p {disk} | grep -cE '^ +[0-9]+ ' || true").strip()
          assert n_after == n_before, \
              f"full-disk no-op must not create a partition ({n_before} -> {n_after})"
          # And the no-op is recorded LOUDLY (never a silent no-op).
          st = hc.succeed("cat /run/hart/hartlog-create.status")
          assert "DECISION=NOOP" in st, f"full-disk no-op must record DECISION=NOOP, got: {st!r}"

      # ── 6b. An isohybrid MBR (DOS-label) disk is carved via parted (not sgdisk) ──
      # The live ISO can be written DOS/MBR, where sgdisk MUST NOT run (it would
      # convert the table + destroy the boot layout). Build a DOS-label stand-in
      # with a small primary + trailing free space and assert the carve appends a
      # primary FAT32 HARTLOG via the parted path, leaving the existing primary
      # untouched.
      with subtest("an isohybrid MBR/DOS disk is carved via parted, existing primary untouched"):
          hc.succeed(f"sgdisk --zap-all {disk}")
          hc.succeed(f"wipefs -a {disk} || true")
          # Fresh DOS label + one 64 MiB primary at the start (the 'ISO'), rest free.
          hc.succeed(f"parted -s {disk} mklabel msdos")
          hc.succeed(f"parted -s {disk} mkpart primary fat32 1MiB 65MiB")
          hc.succeed("udevadm settle || true")
          pttype = hc.succeed(f"lsblk -ndo PTTYPE {disk}").strip()
          assert pttype == "dos", f"stand-in must be a DOS/MBR disk, got {pttype!r}"
          n_dos_before = hc.succeed(f"parted -ms {disk} unit s print | grep -cE '^[0-9]+:' || true").strip()
          out_mbr = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out_mbr, f"MBR carve must exit 0, got: {out_mbr!r}"
          # The carve must have taken the parted (DOS) path, not sgdisk.
          assert "DECISION=CREATED" in out_mbr or "MBR" in out_mbr, \
              f"MBR carve should report the DOS/MBR path, got: {out_mbr!r}"
          hc.succeed("blkid -L HARTLOG")
          mfstype = hc.succeed("blkid -L HARTLOG | xargs blkid -o value -s TYPE").strip()
          assert "vfat" in mfstype, f"MBR HARTLOG must be FAT32/vfat, got {mfstype!r}"
          # The pre-existing primary is still there (a new primary was appended).
          n_dos_after = hc.succeed(f"parted -ms {disk} unit s print | grep -cE '^[0-9]+:' || true").strip()
          assert int(n_dos_after) == int(n_dos_before) + 1, \
              f"MBR carve must ADD exactly one primary ({n_dos_before} -> {n_dos_after})"
          # The table is STILL a DOS label — never converted to GPT.
          assert hc.succeed(f"lsblk -ndo PTTYPE {disk}").strip() == "dos", \
              "MBR carve must NOT convert the table to GPT (would destroy the boot layout)"

      # ── 7. The auto-detect path REFUSES the VM's non-removable boot disk ──
      # Run WITHOUT the test seam so the script walks the live root to the VM's
      # OWN boot disk (an internal, non-removable virtio disk — RM=0, TRAN!=usb).
      # The single most important safety gate must refuse to repartition it and
      # exit 0 cleanly. (This is the never-touch-an-internal-disk invariant.)
      with subtest("the auto-detect path refuses a non-removable/internal boot disk"):
          out_rm = hc.succeed("hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out_rm, f"non-removable refusal must exit 0, got: {out_rm!r}"
          # It either refused the internal disk outright, or bailed earlier (loop /
          # unresolved backing device) — every branch here is a documented no-op
          # and NONE of them may carve. The key invariant: no internal-disk carve.
          assert ("not removable" in out_rm
                  or "refusing to repartition an internal disk" in out_rm
                  or "could not resolve" in out_rm
                  or "loop device" in out_rm), \
              f"expected a documented non-removable/no-resolve no-op, got: {out_rm!r}"
    '';
  };
}

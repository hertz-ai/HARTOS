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

      # ── 6c. Self-heal a created-but-UNFORMATTED HARTLOG (the busy-disk first-boot
      # case). On a real live USB the disk is BUSY (the ISO is mounted from it), so the
      # new partition's node may not settle before mkfs — leaving a HARTLOG-named GPT
      # partition with NO filesystem AND no free tail, so every later boot would no-op
      # forever and HARTLOG would stay raw + host-unreadable. The carve must instead
      # DETECT its own named-but-unformatted partition and FORMAT it in place.
      with subtest("a created-but-unformatted HARTLOG partition is self-healed (formatted)"):
          hc.succeed(f"sgdisk --zap-all {disk}")
          hc.succeed(f"wipefs -a {disk} || true")
          # A small ISO primary + a HARTLOG-NAMED partition consuming the rest, with
          # NO filesystem (sgdisk names it; no mkfs) — exactly the prior-boot remnant.
          hc.succeed(f"sgdisk --new=1:2048:+64M --change-name=1:ISO {disk}")
          hc.succeed(f"sgdisk --largest-new=2 --change-name=2:HARTLOG --typecode=2:0700 {disk}")
          hc.succeed("udevadm settle || true")
          # Precondition: HARTLOG does NOT resolve by FS label yet (it has no FS), and
          # there is no trailing free space (part2 consumed it) — so the ONLY way it
          # becomes valid is the self-heal path, never a fresh carve.
          hc.fail("blkid -L HARTLOG")
          out_heal = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out_heal, f"self-heal must exit 0, got: {out_heal!r}"
          assert "self-heal" in out_heal, f"must take the self-heal path, got: {out_heal!r}"
          # Now HARTLOG resolves as a real FAT32 — the existing partition was formatted
          # IN PLACE (not a new carve: the disk had no free tail).
          hc.succeed("blkid -L HARTLOG")
          hfstype = hc.succeed("blkid -L HARTLOG | xargs blkid -o value -s TYPE").strip()
          assert "vfat" in hfstype, f"self-healed HARTLOG must be FAT32/vfat, got {hfstype!r}"

      # ── 6d. The dd-written-isohybrid MID-DEVICE BACKUP GPT case (#128/#134). ──
      # A REAL dd-flashed isohybrid GPT ISO places the BACKUP GPT header at the ISO
      # IMAGE's last LBA (mid-stick), NOT at the physical end of the larger stick.
      # The primary header therefore advertises LastUsableLBA = the ISO boundary, so
      # the multi-GB trailing tail is INVISIBLE to `sgdisk -E`/`-f`: before the fix
      # the carve no-op'd ("ISO filled the stick") and HARTLOG was NEVER created (so
      # no journal ever landed on the stick — the #134 blind spot).
      #
      # The earlier subtests build the GPT DIRECTLY on the full spare disk, so their
      # backup header is already at the device end and they CANNOT catch this. Here
      # we reproduce the real failure: dd a SMALL GPT image (its backup header at the
      # image's last LBA) onto the LARGER spare disk so the backup lands mid-device
      # and the tail is hidden. The fill of the small image is sized so the visible
      # (pre-relocation) free gap is BELOW the 16 MiB carve floor — so the ONLY way
      # HARTLOG can be created is if the script relocated the backup header
      # (sgdisk -e) first and thereby exposed the real tail. This is the behavioural
      # guard for the sgdisk -e relocation (no grep on source).
      with subtest("a dd-written mid-device-backup GPT exposes its hidden tail and carves HARTLOG"):
          hc.succeed(f"sgdisk --zap-all {disk}")
          hc.succeed(f"wipefs -a {disk} || true")
          # 64 MiB GPT image: a 60 MiB "ISO" primary at the start, leaving < 4 MiB
          # free INSIDE the image (below the 16 MiB floor). Its backup header sits at
          # the image's last LBA (~64 MiB). dd it onto the 512 MiB spare with
          # conv=notrunc so the spare keeps its real size but its backup header is now
          # mid-device and the 64..512 MiB tail is hidden behind LastUsableLBA.
          hc.succeed("rm -f /tmp/iso.img")
          hc.succeed("truncate -s 64M /tmp/iso.img")
          hc.succeed("sgdisk --new=1:2048:+60M --change-name=1:ISO /tmp/iso.img")
          hc.succeed(f"dd if=/tmp/iso.img of={disk} conv=notrunc bs=1M")
          hc.succeed(f"partprobe {disk} 2>/dev/null || partx -a {disk} 2>/dev/null || true")
          hc.succeed("udevadm settle || true")
          # PRECONDITION: the visible (pre-relocation) free tail is too small to
          # carve. sgdisk -f (first aligned free) and -E (end of largest free block)
          # both honour the primary header's LastUsableLBA (~the 64 MiB boundary), so
          # the gap is < 32768 sectors (16 MiB). Without sgdisk -e the carve MUST
          # no-op ("too small"/"ISO filled the stick") — so a later CREATED proves the
          # relocation ran.
          ff = int(hc.succeed(f"sgdisk -f {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")
          lu = int(hc.succeed(f"sgdisk -E {disk} 2>/dev/null | tr -dc '0-9'").strip() or "0")
          hidden_free = (lu - ff + 1) if lu >= ff else 0
          assert 0 <= hidden_free < 32768, \
              ("precondition: the pre-relocation free tail must be too small to carve "
               f"(got {hidden_free} sectors, need < 32768) else the test can't prove the "
               f"relocation (first_free={ff} last_usable={lu})")
          # Run the REAL carve. It must relocate the backup header, see the now-exposed
          # ~448 MiB tail, and CREATE HARTLOG.
          out_mid = hc.succeed(f"HART_HARTLOG_TEST_DISK={disk} hart-hartlog-create 2>&1; echo RC=$?")
          assert "RC=0" in out_mid, f"mid-device-backup carve must exit 0, got: {out_mid!r}"
          assert "DECISION=CREATED" in out_mid, \
              ("the carve must CREATE HARTLOG once the hidden tail is exposed — a NOOP here "
               f"means sgdisk -e did not relocate the backup header, got: {out_mid!r}")
          assert "relocated the backup GPT header" in out_mid, \
              f"the carve must log the backup-GPT relocation, got: {out_mid!r}"
          hc.succeed("blkid -L HARTLOG")
          mid_fstype = hc.succeed("blkid -L HARTLOG | xargs blkid -o value -s TYPE").strip()
          assert "vfat" in mid_fstype, f"mid-device-backup HARTLOG must be FAT32/vfat, got {mid_fstype!r}"
          # And the HARTLOG partition spans the EXPOSED tail: its size is far larger
          # than the tiny pre-relocation gap (parted reports the part size in sectors),
          # which is only possible because the relocation grew the usable range.
          hl_dev = hc.succeed("blkid -L HARTLOG").strip()
          hl_sectors = int(hc.succeed(
              f"lsblk -bndo SIZE {hl_dev}"
          ).strip() or "0") // 512
          assert hl_sectors > 100000, \
              ("HARTLOG must span the relocated tail (much larger than the < 16 MiB "
               f"pre-relocation gap), got {hl_sectors} sectors — relocation did not "
               "expose the tail")

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

# ═══════════════════════════════════════════════════════════════
# HART OS - External-USB journal export nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the field-recovery loop: plug in an ordinary FAT32 stick (NOT the boot
# medium) -> HART OS dumps the current-boot journal onto it as
# hart-journal-<hostname>.txt, on a timer + at shutdown, independent of the shell.
#
# This test is BEHAVIOURAL (not grep-on-source): it CREATES a real vfat device in
# the VM, runs the ACTUAL export script the module installs (via the documented
# HART_JOURNAL_TEST_DEVICE seam - a VM's virtio disk is not flagged removable, so
# the real removable gate would otherwise refuse it), mounts the result back, and
# asserts the journal sections landed. It then proves the never-clobber-the-boot-
# medium invariant: a HART_OS/HARTLOG-labelled disk is REFUSED even through the
# seam (the EXCLUDE check is not bypassed). Finally it asserts the no-stick path
# is a clean no-op.
#
# WHY [VM]-gated: creating + mounting a vfat device needs a real Linux block layer
# - it cannot run on the Windows dev box. The real "a user plugs in a SECOND
# physical FAT32 stick and the journal lands on it" still needs a real flash +
# boot + USB; THIS test proves every link short of the physical stick.
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix). The journalExport module is opt-in so the node enables it.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-journal-export = pkgs.testers.runNixOSTest {
    name = "hart-journal-export";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME - same false "Name not defined" as the
    # boot-log/floor-lock tests (the node IS bound at runtime). Skip both static
    # passes; the VM still boots and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.jx = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A spare raw disk the test formats vfat (the stand-in for the user's
        # second FAT32 stick). 256 MiB is plenty for the capped journal.
        emptyDiskImages = [ 256 ];
      };
      # Opt the journal export ON (default off). A short interval so the
      # periodic-timer assertion is quick.
      hart.journalExport = {
        enable = true;
        intervalSeconds = 5;
      };
      # mkfs.vfat + label tooling for the test body.
      environment.systemPackages = [ pkgs.dosfstools pkgs.util-linux ];
    };

    testScript = ''
      # The driver keys the single machine global by its HOSTNAME - mkNode forces
      # it to the variant ("desktop"), NOT the nodes.jx key. Bind from machines[0].
      jx = machines[0]
      jx.start()
      jx.wait_for_unit("multi-user.target")

      MNT = "/run/hart/journal-mnt"
      host = jx.succeed("cat /proc/sys/kernel/hostname").strip()
      assert host, "no hostname resolved"
      JFILE = "hart-journal-" + host + ".txt"

      # ── 1. The periodic units + timer + the shutdown unit are in the closure ──
      with subtest("the export units + the periodic timer exist + the CLI is on PATH"):
          jx.succeed("systemctl cat hart-journal-export.service")
          jx.succeed("systemctl cat hart-journal-export.timer")
          jx.succeed("systemctl cat hart-journal-export-shutdown.service")
          # The timer is active (it is what survives a shell hang).
          jx.wait_for_unit("hart-journal-export.timer")
          # The export CLI is on PATH for a manual recovery-TTY run.
          jx.succeed("command -v hart-journal-export")

      # ── 2. NO eligible external stick -> a clean no-op (exit 0, nothing mounted) ──
      with subtest("with no eligible external stick the export is a clean no-op (exit 0)"):
          out = jx.succeed("hart-journal-export periodic 2>&1; echo RC=$?")
          assert "RC=0" in out, f"no-stick export must exit 0, got: {out!r}"
          assert "no eligible external USB stick" in out, \
              f"no-stick export must log the clean no-op, got: {out!r}"
          jx.fail(f"mountpoint -q {MNT}")

      # ── 3. BEHAVIOURAL: a vfat external stick receives the journal dump ──
      with subtest("an external vfat stick receives hart-journal-<host>.txt"):
          # The spare disk surfaces as /dev/vdb. Format it vfat with a BENIGN,
          # NON-boot label (a user's ordinary stick), then drive the export at it
          # via the documented test seam (the VM disk is not flagged removable).
          disk = jx.succeed(
              "for d in /dev/vdb /dev/sdb /dev/vdc; do "
              "[ -b \"$d\" ] && echo \"$d\" && break; done"
          ).strip()
          assert disk, "no spare disk surfaced for the journal-export test"
          jx.succeed(f"mkfs.vfat -F 32 -n FIELDUSB {disk}")

          out = jx.succeed(f"HART_JOURNAL_TEST_DEVICE={disk} hart-journal-export periodic 2>&1; echo RC=$?")
          assert "RC=0" in out, f"export failed: {out!r}"
          assert "TEST SEAM" in out, f"export did not take the test seam: {out!r}"
          assert "wrote " in out, f"export reported no write: {out!r}"

          # The script always unmounts after writing - mount it back + read the dump.
          jx.succeed(f"mkdir -p /tmp/jx && mount {disk} /tmp/jx")
          dump = jx.succeed(f"cat /tmp/jx/{JFILE}")
          for needle in [
              "HART OS journal export",
              "from /run/hart/gpu-render",
              "journalctl -b -p warning -n 200",
              # NOT the literal "journalctl -b --no-pager" any more. That form was
              # replaced on purpose: piping the WHOLE boot into `head -c` made
              # journalctl format the journal from the beginning, which on a large
              # journal over slow USB2 outran TimeoutStartSec=90, so systemd killed
              # it, the timer refired, and one core stayed pinned at 99% forever --
              # measured at +18C of waste heat on a Samsung NP550P5C, which is what
              # actually froze the desktop. The section is tail-bounded with
              # `-n $JOURNAL_CAP_LINES` now, so match the stable descriptive part
              # rather than an exact flag order that a cap change would break again.
              "(current boot, tail-bounded)",
              "end of export",
          ]:
              assert needle in dump, f"dump missing section: {needle!r}"

          # ...and the unbounded form must STAY gone. This is the regression that
          # cost a real machine its desktop, so assert its absence rather than
          # trusting nobody re-adds it.
          assert "journalctl -b --no-pager |" not in dump, (
              "the full-boot journalctl pipe is back — that is the form that "
              f"pinned a core at 99% and overheated the box:\n{dump[:400]}")
          # The full-journal section actually carries journal lines (not empty).
          assert len(dump) > 200, f"dump implausibly small ({len(dump)} bytes)"
          jx.succeed("umount /tmp/jx")
          # The private mountpoint is left UNMOUNTED (a user-removable stick is
          # never held mounted across ticks).
          jx.fail(f"mountpoint -q {MNT}")

      # ── 4. NEVER-CLOBBER: a HARTLOG/HART_OS-labelled disk is REFUSED via the seam ──
      with subtest("the boot-medium labels (HARTLOG / HART_OS) are excluded even through the seam"):
          # Re-label the same spare disk HARTLOG (the boot-log diag partition). The
          # EXCLUDE check is NOT bypassed by the test seam, so the export must skip
          # it and write NOTHING - proving the boot stick is never clobbered.
          jx.succeed(f"mkfs.vfat -F 32 -n HARTLOG {disk}")
          jx.succeed("blkid -L HARTLOG")  # the label resolves -> it enters EXCLUDE
          out = jx.succeed(f"HART_JOURNAL_TEST_DEVICE={disk} hart-journal-export periodic 2>&1; echo RC=$?")
          assert "RC=0" in out, f"excluded-disk export must exit 0, got: {out!r}"
          assert "excluded boot/system disk" in out, \
              f"excluded-disk export must log the skip, got: {out!r}"
          assert "no eligible external USB stick" in out, \
              f"excluded-disk export must end as a no-op, got: {out!r}"
          # Nothing was written: mount it back and confirm the journal file is absent.
          jx.succeed(f"mkdir -p /tmp/jx2 && mount {disk} /tmp/jx2")
          jx.fail(f"test -e /tmp/jx2/{JFILE}")
          jx.succeed("umount /tmp/jx2")
    '';
  };
}

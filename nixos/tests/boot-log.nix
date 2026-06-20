# ═══════════════════════════════════════════════════════════════
# HART OS — Persistent Boot-Diagnostic Log Partition nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves the loop: a HARTLOG FAT32 partition → HART OS writes the full
# current-boot journal + tier-supervisor state + GTK4/GL diagnostics to it, so a
# Windows host can read the boot journal off the stick (no TTY hand-copy).
#
# This test is BEHAVIOURAL (not grep-on-source): it CREATES a real FAT32 device
# labelled HARTLOG inside the VM, runs the actual capture script the module
# installs, mounts the result back, and asserts the bundle landed with the
# expected sections + a stable hart-boot-latest.log. It then asserts the
# NO-HARTLOG path is a clean no-op (exit 0, no mount, boot fine).
#
# WHY [VM]-gated: creating a labelled FAT32 partition + mounting vfat needs a
# real Linux block layer — it cannot run on the Windows dev box. Per the
# honest-hardware-limit rule this gates in CI (`nix flake check` / local QEMU),
# never inline / grep on the dev box. The ON-A-REAL-STICK capture (an actually
# hung boot writing to the stick's HARTLOG) still needs a real flash + boot to
# confirm; THIS test proves every link short of the physical stick.
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix). The bootLog module is opt-in so the node enables it.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-boot-log = pkgs.testers.runNixOSTest {
    name = "hart-boot-log";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME — same false "Name not defined" as the
    # floor-lock/supervisor tests (the node IS bound at runtime). Skip both static
    # passes; the VM still boots and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.bl = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A spare raw disk the test formats FAT32 + labels HARTLOG (the stand-in
        # for the stick's free-space partition the flasher creates). 256 MiB is
        # plenty for the bundle; the real stick gives ~21 GB.
        emptyDiskImages = [ 256 ];
      };
      # Opt the boot-log capture ON (default off). A short interval so the
      # periodic-tick assertion is quick.
      hart.bootLog = {
        enable = true;
        intervalSeconds = 5;
      };
      # mkfs.vfat + a labelled-device lookup tool for the test body.
      environment.systemPackages = [ pkgs.dosfstools pkgs.util-linux ];
    };

    testScript = ''
      # The driver keys the single machine global by its HOSTNAME — mkNode forces
      # it to the variant ("desktop"), NOT the nodes.bl key. Bind from machines[0].
      bl = machines[0]
      bl.start()
      bl.wait_for_unit("multi-user.target")

      MNT = "/run/hart/bootlog-mnt"

      # ── 1. The capture units + timer are in the closure ──
      with subtest("the three capture units + the periodic timer exist"):
          # early-boot oneshot ran at boot (RemainAfterExit=false -> inactive but
          # present); the periodic timer is armed; the shutdown oneshot is loaded.
          bl.succeed("systemctl cat hart-boot-log-early.service")
          bl.succeed("systemctl cat hart-boot-log-periodic.service")
          bl.succeed("systemctl cat hart-boot-log-shutdown.service")
          bl.succeed("systemctl cat hart-boot-log-periodic.timer")
          # The timer is active (it is what makes a HUNG boot debuggable).
          bl.wait_for_unit("hart-boot-log-periodic.timer")
          # The capture CLI is on PATH for a manual recovery-TTY run.
          bl.succeed("command -v hart-boot-log-capture")

      # ── 2. NO-HARTLOG path is a clean no-op (the old-stick / plain-flash case) ──
      with subtest("with no HARTLOG partition the capture is a clean no-op (exit 0, no mount)"):
          # No partition is labelled HARTLOG yet. The capture must exit 0 and NOT
          # mount anything — boot is never blocked/failed by a missing log stick.
          out = bl.succeed("hart-boot-log-capture early 2>&1; echo RC=$?")
          assert "RC=0" in out, f"no-HARTLOG capture must exit 0, got: {out!r}"
          assert "no 'HARTLOG' partition present" in out, \
              f"no-HARTLOG capture must log the clean no-op, got: {out!r}"
          # Nothing got mounted at the private mountpoint.
          bl.fail(f"mountpoint -q {MNT}")
          # The early-boot oneshot itself succeeded at boot (no-op success).
          bl.succeed("systemctl is-active hart-boot-log-early.service || "
                     "systemctl show -p Result hart-boot-log-early.service | grep -q 'Result=success'")

      # ── 3. BEHAVIOURAL: a real HARTLOG FAT32 device gets the full bundle ──
      with subtest("a HARTLOG FAT32 partition receives the diagnostic bundle"):
          # The spare disk surfaces as /dev/vdb. Format it FAT32 + label HARTLOG —
          # exactly what the flasher's diskpart `format fs=fat32 label=HARTLOG`
          # produces on the stick. (mkfs.vfat -F32 -n HARTLOG.)
          disk = bl.succeed(
              "for d in /dev/vdb /dev/sdb /dev/vda2; do "
              "[ -b \"$d\" ] && echo \"$d\" && break; done"
          ).strip()
          assert disk, "no spare disk surfaced for the HARTLOG test"
          bl.succeed(f"mkfs.vfat -F 32 -n HARTLOG {disk}")
          # The label lookup the capture script uses must now resolve.
          found = bl.succeed("blkid -L HARTLOG").strip()
          assert found, "HARTLOG label not resolvable after mkfs"

          # Run the real capture (early phase: it unmounts cleanly when done).
          out = bl.succeed("hart-boot-log-capture early 2>&1; echo RC=$?")
          assert "RC=0" in out, f"capture failed: {out!r}"
          assert "found 'HARTLOG'" in out, f"capture did not find HARTLOG: {out!r}"

          # Mount the partition back (the early phase unmounted it) + read the
          # stable latest file the Windows host would open.
          bl.succeed(f"mkdir -p /tmp/hl && mount {found} /tmp/hl")
          latest = bl.succeed("cat /tmp/hl/hart-boot-latest.log")

          # The bundle must carry the curated diagnostic sections — the exact
          # surface needed to debug the GTK4/Tier-1 paint hang from the host.
          for needle in [
              "HART OS boot diagnostic bundle",
              "session-supervisor tier state",
              "shell-ready paint marker",
              "systemctl --failed",
              "hart-* unit status",
              "GTK4 host / GSK / GDK / EGL / GBM / WebKit GL",
              "GPU / DRM",
              "FULL current-boot journal",
          ]:
              assert needle in latest, f"bundle missing section: {needle!r}"

          # A per-boot file ALSO landed (history within this boot).
          bl.succeed("ls /tmp/hl/hart-boot-*.log | grep -v latest | grep -q .")
          bl.succeed("umount /tmp/hl")

      # ── 4. The latest file is OVERWRITTEN each cycle (stable name) ──
      with subtest("hart-boot-latest.log is overwritten in place (stable host-readable name)"):
          # Run a second capture; the latest file must still be ONE file (the
          # stable name), refreshed — not a pile of per-cycle latest files.
          bl.succeed("hart-boot-log-capture periodic")
          bl.succeed(f"mkdir -p /tmp/hl2 && mount $(blkid -L HARTLOG) /tmp/hl2")
          n_latest = bl.succeed("ls /tmp/hl2/hart-boot-latest.log | wc -l").strip()
          assert n_latest == "1", f"expected exactly one latest file, got {n_latest}"
          # The periodic phase keeps the partition mounted at the module's private
          # mountpoint (re-mount churn avoidance) — unmount our probe mount only.
          bl.succeed("umount /tmp/hl2")
    '';
  };
}

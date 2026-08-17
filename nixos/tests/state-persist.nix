# ═══════════════════════════════════════════════════════════════
# HART OS - Stateful-across-boots (HARTSTATE persistence) nixosTest
# ═══════════════════════════════════════════════════════════════
#
# Proves hart-state-persist: on boot, IF a HARTSTATE-labelled partition is
# present, HART OS bind-persists the Wi-Fi credentials + the HART state dir + the
# admin home onto it so they SURVIVE reboot - SECURELY, and NEVER blocking boot.
#
# BEHAVIOURAL (not grep-on-source): it attaches a spare disk that stands in for
# the USB's HARTSTATE partition, formats it, runs the ACTUAL persist script the
# module installs, and asserts:
#   - the unit exists, is a bounded oneshot ordered Before NetworkManager, and
#     NOTHING requires it (so it can never emit a boot-blocking dependency),
#   - with NO HARTSTATE label present the BOOT-time run is a clean DECISION=NOOP
#     and the box still reached multi-user + bound nothing (stateless, as today),
#   - on a real ext4 HARTSTATE stand-in the Wi-Fi / HART-state / home paths become
#     bind mounts, the Wi-Fi dir is SECURE (0700 root:root), and data written to
#     the Wi-Fi path lands on the actual partition (survives),
#   - on a non-POSIX (vfat) HARTSTATE the Wi-Fi bind is FAIL-SECURE skipped (never
#     stores NM secrets world-readable) while the HART state still persists.
#
# WHY [VM]-gated: it needs a real Linux block layer (mkfs/mount/bind on a real
# disk) - it cannot run on the Windows dev box, and nix does not build there. The
# "state survives a real reboot off the real USB's HARTSTATE" link still needs a
# real flash + USB boot (VERIFY ON THE NODE VIA THE LOOP); THIS test proves the
# persist mechanics + the secure-perms + fail-secure + never-block invariants.
#
# #70 discipline preserved: built from `hartModules` alone via the shared mkNode.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-state-persist = pkgs.testers.runNixOSTest {
    name = "hart-state-persist";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.sp = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A spare raw disk standing in for the USB's HARTSTATE partition.
        # 1 GiB, not 256 MiB: the real USB HARTSTATE partition the flasher carves
        # is GBs, and the first-boot persist SEEDS /var/lib/hart (cp -a) into it.
        # 256 MiB was smaller than that seed, so the bulky hart-state copy filled
        # the fs and the home + wifi backing dirs then failed on ENOSPC (VM run
        # 31347690532). A realistic stand-in lets all three persist; the module's
        # reordered wifi-first persist additionally guarantees the credentials
        # survive even a genuinely space-constrained HARTSTATE.
        emptyDiskImages = [ 1024 ];
      };
      hart.statePersist.enable = true;
      # The format/mount tooling the test drives the stand-in with (mkfs.ext4,
      # mkfs.vfat, wipefs, blkid/mount/mountpoint).
      environment.systemPackages = [ pkgs.e2fsprogs pkgs.dosfstools pkgs.util-linux ];
    };

    testScript = ''
      sp = machines[0]
      sp.start()
      sp.wait_for_unit("multi-user.target")

      # ── 1. The unit exists, is bounded, ordered Before NM, requires nothing ──
      with subtest("the persist unit is a bounded oneshot, Before NM, boot-blocking-free"):
          unit = sp.succeed("systemctl cat hart-state-persist.service")
          sp.succeed("command -v hart-state-persist")
          # Ordered Before NetworkManager (the Wi-Fi-in-place-before-NM contract).
          assert "NetworkManager.service" in unit, \
              "persist must be ordered Before NetworkManager.service"
          # A bounded oneshot (never an unbounded hang that could wedge boot).
          props = sp.succeed(
              "systemctl show hart-state-persist.service -p Type -p TimeoutStartUSec"
          )
          assert "Type=oneshot" in props, f"must be a oneshot, got: {props!r}"
          assert "TimeoutStartUSec=infinity" not in props, \
              f"must have a bounded start timeout, got: {props!r}"
          # NOTHING requires it -> it can never emit a boot-blocking dependency
          # (Before is ordering only; no unit hard-waits on it).
          rb = sp.succeed("systemctl show hart-state-persist.service -p RequiredBy").strip()
          assert rb == "RequiredBy=", f"no unit may Require the persist oneshot, got: {rb!r}"

      # ── 2. No HARTSTATE label -> clean boot no-op, still reached multi-user ──
      with subtest("no HARTSTATE partition -> clean DECISION=NOOP, nothing bound"):
          st = sp.succeed("cat /run/hart/state-persist.status")
          assert "DECISION=NOOP" in st, \
              f"a boot with no HARTSTATE must record DECISION=NOOP, got: {st!r}"
          # Nothing was bind-persisted (the OS stays stateless, exactly as today).
          sp.fail("mountpoint -q /etc/NetworkManager/system-connections")
          # The box booted fully despite the Before ordering (proof it never blocked).
          sp.succeed("systemctl is-active multi-user.target")

      # Resolve the spare stand-in disk (the HARTSTATE partition device).
      disk = sp.succeed(
          "for d in /dev/vdb /dev/sdb; do [ -b \"$d\" ] && echo \"$d\" && break; done"
      ).strip()
      assert disk, "no spare disk surfaced"

      # ── 3. FAIL-SECURE: a non-POSIX (vfat) HARTSTATE skips the Wi-Fi bind ──
      # FAT cannot store 0600 root:root, so persisting NM secrets there would leave
      # them world-readable. The module must SKIP the Wi-Fi bind (fail-secure) while
      # still persisting the (non-secret) HART state. Run this BEFORE the ext4 case
      # so no prior bind is in the way; clean up with lazy umounts after.
      with subtest("a vfat HARTSTATE fail-secure skips the wifi bind, state still persists"):
          sp.succeed(f"mkfs.vfat -n HARTSTATE {disk}")
          sp.succeed("udevadm settle || true")
          # The module REFORMATS an EMPTY vfat to ext4 by design (the
          # Windows-flash path: mkfs.ext4 is impossible from Windows, so an
          # empty vfat HARTSTATE is upgraded in place) — and then the wifi
          # bind proceeds legitimately on the resulting POSIX fs, which is
          # exactly how this subtest failed against a CORRECT module
          # (run 30485906966: mountpoint unexpectedly succeeded). Seed a
          # file so the fs is NON-empty, the reformat correctly refuses,
          # and the true fail-secure skip is what gets exercised.
          sp.succeed(f"mkdir -p /tmp/vfseed && mount {disk} /tmp/vfseed "
                     "&& touch /tmp/vfseed/existing-user-data.txt "
                     "&& umount /tmp/vfseed")
          sp.succeed("udevadm settle || true")
          out = sp.succeed("hart-state-persist 2>&1; echo RC=$?")
          assert "RC=0" in out, f"vfat persist must exit 0, got: {out!r}"
          # HART state DID persist (bind works on any fs; only perms differ).
          sp.succeed("mountpoint -q /var/lib/hart")
          # The Wi-Fi bind was FAIL-SECURE skipped (never world-readable secrets).
          sp.fail("mountpoint -q /etc/NetworkManager/system-connections")
          st_v = sp.succeed("cat /run/hart/state-persist.status")
          assert "SKIP wifi persist" in st_v, \
              f"vfat must fail-secure skip the wifi bind, got: {st_v!r}"
          assert "DECISION=PARTIAL" in st_v, \
              f"a skipped wifi bind must record DECISION=PARTIAL, got: {st_v!r}"
          # Tear down the vfat binds (lazy: never fails even if a path is busy) and
          # wipe the label so the ext4 case starts clean.
          for p in ["/etc/NetworkManager/system-connections", "/var/lib/hart",
                    "/home/hart-admin", "/run/hart/hartstate"]:
              sp.succeed(f"umount -l {p} 2>/dev/null || true")
          # Best-effort signature wipe (a just-lazy-unmounted device can briefly
          # report busy); the ext4 mkfs -F below overwrites regardless.
          sp.succeed(f"wipefs -a {disk} 2>/dev/null || true")
          # ...and make the kernel FORGET the partition nodes, not just the
          # on-disk signatures. mkfs.vfat on a bare whole disk leaves a FAT boot
          # sector whose BPB + 0x55AA tail the partition scanner misreads as an
          # MBR, so the kernel invents a phantom `vdb1` (visible in the VM log as
          # "vdb: vdb1" right after the mkfs.vfat). wipefs does not remove that
          # node. The next subtest then mkfs.ext4's the WHOLE disk, blkid
          # resolves LABEL=HARTSTATE to the leftover vdb1, and the mount dies on
          #   EXT4-fs (vdb1): bad geometry: block count 262144 exceeds size of
          #   device (262143 blocks)
          # -- one block short, because the phantom starts an offset in. Nothing
          # binds and the subtest fails on mountpoint -q. blockdev is util-linux,
          # already in systemPackages above.
          sp.succeed(f"blockdev --rereadpt {disk} 2>/dev/null || true")
          sp.succeed("udevadm settle || true")

      # ── 3b. An EMPTY vfat HARTSTATE is upgraded to ext4 (the Windows-flash path) ──
      # The reformat feature the old subtest 3 tripped over, covered on purpose:
      # a Windows flash can only lay down vfat, so an EMPTY vfat HARTSTATE is
      # mkfs.ext4'd in place and the wifi bind then proceeds on the POSIX fs.
      with subtest("an EMPTY vfat HARTSTATE is upgraded to ext4 and wifi persists securely"):
          sp.succeed(f"mkfs.vfat -n HARTSTATE {disk}")
          sp.succeed("udevadm settle || true")
          out_e = sp.succeed("hart-state-persist 2>&1; echo RC=$?")
          assert "RC=0" in out_e, f"empty-vfat upgrade must exit 0, got: {out_e!r}"
          fstype = sp.succeed(f"blkid -o value -s TYPE {disk}").strip()
          assert fstype == "ext4", \
              f"empty vfat HARTSTATE must be upgraded to ext4 (Windows-flash path), got {fstype!r}"
          # SAY WHY WHEN THIS FAILS. `out_e` already holds the script's own
          # narration — every persist_dir failure logs a reason ("bind of X
          # failed", "cannot create backing") and marks the run PARTIAL — and
          # this subtest was capturing it and then asserting only on RC, so a
          # bind failure surfaced as a bare "command `mountpoint -q ...` failed
          # (exit code 32)" with the explanation sitting unused in a local
          # variable (run 30848154453).
          #
          # 32 is util-linux's MNT_EX_FAIL, i.e. "not a mountpoint" — it does
          # NOT distinguish a missing directory from an unbound one, which is
          # the other half of why that message said nothing.
          if sp.execute("mountpoint -q /etc/NetworkManager/system-connections")[0] != 0:
              raise AssertionError(
                  "the wifi dir was not bind-persisted after the ext4 upgrade.\n"
                  "--- hart-state-persist said ---\n" + out_e
                  + "\n--- /run/hart/state-persist.status ---\n"
                  + sp.succeed("cat /run/hart/state-persist.status 2>/dev/null || true")
                  + "\n--- mounts under /etc + the backing store ---\n"
                  + sp.succeed("findmnt -n | grep -iE 'NetworkManager|hart' || true")
                  + "\n--- does the live dir even exist? ---\n"
                  + sp.succeed("ls -ld /etc/NetworkManager/system-connections 2>&1 || true"))
          perms = sp.succeed("stat -c '%a %U' /etc/NetworkManager/system-connections").strip()
          assert perms == "700 root", \
              f"upgraded wifi persist must be 0700 root, got {perms!r}"
          for p in ["/etc/NetworkManager/system-connections", "/var/lib/hart",
                    "/home/hart-admin", "/run/hart/hartstate"]:
              sp.succeed(f"umount -l {p} 2>/dev/null || true")
          sp.succeed(f"wipefs -a {disk} 2>/dev/null || true")
          # ...and make the kernel FORGET the partition nodes, not just the
          # on-disk signatures. mkfs.vfat on a bare whole disk leaves a FAT boot
          # sector whose BPB + 0x55AA tail the partition scanner misreads as an
          # MBR, so the kernel invents a phantom `vdb1` (visible in the VM log as
          # "vdb: vdb1" right after the mkfs.vfat). wipefs does not remove that
          # node. The next subtest then mkfs.ext4's the WHOLE disk, blkid
          # resolves LABEL=HARTSTATE to the leftover vdb1, and the mount dies on
          #   EXT4-fs (vdb1): bad geometry: block count 262144 exceeds size of
          #   device (262143 blocks)
          # -- one block short, because the phantom starts an offset in. Nothing
          # binds and the subtest fails on mountpoint -q. blockdev is util-linux,
          # already in systemPackages above.
          sp.succeed(f"blockdev --rereadpt {disk} 2>/dev/null || true")
          sp.succeed("udevadm settle || true")

      # ── 4. Positive: a real ext4 HARTSTATE bind-persists, SECURELY ──
      with subtest("an ext4 HARTSTATE persists wifi/state/home with secure 0700 root:root wifi"):
          sp.succeed(f"mkfs.ext4 -F -L HARTSTATE {disk}")
          sp.succeed("udevadm settle || true")
          out2 = sp.succeed("hart-state-persist 2>&1; echo RC=$?")
          assert "RC=0" in out2, f"ext4 persist must exit 0, got: {out2!r}"
          # All three stateful paths are now bind mounts (survive reboot).
          sp.succeed("mountpoint -q /etc/NetworkManager/system-connections")
          sp.succeed("mountpoint -q /var/lib/hart")
          sp.succeed("mountpoint -q /home/hart-admin")
          st2 = sp.succeed("cat /run/hart/state-persist.status")
          assert "DECISION=PERSISTED" in st2, \
              f"ext4 persist must record DECISION=PERSISTED, got: {st2!r}"
          # SECURE: the Wi-Fi dir is 0700 root:root (secrets not world-readable).
          perm = sp.succeed("stat -c '%a %U %G' /etc/NetworkManager/system-connections").strip()
          assert perm == "700 root root", \
              f"the wifi dir must be 0700 root:root (secure), got: {perm!r}"
          # Data written to the Wi-Fi path LANDS on the actual HARTSTATE partition
          # (the persistence proof short of a real reboot).
          sp.succeed(
              "printf '[connection]\\nid=hart-test\\n' "
              "> /etc/NetworkManager/system-connections/hart-test.nmconnection"
          )
          sp.succeed("sync")
          sp.succeed(
              "grep -q 'id=hart-test' "
              "/run/hart/hartstate/NetworkManager/system-connections/hart-test.nmconnection"
          )
    '';
  };
}

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
              "INPUT / SEAT / POINTER (#134)",
              # network-wifi degrade dimension: the lspci/rfkill summary that tells
              # "wifi chip not enumerated" apart from "soft-blocked" offline.
              "NETWORK / WiFi / rfkill",
              # boot-root-initrd dimension: root mounted + from where + the root=
              # cmdline + the duplicate-LABEL=HART_OS race count.
              "root / boot device + kernel cmdline (boot-root-initrd)",
              # audio degrade dimension: the per-user wpctl/pactl + kernel
              # /proc/asound probe that tells "sink muted / at volume 0 on boot"
              # (the steward's "no audio out") apart from "no sound card" offline.
              "AUDIO / SINK / VOLUME",
              "FULL current-boot journal",
          ]:
              assert needle in latest, f"bundle missing section: {needle!r}"

          # ── ROOT-MOUNT SUCCESS probe (boot-root-initrd real-HW diagnostic) ──
          # The dimension's real-HW probe: the bundle must record that root actually
          # MOUNTED (so a field reader knows the USB root came up), the kernel root=
          # param (root= mismatch debug), and the duplicate-LABEL=HART_OS race count
          # (the "boots once, panics next boot" failure mode) — the surface needed to
          # debug a "VFS: Unable to mount root fs" from the Windows host. Behavioural:
          # we read what the REAL capture wrote, not the source.
          assert "root-mount: SUCCESS" in latest, \
              "bundle must record root-mount SUCCESS (findmnt / resolved a source)"
          assert "root=" in latest, \
              "bundle must record the kernel root= cmdline param (root= mismatch debug)"
          assert "devices with LABEL=HART_OS" in latest, \
              "bundle must record the duplicate-LABEL=HART_OS race count"

          # ── NETWORK / WiFi / rfkill probe (network-wifi real-HW diagnostic) ──
          # The dimension's real-HW probe: the bundle must carry all the network
          # diagnostic layers (lspci + rfkill + ip link) so "Wi-Fi hardware not
          # detected" can be told apart from a soft/hard rfkill block offline. The
          # tooling (pciutils/iproute2/rfkill) must be in the closure — these run
          # in the headless VM (no real wifi) and still land their labelled blocks.
          for net_needle in [
              "lspci (network class)",
              "rfkill (",
              "ip link (",
              "kernel wifi firmware/driver lines",
          ]:
              assert net_needle in latest, \
                  f"network section missing probe: {net_needle!r}"

          # ── INPUT / SEAT / POINTER probe (#134 real-HW diagnostic) ──
          # The dimension's real-HW probe: the bundle must carry all FOUR input
          # diagnostic layers so a "pointer frozen at 0,0 / nothing types / taps
          # don't register" boot can be localised from the Windows host. This is
          # behavioural: we read what the REAL capture wrote, not the source.
          for needle in [
              # (1) the #134 input-alive beacon report — it must state PRESENT or
              #     ABSENT, never be silently missing (in this headless VM no real
              #     seat event fires, so ABSENT is the expected, correct reading).
              "input-alive beacon",
              # (2) the libinput enumeration subsection.
              "libinput list-devices",
              # (3) the always-present kernel evdev table (no extra package needed).
              "kernel evdev table (/proc/bus/input/devices)",
              # (4) the /dev/input node permission listing + the seat assignment.
              "/dev/input node permissions",
              "loginctl seat-status seat0",
          ]:
              assert needle in latest, f"input/seat/pointer bundle missing: {needle!r}"
          # The kernel evdev table is ALWAYS populated (the VM has a virtio
          # keyboard); prove the probe captured a real device line, not an empty
          # placeholder — i.e. the enumeration actually ran and produced data.
          assert ("input-alive beacon ABSENT" in latest
                  or "PRESENT — the compositor delivered" in latest
                  or "ABSENT — NO pointer/keyboard" in latest), \
              "input-alive beacon section did not report a PRESENT/ABSENT verdict"
          assert ("Name=" in latest or "no /proc/bus/input/devices" in latest), \
              "evdev table section captured no device name lines (probe did not run)"
          # (5) the one-line seat-capability verdict — the offline real-HW answer to
          #     "does the seat expose pointer / keyboard / touch?". Assert the line +
          #     all three verdict tokens are present (the exact yes/no depends on what
          #     THIS VM enumerated, so we assert the FORMAT not the values — robust).
          assert "seat capabilities:" in latest, \
              "input probe missing the one-line seat-capability summary verdict"
          for tok in ("pointer=", "keyboard=", "touch="):
              assert tok in latest, \
                  f"seat-capability summary missing the {tok!r} verdict token"

          # ── AUDIO / SINK / VOLUME probe (audio "no audio out" real-HW diagnostic) ──
          # The dimension's real-HW probe: the bundle must carry every audio
          # diagnostic layer so the steward's "sink EXISTS but is MUTED / at
          # volume 0 on boot -> no audio out" can be told apart from "no sound card
          # at all" from the Windows host, offline. The per-user `wpctl status` /
          # `wpctl get-volume @DEFAULT_AUDIO_SINK@` (run as each session user, since
          # a root unit cannot see the user's PipeWire socket) report the default
          # sink + its mute/level; the kernel /proc/asound/cards + /dev/snd answer
          # "is there audio HW at all". Behavioural: we read what the REAL capture
          # wrote, not the source. (A real PipeWire + a muted null sink getting
          # rescued is covered behaviourally by tests/audio.nix; here we prove the
          # probe is WIRED into the on-stick bundle.)
          for audio_needle in [
              "kernel sound cards (/proc/asound/cards)",
              "/dev/snd nodes",
              "per-user PipeWire default-sink state (wpctl/pactl as each session user)",
              "hart-audio-unmute rescue decisions",
          ]:
              assert audio_needle in latest, \
                  f"audio section missing probe: {audio_needle!r}"
          # The per-user wpctl/pactl probe must have produced a verdict - either a
          # real session block, or the honest "no graphical session" note (the
          # expected reading in this headless VM) - proving the probe path ran and
          # was not a silently-empty section.
          assert ("== session" in latest or "no /run/user/* session" in latest), \
              "per-user audio (wpctl/pactl) probe produced no session verdict"
          # The kernel sound-card probe must have produced a verdict - either the
          # honest "no sound card" / "no /dev/snd" placeholders (the expected
          # reading on this node, which configures NO sound device, the same way
          # the input-alive beacon reads ABSENT here) OR a real ALSA control node
          # if a card is present - proving /proc/asound/cards + /dev/snd were
          # actually read, not silently skipped. (Real-or-placeholder, mirroring
          # the evdev "Name=" / "no /proc/bus/input/devices" assertion above.)
          assert ("no /proc/asound/cards" in latest
                  or "no /dev/snd" in latest
                  or "controlC" in latest), \
              "kernel sound-card probe produced no HW verdict (probe did not run)"

          # A per-boot file ALSO landed (history within this boot).
          bl.succeed("ls /tmp/hl/hart-boot-*.log | grep -v latest | grep -q .")
          bl.succeed("umount /tmp/hl")

      # ── 3b. The early phase unmounts the module's private mountpoint cleanly ──
      # The early/shutdown phases must leave NO mount at the private mountpoint (so
      # a Windows host sees a consistent fs) — only the periodic phase keeps it
      # mounted (re-mount-churn avoidance). After the early capture above, the
      # private mountpoint must be free, AND the FAT fs must be fsck-clean (a dirty
      # bit / lost cluster would mean the unmount/sync contract was violated).
      with subtest("the early phase leaves the private mountpoint unmounted + fs clean"):
          bl.fail(f"mountpoint -q {MNT}")
          # fsck.fat -n is read-only; exit 0 == clean. (dosfstools is in the node.)
          fsck = bl.succeed(f"fsck.fat -n {found} 2>&1; echo RC=$?")
          assert "RC=0" in fsck, f"HARTLOG fs not clean after early-phase unmount: {fsck!r}"

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

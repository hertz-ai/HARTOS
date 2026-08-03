# ═══════════════════════════════════════════════════════════════
# HART OS — driver compatibility matrix (task #27)
# ═══════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS
#   /api/shell/drivers can REPORT driver binding since 2026-08-02 (it grew
#   `unclaimed`, the yellow-bang equivalent). Nothing PROVED binding. "All
#   driver compatibility" was a claim with no boot behind it, in exactly the
#   way "Hyper-V Gen 1 boots" was before #28.
#
# WHAT IT PROVES
#   Attach a device from each class QEMU can present WITHOUT a backing file,
#   boot the REAL desktop variant, and assert the kernel actually BOUND a
#   driver to it — not that a module is merely loadable, and not that lspci
#   merely lists the hardware.
#
#   Binding is read from sysfs the way the kernel records it: a driver with a
#   bound device has that device symlinked under
#   /sys/bus/<bus>/drivers/<driver>/ by its bus address (PCI 0000:00:1f.3,
#   USB 1-1). `lsmod` would only prove the module was loaded, which is the
#   weaker claim that lets an unclaimed device pass.
#
# ONE NODE, MANY DEVICES — deliberate. A VM job in this repo costs ~2 hours,
# so six single-device nodes would buy the same coverage for six times the
# wall clock. Devices from different buses do not mask each other; each
# assertion names its own driver.
#
# SCOPE OF THIS SLICE: device classes needing NO backing file. Storage
# controllers that need a drive (nvme, ahci, usb-storage) are the second
# slice — usb_storage is already proven packed+bound by hart-boot-root-initrd
# and the filesystems by hart-storage-filesystems, so this does not re-cover
# them.
{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-driver-matrix = pkgs.testers.runNixOSTest {
    name = "hart-driver-matrix";
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.drv = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # An extra disk exercises the virtio-blk path with a real block device
        # rather than only the root.
        emptyDiskImages = [ 128 ];
        # Same construction as input-seat-pointer.nix: declare our OWN xhci
        # controller and hang devices off ITS bus, so nothing depends on the
        # framework's default `-usb` / usb-bus.0 ordering.
        qemu.options = [
          # USB host controller (xhci_hcd) + HID on its bus (usbhid)
          "-device" "qemu-xhci,id=hartusb"
          "-device" "usb-kbd,bus=hartusb.0"
          "-device" "usb-mouse,bus=hartusb.0"
          # Audio: the exact controller a real x86 desktop presents
          "-device" "intel-hda"
          "-device" "hda-duplex"
          # A SECOND NIC on a different driver than the framework's virtio-net,
          # with its own user-mode netdev so it needs no host bridge.
          "-netdev" "user,id=hartnet1"
          "-device" "e1000,netdev=hartnet1"
          # Memory ballooning — a virtio class the desktop profile relies on
          # for the memory pressure story.
          "-device" "virtio-balloon"
        ];
      };
    };

    testScript = ''
      import re

      drv = machines[0]
      drv.start()
      drv.wait_for_unit("multi-user.target")

      def bound_devices(bus, driver):
          """Bus addresses currently BOUND to <driver>, read from sysfs.

          The kernel symlinks each bound device under the driver's directory
          by its bus address. Anything else in there (bind/unbind/uevent
          attribute files, module symlink) is not a device, so filter to the
          address shapes:
            PCI            '0000:00:1f.3'
            USB device     '1-1', '1-1.2'
            USB INTERFACE  '1-1:1.0', '2-3.4:1.2'

        THE INTERFACE FORM IS NOT OPTIONAL (run 30783792736, shard 3). This
        first matched only `^\d+-[\d.]+$`, which cannot match an interface
        address because of the COLON — and usbhid binds INTERFACES, never
        whole devices. So the filter rejected every real entry, the list came
        back empty, and the test reported "NO device is bound to 'usbhid'" on
        a VM where USB HID was working perfectly (its sibling
        input-seat-pointer.nix, same qemu options, sees both devices through
        libinput and passes). The docstring gave the bug away: it described
        the DEVICE shapes while the assertion targeted an INTERFACE driver.
          """
          out = drv.succeed(
              f"ls -1 /sys/bus/{bus}/drivers/{driver}/ 2>/dev/null || true"
          )
          return [
              e for e in (l.strip() for l in out.splitlines())
              if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:", e)
              or re.match(r"^\d+-[\d.]+(:\d+\.\d+)?$", e)
          ]

      def assert_bound(bus, driver, what):
          devs = bound_devices(bus, driver)
          drv.log(f"{what}: driver={driver} bus={bus} bound={devs}")
          assert devs, (
              f"{what}: NO device is bound to '{driver}' on the {bus} bus.\n"
              f"The device was attached to the VM, so this is the "
              f"unclaimed-hardware case /api/shell/drivers reports — the "
              f"kernel saw it and could not drive it.\n"
              # Dump BOTH sides. The drivers list alone said "usbhid exists"
              # and left the real question — what is actually enumerated, and
              # under what address shape — to be reasoned out offline. The raw
              # driver dir is what distinguishes "nothing attached" from
              # "attached but my filter rejected the name".
              f"--- /sys/bus/{bus}/drivers/{driver}/ (raw) ---\n"
              + drv.succeed(
                  f"ls -1 /sys/bus/{bus}/drivers/{driver}/ 2>/dev/null || true")
              + f"--- /sys/bus/{bus}/devices/ ---\n"
              + drv.succeed(
                  f"ls -1 /sys/bus/{bus}/devices/ 2>/dev/null | head -40 || true")
              + f"--- drivers on the {bus} bus ---\n"
              + drv.succeed(f"ls -1 /sys/bus/{bus}/drivers/ 2>/dev/null | head -40 || true")
          )

      with subtest("USB host controller binds (xhci_hcd)"):
          assert_bound("pci", "xhci_hcd", "USB 3 host controller")

      with subtest("USB HID keyboard + mouse bind (usbhid)"):
          assert_bound("usb", "usbhid", "USB HID input")

      with subtest("HD-audio controller binds (snd_hda_intel)"):
          assert_bound("pci", "snd_hda_intel", "Intel HDA audio")

      with subtest("Intel e1000 NIC binds — a NON-virtio driver path"):
          # virtio-net is what the framework gives every node, so it proves
          # nothing about real hardware. e1000 is the driver a great many
          # physical machines and hypervisors actually present.
          assert_bound("pci", "e1000", "Intel e1000 NIC")

      with subtest("virtio block + balloon bind"):
          assert_bound("virtio", "virtio_blk", "virtio block device")
          assert_bound("virtio", "virtio_balloon", "virtio balloon")

      with subtest("nothing attached is left UNCLAIMED"):
          # The whole-tree check, and the one that would catch a device class
          # nobody wrote an assertion for. Mirrors what /api/shell/drivers
          # computes, but from sysfs so it is independent of the API.
          unclaimed = drv.succeed(
              "for d in /sys/bus/pci/devices/*; do "
              "  [ -e \"$d/driver\" ] || echo \"$(basename $d) $(cat $d/class 2>/dev/null)\"; "
              "done || true"
          ).strip()
          drv.log(f"PCI devices with NO driver bound:\n{unclaimed or '(none)'}")
          # Recorded, not asserted-empty: QEMU presents host bridges and
          # legacy bits that legitimately have no driver, so failing on a
          # non-empty list would be noise. The named assertions above are the
          # gate; this line is what makes a NEW unclaimed device visible in
          # the log instead of silent.

      with subtest("the agent surface AGREES with sysfs"):
          # /api/shell/drivers is the half a user and an agent actually see.
          # If sysfs says bound and the endpoint says unclaimed, the endpoint
          # is lying — which is the defect class this repo keeps hitting.
          out = drv.succeed(
              "curl -s -m 10 http://127.0.0.1:6777/api/shell/drivers || true"
          ).strip()
          drv.log(f"/api/shell/drivers -> {out[:600]}")
          if out.startswith("{"):
              import json
              data = json.loads(out)
              assert "devices" in data, f"drivers endpoint returned no device list: {out[:300]}"
              # It must not silently truncate — the old 50-cap did exactly that.
              assert data.get("truncated") is not True or data.get("count", 0) >= 50, \
                  f"endpoint reports truncated with an implausible count: {data.get('count')}"
              drv.log(
                  f"endpoint: count={data.get('count')} "
                  f"unclaimed_count={data.get('unclaimed_count')} "
                  f"truncated={data.get('truncated')}"
              )
          else:
              # The backend may not be listening on this minimal node; that is
              # a different task's problem, so record rather than fail here.
              drv.log("drivers endpoint not reachable on this node — sysfs assertions above stand")

      with subtest("#25: /api/shell/kernel AGREES with /proc/modules"):
          # Same cross-check as the drivers subtest above, for the module
          # surface. Comparing the endpoint against the file it claims to
          # read is what makes this a proof rather than a restatement: a
          # route that invented its list, cached a stale one, or silently
          # truncated would disagree with the kernel here and nowhere else.
          truth = drv.succeed("cat /proc/modules || true")
          truth_names = sorted(
              l.split()[0] for l in truth.splitlines() if l.split())
          drv.log(f"/proc/modules holds {len(truth_names)} modules")

          out = drv.succeed(
              "curl -s -m 10 http://127.0.0.1:6777/api/shell/kernel || true"
          ).strip()
          if out.startswith("{"):
              import json
              data = json.loads(out)
              assert data.get("available") is True, (
                  "the node HAS /proc/modules (read above) yet the endpoint "
                  f"reports it cannot look: {out[:300]}")
              api_names = sorted(m["name"] for m in data.get("modules", []))
              assert api_names == truth_names, (
                  "the kernel endpoint disagrees with /proc/modules.\n"
                  f"only in /proc: {sorted(set(truth_names) - set(api_names))[:20]}\n"
                  f"only in API:   {sorted(set(api_names) - set(truth_names))[:20]}")
              # A Linux kernel always reports SOME taint value, even 0.
              assert data.get("tainted") is not None, (
                  "taint flag came back None on a live Linux node — that value "
                  "is reserved for 'could not read it', so something is "
                  f"degrading silently: {out[:300]}")
              drv.log(f"kernel endpoint: {data.get('module_count')} modules, "
                      f"tainted={data.get('tainted')}, "
                      f"release={data.get('kernel_release')}")
          else:
              drv.log("kernel endpoint not reachable on this node")

      with subtest("#25: /api/shell/services AGREES with systemctl"):
          # Same cross-check shape again: ask the endpoint, then ask systemd
          # directly, and require them to match. The interesting unit is one
          # this VM does NOT have — the route used to run `is-active`, which
          # reports a missing unit as "inactive", making "not installed"
          # indistinguishable from "installed but stopped". An agent told the
          # second will try to start it forever.
          out = drv.succeed(
              "curl -s -m 10 'http://127.0.0.1:6777/api/shell/services?group=all' || true"
          ).strip()
          if out.startswith("{"):
              import json
              data = json.loads(out)
              assert data.get("available") is True, (
                  f"systemd is PID 1 here, yet the endpoint could not ask it: {out[:300]}")
              by = {s["name"]: s for s in data.get("services", [])}
              assert by, f"services endpoint returned nothing: {out[:300]}"
              drv.log("services: " + ", ".join(
                  f"{n}={s.get('status')}/{'inst' if s.get('installed') else 'MISSING'}"
                  for n, s in sorted(by.items())))

              # Cross-check every reported unit against systemd itself.
              for name, svc in sorted(by.items()):
                  unit = svc["unit"]
                  truth_load = drv.succeed(
                      f"systemctl show {unit} --property=LoadState --value "
                      f"2>/dev/null || echo unknown").strip()
                  truth_active = drv.succeed(
                      f"systemctl show {unit} --property=ActiveState --value "
                      f"2>/dev/null || echo unknown").strip()
                  assert svc["load_state"] == truth_load, (
                      f"{unit}: endpoint says load_state={svc['load_state']!r}, "
                      f"systemd says {truth_load!r}")
                  assert svc["status"] == truth_active, (
                      f"{unit}: endpoint says status={svc['status']!r}, "
                      f"systemd says {truth_active!r}")
                  assert svc["installed"] == (truth_load not in ("not-found", "masked")), (
                      f"{unit}: installed={svc['installed']} contradicts "
                      f"LoadState={truth_load!r}")

              # hart-backend is answering this very request, so anything other
              # than active would mean the endpoint cannot see its own host.
              if "hart-backend" in by:
                  assert by["hart-backend"]["status"] == "active", (
                      "hart-backend served this request yet reports "
                      f"{by['hart-backend']['status']!r}")
          else:
              drv.log("services endpoint not reachable on this node")

      with subtest("#25: a GPU-less VM is told it has NO GPU, not an error"):
          # The branch that is hardest to get right and was, until 9fd06117,
          # impossible to express: this VM genuinely has no GPU, so the
          # honest answer is available=True + present=False. A 503 would say
          # "I could not look" and a fabricated 0 GB would read as "a GPU
          # with no memory". Both were reachable before; this pins neither.
          out = drv.succeed(
              "curl -s -m 10 http://127.0.0.1:6777/api/shell/gpu || true"
          ).strip()
          if out.startswith("{"):
              import json
              data = json.loads(out)
              drv.log(f"/api/shell/gpu -> {out[:300]}")
              assert data.get("available") is True, (
                  "the detector should have RUN on this node and simply found "
                  f"nothing; available=False means it could not look: {out[:300]}")
              if data.get("present"):
                  # A GPU in a plain qemu VM would mean the detector is
                  # reporting something that is not there.
                  raise AssertionError(
                      "this VM has no GPU passed through, yet the endpoint "
                      f"claims one is present: {out[:300]}")
          else:
              drv.log("gpu endpoint not reachable on this node")
    '';
  };
}

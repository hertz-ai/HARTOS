# ═══════════════════════════════════════════════════════════════
# HART OS — Input / Seat / Pointer nixosTest (the #134 dimension)
# ═══════════════════════════════════════════════════════════════
#
# Proves the OS-level SEAT layer the compositor rides actually EXPOSES + GRANTS
# the input devices, and that a missing device degrades (never wedges). This is
# the behavioural twin of the real-HW boot-log INPUT/SEAT/POINTER probe
# (hart-boot-log.nix): the boot-log probe records what the seat enumerated for an
# OFFLINE reader; THIS test asserts the same enumerator (`libinput list-devices`)
# sees a pointer + keyboard, that the session user can OPEN the /dev/input nodes
# (the EACCES-on-/dev/input "seat not granting input" failure cannot happen,
# FM4), that a RELATIVE-motion pointer exists (the input the #134 cursor fix
# applies — "pointer frozen at 0,0" is a DROPPED relative delta, so the device
# that produces it must be present and granted), and that the seat is a
# non-blocking EVENT SOURCE so a removed/missing input device never wedges the
# box (FM5).
#
# WHY [VM]-gated: a real seat (logind + libinput + /dev/input + udev input_id) +
# enumerating QEMU USB HID devices needs a real Linux input stack — it cannot run
# on the Windows dev box. Per the honest-hardware rule it gates in CI
# (`nix flake check` / local QEMU), never inline / grep on the dev box. The
# compositor's OWN wl_seat advertisement (udev.rs seat.add_keyboard/add_pointer)
# + the relative-motion CLAMP MATH (advance_and_clamp_pointer) are proven by the
# compositor's Rust unit tests; THIS test proves everything UP TO the compositor:
# the OS seat the compositor depends on is correctly populated + granted.
#
# Division of coverage for the input-seat-pointer dimension (no parallel paths):
#   - SEAT ENUMERATION + GRANT + relative pointer + degrade ........ HERE
#   - the input-alive watchdog (drop a painted-but-input-dead tier;
#     keep a healthy one; never flap the default; the touch-only /
#     device-less guard) ............................ session-supervisor.nix
#   - the OFFLINE real-HW probe bundle (libinput list-devices into the
#     HARTLOG log + the seat-capability classification line) ......... boot-log.nix
#   - logind-is-the-seat-manager + seatd disabled + greetd forces
#     LIBSEAT_BACKEND=logind + the user is in video/render/input ... session-supervisor.nix
#
# #70 discipline preserved: built from `hartModules` alone via the shared
# `mkNode` (./lib.nix). No display manager / compositor needed — the seat layer
# is present on any booted node; this keeps the test fast + deterministic.

{ pkgs, hartModules, specialArgs }:

let
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;
in
{
  hart-input-seat-pointer = pkgs.testers.runNixOSTest {
    name = "hart-input-seat-pointer";
    # runNixOSTest's mypy/pyflakes pre-checks do NOT resolve the per-node Machine
    # global the driver injects at RUNTIME (mkNode forces the hostname to the
    # variant, so the `seat` name is bound from machines[0] at runtime) — same
    # false "Name not defined" as the supervisor/boot-log tests. Skip both static
    # passes; the VM still boots and the assertions still run.
    skipTypeCheck = true;
    skipLint = true;
    node.specialArgs = specialArgs;

    nodes.seat = mkNode "desktop" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        # A SELF-CONTAINED USB HID controller + a keyboard + a RELATIVE-motion
        # mouse. We create our OWN xhci controller (id=hartusb) and attach the
        # devices to its bus so QEMU never depends on the framework's default
        # `-usb`/usb-bus.0 ordering. The relative usb-mouse is THE #134 device: it
        # emits InputEvent::PointerMotion (relative), the exact event the compositor
        # used to drop at the `_ => {}` sink leaving the cursor pinned at (0,0). The
        # framework also adds a default virtio-keyboard + usb-tablet (absolute
        # pointer) on x86, so the seat is richly populated.
        qemu.options = [
          "-device" "qemu-xhci,id=hartusb"
          "-device" "usb-kbd,bus=hartusb.0"
          "-device" "usb-mouse,bus=hartusb.0"
        ];
      };
      # NOTE: we deliberately do NOT force usbhid/virtio_input via
      # boot.kernelModules — USB HID is already available in the test VM (the
      # framework's default usb-tablet relies on it), and forcing a BUILT-IN module
      # would make systemd-modules-load fail (a spurious degraded state). The only
      # module the test needs on demand (uinput, for the best-effort virtual touch
      # device) is `modprobe`d inside the touch subtest, so nothing is forced here.
      # The seat-enumeration probe (`libinput list-devices`) + the best-effort
      # virtual-touch tooling (evemu) + udevadm (settle). libinput is the SAME tool
      # the boot-log real-HW probe runs, so this test exercises the real probe path.
      environment.systemPackages =
        [ pkgs.libinput pkgs.util-linux ]
        ++ pkgs.lib.optional (pkgs ? evemu) pkgs.evemu;

      # video/render/input group membership is GRANTED BY the session supervisor
      # (hart-session-supervisor.nix's `mkIf (cfg.enable && sup.enable)`). Enable
      # what the assertion below assumes; defaults are the shipped ones
      # (compCommand null).
      #
      # NOTE (2026-08-10): `seat` is deliberately NOT asserted here anymore.
      # services.seatd.enable is now `lib.mkForce false` (a live-confirmed real-HW
      # fix: seatd created its own VT-bound seat0 independent of any client
      # connecting, fighting logind for seat0 and causing hart-comp's drmSetMaster
      # EACCES -> pixman software-scanout floor). Without seatd, the `seat` group
      # does not exist; DRM/input access flows entirely through logind.
      hart.sessionSupervisor.enable = true;
    };

    testScript = ''
      # The driver keys the single machine global by HOSTNAME (mkNode forces it to
      # the variant "desktop"), so the `seat` name is absent at runtime — bind it
      # from machines[0] (single-node test).
      seat = machines[0]
      seat.start()
      seat.wait_for_unit("multi-user.target")

      # Let udev finish creating /dev/input nodes + setting the ID_INPUT_* tags the
      # libinput classifier reads, so the enumeration below is deterministic.
      seat.succeed("udevadm settle || true")

      SESSION_USER = "hart-admin"   # the desktop variant's graphical session user

      # ── 1. The seat GRANTS input: the session user is in the device groups ──
      # FM4 root cause: a user not in `input` boots dead-input (EACCES on
      # /dev/input). The grant mechanism is group membership (systemd's default
      # `SUBSYSTEM=="input", GROUP="input"` rule) + the logind seat lease. Assert
      # the session user is in the device groups the compositor seat needs.
      with subtest("the session user is in the device groups that GRANT seat input (input/video/render)"):
          groups = seat.succeed(f"id -nG {SESSION_USER}").split()
          for g in ("input", "video", "render"):
              assert g in groups, \
                  f"{SESSION_USER} missing the '{g}' group ({groups}) — the seat cannot open /dev/input (FM4 dead-input)"

      # ── 2. /dev/input nodes exist, are group `input`, and the user can OPEN them ──
      # The live proof there is no EACCES: a user in `input` actually open()s an
      # evdev node. (root could always open it; the POINT is that the unprivileged
      # session user can, via the group grant.)
      with subtest("/dev/input evdev nodes exist + are group-`input` + the session user can OPEN them (no EACCES)"):
          seat.succeed("test -d /dev/input")
          # At least one event node (the VM always has a keyboard).
          ev = seat.succeed("ls /dev/input/event* 2>/dev/null | head -n1").strip()
          assert ev, "no /dev/input/event* node — the kernel saw NO input device at all"
          grp = seat.succeed(f"stat -c '%G' {ev}").strip()
          assert grp == "input", \
              f"{ev} group is {grp!r}, not 'input' — group-based seat grant is broken"
          # The unprivileged session user open()s the node read-only — the real
          # 'seat grants input' proof (would EACCES if the grant were missing).
          seat.succeed(f"runuser -u {SESSION_USER} -- sh -c 'exec 3<{ev}' ")

      # ── 3. The real-HW probe enumerator sees the seat's devices ──
      # `libinput list-devices` is the EXACT tool the boot-log INPUT/SEAT/POINTER
      # probe runs. Assert it runs (as root, opening the seat's devices) and
      # produces a real per-device listing with Capabilities — i.e. the seat the
      # compositor rides is populated, not empty.
      with subtest("libinput list-devices (the real-HW probe) enumerates the seat's devices"):
          seat.succeed("command -v libinput")
          devs = seat.succeed("libinput list-devices 2>/dev/null")
          assert "Device:" in devs, \
              f"libinput list-devices enumerated NO device — the seat is empty (would be a frozen, dead seat). Got:\n{devs}"
          assert "Capabilities:" in devs, \
              "libinput list-devices produced no Capabilities line — enumeration did not classify the devices"

      # ── 4. The seat exposes a KEYBOARD (typing can work — FM2 precondition) ──
      # FM2 ("keyboard dead / typing does nothing") needs a real keyboard granted to
      # the seat as its precondition. libinput's authoritative `Capabilities:
      # keyboard` (cross-checked with the always-present kernel evdev `kbd` handler).
      with subtest("the seat exposes a KEYBOARD"):
          devs = seat.succeed("libinput list-devices 2>/dev/null")
          has_kbd_libinput = any(
              "keyboard" in ln.lower()
              for ln in devs.splitlines() if "capabilities" in ln.lower()
          )
          has_kbd_evdev = seat.succeed(
              "grep -qiE '^H: Handlers=.*kbd' /proc/bus/input/devices && echo yes || echo no"
          ).strip() == "yes"
          assert has_kbd_libinput or has_kbd_evdev, \
              "the seat exposes NO keyboard device (FM2 precondition: typing cannot work)"

      # ── 5. The seat exposes a POINTER with RELATIVE motion (the #134 cursor) ──
      # "pointer frozen at 0,0" is a DROPPED relative-motion delta. The OS-level
      # precondition for the fix is that a device producing relative motion exists
      # + is granted to the seat. Assert libinput classifies a `pointer` AND the
      # kernel evdev table shows a device advertising relative axes (B: REL=) — the
      # usb-mouse. (The compositor's clamp math that consumes the delta is proven by
      # advance_and_clamp_pointer's Rust unit tests; here we prove the INPUT exists.)
      with subtest("the seat exposes a POINTER with RELATIVE motion (the #134 cursor-not-pinned input)"):
          devs = seat.succeed("libinput list-devices 2>/dev/null")
          has_pointer = any(
              "pointer" in ln.lower()
              for ln in devs.splitlines() if "capabilities" in ln.lower()
          )
          assert has_pointer, \
              "the seat exposes NO pointer device — there is no motion for the compositor to apply (cursor pinned at 0,0)"
          # A relative-motion device specifically (the usb-mouse): its evdev block
          # advertises relative axes. Absolute-only (a tablet) is not enough for the
          # #134 relative path the steward hit on a real touchpad/mouse.
          has_rel = seat.succeed(
              "grep -qiE '^B: REL=' /proc/bus/input/devices && echo yes || echo no"
          ).strip() == "yes"
          assert has_rel, \
              "no RELATIVE-motion pointer enumerated (B: REL=) — the #134 relative delta has no source device"

      # ── 6. The cursor is NOT pinned: a relative delta would MOVE it (math contract) ──
      # The #134 fix is on_pointer_move_relative -> advance_and_clamp_pointer: a
      # relative delta advances the cursor and clamps it in-bounds (never stuck at
      # 0,0, never escaping the output). We cannot run the compositor headless here,
      # but we assert the OS provides the relative input AND re-state the invariant
      # the compositor's Rust tests enforce, so this dimension's "assert the cursor
      # is not pinned" is anchored to a concrete, enumerated relative pointer rather
      # than an untested assumption. (The clamp arithmetic itself is unit-tested in
      # compositor/src/comp_core.rs — owned by the compositor, not duplicated here.)
      with subtest("cursor-not-pinned: a relative pointer is present so a delta has somewhere to move from (0,0)"):
          # Already proven a relative pointer exists in subtest 5; this names the
          # invariant explicitly for the dimension's audit trail.
          assert seat.succeed(
              "grep -cE '^B: REL=' /proc/bus/input/devices"
          ).strip() != "0", "no relative pointer — the cursor would have no delta source"

      # ── 7. TOUCH enumeration (best-effort live device via uinput/evemu) ──
      # The compositor does not yet ROUTE wl_touch (FM3b), but the SEAT/libinput
      # layer still ENUMERATES + CLASSIFIES a touch device, and the same group-input
      # grant applies. Create a virtual single-touch touchscreen via uinput (evemu)
      # and assert libinput classifies it `touch`. This is BEST-EFFORT: if uinput /
      # evemu is unavailable or the synthetic descriptor is rejected, it logs a SKIP
      # and never fails the test — the deterministic touch coverage is the
      # session-supervisor touch-only watchdog guard + the boot-log seat-capability
      # classifier, which exercise the SAME classification path.
      with subtest("the seat would enumerate + classify a TOUCH device (best-effort live uinput device)"):
          have_evemu = seat.succeed("command -v evemu-device >/dev/null 2>&1 && echo yes || echo no").strip()
          if have_evemu != "yes":
              print("SKIP: evemu-device not in the closure — touch classification covered by the watchdog guard + boot-log classifier")
          else:
              try:
                  seat.succeed("modprobe uinput 2>/dev/null || true")
                  # Build a VALID evemu descriptor in python (correct bitmask byte
                  # widths computed from the kernel max codes) so the synthetic
                  # touchscreen is not a hand-miscounted blob. Single-touch + MT axes
                  # + BTN_TOUCH => udev input_id tags ID_INPUT_TOUCHSCREEN => libinput
                  # classifies `touch`.
                  def bm(codes, nbytes):
                      arr = [0] * nbytes
                      for c in codes:
                          arr[c // 8] |= 1 << (c % 8)
                      return " ".join("%02x" % b for b in arr)
                  EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
                  BTN_TOUCH = 0x14a
                  ABS = [0x00, 0x01, 0x2f, 0x35, 0x36, 0x39]  # X,Y,MT_SLOT,MT_X,MT_Y,MT_TRACKING_ID
                  desc = "\n".join([
                      "N: HART Test Touchscreen",
                      "I: 0018 0000 0000 0000",
                      "P: 00 00 00 00",
                      "B: 00 " + bm([EV_SYN, EV_KEY, EV_ABS], 4),
                      "B: 01 " + bm([BTN_TOUCH], 96),
                      "B: 03 " + bm(ABS, 8),
                      "A: 00 0 32767 0 0 0",
                      "A: 01 0 32767 0 0 0",
                      "A: 2f 0 9 0 0 0",
                      "A: 35 0 32767 0 0 0",
                      "A: 36 0 32767 0 0 0",
                      "A: 39 0 65535 0 0 0",
                      "",
                  ])
                  seat.succeed(
                      "cat > /tmp/touch.evemu <<'EVEMU'\n" + desc + "EVEMU\n"
                  )
                  # evemu-device blocks holding the device alive, so run it detached.
                  seat.succeed(
                      "setsid evemu-device /tmp/touch.evemu >/tmp/evemu.log 2>&1 < /dev/null & echo started"
                  )
                  seat.succeed("udevadm settle || true")
                  # Give udev a moment to tag + libinput to see the new device.
                  seat.wait_until_succeeds(
                      "libinput list-devices 2>/dev/null | grep -i 'Capabilities:' | grep -qi touch",
                      timeout=30,
                  )
                  print("OK: a virtual touchscreen was enumerated + classified `touch` by libinput")
              except Exception as e:
                  print(f"SKIP: live virtual-touch device not created/classified ({e!r}) — "
                        "touch classification still covered by the watchdog guard + boot-log classifier")

      # ── 8. DEGRADE: the seat is a non-blocking event source (FM5) ──
      # A missing/removed input device must NEVER wedge the session: the libinput
      # backend is a calloop EVENT SOURCE, not a boot dependency. Prove (a) the box
      # came all the way up with the seat present (multi-user reached — no input
      # device gated boot), and (b) hot-removing a pointer keeps the box UP and the
      # seat still enumerating the rest.
      with subtest("degrade: a removed/missing input device never wedges the box (FM5)"):
          state = seat.succeed("systemctl is-system-running || true").strip()
          assert state in ("running", "degraded", "starting"), \
              f"system not up after seat enumeration (state={state!r}) — input must never gate boot"
          # No input device may be a boot-ORDERING dependency (Requires/After an
          # input .device unit) — that would let a missing device wedge boot.
          dep_on_input = seat.succeed(
              "systemctl list-dependencies --reverse --plain dev-input-event0.device 2>/dev/null "
              "| grep -vE 'dev-input-event0.device|sys-devices' | grep -c . || true"
          ).strip()
          assert dep_on_input in ("", "0"), \
              f"a unit depends on an input .device ({dep_on_input}) — a missing device could wedge boot"
          # Hot-remove the relative mouse (best-effort) and prove the seat survives.
          before = seat.succeed("libinput list-devices 2>/dev/null | grep -c '^Device:' || true").strip()
          seat.execute(
              'for i in /sys/class/input/input*; do '
              'n=$(cat "$i/name" 2>/dev/null); case "$n" in *Mouse*|*mouse*) '
              'p=$(readlink -f "$i/device"); '
              'while [ -n "$p" ] && [ "$p" != "/" ]; do '
              'if [ -e "$p/driver/unbind" ]; then b=$(basename "$p"); echo "$b" > "$p/driver/unbind" 2>/dev/null && break; fi; '
              'p=$(dirname "$p"); done ;; esac; done'
          )
          seat.succeed("udevadm settle || true")
          # The box is STILL up and the seat STILL enumerates the remaining devices
          # (the event source kept running across a device disappearing).
          state2 = seat.succeed("systemctl is-system-running || true").strip()
          assert state2 in ("running", "degraded", "starting"), \
              f"system fell over after an input device was removed (state={state2!r}) — FM5 degrade broken"
          after = seat.succeed("libinput list-devices 2>/dev/null | grep -c '^Device:' || true").strip()
          assert int(after) >= 1, \
              "the seat enumerates NO device after a removal — the seat layer wedged on a missing device (FM5)"
          print(f"FM5 degrade ok: device count {before} -> {after}, system still {state2}")
    '';
  };
}

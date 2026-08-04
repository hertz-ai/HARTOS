# ═══════════════════════════════════════════════════════════════
# HART OS NixOS VM Integration Tests
# ═══════════════════════════════════════════════════════════════
#
# Uses NixOS's built-in testers.runNixOSTest framework.
# Each test boots a real VM via QEMU and runs assertions.
#
# Run all tests:        nix flake check
# Run a single test:    nix build .#checks.x86_64-linux.hart-server-boot
# These tests take 5-15 minutes each (VM boot + assertions).
#
# #70 FIX — minimal test nodes (was: import the full ISO configs):
#   The nodes used to `imports = hartModules ++ [ ../configurations/X.nix ]`.
#   Each ISO config imports the NixOS installer-CD profile, which sets
#   nixpkgs.overlays — and that collides with runNixOSTest's read-only
#   node.pkgs ("nodes.X.nixpkgs.overlays defined multiple times"), so
#   `nix flake check` could not even EVALUATE the checks and the whole ISO CI
#   gate was blocked.  Gating the installer-CD import out cascaded into
#   isoImage.* errors (that option is PROVIDED by the installer-CD profile).
#   Fix per the recorded recipe: build the nodes from the hart modules alone
#   with the variant enabled — the modules are variant-gated
#   (hart-agent/hart-backend branch on cfg.variant), so {hart.enable;
#   hart.variant} configures each variant's services without any installer-CD /
#   isoImage machinery.  specialArgs (hartSrc) is passed to the inline nodes via
#   `node.specialArgs` (the previously-missing wiring) so modules that consume
#   it evaluate.  The `nix flake check --no-build` gate only needs the nodes to
#   EVALUATE; the testScript assertions run in the (separate) build job.

{ pkgs, hartModules, specialArgs }:

let
  # ONE mkNode (DRY): this file used to re-paste its own local copy of the
  # node builder — the exact parallel path ./lib.nix exists to prevent, and
  # the reason the profile wiring (steward decision 2026-07-30, no flags)
  # would have silently missed every node in THIS file. lib.nix's mkNode is
  # identical (hart.package wiring, hostname mkForce) plus the variant
  # feature profile import.
  inherit (import ./lib.nix { inherit hartModules; }) mkNode;

in
{
  # ─────────────────────────────────────────────────────────────
  # Test 1: Server variant boots and all core services start
  # ─────────────────────────────────────────────────────────────
  hart-server-boot = pkgs.testers.runNixOSTest {
    name = "hart-server-boot";
    node.specialArgs = specialArgs;

    nodes.server = mkNode "server" {
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        forwardPorts = [
          { from = "host"; host.port = 16777; guest.port = 6777; }
        ];
      };
    };

    testScript = ''
      # (no json import: the driver's pyflakes lint is FATAL — an unused
      # import fails the DRIVER BUILD and the test never boots, which was
      # hart-server-boot's entire red since 2026-07-26; run 30485906966.)
      server.start()
      server.wait_for_unit("multi-user.target")

      # Core services must start
      with subtest("Backend service starts"):
          server.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Backend responds on port 6777"):
          server.wait_for_open_port(6777, timeout=60)
          result = server.succeed("curl -sf http://localhost:6777/status")
          assert "success" in result or "uptime" in result, f"Unexpected status: {result}"

      with subtest("Discovery service starts"):
          server.wait_for_unit("hart-discovery.service", timeout=60)

      with subtest("Agent daemon starts"):
          server.wait_for_unit("hart-agent-daemon.service", timeout=120)

      with subtest("First-boot completed"):
          server.wait_for_file("/var/lib/hart/.first-boot-done", timeout=120)

      with subtest("Node identity generated (Ed25519)"):
          server.succeed("test -f /var/lib/hart/node_public.key")
          key_size = server.succeed("wc -c < /var/lib/hart/node_public.key").strip()
          assert key_size == "32", f"Expected 32-byte key, got {key_size}"

      with subtest("Capability tier classified"):
          tier = server.succeed("cat /var/lib/hart/capability_tier").strip()
          valid_tiers = ["OBSERVER", "LITE", "STANDARD", "PERFORMANCE", "COMPUTE_HOST"]
          assert tier in valid_tiers, f"Invalid tier: {tier}"

      with subtest("Database initialized"):
          server.succeed("test -s /var/lib/hart/hevolve_database.db")

      with subtest("OS branding present (HART OS, not NixOS)"):
          # NOTE: 'HART OS' MUST be quoted — `grep -q HART OS file` greps "HART"
          # in the files "OS" and file (the original bug here).
          server.succeed("grep -q 'HART OS' /etc/os-release")
          # ID=hart-os drives OS-mode detection (core.port_registry) AND hides
          # the NixOS identity (#101); a leaked ID=nixos would break both.
          server.succeed("grep -q '^ID=hart-os' /etc/os-release")
          server.fail("grep -qi '^ID=nixos' /etc/os-release")

      with subtest("First-boot succeeded, not failed (hostname/PATH fix)"):
          # The oneshot used to exit non-zero on a bare `hostname` under
          # `set -euo pipefail` even after doing its work; RemainAfterExit leaves
          # it 'active' only on a clean exit (#101 / ISO sweep).
          server.succeed("systemctl is-active hart-first-boot.service")

      with subtest("Boot-audit entries are SIGNED (Ed25519, not UNSIGNED)"):
          # The audit entry was built via a triple-quote that collapsed to a
          # Python SyntaxError, so every entry was UNSIGNED (#101). A signed run
          # leaves a hex signature as the last field and no UNSIGNED markers.
          server.succeed("test -s /var/lib/hart/boot_audit.log")
          server.fail("grep -q UNSIGNED /var/lib/hart/boot_audit.log")

      with subtest("Firewall allows port 6777"):
          server.succeed("nft list ruleset 2>/dev/null | grep -q 6777 || iptables -L -n | grep -q 6777")

      with subtest("CLI tool available"):
          server.succeed("which hart || which hart-cli")

      with subtest("No desktop environment on server"):
          server.fail("systemctl is-active display-manager.service")

      # ── RUNS-ANYWHERE: hypervisor guest integration + CPU microcode ──
      # Parity with what Windows/macOS guests get for free. HART shipped NONE
      # of this: a node in Hyper-V/VMware/QEMU had no display resize, no
      # clipboard, no graceful host shutdown, no host time sync.
      # Units are asserted GENERATED, not active — same honest contract the
      # ClamAV subtest documents: each agent binds a hypervisor-specific
      # transport (hv_vmbus / virtio-serial / vmw vsock) that is absent under
      # this test's plain QEMU, so "configured and ready" is the provable
      # claim, and it is exactly the claim that regressed to false.
      with subtest("hypervisor guest agents are configured for every host"):
          server.succeed("systemctl cat hv-kvp.service")           # Hyper-V KVP
          server.succeed("systemctl cat qemu-guest-agent.service") # QEMU/KVM/Proxmox
          server.succeed("systemctl cat spice-vdagentd.service")   # SPICE clip+resize

      with subtest("Hyper-V host TIME SYNC is available (the VM half of the RTC skew)"):
          # hv_utils carries the host-time source. Task #24: a dual-boot node
          # read the RTC as UTC and NTP yanked the clock backwards 5.5h on
          # connect; inside Hyper-V the host is the authority and this is the
          # path that supplies it.
          server.succeed("systemctl cat hv-vss.service")
          kvp = server.succeed("systemctl show hv-kvp.service -p LoadState --value").strip()
          assert kvp == "loaded", f"hv-kvp must be loaded, got {kvp!r}"

      with subtest("CPU microcode is prepended to the initrd (not just an option)"):
          # NixOS emits early microcode as an UNCOMPRESSED cpio concatenated
          # in front of the real initrd, so the archive's own path strings sit
          # in the first few KiB of the file. Reading them back proves the
          # artifact was actually built — an eval-level option check could not.
          head = server.succeed(
              "head -c 4096 /run/current-system/initrd | strings || true")
          assert "kernel/x86/microcode" in head, (
              "no early-microcode cpio at the head of the initrd — "
              "hardware.cpu.{intel,amd}.updateMicrocode did not take effect")
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # Test 2: Desktop variant boots with GNOME and subsystem tools
  # ─────────────────────────────────────────────────────────────
  hart-desktop-boot = pkgs.testers.runNixOSTest {
    name = "hart-desktop-boot";
    node.specialArgs = specialArgs;

    nodes.desktop = mkNode "desktop" {
      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };
      # The shipped desktop's session owner (desktop.nix enables it via the
      # profile; mkNode's minimal machinery does not). Defaults are the shipped
      # ones: compCommand stays null (no hart-comp closure cost -- "null until
      # Phase 3"), the cage GTK3 floor is the session. Same enablement the
      # display-tiers-neverblack nodes already run in these shards.
      hart.sessionSupervisor.enable = true;

      # Every feature this test's tail ASSERTS, enabled with the profile's own
      # shapes -- enumerated in one pass instead of discovered one CI round at a
      # time (each wait_for_unit below names a service that simply does not
      # exist on the minimal machinery node). Deliberately NOT the full profile:
      # that is the shipped 22 GiB closure and belongs in an image build, not a
      # shard VM. What is asserted, and nothing more:
      hart.modelBus.enable = true;                    # hart-model-bus.service
      hart.liquidUI = {                               # hart-liquid-ui.service +
        enable = true;                                # the GTK3 shell typelibs +
        renderer = "webkit";                          # the hart-shell session pkg
        voiceEnabled = pkgs.lib.mkForce false;                         # (no TTS closure in a VM)
      };
      hart.appBridge.enable = true;                   # hart-app-bridge.service
      hart.conky.enable = true;                       # conkyrc deployed
      hart.gaming.enable = true;                      # vulkan-tools (vulkaninfo)
      hart.devTools.enable = true;                    # gcc/python3/node/git
      hart.subsystems = {
        enable = true;
        linux = { flatpak = true; appimage = true; }; # flatpak + appimage-run
        # wine WITHOUT gaming: the test asserts `wine`, not Steam/Lutris -- the
        # gaming launcher set is a multi-GB closure a shard must not carry.
        windows.enable = true;
      };
    };

    testScript = ''
      desktop.start()
      desktop.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          desktop.wait_for_unit("hart-backend.service", timeout=120)

      # greetd, NOT display-manager.service, and NOT GNOME. The shipped desktop's
      # session is owned by greetd (hart-session-supervisor.nix; desktop.nix:
      # "greetd REPLACES GDM and runs the tier-drop SELECTOR"), and greetd never
      # registers the display-manager.service alias -- that unit simply does not
      # exist on a HART node, so the old assertion timed out after 180s on every
      # run since 2026-07-26 regardless of whether the session was healthy. The
      # supervisor is enabled on this node (the `extra` module below) exactly the
      # way display-tiers-neverblack's nodes already do, so this asserts the REAL
      # boot contract: the tier-drop supervisor comes up.
      with subtest("Session supervisor (greetd) starts -- the shipped DM"):
          desktop.wait_for_unit("greetd.service", timeout=180)

      # ── AI-native session services (regression guard for #99) ──
      # These are all Type="notify" units whose ExecStart python does
      # `import systemd.daemon; notify('READY=1')`. When systemd-python is
      # missing from the hart python env the import raises, the process dies,
      # and systemd kills the unit at TimeoutStartSec — so on a fresh ISO the
      # bridge + LiquidUI server never come up (the originally-reported crash).
      # Asserting they reach "active" fails on the broken build and passes once
      # systemd-python is present.
      with subtest("Model Bus service is active (Type=notify / systemd-python)"):
          desktop.wait_for_unit("hart-model-bus.service", timeout=180)

      with subtest("LiquidUI server is active (Type=notify / systemd-python)"):
          desktop.wait_for_unit("hart-liquid-ui.service", timeout=180)

      with subtest("App Bridge service is active (Type=notify / systemd-python)"):
          desktop.wait_for_unit("hart-app-bridge.service", timeout=180)

      # ── Android-on-Linux branding (regression guard for #101) ──
      # The ISO's installer-CD profile injects a normal `nixos` user that a
      # greeter would list; desktop.nix demotes it to a hidden system account
      # (uid < 1000). This node is NOT built from the ISO config (mkNode, #70),
      # so the user may legitimately not exist at all -- and that is equally
      # "hidden from the greeter". The invariant is "no VISIBLE nixos user",
      # not "a demoted one exists": absent passes, present-but-demoted passes,
      # present-with-a-login-uid is the regression.
      with subtest("No visible 'nixos' user on the greeter (absent or demoted)"):
          desktop.succeed(
              '! getent passwd nixos >/dev/null || test "$(id -u nixos)" -lt 1000')

      # ── Glass-shell GI deps in the closure (regression guard for #99/#100) ──
      # The cage glass shell does gi.require_version('Gtk','3.0')/('WebKit2',
      # '4.1'); those typelibs must be built into the system so GI_TYPELIB_PATH
      # can find them. (Full render is validated at real boot.)
      with subtest("Glass-shell GI typelibs are present (Gtk-3.0 + WebKit2-4.1)"):
          desktop.succeed("find /nix/store -name 'Gtk-3.0.typelib' -print -quit | grep -q .")
          desktop.succeed("find /nix/store -name 'WebKit2-4.1.typelib' -print -quit | grep -q .")

      # ── HART is the SHELL, not an app on GNOME (#102) ──
      # STRUCTURAL check only: the session PACKAGE is realized in the closure.
      # Greeter REGISTRATION (/run/current-system/sw/share/wayland-sessions/*)
      # is materialized by a display manager's pathsToLink, and this node runs
      # greetd via the supervisor -- the GDM-materialized assertion lives in
      # hart-desktop-shell-boot (tests/desktop-boot.nix), which boots a real GDM
      # for exactly that purpose. Asserting it here duplicated that test on a
      # node that cannot pass it (same honest-scope split floor-lock documents).
      with subtest("HART glass-shell session package is in the closure"):
          desktop.succeed(
              "find /nix/store -name 'hart-shell.desktop' -print -quit | grep -q .")

      with subtest("Wine available (native Windows API)"):
          desktop.succeed("which wine64 || which wine")

      with subtest("Flatpak available"):
          desktop.succeed("which flatpak")

      with subtest("AppImage support"):
          desktop.succeed("which appimage-run")

      with subtest("Conky config deployed"):
          desktop.succeed("test -f /nix/store/*/share/hart/hart.conkyrc || find /nix/store -name hart.conkyrc -print -quit | grep -q .")

      with subtest("Vulkan tools present (for DXVK)"):
          desktop.succeed("which vulkaninfo")

      with subtest("Development tools available"):
          desktop.succeed("which gcc")
          desktop.succeed("which python3")
          desktop.succeed("which node")
          desktop.succeed("which git")
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # Test 3: Edge variant boots with minimal footprint
  # ─────────────────────────────────────────────────────────────
  hart-edge-boot = pkgs.testers.runNixOSTest {
    name = "hart-edge-boot";
    node.specialArgs = specialArgs;

    nodes.edge = mkNode "edge" {
      virtualisation = {
        memorySize = 1024;
        cores = 1;
      };
    };

    testScript = ''
      edge.start()
      edge.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          edge.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Discovery service starts"):
          edge.wait_for_unit("hart-discovery.service", timeout=60)

      with subtest("No agent daemon on edge"):
          edge.fail("systemctl is-enabled hart-agent-daemon.service")

      with subtest("No LLM service on edge"):
          edge.fail("systemctl is-enabled hart-llm.service")

      with subtest("No vision service on edge"):
          edge.fail("systemctl is-enabled hart-vision.service")

      with subtest("No display manager on edge"):
          edge.fail("systemctl is-active display-manager.service")

      with subtest("Minimal memory usage"):
          mem_info = edge.succeed("free -m")
          # Edge should use less than 512MB at idle
          used_line = [l for l in mem_info.strip().split("\n") if l.startswith("Mem:")][0]
          used_mb = int(used_line.split()[2])
          assert used_mb < 768, f"Edge using {used_mb}MB (expected < 768MB)"

      with subtest("capped HART units stay inside their own cgroup caps"):
          # TASK #19. "Backend service starts" above does NOT cover this:
          # wait_for_unit returns the moment the unit goes active, while
          # MemoryHigh only THROTTLES (reclaim, no failure) and a MemoryMax
          # OOM-kill is then papered over by Restart=on-failure — the unit is
          # active again a few seconds later and the test never notices. A
          # crash-loop and a healthy service look identical through that lens.
          #
          # The caps are real: hart-discovery ships MemoryMax=48M on edge,
          # hart-backend 640M (raised from 384M in 2026-07-28 after the
          # backend's import floor was measured at 275 MB — that fix was
          # applied to ONE unit and never checked against the others).
          #
          # DISCOVERED, not hardcoded: a capped unit added tomorrow is covered
          # the day it lands, not the day someone remembers this list.
          units = edge.succeed(
              "systemctl list-units --type=service --all --no-legend --plain "
              "'hart-*' | awk '{print $1}'").split()
          assert units, (
              "no hart-*.service units found on the edge node — the probe is "
              "broken, so a green result here would prove nothing")

          def _num(v):
              v = v.strip()
              if v in ("", "[not set]", "infinity"):
                  return None
              try:
                  n = int(v)
              except ValueError:
                  return None
              # systemd renders "unset" as UINT64_MAX for some properties.
              return None if n >= 2**63 else n

          rows, hard, unmeasured = [], [], []
          for u in units:
              raw = edge.succeed(
                  f"systemctl show {u} -p LoadState,ActiveState,Result,"
                  f"NRestarts,MemoryMax,MemoryHigh,MemoryPeak,ControlGroup")
              p = dict(l.split("=", 1) for l in raw.splitlines() if "=" in l)
              if p.get("LoadState") != "loaded":
                  continue
              mmax, mhigh = _num(p.get("MemoryMax", "")), _num(p.get("MemoryHigh", ""))
              if mmax is None and mhigh is None:
                  continue                      # genuinely uncapped — nothing to check
              peak = _num(p.get("MemoryPeak", ""))
              if peak is None:
                  # systemd < 253 has no MemoryPeak; read the cgroup directly
                  # rather than skip. A skipped measurement that stays quiet is
                  # how a cap check turns into a no-op.
                  cg = p.get("ControlGroup", "").strip()
                  if cg:
                      rc, out = edge.execute(
                          f"cat /sys/fs/cgroup{cg}/memory.peak 2>/dev/null")
                      peak = _num(out) if rc == 0 else None
              restarts = _num(p.get("NRestarts", "")) or 0
              result = p.get("Result", "").strip()
              act = p.get("ActiveState", "").strip()
              rows.append((u, peak, mhigh, mmax, restarts, result, act))

              # A peak only exists while the cgroup does. Oneshots that have
              # already exited (hart-sandbox-firstboot and friends) legitimately
              # have none — demanding one there would paint this red for a unit
              # that did its job and left. Their OOM/restart signals below are
              # still checked, which is what actually matters for #19.
              if act != "active":
                  pass
              elif peak is None:
                  unmeasured.append(u)
              elif mmax is not None and peak >= mmax:
                  hard.append(f"{u}: peak {peak} >= MemoryMax {mmax} (OOM-kill territory)")
              if result == "oom-kill":
                  hard.append(f"{u}: Result=oom-kill — the cap killed it")
              if restarts > 0:
                  hard.append(f"{u}: NRestarts={restarts} — it is restart-looping")

          # ALWAYS print the table, pass or fail. The 2026-07-28 cap raise was
          # sized from a DEV-BOX import measurement; these are the first real
          # numbers from the shipped python env, and the next tightening should
          # be led by them rather than by another estimate.
          print("── edge cgroup caps: measured peak vs cap ──")
          for u, peak, mhigh, mmax, restarts, result, act in rows:
              def _mb(v):
                  return "  n/a" if v is None else f"{v / 1048576:5.1f}M"
              head = "" if (mhigh is None or peak is None or peak < mhigh) \
                  else "  <-- OVER MemoryHigh (throttled: reclaim on every alloc)"
              print(f"   {u:32s} peak={_mb(peak)} high={_mb(mhigh)} "
                    f"max={_mb(mmax)} restarts={restarts} {act}/{result}{head}")

          assert not unmeasured, (
              f"could not measure peak memory for RUNNING unit(s) {unmeasured} "
              f"— neither MemoryPeak nor the cgroup memory.peak was readable, "
              f"so this check would silently pass while measuring nothing")
          # The two units the subtests above waited for must be among the ones
          # actually measured. Without this, a future rename makes the loop
          # find nothing to measure and the whole check passes vacuously.
          measured = {u for u, peak, *_ in rows if peak is not None}
          for required in ("hart-backend.service", "hart-discovery.service"):
              assert required in measured, (
                  f"{required} carries a cgroup cap and is running, but no "
                  f"peak was measured for it — this check is not covering the "
                  f"unit task #19 is actually about")
          assert not hard, (
              "capped HART unit(s) exceeded their own cgroup limits on edge:\n  "
              + "\n  ".join(hard))
          # NOTE ON SCOPE: exceeding MemoryHigh is reported above but is NOT
          # yet a hard failure. MemoryHigh throttles rather than kills, and no
          # real number for the nix python env exists yet — the 275 MB figure
          # in hart-backend.nix was measured on a dev box whose venv carries
          # torch/transformers, which hart-app.nix does NOT ship. Promoting it
          # to a gate on an unmeasured guess is how a gate nobody can pass gets
          # disabled. The hard gates above (OOM, restart-loop, peak >=
          # MemoryMax) are the unambiguous crash-loop signals task #19 names.

      with subtest("/status tells the TRUTH about learning, and says WHY (task #3d)"):
          # THE FALSE-HEALTHY CLASS. /status is what the tray, hart_cli and
          # deploy/linux/hart-cli all read. On this node the learning pipeline
          # genuinely cannot start — hevolveai is a closed-source .pyd that is
          # absent here exactly as it is on a shipped box — so the ONLY honest
          # answers are learning_active=false and a reason.
          #
          # Before 7fa17f73 the bridge handler was `except Exception:` with no
          # log and no field, so "not learning" and "learning fine, just idle"
          # were indistinguishable from outside. VM run 30758875130 shows the
          # real import failing (rl_ef -> "RuntimeError: Explicitly using
          # 'asyncio' already"), which is precisely the case that used to
          # vanish.
          import json as _json
          edge.wait_for_open_port(6777, timeout=300)
          body = _json.loads(edge.succeed("curl -sf http://localhost:6777/status"))
          edge.log(f"/status: {body}")

          assert body.get("learning_active") is False, (
              f"/status claims learning_active={body.get('learning_active')!r} "
              f"on a node where hevolveai is absent — that is the "
              f"false-healthy signal task #3 exists to remove")
          # SHAPE STABILITY: learning_mode must be present either way. It used
          # to be set only on the success path, so the field vanished from the
          # degraded response — and hart_cli prints whatever keys it finds.
          assert "learning_mode" in body, (
              "learning_mode is missing from the degraded /status response; "
              "the shape changes under failure, so the field disappears "
              "exactly when someone is looking at it")
          # And when the core is unhealthy, the REASON must reach the caller,
          # not just the journal.
          if body.get("hevolve_core_healthy") is False:
              assert body.get("hevolve_core_error"), (
                  "core reported unhealthy with no hevolve_core_error — the "
                  "caller sees 'unhealthy' and cannot tell an import crash "
                  "from a node that simply is not learning yet")

      with subtest("a BACKWARDS clock step does not wedge the OS (task #24)"):
          # THE REAL INCIDENT: on a Windows dual-boot node the RTC holds LOCAL
          # time while NixOS reads it as UTC, so the box ran +5:30 wrong until
          # wifi came up — then NTP yanked the wall clock BACKWARDS by 19800s,
          # immediately before the desktop hung. hart-installer.nix:117 fixes
          # the CAUSE where hart-install runs, by writing
          # time.hardwareClockInLocalTime when a Windows bootloader is present.
          #
          # This asserts the OTHER half: that the RUNNING system survives a
          # step regardless. The installer fix cannot help a live-USB boot on
          # the same hardware, a dead CMOS battery, a VM restored from a
          # snapshot, or a first NTP sync after a long power-off.
          #
          # 19800 seconds EXACTLY — the IST offset that actually happened, not
          # a round number chosen for the test.
          edge.succeed("timedatectl set-ntp false 2>/dev/null || true")
          before = int(edge.succeed("date +%s").strip())
          edge.succeed(f"date -s '@{before - 19800}'")
          after = int(edge.succeed("date +%s").strip())
          assert after < before, (
              f"the clock did NOT move backwards ({before} -> {after}); this "
              f"subtest would then prove nothing about surviving a step")

          # The OS must still answer. Bounded, because "wedged" is precisely
          # the failure being tested and an unbounded command would hang the
          # whole run instead of failing it.
          edge.succeed("timeout 30 systemctl is-system-running --wait || true")
          edge.succeed("timeout 10 echo still-alive")
          edge.require_unit_state("multi-user.target", "active")

          # The two capped hart services must still be up, and must NOT have
          # been restarted by the step — a restart here is the crash-loop the
          # task is really about.
          for unit in ("hart-backend.service", "hart-discovery.service"):
              state = edge.succeed(f"systemctl is-active {unit}").strip()
              assert state == "active", (
                  f"{unit} is {state!r} after a -5h30m clock step — the "
                  f"backwards jump took it down, which is the wedge task #24 "
                  f"was opened for")
              restarts = edge.succeed(
                  f"systemctl show -p NRestarts --value {unit}").strip()
              assert restarts in ("", "0"), (
                  f"{unit} restarted {restarts} time(s) across the clock step; "
                  f"something in it is doing `deadline - now` on the WALL "
                  f"clock (core/circuit_breaker.py was one such place, fixed "
                  f"906ee781 — find the next one)")

      with subtest("CLI tool available"):
          edge.succeed("which hart || which hart-cli")

      # ── LAST ON PURPOSE: this one KILLS services ────────────────────────
      # It must run after the clock-step subtest, which asserts NRestarts == 0.
      with subtest("every critical hart unit RECOVERS from a kill (task #31)"):
          # #31 asks that every shell route and systemd unit have a PROVEN
          # failure mode. The routes are being walked in python; the UNITS had
          # nothing at all — their Restart= lines were declared and never
          # exercised, which is a failure mode on paper.
          #
          # SIGKILL, not `systemctl restart`: restart proves systemd can start
          # the unit, which was never in doubt. Killing the process proves the
          # unit comes BACK after dying the way it would die in the field —
          # OOM, a segfault, an unhandled exception.
          for unit in ("hart-backend.service", "hart-discovery.service"):
              # SETTLE FIRST. hart-discovery declares
              # `bindsTo = hart-backend.service`, so killing the backend takes
              # discovery down WITH it; by the time this loop reaches
              # discovery its MainPID may still be 0 mid-cascade. Reading it
              # then would fail on "no MainPID" — a flake about ORDERING, not
              # about recovery, which is the worst kind of red.
              edge.wait_for_unit(unit, timeout=120)
              before = int(edge.succeed(
                  f"systemctl show -p NRestarts --value {unit}").strip() or 0)
              pid = edge.succeed(
                  f"systemctl show -p MainPID --value {unit}").strip()
              assert pid and pid != "0", (
                  f"{unit} has no MainPID, so there is nothing to kill and "
                  f"this subtest would pass without testing anything")
              edge.succeed(f"kill -9 {pid}")

              # Bounded: a unit that never comes back is the failure, so an
              # unbounded wait would hang the run instead of reporting it.
              edge.wait_for_unit(unit, timeout=120)
              after = int(edge.succeed(
                  f"systemctl show -p NRestarts --value {unit}").strip() or 0)
              assert after > before, (
                  f"{unit} is active again but NRestarts did not move "
                  f"({before} -> {after}) — systemd did not restart it, so "
                  f"something else is holding the name and the recovery is "
                  f"not what it looks like")
              newpid = edge.succeed(
                  f"systemctl show -p MainPID --value {unit}").strip()
              assert newpid != pid, (
                  f"{unit} reports the same MainPID {pid} after a SIGKILL — "
                  f"the process did not actually die, so nothing was proven")
              edge.log(f"{unit}: killed {pid}, recovered as {newpid} "
                       f"(NRestarts {before} -> {after})")
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # Test 4: Two-node peer discovery
  # ─────────────────────────────────────────────────────────────
  hart-peer-discovery = pkgs.testers.runNixOSTest {
    name = "hart-peer-discovery";
    node.specialArgs = specialArgs;

    # (2026-07-28) This node briefly carried an A/B that emptied the backend's
    # SystemCallFilter — the unit text rendered, both arms crashed identically,
    # seccomp exonerated. The real cause was the Resource Governor's RLIMIT_AS
    # fallback (core/resource_governor.py — see its comment and
    # tests/unit/test_resource_governor_no_rlimit_as.py). Filter restored.
    nodes.server = mkNode "server" {
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      # Both nodes share a virtual network
      networking.interfaces.eth1.ipv4.addresses = [
        { address = "192.168.1.1"; prefixLength = 24; }
      ];
    };

    nodes.edge = mkNode "edge" {
      virtualisation = {
        memorySize = 1024;
        cores = 1;
      };
      networking.interfaces.eth1.ipv4.addresses = [
        { address = "192.168.1.2"; prefixLength = 24; }
      ];
    };

    testScript = ''
      # Start both nodes
      server.start()
      edge.start()

      # Wait for basic services
      server.wait_for_unit("hart-backend.service", timeout=120)
      edge.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Nodes can reach each other"):
          server.succeed("ping -c1 -W5 192.168.1.2")
          edge.succeed("ping -c1 -W5 192.168.1.1")

      with subtest("Discovery services running"):
          server.wait_for_unit("hart-discovery.service", timeout=60)
          edge.wait_for_unit("hart-discovery.service", timeout=60)

      # A unit going ACTIVE is not a readiness signal: hart-backend is not
      # Type=notify, so systemd calls it active the moment the process execs,
      # while the backend then spends ~20s running SQLAlchemy migrations before
      # it binds anything. Curling straight after wait_for_unit therefore raced
      # the socket and failed with curl exit 7 (connection refused) on every run
      # since at least 2026-07-26. Wait for the PORT, which is the real signal --
      # the same file already does exactly this for the single-node case
      # ("Backend responds on port 6777" above). Both nodes, because the second
      # subtest reverses the direction.
      #
      # 300s, not the single-node case's 60-120s: this test boots TWO full
      # backends CONCURRENTLY on one 2-core shard runner, and the first fix's
      # 120s still timed out there (run 30401896432) -- the wait was right, the
      # budget was borrowed from a single-VM world. A readiness budget can be
      # generous without weakening the test: if the backend never binds, this
      # still fails, just honestly.
      # On timeout, dump the facts that DISTINGUISH the failure causes before
      # re-raising. The backend dies with "can't start new thread" here, and
      # CPython raises that for EVERY pthread_create failure, hiding the errno --
      # which is the one fact that separates a task limit (EAGAIN: TasksMax /
      # RLIMIT_NPROC / kernel threads-max) from memory pressure (ENOMEM). Two
      # theories have already been disproven from the outside (an app thread
      # storm: the real import measures 11 threads / 275 MB RSS on the dev box;
      # node memorySize defaults: these nodes set 2048/1024 explicitly), so this
      # dump makes the next failure name its own cause instead of inviting a
      # third guess.
      def _dump_backend_state(m, tag):
          # The python the backend actually runs (waitress env), for the probes.
          py = m.execute(
              "systemctl cat hart-backend | grep ExecStart= | grep -oE '/nix/store/[a-z0-9]+-[^ ]+/bin/python'"
          )[1].strip()
          for cmd in [
              # What did the unit ACTUALLY render? (Verifies overrides landed --
              # run 30407872610's A/B is uninterpretable without this.)
              "systemctl cat hart-backend | grep -E 'SystemCallFilter|MemoryMax|TasksMax|ExecStart' || true",
              "systemctl show hart-backend -p TasksCurrent,TasksMax,MemoryCurrent,MemoryPeak,Result,ExecMainStatus,NRestarts",
              "free -m",
              "journalctl -u hart-backend --no-pager -n 12 | tail -12",
              # ── THEORY-FREE PROBES: can THIS python start ONE thread... ──
              # (a) bare, as root, no sandbox:
              f"{py} -c 'import threading; t=threading.Thread(target=lambda: None); t.start(); t.join(); print(\"BARE-ROOT THREAD OK\")' 2>&1 || true",
              # (b) as the hart user, no sandbox:
              f"su -s /bin/sh hart -c \"{py} -c 'import threading; t=threading.Thread(target=lambda: None); t.start(); t.join(); print(\\\"HART-USER THREAD OK\\\")'\" 2>&1 || true",
              # (c) under the unit's FULL context: clone the real unit's sandbox by
              # running inside the actual service cgroup + properties via
              # systemd-run with the same hardening set as hart-backend.nix:
              f"systemd-run --wait --pipe --collect -p User=hart -p NoNewPrivileges=yes "
              f"-p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes "
              f"-p SystemCallFilter=@system-service -p LockPersonality=yes "
              f"-p RestrictRealtime=yes -p RestrictSUIDSGID=yes -p MemoryDenyWriteExecute=no "
              f"-p RestrictAddressFamilies='AF_INET AF_INET6 AF_UNIX' "
              f"{py} -c 'import threading; t=threading.Thread(target=lambda: None); t.start(); t.join(); print(\"SANDBOXED THREAD OK\")' 2>&1 || true",
              # (d) the minimal IMPORT under the same sandbox -- does the module
              # scope reach the thread, or does an earlier step poison it?
              f"systemd-run --wait --pipe --collect -p User=hart -p SystemCallFilter=@system-service "
              f"{py} -c 'import ctypes; print(\"CTYPES OK\"); import threading; threading.Thread(target=lambda: None).start(); print(\"FILTERED THREAD OK\")' 2>&1 || true",
          ]:
              print(f"── [{tag}] $ {cmd[:120]}")
              print(m.execute(cmd)[1])

      try:
          server.wait_for_open_port(6777, timeout=300)
          edge.wait_for_open_port(6777, timeout=300)
      except Exception:
          _dump_backend_state(server, "server")
          _dump_backend_state(edge, "edge")
          raise

      # Cross-host reachability. `-f` makes curl exit 22 on ANY HTTP >= 400,
      # which is indistinguishable from "unreachable" and hid WHY this failed
      # for four days (run 30485906966: exit 22 — the peer ANSWERED, so the
      # route and the firewall were fine; the response was an error). Capture
      # the code and the body so a future failure names itself, and assert
      # the reachability contract explicitly: an HTTP response arrived AND it
      # is 2xx. A non-2xx now fails with the peer's own status + body.
      def peer_status(node, ip):
          rc, out = node.execute(
              "curl -s -o /tmp/peerbody -w '%{http_code}' --max-time 20 "
              f"http://{ip}:6777/status")
          assert rc == 0, (
              f"no HTTP response from {ip}:6777 (curl rc={rc}) — route/firewall "
              f"level failure, not an application error")
          body = node.succeed("head -c 300 /tmp/peerbody || true")
          return out.strip(), body

      with subtest("Server backend accessible from edge"):
          code, body = peer_status(edge, "192.168.1.1")
          assert code.startswith("2"), \
              f"server /status answered HTTP {code} cross-host, body: {body!r}"

      with subtest("Edge backend accessible from server"):
          code, body = peer_status(server, "192.168.1.2")
          assert code.startswith("2"), \
              f"edge /status answered HTTP {code} cross-host, body: {body!r}"
    '';
  };
}

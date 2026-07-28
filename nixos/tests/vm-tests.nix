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
  # Minimal node: hart modules + variant, NO ../configurations/X.nix (and thus
  # no installer-CD overlay collision).  `extra` carries per-test virtualisation
  # / networking overrides.
  # `extra` is imported as a module (NOT merged with //) so its nested attrs
  # (e.g. networking.interfaces on the peer-discovery nodes) recursively merge
  # with the base instead of clobbering networking.hostName.
  mkNode = variant: extra: { pkgs, lib, hartSrc, ... }: {
    imports = hartModules ++ [ extra ];
    hart.enable = true;
    hart.variant = variant;
    hart.version = "0.0.0-test";
    # hart.package has NO default (mkOption type=package, "set in variant
    # config").  The full configs set it via callPackage hart-app.nix; the
    # minimal node must too, else system.build.toplevel can't evaluate the
    # hart-agent/backend/discovery services that read config.hart.package.
    # `--no-build` only evaluates this derivation (it is not built here).
    hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };
    # hart-base sets networking.hostName = mkDefault "hart-node"; runNixOSTest
    # also sets a default (the node name) -> two same-priority defaults conflict.
    # Force a deterministic per-node value (tests address by IP, not hostname).
    networking.hostName = lib.mkForce variant;
  };

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
      import json

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
        voiceEnabled = false;                         # (no TTS closure in a VM)
      };
      hart.appBridge.enable = true;                   # hart-app-bridge.service
      hart.conky.enable = true;                       # conkyrc deployed
      hart.gaming.enable = true;                      # vulkan-tools (vulkaninfo)
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

      with subtest("CLI tool available"):
          edge.succeed("which hart || which hart-cli")
    '';
  };

  # ─────────────────────────────────────────────────────────────
  # Test 4: Two-node peer discovery
  # ─────────────────────────────────────────────────────────────
  hart-peer-discovery = pkgs.testers.runNixOSTest {
    name = "hart-peer-discovery";
    node.specialArgs = specialArgs;

    nodes.server = mkNode "server" ({ lib, ... }: {
      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };
      # Both nodes share a virtual network
      networking.interfaces.eth1.ipv4.addresses = [
        { address = "192.168.1.1"; prefixLength = 24; }
      ];

      # ── A/B EXPERIMENT (server node ONLY — edge below keeps the filter) ──
      # The backend's FIRST pthread_create fails deterministically in every VM
      # ("can't start new thread" at hart_intelligence_entry.py:1909, 23 tests,
      # every restart) while the diagnostic dump shows nothing else is scarce:
      # 1.7G free RAM, 89 tasks system-wide, nproc 7824, overcommit=1,
      # TasksMax=512 untouched. First-thread + same-line + roomy-limits is the
      # seccomp shape: SystemCallFilter=@system-service is an ALLOWLIST whose
      # unlisted syscalls return EPERM, and glibc's pthread_create only falls
      # back from clone3 to clone on ENOSYS — an EPERM kills it. This node drops
      # the filter; edge keeps it. If server's backend boots and edge's still
      # dies, the mechanism is PROVEN and the fix is adding the missing syscall
      # to the unit's filter (never deleting the hardening) — if both still die,
      # the seccomp theory joins the two already disproven.
      systemd.services.hart-backend.serviceConfig.SystemCallFilter = lib.mkForce [ ];
    });

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
          for cmd in [
              "systemctl show hart-backend -p TasksCurrent,TasksMax,MemoryCurrent,MemoryPeak,Result,ExecMainStatus,NRestarts",
              "cat /sys/fs/cgroup/system.slice/hart-backend.service/pids.current /sys/fs/cgroup/system.slice/hart-backend.service/pids.max 2>/dev/null || true",
              "free -m",
              "cat /proc/sys/kernel/threads-max /proc/sys/kernel/pid_max /proc/sys/vm/overcommit_memory",
              "su -s /bin/sh hart -c 'ulimit -u -v -m' 2>/dev/null || true",
              "ps -eLf | wc -l",
              "journalctl -u hart-backend --no-pager -n 30 | tail -30",
          ]:
              print(f"── [{tag}] $ {cmd}")
              print(m.execute(cmd)[1])

      try:
          server.wait_for_open_port(6777, timeout=300)
          edge.wait_for_open_port(6777, timeout=300)
      except Exception:
          _dump_backend_state(server, "server")
          _dump_backend_state(edge, "edge")
          raise

      with subtest("Server backend accessible from edge"):
          edge.succeed("curl -sf http://192.168.1.1:6777/status")

      with subtest("Edge backend accessible from server"):
          server.succeed("curl -sf http://192.168.1.2:6777/status")
    '';
  };
}

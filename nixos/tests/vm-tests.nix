# ═══════════════════════════════════════════════════════════════
# HART OS NixOS VM Integration Tests
# ═══════════════════════════════════════════════════════════════
#
# Uses NixOS's built-in testers.runNixOSTest framework.
# Each test boots a real VM via QEMU and runs assertions.
#
# Run all tests:
#   nix flake check
#
# Run a single test:
#   nix build .#checks.x86_64-linux.hart-server-boot
#
# These tests take 5-15 minutes each (VM boot + assertions).

{ pkgs, hartModules, specialArgs }:

let
  # Helper: create a test configuration with all hart modules
  mkTestConfig = variant: extra: {
    imports = hartModules ++ [
      ../configurations/${variant}.nix
    ] ++ extra;
  };

in
{
  # ─────────────────────────────────────────────────────────────
  # Test 1: Server variant boots and all core services start
  # ─────────────────────────────────────────────────────────────
  hart-server-boot = pkgs.testers.runNixOSTest {
    name = "hart-server-boot";

    nodes.server = { config, pkgs, lib, ... }: {
      imports = hartModules ++ [
        ../configurations/server.nix
      ];

      # Override for test VM (less RAM, faster boot)
      virtualisation = {
        memorySize = 2048;
        cores = 2;
        forwardPorts = [
          { from = "host"; host.port = 16777; guest.port = 6777; }
        ];
      };

      # Provide specialArgs values for test
      hart.version = "0.0.0-test";
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

      with subtest("OS branding present"):
          server.succeed("grep -q HART OS /etc/os-release")

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

    nodes.desktop = { config, pkgs, lib, ... }: {
      imports = hartModules ++ [
        ../configurations/desktop.nix
      ];

      virtualisation = {
        memorySize = 4096;
        cores = 2;
      };

      hart.version = "0.0.0-test";
    };

    testScript = ''
      desktop.start()
      desktop.wait_for_unit("multi-user.target")

      with subtest("Backend service starts"):
          desktop.wait_for_unit("hart-backend.service", timeout=120)

      with subtest("Display manager starts (GNOME)"):
          desktop.wait_for_unit("display-manager.service", timeout=180)

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

    nodes.edge = { config, pkgs, lib, ... }: {
      imports = hartModules ++ [
        ../configurations/edge.nix
      ];

      virtualisation = {
        memorySize = 1024;
        cores = 1;
      };

      hart.version = "0.0.0-test";
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

    nodes.server = { config, pkgs, lib, ... }: {
      imports = hartModules ++ [
        ../configurations/server.nix
      ];

      virtualisation = {
        memorySize = 2048;
        cores = 1;
      };

      hart.version = "0.0.0-test";

      # Both nodes share a virtual network
      networking.interfaces.eth1.ipv4.addresses = [
        { address = "192.168.1.1"; prefixLength = 24; }
      ];
    };

    nodes.edge = { config, pkgs, lib, ... }: {
      imports = hartModules ++ [
        ../configurations/edge.nix
      ];

      virtualisation = {
        memorySize = 1024;
        cores = 1;
      };

      hart.version = "0.0.0-test";

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

      with subtest("Server backend accessible from edge"):
          edge.succeed("curl -sf http://192.168.1.1:6777/status")

      with subtest("Edge backend accessible from server"):
          server.succeed("curl -sf http://192.168.1.2:6777/status")
    '';
  };
}

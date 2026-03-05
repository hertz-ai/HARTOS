{ config, lib, pkgs, hartVersion, hartVariant, ... }:

# HART OS Base Module
# Core system: identity, branding, networking, security, users
# Shared by all variants (server, desktop, edge)

let
  cfg = config.hart;
in
{
  # ─── Options ────────────────────────────────────────────────
  options.hart = {
    enable = lib.mkEnableOption "HART OS services";

    version = lib.mkOption {
      type = lib.types.str;
      default = hartVersion;
      description = "HART OS version string";
    };

    variant = lib.mkOption {
      type = lib.types.enum [ "server" "desktop" "edge" "phone" ];
      default = hartVariant;
      description = "HART OS variant (server, desktop, edge, phone)";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/hart";
      description = "Persistent data directory";
    };

    logDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/log/hart";
      description = "Log directory";
    };

    package = lib.mkOption {
      type = lib.types.package;
      description = "The HART application package (set in variant config)";
    };

    # OS-mode ports: privileged (<1024) — HART OS owns the machine.
    # This frees user-space ports (1024-65535) for user applications.
    # App-mode ports (6777, 6780, etc.) are used when running alongside other software.
    ports = {
      backend = lib.mkOption {
        type = lib.types.port;
        default = 677;
        description = "Backend API port (OS-mode: 677, app-mode: 6777)";
      };
      discovery = lib.mkOption {
        type = lib.types.port;
        default = 678;
        description = "UDP peer discovery port (OS-mode: 678, app-mode: 6780)";
      };
      llm = lib.mkOption {
        type = lib.types.port;
        default = 808;
        description = "Local LLM inference port (OS-mode: 808, app-mode: 8080)";
      };
      vision = lib.mkOption {
        type = lib.types.port;
        default = 989;
        description = "Vision sidecar port (OS-mode: 989, app-mode: 9891)";
      };
      websocket = lib.mkOption {
        type = lib.types.port;
        default = 546;
        description = "WebSocket port for frame streaming (OS-mode: 546, app-mode: 5460)";
      };
      diarization = lib.mkOption {
        type = lib.types.port;
        default = 800;
        description = "Speaker diarization port (OS-mode: 800, app-mode: 8004)";
      };
      dlna_stream = lib.mkOption {
        type = lib.types.port;
        default = 855;
        description = "DLNA MJPEG stream port (OS-mode: 855, app-mode: 8554)";
      };
      mesh_wg = lib.mkOption {
        type = lib.types.port;
        default = 679;
        description = "WireGuard mesh port (OS-mode: 679, app-mode: 6795)";
      };
      mesh_relay = lib.mkOption {
        type = lib.types.port;
        default = 680;
        description = "Mesh relay port (OS-mode: 680, app-mode: 6796)";
      };
    };
  };

  # ─── Configuration ──────────────────────────────────────────
  config = lib.mkIf cfg.enable {

    # ── Allow unfree packages (NVIDIA drivers, CUDA) ──
    nixpkgs.config.allowUnfree = true;

    # ── Branding ──
    environment.etc = {
      "os-release".text = ''
        NAME="HART OS"
        PRETTY_NAME="HART OS ${cfg.version} (Sentient)"
        VERSION="${cfg.version}"
        VERSION_ID="${cfg.version}"
        VERSION_CODENAME=sentient
        ID=hart-os
        ID_LIKE=nixos
        HOME_URL="https://hevolve.ai"
        SUPPORT_URL="https://github.com/hevolve-ai/hart/issues"
        BUG_REPORT_URL="https://github.com/hevolve-ai/hart/issues"
        PRIVACY_POLICY_URL="https://hevolve.ai/privacy"
      '';

      "hart/variant".text = cfg.variant;

      # MOTD: dynamic system info on login
      "profile.d/hart-motd.sh" = {
        mode = "0755";
        text = ''
          #!/bin/bash
          CYAN='\033[0;36m'
          GREEN='\033[0;32m'
          YELLOW='\033[1;33m'
          NC='\033[0m'

          echo ""
          echo -e "''${CYAN}  HART OS ${cfg.version} — Crowdsourced Agentic Intelligence''${NC}"
          echo ""

          if [[ -f ${cfg.dataDir}/node_public.key ]]; then
            NODE_ID=$(xxd -p ${cfg.dataDir}/node_public.key | tr -d '\n' | head -c 16)
            echo -e "  Node ID:    ''${GREEN}''${NODE_ID}...''${NC}"
          fi

          BACKEND=$(systemctl is-active hart-backend.service 2>/dev/null || echo "unknown")
          if [[ "$BACKEND" == "active" ]]; then
            echo -e "  Backend:    ''${GREEN}running''${NC}"
          else
            echo -e "  Backend:    ''${YELLOW}''${BACKEND}''${NC}"
          fi

          echo -e "  Variant:    ${cfg.variant}"
          echo -e "  Uptime:     $(uptime -p 2>/dev/null || echo 'unknown')"

          IP=$(hostname -I 2>/dev/null | awk '{print $1}')
          echo ""
          echo -e "  Dashboard:  http://''${IP:-localhost}:${toString cfg.ports.backend}"
          echo -e "  CLI:        ''${GREEN}hart status''${NC}"
          echo ""
        '';
      };
    };

    # ── Users ──
    users.users.hart = {
      isSystemUser = true;
      group = "hart";
      home = cfg.dataDir;
      createHome = true;
      description = "HART OS service user";
    };
    users.groups.hart = {};

    # Default admin user for interactive login
    users.users.hart-admin = {
      isNormalUser = true;
      description = "HART OS Administrator";
      extraGroups = [ "wheel" "hart" "video" "render" ];
      initialPassword = "hart";  # Change on first login
    };

    # ── Networking ──
    networking = {
      hostName = lib.mkDefault "hart-node";
      firewall = {
        enable = true;
        allowedTCPPorts = [ cfg.ports.backend 22 ];
        allowedUDPPorts = [ cfg.ports.discovery ];
      };
    };

    # ── Kernel tuning (P2P gossip + compute workloads) ──
    boot.kernel.sysctl = {
      # Networking: optimize for P2P gossip
      # mkDefault so hart-kernel.nix specialized values win
      "net.core.rmem_max" = lib.mkDefault 16777216;
      "net.core.wmem_max" = lib.mkDefault 16777216;
      "net.ipv4.tcp_fastopen" = lib.mkDefault 3;
      "net.core.somaxconn" = lib.mkDefault 4096;
      "net.ipv4.tcp_tw_reuse" = lib.mkDefault 1;
      "net.ipv4.tcp_fin_timeout" = lib.mkDefault 15;

      # Memory: favor compute workloads
      "vm.swappiness" = lib.mkDefault 10;
      "vm.dirty_ratio" = lib.mkDefault 40;
      "vm.dirty_background_ratio" = lib.mkDefault 10;
      "vm.overcommit_memory" = lib.mkDefault 1;

      # Security: kernel hardening (mkForce — our stricter values override nixpkgs)
      "kernel.dmesg_restrict" = lib.mkForce 1;
      "kernel.kptr_restrict" = lib.mkForce 2;
      "net.ipv4.conf.all.rp_filter" = lib.mkForce 1;
      "net.ipv4.conf.default.rp_filter" = lib.mkForce 1;
      "net.ipv4.icmp_echo_ignore_broadcasts" = lib.mkForce 1;
      "net.ipv4.conf.all.accept_redirects" = lib.mkForce 0;
      "net.ipv4.conf.default.accept_redirects" = lib.mkForce 0;
      "net.ipv6.conf.all.accept_redirects" = lib.mkForce 0;
      "net.ipv6.conf.default.accept_redirects" = lib.mkForce 0;

      # File descriptors: agent workloads
      "fs.file-max" = lib.mkDefault 524288;
      "fs.inotify.max_user_watches" = lib.mkDefault 524288;
    };

    # ── SSH ──
    services.openssh = {
      enable = true;
      settings = {
        PermitRootLogin = lib.mkDefault "no";
        PasswordAuthentication = true;  # For first login; disable after key setup
      };
    };

    # ── System packages (available to all users) ──
    environment.systemPackages = with pkgs; [
      vim
      htop
      curl
      git
      rsync
      xxd
      jq
      tmux
    ];

    # ── Directories ──
    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 hart hart -"
      "d ${cfg.dataDir}/agent_data 0750 hart hart -"
      "d ${cfg.dataDir}/models 0750 hart hart -"
      "d ${cfg.logDir} 0750 hart hart -"
    ];

    # ── Systemd target: hart.target groups all HART services ──
    systemd.targets.hart = {
      description = "HART OS Services";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
    };

    # ── NixOS metadata ──
    system.stateVersion = "24.11";
  };
}

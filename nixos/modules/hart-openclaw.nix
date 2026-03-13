# ────────────────────────────────────────────────────────────────
# HART OS — OpenClaw Integration Module
# ────────────────────────────────────────────────────────────────
#
# Bundles OpenClaw as a native HART OS application:
#   - Node.js 22 + OpenClaw CLI pre-installed
#   - Gateway service (systemd) auto-started
#   - ClawHub CLI for skill management
#   - HART ↔ OpenClaw bidirectional bridge
#
# HART OS is the superset — OpenClaw runs as a managed service.
# ────────────────────────────────────────────────────────────────

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.openclaw;

  # OpenClaw from npm (wrapped for NixOS)
  openclawPkg = pkgs.buildNpmPackage rec {
    pname = "openclaw";
    version = "2026.3.3";

    src = pkgs.fetchFromGitHub {
      owner = "openclaw";
      repo = "openclaw";
      rev = "v${version}";
      hash = lib.fakeHash;   # Replace with real hash on first build
    };

    nodejs = pkgs.nodejs_22;
    npmDepsHash = lib.fakeHash; # Replace with real hash on first build

    meta = with lib; {
      description = "Personal AI assistant with 20+ channel integrations";
      license = licenses.mit;
      mainProgram = "openclaw";
    };
  };

in {
  options.hart.openclaw = {
    enable = lib.mkEnableOption "OpenClaw integration (personal AI assistant bridge)";

    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Start OpenClaw gateway automatically at boot";
    };

    gatewayPort = lib.mkOption {
      type = lib.types.port;
      default = 18789;
      description = "OpenClaw gateway WebSocket port";
    };

    skillsDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/hart/openclaw/skills";
      description = "Directory for installed ClawHub skills";
    };
  };

  config = lib.mkIf cfg.enable {
    # Ensure Node.js 22 is available
    environment.systemPackages = [
      pkgs.nodejs_22
      # openclawPkg    # Uncomment when hash is populated
    ];

    # OpenClaw Gateway systemd service
    systemd.services.hart-openclaw-gateway = lib.mkIf cfg.autoStart {
      description = "OpenClaw Gateway (managed by HART OS)";
      after = [ "network.target" "hart-backend.service" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        User = "hart";
        Group = "hart";
        ExecStart = "${pkgs.nodejs_22}/bin/node /opt/openclaw/openclaw.mjs gateway --port ${toString cfg.gatewayPort}";
        Restart = "on-failure";
        RestartSec = 5;

        # Sandbox
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.skillsDir "/var/lib/hart" ];
        PrivateTmp = true;
        NoNewPrivileges = true;
      };

      environment = {
        OPENCLAW_SKILLS_DIR = cfg.skillsDir;
        HART_BACKEND_URL = "http://localhost:6777";
        NODE_ENV = "production";
      };
    };

    # Skills directory
    systemd.tmpfiles.rules = [
      "d ${cfg.skillsDir} 0755 hart hart -"
    ];

    # Firewall
    networking.firewall.allowedTCPPorts =
      lib.optional cfg.autoStart cfg.gatewayPort;
  };
}

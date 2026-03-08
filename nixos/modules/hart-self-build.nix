{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS Self-Build Module — The OS that builds itself
# ═══════════════════════════════════════════════════════════════
#
# Unlike static Linux distros, HART OS can modify its own NixOS
# configuration at runtime and rebuild itself live. Every change
# is a new atomic generation with instant rollback.
#
# Architecture:
#   /etc/hart/runtime.nix    — Mutable config (packages, services, settings)
#   /etc/hart/overlays.nix   — User/agent Nix overlays
#   /etc/hart/modules/       — Drop-in NixOS modules added at runtime
#
# How it works:
#   1. User or agent requests a change via API/CLI
#   2. hart-self-build writes valid Nix to /etc/hart/runtime.nix
#   3. nixos-rebuild switch --flake /etc/nixos evaluates the change
#   4. If build succeeds → new generation (old one kept for rollback)
#   5. If build fails → nothing changes (NixOS is atomic)
#   6. Canary monitor watches for health regression → auto-rollback
#
# This is NOT configuration drift. Every state is declarative,
# reproducible, and version-controlled in /etc/hart/.

let
  cfg = config.hart;
  sb = config.hart.selfBuild;

  # Python for runtime config manipulation
  pythonForBuild = pkgs.python310.withPackages (ps: [ ps.pyyaml ]);

  # Seed runtime.nix if it doesn't exist
  seedRuntimeConfig = pkgs.writeShellScript "hart-seed-runtime" ''
    set -euo pipefail
    RUNTIME_DIR="/etc/hart"
    RUNTIME_NIX="$RUNTIME_DIR/runtime.nix"
    MODULES_DIR="$RUNTIME_DIR/modules"

    mkdir -p "$RUNTIME_DIR" "$MODULES_DIR"

    # Seed runtime.nix with empty config if missing
    if [[ ! -f "$RUNTIME_NIX" ]]; then
      cat > "$RUNTIME_NIX" << 'SEED'
    # HART OS Runtime Configuration
    # This file is managed by hart-self-build.
    # Changes here are applied on next `hart-ota self-build`.
    # DO NOT edit manually — use `hart pkg install <name>` or the API.
    { config, pkgs, lib, ... }:
    {
      # ── Runtime-installed packages ──
      environment.systemPackages = with pkgs; [
        # Packages added at runtime appear here
      ];

      # ── Runtime-enabled services ──
      # services.<name>.enable = true;

      # ── Runtime environment variables ──
      environment.variables = {
        # HART_CUSTOM_VAR = "value";
      };
    }
    SEED
      chmod 644 "$RUNTIME_NIX"
      echo "[SelfBuild] Seeded $RUNTIME_NIX"
    fi

    # Seed overlays.nix
    if [[ ! -f "$RUNTIME_DIR/overlays.nix" ]]; then
      cat > "$RUNTIME_DIR/overlays.nix" << 'SEED'
    # HART OS Runtime Overlays
    # Nix overlays added at runtime (e.g., custom package builds)
    []
    SEED
      chmod 644 "$RUNTIME_DIR/overlays.nix"
    fi
  '';

  # The self-build script
  selfBuildScript = pkgs.writeShellScript "hart-self-build" ''
    set -euo pipefail

    ACTION="''${1:-build}"
    LOG="/var/log/hart/self-build.log"
    LOCK="/run/hart-self-build.lock"
    RUNTIME_NIX="/etc/hart/runtime.nix"
    FLAKE_DIR="/etc/nixos"
    VARIANT="${cfg.variant}"

    mkdir -p "$(dirname "$LOG")"
    exec > >(tee -a "$LOG") 2>&1

    # Prevent concurrent builds
    exec 200>"$LOCK"
    ${pkgs.flock}/bin/flock -n 200 || {
      echo "[SelfBuild] Another build is in progress"
      exit 1
    }

    echo ""
    echo "═══════════════════════════════════════════════"
    echo "  HART OS Self-Build — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "═══════════════════════════════════════════════"

    # Validate runtime.nix syntax before building
    echo "[SelfBuild] Validating runtime.nix..."
    if ! ${pkgs.nix}/bin/nix-instantiate --parse "$RUNTIME_NIX" >/dev/null 2>&1; then
      echo "[SelfBuild] ERROR: runtime.nix has syntax errors!"
      echo "[SelfBuild] Fix with: hart pkg edit"
      exit 2
    fi

    # Snapshot current generation for rollback reference
    CURRENT_GEN=$(readlink /nix/var/nix/profiles/system 2>/dev/null || echo "unknown")
    echo "[SelfBuild] Current generation: $CURRENT_GEN"

    case "$ACTION" in
      build|switch)
        echo "[SelfBuild] Building new generation..."
        TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)

        # Backup runtime.nix
        cp "$RUNTIME_NIX" "/var/lib/hart/ota/history/runtime_$TIMESTAMP.nix" 2>/dev/null || true

        # Build and switch atomically
        if ${pkgs.nixos-rebuild}/bin/nixos-rebuild switch \
            --flake "$FLAKE_DIR#hart-$VARIANT" \
            --impure 2>&1; then
          NEW_GEN=$(readlink /nix/var/nix/profiles/system 2>/dev/null || echo "unknown")
          echo "[SelfBuild] SUCCESS — switched to generation: $NEW_GEN"

          # Write build record
          ${pkgs.jq}/bin/jq -n \
            --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            --arg prev "$CURRENT_GEN" \
            --arg curr "$NEW_GEN" \
            --arg action "$ACTION" \
            '{timestamp: $ts, previous: $prev, current: $curr, action: $action, status: "success"}' \
            >> "/var/lib/hart/ota/history/builds.jsonl" 2>/dev/null || true

          echo "[SelfBuild] Build record written"
        else
          echo "[SelfBuild] BUILD FAILED — system unchanged"
          echo "[SelfBuild] The previous generation is still active"
          exit 3
        fi
        ;;

      dry-run|test)
        echo "[SelfBuild] Dry-run (build only, no switch)..."
        if ${pkgs.nixos-rebuild}/bin/nixos-rebuild build \
            --flake "$FLAKE_DIR#hart-$VARIANT" \
            --impure 2>&1; then
          echo "[SelfBuild] Dry-run SUCCESS — build is valid"
        else
          echo "[SelfBuild] Dry-run FAILED — fix errors before applying"
          exit 3
        fi
        ;;

      diff)
        echo "[SelfBuild] Showing what would change..."
        ${pkgs.nixos-rebuild}/bin/nixos-rebuild build \
          --flake "$FLAKE_DIR#hart-$VARIANT" \
          --impure 2>/dev/null || true
        ${pkgs.nix}/bin/nix store diff-closures \
          /nix/var/nix/profiles/system ./result 2>/dev/null || \
          echo "(diff not available — build first)"
        ;;

      *)
        echo "Usage: hart-self-build {switch|dry-run|diff}"
        exit 1
        ;;
    esac
  '';

  # Package management helper
  pkgScript = pkgs.writeShellScriptBin "hart-pkg" ''
    #!/usr/bin/env bash
    # HART OS Runtime Package Manager
    # Modifies /etc/hart/runtime.nix and triggers self-build

    RUNTIME_NIX="/etc/hart/runtime.nix"

    case "''${1:-help}" in
      install|add)
        shift
        if [[ $# -eq 0 ]]; then
          echo "Usage: hart pkg install <package> [package...]"
          exit 1
        fi
        for PKG in "$@"; do
          # Verify package exists in nixpkgs
          if ! ${pkgs.nix}/bin/nix eval "nixpkgs#$PKG.name" --impure 2>/dev/null; then
            echo "Package not found in nixpkgs: $PKG"
            continue
          fi
          # Check if already in runtime.nix
          if ${pkgs.gnugrep}/bin/grep -q "^\s*$PKG\b" "$RUNTIME_NIX" 2>/dev/null; then
            echo "Already installed: $PKG"
            continue
          fi
          # Add to runtime.nix (insert before the closing bracket of systemPackages)
          ${pkgs.gnused}/bin/sed -i "/# Packages added at runtime appear here/a\\    $PKG" "$RUNTIME_NIX"
          echo "Added: $PKG"
        done
        echo ""
        echo "Run 'hart-ota self-build' to apply changes"
        ;;

      remove|uninstall)
        shift
        if [[ $# -eq 0 ]]; then
          echo "Usage: hart pkg remove <package> [package...]"
          exit 1
        fi
        for PKG in "$@"; do
          if ${pkgs.gnugrep}/bin/grep -q "^\s*$PKG\b" "$RUNTIME_NIX" 2>/dev/null; then
            ${pkgs.gnused}/bin/sed -i "/^\s*$PKG\b/d" "$RUNTIME_NIX"
            echo "Removed: $PKG"
          else
            echo "Not in runtime config: $PKG"
          fi
        done
        echo ""
        echo "Run 'hart-ota self-build' to apply changes"
        ;;

      list)
        echo "=== Runtime-installed packages ==="
        ${pkgs.gnugrep}/bin/grep -E '^\s+\S+$' "$RUNTIME_NIX" 2>/dev/null | \
          ${pkgs.gnugrep}/bin/grep -v '^#' | \
          ${pkgs.gnugrep}/bin/grep -v 'systemPackages\|with pkgs\|environment\|services\|variables' | \
          ${pkgs.gnused}/bin/sed 's/^\s*/  /' || echo "  (none)"
        ;;

      search)
        shift
        ${pkgs.nix}/bin/nix search nixpkgs "$@" --impure 2>/dev/null || \
          echo "Search failed (requires network)"
        ;;

      edit)
        ''${EDITOR:-nano} "$RUNTIME_NIX"
        ;;

      help|--help|-h)
        echo "hart-pkg — HART OS Runtime Package Manager"
        echo ""
        echo "Commands:"
        echo "  hart-pkg install <pkg>    Add package to runtime config"
        echo "  hart-pkg remove <pkg>     Remove package from runtime config"
        echo "  hart-pkg list             List runtime-installed packages"
        echo "  hart-pkg search <term>    Search nixpkgs for a package"
        echo "  hart-pkg edit             Edit runtime.nix directly"
        echo ""
        echo "After install/remove, run 'hart-ota self-build' to apply."
        echo "Or use 'hart-pkg install <pkg> && hart-ota self-build' for one-shot."
        ;;

      *)
        echo "Unknown command: $1 (try: hart-pkg help)"
        exit 1
        ;;
    esac
  '';
in
{
  options.hart.selfBuild = {
    enable = lib.mkEnableOption "HART OS runtime self-build";

    allowAgentBuilds = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Allow AI agents to trigger self-builds.
        When true, agents can install packages and rebuild the OS.
        Requires human approval by default (agent proposes, user confirms).
      '';
    };

    autoRebuild = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Automatically rebuild after runtime.nix changes.
        If false (default), changes are staged until manual `hart-ota self-build`.
      '';
    };

    maxBuildsPerDay = lib.mkOption {
      type = lib.types.int;
      default = 10;
      description = "Maximum self-builds per day (prevents rebuild loops)";
    };
  };

  config = lib.mkIf (cfg.enable && sb.enable) {

    # Seed runtime config on first boot
    systemd.services.hart-self-build-seed = {
      description = "HART OS Self-Build Configuration Seed";
      wantedBy = [ "multi-user.target" ];
      before = [ "hart-backend.service" ];

      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = seedRuntimeConfig;
      };
    };

    # Install CLI tools
    environment.systemPackages = [
      pkgScript
      (pkgs.writeShellScriptBin "hart-self-build" ''
        exec ${selfBuildScript} "$@"
      '')
    ];

    # Extend hart-ota CLI with self-build command
    # (hart-ota self-build delegates to hart-self-build)

    # Include runtime.nix as a NixOS module
    # This is the key: the flake evaluates this file on every rebuild
    imports = lib.optional
      (builtins.pathExists "/etc/hart/runtime.nix")
      /etc/hart/runtime.nix;

    # Filesystem watcher for auto-rebuild (optional)
    systemd.paths.hart-self-build-watch = lib.mkIf sb.autoRebuild {
      description = "Watch runtime.nix for changes";
      wantedBy = [ "paths.target" ];
      pathConfig = {
        PathChanged = "/etc/hart/runtime.nix";
        MakeDirectory = true;
      };
    };

    systemd.services.hart-self-build-watch = lib.mkIf sb.autoRebuild {
      description = "HART OS Auto Self-Build on Config Change";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${selfBuildScript} switch";
        StandardOutput = "journal";
        SyslogIdentifier = "hart-self-build";
      };
    };
  };
}

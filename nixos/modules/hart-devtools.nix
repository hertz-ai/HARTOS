# HART OS — Developer Tools
#
# LSP servers, debuggers, linters/formatters, container tools.
# Category-based: each feature set independently toggleable.
# Base languages (Python, Node, Rust, Go, Java) are in hart-desktop.nix.
#
# CLI: hart-dev status|lsp|help

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.devtools;
in
{
  options.hart.devtools = {
    enable = lib.mkEnableOption "HART OS developer tools";

    lsp = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Language Server Protocol servers.";
    };

    debug = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Debuggers (gdb, lldb, delve, debugpy).";
    };

    lint = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Linters and formatters.";
    };

    containers = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Container tools (podman, buildkit). Disabled by default.";
    };

    editors = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Terminal editors with LSP (neovim, helix).";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    # LSP servers
    (lib.mkIf cfg.lsp {
      environment.systemPackages = with pkgs; [
        clang-tools
        python310Packages.python-lsp-server
        gopls
        rust-analyzer
        nodePackages.typescript-language-server
        nodePackages.typescript
        nil
        nodePackages.yaml-language-server
      ];
    })

    # Debuggers
    (lib.mkIf cfg.debug {
      environment.systemPackages = with pkgs; [
        gdb lldb delve
        python310Packages.debugpy
        strace ltrace valgrind
      ];
      boot.kernel.sysctl."kernel.yama.ptrace_scope" = 0;
    })

    # Linters / formatters
    (lib.mkIf cfg.lint {
      environment.systemPackages = with pkgs; [
        python310Packages.pylint
        python310Packages.black
        python310Packages.flake8
        python310Packages.mypy
        nodePackages.eslint
        nodePackages.prettier
        golangci-lint
        shellcheck shfmt
        nixpkgs-fmt
      ];
    })

    # Container tools (rootless podman)
    (lib.mkIf cfg.containers {
      environment.systemPackages = with pkgs; [
        podman skopeo dive
      ];
      virtualisation.podman = {
        enable = true;
        dockerCompat = true;
        defaultNetwork.settings.dns_enabled = true;
      };
    })

    # Editors
    (lib.mkIf cfg.editors {
      environment.systemPackages = with pkgs; [ neovim helix ];
    })

    # CLI tool
    {
      environment.systemPackages = [
        (pkgs.writeShellScriptBin "hart-dev" ''
          case "''${1:-status}" in
            status)
              echo "=== HART OS Developer Tools ==="
              echo "LSP:        ${if cfg.lsp then "enabled" else "disabled"}"
              echo "Debuggers:  ${if cfg.debug then "enabled" else "disabled"}"
              echo "Linters:    ${if cfg.lint then "enabled" else "disabled"}"
              echo "Containers: ${if cfg.containers then "enabled" else "disabled"}"
              echo "Editors:    ${if cfg.editors then "enabled" else "disabled"}"
              echo ""
              python3 --version 2>/dev/null || echo "Python: not found"
              node --version 2>/dev/null || echo "Node.js: not found"
              go version 2>/dev/null || echo "Go: not found"
              rustc --version 2>/dev/null || echo "Rust: not found"
              ;;
            lsp)
              for cmd in clangd pylsp gopls rust-analyzer typescript-language-server nil; do
                if command -v "$cmd" >/dev/null 2>&1; then
                  echo "  [OK] $cmd"
                else
                  echo "  [--] $cmd"
                fi
              done
              ;;
            help|--help|-h)
              echo "hart-dev {status|lsp|help}"
              ;;
            *) echo "Unknown: $1 (try: hart-dev help)"; exit 1 ;;
          esac
        '')
      ];
    }
  ]);
}

# HART OS — IDE / editor tooling
#
# LSP servers, debuggers, linters/formatters, container tools, editors.
# Category-based: each feature set independently toggleable.
# Base languages (Python, Node, Rust, Go, Java) are hart.devTools, a DIFFERENT
# option owned by modules/hart-dev-tools.nix — see the rename note below.
#
# CLI: hart-dev status|lsp|help
#
# ═══ RENAMED 2026-08-04: hart.devtools -> hart.ideTools ═══
# The old name differed from hart.devTools by ONE CAPITAL. Nix treats those as
# two unrelated options and reports no conflict, so `hart.devtools.enable =
# true` silently configured whichever module the author did not mean, and a
# closure audit attributed gigabytes to the wrong feature — which nearly
# happened: the measured 2.8 GiB was one step from being recorded against the
# module that ships gcc and jdk21.
#
# The two are genuinely different things and both are justified, so merging was
# never the answer; the NAME was the defect. `ideTools` says what this module
# actually is — the editing environment (LSP, debuggers, linters, containers,
# editors) — as opposed to the compilers in hart.devTools.
#
# Every leaf gets a mkRenamedOptionModule alias, so an existing config setting
# hart.devtools.* keeps evaluating and gets a deprecation warning naming the
# new path, rather than the silent-wrong-module behaviour it has today.

{ config, lib, pkgs, ... }:

let
  cfg = config.hart.ideTools;
in
{
  imports = [
    # One alias per leaf: mkRenamedOptionModule renames a single option, not a
    # subtree, so a bare [ "hart" "devtools" ] would leave every child
    # unaliased and silently undefined.
    (lib.mkRenamedOptionModule [ "hart" "devtools" "enable" ]             [ "hart" "ideTools" "enable" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "lsp" ]                [ "hart" "ideTools" "lsp" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "debug" ]              [ "hart" "ideTools" "debug" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "lint" ]               [ "hart" "ideTools" "lint" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "containers" ]         [ "hart" "ideTools" "containers" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "editors" ]            [ "hart" "ideTools" "editors" ])
    (lib.mkRenamedOptionModule [ "hart" "devtools" "ptraceUnrestricted" ] [ "hart" "ideTools" "ptraceUnrestricted" ])
  ];

  options.hart.ideTools = {
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

    ptraceUnrestricted = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Let ANY process ptrace any other process of the same user
        (kernel.yama.ptrace_scope = 0) instead of direct children only.

        SPLIT OUT OF `debug` (2026-07-30). The debugger BUNDLE used to open
        ptrace unconditionally, and hart-security documents that as correct
        for "a debug box". It stopped being correct the moment the desktop
        PROFILE enabled devtools for every shipped machine: an OS-wide
        posture change arrived as a side effect of installing gdb, silently
        overriding hart-security's `mkDefault 1` (a plain definition beats
        mkDefault) and quietly contradicting the assertion in
        nixos/tests/security.nix that ptrace_scope is 1.

        Neither Windows nor macOS ships this open — Windows gates it behind
        SeDebugPrivilege, macOS behind SIP and entitlements — so restricted
        IS the parity-correct default.

        Nothing is lost by defaulting it off: gdb/lldb/delve still debug
        processes they LAUNCH, which is the overwhelmingly common case.
        Attaching to an ALREADY-RUNNING unrelated process is what needs
        this, and that is a deliberate choice a developer box opts into.
      '';
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
    })

    # Opening ptrace is now its OWN opt-in, never a side effect of having a
    # debugger installed. mkForce because it must deliberately beat
    # hart-security's mkDefault 1 — an explicit override of a hardening is
    # exactly the thing that should be loud in the source, not implicit in a
    # bundle. See the ptraceUnrestricted option for why the default flipped.
    (lib.mkIf cfg.ptraceUnrestricted {
      boot.kernel.sysctl."kernel.yama.ptrace_scope" = lib.mkForce 0;
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

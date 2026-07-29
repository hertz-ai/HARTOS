# ═══════════════════════════════════════════════════════════════
# HART OS — developer toolchain (one writer, four consumers)
# ═══════════════════════════════════════════════════════════════
#
# The "all major languages, native" toolchain the desktop advertises. It lived
# as two raw lines inside configurations/desktop.nix's big systemPackages list,
# which meant: the ISO/raw images had it, the INSTALLED system (profile +
# hardware, task #17) silently did NOT, the desktop-boot nixosTest could only
# assert it by duplicating the list, and edge had no way to say "off" because
# there was nothing to turn off. One option, four consumers:
#
#   profiles/desktop.nix   -> enable = true   (every desktop: iso, raw, installed)
#   profiles/edge.nix      -> stays off       (edge is minimal by definition)
#   tests/vm-tests.nix     -> enable = true   (asserts `which gcc` etc. behaviourally)
#   configurations/*       -> nothing (the raw lines are deleted, not mirrored)
#
# CLOSURE COST (task #14, measured 2026-07-28): this list is most of the
# "unexplained toolchains" in the 22 GiB desktop closure — gcc 221 MiB,
# go 227 MiB, jdk21 ~1.1 GiB (both outputs), rustup small but pulls toolchains
# at runtime. It is DELIBERATE product surface (the coding agent + "OS
# completeness" preinstall principle), not a leak — but any future size
# decision now has exactly one place to make it.
{ config, lib, pkgs, ... }:

let
  cfg = config.hart.devTools;
in
{
  options.hart.devTools = {
    enable = lib.mkEnableOption ''
      the preinstalled developer toolchain (git, C/C++, Python, Node, Rust via
      rustup, Go, JDK). Variant profiles enable it; edge stays minimal
    '';
  };

  config = lib.mkIf cfg.enable {
    # Moved VERBATIM from configurations/desktop.nix (2026-07-29); the comment
    # there dated to the OS-completeness preinstall work.
    environment.systemPackages = with pkgs; [
      # ── Development (all major languages, native) ──
      git gcc gnumake cmake
      python310 nodejs_20 rustup go jdk21
    ];
  };
}

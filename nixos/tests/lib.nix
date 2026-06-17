# ═══════════════════════════════════════════════════════════════
# Shared helpers for HART OS NixOS VM integration tests
# ═══════════════════════════════════════════════════════════════
#
# Single source of truth for the minimal `mkNode` builder used by EVERY
# `nixosTest` node (vm-tests.nix, floor-lock.nix, and later phases). Factored
# out so no test file re-pastes the #70-safe node construction — one writer for
# "how a hart test node is built", per the DRY gate.
#
# The #70 fix lives here: nodes are built from the hart modules alone with the
# variant enabled (modules are variant-gated on cfg.variant), NOT by importing
# ../configurations/X.nix — which would drag in the installer-CD overlay and make
# `nix flake check` un-EVALUABLE ("nixpkgs.overlays defined multiple times").
# See vm-tests.nix's header for the full incident write-up.

{ hartModules }:

{
  # mkNode variant extra -> a NixOS module function for one runNixOSTest node.
  #   variant : "server" | "desktop" | "edge" | ...
  #   extra   : a module (imported, NOT // merged) carrying per-test
  #             virtualisation / networking overrides.
  mkNode = variant: extra: { pkgs, lib, hartSrc, ... }: {
    imports = hartModules ++ [ extra ];
    hart.enable = true;
    hart.variant = variant;
    hart.version = "0.0.0-test";
    # hart.package has NO default; the minimal node must set it so the
    # variant services that read config.hart.package can evaluate.
    hart.package = pkgs.callPackage ../packages/hart-app.nix { inherit hartSrc; };
    # hart-base sets networking.hostName = mkDefault "hart-node"; runNixOSTest
    # also sets a same-priority default (the node name) -> conflict. Force a
    # deterministic per-node value (tests address by IP, not hostname).
    networking.hostName = lib.mkForce variant;
  };
}

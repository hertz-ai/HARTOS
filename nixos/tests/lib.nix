# ═══════════════════════════════════════════════════════════════
# Shared helpers for HART OS NixOS VM integration tests
# ═══════════════════════════════════════════════════════════════
#
# Single source of truth for the minimal `mkNode` builder used by EVERY
# `nixosTest` node (vm-tests.nix, floor-lock.nix, and later phases). Factored
# out so no test file re-pastes the #70-safe node construction — one writer for
# "how a hart test node is built", per the DRY gate.
#
# The #70 fix lives here: nodes are built from the hart modules + the VARIANT
# FEATURE PROFILE, NOT by importing ../configurations/X.nix — which would drag
# in the installer-CD overlay and make `nix flake check` un-EVALUABLE
# ("nixpkgs.overlays defined multiple times"). See vm-tests.nix's header for
# the full incident write-up.
#
# PROFILE WIRING (steward decision 2026-07-30, no feature flags): mkNode is the
# THIRD declared consumer of profiles/<variant>.nix (its header named this
# from day one — "nodes ran with every hart.* feature at default-false, which
# is why 25 nixosTests were red, #15" — but the wiring was deferred and never
# tracked; this closes it). Test nodes now compose the REAL variant: what a
# test boots is what an image and an installed system boot. A test that needs
# a leaf to differ from the variant baseline overrides it with mkForce at its
# own site — the profile is the baseline, never the other way around.
{ hartModules }:

{
  # mkNode variant extra -> a NixOS module function for one runNixOSTest node.
  #   variant : "server" | "desktop" | "edge" | ...
  #   extra   : a module (imported, NOT // merged) carrying per-test
  #             virtualisation / networking overrides.
  mkNode = variant: extra: { pkgs, lib, hartSrc, ... }: {
    imports = hartModules ++ [ ../profiles/${variant}.nix extra ];
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

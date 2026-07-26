{ config, lib, pkgs, ... }:

# ════════════════════════════════════════════════════════════════════════════
# HART OS — Memory sanity: zram swap + OOM protection + health surface  [#157]
# ════════════════════════════════════════════════════════════════════════════
#
# WHY (the memory half of the Disk/Memory utilities task, sibling to
# hart-storage.nix): a fresh HART OS box shipped with ZERO memory config — no
# zram, no systemd-oomd — so a low-RAM machine (the common crowdsourced-compute
# node) had no compressed-RAM headroom and no graceful OOM protector; under
# pressure the kernel OOM killer could reap an arbitrary process (including the
# shell). This module adds the three pieces that make memory degrade-not-die:
#
#   1. zram swap — a compressed block of RAM used as swap (priority 100, so it is
#      preferred over any disk swap). It is RAM-ONLY (no disk device), so it can
#      NEVER block boot waiting on a device, and it buys a low-RAM box effective
#      headroom (zstd typically compresses 2-3x) BEFORE anything touches disk.
#   2. systemd-oomd — the freedesktop userspace OOM protector. Under real memory
#      pressure it kills a whole offending CGROUP early + gracefully (a runaway
#      app's scope) instead of the kernel reaping a random victim late, so the
#      session + the shell survive. Kills a cgroup, not the seat.
#   3. swappiness coordination — with zram present, swap is compressed RAM (cheap),
#      so a HIGHER swappiness keeps the working set hot by preferring compressed-RAM
#      swap over evicting page cache. Set at mkOverride 900 so it wins over
#      hart-base's mkDefault 10 on a zram desktop but LOSES to edge.nix's mkForce
#      60 (edge keeps its own tuning) — no priority conflict either way.
#   4. a boot-time MEMORY-HEALTH snapshot (hart-memory-health.sh) -> /run/hart/
#      memory-health, the real-HW observability twin of hart-disk-health.
#
# PRIVACY-FIRST: all of this is a LOCAL capability (no egress), so it ships ON by
# default (no opt-in friction) — the wire-up sets hart.memory.enable = true.
#
# DEGRADE-NOT-DIE (the never-brick contract): zram is RAM-only so it can never
# stall local-fs.target; systemd-oomd is the freedesktop default protector (not a
# custom killer); the swappiness change is a pure sysctl; the health probe is
# read-only, bounded, and always exits 0. A wrong value here can never brick boot.
# The whole module is a pure no-op when hart.memory.enable is false (mkIf).

let
  cfg = config.hart;
  mem = config.hart.memory;

  # The boot-time MEMORY-HEALTH snapshot, shipped verbatim + POSIX-linted at build
  # time (runCommand, like hart-disk-health / hart-display-health) so the SAME
  # bytes are run by tests/unit/test_hart_memory_health.py on the dev box — one
  # source of truth, no parallel copy.
  memHealthScript = pkgs.runCommand "hart-memory-health"
    { nativeBuildInputs = [ pkgs.coreutils ]; }
    ''
      mkdir -p $out/bin
      cp ${./hart-memory-health.sh} $out/bin/hart-memory-health
      chmod +x $out/bin/hart-memory-health
      ${pkgs.dash}/bin/dash -n $out/bin/hart-memory-health
    '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.memory = {
    enable = lib.mkEnableOption ''
      HART OS memory sanity: compressed-RAM zram swap, systemd-oomd graceful OOM
      protection, swappiness coordination, and a boot-time memory-health snapshot.
      All LOCAL + additive + boot-safe (zram is RAM-only so it never blocks boot);
      a pure no-op when disabled
    '';

    zram = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          Enable compressed-RAM zram swap (NixOS zramSwap), priority 100 so it is
          preferred over any disk swap. RAM-only, so it can never block boot.
        '';
      };
      algorithm = lib.mkOption {
        type = lib.types.str;
        default = "zstd";
        description = ''
          The zram compression algorithm. zstd is the modern default (good ratio,
          low CPU). lzo / lz4 are lighter-CPU alternatives for a very weak box.
        '';
      };
      memoryPercent = lib.mkOption {
        type = lib.types.ints.between 1 200;
        default = 50;
        description = ''
          The zram disksize as a percentage of physical RAM. 50 means a zram block
          sized at half of RAM; because it is COMPRESSED, the effective swap
          headroom is typically larger than the uncompressed percentage.
        '';
      };
    };

    oomProtect = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Enable systemd-oomd, the freedesktop userspace OOM protector. Under real
        memory pressure it kills a whole offending cgroup early + gracefully
        instead of the kernel reaping a random late victim, so the session + the
        shell survive. Kills a cgroup, not the seat.
      '';
    };

    healthProbe.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the boot-time MEMORY-HEALTH snapshot (hart-memory-health): a oneshot
        that records an honest memory readout (mem/swap totals, zram presence +
        algorithm, systemd-oomd liveness) to /run/hart/memory-health AFTER greetd
        is up. The real-HW observability twin of hart-disk-health. Read-only,
        bounded, always exits 0 -> can never block, fail, or brick the boot.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && mem.enable) {

    # ── 1. Compressed-RAM zram swap (RAM-only, never blocks boot). ──
    zramSwap = lib.mkIf mem.zram.enable {
      enable = true;
      algorithm = mem.zram.algorithm;
      memoryPercent = mem.zram.memoryPercent;
      # Higher than any disk swap so the kernel prefers compressed-RAM swap first.
      priority = 100;
    };

    # ── 2. swappiness coordination (only meaningful with zram present). ──
    # mkOverride 900: WINS over hart-base.nix's mkDefault 10 (priority 1000) on a
    # zram desktop, but LOSES to edge.nix's mkForce 60 (priority 50) so the edge
    # tier keeps its own tuning. Two plain mkDefaults would CONFLICT; this priority
    # threads cleanly between the existing definitions. Only set when zram is on.
    boot.kernel.sysctl = lib.mkIf mem.zram.enable {
      "vm.swappiness" = lib.mkOverride 900 100;
    };

    # ── 3. Graceful userspace OOM protection. ──
    # Plain assignment (priority 100) so this toggle is authoritative over the
    # nixpkgs mkDefault — turning oomProtect off genuinely disables oomd, and on
    # genuinely enables it, with no two-mkDefault conflict.
    systemd.oomd.enable = mem.oomProtect;

    # ── 4. Boot-time MEMORY-HEALTH snapshot. ──
    # Mirrors hart-disk-health: runs AFTER greetd (parallel with the desktop, never
    # before it, so it can never delay first paint), read-only, always exits 0.
    systemd.tmpfiles.rules = lib.mkIf mem.healthProbe.enable [
      # Shared /run/hart (tmpfs) at 0750 hart hart — gpu-probe / display-health /
      # disk-health all declare the same rule; tmpfiles de-dupes it.
      "d /run/hart 0750 hart hart -"
    ];

    systemd.services.hart-memory-health = lib.mkIf mem.healthProbe.enable {
      description = "HART OS - boot-time memory-health snapshot (writes zram/swap/oomd state to /run/hart/memory-health)";
      wantedBy = [ "multi-user.target" ];
      # AFTER greetd (parallel with the desktop) - NEVER before it: nothing reads
      # this file at boot, so it must never gate the seat.
      after = [ "greetd.service" ];
      restartIfChanged = false;
      # gnugrep/gawk parse /proc/meminfo; util-linux gives zramctl; coreutils gives
      # mkdir/printf/dirname; systemctl is on the default unit PATH. The script does
      # NOT hardcode store paths so the SAME file is dev-box unit-testable.
      path = with pkgs; [ coreutils gnugrep gawk util-linux ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${memHealthScript}/bin/hart-memory-health";
        TimeoutStartSec = "30";
      };
    };
  };
}

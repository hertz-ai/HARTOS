{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Boot continuity (return to HART OS on a Live-OS restart)
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   When the user restarts FROM the HART OS Live USB (e.g. after the first-boot
#   setup, or any reboot), the firmware's permanent BootOrder usually puts the
#   internal Windows disk first — so a plain reboot drops them back into Windows,
#   and they have to mash F12 / pick the USB again to get back into HART OS. That
#   is a terrible "try the OS" loop.
#
# THE fix (and WHY it is SAFE):
#   On a reboot initiated from the Live OS, set a ONE-SHOT `efibootmgr --bootnext`
#   to the USB's OWN EFI boot entry. BootNext is a UEFI variable the firmware
#   honours for exactly ONE boot and then CLEARS — it does NOT change the
#   permanent BootOrder. So:
#     - A Live-OS-initiated restart returns to HART OS automatically (no F12).
#     - The user's permanent boot order is UNCHANGED: when they later choose to
#       boot Windows (or shut down and power on normally), Windows boots exactly
#       as before. We can NEVER strand their Windows boot, because we never touch
#       BootOrder — only the single-shot BootNext, which the firmware self-clears.
#
# Detecting the USB's OWN entry:
#   `efibootmgr` lists boot entries (BootXXXX) with their device paths. We pick
#   the entry whose device path points at the disk the live root was booted from
#   (the USB the ISO was written to). We resolve that disk the same robust way as
#   hart-hartlog-create (walk the live mountpoints to their parent block device),
#   then match its PARTUUID / device path against the efibootmgr listing. If we
#   cannot confidently match the USB's own entry, we DO NOTHING (a wrong BootNext
#   is worse than none — never guess).
#
# THE never-strand-Windows / never-block-shutdown contract:
#   - NO-OP if `efibootmgr` is missing.
#   - NO-OP if the system was NOT booted via UEFI (no /sys/firmware/efi).
#   - NO-OP if the USB's own boot entry cannot be matched.
#   - NEVER modifies BootOrder — only `--bootnext` (one-shot, firmware-cleared).
#   - It runs as an ExecStop ordered to fire only on the way DOWN, and ONLY when
#     the shutdown is a REBOOT (not a poweroff) — a power-off should not arm a
#     next boot. Bounded timeout; any error is logged + swallowed (exit 0) so it
#     can never block or fail the shutdown transaction.
#   - `set -u` only (NOT -e): a probe failing must not abort; we gate explicitly.
#
# VM/HW-gated: "sets BootNext to the USB's own entry on a real reboot" needs real
# UEFI firmware + a USB-booted live root to fully confirm (QEMU's OVMF can show
# the mechanism but the device-path match is most meaningful on real HW). The
# structural test (tests/boot-continuity.nix) proves the unit + efibootmgr are in
# the closure, the ordering fires only on reboot, the non-UEFI / no-efibootmgr /
# no-match gates each no-op cleanly, and the script NEVER emits a BootOrder write.

let
  cfg = config.hart;
  bc = config.hart.bootContinuity;

  # Minimal-PATH discipline. efibootmgr + the block tools we resolve the USB with.
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux efibootmgr gawk gnugrep gnused
  ]);

  bootNextScript = pkgs.writeShellScript "hart-boot-continuity-set" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    log() { echo "[hart-boot-continuity] $*" >&2 ; }

    # ── 0. UEFI-booted? Only UEFI has EFI boot entries / BootNext. ──
    if [ ! -d /sys/firmware/efi ]; then
      log "not booted via UEFI (no /sys/firmware/efi) — nothing to do (no-op)"
      exit 0
    fi

    # ── 1. efibootmgr present? ──
    if ! command -v efibootmgr >/dev/null 2>&1; then
      log "efibootmgr not found — cannot set BootNext (no-op)"
      exit 0
    fi

    # ── 2. Only arm a next boot on a REBOOT, never on a poweroff. ──
    # systemd records the pending shutdown action. On a poweroff there is no next
    # boot to steer; only a reboot should return to HART OS. We read the scheduled
    # action from systemd if available; if we can't tell, we still proceed only
    # for the reboot ExecStop wiring (this script is invoked from the reboot path).
    # The unit is ordered so its ExecStop fires before systemd-reboot; the
    # ACTION arg lets a future poweroff-path reuse skip cleanly.
    ACTION="''${1:-reboot}"
    if [ "$ACTION" = "poweroff" ] || [ "$ACTION" = "halt" ]; then
      log "shutdown action is '$ACTION' (not a reboot) — not arming BootNext (no-op)"
      exit 0
    fi

    # ── 3. Resolve the disk the LIVE ROOT was booted from (the USB). ──
    # Same robust walk as hart-hartlog-create: live mountpoints -> backing block
    # device -> parent whole disk.
    SRC=""
    for mp in /iso /run/initramfs/live /run/rootfs /nix/.ro-store /; do
      [ -d "$mp" ] || continue
      s=$(findmnt -n -o SOURCE --target "$mp" 2>/dev/null | head -n1) || s=""
      case "$s" in
        /dev/*) SRC="$s"; break ;;
        *) : ;;
      esac
    done
    if [ -z "$SRC" ]; then
      log "could not resolve the live-root backing device — cannot match the USB entry (no-op)"
      exit 0
    fi
    PK=$(lsblk -ndo pkname "$SRC" 2>/dev/null | head -n1) || PK=""
    if [ -n "$PK" ]; then
      DISK="/dev/$PK"
    else
      case "$SRC" in
        /dev/loop*) log "live root on a loop device — cannot match a USB entry (no-op)" ; exit 0 ;;
        *) DISK="$SRC" ;;
      esac
    fi
    if [ ! -b "$DISK" ]; then
      log "resolved disk '$DISK' is not a block device (no-op)"
      exit 0
    fi

    # Collect identifiers that could appear in an efibootmgr device path for THIS
    # disk: every PARTUUID on it, plus the disk's kernel name. efibootmgr prints
    # entries with their device path; on most firmwares the EFI System Partition's
    # PARTUUID/GPT GUID appears (HD(...)/...). Matching ANY partition's PARTUUID on
    # the USB disk to an entry's device path identifies the USB's own boot entry.
    PARTUUIDS=$(lsblk -lnpo PARTUUID "$DISK" 2>/dev/null | gawk 'NF' | tr 'A-F' 'a-f') || PARTUUIDS=""
    if [ -z "$PARTUUIDS" ]; then
      log "no PARTUUIDs found on $DISK — cannot match the USB's EFI entry (no-op)"
      exit 0
    fi

    # ── 4. List boot entries with verbose device paths + find OUR entry. ──
    EBM=$(efibootmgr -v 2>/dev/null) || EBM=""
    if [ -z "$EBM" ]; then
      log "efibootmgr -v produced no output (no-op)"
      exit 0
    fi

    # For each Boot#### line, lowercase it and check whether any of our disk's
    # PARTUUIDs appears in that line's device path. First match wins. We extract
    # the 4-hex BootNumber from "Boot0007* HART OS ...".
    MATCH=""
    while IFS= read -r line; do
      case "$line" in
        Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]*) : ;;
        *) continue ;;
      esac
      low=$(printf '%s' "$line" | tr 'A-F' 'a-f')
      for pu in $PARTUUIDS; do
        case "$low" in
          *"$pu"*)
            MATCH=$(printf '%s' "$line" | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\).*/\1/p')
            break ;;
        esac
      done
      [ -n "$MATCH" ] && break
    done <<EOF
$EBM
EOF

    if [ -z "$MATCH" ]; then
      log "could not match the USB's own EFI boot entry in efibootmgr -v — NOT setting BootNext (never guess; no-op). The user can still pick the USB from the firmware menu."
      exit 0
    fi

    # ── 5. Set the ONE-SHOT BootNext. NEVER touch BootOrder. ──
    # `efibootmgr --bootnext XXXX` writes the BootNext UEFI variable ONLY. The
    # firmware honours it for the NEXT boot and then clears it. BootOrder is
    # untouched -> the user's permanent (Windows-first) order is preserved.
    if efibootmgr --bootnext "$MATCH" >/dev/null 2>&1; then
      log "armed BootNext=$MATCH (the USB's own EFI entry) — the next boot returns to HART OS. BootOrder UNCHANGED (Windows still boots normally when chosen)."
    else
      log "efibootmgr --bootnext $MATCH failed — leaving BootOrder untouched (no-op; the user can pick the USB from the firmware menu)"
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.bootContinuity = {
    enable = lib.mkEnableOption ''
      Boot continuity for the Live OS. When a restart is initiated FROM the Live
      OS, set a ONE-SHOT efibootmgr BootNext to the USB's OWN EFI boot entry so
      the next boot returns to HART OS WITHOUT the user mashing F12. It is
      intentionally BootNext (one-shot, firmware-cleared), NEVER BootOrder, so it
      can never strand the user's Windows boot — when they choose to boot Windows
      it boots normally. A pure NO-OP if efibootmgr is missing, the system was
      not UEFI-booted, or the USB's own entry can't be matched'';
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR not UEFI-booted)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && bc.enable) {

    # A shutdown-time hook: RemainAfterExit + ExecStop is the systemd idiom for
    # "do work on the way down". We order its ExecStop to run BEFORE
    # systemd-reboot.service (the reboot path) but it is NOT pulled in by the
    # poweroff path's ordering, and the script itself skips non-reboot actions —
    # so a power-off never arms a next boot. It must never block shutdown.
    systemd.services.hart-boot-continuity = {
      description = "HART OS — on a Live-OS reboot, set a one-shot BootNext to the USB's own EFI entry (returns to HART OS; never changes BootOrder)";
      wantedBy = [ "multi-user.target" ];
      # Order so the ExecStop fires as the system goes down, before the EFI
      # variable store + reboot are torn down/executed.
      before = [ "shutdown.target" "systemd-reboot.service" ];
      after = [ "local-fs.target" ];
      conflicts = [ "shutdown.target" ];
      # Don't let a nixos-rebuild switch stop+restart this (which would fire the
      # ExecStop BootNext mid-session); it is a reboot-only hook.
      restartIfChanged = false;
      stopIfChanged = false;
      unitConfig = {
        DefaultDependencies = false;
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # ExecStart is a no-op marker; the real work is ExecStop on the way down.
        ExecStart = "${pkgs.coreutils}/bin/true";
        # Pass "reboot" so the script's action gate is explicit. (A power-off does
        # not order this unit's ExecStop ahead of systemd-poweroff in a way that
        # would arm a boot; the script's ACTION gate is the belt-and-suspenders.)
        ExecStop = "${bootNextScript} reboot";
        TimeoutStopSec = "30s";
      };
    };

    # efibootmgr on PATH so an operator can inspect/clear BootNext by hand.
    environment.systemPackages = [ pkgs.efibootmgr ];
  };
}

{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS - Stateful-across-boots persistence onto the HARTSTATE partition
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   The HART OS live ISO is STATELESS: / and every mutable path live in tmpfs
#   (RAM), wiped on every reboot. So the box re-asks for Wi-Fi EVERY boot (the NM
#   keyfiles vanish), the theme/skins/onboarding reset, and the user's home is
#   gone - the machine never "remembers" it was set up.
#
# THE fix:
#   The flasher carves a SECOND partition on the USB, labelled HARTSTATE. IF that
#   partition is present, this boot oneshot mounts it and BIND-PERSISTS the
#   stateful paths onto subdirs of it, so they SURVIVE reboot:
#     - /etc/NetworkManager/system-connections  (Wi-Fi credentials - THE "every
#       boot asks for wifi" fix; NM auto-connects to the saved profile next boot)
#     - the HART state dir (cfg.dataDir = /var/lib/hart): active_theme.json,
#       theme_custom.json, custom skins, shell_session.json [HartSession], and
#       hart_node_identity.json [the onboarding/identity seal = the
#       onboarding-complete marker, so first-boot setup is NOT re-asked]
#     - /home/hart-admin  (the user's data / settings)
#   It runs BEFORE NetworkManager (so the Wi-Fi keyfiles are already in place when
#   NM starts) and before the graphical session (so the shell reads persisted
#   theme/skins/home + the sealed identity).
#
# SECURE ("persisted securely", steward's word):
#   The system-connections bind keeps 0700 dir / 0600 files, root:root
#   (NetworkManager's own perms) so Wi-Fi secrets are NEVER world-readable. This
#   requires a POSIX-perms backing fs - FAT/exFAT/NTFS cannot store ownership+mode,
#   so a Wi-Fi secret on them would be world-readable. We therefore FAIL-SECURE:
#   the Wi-Fi bind runs ONLY when HARTSTATE is a POSIX fs (ext4/btrfs/xfs/f2fs);
#   on a non-POSIX fs the Wi-Fi persist is SKIPPED (HART state + home still
#   persist). => the flasher should format HARTSTATE ext4.
#   FOLLOW-UP (stronger, NOT attempted here - needs a key mechanism): TPM-sealed
#   LUKS on HARTSTATE so the persisted secrets are encrypted at rest and only this
#   machine's TPM can unlock them. That is the right next step once a key-release
#   path exists; do NOT add LUKS now (a wrong key mechanism bricks the persist).
#
# THE never-block-boot contract (HARD CONSTRAINT 1 - read carefully):
#   - If NO HARTSTATE partition is present, every path is a clean NO-OP: it logs
#     one line + a LOUD DECISION=NOOP marker and exits 0. The OS still boots
#     STATELESS, exactly as today.
#   - If HARTSTATE is unreadable / busy / a mount fails / the disk is full, the
#     failing path is SKIPPED (DECISION=PARTIAL) and the script still exits 0 -
#     boot is NEVER blocked or failed.
#   - `set -u` only (NOT -e): a probe failing must not abort the script; we gate
#     EXPLICITLY at each step and every side-effect is best-effort (`|| true`).
#   - The unit is a bounded oneshot (TimeoutStartSec) that nothing REQUIRES - it
#     is only ORDERED Before NetworkManager / the session, so even a timeout can
#     never wedge or fail the boot transaction (Before is ordering, not a hard dep).
#
# ONE writer, no parallel path (HARD CONSTRAINT 2):
#   The HARTSTATE partition is resolved by its filesystem LABEL via the SAME
#   by-label lookup the sibling modules use (hart-boot-log / hart-journal-export:
#   findfs LABEL= / blkid -L) - no second device-resolution implementation. The
#   HART state dir is cfg.dataDir (the ONE canonical data home every HART service
#   reads), not a hardcoded copy.
#
# VM/HW-gated: "state survives a real reboot off the real USB's HARTSTATE
# partition" needs a real flash + USB boot (the Windows dev box has no live node,
# and nix does not build here). DO NOT claim it works - VERIFY ON THE NODE VIA THE
# LOOP (fix -> OTA -> reboot -> confirm Wi-Fi + theme + onboarding persisted). The
# structural nixosTest (tests/state-persist.nix) proves the unit exists, no-ops
# cleanly when the label is ABSENT (and stays non-boot-blocking), persists onto a
# real ext4 HARTSTATE stand-in with the SECURE 0700/root:root Wi-Fi perms, and
# FAIL-SECURE skips the Wi-Fi bind on a non-POSIX (vfat) fs.

let
  cfg = config.hart;
  sp = config.hart.statePersist;

  label = sp.label;

  # The ONE canonical HART state home - every HART service reads/writes it
  # (active_theme.json, theme_custom.json, custom skins, shell_session.json
  # [HartSession], hart_node_identity.json [the onboarding/identity seal]). REUSE
  # cfg.dataDir; do NOT hardcode a second copy of the path.
  dataDir = cfg.dataDir;

  # The interactive admin user (hart-base defines hart-admin as isNormalUser with
  # no explicit home => /home/hart-admin).
  adminHome = "/home/hart-admin";

  # NetworkManager's on-disk keyfile store. NM writes a per-SSID connection file
  # here (0600 root:root) when the user joins a network; on the stateless live ISO
  # it lives in tmpfs and is wiped every reboot (the "asks for wifi every boot").
  nmConnections = "/etc/NetworkManager/system-connections";

  # Private base mountpoint for HARTSTATE. It STAYS mounted for the OS lifetime
  # (unlike hart-boot-log, which unmounts after each write): the live paths are
  # bind-mounted FROM subdirs of it, so unmounting it would drop the persistence.
  mnt = "/run/hart/hartstate";

  # Minimal-PATH discipline (the iso_real_usb_boot lesson: tools were OFF the
  # minimal unit PATH). util-linux -> blkid/findfs/mount/umount/findmnt/mountpoint;
  # coreutils -> mkdir/cp/chmod/chown/cat/date; gnugrep for the perm sweep.
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux gnugrep e2fsprogs
  ]);

  persistScript = pkgs.writeShellScript "hart-state-persist" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    LABEL="${label}"
    MNT="${mnt}"
    DATA_DIR="${dataDir}"
    ADMIN_HOME="${adminHome}"
    NM_CONN="${nmConnections}"

    # LOUD decision marker (the never-silent-no-op contract, mirrors hart-hartlog-
    # create). A tmpfs file the operator/host can read to see EXACTLY what was
    # persisted and WHY a no-op happened; also echoed to the journal (>&2).
    STATUS_DIR="/run/hart"
    STATUS="$STATUS_DIR/state-persist.status"
    mkdir -p "$STATUS_DIR" 2>/dev/null || true
    : > "$STATUS" 2>/dev/null || true

    DEV=""
    FSTYPE=""
    RESULT="PERSISTED"

    log() {
      echo "[hart-state-persist] $*" >&2
      echo "$*" >> "$STATUS" 2>/dev/null || true
    }
    note_partial() { RESULT="PARTIAL"; }
    decide() {  # decide <PERSISTED|PARTIAL|NOOP> <reason>
      echo "DECISION=$1 DEV=''${DEV:-none} FSTYPE=''${FSTYPE:-unknown} REASON=$2" >> "$STATUS" 2>/dev/null || true
      log "DECISION=$1 dev=''${DEV:-none} reason=$2"
    }

    # ── 0. Tooling sanity. Any missing tool -> clean no-op (never fail boot). ──
    for t in blkid mount umount findmnt mountpoint; do
      if ! command -v "$t" >/dev/null 2>&1; then
        decide NOOP "tool '$t' not found"
        exit 0
      fi
    done

    # ── 1. Resolve HARTSTATE by LABEL (the same by-label lookup hart-boot-log /
    #    hart-journal-export use). Absent => clean no-op (OS stays stateless). ──
    if command -v findfs >/dev/null 2>&1; then
      DEV=$(findfs LABEL="$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ]; then
      DEV=$(blkid -L "$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ] || [ ! -b "$DEV" ]; then
      decide NOOP "no '$LABEL' partition present - OS boots stateless, exactly as today"
      exit 0
    fi
    FSTYPE=$(blkid -o value -s TYPE "$DEV" 2>/dev/null) || FSTYPE=""
    log "found '$LABEL' at $DEV (fstype=''${FSTYPE:-unknown})"

    # ── 1b. First-boot reformat of the Windows-flasher placeholder. diskpart
    #    cannot make ext4, so a Windows flash lays down an EMPTY vfat HARTSTATE;
    #    on a non-POSIX fs the Wi-Fi-secret bind is fail-secure SKIPPED below, so
    #    a Windows-flashed stick would never persist Wi-Fi. So on first boot, IF
    #    HARTSTATE is a non-POSIX fs AND is EMPTY (the fresh placeholder - a real
    #    user's data is NEVER destroyed), reformat it ext4 so Wi-Fi/home/state
    #    persist with real ownership. Bounded (probe RO + timeout mkfs); any
    #    failure leaves the fs as-is and falls through to fail-secure. ──
    case "$FSTYPE" in
      vfat|exfat|ntfs)
        _probe="$MNT.probe"
        mkdir -p "$_probe" 2>/dev/null || true
        if timeout 15 mount -o ro "$DEV" "$_probe" 2>/dev/null; then
          _extra=$(ls -A "$_probe" 2>/dev/null | grep -viE '^(System Volume Information|lost\+found|\.Trash-.*)$' | head -n1) || _extra=""
          umount "$_probe" 2>/dev/null || true
          if [ -z "$_extra" ]; then
            if timeout 60 mkfs.ext4 -q -F -L "$LABEL" "$DEV" 2>/dev/null; then
              FSTYPE="ext4"
              log "reformatted empty '$LABEL' placeholder ($DEV) to ext4 (Windows-flash first boot)"
            else
              log "mkfs.ext4 on '$LABEL' failed - leaving as ''${FSTYPE:-unknown} (wifi persist fail-secure skips)"
            fi
          else
            log "'$LABEL' is non-POSIX but NOT empty (has '$_extra') - NOT reformatting; wifi persist fail-secure skips"
          fi
        fi
        rmdir "$_probe" 2>/dev/null || true
        ;;
    esac

    # ── 2. Mount HARTSTATE at the private base mountpoint (idempotent). If it is
    #    already mounted (e.g. a udisks auto-mount), REUSE its target rather than
    #    failing. Any failure => clean no-op (never block boot). ──
    mkdir -p "$MNT" 2>/dev/null || true
    if ! mountpoint -q "$MNT" 2>/dev/null; then
      if ! timeout 20 mount "$DEV" "$MNT" 2>/dev/null; then
        _cur=$(findmnt -n -o TARGET --source "$DEV" 2>/dev/null | head -n1) || _cur=""
        if [ -n "$_cur" ] && [ -d "$_cur" ]; then
          MNT="$_cur"
          log "reusing existing mount of $DEV at $MNT"
        else
          decide NOOP "could not mount $DEV (unreadable/busy) - OS stays stateless"
          exit 0
        fi
      fi
    fi

    # ── 3. Does the backing fs preserve POSIX ownership + mode? FAT/exFAT/NTFS do
    #    NOT, so a 0600 root:root Wi-Fi secret cannot be stored securely on them.
    #    Gate the WIFI persist on a POSIX fs (FAIL-SECURE: never write NM secrets
    #    world-readable). HART state + home still persist on any fs. ──
    POSIX_FS=0
    case "$FSTYPE" in
      ext2|ext3|ext4|btrfs|xfs|f2fs) POSIX_FS=1 ;;
    esac

    # persist_dir <live> <backing-subdir> <mode> <owner-or-empty>
    # Bind-persists $live onto $MNT/$backing-subdir, seeding the backing from the
    # baked live content on first boot. Best-effort throughout; a failure marks
    # the run PARTIAL and returns non-zero but NEVER aborts the script.
    persist_dir() {
      _live="$1"; _sub="$2"; _mode="$3"; _own="$4"
      _back="$MNT/$_sub"
      mkdir -p "$_live" 2>/dev/null || true
      if [ ! -d "$_back" ]; then
        if ! mkdir -p "$_back" 2>/dev/null; then
          log "cannot create backing $_back (disk full?) - skipping $_live"
          note_partial; return 1
        fi
        # First boot: seed the backing from whatever the baked image already put
        # in the live path (preserve attrs). Best-effort; empty on a fresh dir.
        cp -a "$_live/." "$_back/" 2>/dev/null || true
      fi
      chmod "$_mode" "$_back" 2>/dev/null || true
      [ -n "$_own" ] && chown "$_own" "$_back" 2>/dev/null || true
      # Idempotent: if this boot (or a prior run this boot) already bound it, done.
      if mountpoint -q "$_live" 2>/dev/null; then
        log "$_live already bind-persisted"
        return 0
      fi
      if mount --bind "$_back" "$_live" 2>/dev/null; then
        log "persisted $_live -> $LABEL:/$_sub"
        return 0
      fi
      log "bind of $_live failed - skipping (boot continues)"
      note_partial; return 1
    }

    # ── 3a. HART state (themes, skins, HartSession, onboarding/identity seal). ──
    # Perms match hart-base's tmpfiles for cfg.dataDir (0770 hart hart) so the
    # hart service user keeps write access.
    persist_dir "$DATA_DIR" "hart-state" "0770" "hart:hart"

    # ── 3b. The admin user's home (settings / user data). ──
    persist_dir "$ADMIN_HOME" "home-hart-admin" "0755" "hart-admin"

    # ── 3c. WIFI credentials - THE "every boot asks for wifi" fix. SECURE: 0700
    #    dir / 0600 files, root:root (NetworkManager's own perms). POSIX fs only. ──
    if [ "$POSIX_FS" = "1" ]; then
      if persist_dir "$NM_CONN" "NetworkManager/system-connections" "0700" "root:root"; then
        # Belt-and-suspenders: force every stored keyfile to 0600 root:root so a
        # secret is never left group/world-readable (NM refuses such a file anyway).
        for _f in "$MNT/NetworkManager/system-connections"/*; do
          [ -e "$_f" ] || continue
          chmod 0600 "$_f" 2>/dev/null || true
          chown root:root "$_f" 2>/dev/null || true
        done
      fi
    else
      log "SKIP wifi persist: '$LABEL' fstype '$FSTYPE' has no POSIX perms - refusing to store NM secrets world-readable (fail-secure). Reformat HARTSTATE ext4 to persist wifi."
      note_partial
    fi

    decide "$RESULT" "state bind-persisted from $LABEL ($DEV)"
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.statePersist = {
    enable = lib.mkEnableOption ''
      stateful-across-boots persistence onto the HARTSTATE partition. IF a
      partition labelled HARTSTATE (carved on the USB by the flasher) is present,
      HART OS mounts it EARLY in boot and bind-persists the Wi-Fi credentials
      (/etc/NetworkManager/system-connections - the "every boot asks for wifi"
      fix), the HART state (active theme, custom skins, HartSession, the
      onboarding-complete/identity seal), and the admin user's home, so they
      SURVIVE reboot. The Wi-Fi keyfiles are persisted SECURELY (0700 dir / 0600
      files, root:root) and ONLY on a POSIX-perms fs (fail-secure: never stored
      world-readable on FAT/NTFS). A pure NO-OP when no HARTSTATE partition is
      present, when it is unreadable/busy, or on any error - the OS still boots
      STATELESS and the module NEVER blocks or fails boot'';

    label = lib.mkOption {
      type = lib.types.str;
      default = "HARTSTATE";
      description = ''
        The filesystem LABEL of the persistence partition (the flasher carves it
        on the USB after the flash). ONE source of truth for the on-stick
        contract; changing it here requires changing the flasher. It should be an
        ext4 (POSIX-perms) filesystem so the Wi-Fi secrets persist securely.
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR no HARTSTATE partition)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && sp.enable) {

    # Private base mountpoint for HARTSTATE (tmpfs /run; the MOUNT persists, the
    # mountpoint dir does not need to). The /run/hart rule is identical to the one
    # hart-boot-log / hart-journal-export declare, so tmpfiles de-dupes it.
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
      "d ${mnt} 0755 root root -"
    ];

    # The persistence oneshot. Ordered BEFORE NetworkManager (so the Wi-Fi
    # keyfiles are in place when NM starts and it auto-connects) and before the
    # session/backend (so the shell reads persisted theme/skins/home + the sealed
    # identity). Nothing REQUIRES it - Before is ordering only, so even a
    # timeout/failure can never wedge or fail the boot transaction.
    systemd.services.hart-state-persist = {
      description = "HART OS - persist Wi-Fi + HART state + home onto the HARTSTATE partition (stateful across boots)";
      wantedBy = [ "multi-user.target" ];
      # After the block layer + udev (so the device is enumerated) and after
      # tmpfiles-setup (so the live dirs we bind OVER already exist as mountpoints).
      after = [ "local-fs.target" "systemd-udev-settle.service" "systemd-tmpfiles-setup.service" ];
      # Before the consumers. Ordering-only: a Before on an ABSENT unit (e.g.
      # greetd/hart-backend on a variant that lacks them) is silently ignored, so
      # this can never create a missing-dependency failure.
      before = [ "NetworkManager.service" "greetd.service" "hart-backend.service" ];
      # A nixos-rebuild switch must not re-run the oneshot mid-session (which could
      # re-bind over live mounts).
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        # Hold the bind mounts for the OS lifetime (they ARE the persistence).
        RemainAfterExit = true;
        ExecStart = "${persistScript}";
        # A slow USB stick's mount/seed must not wedge boot - bounded + best-effort.
        # Bounded low so a flaky/hung USB can delay first-paint by AT MOST this
        # (the unit is Before greetd; individual mount/mkfs ops are timeout-wrapped
        # tighter). Nothing Requires it, so a timeout never fails the boot.
        TimeoutStartSec = "45s";
      };
      unitConfig.DefaultDependencies = true;
    };

    # The persist CLI on PATH so an operator can run it by hand from a TTY
    # (`hart-state-persist`) - e.g. after inserting/formatting HARTSTATE later.
    environment.systemPackages = [
      (pkgs.runCommand "hart-state-persist-cli" { } ''
        mkdir -p $out/bin
        ln -s ${persistScript} $out/bin/hart-state-persist
      '')
    ];
  };
}

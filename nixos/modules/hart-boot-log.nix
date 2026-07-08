{ config, lib, pkgs, ... }:

# ═══════════════════════════════════════════════════════════════
# HART OS — Persistent Boot-Diagnostic Log Partition
# ═══════════════════════════════════════════════════════════════
#
# THE problem this solves:
#   The HART OS live ISO's journal is in tmpfs (RAM) — it is WIPED on every
#   reboot and never lands on the USB stick, so a Windows host cannot read it
#   to debug a boot. Worse, the failure we most need to debug — the GTK4/Tier-1
#   "boots to only a mouse pointer" paint hang — leaves the compositor UP but
#   never settles, so the user can't hand-copy the journal out of a TTY either.
#
# THE fix:
#   If a partition labelled `HARTLOG` (vfat/FAT32) is present on any disk, this
#   module mounts it rw and writes a full diagnostic bundle to it — EARLY in
#   boot, AGAIN on a periodic timer (so a HUNG, never-settling boot still leaves
#   the journal-so-far), and ONCE MORE at shutdown. Each write fsync()s so an
#   abrupt power-off doesn't lose the bundle. FAT32 so the Windows host reads
#   the drive natively: plug the stick into Windows → the HARTLOG drive shows
#   up → open hart-boot-latest.log.
#
#   The flasher (scripts/hart_usb_flasher.py) creates the HARTLOG partition in
#   the stick's free space after a successful flash. So the loop is:
#     flash → boot (even if Tier-1 hangs) → plug into Windows → read the journal.
#
# ROBUSTNESS (the never-block-boot contract):
#   - If NO HARTLOG partition is present, every unit is a clean NO-OP: it logs
#     one line and exits 0. It never blocks, never slows, never fails boot.
#   - The capture is best-effort throughout (`|| true` on every probe) so a
#     missing tool / unavailable subsystem can never fail the unit.
#   - The mount is `nofail`-style (the script does the mount itself, guarded),
#     and the partition is synced + unmounted cleanly after each write.
#
# WHAT IT CAPTURES (the GTK4/Tier-1 paint-hang debug surface, specifically):
#   - `journalctl -b --no-pager` — the FULL current-boot journal.
#   - `systemctl --failed` + `systemctl status 'hart-*'` + their unit journals.
#   - The session-supervisor tier latch + crash window + which tier is active,
#     and the supervisor's own journal (the tier-drop decisions).
#   - Presence/absence + mtime of /run/hart/session/shell-ready (the paint
#     marker) — so "did the shell ever paint?" is answerable offline.
#   - The GTK4 host env + any GSK/GDK/EGL/GBM/WebKit GL errors from the journal,
#     so the GSK_RENDERER=cairo fix (75ba78d) is CONFIRMABLE from the host.
#   - dmesg tail, loginctl/active session, drm_info/the GPU if available.
#
# VM/HW-gated: the "writes the bundle to a real HARTLOG FAT32 partition" claim
# needs a real flash + boot to fully confirm (no HARTLOG partition exists on the
# Windows dev box). The structural test (tests/boot-log.nix) proves the units +
# tooling are in the closure, the no-HARTLOG path is a clean no-op, and the
# capture script parses under POSIX sh.

let
  cfg = config.hart;
  blog = config.hart.bootLog;

  # The label the flasher writes (scripts/hart_usb_flasher.py) — ONE source of
  # truth for the contract. vfat/FAT32 so the Windows host reads it natively.
  label = blog.label;

  # Where the HARTLOG partition is mounted while we write (a private mountpoint,
  # not a user-facing path). Created by tmpfiles below.
  mnt = "/run/hart/bootlog-mnt";

  # The supervisor's latch contract (kept in lockstep with
  # hart-session-supervisor.nix — same paths, read-only here).
  latchFile  = "/var/lib/hart/session-tier";
  windowFile = "/var/lib/hart/session-tier.window";
  readyFlag  = "/run/hart/session/shell-ready";
  # Written by the session-supervisor's write_tier on a DOWNWARD drop (from=X to=Y
  # ts=…); it both triggers this capture (the path unit below) and names the exact
  # fall-back in the bundle, so "why did it fall to cage" is answerable offline.
  degradeFlag = "/run/hart/session/tier-degraded";
  # The INPUT twin of the paint marker: hart-comp's note_input_alive (#134) touches
  # this on the FIRST real seat event, proving the libinput -> Seat delivery path is
  # live. Surfacing its presence/absence here is what tells a "pointer frozen at 0,0,
  # nothing types" boot (painted, dead seat) apart from a working desktop.
  inputAliveFlag = "/run/hart/session/input-alive";

  # Every tool referenced by absolute store path — the unit PATH is minimal and
  # several of these (lsblk, dmesg, drm_info, loginctl) are NOT on it (the
  # iso_real_usb_boot lesson: awk/lspci/xxd/curl were off the minimal unit PATH).
  binPath = lib.makeBinPath (with pkgs; [
    coreutils util-linux systemd kmod gnugrep gawk
    # pciutils -> lspci (network-class device enumeration); iproute2 -> ip (link
    # state). rfkill ships in util-linux (above). These power the NETWORK / WiFi /
    # rfkill section so "wifi chip not enumerated" vs "soft-blocked" is answerable
    # OFFLINE from the stick (the network-wifi degrade dimension's real-HW probe).
    pciutils iproute2
  ]);

  # drm_info is its own package; attr-guarded so a nixpkgs rev lacking it cannot
  # break evaluation (the rustdesk attr-guard pattern from desktop.nix).
  drmInfoBin =
    if pkgs ? drm_info then "${pkgs.drm_info}/bin/drm_info"
    else "";

  # `libinput list-devices` enumerates exactly what the compositor's seat sees:
  # every keyboard / pointer / touch / touchpad device + its capabilities (the
  # "Capabilities:" line) and tap/scroll config. It is THE real-HW probe for the
  # input-seat-pointer dimension — a "pointer frozen at 0,0 / nothing types / taps
  # don't register" boot is diagnosed by whether this lists a pointer/keyboard/touch
  # at all (seat saw no device) vs lists them but the cursor never moved (routing
  # bug). Attr-guarded like drm_info so a rev lacking the attr cannot break eval; the
  # `list-devices` subcommand needs CAP to open /dev/input/event*, so it is run as
  # root (the unit is root) and `|| true`-guarded (a seat with zero devices exits
  # non-zero, which is itself the signal).
  libinputBin =
    if pkgs ? libinput then "${pkgs.libinput}/bin/libinput"
    else "";

  # Audio probe tools, attr-guarded the same way. wpctl ships with WirePlumber,
  # pactl with the pulseaudio package (present on a PipeWire-pulse system). They
  # talk to the PER-USER PipeWire socket, so the capture runs them via `runuser`
  # against each active user session's XDG_RUNTIME_DIR (a root unit cannot see the
  # user's PipeWire instance). "Is the default sink muted / at volume 0?" is
  # exactly the real-HW "no audio out" failure the steward hit; the kernel-level
  # /proc/asound/cards + /dev/snd answer the prior "is there a sound card at all?".
  wpctlBin =
    if pkgs ? wireplumber then "${pkgs.wireplumber}/bin/wpctl"
    else "";
  pactlBin =
    if pkgs ? pulseaudio then "${pkgs.pulseaudio}/bin/pactl"
    else "";

  # ── The diagnostic-bundle capture script ──────────────────────────────────
  # Pure POSIX sh. `set -u` only (NOT -e): a probe failing must NEVER abort the
  # bundle — we want a PARTIAL bundle from a hung boot, not nothing. Every probe
  # is `|| true`-guarded. The script:
  #   1. Finds a block device with the HARTLOG label (no-op + exit 0 if absent).
  #   2. Mounts it rw vfat at $MNT.
  #   3. Writes the bundle to hart-boot-<short-boot-id>.log AND overwrites the
  #      stable hart-boot-latest.log, fsync'ing each.
  #   4. Syncs + unmounts cleanly.
  # Called by the early-boot, periodic-timer, and shutdown units with a $PHASE
  # arg ("early" / "periodic" / "shutdown") that is stamped in the header.
  captureScript = pkgs.writeShellScript "hart-boot-log-capture" ''
    set -u
    export PATH=${binPath}''${PATH:+:$PATH}

    PHASE="''${1:-periodic}"
    LABEL="${label}"
    MNT="${mnt}"
    LATCH="${latchFile}"
    WINDOW="${windowFile}"
    READY="${readyFlag}"
    INPUT_ALIVE="${inputAliveFlag}"
    DEGRADE="${degradeFlag}"
    DRM_INFO="${drmInfoBin}"
    LIBINPUT="${libinputBin}"
    WPCTL="${wpctlBin}"
    PACTL="${pactlBin}"

    log() { echo "[hart-boot-log] $*" >&2 ; }

    # ── 1. Find the HARTLOG partition. NO-OP cleanly if it isn't there. ──
    # blkid is the canonical label→device lookup; findfs is the simplest. If
    # neither finds it, there is no log partition (an old stick / a plain ISO
    # flash without the free-space partition) — exit 0, never fail boot.
    DEV=""
    if command -v findfs >/dev/null 2>&1; then
      DEV=$(findfs LABEL="$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ] && command -v blkid >/dev/null 2>&1; then
      DEV=$(blkid -L "$LABEL" 2>/dev/null) || DEV=""
    fi
    if [ -z "$DEV" ] || [ ! -b "$DEV" ]; then
      log "no '$LABEL' partition present — nothing to write (clean no-op, phase=$PHASE)"
      exit 0
    fi
    log "found '$LABEL' at $DEV (phase=$PHASE)"

    # ── 2. Mount it rw (vfat/FAT32 so the Windows host reads it natively). ──
    mkdir -p "$MNT" 2>/dev/null || true
    # Already mounted from a previous phase this boot? Reuse it.
    if ! mountpoint -q "$MNT" 2>/dev/null; then
      if ! mount -t vfat -o rw,flush,umask=0000 "$DEV" "$MNT" 2>/dev/null; then
        # Retry without an explicit fs type (let the kernel auto-detect) — a
        # quick-formatted stick can surface as vfat under a different probe.
        if ! mount -o rw "$DEV" "$MNT" 2>/dev/null; then
          log "could not mount $DEV at $MNT — skipping (boot continues)"
          exit 0
        fi
      fi
    fi

    BOOT_ID=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null | tr -d '-' | cut -c1-12) || BOOT_ID="unknown"
    STAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null) || STAMP="?"
    PER_BOOT="$MNT/hart-boot-$BOOT_ID.log"
    LATEST="$MNT/hart-boot-latest.log"

    # ── 3. Build the bundle into a temp file, then copy to both targets. ──
    # Build off-partition first (in tmpfs) so a slow FAT write doesn't interleave
    # with the probes, then a single copy + fsync lands it atomically-ish.
    TMP=$(mktemp 2>/dev/null) || TMP="/tmp/hart-boot-log.$$"
    {
      echo "════════════════════════════════════════════════════════════"
      echo " HART OS boot diagnostic bundle"
      echo "   phase    : $PHASE"
      echo "   written  : $STAMP (UTC)"
      echo "   boot_id  : $BOOT_ID"
      echo "   hostname : $(cat /proc/sys/kernel/hostname 2>/dev/null || echo '?')"
      echo "   os       : $(cat /etc/os-release 2>/dev/null | grep -m1 PRETTY_NAME || echo '?')"
      echo "════════════════════════════════════════════════════════════"
      echo ""

      echo "───────────── session-supervisor tier state ─────────────"
      # The single most important signal for the Tier-1 paint hang: which tier
      # is latched, the crash window, and whether the shell ever painted.
      if [ -r "$LATCH" ]; then
        echo "latched tier (active on next/this boot): $(cat "$LATCH" 2>/dev/null)"
      else
        echo "latched tier: <absent> (fresh/un-latched boot -> starts at startTier)"
      fi
      if [ -r "$WINDOW" ]; then
        echo "crash window (epoch timestamps, one per fast exit):"
        cat "$WINDOW" 2>/dev/null || true
      else
        echo "crash window: <absent> (no recent crash-loop accounting)"
      fi
      echo ""
      echo "── last tier-degrade (WHY the ladder fell back toward cage) ──"
      if [ -e "$DEGRADE" ]; then
        echo "DROP: $(cat "$DEGRADE" 2>/dev/null || echo '?')"
        echo "  mtime: $(date -r "$DEGRADE" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || stat -c '%y' "$DEGRADE" 2>/dev/null || echo '?')"
        echo "  (armed by the session-supervisor's write_tier on this downward drop;"
        echo "   the drop REASON is in the supervisor journal captured below.)"
      else
        echo "<absent> — no tier fell back this boot (the ladder held its rung)"
      fi
      echo ""
      echo "── shell-ready paint marker ($READY) ──"
      if [ -e "$READY" ]; then
        echo "PRESENT — the glass shell signalled its first painted frame"
        echo "  mtime: $(date -r "$READY" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || stat -c '%y' "$READY" 2>/dev/null || echo '?')"
      else
        echo "ABSENT — the shell NEVER painted a first frame this boot"
        echo "  (= the Tier-1/Tier-2 pointer-only hang; the paint-watchdog should"
        echo "   have dropped a tier. Cross-check the supervisor journal below.)"
      fi
      echo ""

      echo "───────────── root / boot device + kernel cmdline (boot-root-initrd) ─────────────"
      # THE real-HW probe for the boot-root-initrd dimension: record that root
      # actually MOUNTED, from WHERE, and that the USB-enumeration modules loaded —
      # so "did the USB root come up, and did it race the duplicate LABEL=HART_OS?"
      # is answerable OFFLINE from the stick (the failure modes a real USB boot hits
      # that a virtio-root VM never does). Every probe is best-effort (|| true).
      echo "── kernel cmdline (the root= the kernel was told to mount) ──"
      cat /proc/cmdline 2>/dev/null || echo "(/proc/cmdline unavailable)"
      echo ""
      echo "── root (/) mount — ROOT-MOUNT SUCCESS if a SOURCE+FSTYPE is shown ──"
      if findmnt -n -o SOURCE,FSTYPE,OPTIONS / 2>/dev/null; then
        ROOT_SRC=$(findmnt -n -o SOURCE / 2>/dev/null | head -n1) || ROOT_SRC=""
        echo "root-mount: SUCCESS (/ is mounted from $ROOT_SRC)"
        # The disk the root device lives on + whether it is removable/USB — so a
        # field reader can tell the live USB root from an internal install.
        ROOT_PK=$(lsblk -ndo pkname "$ROOT_SRC" 2>/dev/null | head -n1) || ROOT_PK=""
        if [ -n "$ROOT_PK" ]; then
          echo "root parent disk: /dev/$ROOT_PK  (RM=$(lsblk -ndo RM /dev/$ROOT_PK 2>/dev/null | tr -d ' ') TRAN=$(lsblk -ndo TRAN /dev/$ROOT_PK 2>/dev/null | tr -d ' '))"
        fi
      else
        echo "root-mount: UNKNOWN — findmnt could not report / (this should never"
        echo "  print from a booted system; if it does, the root pivot is suspect)"
      fi
      echo ""
      echo "── full block topology (lsblk) ──"
      lsblk -o NAME,TYPE,SIZE,FSTYPE,LABEL,PARTLABEL,RM,TRAN,MOUNTPOINT 2>/dev/null || echo "(lsblk unavailable)"
      echo ""
      echo "── duplicate-LABEL=HART_OS race check (devices answering to the ISO label) ──"
      # The intermittent real-HW root-mount panic is a DUPLICATE-LABEL race: if MORE
      # THAN ONE device answers to LABEL=HART_OS (the whole disk AND partition 1),
      # the by-label root device is decided by per-boot udev order — boots once,
      # panics next boot. Counting the matches here surfaces it from the stick.
      HARTOS_HITS=$(blkid -L HART_OS 2>/dev/null | wc -l 2>/dev/null) || HARTOS_HITS="?"
      echo "devices with LABEL=HART_OS: $HARTOS_HITS (EXPECT 1; >1 = the duplicate-LABEL root race is armed)"
      blkid 2>/dev/null | grep -i 'HART_OS' || echo "(no HART_OS-labelled devices reported by blkid)"
      echo ""
      echo "── USB-root enumeration modules loaded (proves the initrd modules took on real HW) ──"
      # If usb_storage / an xhci controller / sd_mod are loaded, the USB stick
      # actually enumerated — the initrd carried + udev loaded the right modules.
      # On a non-USB (internal) boot these may be absent; that is fine, this is a
      # diagnostic, not a gate.
      lsmod 2>/dev/null | grep -iE 'usb_storage|uas|xhci|ehci|^sd_mod|sd_mod ' || echo "(none of usb_storage/uas/xhci/ehci/sd_mod currently loaded)"
      echo ""

      echo "───────────── systemctl --failed ─────────────"
      systemctl --failed --no-pager --no-legend 2>/dev/null || echo "(systemctl --failed unavailable)"
      echo ""

      echo "───────────── hart-* unit status ─────────────"
      systemctl status 'hart-*' --no-pager 2>/dev/null || echo "(systemctl status hart-* unavailable)"
      echo ""

      echo "───────────── session-supervisor (greetd) journal ─────────────"
      # The tier-drop decisions are logged by the selector wrapper with the
      # [hart-session-supervisor] prefix to greetd's journal.
      journalctl -b --no-pager -u greetd.service 2>/dev/null || echo "(greetd journal unavailable)"
      echo ""
      echo "── tier-drop decisions (grep of the whole boot journal) ──"
      journalctl -b --no-pager 2>/dev/null | grep -i 'hart-session-supervisor\|session tier\|latched\|paint-watchdog\|HUNG' || echo "(no supervisor decision lines)"
      echo ""

      echo "───────────── GTK4 host / GSK / GDK / EGL / GBM / WebKit GL ─────────────"
      # The exact error class the GSK_RENDERER=cairo fix (75ba78d) addresses.
      # Surfacing these here makes the fix CONFIRMABLE from the Windows host: a
      # clean boot shows GSK_RENDERER=cairo in the env dump and NO GL hang lines.
      echo "── GL-relevant env from the glass-shell unit(s) ──"
      systemctl show 'hart-liquid-ui-renderer.service' -p Environment --no-pager 2>/dev/null || true
      ( systemctl cat 'hart-*glass*' --no-pager 2>/dev/null | grep -i 'GSK_RENDERER\|GDK_GL\|LIBGL\|WLR_RENDERER\|WEBKIT_DISABLE\|HardwareAcceleration' ) || true
      echo ""
      echo "── GL/EGL/GBM/GSK/WebKit error lines from the boot journal ──"
      journalctl -b --no-pager 2>/dev/null | grep -iE 'gsk|gdk|egl|gbm|glx|webkit|renderer|dri|drm|wlroots|wayland|software rendering|llvmpipe|MESA|failed to (create|bind|make)' | tail -n 400 || echo "(no GL-class lines)"
      echo ""

      echo "───────────── active session / loginctl ─────────────"
      loginctl --no-pager 2>/dev/null || echo "(loginctl unavailable)"
      echo ""
      loginctl session-status 2>/dev/null || true
      echo ""

      echo "───────────── GPU / DRM ─────────────"
      if [ -n "$DRM_INFO" ] && [ -x "$DRM_INFO" ]; then
        "$DRM_INFO" 2>/dev/null | head -n 200 || echo "(drm_info produced no output)"
      else
        echo "(drm_info not in closure — falling back to sysfs)"
        for d in /sys/class/drm/card*/device/uevent; do
          [ -r "$d" ] && { echo "== $d =="; cat "$d" 2>/dev/null; }
        done
      fi
      echo "── /dev/dri ──"
      ls -l /dev/dri 2>/dev/null || echo "(no /dev/dri — no DRM/KMS node)"
      echo ""

      echo "───────────── NETWORK / WiFi / rfkill ─────────────"
      # THE diagnostic for "Wi-Fi hardware not detected" / "wifi shows off but the
      # chip is fine". Four layers, top-down, so a reader can tell apart:
      #   (1) lspci network-class devices — is the wifi/ethernet CHIP even on the
      #       bus? (missing here == no driver bound / no hardware, NOT a soft issue)
      #   (2) rfkill — is a present radio SOFT- or HARD-blocked? (a soft-block reads
      #       as "off"; a hard-block needs a physical switch; both PROVE the chip is
      #       enumerated, so they are distinct from "no hardware")
      #   (3) ip link — which net interfaces the kernel actually created + their
      #       UP/DOWN state.
      #   (4) the kernel's wifi firmware/driver lines (iwlwifi/ath/rtw/brcm load OR
      #       a "firmware: failed to load" — the redistributable-firmware gap).
      echo "── lspci (network class) ──"
      if command -v lspci >/dev/null 2>&1; then
        lspci -nn 2>/dev/null | grep -iE 'network|ethernet|wireless|wi-?fi' \
          || echo "(no network-class PCI device — wifi/eth chip not enumerated)"
      else
        echo "(lspci unavailable)"
      fi
      echo "── rfkill (soft/hard block state — distinguishes a block from no-hw) ──"
      if command -v rfkill >/dev/null 2>&1; then
        rfkill list 2>/dev/null || echo "(rfkill produced no output)"
      else
        # Fall back to sysfs (the same source the shell's wifi probe reads), so the
        # block state is captured even on a build without the rfkill CLI.
        echo "(rfkill CLI unavailable — sysfs /sys/class/rfkill follows)"
        for r in /sys/class/rfkill/rfkill*; do
          [ -d "$r" ] || continue
          echo "== $r ==  type=$(cat "$r/type" 2>/dev/null)  soft=$(cat "$r/soft" 2>/dev/null)  hard=$(cat "$r/hard" 2>/dev/null)"
        done
      fi
      echo "── ip link (interface up/down) ──"
      if command -v ip >/dev/null 2>&1; then
        ip -br link 2>/dev/null || ip link 2>/dev/null || echo "(ip produced no output)"
      else
        echo "(ip unavailable — /sys/class/net follows)"
        ls /sys/class/net 2>/dev/null || echo "(no /sys/class/net)"
      fi
      echo "── kernel wifi firmware/driver lines (iwlwifi/ath/rtw/brcm) ──"
      journalctl -b --no-pager 2>/dev/null \
        | grep -iE 'iwlwifi|iwlmvm|ath[0-9]|ath1[0-9]k|rtw[0-9]|rtl[0-9]|brcm|cfg80211|firmware: (failed|direct)' \
        | tail -n 80 || echo "(no wifi driver/firmware lines)"
      echo ""

      echo "───────────── INPUT / SEAT / POINTER (#134) ─────────────"
      # THE diagnostic for "pointer frozen at 0,0 / nothing types / taps don't
      # register". Four layers, top-down, so a reader can localise the break:
      #   (1) the compositor's input-alive beacon (did the libinput->Seat path ever
      #       deliver a real event this boot),
      #   (2) libinput list-devices (what the seat actually enumerated: pointer /
      #       keyboard / touch / touchpad + capabilities),
      #   (3) the raw kernel evdev table + the /dev/input node permissions (did the
      #       devices exist + were they openable by the seat's user/group),
      #   (4) loginctl seat-status (did logind assign the input devices to seat0).
      echo "── input-alive beacon ($INPUT_ALIVE) — the #134 liveness signal ──"
      if [ -e "$INPUT_ALIVE" ]; then
        echo "PRESENT — the compositor delivered a real seat event (libinput/Seat path LIVE)"
        echo "  mtime: $(date -r "$INPUT_ALIVE" -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || stat -c '%y' "$INPUT_ALIVE" 2>/dev/null || echo '?')"
      else
        echo "ABSENT — NO pointer/keyboard event was ever delivered into the seat this boot."
        echo "  If the shell-ready paint marker above is PRESENT, this is the painted-but-"
        echo "  input-starved seat (#134 'pointer frozen at 0,0, nothing types'). Cross-check"
        echo "  the device enumeration below: a seat with no pointer/keyboard means the"
        echo "  devices never opened; devices present + no beacon means an input-routing bug."
      fi
      echo ""
      echo "── libinput list-devices (what the seat enumerated) ──"
      if [ -n "$LIBINPUT" ] && [ -x "$LIBINPUT" ]; then
        # Needs root to open /dev/input/event*; the unit is root. Non-zero exit (a
        # seat with zero devices) is itself the signal — never abort the bundle.
        "$LIBINPUT" list-devices 2>/dev/null || echo "(libinput list-devices found NO devices / exited non-zero — seat granted no input)"
      else
        echo "(libinput not in closure — falling back to the raw evdev table below)"
      fi
      echo ""
      echo "── seat capability summary (does the seat expose pointer / keyboard / touch?) ──"
      # A one-line verdict so a Windows-host reader answers "does this boot's seat
      # expose pointer+keyboard+touch?" at a glance instead of eyeballing the raw
      # enumeration above. Derived from libinput's authoritative `Capabilities:`
      # lines when available, else the kernel evdev handlers (mouseN=pointer,
      # kbd=keyboard; the raw table carries no ID_INPUT_TOUCHSCREEN tag, so touch is
      # `unknown` on the fallback). pointer=no AND keyboard=no with a PRESENT
      # shell-ready marker IS the "pointer frozen at 0,0, nothing types" seat.
      _isp_caps=""
      if [ -n "$LIBINPUT" ] && [ -x "$LIBINPUT" ]; then
        _isp_caps="$("$LIBINPUT" list-devices 2>/dev/null | grep -i 'Capabilities:' 2>/dev/null)" || _isp_caps=""
      fi
      _isp_ptr=no; _isp_kbd=no; _isp_tch=no
      if [ -n "$_isp_caps" ]; then
        printf '%s\n' "$_isp_caps" | grep -qi 'pointer'  && _isp_ptr=yes
        printf '%s\n' "$_isp_caps" | grep -qi 'keyboard' && _isp_kbd=yes
        printf '%s\n' "$_isp_caps" | grep -qi 'touch'    && _isp_tch=yes
      elif [ -r /proc/bus/input/devices ]; then
        grep -qiE '^H: Handlers=.*mouse[0-9]' /proc/bus/input/devices && _isp_ptr=yes
        grep -qiE '^H: Handlers=.*kbd'        /proc/bus/input/devices && _isp_kbd=yes
        _isp_tch=unknown
      fi
      echo "seat capabilities: pointer=$_isp_ptr keyboard=$_isp_kbd touch=$_isp_tch"
      if [ "$_isp_ptr" = no ] && [ "$_isp_kbd" = no ]; then
        echo "  (NO pointer AND NO keyboard enumerated -> if the shell-ready marker is"
        echo "   PRESENT above, this is the painted-but-input-starved seat: pointer frozen"
        echo "   at 0,0 / nothing types. If ABSENT, the devices never opened at all.)"
      fi
      echo ""
      echo "── kernel evdev table (/proc/bus/input/devices) ──"
      # Always present (no extra package): lists every input device the KERNEL sees
      # with its EV= capability bitmask (EV=120013 keyboard, EV=17 mouse, EV=b touch),
      # so a pointer/keyboard/touch can be confirmed even when libinput is absent.
      cat /proc/bus/input/devices 2>/dev/null || echo "(no /proc/bus/input/devices)"
      echo ""
      echo "── /dev/input node permissions (seat must be able to open these) ──"
      ls -l /dev/input 2>/dev/null || echo "(no /dev/input — kernel saw no input devices at all)"
      echo ""
      echo "── loginctl seat-status seat0 (logind's device assignment) ──"
      # The seat (logind) must ATTACH the input devices to seat0 and grant the
      # active session a device lease. A seat0 with no input devices == the seat
      # never granted them (the 'seat not granting input' failure).
      loginctl seat-status seat0 2>/dev/null || echo "(loginctl seat-status unavailable)"
      echo ""

      echo "───────────── AUDIO / SINK / VOLUME ─────────────"
      # THE diagnostic for "no audio out": a sink that EXISTS but is MUTED or at
      # volume 0 on boot (the steward's real-HW bug), distinguished from "no sound
      # card at all". Three layers, bottom-up:
      #   (1) the KERNEL sound cards + /dev/snd nodes — is there hardware at all?
      #   (2) the PER-USER PipeWire/WirePlumber state via wpctl/pactl, run as the
      #       session user (a root unit cannot see the user's PipeWire socket) —
      #       the default sink, its MUTE flag + VOLUME level.
      #   (3) the hart-audio-unmute rescue's own decision lines from the journal —
      #       did the boot-time unmute run + what did it do.
      echo "── kernel sound cards (/proc/asound/cards) — is there audio HW at all ──"
      cat /proc/asound/cards 2>/dev/null || echo "(no /proc/asound/cards — kernel saw no sound card)"
      echo "── /dev/snd nodes ──"
      ls -l /dev/snd 2>/dev/null || echo "(no /dev/snd — no ALSA device nodes)"
      echo ""
      echo "── per-user PipeWire default-sink state (wpctl/pactl as each session user) ──"
      # wpctl/pactl connect to the per-user PipeWire socket under each session's
      # XDG_RUNTIME_DIR. Iterate /run/user/<uid>, resolve the owner with stat, and
      # runuser into that user. All best-effort (|| true): a headless boot with no
      # user session simply prints the no-session note and the bundle continues.
      _did_user=0
      for RUNDIR in /run/user/*; do
        [ -d "$RUNDIR" ] || continue
        UID_N=$(basename "$RUNDIR")
        UNAME=$(stat -c %U "$RUNDIR" 2>/dev/null) || UNAME=""
        [ -n "$UNAME" ] || continue
        _did_user=1
        echo "== session $UNAME (uid $UID_N) =="
        if [ -n "$WPCTL" ] && [ -x "$WPCTL" ]; then
          runuser -u "$UNAME" -- env XDG_RUNTIME_DIR="$RUNDIR" "$WPCTL" status 2>/dev/null \
            || echo "(wpctl status unavailable for $UNAME — no PipeWire session?)"
          runuser -u "$UNAME" -- env XDG_RUNTIME_DIR="$RUNDIR" "$WPCTL" get-volume @DEFAULT_AUDIO_SINK@ 2>/dev/null \
            || echo "(wpctl get-volume: no default sink for $UNAME)"
        fi
        if [ -n "$PACTL" ] && [ -x "$PACTL" ]; then
          echo "default-sink: $(runuser -u "$UNAME" -- env XDG_RUNTIME_DIR="$RUNDIR" "$PACTL" get-default-sink 2>/dev/null || echo '(none)')"
          runuser -u "$UNAME" -- env XDG_RUNTIME_DIR="$RUNDIR" "$PACTL" list short sinks 2>/dev/null \
            || echo "(pactl list sinks unavailable for $UNAME)"
        fi
      done
      [ "$_did_user" = "1" ] || echo "(no /run/user/* session — headless/no graphical login; no per-user audio state)"
      echo ""
      echo "── hart-audio-unmute rescue decisions (from the boot journal) ──"
      journalctl -b --no-pager 2>/dev/null | grep -i 'hart-audio-unmute\|pipewire\|wireplumber\|PipeWire\|default sink\|MUTED' | tail -n 120 \
        || echo "(no audio-rescue / PipeWire lines this boot)"
      echo ""

      echo "───────────── dmesg (tail) ─────────────"
      dmesg 2>/dev/null | tail -n 300 || echo "(dmesg unavailable — kernel.dmesg_restrict?)"
      echo ""

      echo "───────────── FULL current-boot journal (journalctl -b) ─────────────"
      # The full journal LAST so a reader hits the curated summary first but the
      # complete record is always present for deep dives.
      journalctl -b --no-pager 2>/dev/null || echo "(journalctl -b unavailable)"
      echo ""
      echo "═══════════════════ end of bundle (phase=$PHASE) ═══════════════════"
    } > "$TMP" 2>&1 || true

    # ── 4. Land it on the FAT32 partition + fsync so a power-off can't lose it. ──
    # Per-boot file (history across this boot's phases) + the stable latest file
    # the Windows host always opens. cp then `sync` the mountpoint's device.
    cp -f "$TMP" "$PER_BOOT" 2>/dev/null || log "write of $PER_BOOT failed"
    cp -f "$TMP" "$LATEST"   2>/dev/null || log "write of $LATEST failed"
    rm -f "$TMP" 2>/dev/null || true

    # fsync the partition so an abrupt power-off after a hung boot still keeps the
    # bytes (FAT has no journal; `sync` flushes the page cache to the device).
    sync "$MNT" 2>/dev/null || sync || true
    log "wrote bundle to $LATEST (+ $PER_BOOT), phase=$PHASE"

    # ── Unmount cleanly so the Windows host sees a consistent filesystem. On the
    # periodic phase we KEEP it mounted (re-mount churn on a slow stick is worse
    # than holding the mount). On every OTHER phase (early/shutdown/manual) we
    # unmount IF it is mounted — regardless of which phase mounted it (a prior
    # periodic tick may hold the mount), so shutdown always leaves a clean fs and
    # an early-boot crash before the first periodic tick still leaves a clean fs.
    if [ "$PHASE" != "periodic" ] && mountpoint -q "$MNT" 2>/dev/null; then
      sync || true
      umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
    fi
    exit 0
  '';
in
{
  # ═══════════════════════════════════════════════════════════
  # Options
  # ═══════════════════════════════════════════════════════════
  options.hart.bootLog = {
    enable = lib.mkEnableOption ''
      the persistent boot-diagnostic log partition. When a partition labelled
      HARTLOG (FAT32) is present, HART OS writes the full current-boot journal +
      tier-supervisor state + GTK4/GL diagnostics to it early in boot, on a
      periodic timer (so a HUNG boot still leaves a record), and at shutdown — so
      a Windows host can read the boot journal off the stick WITHOUT the user
      hand-copying from a TTY. A pure NO-OP when no HARTLOG partition exists'';

    label = lib.mkOption {
      type = lib.types.str;
      default = "HARTLOG";
      description = ''
        The filesystem LABEL of the diagnostic-log partition. Must match the
        label the flasher (scripts/hart_usb_flasher.py) writes when it creates
        the FAT32 partition in the stick's free space. ONE source of truth for
        the on-stick contract; changing it here requires changing the flasher.
      '';
    };

    intervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 20;
      description = ''
        The periodic re-capture interval (seconds). This is what makes a HUNG
        boot debuggable: the Tier-1 pointer-only hang never settles + never
        exits, so only a periodic capture leaves the journal-so-far on the
        stick. Architecture default 20s (frequent enough to catch the hang,
        infrequent enough not to thrash a slow USB stick).
      '';
    };
  };

  # ═══════════════════════════════════════════════════════════
  # Configuration  (opt-in; pure no-op when disabled OR when no HARTLOG present)
  # ═══════════════════════════════════════════════════════════
  config = lib.mkIf (cfg.enable && blog.enable) {

    # Private mountpoint for the HARTLOG partition (tmpfs /run, never persisted).
    systemd.tmpfiles.rules = [
      "d /run/hart 0750 hart hart -"
      "d ${mnt}    0755 root root -"
    ];

    # ── EARLY-boot capture ───────────────────────────────────────────────────
    # Fire as early as the journal + a mounted-able block layer exist, so even a
    # boot that hangs seconds later leaves an initial bundle. DefaultDependencies
    # stay on (we WANT it ordered after local-fs/systemd-journald) but it must
    # never gate anything — nothing waits on it.
    systemd.services.hart-boot-log-early = {
      description = "HART OS — early boot diagnostic capture to the HARTLOG partition";
      wantedBy = [ "multi-user.target" ];
      after = [ "local-fs.target" "systemd-journald.service" ];
      # A nixos-rebuild switch must not re-run a one-shot capture mid-session.
      restartIfChanged = false;
      # Best-effort: never block the boot transaction.
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = false;
        ExecStart = "${captureScript} early";
        # A capture stall (e.g. a very slow USB stick) must not wedge boot.
        TimeoutStartSec = "90s";
      };
    };

    # ── PERIODIC capture (THE hung-boot debugger) ────────────────────────────
    # A monotonic timer that re-captures every intervalSeconds. The Tier-1
    # pointer-only hang never exits, so this periodic tick is the ONLY thing that
    # lands the journal-so-far on the stick. OnBootSec small so the first tick is
    # quick; OnUnitActiveSec = the configured interval.
    systemd.services.hart-boot-log-periodic = {
      description = "HART OS — periodic boot diagnostic capture to the HARTLOG partition";
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${captureScript} periodic";
        TimeoutStartSec = "90s";
      };
    };
    systemd.timers.hart-boot-log-periodic = {
      description = "HART OS — periodic boot-diagnostic capture timer (catches a HUNG boot)";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "15s";
        OnUnitActiveSec = "${toString blog.intervalSeconds}s";
        # No Persistent — this is purely a live-boot debugger; a missed tick
        # while powered off is meaningless.
        AccuracySec = "2s";
      };
    };

    # ── SHUTDOWN capture (final state on a clean power-off / reboot) ──────────
    # A oneshot ordered before shutdown.target that runs its ExecStop at
    # shutdown time (RemainAfterExit + ExecStop is the systemd idiom for "do work
    # on the way down"). Captures the final journal so a clean reboot still
    # leaves the last-known-good state on the stick.
    systemd.services.hart-boot-log-shutdown = {
      description = "HART OS — shutdown-time diagnostic capture to the HARTLOG partition";
      wantedBy = [ "multi-user.target" ];
      # Order so its ExecStop runs as the system goes down, before the block
      # layer + journald are torn down.
      before = [ "shutdown.target" "umount.target" ];
      after = [ "local-fs.target" ];
      conflicts = [ "shutdown.target" ];
      # Don't let a nixos-rebuild switch stop+restart this (which would fire the
      # ExecStop capture mid-session); it is a shutdown-only hook.
      restartIfChanged = false;
      stopIfChanged = false;
      unitConfig = {
        DefaultDependencies = false;
        # Stay loaded the whole uptime so the ExecStop fires on the way down.
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # ExecStart is a no-op marker; the real work is ExecStop at shutdown.
        ExecStart = "${pkgs.coreutils}/bin/true";
        ExecStop = "${captureScript} shutdown";
        TimeoutStopSec = "60s";
      };
    };

    # ── DEGRADE-triggered capture (a session tier fell back toward cage) ──────
    # The session-supervisor touches /run/hart/session/tier-degraded when write_tier
    # LOWERS the tier (the 1->2 / 2->3 fall-backs — hart-session-supervisor.nix).
    # Capture the bundle RIGHT THEN: the volatile live-USB journal still holds the
    # failed tier's output + the supervisor's drop-REASON log, so this pins down
    # "why did it fall to cage" at the exact moment — beating a wait of up to one
    # periodic interval, and grabbing it before the next tier floods/rolls the
    # journal. Reuses the ONE captureScript (mode label "degrade") — no parallel
    # journalctl. Runs as a root system service (the capture mounts HARTLOG, which
    # the greetd-session supervisor cannot do itself). Best-effort like every other
    # capture unit: a no-HARTLOG stick is a clean no-op.
    systemd.paths.hart-boot-log-degrade = {
      description = "HART OS — watch for a session tier-degrade to trigger a capture";
      wantedBy = [ "multi-user.target" ];
      pathConfig = {
        # The supervisor writes this file only on a real downward drop. systemd
        # watches the (tmpfiles-created) parent dir until the file appears.
        PathModified = "/run/hart/session/tier-degraded";
        Unit = "hart-boot-log-degrade.service";
      };
    };
    systemd.services.hart-boot-log-degrade = {
      description = "HART OS — capture diagnostics on a session tier-degrade (to HARTLOG)";
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = false;
        ExecStart = "${captureScript} degrade";
        # A slow USB stick must not wedge the capture; match the other units.
        TimeoutStartSec = "90s";
      };
    };

    # The capture binary + its probe tools on the system PATH (so an operator can
    # also run it by hand from a recovery TTY: `hart-boot-log-capture manual`).
    environment.systemPackages = [
      (pkgs.runCommand "hart-boot-log-cli" { } ''
        mkdir -p $out/bin
        ln -s ${captureScript} $out/bin/hart-boot-log-capture
      '')
    ]
    ++ lib.optional (pkgs ? drm_info) pkgs.drm_info
    # libinput ships the `libinput list-devices` CLI the input-seat-pointer probe
    # (and a recovery-TTY operator) calls to enumerate the seat's pointer/keyboard/
    # touch devices. Attr-guarded so a rev lacking it cannot break eval.
    ++ lib.optional (pkgs ? libinput) pkgs.libinput;
  };
}

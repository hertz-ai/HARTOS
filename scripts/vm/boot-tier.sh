#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# boot-tier.sh — headless QEMU-KVM boot of a HART OS tier ISO with serial console
# ═══════════════════════════════════════════════════════════════════════════
#
# Phase-0 (Floor-lock + CI/VM gate harness) deliverable. The companion to the CI
# `nixosTest` node in nixos/tests/floor-lock.nix: where the nixosTest is the
# AUTHORITATIVE gate (it boots cage-on-llvmpipe and fetches /shell/static in a
# managed VM), THIS is the FAST local-iteration runbook — boot any tier's ISO
# headless under QEMU with a serial console so a human can watch the compositor
# paint, the WebView frame land, the agent arrange windows (later phases), or the
# tier-drop supervisor fall to cage (Phase 1), without a CI round-trip.
#
# WSL note: this box is Windows; QEMU-KVM runs under WSL2 (or any Linux host with
# /dev/kvm). The script is POSIX sh + standard qemu-system-x86_64 flags. KVM is
# auto-detected: present → `-enable-kvm -cpu host` (fast); absent → TCG software
# emulation (slow but works in a nested/CI container) with a warning.
#
# It does NOT compile a compositor and does NOT author any L1/L2 paint logic —
# it only BOOTS an already-built ISO. Per the honest-hardware-limit rule, every
# compositor paint / software-GL / XWayland / screencast claim is proven inside
# the VM this script launches, never authored blind on the dev box.
#
# ── Usage ──────────────────────────────────────────────────────────────────
#   scripts/vm/boot-tier.sh --iso <path-to.iso> [--tier cage|sway|hart-comp]
#                           [--mem MB] [--cpus N] [--software-gl]
#                           [--ssh-port P] [--curl-static] [--extra "<qemu args>"]
#
#   # Build a desktop ISO with nix first (on the Linux host), then:
#   nix build .#iso-desktop -o result-iso
#   scripts/vm/boot-tier.sh --iso result-iso/iso/*.iso --tier cage --software-gl
#
# ── What --tier does ───────────────────────────────────────────────────────
#   The ISO already ships every session (cage Tier-3 today; sway Tier-2 +
#   hart-comp Tier-1 land in later phases). --tier is recorded into the kernel
#   cmdline as `hart.session_tier=<tier>` (read by the greeter / Phase-1
#   supervisor via /var/lib/hart/session-tier — see SESSION_TIER_CONTRACT.md) and
#   echoed to the serial log so the operator can confirm which floor booted. It
#   never *compiles* a tier; it selects which already-built session the VM lands
#   on. Defaults to `cage` — the audited never-fail floor that ships today.
#
# ── --software-gl ──────────────────────────────────────────────────────────
#   Forces the broken-GPU / llvmpipe path the cage floor is hardened for: no
#   host GPU passthrough, `-vga std`, and the same env the kiosk launcher exports
#   (WLR_RENDERER_ALLOW_SOFTWARE / LIBGL_ALWAYS_SOFTWARE / WEBKIT_DISABLE_DMABUF_
#   RENDERER) appended to the kernel cmdline as `hart.force_software_gl=1`. This
#   is the bit-for-bit floor the nixosTest also exercises, so local + CI agree.
#
# ── --curl-static (dead-husk-aware health probe) ───────────────────────────
#   After boot, poll http://localhost:<ssh-port-mapped 6800>/shell/static/
#   hartHero.js over the forwarded port and assert HTTP 200 + non-empty body —
#   the SAME real-fetch check (NOT inline-render) the f294f52 dead-husk lesson
#   demands and the nixosTest enforces. Requires the guest's :6800 forwarded.
#
# Exit status: 0 if the VM booted (and, with --curl-static, the shell served its
# assets); non-zero on a boot failure or a dead-husk 404.

set -eu

# ── Defaults ───────────────────────────────────────────────────────────────
ISO=""
TIER="cage"
MEM_MB="4096"
CPUS="2"
SOFTWARE_GL="0"
SSH_PORT="2222"
SHELL_PORT="16800"   # host port forwarded to guest :6800 (LiquidUI)
BACKEND_PORT="16777" # host port forwarded to guest :6777 (brain)
CURL_STATIC="0"
EXTRA=""
SERIAL_LOG="${HART_VM_SERIAL_LOG:-/tmp/hart-vm-serial.log}"
BOOT_TIMEOUT="${HART_VM_BOOT_TIMEOUT:-300}"

VALID_TIERS="cage sway hart-comp"

usage() {
  sed -n '2,/^set -eu/p' "$0" | sed 's/^# \{0,1\}//; s/^#$//'
  exit "${1:-0}"
}

# ── Arg parse ──────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --iso)         ISO="${2:?--iso needs a path}"; shift 2 ;;
    --tier)        TIER="${2:?--tier needs a value}"; shift 2 ;;
    --mem)         MEM_MB="${2:?--mem needs MB}"; shift 2 ;;
    --cpus)        CPUS="${2:?--cpus needs N}"; shift 2 ;;
    --software-gl) SOFTWARE_GL="1"; shift ;;
    --ssh-port)    SSH_PORT="${2:?--ssh-port needs P}"; shift 2 ;;
    --shell-port)  SHELL_PORT="${2:?--shell-port needs P}"; shift 2 ;;
    --curl-static) CURL_STATIC="1"; shift ;;
    --extra)       EXTRA="${2:?--extra needs a string}"; shift 2 ;;
    -h|--help)     usage 0 ;;
    *) echo "boot-tier.sh: unknown arg '$1'" >&2; usage 1 ;;
  esac
done

# ── Validate ───────────────────────────────────────────────────────────────
if [ -z "$ISO" ]; then
  echo "boot-tier.sh: --iso <path-to.iso> is required" >&2
  usage 1
fi
if [ ! -f "$ISO" ]; then
  echo "boot-tier.sh: ISO not found: $ISO" >&2
  echo "  Build one first on the Linux host, e.g.:" >&2
  echo "    nix build .#iso-desktop -o result-iso && ls result-iso/iso/*.iso" >&2
  exit 1
fi
case " $VALID_TIERS " in
  *" $TIER "*) : ;;
  *) echo "boot-tier.sh: invalid --tier '$TIER' (valid: $VALID_TIERS)" >&2; exit 1 ;;
esac

QEMU="${QEMU_BIN:-qemu-system-x86_64}"
if ! command -v "$QEMU" >/dev/null 2>&1; then
  echo "boot-tier.sh: '$QEMU' not found. Install qemu (Linux/WSL2):" >&2
  echo "    nix shell nixpkgs#qemu   # or: sudo apt-get install qemu-system-x86" >&2
  exit 1
fi

# ── KVM auto-detect (WSL2 may or may not expose /dev/kvm) ──────────────────
ACCEL_ARGS=""
if [ -w /dev/kvm ]; then
  ACCEL_ARGS="-enable-kvm -cpu host"
  echo "[boot-tier] KVM available — hardware acceleration ON"
else
  echo "[boot-tier] WARNING: /dev/kvm not writable — falling back to TCG (slow)." >&2
  echo "[boot-tier]   On WSL2 enable nested virt + 'modprobe kvm_intel'/'kvm_amd'." >&2
fi

# ── Software-GL / tier kernel cmdline (read by greeter + Phase-1 supervisor) ─
# These are appended to the ISO's boot cmdline. The cage floor's kiosk launcher
# already exports the wlroots/Mesa software-GL env; passing the marker on the
# cmdline lets the (future) supervisor + greeter pick the forced-software session
# deterministically AND records the operator's intent in the serial log.
CMDLINE="hart.session_tier=${TIER}"
if [ "$SOFTWARE_GL" = "1" ]; then
  CMDLINE="${CMDLINE} hart.force_software_gl=1"
  echo "[boot-tier] forced software GL (llvmpipe / wlroots pixman) — broken-GPU floor"
fi

echo "[boot-tier] ISO=$ISO tier=$TIER mem=${MEM_MB}M cpus=$CPUS"
echo "[boot-tier] serial console -> $SERIAL_LOG"
echo "[boot-tier] forwarding host :$SSH_PORT->guest :22, host :$SHELL_PORT->guest :6800, host :$BACKEND_PORT->guest :6777"
: > "$SERIAL_LOG"

# ── VGA: plain std (no virtio-gpu) so the guest can never hardware-accelerate;
# this is what makes --software-gl honest — the GPU path simply is not there.
VGA="-vga std"
if [ "$SOFTWARE_GL" = "0" ] && [ -w /dev/kvm ]; then
  VGA="-vga virtio"  # only when NOT proving the software floor
fi

# ── Run QEMU headless with serial redirected to a file (and tee'd to stdout) ─
# -nographic: no SDL/GTK window (headless CI + WSL). -serial mon:stdio would mix
# the monitor in; we instead log serial to a file the --curl-static probe and CI
# can grep, while still echoing to the terminal.
# shellcheck disable=SC2086
run_qemu() {
  "$QEMU" \
    $ACCEL_ARGS \
    -m "$MEM_MB" \
    -smp "$CPUS" \
    -cdrom "$ISO" \
    -boot d \
    $VGA \
    -nographic \
    -serial "file:$SERIAL_LOG" \
    -netdev "user,id=net0,hostfwd=tcp::${SSH_PORT}-:22,hostfwd=tcp::${SHELL_PORT}-:6800,hostfwd=tcp::${BACKEND_PORT}-:6777" \
    -device virtio-net-pci,netdev=net0 \
    -append "console=ttyS0 $CMDLINE" \
    $EXTRA &
  echo $!
}

QEMU_PID="$(run_qemu)"
echo "[boot-tier] qemu pid=$QEMU_PID"

cleanup() {
  if kill -0 "$QEMU_PID" 2>/dev/null; then
    echo "[boot-tier] shutting down VM (pid=$QEMU_PID)"
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Stream the serial log to stdout in the background so the operator sees boot.
( tail -f "$SERIAL_LOG" 2>/dev/null & echo $! > /tmp/hart-vm-tail.pid ) &

# ── Dead-husk-aware health probe (the f294f52 lesson, real fetch not inline) ─
if [ "$CURL_STATIC" = "1" ]; then
  echo "[boot-tier] waiting up to ${BOOT_TIMEOUT}s for shell to serve /shell/static ..."
  PROBE_URL="http://localhost:${SHELL_PORT}/shell/static/hartHero.js"
  deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
  ok=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
      echo "[boot-tier] FAIL: VM exited before the shell came up" >&2
      exit 1
    fi
    # -f: fail on HTTP >=400 (catches the dead-husk 404). -s: silent.
    body="$(curl -fs "$PROBE_URL" 2>/dev/null || true)"
    if [ -n "$body" ]; then
      ok=1
      break
    fi
    sleep 5
  done
  if [ "$ok" = "1" ]; then
    echo "[boot-tier] OK: $PROBE_URL served 200 + non-empty body (NOT a dead husk)"
    exit 0
  else
    echo "[boot-tier] FAIL: $PROBE_URL never returned a non-empty 200 within ${BOOT_TIMEOUT}s" >&2
    echo "[boot-tier]   (this is the dead-husk regression class: shell HTML rendered but" >&2
    echo "[boot-tier]    /shell/static/* 404'd — see f294f52 / SESSION_TIER_CONTRACT.md)" >&2
    exit 2
  fi
fi

# Without --curl-static, block on the VM so the operator can interact via serial.
echo "[boot-tier] VM running; press Ctrl-C to stop. Serial: $SERIAL_LOG"
wait "$QEMU_PID"

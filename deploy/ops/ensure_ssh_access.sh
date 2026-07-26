#!/usr/bin/env bash
# ensure_ssh_access.sh — make key-based SSH to maintenance targets programmatic.
#
# Why this exists: the cert push in renew_ssl_v2.py (deploy_mailserver) needs
# passwordless root SSH to the mail VM. That key was first installed by hand,
# which does not survive rebuilding a box or adding another DeepBox clone. This
# script makes the step reproducible, so provisioning a new host or re-pairing
# an existing one is one command rather than a remembered ritual.
#
# Idempotent by design:
#   * key is generated only if absent
#   * a target that already accepts the key needs NO password and is left alone
#   * a password is required only to BOOTSTRAP a target that does not yet trust us
#
# Usage:
#   ensure_ssh_access.sh root@104.254.246.77 [root@other-host:2222 ...]
#   HEVOLVE_SSH_TARGETS="root@a,root@b:2222" ensure_ssh_access.sh
#   TARGET_PASS='...' ensure_ssh_access.sh root@newclone      # bootstrap
#
# Exit: 0 all targets reachable by key; 1 one or more still need bootstrapping.

set -u

KEY="${HEVOLVE_SSH_KEY:-/root/.ssh/id_ed25519}"
SSH_OPTS="-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15"

log() { echo "[ensure-ssh] $*"; }

# ── 1. ensure we have a key ──────────────────────────────────────────────
ensure_key() {
    if [ -f "$KEY" ] && [ -f "$KEY.pub" ]; then
        log "key present: $KEY"
        return 0
    fi
    log "no key at $KEY — generating ed25519"
    mkdir -p "$(dirname "$KEY")"
    chmod 700 "$(dirname "$KEY")"
    # -N '' : no passphrase, required for unattended cron use
    ssh-keygen -t ed25519 -N '' -f "$KEY" -C "hevolve-ops-$(hostname)" >/dev/null
    chmod 600 "$KEY"
    log "generated $KEY"
}

# ── 2. does the target already trust us? ─────────────────────────────────
# Split user@host:port into ssh args. Returns 0 if passwordless works.
target_ok() {
    _t="$1"
    _port=22
    case "$_t" in *:*) _port="${_t##*:}"; _t="${_t%:*}";; esac
    # shellcheck disable=SC2086
    ssh -i "$KEY" $SSH_OPTS -p "$_port" "$_t" 'exit 0' >/dev/null 2>&1
}

# ── 3. bootstrap a target using a one-time password ──────────────────────
bootstrap() {
    _t="$1"
    _port=22
    case "$_t" in *:*) _port="${_t##*:}"; _t="${_t%:*}";; esac
    if [ -z "${TARGET_PASS:-}" ]; then
        log "  NEEDS BOOTSTRAP but TARGET_PASS is unset — skipping"
        return 1
    fi
    if ! command -v sshpass >/dev/null 2>&1; then
        log "  sshpass missing; cannot bootstrap unattended"
        return 1
    fi
    log "  installing pubkey via ssh-copy-id"
    sshpass -p "$TARGET_PASS" ssh-copy-id -i "$KEY.pub" \
        -o StrictHostKeyChecking=no -p "$_port" "$_t" >/dev/null 2>&1
    return $?
}

# ── main ─────────────────────────────────────────────────────────────────
ensure_key

TARGETS="$*"
if [ -z "$TARGETS" ]; then
    TARGETS="$(echo "${HEVOLVE_SSH_TARGETS:-}" | tr ',' ' ')"
fi
if [ -z "$TARGETS" ]; then
    log "no targets given (argv or HEVOLVE_SSH_TARGETS)"
    exit 0
fi

rc=0
for t in $TARGETS; do
    log "target $t"
    if target_ok "$t"; then
        log "  OK — key auth already works"
        continue
    fi
    if bootstrap "$t" && target_ok "$t"; then
        log "  BOOTSTRAPPED — key auth now works"
    else
        log "  FAILED — still no key access"
        rc=1
    fi
done

exit $rc

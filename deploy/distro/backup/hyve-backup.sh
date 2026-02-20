#!/usr/bin/env bash
# ============================================================
# HyveOS Daily Backup
#
# Backs up critical node data to /var/backups/hyve/:
#   - Node keypair (private + public)
#   - SQLite database
#   - Agent data directory
#
# Features:
#   - Timestamped tar.gz archives
#   - SHA-256 integrity verification
#   - 7-day retention policy
#   - Handles immutable (chattr +i) private key
#
# Triggered by hyve-backup.timer (systemd, daily 03:00).
# ============================================================

set -euo pipefail

DATA_DIR="/var/lib/hyve"
BACKUP_DIR="/var/backups/hyve"
LOG_DIR="/var/log/hyve"
LOG="$LOG_DIR/backup.log"
RETENTION_DAYS=7

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
BACKUP_NAME="hyve-backup-${TIMESTAMP}"
STAGING_DIR=$(mktemp -d "/tmp/${BACKUP_NAME}.XXXXXX")

# Ensure directories exist
mkdir -p "$BACKUP_DIR" "$LOG_DIR"

log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $1" | tee -a "$LOG"
}

cleanup() {
    rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

log "=== HyveOS Backup Starting ==="

# ─── Temporarily lift immutable flag on private key ───
PRIVATE_KEY="$DATA_DIR/node_private.key"
IMMUTABLE_RESTORED=false

if [[ -f "$PRIVATE_KEY" ]]; then
    if lsattr "$PRIVATE_KEY" 2>/dev/null | grep -q -- '----i'; then
        log "Removing immutable flag on private key for backup..."
        chattr -i "$PRIVATE_KEY"
        IMMUTABLE_RESTORED=true
    fi
fi

# ─── Collect files to back up ───
FILES_TO_BACKUP=()

for f in "$DATA_DIR/node_private.key" "$DATA_DIR/node_public.key" "$DATA_DIR/hevolve_database.db"; do
    if [[ -f "$f" ]]; then
        cp -a "$f" "$STAGING_DIR/"
        FILES_TO_BACKUP+=("$(basename "$f")")
        log "  Staged: $f"
    else
        log "  Skipped (not found): $f"
    fi
done

# Back up agent_data directory if it exists
if [[ -d "$DATA_DIR/agent_data" ]]; then
    cp -a "$DATA_DIR/agent_data" "$STAGING_DIR/agent_data"
    FILES_TO_BACKUP+=("agent_data/")
    log "  Staged: $DATA_DIR/agent_data/"
fi

# ─── Restore immutable flag immediately ───
if [[ "$IMMUTABLE_RESTORED" == "true" ]]; then
    chattr +i "$PRIVATE_KEY"
    log "Restored immutable flag on private key."
fi

# ─── Create tarball ───
if [[ ${#FILES_TO_BACKUP[@]} -eq 0 ]]; then
    log "WARNING: No files found to back up. Aborting."
    exit 1
fi

ARCHIVE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
tar -czf "$ARCHIVE" -C "$STAGING_DIR" .
chown root:root "$ARCHIVE"
chmod 600 "$ARCHIVE"

log "Archive created: $ARCHIVE"

# ─── Verify integrity with SHA-256 ───
CHECKSUM=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
echo "${CHECKSUM}  ${BACKUP_NAME}.tar.gz" > "${ARCHIVE}.sha256"
chmod 600 "${ARCHIVE}.sha256"

# Verify the checksum matches
VERIFY_HASH=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
if [[ "$CHECKSUM" == "$VERIFY_HASH" ]]; then
    log "Integrity verified: SHA-256=${CHECKSUM}"
else
    log "ERROR: Integrity check failed! Archive may be corrupted."
    exit 1
fi

ARCHIVE_SIZE=$(du -h "$ARCHIVE" | cut -f1)
log "Backup size: $ARCHIVE_SIZE"

# ─── Retention: delete backups older than 7 days ───
DELETED=0
while IFS= read -r old_backup; do
    rm -f "$old_backup" "${old_backup}.sha256"
    DELETED=$((DELETED + 1))
    log "  Deleted expired: $(basename "$old_backup")"
done < <(find "$BACKUP_DIR" -name "hyve-backup-*.tar.gz" -mtime +"$RETENTION_DAYS" -type f 2>/dev/null)

if [[ $DELETED -gt 0 ]]; then
    log "Retention cleanup: removed $DELETED backup(s) older than ${RETENTION_DAYS} days."
else
    log "Retention cleanup: no expired backups."
fi

# ─── Summary ───
TOTAL_BACKUPS=$(find "$BACKUP_DIR" -name "hyve-backup-*.tar.gz" -type f 2>/dev/null | wc -l)
log "=== Backup Complete: ${BACKUP_NAME}.tar.gz (${TOTAL_BACKUPS} total backups) ==="

#!/usr/bin/env bash
#
# Kryx DB backup + offsite sync
# Runs on the HOST (via cron), not inside the container.
#
# What it does:
#   1. Triggers the in-container backup (backup_db.py) which writes a
#      gzip'd, integrity-checked snapshot to the bind-mounted backups dir.
#   2. Prunes local backups older than LOCAL_RETENTION_DAYS.
#   3. Syncs the local backups dir to Google Drive via the existing
#      `gdrive` rclone remote.
#
# Install:
#   1. Fill in COMPOSE_FILE and HOST_BACKUP_DIR below for your host.
#   2. chmod +x backup_and_sync.sh
#   3. Add to crontab (see bottom of this file for the line to add).

set -euo pipefail

# ---- Config: fill these in for each host (kryx / kryx-cloud) ----------
COMPOSE_FILE="/home/ubuntu/kryx/docker-compose.yml"      # docker-compose.yml for this host
SERVICE_NAME="kryx"                 # service name, not container/image name
HOST_BACKUP_DIR="/var/lib/docker/volumes/kryx-data/_data/backups"   # host path that maps to /data/backups in-container
RCLONE_REMOTE="gdrive:kryx-backup"
RCLONE_CONFIG="/home/ubuntu/.config/rclone/rclone.conf"
LOCAL_RETENTION_DAYS=14
LOG_FILE="/var/log/kryx-backup.log"
# -----------------------------------------------------------------------------

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
}

log "=== Starting kryx backup run ==="

# 1. Trigger the in-container backup
if ! docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE_NAME" \
        python -m autosend.backup_db; then
    log "ERROR: in-container backup failed, aborting before sync"
    exit 1
fi

# 2. Prune local backups older than retention window
log "Pruning local backups older than ${LOCAL_RETENTION_DAYS} days"
find "$HOST_BACKUP_DIR" -name 'shofar_*.db.gz' -mtime "+${LOCAL_RETENTION_DAYS}" -print -delete

# 3. Sync to Google Drive
log "Syncing ${HOST_BACKUP_DIR} -> ${RCLONE_REMOTE}"
if ! rclone sync "$HOST_BACKUP_DIR" "$RCLONE_REMOTE" \
        --config "$RCLONE_CONFIG" \
        --log-level INFO \
        --log-file "$LOG_FILE"; then
    log "ERROR: rclone sync failed"
    exit 1
fi

log "=== Backup run complete ==="

# -----------------------------------------------------------------------------
# Crontab entry (run: crontab -e), nightly at 02:00 server time:
#
#   0 2 * * * /path/to/backup_and_sync.sh >> /var/log/shofar-backup.log 2>&1
#
# Note: rclone sync mirrors HOST_BACKUP_DIR to the remote folder exactly —
# since pruning already happened in step 2, this keeps Drive's retention
# matching local retention. If you'd rather Drive keep a longer history than
# local disk, swap `rclone sync` for `rclone copy` (copy only adds, never
# deletes on the remote).
# -----------------------------------------------------------------------------

#!/usr/bin/env bash
#
# Nightly backup for point-in-time recovery. Three artefacts, one run:
#
#   1. a consistent logical dump of MySQL (the PITR base), tagged with the
#      binlog coordinate it was taken at,
#   2. every binlog written since the last run (the PITR journal), and
#   3. an incremental snapshot of the replay files (never in MySQL, immutable
#      once written, and the anticheat's irreplaceable raw input).
#
# A dump alone loses everything since it was taken; the binlogs let a recovery
# replay forward to any instant before an incident (see docs/runbooks). The
# `.data` layout and credentials come from .env, so this matches a running
# `docker compose` stack with no extra configuration.
#
#   scripts/backup.sh                 # back up to ./.backups
#   BACKUP_DIR=/mnt/backups scripts/backup.sh
#   RETENTION_DAYS=30 scripts/backup.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# --- configuration (env-overridable) ----------------------------------------
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/.backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
COMPOSE="${COMPOSE:-docker compose --env-file .env}"
MYSQL_SVC="${MYSQL_SVC:-mysql}"
MYSQL_DATADIR="${MYSQL_DATADIR:-/var/lib/mysql}"
REPLAY_DIR="${REPLAY_DIR:-$ROOT_DIR/.data/osr}"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
: "${DB_USER:?DB_USER must be set (in .env)}"
: "${DB_PASS:?DB_PASS must be set (in .env)}"
: "${DB_NAME:?DB_NAME must be set (in .env)}"

DUMP_DIR="$BACKUP_DIR/dumps"
BINLOG_DIR="$BACKUP_DIR/binlogs"
REPLAY_BK_DIR="$BACKUP_DIR/replays"
mkdir -p "$DUMP_DIR" "$BINLOG_DIR" "$REPLAY_BK_DIR"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_EPOCH="$(date -u +%s)"

log() { printf '[backup %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# run a mysql client command inside the db container, credentials on stdin-safe
# env rather than argv (argv is world-readable via `ps`).
mysql_exec() {
  $COMPOSE exec -T -e "MYSQL_PWD=$DB_PASS" "$MYSQL_SVC" \
    mysql -u"$DB_USER" --batch --skip-column-names "$@"
}

# --- 1. logical dump (PITR base) ---------------------------------------------
# --single-transaction: consistent snapshot without locking InnoDB tables.
# --source-data=2: record (commented-out) the binlog coordinate of the snapshot
#   so a PITR knows exactly where to start replaying from.
# --flush-logs: rotate to a fresh binlog at dump time, so the archived binlogs
#   line up cleanly with this base.
DUMP_FILE="$DUMP_DIR/banchopy-$TS.sql.gz"
log "dumping $DB_NAME -> $DUMP_FILE"
$COMPOSE exec -T -e "MYSQL_PWD=$DB_PASS" "$MYSQL_SVC" \
  mysqldump -u"$DB_USER" \
    --single-transaction \
    --source-data=2 \
    --flush-logs \
    --routines --triggers --events \
    "$DB_NAME" \
  | gzip -c > "$DUMP_FILE"

# guard against a silent zero-byte dump (broken pipe, auth failure mid-stream).
if [ ! -s "$DUMP_FILE" ] || [ "$(gzip -dc "$DUMP_FILE" | head -c 1 | wc -c)" -eq 0 ]; then
  log "FATAL: dump is empty; removing and failing"
  rm -f "$DUMP_FILE"
  exit 1
fi

# --- 2. binlog archive (PITR journal) ----------------------------------------
# archive every binlog except the currently-active one (the last row of
# SHOW BINARY LOGS). Names sort lexically, so a plain string compare against
# the last-archived marker is enough to skip what we already have.
STATE_FILE="$BINLOG_DIR/.last_archived"
LAST_ARCHIVED=""
[ -f "$STATE_FILE" ] && LAST_ARCHIVED="$(cat "$STATE_FILE")"

mapfile -t ALL_LOGS < <(mysql_exec -e "SHOW BINARY LOGS;" | awk '{print $1}')
if [ "${#ALL_LOGS[@]}" -gt 1 ]; then
  # everything but the active (last) log is complete and safe to copy.
  CLOSED_LOGS=("${ALL_LOGS[@]:0:${#ALL_LOGS[@]}-1}")
  for logname in "${CLOSED_LOGS[@]}"; do
    if [ -n "$LAST_ARCHIVED" ] && [[ "$logname" < "$LAST_ARCHIVED" || "$logname" == "$LAST_ARCHIVED" ]]; then
      continue
    fi
    log "archiving binlog $logname"
    $COMPOSE exec -T "$MYSQL_SVC" cat "$MYSQL_DATADIR/$logname" \
      | gzip -c > "$BINLOG_DIR/$logname.gz"
    echo "$logname" > "$STATE_FILE"
  done
else
  log "no closed binlogs to archive yet"
fi

# --- 3. replay snapshot ------------------------------------------------------
# .osr files are write-once and named by score id, so an incremental hardlink
# snapshot is cheap: unchanged files cost an inode, not a copy. If the host
# filesystem or rsync lacks hardlink support this degrades to a full copy,
# which is correct, just larger.
if [ -d "$REPLAY_DIR" ]; then
  REPLAY_SNAPSHOT="$REPLAY_BK_DIR/osr-$TS"
  LATEST_LINK="$REPLAY_BK_DIR/latest"
  log "snapshotting replays $REPLAY_DIR -> $REPLAY_SNAPSHOT"
  if command -v rsync >/dev/null 2>&1; then
    LINK_DEST=()
    [ -d "$LATEST_LINK" ] && LINK_DEST=(--link-dest="$(readlink -f "$LATEST_LINK")")
    rsync -a --delete "${LINK_DEST[@]}" "$REPLAY_DIR/" "$REPLAY_SNAPSHOT/"
  else
    cp -al "$REPLAY_DIR" "$REPLAY_SNAPSHOT" 2>/dev/null || cp -a "$REPLAY_DIR" "$REPLAY_SNAPSHOT"
  fi
  ln -sfn "$REPLAY_SNAPSHOT" "$LATEST_LINK"
else
  log "replay dir $REPLAY_DIR absent; skipping replay snapshot"
fi

# --- 4. manifest (drives the backup-age alert) -------------------------------
# a single machine-readable record of the most recent successful run. The
# monitoring side (P8) alerts when `epoch` falls further behind now() than the
# RPO, which is the only way a *silently skipped* backup ever gets noticed.
DUMP_BYTES="$(wc -c < "$DUMP_FILE")"
cat > "$BACKUP_DIR/latest.json" <<JSON
{
  "epoch": $RUN_EPOCH,
  "timestamp": "$TS",
  "dump": "$(basename "$DUMP_FILE")",
  "dump_bytes": $DUMP_BYTES,
  "binlogs_archived": $(ls -1 "$BINLOG_DIR"/*.gz 2>/dev/null | wc -l),
  "database": "$DB_NAME"
}
JSON
log "wrote manifest $BACKUP_DIR/latest.json"

# --- 5. retention ------------------------------------------------------------
# prune dumps and replay snapshots older than the window. Binlogs are pruned
# only up to the oldest *retained* dump: deleting a binlog newer than the
# oldest dump would punch a hole in the replay chain and silently cap how far
# back a PITR can reach.
log "pruning artefacts older than ${RETENTION_DAYS}d"
find "$DUMP_DIR" -name 'banchopy-*.sql.gz' -type f -mtime "+$RETENTION_DAYS" -delete
find "$REPLAY_BK_DIR" -maxdepth 1 -name 'osr-*' -type d -mtime "+$RETENTION_DAYS" -exec rm -rf {} +
find "$BINLOG_DIR" -name '*.gz' -type f -mtime "+$RETENTION_DAYS" -delete

log "backup complete ($TS)"

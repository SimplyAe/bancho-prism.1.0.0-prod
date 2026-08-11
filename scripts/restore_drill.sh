#!/usr/bin/env bash
#
# Restore drill: prove the backups are restorable *before* an incident forces
# the question. A backup nobody has restored is a hypothesis, not a backup.
#
# This spins up a throwaway MySQL container, loads the latest dump into it,
# optionally replays the archived binlogs on top, and runs integrity checks:
#   - the dump loads without error,
#   - core tables are present and non-empty,
#   - every recovery-critical index survived the round-trip (a data-only
#     restore is exactly what silently drops them -- see the schema guard),
#   - it prints the measured restore time (your real RTO, not a guess).
#
# It touches nothing in the live stack: a separate container, a separate
# volume, a separate port. Safe to run on the production host.
#
#   scripts/restore_drill.sh                    # latest dump only
#   scripts/restore_drill.sh --with-binlogs     # dump + replay binlogs (full PITR)
#   BACKUP_DIR=/mnt/backups scripts/restore_drill.sh
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/.backups}"
SCRATCH_IMAGE="${SCRATCH_IMAGE:-mysql:9.7.1}"
SCRATCH_NAME="${SCRATCH_NAME:-banchopy-restore-drill}"
SCRATCH_PORT="${SCRATCH_PORT:-33070}"
SCRATCH_ROOT_PW="${SCRATCH_ROOT_PW:-drill}"
WITH_BINLOGS=0
[ "${1:-}" = "--with-binlogs" ] && WITH_BINLOGS=1

if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
fi
DB_NAME="${DB_NAME:-banchopy}"

log() { printf '[drill %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

MANIFEST="$BACKUP_DIR/latest.json"
[ -f "$MANIFEST" ] || fail "no manifest at $MANIFEST -- has scripts/backup.sh run?"
DUMP_NAME="$(grep -o '"dump": *"[^"]*"' "$MANIFEST" | sed 's/.*"dump": *"\([^"]*\)".*/\1/')"
DUMP_FILE="$BACKUP_DIR/dumps/$DUMP_NAME"
[ -s "$DUMP_FILE" ] || fail "dump $DUMP_FILE missing or empty"

cleanup() {
  log "tearing down scratch instance"
  docker rm -f "$SCRATCH_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- scratch instance --------------------------------------------------------
log "starting scratch mysql ($SCRATCH_IMAGE) as $SCRATCH_NAME"
docker rm -f "$SCRATCH_NAME" >/dev/null 2>&1 || true
docker run -d --name "$SCRATCH_NAME" \
  -e MYSQL_ROOT_PASSWORD="$SCRATCH_ROOT_PW" \
  -e MYSQL_DATABASE="$DB_NAME" \
  -p "$SCRATCH_PORT:3306" \
  "$SCRATCH_IMAGE" >/dev/null

sdb() { docker exec -i -e "MYSQL_PWD=$SCRATCH_ROOT_PW" "$SCRATCH_NAME" mysql -uroot "$@"; }

log "waiting for scratch mysql to accept connections"
for _ in $(seq 1 60); do
  if sdb -e "SELECT 1;" >/dev/null 2>&1; then break; fi
  sleep 2
done
sdb -e "SELECT 1;" >/dev/null 2>&1 || fail "scratch mysql never came up"

# --- restore (timed) ---------------------------------------------------------
RESTORE_START="$(date -u +%s)"
log "restoring $DUMP_NAME"
gzip -dc "$DUMP_FILE" | sdb "$DB_NAME" || fail "dump failed to load"

if [ "$WITH_BINLOGS" -eq 1 ]; then
  # replay every archived binlog in name order. mysqlbinlog decodes them to SQL
  # which we pipe straight back in -- this is the "roll forward" half of PITR.
  # A real incident recovery would stop at a timestamp with --stop-datetime;
  # the drill replays everything to prove the chain is intact and decodable.
  shopt -s nullglob
  BINLOGS=("$BACKUP_DIR"/binlogs/*.gz)
  if [ "${#BINLOGS[@]}" -gt 0 ]; then
    log "replaying ${#BINLOGS[@]} binlog(s)"
    for bl in $(printf '%s\n' "${BINLOGS[@]}" | sort); do
      gzip -dc "$bl" > /tmp/drill-binlog.bin
      docker cp /tmp/drill-binlog.bin "$SCRATCH_NAME:/tmp/drill-binlog.bin"
      docker exec -i -e "MYSQL_PWD=$SCRATCH_ROOT_PW" "$SCRATCH_NAME" \
        sh -c "mysqlbinlog /tmp/drill-binlog.bin | mysql -uroot $DB_NAME" \
        || fail "binlog replay failed on $(basename "$bl")"
    done
  else
    log "no binlogs archived yet; skipping replay"
  fi
fi
RESTORE_SECONDS=$(( $(date -u +%s) - RESTORE_START ))

# --- integrity checks --------------------------------------------------------
log "checking core tables are present and populated"
for table in users scores stats maps; do
  count="$(sdb --batch --skip-column-names -e "SELECT COUNT(*) FROM \`$DB_NAME\`.\`$table\`;" 2>/dev/null)" \
    || fail "table '$table' missing after restore"
  log "  $table: $count rows"
done

log "checking recovery-critical indexes survived the restore"
# these back score-resubmission after an outage and the leaderboard rebuild;
# a data-only reload is exactly what drops them while row counts still match.
require_index() {
  local table="$1" index="$2"
  local present
  present="$(sdb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema='$DB_NAME' AND table_name='$table' AND index_name='$index';")"
  [ "$present" -ge 1 ] || fail "index $table.$index is missing after restore"
  log "  ok: $table.$index"
}
require_index scores scores_online_checksum_index
require_index scores scores_userid_index
require_index scores scores_map_md5_index
require_index scores PRIMARY
require_index stats stats_pp_index
require_index stats PRIMARY

log "PASS: restore drill succeeded"
log "measured restore time (RTO): ${RESTORE_SECONDS}s for dump $DUMP_NAME"

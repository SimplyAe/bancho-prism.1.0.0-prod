# Runbook: Disk full

`No space left on device` (ENOSPC). Dangerous because it fails writes in ways
the databases don't surface as data errors: MySQL can't extend a tablespace or
write its binlog, redis can't append to its AOF, and score submission can't
write `.osr` files. Left alone it turns into corruption and a crashloop.

## Detection signal

- `GET /ready` fails on `replays` (`"detail": "...No space left..."`) or `mysql`.
- App logs: `OSError: [Errno 28] No space left on device`.
- MySQL logs: can't write binlog / temp / ibdata.
- Confirm:
  ```sh
  df -h                      # which filesystem is at 100%
  docker system df           # how much docker itself is holding
  du -sh .data/osr .backups /var/lib/docker/volumes/* 2>/dev/null | sort -h | tail
  ```

## Mitigation

Free space **now**, from the safest sources first. The goal is to get below
~85% so writes succeed again — not to do a full cleanup yet.

Safe to delete immediately (regenerable or already archived):
```sh
docker image prune -f                 # dangling images
docker container prune -f             # stopped one-offs
journalctl --vacuum-size=200M         # if systemd journald is the hog
```

Reclaimable with judgement:
- Old backups **that are already copied off-host** — never the only copy:
  ```sh
  ls -lh .backups/dumps .backups/binlogs
  ```
- App logs if they're not shipped elsewhere.

Do **not**, to free space:
- delete binlogs newer than the oldest retained dump — that silently caps how
  far a PITR can roll forward (see [mysql-lost](mysql-lost.md)),
- delete `.data/osr` replays — they're irreplaceable and not in any DB,
- `docker volume prune` blindly — it can take `db-data`/`redis-data`.

## Recovery

Once you're back under the line:

1. Confirm the databases are healthy, not just running:
   ```sh
   docker compose exec mysql mysqladmin ping
   redis-cli ping
   docker compose logs --tail=100 mysql redis   # look for prior write errors
   ```
2. If MySQL hit ENOSPC mid-write, check for corruption before trusting it
   (`CHECK TABLE scores;` on the hot tables). If corrupt, this becomes
   [mysql-lost](mysql-lost.md).
3. Address the root cause so it doesn't refill:
   - **replays growing unbounded** is expected — that's why they have their own
     backup + retention. Make sure `scripts/backup.sh` retention is actually
     running and old snapshots are pruned.
   - move `.backups` to a separate/larger volume, or shorten `RETENTION_DAYS`.
   - if binlogs are the hog, they expire per `binlog_expire_logs_seconds`
     (7 days, `ext/mysql/pitr.cnf`) — shorten only if archiving keeps up.
4. Restart anything that crashed on ENOSPC: `docker compose up -d`.

## Verification

- `df -h` shows healthy headroom (target < 85%) on the affected filesystem.
- `GET /ready` → `200` on all dependencies including `replays`.
- A real write path works end to end: submit a test score, confirm both the
  `scores` row and its `.osr` appear (`python -m tools.reconcile_replays`
  reports no *new* divergence).
- The nightly `scripts/backup.sh` completes and updates `.backups/latest.json`.
- Set an alert on disk usage so the next one pages *before* ENOSPC, not after.

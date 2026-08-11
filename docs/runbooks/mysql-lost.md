# Runbook: MySQL lost, corrupt, or unreachable

MySQL is the source of truth. Everything else (redis ranks, replay analysis)
can be rebuilt from it; it cannot be rebuilt from them. This is the runbook the
backups exist for.

First decide which of two very different situations you're in:

- **Unreachable but intact** — the data is fine, the server can't reach it
  (network, container down, credentials, disk). Fix the connection; do **not**
  restore.
- **Lost or corrupt** — data is actually gone or damaged. Restore from backup,
  then roll forward with binlogs.

## Detection signal

- `GET /ready` returns `503` with `"mysql": {"healthy": false, ...}` naming the
  error.
- Score submission and login fail; `bancho_logins_total{result="failure"}`
  climbs.
- App logs show connection errors, or InnoDB corruption on the mysql container.

Triage which situation:

```sh
docker compose ps mysql                       # is it even running?
docker compose exec mysql mysqladmin ping     # "mysqld is alive"?
docker compose exec -e MYSQL_PWD="$DB_PASS" mysql \
  mysql -u"$DB_USER" -e "SELECT COUNT(*) FROM $DB_NAME.users;"
```

If that `SELECT` returns a sane count, the data is **intact** — this is a
connectivity problem, skip to *Recovery: unreachable*. If it errors with
corruption or the table/db is gone, go to *Recovery: lost or corrupt*.

## Mitigation

- The app is `restart: unless-stopped` and reconnects on its own once mysql is
  back — you do not need to touch the app to restore its DB connection.
- If mysql is down hard and will be for a while, expect `/ready` to keep the
  server out of rotation. That is correct; leave it.
- **Do not** delete the `db-data` volume or re-run init while triaging. A
  corrupt datadir is still evidence and may be partially recoverable.

## Recovery: unreachable (data intact)

Work outward from the container:

1. `docker compose up -d mysql` if it's stopped; check `docker compose logs mysql`.
2. Verify the healthcheck passes: `docker compose ps` shows `mysql` healthy.
3. If it's a credentials/DSN mismatch, the app logs the auth error — fix `.env`
   (`DB_USER`/`DB_PASS`/`DB_HOST`/`DB_PORT`/`DB_NAME`) and `docker compose up -d bancho`.
4. If it's disk, see [disk-full](disk-full.md) first.

No restore. Once `/ready` is `200`, you're done — jump to Verification.

## Recovery: lost or corrupt (restore + roll forward)

This is point-in-time recovery: load the last logical dump, then replay the
archived binlogs on top to recover the writes that happened *after* the dump.

1. **Stop writers** so nothing races the restore:
   ```sh
   docker compose stop bancho
   ```
2. **Confirm you have a good backup** before destroying anything:
   ```sh
   cat .backups/latest.json          # epoch, dump name, size
   scripts/restore_drill.sh --with-binlogs   # proves it restores, on a scratch instance
   ```
   Only proceed past a **passing** drill.
3. **Rebuild the datadir** and restore the base dump:
   ```sh
   docker compose stop mysql
   docker volume rm <project>_db-data           # discard the corrupt datadir
   docker compose up -d mysql                    # fresh init from migrations/base.sql
   # wait for healthy, then load the dump:
   gzip -dc .backups/dumps/<dump-from-latest.json>.sql.gz \
     | docker compose exec -T -e MYSQL_PWD="$DB_PASS" mysql mysql -u"$DB_USER" "$DB_NAME"
   ```
4. **Roll forward** with the archived binlogs (recovers post-dump writes). To
   recover *everything* up to the failure, replay them all in order:
   ```sh
   for f in $(ls -1 .backups/binlogs/*.gz | sort); do
     gzip -dc "$f" \
       | docker compose exec -T -e MYSQL_PWD="$DB_PASS" mysql \
           sh -c "mysqlbinlog - | mysql -u$DB_USER $DB_NAME"
   done
   ```
   (To stop *before* a specific bad statement instead — e.g. an errant
   `DELETE` — see [bad-admin-action](bad-admin-action.md), which uses the same
   binlogs with `--stop-datetime`.)
5. **Restart writers**: `docker compose up -d bancho`.

## Verification

- `GET /ready` → `200`, `"mysql": {"healthy": true}`.
- Row counts are sane vs. what you expect:
  ```sh
  SELECT COUNT(*) FROM users; SELECT COUNT(*) FROM scores; SELECT MAX(id) FROM scores;
  ```
- **Indexes survived the restore** — a data-only reload silently drops them.
  The restore drill already checks this; on the live instance:
  ```sh
  SHOW INDEX FROM scores;    -- expect 10 (9 secondary + PRIMARY)
  ```
- Redis ranks were untouched by the DB restore but may now disagree with the
  restored pp. Rebuild them: `python -m tools.rebuild_leaderboards`
  (see [redis-flushed](redis-flushed.md)).
- Replay files vs. rows: `python -m tools.reconcile_replays` — expected to
  report some *missing* replays if you restored to a point behind the newest
  scores; that's correct, not a new fault.
- Record the actual wall-clock recovery time as your measured RTO.

# Runbook: Bad admin action

The realistic disaster. Not hardware — a human with credentials ran something
destructive: a `DELETE`/`UPDATE` without the right `WHERE`, a wrong migration, a
mass-restrict, a `DROP`. The data was *validly* written, so no integrity check
fires. This is the exact case point-in-time recovery exists for: restore the
last dump, then replay the binlogs up to the instant **before** the mistake.

## Detection signal

- A person reports "I just ran X and now Y is gone / wrong."
- A sudden step change: `SELECT COUNT(*)` on a table far from expected, a wave
  of users restricted/deleted at the same timestamp.
- Unlike other runbooks, `/ready` is **green** and metrics may look fine — the
  server is healthy, the *data* is wrong.

Pin down the moment as precisely as you can — the recovery target depends on it:
```sh
# from shell history, audit_log, app logs, or the binlog itself:
docker compose exec mysql sh -c \
  'mysqlbinlog --start-datetime="2026-08-11 14:00:00" /var/lib/mysql/binlog.000042' \
  | grep -n -iE 'delete|update|drop|alter' | head
```
You want the timestamp (or binlog file+position) *immediately before* the bad
statement.

## Mitigation

**Stop writes immediately.** Every new write after the mistake is one you'll
have to redo by hand, because PITR can't cherry-pick around it:

```sh
docker compose stop bancho
```

Do **not** try to "undo" with an opposite statement (another `UPDATE` to put
values back) unless the change is tiny and you're certain — you'll usually make
the recovery harder and destroy evidence of the original state. Freeze, then
restore.

## Recovery — point-in-time recovery to just-before

The mistake is *inside* the binlog stream. Restore the base dump (which predates
it) and roll forward, stopping right before the bad statement.

1. **Verify you can restore before touching prod** — dry-run on a scratch
   instance, which also lets you find the exact stop point safely:
   ```sh
   scripts/restore_drill.sh --with-binlogs
   ```
2. **Identify the stop point.** Either a timestamp:
   `--stop-datetime="2026-08-11 14:32:07"`, or, more precisely, a binlog
   file + position from the `grep` above: `--stop-position=<pos>` on the
   specific `--start-position` binlog. Position is exact; timestamp can catch
   two statements sharing a second.
3. **Rebuild the datadir and load the base dump** (as in
   [mysql-lost](mysql-lost.md) step 3).
4. **Roll forward, stopping before the mistake.** Replay whole binlogs up to the
   one containing the bad statement, then replay that last one with a stop
   point:
   ```sh
   # earlier binlogs in full:
   for f in binlog.000040 binlog.000041; do
     docker compose exec -T -e MYSQL_PWD="$DB_PASS" mysql \
       sh -c "mysqlbinlog /var/lib/mysql/$f | mysql -u$DB_USER $DB_NAME"
   done
   # the binlog holding the mistake, stopping just before it:
   docker compose exec -T -e MYSQL_PWD="$DB_PASS" mysql sh -c \
     "mysqlbinlog --stop-datetime='2026-08-11 14:32:07' /var/lib/mysql/binlog.000042 \
      | mysql -u$DB_USER $DB_NAME"
   ```
   (Archived, off-host copies live in `.backups/binlogs/*.gz` — `gzip -dc` them
   if the on-box originals are gone.)
5. **Reconcile what you deliberately left behind.** Any *legitimate* writes
   between the mistake and when you froze writes were rolled back too. Recover
   them by hand from the binlog if they matter (that's why we froze fast — to
   keep this set small).
6. **Restart writers:** `docker compose up -d bancho`.

## Verification

- The destroyed data is back and correct: the `COUNT`/rows that were wrong now
  match expectations.
- The bad statement's effect is **absent** (the restricted users are
  unrestricted, the deleted rows present, etc.).
- Indexes intact after the reload: `SHOW INDEX FROM scores;` (expect 10).
- Rebuild redis ranks, which were untouched by the DB rollback and now disagree
  with restored pp: `python -m tools.rebuild_leaderboards`.
- `python -m tools.reconcile_replays` to re-square score rows with `.osr` files
  after the row set changed.
- Write down the timeline (mistake time, freeze time, target, RTO) and, most
  importantly, remove the ability to repeat it: least-privilege DB accounts,
  reviewed migrations, no ad-hoc `DELETE` on prod without a transaction.

# Runbooks

Incident procedures for the server. Each one is written for the person on the
other end of a page at 3am who did not build the system, so every step is a
command to run or a signal to read, not a description of how the code works.

Every runbook has the same four sections, in the order you need them:

1. **Detection signal** — what you saw that brought you here, and the one-line
   check that confirms this is the right runbook.
2. **Mitigation** — stop the bleeding. Buys time; does not fix root cause.
3. **Recovery** — restore correct state.
4. **Verification** — the check that proves you are actually done, not just
   that the error stopped showing up.

## Index

| Runbook | You are here because… |
|---|---|
| [redis-flushed](redis-flushed.md) | ranks read as unranked / leaderboards empty |
| [mysql-lost](mysql-lost.md) | the database is gone, corrupt, or unreachable |
| [crashloop](crashloop.md) | a container keeps restarting |
| [disk-full](disk-full.md) | writes failing, `No space left on device` |
| [bad-deploy](bad-deploy.md) | a release made things worse |
| [bad-admin-action](bad-admin-action.md) | a mistaken query/migration destroyed data |

## The tools these runbooks lean on

- **Health**: `GET /health` (liveness — process up), `GET /ready` (readiness —
  per-dependency: mysql, redis, replay volume). `503` from `/ready` names the
  failed dependency in its body.
- **Metrics**: `GET /metrics` (bearer `METRICS_TOKEN`). Key series:
  `bancho_online_players`, `bancho_scores_submitted_total`,
  `bancho_logins_total`, `bancho_background_task_consecutive_failures`,
  `bancho_http_request_duration_seconds`.
- **Leaderboard rebuild**: `python -m tools.rebuild_leaderboards` — rebuilds the
  redis ranks from mysql, atomically. Also runs automatically at startup when
  the global leaderboard is empty but mysql has ranked players.
- **Replay reconciliation**: `python -m tools.reconcile_replays` — finds score
  rows with no `.osr` and orphan `.osr` files with no row.
- **Backups**: `scripts/backup.sh` (nightly dump + binlog archive + replay
  snapshot). `scripts/restore_drill.sh` (restore into a scratch instance and
  verify). Manifest of the last good run: `.backups/latest.json`.
- **Restart policy**: every service is `restart: unless-stopped`. A crash comes
  back on its own; a `docker compose stop` stays stopped.

## Before an incident

Run the drill on a schedule, not for the first time during an outage:

```sh
scripts/backup.sh                    # nightly, via cron/systemd timer
scripts/restore_drill.sh             # weekly — records your real RTO
```

If `restore_drill.sh` has never passed, you do not have backups. You have files.

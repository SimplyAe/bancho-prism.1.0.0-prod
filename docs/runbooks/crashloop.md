# Runbook: Crashloop

A container keeps exiting and being restarted by `restart: unless-stopped`. The
restart policy is doing its job — keeping the service trying — but a *loop*
means each restart hits the same wall. Your goal is to read the wall, not to
keep restarting.

## Detection signal

- `docker compose ps` shows a service `Restarting` or with a rising restart
  count.
- `bancho_online_players` sawtooths (drops to 0, climbs, drops) as the app dies
  and reconnects.
- The container healthcheck flaps between `healthy` and `unhealthy`.

Confirm and read the reason:

```sh
docker compose ps
docker inspect --format '{{.RestartCount}} {{.State.Error}} {{.State.ExitCode}}' <container>
docker compose logs --tail=200 bancho
```

Exit code is the first fork in the road:
- **Exit 0 shortly after start** — historically this was the *failed-migration*
  trap (a bad migration raised `KeyboardInterrupt`, which looks like a clean
  shutdown). That path now raises a real `MigrationError` with a non-zero exit,
  so a clean `0` today is more likely a config/env issue caught at import.
- **Non-zero exit** — real crash; the traceback is in the logs.
- **Healthcheck killing it** — the process is alive but `/health` isn't
  answering (wedged event loop, port not bound). Logs show no crash, but
  `docker inspect` shows the healthcheck failing.

## Mitigation

Stop the loop so you can work without the container thrashing under you — this
is deliberate, so `unless-stopped` won't fight you:

```sh
docker compose stop bancho
```

Then run it in the foreground to watch it fail in real time:

```sh
docker compose run --rm bancho
```

If the crash is in one dependency (e.g. mysql), the app crashlooping is a
*symptom* — go fix that dependency first ([mysql-lost](mysql-lost.md),
[disk-full](disk-full.md)). The app is designed to retry DB/redis connects at
startup with backoff, so a dependency that's *slow* shouldn't loop the app;
a dependency that's *absent* past the retry budget will.

## Recovery

Match the fix to the reason you found above:

- **Bad config / missing env var** — settings validation reports *every*
  missing/invalid var at once (P2). Fix `.env`, `docker compose up -d bancho`.
- **Failed migration** — the log names the failing statement. Fix the migration
  or the DB state, then restart. Do not skip migrations to get past it.
- **Bad release** — if the loop started right after a deploy, this is really a
  [bad-deploy](bad-deploy.md); roll back the image.
- **Wedged healthcheck, process otherwise fine** — capture a stack dump before
  killing it (so the hang is diagnosable), then restart. If it recurs, treat as
  a code bug, not an ops incident.
- **Dependency down** — fix the dependency; the app recovers on its own.

## Verification

- `docker inspect --format '{{.RestartCount}}' <container>` stops climbing.
- `docker compose ps` shows the service `healthy` and stable for several minutes.
- `GET /ready` → `200`.
- `bancho_online_players` climbs back to its normal band and **stays** there
  (no sawtooth).
- The exit code / traceback that defined the incident no longer appears in
  `docker compose logs`.

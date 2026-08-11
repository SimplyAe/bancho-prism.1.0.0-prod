# Runbook: Bad deploy

A release made things worse — errors up, latency up, a feature broken, or a
crashloop that started the moment the new image went live. The fastest safe
action is almost always **roll back first, diagnose after**.

## Detection signal

- The incident's start lines up with a deploy (image tag / commit changed).
- After the deploy: `bancho_http_requests_total{status=~"5.."}` climbs,
  `bancho_http_request_duration_seconds` p99 jumps, or `bancho_logins_total`
  /`bancho_scores_submitted_total` fall off a cliff.
- New tracebacks in `docker compose logs bancho` that weren't there before.
- If it's a crashloop, you'll have arrived via [crashloop](crashloop.md);
  come back here if the trigger was a release.

Confirm what changed:
```sh
docker compose images bancho            # current image/tag in use
docker inspect --format '{{.Config.Image}}' <container>
git log --oneline -n 5                  # what shipped
```

## Mitigation

Roll back to the last known-good image. This is the mitigation *and* usually the
recovery — a deploy is reversible in a way a data change is not.

```sh
# pin the previous known-good tag (don't rely on :latest during an incident):
export BANCHO_IMAGE=bancho:<previous-good-tag>
docker compose up -d bancho
```

If you build locally rather than pull, check out the previous good commit and
rebuild:
```sh
git checkout <last-good-commit>
make build && docker compose up -d bancho
```

**Before rolling back, check whether the deploy ran a migration.** If it did,
the schema moved forward and a plain image rollback can leave old code against
new schema:
```sh
docker compose logs bancho | grep -i migrat
```
If a migration ran and is the problem, this is not a clean rollback — treat the
schema change with [bad-admin-action](bad-admin-action.md) (you may need to
restore/PITR to just before the migration). If the migration is benign and
backward-compatible, the image rollback alone is fine.

## Recovery

1. Confirm the rollback restored service (see Verification) — mitigation done.
2. Diagnose the bad release **off the hot path**: reproduce locally or on
   staging, `make utest && make type-check`, find the regression.
3. Fix forward: land the fix, re-run the suite, deploy the corrected image.
4. Capture why it wasn't caught pre-deploy (missing test, config-only change,
   migration ordering) and close that gap — that's the actual fix.

## Verification

- Error rate and p99 latency return to their pre-deploy baseline on `/metrics`.
- `GET /ready` → `200`; `docker compose ps` shows `bancho` healthy and stable.
- `bancho_online_players`, logins, and score submissions back to normal bands.
- The specific broken behavior from the report is exercised and works.
- The running image is the one you intended (`docker compose images bancho`),
  not a stale or `:latest` surprise.

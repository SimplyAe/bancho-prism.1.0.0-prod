# Prism

`1.0.0-prod`

Prism is a hard fork of [bancho.py](https://github.com/osuAkatsuki/bancho.py), an osu! private server backend. It started life as 5.3.x and then went its own way. No upstream rebase, no waiting on anyone else's roadmap. The whole idea was to take a solid but bare server and turn it into something you can actually run in production without it falling over the first time Redis hiccups or someone points a spoofer at it.

This is backend only. There's no website in here and there won't be one bundled. Prism keeps bancho.py's API and database contract intact (and adds a bunch on top), so any frontend that already talks to bancho.py can talk to Prism. Building or picking a frontend is a separate job for later. Everything below is about the server.

Short version if you don't want to read the whole thing: it's bancho.py, but it doesn't die when things go wrong, it watches replays for cheats and hands them to a human instead of swinging the ban hammer, it remembers your history, and it's got the social plumbing (feeds, follows, multiplayer records, spectator logs, a Discord #1 feed) that stock bancho.py just doesn't keep.

---

## Why bother forking

Stock bancho.py is a great starting point and genuinely well written. But out of the box it makes some assumptions that stop being true the second real people start hammering it:

- a container with no restart policy stays dead after a crash until someone wakes up and notices
- one flushed Redis and every rank on the server reads `0` until each player logs back in one at a time
- an `httpx` client with no timeout will happily hang a request forever if an upstream stalls
- the replay gets written to disk *after* the DB commit, on the event loop, so a bad moment leaves you with score rows that have no `.osr` and can never be analysed
- 40-something bare `os.environ[...]` reads means one missing env var is a `KeyError` at import with zero context

None of that is a dig. It's just the gap between "works on my machine" and "runs unattended for months." Prism closes that gap first, then builds features on top of a foundation that actually holds.

The build order was on purpose: **reliability, then anticheat, then data, then social.** Features multiply load. No point shipping a slick activity feed on top of a server that can't survive its Redis restarting.

---

## What's actually in it

Four tracks. Tracks 1 through 3 are done and tested. Track 4 (social) is mostly done with two things left. Here's the full list, feature by feature.

### 1. Reliability and recovery (done)

The boring stuff that matters most.

- **Process supervision.** `restart: unless-stopped` on mysql, redis, and bancho. Not `always`, on purpose, so a deliberate stop stays stopped. A real healthcheck on the app hitting a readiness endpoint, so a *hung* event loop gets caught, not just a hard crash. Redis runs AOF with `appendfsync everysec`, so most Redis incidents become "restart and keep going" instead of "rebuild everything."
- **Config that fails loud and early.** Rewrote `app/settings.py` on top of `pydantic-settings`. Same constant names everywhere (`app.settings.WHATEVER` still works at ~100 call sites), but now a bad or missing config lists *every* problem at once instead of dying on whichever var happened to be alphabetically first.
- **Connections that give up.** `httpx` got real timeouts, connection limits, and retries. Redis got a pool size, socket timeouts, `retry_on_timeout`, and a health check interval. A stalled upstream now fails in bounded time instead of eating a worker forever.
- **`get_ip` that doesn't trust the client.** Explicit trust chain: `CF-Connecting-IP`, then an authenticated client-IP header, then the *rightmost* untrusted XFF hop. Fail-closed for anything security related, fail-open only where it's spelled out. The old code trusted the leftmost (client-controlled) hop, which is exactly backwards.
- **Lifecycle that survives a slow boot.** Startup retries DB and Redis with backoff (via `tenacity`) instead of crashlooping when a dependency is 5 seconds late. Shutdown drains in-flight requests up to a bounded timeout. A failed migration raises a real `MigrationError` instead of (no joke) `KeyboardInterrupt`, which used to make a broken migration look like a clean Ctrl-C.
- **Leaderboard rebuild.** The one piece of data loss stock bancho.py can't fix in code. Prism can rebuild every rank from MySQL into a temp key and `RENAME` it over the live one. Atomic, never a half-populated window, skips restricted players. Triggers on startup if the key's empty, from an admin path, or `tools/rebuild_leaderboards.py`.
- **Score and replay durability.** Reordered: write the replay to a temp path, commit the row, then atomic `rename`. Orphan files are harmless and easy to GC. Orphan rows are permanently unanalysable, so that's the failure we designed out. The disk write moved off the event loop too. `tools/reconcile_replays.py` finds rows with no `.osr` and orphan `.osr` files with no row.
- **Background loops that don't die quietly.** Every loop body is wrapped so one exception doesn't permanently kill the sweep, with a metric per iteration so a dead loop is *visible*. Killed a bare `assert player is not None` that used to nuke the donor sweep if a user got deleted mid-iteration.
- **DB-authoritative API keys.** A key issued after startup is recognised without a restart. It used to be loaded into RAM once at boot, so any key you added later was invisible until you bounced the process.
- **Observability.** Prometheus plus structured JSON logs plus Sentry, with the existing Datadog hooks left intact so nobody's deployment breaks. `/health` (liveness), `/ready` (per-dependency: mysql, redis, disk, replay volume), `/metrics`. Counters and histograms for submissions, logins, pp-calc duration, pool saturation, online players, bg-loop liveness, and the AC queue depth.
- **Backups and runbooks.** Logical dumps plus binlog archiving for point-in-time recovery (the realistic disaster is a bad admin action, not a dead disk). A restore *drill* script that loads into a scratch container, checks the recovery-critical indexes are present, and prints the measured RTO. Six runbooks in `docs/runbooks/` (redis flushed, mysql lost, crashloop, disk full, bad deploy, bad admin action), each written as detection, mitigation, recovery, verification.

### 2. Anticheat, replay-based, flag-not-ban (done)

Hard rule I set for myself and stuck to: **build detection, never evasion, and flag for a human, never auto-ban.** Nothing here touches privileges. It surfaces suspicion to staff and writes an audit trail. A human makes the call.

- **`.osr` replay parser.** Pure, bounds-checked, LZMA1. Handles both full `.osr` files and the bare compressed frame block the client actually uploads (the header lives in the score row, not the file, which trips people up).
- **Feature extraction that's *tap-aligned*, not per-frame,** because that's how real aim reads: geometry between key presses. Frame-level mechanical signatures (frozen cursor, jitter, straight runs, constant-velocity runs) stay per-frame so they still catch relax and autopilot style injection regardless of tapping.
- **Detectors.** Hold-duration uniformity (autoclicker or injected zero-width holds), aim-controller fit ("too clean" aim that sits at the human median with no natural tail), robotic timing (pixel-identical tap combs), and timing/aim independence (catches partial cheats a single-channel detector under-weights).
- **Login-hash spoofer table.** osu! login sends four md5 hardware fingerprints. Known spoofers md5 a random int from a small band instead of real hardware. Prism precomputes that rainbow table once at startup and matches against it. Parameterized over candidate encodings because the exact preimage encoding isn't confirmed, so, again, flag-not-ban.
- **Redis queue plus a separate worker process.** Analysis never runs on the hot path. Submissions enqueue. A standalone `python -m app.anticheat_worker` process (its own container in compose) consumes, plus a keyset-paginated backfill that drains the cold-start backlog over passes. The durable source-of-truth for "analysed?" lives in a table, so queue duplicates are harmless.
- **Durable analysis stats.** One row per score in `score_replay_stats` holding the extracted features (JSON) plus the promoted scalar columns, so re-running detectors on a threshold bump never needs to re-read the replay off disk.
- **Staff review queue.** Flags persist one row per score. Re-analysis can refresh the detection but can *never* silently re-open a flag a human already dismissed. That stickiness is the whole guarantee.
- **Review API (v2, staff-gated).** List, filter (by status, mode, user), inspect, and resolve flags over HTTP. Auth is checked in-handler so a denial still comes back in the proper v2 error envelope instead of a bare 401 page.
- **Audit trail (`audit_law`).** Resolving a flag writes a terminal verdict into the same moderation `logs` trail as every other staff action, so an AC decision sits right next to restricts and silences. It records the decision only. It never touches privileges.

One honest caveat: real thresholds need to be re-derived from server-collected verified replays, and the spoofer preimage encoding needs validating against known-spoofed accounts. Both are blocked on live data, and the second one edges toward evasion research, so it's shelved. The bundled calibration file is a *reference*, not production thresholds.

### 3. Data foundation (done)

The stuff that's unrecoverable if you don't start collecting it *now*. Every day without it is a day of history you never get back.

- **`stat_snapshots`.** One immutable row per player, per mode, per day. Global and country rank computed straight from MySQL (`ROW_NUMBER() OVER (...)`), so a snapshot is correct even with cold Redis. Powers history, peak rank, and future divisions. Daily supervised loop, idempotent, safe to run on every boot.
- **Daily snapshot service and loop.** Pure service over the repo, runs on a 24h supervised loop on a UTC date boundary, with a metric on rows written so a silently-empty capture is visible instead of quietly losing a day.
- **Persisted pp skill components.** `akatsuki_pp_py` already computes `pp_aim`, `pp_speed`, and `pp_flashlight` on every submission, and stock bancho.py just throws them away. Prism threads them through and stores them. Nullable columns, so backfilling old scores is optional. New submissions capture them going forward.

### 4. Social and features (mostly done)

Everything stock bancho.py forgets the moment a connection drops.

- **Activity feed.** Service seam plus a v2 read surface plus producers off score submission and the daily snapshot (rank-ups, pp records). Done.
- **Follow graph.** The reverse direction (followers, counts, viewer relation) built on the existing relationships. Done.
- **Durable multiplayer history.** `mp_matches` and `mp_match_games`. Stock bancho.py forgets a lobby when the last player leaves. Prism keeps the match, its games, and per-player scoreboards (final score, combo, accuracy, mods, team, pass/fail, placement). Write side is fire-and-forget off the packet handlers. Read side has a proper visibility rule (public listing is public-only, a specific match is visible to public, host, or staff, unknown means invisible means 404). Done.
- **Per-player game scoreboards.** `mp_match_game_scores`, a child table filled off the MATCH_COMPLETE path (not the hot path) with computed accuracy and 1-based placement. A game is confirmed to belong to its match before any score is disclosed, so there's no wrong-match pairing leak. Done.
- **Spectator-session history.** `spectator_sessions`, a durable record of who watched whom and for how long, since `Player.spectators` is live-only. Public data (spectating is visible to both sides in real time), so no visibility gate. Done.
- **Tournaments read surface.** v2 read endpoints over the existing `tourney_pools` tables. Authoring stays in-game and staff-gated. Done.
- **Discord #1 feed.** Posts an embed to a webhook whenever someone takes #1 on a ranked map. Mirrors the in-game announce, off the hot path, gated on config (empty webhook means disabled), and a slow or failing webhook can never touch a committed score. Done.
- **Discord OAuth account linking.** Ties an osu! account to a Discord account the player proved they own, one link each way, so a bot can map either direction. In progress right now.

---

## New and upcoming features, at a glance

If you just want the checklist of what changed versus stock bancho.py and what's coming:

**Landed**
- [x] Docker restart policy plus app healthcheck plus Redis AOF persistence
- [x] `pydantic-settings` config that validates and reports everything wrong at once
- [x] `httpx` and Redis connection hardening (timeouts, limits, retries, pools)
- [x] Trusted-proxy `get_ip` with a real trust chain, fail-closed on the security path
- [x] `tenacity` startup retry, graceful shutdown drain, real `MigrationError`
- [x] Leaderboard rebuild from MySQL (atomic swap) plus a CLI tool and startup trigger
- [x] Two-phase replay durability plus a reconciliation tool
- [x] Background loop supervision with per-iteration liveness metrics
- [x] DB-authoritative API keys (no restart needed for a new key)
- [x] Prometheus, Sentry, JSON logs, and `/health` `/ready` `/metrics` endpoints
- [x] Backups, binlog PITR, a restore drill, and six incident runbooks
- [x] `.osr` replay parser (full files and bare frame blocks)
- [x] Tap-aligned feature extraction plus per-frame mechanical signatures
- [x] Movement and timing detectors (hold uniformity, aim fit, robotic timing, timing/aim independence)
- [x] Login-hash spoofer rainbow table
- [x] Redis analysis queue plus a standalone worker process plus keyset backfill
- [x] Durable per-score analysis stats table
- [x] Sticky staff review queue (dismissals can't be silently re-opened)
- [x] Staff-gated v2 review API (list, filter, inspect, resolve)
- [x] `audit_law` terminal verdicts written to the moderation log trail
- [x] `stat_snapshots` daily history with MySQL-authoritative global and country rank
- [x] Daily snapshot service and supervised loop
- [x] Persisted `pp_aim` / `pp_speed` / `pp_flashlight` skill components
- [x] Activity feed (service, v2 read surface, producers)
- [x] Follow graph (followers, counts, viewer relation)
- [x] Durable multiplayer match and game history
- [x] Per-player multiplayer game scoreboards
- [x] Spectator-session history
- [x] Tournaments v2 read surface
- [x] Discord #1 feed

**In progress**
- [ ] Discord OAuth account linking

**Next**
- [ ] Beatmap submission (upload, `.osz` parsing, storage)
- [ ] Re-derive real detector thresholds from server-collected verified replays (blocked: needs live data)
- [ ] `audit_law` follow-through, a richer policy layer on top of actioned flags
- [ ] Validate spoofer preimage encoding against known-spoofed accounts (shelved, edges toward evasion, low priority)
- [ ] Backfill pp skill components for historical scores (columns are nullable on purpose so this is optional)

**Later**
- [ ] Divisions and seasonal ranking on top of `stat_snapshots`
- [ ] Multi-core story. The server is stateful (in-memory player, channel, and match singletons plus per-player packet queues), so you can't just add uvicorn workers. The real CPU cost is synchronous pp calc on the event loop. Offloading that to a thread or process pool is the actual win. Analysis done, not built.
- [ ] A frontend, eventually. Not in this repo.

---

## Security and reinforcement, in one place

Pulling the defensive stuff together since it's spread across the tracks:

- **Trusted-proxy IP resolution.** No more trusting client-controlled `X-Forwarded-For`. Fail-closed on the security path.
- **Fail-closed auth on Redis loss.** Auth fails *closed* if Redis is down. It doesn't silently drop the rate-limit either.
- **DB-authoritative API keys.** A key issued after startup is recognised without a restart (used to be loaded into RAM once at boot).
- **Config validation.** One clear error listing everything wrong, instead of a bare `KeyError` at import.
- **Bounded everything.** HTTP timeouts, limits, retries, Redis socket timeouts, startup retry with backoff, shutdown drain. Nothing hangs forever.
- **Two-phase replay durability.** No score row ever exists without a path to its replay, and a reconciliation tool flags any divergence.
- **Anticheat that can't overreach.** Detection only, flag-not-ban, dismissals are sticky, every verdict is audit-logged, and it runs in its own process so it can't stall submissions.
- **Backups plus PITR plus tested restores.** Binlog archiving, a restore drill that verifies recovery-critical indexes and measures RTO, and runbooks for the six ways things actually go wrong.

Everything above ships with tests. The suite is unit tests with hand-written fakes and `SimpleNamespace` (no `unittest.mock`), mypy runs in `strict` mode, and each track landed green before the next started.

---

## Running it

Same shape as bancho.py. If you've run that, you're home.

```bash
cp .env.example .env      # fill this in; bad config now tells you exactly what's wrong
make build
make run                  # or run-bg for detached
```

Handy targets:

```bash
make utest                # unit tests, no docker
make type-check           # mypy strict
make lint                 # pre-commit
make test                 # integration, in compose
make backup               # logical dump + binlog archive
make restore-drill        # restore into a scratch container, verify, print RTO
```

The anticheat worker is its own process (`bancho-worker` in compose, or `python -m app.anticheat_worker` standalone). It shares the app image and env. It just runs the queue consumer plus backfill loop instead of the web server.

For the original bancho.py setup guide, the [upstream wiki](https://github.com/osuAkatsuki/bancho.py/wiki) still applies to all the base config. Prism didn't change how you point it at MySQL or Redis or set your domain.

---

## Frontend

There isn't one, and that's on purpose for now. Prism is the backend and that's where all the work is going. What matters is that the API and DB contract from bancho.py are preserved and extended, not broken, so whenever a frontend does get built (or an existing bancho.py-compatible one gets pointed at it), it'll work. That's a later problem. Don't expect a website in this repo.

---

## Credit

Built on top of [bancho.py](https://github.com/osuAkatsuki/bancho.py) by the Akatsuki team. Genuinely good code, and none of this exists without it. Prism is a hard fork with its own direction, but the foundation is theirs.

## License

Prism inherits bancho.py's [MIT License](https://opensource.org/license/mit/). See [LICENSE](LICENSE).

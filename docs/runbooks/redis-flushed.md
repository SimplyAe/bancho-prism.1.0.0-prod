# Runbook: Redis flushed / leaderboards empty

The single most common recoverable incident, and the one the whole rebuild path
in P5 exists for. Redis is a **cache** of ranks; mysql is the source of truth.

## Detection signal

- Players report their rank shows as `#0` / unranked, in-game or on the site.
- `redis-cli ZCARD bancho:leaderboard:0` returns `0` (vn!std global board).
- Confirm mysql still has the underlying data:
  ```sh
  redis-cli ZCARD bancho:leaderboard:0        # 0  -> ranks gone from redis
  # in mysql:
  SELECT COUNT(*) FROM stats WHERE pp > 0;    # >0 -> source of truth intact
  ```
  If the second number is `0` too, this is not your runbook — the data loss is
  in mysql. Go to [mysql-lost](mysql-lost.md).

Common causes: a `FLUSHALL`/`FLUSHDB`, an eviction under `maxmemory`, or a redis
restart that came up without its AOF (the compose file enables
`appendonly yes --appendfsync everysec`, so a *configured* instance survives a
restart — suspect this if someone ran redis outside compose).

## Mitigation

None needed for correctness — logins and score submission keep working, and
each player who submits a score re-adds *themselves* to the board. The problem
is the long tail of players who won't log in soon. Don't wait for them.

If ranks are actively misleading users (e.g. a public ladder), you can put up a
maintenance notice, but the rebuild below is fast enough that it's usually not
worth it.

## Recovery

Rebuild the sorted sets from mysql. Safe on a live server — it stages into
temp keys and swaps atomically, so readers see the old board or the new one,
never a half-built one:

```sh
# all modes:
python -m tools.rebuild_leaderboards

# or a single mode if only one board was affected (0 = vn!std):
python -m tools.rebuild_leaderboards -m 0
```

Note: the server also triggers this **automatically at startup** when the global
board is empty but mysql has ranked players, so a plain restart would also fix
it — running the tool is just the faster, no-downtime path.

## Verification

The rebuild is correct when redis matches a fresh mysql ordering exactly:

```sh
# count matches the number of ranked players in mysql:
redis-cli ZCARD bancho:leaderboard:0
#   vs
SELECT COUNT(*) FROM stats s JOIN users u ON u.id = s.id
WHERE s.mode = 0 AND s.pp > 0 AND (u.priv & 1);   -- 1 = UNRESTRICTED

# top of the board matches ORDER BY pp DESC:
redis-cli ZREVRANGE bancho:leaderboard:0 0 4 WITHSCORES
```

Confirm:

- counts are equal,
- **restricted players are absent** from the board (they must not appear),
- country boards came back too: `redis-cli KEYS 'bancho:leaderboard:0:*'`.

Then clear the incident: `bancho_online_players` unaffected, and a spot-check
from a normal account shows the correct rank.

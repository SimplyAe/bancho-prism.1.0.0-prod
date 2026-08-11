#!/usr/bin/env bash
# NOTE: `-x` is deliberately omitted; it echoes every expanded command
# (including credentials from the environment) into container logs.
set -euo pipefail

# Wait indefinitely rather than failing after 60s.
#
# With `set -e`, a wait-for-it timeout exits this script. Combined with
# docker's default restart policy of `no`, a host reboot where MySQL took
# longer than the timeout to accept connections would leave the server
# offline until someone noticed. The compose restart policy is the real
# backstop, so blocking here is both safe and more predictable.
scripts/wait-for-it.sh --timeout=0 "$DB_HOST:$DB_PORT"
scripts/wait-for-it.sh --timeout=0 "$REDIS_HOST:$REDIS_PORT"

exec python main.py

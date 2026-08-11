#!/usr/bin/env python3
"""Container liveness probe.

Used by docker-compose's healthcheck for the `bancho` service. Kept
dependency-free (stdlib only) because the runtime image ships neither
curl nor wget -- only python, the mysql client, and redis-tools.

Exits 0 when the app's liveness endpoint responds 200, 1 otherwise.
"""
from __future__ import annotations

import os
import sys
import urllib.request

TIMEOUT_SECONDS = 5


def main() -> int:
    port = os.environ.get("APP_PORT", "10000")
    url = f"http://127.0.0.1:{port}/health"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        # any failure (connection refused, timeout, non-2xx) is unhealthy.
        return 1


if __name__ == "__main__":
    sys.exit(main())

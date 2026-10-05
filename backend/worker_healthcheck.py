#!/usr/bin/env python
"""Container healthcheck for the Kafka worker container (issue #156).

The worker does not listen on any port, so the image's HTTP /health probe can
never pass.  This script checks the worker's real liveness signal instead: a
heartbeat file that the consume loop refreshes on every Kafka poll.  Exit 0
while the heartbeat is fresh; exit 1 when it is missing or stale — i.e. the
consumer loop stopped making progress (frozen, dead, or stuck).

Run inside the container: ``python worker_healthcheck.py``.
"""

import sys

from app.worker.heartbeat import heartbeat_age, heartbeat_max_age, heartbeat_path


def check(path: str | None = None, max_age: float | None = None, now: float | None = None):
    """Return ``(exit_code, message)`` for the current heartbeat state."""
    age = heartbeat_age(path, now=now)
    if age is None:
        return 1, (
            f"UNHEALTHY: no heartbeat file at {path or heartbeat_path()} "
            "(consumer never started?)"
        )
    limit = heartbeat_max_age() if max_age is None else max_age
    if age > limit:
        return 1, (
            f"UNHEALTHY: heartbeat stale: age={age:.1f}s > max={limit:.0f}s "
            "(consumer loop stopped progressing)"
        )
    return 0, f"healthy: heartbeat age={age:.1f}s <= max={limit:.0f}s"


def main() -> int:
    code, message = check()
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())

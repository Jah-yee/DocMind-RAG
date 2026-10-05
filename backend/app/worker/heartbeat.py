"""Worker liveness heartbeat for the Kafka consumer container (issue #156).

The worker container reuses the backend image, whose ``HEALTHCHECK`` probes
``http://localhost:8000/health`` — a port the worker never listens on — so a
perfectly healthy worker was permanently reported as ``unhealthy``.

Instead of an HTTP socket, the worker exposes a *liveness* signal on disk: the
consume loop refreshes a heartbeat file after every Kafka poll (and at
document-pipeline stage boundaries), while ``worker_healthcheck.py`` reports
unhealthy when that file goes stale.  A frozen process (SIGSTOP), a dead event
loop, or a consumer that stops making progress therefore turns the container
unhealthy — unlike the old probe, which could never be green, this one cannot
stay green when the loop stalls.
"""

import os
import time
from pathlib import Path

DEFAULT_HEARTBEAT_FILE = "/tmp/docmind_worker_heartbeat"
DEFAULT_MAX_AGE_SECONDS = 90.0


def heartbeat_path() -> str:
    """Heartbeat file path (``WORKER_HEARTBEAT_FILE``, default /tmp/...)."""
    return os.environ.get("WORKER_HEARTBEAT_FILE") or DEFAULT_HEARTBEAT_FILE


def heartbeat_max_age() -> float:
    """Tolerated heartbeat age in seconds (``WORKER_HEARTBEAT_MAX_AGE_SECONDS``)."""
    raw = os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_SECONDS


def touch_heartbeat(path: str | None = None) -> None:
    """Refresh the heartbeat (mtime of the heartbeat file).

    Called from the consume loop, so it must never raise into it: a broken
    heartbeat write is itself a liveness failure and is surfaced by the
    healthcheck as a stale/missing file, not by crashing the consumer.
    """
    try:
        target = Path(path or heartbeat_path())
        target.parent.mkdir(parents=True, exist_ok=True)
        # Path.touch(exist_ok=True) 也会对已存在的文件执行 os.utime()，
        # 因此 mtime（存活性时钟）每次都会被刷新。
        target.touch(exist_ok=True)
    except OSError:
        pass


def heartbeat_age(path: str | None = None, now: float | None = None) -> float | None:
    """Seconds since the last heartbeat; ``None`` when no heartbeat exists yet."""
    try:
        mtime = Path(path or heartbeat_path()).stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime

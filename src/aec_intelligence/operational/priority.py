"""Interactive priority: background writers yield to queries.

Re-embedding and the ingest workers' vector inserts compete with interactive search for the same
disk (HNSW pages, WAL). On a slow disk a re-embed made ``/v1/search`` 10x slower. So:

* the API stamps ``aec.interactive_activity`` (one UNLOGGED row per source, throttled, written from a
  background thread so a request never waits for it) when a query starts and when it ends;
* writers call :meth:`InteractiveGate.wait_turn` between chunks. It returns at once unless the API
  answered something in the last ``AEC_INTERACTIVE_YIELD_SECONDS`` (default 15; 0 disables) or a
  query of the API (``application_name = 'aec-api'``) is running right now. Then it sleeps in
  ``poll`` steps until the API has been idle that long, but never longer than
  ``AEC_INTERACTIVE_MAX_WAIT_SECONDS`` (default 120) per call, so a busy API cannot starve ingest;
* re-embed can additionally be limited to a daily window (``AEC_REEMBED_HOURS``, e.g. ``22-7``):
  outside it the run stops cleanly after the current chunk (everything written stays committed) and
  the next scheduled run resumes.

A missing table (database not migrated yet) or any error in the check counts as "not busy": priority
is an optimisation and must never stop a writer.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

API_APPLICATION_NAME = "aec-api"
DEFAULT_WINDOW_SECONDS = 15.0
DEFAULT_MAX_WAIT_SECONDS = 120.0
TOUCH_MIN_INTERVAL = 1.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from None


def parse_hours(spec: str | None) -> tuple[int, int] | None:
    """``"22-7"`` -> (22, 7): allowed from 22:00 until 07:00 (wraps midnight). Empty -> None (always)."""
    spec = (spec or "").strip()
    if not spec:
        return None
    try:
        start, end = (int(x) for x in spec.split("-", 1))
    except ValueError:
        raise ValueError(f"hours window must look like '22-7', got {spec!r}") from None
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise ValueError(f"hours window must be two different hours 0-24, got {spec!r}")
    return start, end % 24


def in_window(window: tuple[int, int] | None, now: _dt.datetime | None = None) -> bool:
    if window is None:
        return True
    hour = (now or _dt.datetime.now()).hour
    start, end = window
    return start <= hour < end if start < end else (hour >= start or hour < end)


class InteractiveGate:
    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS, *, poll: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.window = float(window_seconds)
        self.max_wait = float(max_wait_seconds)
        self.poll = max(0.05, float(poll))
        self.sleep = sleep
        self.clock = clock
        self.waited_total = 0.0
        self.yields = 0

    @classmethod
    def from_env(cls, *, max_wait_cap: float | None = None, **kw) -> "InteractiveGate":
        """``max_wait_cap`` bounds the wait further (ingest jobs hold a transaction while they wait)."""
        max_wait = _env_float("AEC_INTERACTIVE_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS)
        if max_wait_cap is not None:
            max_wait = min(max_wait, max_wait_cap)
        return cls(_env_float("AEC_INTERACTIVE_YIELD_SECONDS", DEFAULT_WINDOW_SECONDS), max_wait, **kw)

    @property
    def enabled(self) -> bool:
        return self.window > 0 and self.max_wait > 0

    def busy(self, conn) -> bool:
        """True while the API answered within the window or has a query running now."""
        if not self.enabled:
            return False
        try:
            with conn.transaction():  # a savepoint inside a job's transaction, a short txn otherwise
                row = conn.execute(
                    """SELECT EXISTS (SELECT 1 FROM aec.interactive_activity
                                      WHERE last_at > now() - make_interval(secs => %s)) AS recent,
                              EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = %s
                                      AND state = 'active' AND pid <> pg_backend_pid()) AS running""",
                    (self.window, API_APPLICATION_NAME)).fetchone()
        except Exception as exc:  # noqa: BLE001 - not migrated yet / no permission: never block a writer
            logger.debug("interactive gate check failed: %s", exc)
            return False
        if isinstance(row, dict):
            return bool(row["recent"] or row["running"])
        return bool(row and (row[0] or row[1]))

    def wait_turn(self, conn) -> float:
        """Sleep while the API is busy (at most ``max_wait`` seconds); returns the seconds waited."""
        if not self.enabled or not self.busy(conn):
            return 0.0
        started = self.clock()
        self.yields += 1
        while self.clock() - started < self.max_wait:
            self.sleep(min(self.poll, self.max_wait - (self.clock() - started)))
            if not self.busy(conn):
                break
        waited = self.clock() - started
        self.waited_total += waited
        return waited


class ActivityStamp:
    """API side: stamp aec.interactive_activity without adding latency to the request."""

    def __init__(self, dsn: str, source: str = "api", *, min_interval: float = TOUCH_MIN_INTERVAL):
        self.dsn = dsn
        self.source = source
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aec-activity")
        self.enabled = _env_float("AEC_INTERACTIVE_YIELD_SECONDS", DEFAULT_WINDOW_SECONDS) > 0

    def touch(self, force: bool = False) -> None:
        """Queue one stamp. Throttled to ``min_interval``; ``force`` (end of a request) only skips
        stamps closer than 0.25 s, so the end of a long query always refreshes the window."""
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last < (0.25 if force else self.min_interval):
                return
            self._last = now
        self._pool.submit(self._write)

    def _write(self) -> None:
        try:
            import psycopg

            with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5,
                                 application_name=API_APPLICATION_NAME + "-stamp") as conn:
                conn.execute("""INSERT INTO aec.interactive_activity(source, last_at) VALUES (%s, now())
                                ON CONFLICT (source) DO UPDATE SET last_at = now()""", (self.source,))
        except Exception as exc:  # noqa: BLE001 - a hint only
            logger.debug("interactive activity stamp failed: %s", exc)

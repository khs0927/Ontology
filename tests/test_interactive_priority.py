"""Interactive priority: background writers yield to API queries (operational/priority.py)."""

import datetime as dt
import os

import pytest

from aec_intelligence.operational import embeddings as emb
from aec_intelligence.operational.priority import ActivityStamp, InteractiveGate, in_window, parse_hours


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class ScriptedGate(InteractiveGate):
    """busy() answers from a script instead of the database."""

    def __init__(self, answers, **kw):
        super().__init__(**kw)
        self.answers = list(answers)

    def busy(self, conn):
        return self.answers.pop(0) if self.answers else False


def test_parse_hours_and_window():
    assert parse_hours("") is None and parse_hours(None) is None
    assert parse_hours("22-7") == (22, 7) and parse_hours("1-24") == (1, 0)
    for bad in ("22", "a-b", "5-5", "25-3"):
        with pytest.raises(ValueError):
            parse_hours(bad)
    at = lambda h: dt.datetime(2026, 10, 4, h, 30)  # noqa: E731
    assert in_window(None, at(12))
    assert in_window((22, 7), at(23)) and in_window((22, 7), at(3)) and not in_window((22, 7), at(7))
    assert in_window((9, 18), at(9)) and not in_window((9, 18), at(18))


def test_gate_idle_returns_at_once():
    clock = Clock()
    gate = ScriptedGate([False], window_seconds=15, max_wait_seconds=60, sleep=clock.sleep, clock=clock)
    assert gate.wait_turn(object()) == 0.0 and clock.sleeps == [] and gate.yields == 0


def test_gate_waits_until_api_idle():
    clock = Clock()
    gate = ScriptedGate([True, True, True, False], window_seconds=15, max_wait_seconds=60, poll=2,
                        sleep=clock.sleep, clock=clock)
    assert gate.wait_turn(object()) == 6.0 and gate.yields == 1 and gate.waited_total == 6.0


def test_gate_wait_is_capped():
    clock = Clock()
    gate = ScriptedGate([True] * 100, window_seconds=15, max_wait_seconds=5, poll=2, sleep=clock.sleep, clock=clock)
    assert gate.wait_turn(object()) == 5.0 and clock.sleeps == [2, 2, 1]


def test_gate_disabled_and_db_errors_never_block(monkeypatch):
    monkeypatch.setenv("AEC_INTERACTIVE_YIELD_SECONDS", "0")
    assert not InteractiveGate.from_env().enabled

    class Broken:
        def transaction(self):
            raise RuntimeError("relation aec.interactive_activity does not exist")

    monkeypatch.delenv("AEC_INTERACTIVE_YIELD_SECONDS")
    gate = InteractiveGate.from_env(max_wait_cap=30)
    assert gate.enabled and gate.max_wait == 30 and gate.busy(Broken()) is False


def test_gate_from_env_rejects_garbage(monkeypatch):
    monkeypatch.setenv("AEC_INTERACTIVE_MAX_WAIT_SECONDS", "2m")
    with pytest.raises(ValueError):
        InteractiveGate.from_env()


def test_activity_stamp_is_throttled(monkeypatch):
    stamp = ActivityStamp("postgresql://x")
    writes = []
    monkeypatch.setattr(stamp, "_pool", type("P", (), {"submit": lambda self, fn: writes.append(fn)})())
    stamp.touch()
    stamp.touch()  # within 1 s: dropped
    stamp.touch(force=True)  # end of a sub-0.25 s request: dropped too
    assert len(writes) == 1
    stamp._last -= 0.3
    stamp.touch(force=True)
    assert len(writes) == 2


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """Enough of a psycopg connection for reindex_embeddings without a database."""

    def __init__(self, n):
        self.rows = [{"id": f"o{i}", "document_id": "d", "revision": 0, "type": "Door", "search_text": f"door {i}"}
                     for i in range(n)]
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return _Rows([] if "text_vectors" in sql else self.rows)

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def executemany(self, sql, rows):
                conn.last_many = rows

        return Cur()

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _run(monkeypatch, conn, **kw):
    monkeypatch.setenv("AEC_EMBEDDING_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(emb.EmbeddingService, "_call_remote_endpoint", lambda self, texts: [[1.0] * 1024 for _ in texts])
    db = type("DB", (), {"connect": lambda self: conn})()
    from aec_intelligence.operational.config import Settings

    return emb.reindex_embeddings(db, Settings.from_env(), batch_size=1, **kw)


def test_reembed_yields_between_chunks(monkeypatch):
    conn = FakeConn(3)  # 3 texts, 8 per chunk -> one chunk; the API is busy for one poll before it
    clock = Clock()
    gate = ScriptedGate([True, False], window_seconds=15, max_wait_seconds=60, poll=3, sleep=clock.sleep, clock=clock)
    res = _run(monkeypatch, conn, gate=gate, sleep=clock.sleep)
    assert res["written"] == 3 and res["complete"] and res["yields"] == 1 and res["yielded_seconds"] == 3.0


def test_reembed_outside_hours_is_a_noop(monkeypatch):
    conn = FakeConn(3)
    res = _run(monkeypatch, conn, hours=(22, 7), clock_now=lambda: dt.datetime(2026, 10, 4, 12, 0))
    assert res["written"] == 0 and not res["complete"] and "outside" in res["stopped"] and conn.commits == 0


def test_reembed_stops_when_window_closes(monkeypatch):
    conn = FakeConn(20)  # 20 texts, 8 per chunk -> 3 chunks
    times = iter([dt.datetime(2026, 10, 4, 6, 59)] * 2 + [dt.datetime(2026, 10, 4, 7, 0)] * 10)
    res = _run(monkeypatch, conn, hours=(22, 7), clock_now=lambda: next(times))
    assert res["written"] == 8 and res["skipped"] == 12 and not res["complete"] and res["error"] is None
    assert "22-07" in res["stopped"] and conn.commits == 1


@pytest.mark.skipif(not os.getenv("AEC_TEST_DATABASE_URL"), reason="needs AEC_TEST_DATABASE_URL (Postgres CI)")
def test_gate_sees_api_stamp_postgres():
    import psycopg

    from aec_intelligence.operational.db import Database

    dsn = os.environ["AEC_TEST_DATABASE_URL"]
    Database(dsn).initialize()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM aec.interactive_activity")
        gate = InteractiveGate(window_seconds=15, max_wait_seconds=1)
        assert gate.busy(conn) is False
        ActivityStamp(dsn)._write()
        assert gate.busy(conn) is True
        conn.execute("UPDATE aec.interactive_activity SET last_at = now() - interval '1 minute'")
        assert gate.busy(conn) is False


def test_reembed_retries_a_chunk_after_a_statement_timeout(monkeypatch):
    from psycopg import errors

    conn = FakeConn(3)
    real_cursor = conn.cursor
    state = {"fail": 1}

    def cursor():
        cur = real_cursor()
        orig = cur.executemany

        def executemany(sql, rows):
            if "text_vectors" in sql and state["fail"]:
                state["fail"] -= 1
                raise errors.QueryCanceled("canceling statement due to statement timeout")
            return orig(sql, rows)

        cur.executemany = executemany
        return cur

    conn.cursor = cursor
    clock = Clock()
    res = _run(monkeypatch, conn, chunk_retries=3, sleep=clock.sleep, gate=ScriptedGate([]))
    assert res["written"] == 3 and res["complete"] and res["retried_chunks"] == 1 and clock.sleeps == [5.0]


def test_reembed_does_not_retry_programming_errors(monkeypatch):
    conn = FakeConn(2)

    def boom():
        raise TypeError("bug")

    conn.cursor = boom
    with pytest.raises(TypeError):
        _run(monkeypatch, conn, chunk_retries=3, gate=ScriptedGate([]))

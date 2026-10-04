"""Operator measurement tools (formerly ad-hoc scripts on the PC): search quality/latency and ingest stats.

* ``search_eval``: run a golden set of search queries in-process (the API's SearchRouter, no HTTP) and
  report hit@1/3/10, MRR and latency per set (e.g. ``ko``/``en``). Cases live OUTSIDE the repository
  (they name private drawings): a JSON list of ``{"q": ..., "expect": <document_id substring>, "set": "ko"}``.
* ``ingest_stats``: queue state, failure rate, throughput over the last hours and ETA from ``aec.jobs``
  (``updated_at`` of finished jobs), and optionally per-job claim -> done durations parsed from the
  workers log (``bulk-run.ps1`` output, lines ``Worker .. claimed job <id>`` / ``Job <id> succeeded``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import time
from pathlib import Path
from typing import Any


def _pct(values: list[float], q: float):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))]


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    cases = []
    for c in data:
        if isinstance(c, (list, tuple)):
            c = {"q": c[0], "expect": c[1], "set": c[2] if len(c) > 2 else "default"}
        cases.append({"q": c["q"], "expect": c["expect"], "set": c.get("set") or "default"})
    return cases


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ms = [r["ms"] for r in rows]
    return {"n": n, "hit@1": sum(r["rank"] == 1 for r in rows), "hit@3": sum(1 <= r["rank"] <= 3 for r in rows),
            "hit@10": sum(1 <= r["rank"] <= 10 for r in rows),
            "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3) if n else None,
            "ms_p50": _pct(ms, 0.5), "ms_p95": _pct(ms, 0.95), "ms_max": max(ms) if ms else None}


def search_eval(router, cases: list[dict[str, Any]], *, top_k: int = 10, warmup: bool = True,
                on_query=None) -> dict[str, Any]:
    """``on_query`` runs before every query (the CLI stamps aec.interactive_activity so background writers
    yield exactly as they do for API queries: the run then measures interactive latency)."""
    if warmup:
        router.search("warmup", top_k=3, expand_graph=False)
    rows = []
    for c in cases:
        if on_query is not None:
            on_query()
        started = time.perf_counter()
        res = router.search(c["q"], top_k=top_k, expand_graph=False)
        ms = int((time.perf_counter() - started) * 1000)
        docs = [h.citation.document_id or "" for h in res.hits]
        rank = next((i for i, d in enumerate(docs, 1) if c["expect"] in d), 0)
        rows.append({"q": c["q"], "set": c["set"], "rank": rank, "ms": ms,
                     "expanded": next((w for w in res.warnings if w.startswith("query expanded")), None)})
    sets = sorted({r["set"] for r in rows})
    return {"sets": {s: summarize([r for r in rows if r["set"] == s]) for s in sets}, "all": summarize(rows),
            "rows": rows}


_CLAIM = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\S* .*?claimed job (\S+?)(?: |$)")
_DONE = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\S* .*?Job (\S+?):? (succeeded|failed)")


def parse_worker_log(lines) -> list[dict[str, Any]]:
    """claim -> done durations per job from worker log lines (the last claim before a result wins)."""
    claims: dict[str, dt.datetime] = {}
    out = []
    for line in lines:
        m = _DONE.search(line)
        if m and m.group(2) in claims:
            t = dt.datetime.fromisoformat(m.group(1).replace(" ", "T"))
            claimed = claims.pop(m.group(2))
            out.append({"job": m.group(2), "claimed": claimed, "done": t,
                        "seconds": (t - claimed).total_seconds(), "result": m.group(3)})
            continue
        m = _CLAIM.search(line)
        if m:
            claims[m.group(2)] = dt.datetime.fromisoformat(m.group(1).replace(" ", "T"))
    return out


def ingest_stats(db, *, hours: float = 6.0, log_path: str | None = None) -> dict[str, Any]:
    with db.connect() as conn:
        states = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, count(*) AS n FROM aec.jobs GROUP BY state").fetchall()}
        recent = {r["state"]: r["n"] for r in conn.execute(
            """SELECT state, count(*) AS n FROM aec.jobs WHERE state IN ('SUCCEEDED','FAILED')
               AND updated_at > now() - make_interval(secs => %s) GROUP BY state""", (hours * 3600,)).fetchall()}
        hourly = [{"hour": r["h"].isoformat(), "succeeded": r["s"], "failed": r["f"]} for r in conn.execute(
            """SELECT date_trunc('hour', updated_at) AS h, count(*) FILTER (WHERE state='SUCCEEDED') AS s,
                      count(*) FILTER (WHERE state='FAILED') AS f
               FROM aec.jobs WHERE state IN ('SUCCEEDED','FAILED') AND updated_at > now() - make_interval(secs => %s)
               GROUP BY 1 ORDER BY 1""", (hours * 3600,)).fetchall()]
        top_errors = [{"error": r["e"], "n": r["n"]} for r in conn.execute(
            """SELECT left(regexp_replace(coalesce(error, ''), '[0-9a-f-]{8,}|[A-Z]:\\\\[^ ]+|/[^ ]+', '…', 'g'), 120) AS e,
                      count(*) AS n FROM aec.jobs WHERE state='FAILED' GROUP BY 1 ORDER BY 2 DESC LIMIT 5""").fetchall()]
    finished = states.get("SUCCEEDED", 0) + states.get("FAILED", 0)
    done_recent = recent.get("SUCCEEDED", 0) + recent.get("FAILED", 0)
    per_hour = done_recent / hours if hours else 0
    queued = states.get("QUEUED", 0) + states.get("RUNNING", 0)
    out: dict[str, Any] = {
        "jobs_by_state": states, "failure_rate": round(states.get("FAILED", 0) / finished, 4) if finished else None,
        "window_hours": hours, "finished_in_window": recent, "jobs_per_hour": round(per_hour, 1),
        "eta_days": round(queued / per_hour / 24, 1) if per_hour else None, "hourly": hourly,
        "top_errors": top_errors,
    }
    if log_path:
        jobs = parse_worker_log(Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines())
        cut = dt.datetime.now() - dt.timedelta(hours=hours)
        secs = [j["seconds"] for j in jobs if j["done"] >= cut]
        out["log"] = {"jobs": len(secs), "median_s": round(statistics.median(secs), 1) if secs else None,
                      "p90_s": _pct(secs, 0.9), "max_s": max(secs) if secs else None}
    return out

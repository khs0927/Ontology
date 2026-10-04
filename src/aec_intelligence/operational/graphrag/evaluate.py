"""Graph RAG evaluation: build a Korean golden set from the ingested data, run it, report metrics.

The golden set holds private project/room/drawing names, so it is written under AEC_DATA_ROOT (the PC),
never into the repository. Only aggregate metrics are meant to be published.

Metrics
- retrieval recall@10: share of answerable questions whose gold ids (KG node / document / object ids)
  appear among the first 10 retrieved context items;
- route accuracy: share of questions routed to the expected retrieval mode (graph templates = Cypher path);
- citation validity: share of answer citations whose document exists, whose object ids exist and belong
  to that document set, and that carry a bbox whenever the cited object has one;
- refusal accuracy on unanswerable questions; answer check (expected number/name appears in the answer);
- latency p50/p95 for retrieval and the full answer.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import time
from pathlib import Path
from typing import Any

from .ask import GraphRAG
from .resolve import KIND_KO

EXCLUDE_PROJECTS = re.compile(r"^(eval20|eval20b|trial|it-\d+|blocks-\d+|ko-\d+.*|작업중)$")


def josa(word: str, pair: str = "은는") -> str:
    """Korean particle after ``word`` (은/는, 이/가, 을/를) by its final consonant."""
    last = word.strip()[-1:] if word.strip() else ""
    has_final = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    if not ("가" <= last <= "힣"):
        has_final = last.isdigit() and last in "013678"
    return word + (pair[0] if has_final else pair[1])


def _p(values, q):
    if not values:
        return None
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))
    return values[k]


def pick_projects(conn, n=3) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.project_key, p.name, (p.props->>'drawings')::int AS drawings,
                  (SELECT count(*) FROM aec.kg_nodes s WHERE s.project_key = p.project_key AND s.type='Space') AS spaces
           FROM aec.kg_nodes p WHERE p.type='Project' ORDER BY 3 DESC""").fetchall()
    good = [dict(r) for r in rows if not EXCLUDE_PROJECTS.match(r["project_key"]) and r["drawings"] >= 5]
    good.sort(key=lambda r: (-(r["spaces"] > 0), -r["drawings"]))
    return good[:n]


def make_eval_set(db, out: str | Path, *, projects: int = 3, total: int = 50, seed: int = 7) -> dict[str, Any]:
    rnd = random.Random(seed)
    qs: list[dict[str, Any]] = []
    with db.connect() as conn:
        chosen = pick_projects(conn, projects)

        def nodes(sql, params):
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

        for proj in chosen:
            key, name = proj["project_key"], proj["name"]
            # storey listings
            for st in nodes("""SELECT st.id, st.name FROM aec.kg_nodes st WHERE st.project_key=%s AND st.type='Storey'
                               AND EXISTS (SELECT 1 FROM aec.kg_edges e WHERE e.src=st.id AND e.predicate='hasSpace')
                               ORDER BY st.name LIMIT 3""", (key,)):
                spaces = [r["id"] for r in nodes("SELECT e.dst AS id FROM aec.kg_edges e WHERE e.src=%s AND "
                                                  "e.predicate='hasSpace'", (st["id"],))]
                qs.append({"type": "storey", "route": "graph:storey", "q": f"{name} {st['name']}에 있는 실 목록을 알려줘",
                           "gold": [st["id"], *spaces[:5]]})
            # room location
            for sp in nodes("""SELECT id, name, props->>'storey' AS storey FROM aec.kg_nodes WHERE project_key=%s
                               AND type='Space' AND props->>'storey' IS NOT NULL
                               ORDER BY (props->>'occurrences')::int DESC, name LIMIT 3""", (key,)):
                qs.append({"type": "room", "route": "graph:room", "q": f"{name} 프로젝트에서 {josa(sp['name'])} 어느 층에 있어?",
                           "gold": [sp["id"]], "expect": [sp["storey"]]})
            # element counts
            for eg in nodes("""SELECT props->>'kind' AS kind, max((props->>'count')::int) AS n,
                                      array_agg(id) AS ids FROM aec.kg_nodes WHERE project_key=%s AND type='ElementGroup'
                               AND props->>'kind' IN ('Door','Window','Column','Stair','Wall') GROUP BY 1
                               ORDER BY 2 DESC LIMIT 2""", (key,)):
                qs.append({"type": "count", "route": "graph:elements",
                           "q": f"{name} 도면 전체에 {josa(KIND_KO[eg['kind']])} 몇 개야?", "gold": list(eg["ids"])[:20],
                           "expect": [str(eg["n"])]})
            # element counts on one storey
            for eg in nodes("""SELECT props->>'kind' AS kind, props->>'storey' AS storey,
                                      max((props->>'count')::int) AS n, array_agg(id) AS ids
                               FROM aec.kg_nodes WHERE project_key=%s AND type='ElementGroup'
                               AND props->>'storey' IS NOT NULL
                               AND props->>'kind' IN ('Door','Window','Column','Stair','Wall') GROUP BY 1, 2
                               ORDER BY 3 DESC LIMIT 2""", (key,)):
                qs.append({"type": "storey_count", "route": "graph:elements",
                           "q": f"{name} {eg['storey']} {KIND_KO[eg['kind']]} 개수는?", "gold": list(eg["ids"])[:20],
                           "expect": [str(eg["n"])]})
            # room area
            for sp in nodes("""SELECT id, name, props->'areas'->>0 AS area FROM aec.kg_nodes WHERE project_key=%s
                               AND type='Space' AND jsonb_array_length(coalesce(props->'areas','[]')) > 0
                               ORDER BY (props->>'occurrences')::int DESC, name LIMIT 2""", (key,)):
                qs.append({"type": "room_area", "route": "graph:room", "q": f"{name}의 {sp['name']} 면적이 얼마야?",
                           "gold": [sp["id"]], "expect": [str(sp["area"])]})
            # sheet numbers
            for d in nodes("""SELECT id, props->>'sheet_number' AS no, document_ids FROM aec.kg_nodes
                              WHERE project_key=%s AND type='Drawing' AND props->>'sheet_number' IS NOT NULL
                              ORDER BY random() LIMIT 2""", (key,)):
                qs.append({"type": "sheet", "route": "keyword:sheet", "q": f"{name}의 {d['no']} 도면은 무슨 도면이야?",
                           "gold": [d["id"], *d["document_ids"]]})
            # drawing title (semantic / lexical over objects + KG)
            for d in nodes("""SELECT id, name, props->>'title' AS title, document_ids FROM aec.kg_nodes
                              WHERE project_key=%s AND type='Drawing' AND coalesce(props->>'title','') ~ '[가-힣]{2,}'
                              ORDER BY random() LIMIT 3""", (key,)):
                words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", d["title"]) if not re.fullmatch(r"\d+", w)]
                if not words:
                    continue
                phrase = " ".join(words[:3])
                qs.append({"type": "drawing", "route": "graph:drawings", "q": f"{name} 프로젝트의 {phrase} 관련 도면을 찾아줘",
                           "gold": [d["id"], *d["document_ids"]]})
            # summary
            c = nodes("SELECT id FROM aec.kg_communities WHERE project_key=%s AND level=0", (key,))
            if c:
                qs.append({"type": "summary", "route": "summary", "q": f"{name} 프로젝트 개요를 요약해줘",
                           "gold": [c[0]["id"], f"kg:p:{key}"]})
        # steel sections (any project with sections)
        for s in nodes("""SELECT s.id, s.name, s.project_key, p.name AS pname FROM aec.kg_nodes s
                          JOIN aec.kg_nodes p ON p.id = 'kg:p:' || s.project_key
                          WHERE s.type='SteelSection' ORDER BY (s.props->>'occurrences')::int DESC LIMIT 4""", ()):
            qs.append({"type": "section", "route": "graph:section",
                       "q": f"{s['pname']} 프로젝트에서 {s['name']} 단면은 어느 도면에 쓰였어?",
                       "gold": [s["id"]]})
        # revisions
        for s in nodes("SELECT id, name FROM aec.kg_nodes WHERE type='DrawingSeries' LIMIT 2", ()):
            qs.append({"type": "revision", "route": "graph:revision", "q": f"{s['name']} 도면의 최신 버전은 뭐야?",
                       "gold": [s["id"]]})
        # unanswerable -> refusal expected
        pname = chosen[0]["name"] if chosen else "이"
        for q in (f"{pname} 프로젝트의 총 공사비는 얼마야?", "오늘 부산 날씨 어때?",
                  f"{pname} 건축주의 전화번호를 알려줘", "이 도면의 설계자가 받은 상 이름은?",
                  "주식 시장 전망을 알려줘"):
            qs.append({"type": "unanswerable", "route": None, "q": q, "gold": [], "expect_refusal": True})
    answerable = [q for q in qs if not q.get("expect_refusal")]
    refusals = [q for q in qs if q.get("expect_refusal")]
    rnd.shuffle(answerable)
    final = answerable[: max(0, total - len(refusals))] + refusals
    for i, q in enumerate(final, 1):
        q["id"] = f"q{i:02d}"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(q, ensure_ascii=False) for q in final) + "\n", encoding="utf-8")
    types: dict[str, int] = {}
    for q in final:
        types[q["type"]] = types.get(q["type"], 0) + 1
    return {"file": str(out), "questions": len(final), "projects": [p["project_key"] for p in chosen],
            "by_type": types}


def _citation_valid(conn, c: dict[str, Any]) -> tuple[bool, bool]:
    """(valid, has_bbox_when_object_has_one)."""
    if not c.get("document_id"):
        return False, False
    docs = set(c.get("document_ids") or []) | {c["document_id"]}
    found = conn.execute("SELECT count(*) AS n FROM aec.documents WHERE id = ANY(%s)", (list(docs),)).fetchone()["n"]
    if found != len(docs):
        return False, False
    objs = c.get("object_ids") or []
    if not objs:
        return True, True
    rows = conn.execute("SELECT id, document_id, payload->'bbox' ? 'min_x' AS has_bbox FROM aec.objects "
                        "WHERE id = ANY(%s)", (objs,)).fetchall()
    if len(rows) != len(objs) or any(r["document_id"] not in docs for r in rows):
        return False, False
    any_bbox = any(r["has_bbox"] for r in rows)
    return True, (not any_bbox) or bool(c.get("bbox"))


def run_eval(db, settings, eval_file, *, out=None, use_llm=True, limit=None, k=10) -> dict[str, Any]:
    from .llm import LocalLLM

    rag = GraphRAG(db, settings, llm=LocalLLM() if use_llm else None)
    questions = [json.loads(line) for line in Path(eval_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit:
        questions = questions[:limit]
    rows = []
    for q in questions:
        started = time.monotonic()
        res = rag.ask(q["q"], top_k=12, generate=use_llm)
        total_ms = round((time.monotonic() - started) * 1000)
        ret = rag.retrieve(q["q"], top_k=k) if q["gold"] else None
        ids_at_k: set[str] = set()
        if ret:
            for item in ret["items"][:k]:
                ids_at_k.update([item.node_id or "", *item.document_ids, *item.object_ids])
        hit = bool(set(q["gold"]) & ids_at_k) if q["gold"] else None
        with db.connect() as conn:
            checks = [_citation_valid(conn, c) for c in res["citations"]]
        expect_ok = None
        if q.get("expect"):
            expect_ok = all(str(e) in res["answer"] for e in q["expect"])
        rows.append({
            "id": q["id"], "type": q["type"], "route_expected": q.get("route"), "route": res["route"],
            "hit@k": hit, "refused": res["refused"], "expect_refusal": bool(q.get("expect_refusal")),
            "citations": len(res["citations"]), "citations_valid": sum(1 for v, _ in checks if v),
            "citations_bbox_ok": sum(1 for _, b in checks if b), "answer_mode": res["answer_mode"],
            "expect_ok": expect_ok, "retrieval_ms": res["retrieval_ms"], "total_ms": total_ms,
            "answer": res["answer"][:600], "question": q["q"],
        })
    answerable = [r for r in rows if not r["expect_refusal"]]
    unans = [r for r in rows if r["expect_refusal"]]
    cites = sum(r["citations"] for r in rows)
    routed = [r for r in answerable if r["route_expected"]]
    summary = {
        "questions": len(rows), "answerable": len(answerable), "unanswerable": len(unans),
        "recall_at_10": round(sum(1 for r in answerable if r["hit@k"]) / max(1, len(answerable)), 3),
        "route_accuracy": round(sum(1 for r in routed if r["route"] == r["route_expected"]) / max(1, len(routed)), 3),
        "citations": cites,
        "citation_validity": round(sum(r["citations_valid"] for r in rows) / max(1, cites), 3) if cites else None,
        "citation_bbox_ok": round(sum(r["citations_bbox_ok"] for r in rows) / max(1, cites), 3) if cites else None,
        "answered_with_citation": round(sum(1 for r in answerable if r["citations"] and not r["refused"])
                                        / max(1, len(answerable)), 3),
        "refusal_accuracy": round(sum(1 for r in unans if r["refused"]) / max(1, len(unans)), 3) if unans else None,
        "false_refusals": sum(1 for r in answerable if r["refused"]),
        "expect_ok": f"{sum(1 for r in rows if r['expect_ok'])}/{sum(1 for r in rows if r['expect_ok'] is not None)}",
        "retrieval_ms_p50": _p([r["retrieval_ms"] for r in rows], 0.5),
        "retrieval_ms_p95": _p([r["retrieval_ms"] for r in rows], 0.95),
        "answer_ms_p50": _p([r["total_ms"] for r in rows], 0.5),
        "answer_ms_p95": _p([r["total_ms"] for r in rows], 0.95),
        "by_type": {t: {"n": len(v), "recall": round(sum(1 for r in v if r["hit@k"]) / max(1, len(v)), 2)}
                    for t in sorted({r["type"] for r in answerable})
                    for v in [[r for r in answerable if r["type"] == t]]},
        "llm": use_llm,
        "mean_total_ms": round(statistics.mean(r["total_ms"] for r in rows)) if rows else None,
    }
    out_path = Path(out) if out else Path(eval_file).with_suffix(".report.json")
    out_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"summary": summary, "rows": rows, "report": str(out_path)}

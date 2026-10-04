"""Community detection over the project knowledge graph and cached Korean summaries by the local LLM.

Level 0 is one community per project (overview). Level 1 groups the project graph into named aspects:
one per discipline (its drawings), one per storey (spaces, drawings, element counts) and one for steel
sections. With ``python-igraph`` installed, level 2 adds Leiden communities over the whole project graph.

Each community stores its fact lines and an ``input_hash`` (model + prompt version + facts). ``summarize``
skips DONE rows whose hash is unchanged and retries FAILED/PENDING ones, so it is cached and resumable.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any

from .resolve import DISCIPLINE_KO, KIND_KO, short_hash, storey_sort_key

log = logging.getLogger(__name__)
PROMPT_VERSION = "kg-summary-ko-1"
MAX_FACTS = 80

SUMMARY_SYSTEM = (
    "너는 건축 도면 데이터베이스의 요약 작성자다. 아래 '사실' 목록에 있는 내용만 사용해 한국어로 요약한다. "
    "사실에 없는 내용(법규 판단, 추측, 일반 상식)은 쓰지 않는다. 숫자와 도면번호, 층, 실 이름은 그대로 옮긴다. "
    "4~7문장, 평문으로만 쓴다."
)


def _fmt_drawing(n: dict[str, Any]) -> str:
    p = n["props"]
    bits = [p.get("sheet_number") or "", p.get("title") or p.get("title_titleblock") or n["name"]]
    tags = [DISCIPLINE_KO.get(p.get("discipline") or "", ""), p.get("phase") or "", p.get("date") or ""]
    tags = [t for t in tags if t]
    return " ".join(b for b in bits if b).strip() + (f" ({', '.join(tags)})" if tags else "")


def _load(conn, project_key: str):
    nodes = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, type, name, props, document_ids, object_ids FROM aec.kg_nodes WHERE project_key=%s",
        (project_key,)).fetchall()}
    edges = [dict(r) for r in conn.execute(
        "SELECT src, predicate, dst FROM aec.kg_edges WHERE project_key=%s", (project_key,)).fetchall()]
    return nodes, edges


def _facts_project(nodes, project) -> list[str]:
    p = project["props"]
    facts = [f"프로젝트: {project['name']}", f"도면 수: {p.get('drawings', 0)}"]
    if p.get("phases"):
        facts.append("단계 폴더: " + ", ".join(p["phases"]))
    disc = p.get("disciplines") or {}
    if disc:
        facts.append("공종별 도면 수: " + ", ".join(f"{DISCIPLINE_KO.get(k, k if k != 'UNKNOWN' else '미분류')} {v}"
                                               for k, v in sorted(disc.items(), key=lambda kv: -kv[1])))
    if p.get("file_types"):
        facts.append("파일 형식: " + ", ".join(f"{k} {v}" for k, v in p["file_types"].items()))
    storeys = sorted((n for n in nodes.values() if n["type"] == "Storey"),
                     key=lambda n: storey_sort_key(n["name"]))
    if storeys:
        facts.append("층: " + ", ".join(n["name"] for n in storeys))
    spaces = Counter(n["name"] for n in nodes.values() if n["type"] == "Space")
    if spaces:
        facts.append("주요 실: " + ", ".join(f"{k}" for k, _ in spaces.most_common(25)))
    kinds: Counter = Counter()
    for n in nodes.values():
        if n["type"] == "ElementGroup":
            kinds[n["props"].get("kind")] += int(n["props"].get("count") or 0)
    if kinds:
        facts.append("도면 요소 수(후보 포함): " + ", ".join(f"{KIND_KO.get(k, k)} {v}" for k, v in kinds.most_common()))
    secs = sorted((n for n in nodes.values() if n["type"] == "SteelSection"),
                  key=lambda n: -int(n["props"].get("occurrences") or 0))
    if secs:
        facts.append("철골 단면: " + ", ".join(f"{n['name']}({n['props'].get('occurrences')})" for n in secs[:15]))
    drawings = [n for n in nodes.values() if n["type"] == "Drawing"]
    for n in sorted(drawings, key=lambda n: (n["props"].get("sheet_number") or "~", n["name"]))[:40]:
        facts.append("도면: " + _fmt_drawing(n))
    return facts[:MAX_FACTS]


def detect(conn, project_key: str, *, leiden: bool = False) -> list[dict[str, Any]]:
    nodes, edges = _load(conn, project_key)
    project = nodes.get(f"kg:p:{project_key}")
    if project is None:
        return []
    out = [{"id": f"kgc:{project_key}:0", "level": 0, "title": f"{project['name']} 프로젝트 개요",
            "node_ids": [project["id"]], "facts": _facts_project(nodes, project)}]

    by_disc: dict[str, list[dict]] = defaultdict(list)
    for n in nodes.values():
        if n["type"] == "Drawing":
            by_disc[n["props"].get("discipline") or "UNKNOWN"].append(n)
    for disc, ds in sorted(by_disc.items()):
        if len(ds) < 2:
            continue
        label = DISCIPLINE_KO.get(disc, "미분류")
        facts = [f"프로젝트: {project['name']}", f"{label} 도면 {len(ds)}건"]
        facts += ["도면: " + _fmt_drawing(n) for n in sorted(ds, key=lambda n: (n['props'].get('sheet_number') or '~',
                                                                              n['name']))[:MAX_FACTS - 2]]
        out.append({"id": f"kgc:{project_key}:1:disc:{disc}", "level": 1, "title": f"{project['name']} {label} 도면",
                    "node_ids": [n["id"] for n in ds], "facts": facts})

    adj: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        adj[e["src"]].append((e["predicate"], e["dst"]))
    for st in sorted((n for n in nodes.values() if n["type"] == "Storey"), key=lambda n: storey_sort_key(n["name"])):
        spaces = [nodes[d] for p, d in adj[st["id"]] if p == "hasSpace" and d in nodes]
        groups = [nodes[d] for p, d in adj[st["id"]] if p == "hasElements" and d in nodes]
        drawings = [nodes[e["src"]] for e in edges if e["dst"] == st["id"] and e["predicate"] == "depictsStorey"
                    and nodes.get(e["src"], {}).get("type") == "Drawing"]
        if not (spaces or groups or drawings):
            continue
        facts = [f"프로젝트: {project['name']}", f"층: {st['name']}"]
        if spaces:
            facts.append("실: " + ", ".join(
                s["name"] + (f"({s['props']['areas'][0]}㎡)" if s["props"].get("areas") else "") for s in spaces[:40]))
        kinds: Counter = Counter()
        for gnode in groups:
            kinds[gnode["props"].get("kind")] += int(gnode["props"].get("count") or 0)
        if kinds:
            facts.append("요소 수(후보 포함): " + ", ".join(f"{KIND_KO.get(k, k)} {v}" for k, v in kinds.most_common()))
        facts += ["도면: " + _fmt_drawing(d) for d in drawings[:30]]
        out.append({"id": f"kgc:{project_key}:1:storey:{st['name']}", "level": 1,
                    "title": f"{project['name']} {st['name']}", "facts": facts[:MAX_FACTS],
                    "node_ids": [st["id"]] + [s["id"] for s in spaces] + [d["id"] for d in drawings]})

    secs = [n for n in nodes.values() if n["type"] == "SteelSection"]
    if secs:
        facts = [f"프로젝트: {project['name']}", f"철골 단면 {len(secs)}종"]
        for s in sorted(secs, key=lambda n: -int(n["props"].get("occurrences") or 0))[:MAX_FACTS - 2]:
            cat = s["props"].get("catalog") or {}
            mk = s["props"].get("member_kinds") or {}
            extra = [f"표기 {s['props'].get('occurrences')}회"]
            if mk:
                extra.append("부재 " + ", ".join(f"{KIND_KO.get(k, k)} {v}" for k, v in mk.items()))
            if cat.get("unit_weight_kg_m"):
                extra.append(f"단위중량 {cat['unit_weight_kg_m']} kg/m")
            facts.append(f"단면 {s['name']}: " + ", ".join(extra))
        out.append({"id": f"kgc:{project_key}:1:steel", "level": 1, "title": f"{project['name']} 철골 단면",
                    "node_ids": [s["id"] for s in secs], "facts": facts})

    if leiden:
        out += _leiden(project_key, project, nodes, edges)
    return out


def _leiden(project_key, project, nodes, edges) -> list[dict[str, Any]]:
    try:
        import igraph  # optional: pip install python-igraph
    except ImportError:
        log.info("python-igraph not installed; skipping Leiden level")
        return []
    ids = [i for i, n in nodes.items() if n["type"] not in {"Project", "Phase"}]
    index = {i: k for k, i in enumerate(ids)}
    pairs = [(index[e["src"]], index[e["dst"]]) for e in edges if e["src"] in index and e["dst"] in index]
    if not pairs:
        return []
    graph = igraph.Graph(n=len(ids), edges=pairs, directed=False)
    parts = graph.community_leiden(objective_function="modularity", resolution=1.0, n_iterations=10)
    out = []
    for k, members in enumerate(sorted(parts, key=len, reverse=True)):
        if len(members) < 4:
            continue
        member_nodes = [nodes[ids[m]] for m in members]
        types = Counter(n["type"] for n in member_nodes)
        facts = [f"프로젝트: {project['name']}", "구성: " + ", ".join(f"{t} {c}" for t, c in types.most_common())]
        for n in member_nodes[:MAX_FACTS - 2]:
            facts.append(f"{n['type']}: " + (_fmt_drawing(n) if n["type"] == "Drawing" else n["name"]))
        out.append({"id": f"kgc:{project_key}:2:{k}", "level": 2, "title": f"{project['name']} 군집 {k + 1}",
                    "node_ids": [n["id"] for n in member_nodes], "facts": facts})
    return out


def refresh(db, project_key: str | None = None, *, leiden: bool = False, model: str = "") -> dict[str, int]:
    """Recompute communities and their input hashes; summaries of unchanged communities are kept."""
    counts = Counter()
    with db.connect(statement_timeout_seconds=300) as conn:
        keys = [project_key] if project_key else [r["project_key"] for r in conn.execute(
            "SELECT project_key FROM aec.kg_build_state ORDER BY project_key").fetchall()]
        for key in keys:
            found = detect(conn, key, leiden=leiden)
            with conn.transaction():
                existing = {r["id"]: r for r in conn.execute(
                    "SELECT id, input_hash, status FROM aec.kg_communities WHERE project_key=%s", (key,)).fetchall()}
                keep = set()
                for c in found:
                    h = short_hash(PROMPT_VERSION, json.dumps(c["facts"], ensure_ascii=False), n=32)
                    keep.add(c["id"])
                    old = existing.get(c["id"])
                    if old and old["input_hash"] == h:
                        counts["unchanged"] += 1
                        continue
                    counts["changed" if old else "new"] += 1
                    conn.execute(
                        """INSERT INTO aec.kg_communities(id, project_key, level, title, node_ids, facts, input_hash,
                                                          status, summary, error, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING',NULL,NULL,now())
                           ON CONFLICT (id) DO UPDATE SET level=EXCLUDED.level, title=EXCLUDED.title,
                             node_ids=EXCLUDED.node_ids, facts=EXCLUDED.facts, input_hash=EXCLUDED.input_hash,
                             status='PENDING', summary=NULL, error=NULL, embedding=NULL, updated_at=now()""",
                        (c["id"], key, c["level"], c["title"], c["node_ids"],
                         json.dumps(c["facts"], ensure_ascii=False), h))
                stale = [i for i in existing if i not in keep]
                if stale:
                    conn.execute("DELETE FROM aec.kg_communities WHERE id = ANY(%s)", (stale,))
                    counts["removed"] += len(stale)
    return dict(counts)


def summarize(db, llm, *, embedder=None, project_key: str | None = None, limit: int | None = None,
              max_level: int = 1) -> dict[str, Any]:
    """Summarise PENDING/FAILED communities with the local LLM (resumable: one commit per community)."""
    done = failed = 0
    seconds = 0.0
    with db.connect(statement_timeout_seconds=120) as conn:
        sql = ("SELECT id, title, facts FROM aec.kg_communities WHERE status <> 'DONE' AND level <= %s"
               + (" AND project_key = %s" if project_key else "") + " ORDER BY level, project_key, id")
        params: list[Any] = [max_level] + ([project_key] if project_key else [])
        rows = conn.execute(sql, params).fetchall()
        for row in rows[:limit] if limit else rows:
            facts = row["facts"] if isinstance(row["facts"], list) else json.loads(row["facts"])
            user = f"제목: {row['title']}\n사실:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n위 사실만으로 요약하라."
            try:
                res = llm.chat(SUMMARY_SYSTEM, user, max_tokens=450)
                text = res["text"].strip()
                if not text:
                    raise ValueError("empty summary")
                vec, vec_model = None, None
                if embedder is not None:
                    from ..embeddings import HASH_MODEL, vector_literal
                    vec_model, vecs = embedder.embed_with_model([f"{row['title']}\n{text}"])
                    if vec_model == HASH_MODEL:
                        vec_model = None
                    else:
                        vec = vector_literal(vecs[0])
                with conn.transaction():
                    conn.execute(
                        """UPDATE aec.kg_communities SET summary=%s, model=%s, status='DONE', error=NULL,
                             embedding=%s::vector, embedding_model=%s, updated_at=now() WHERE id=%s""",
                        (text, res["model"], vec, vec_model, row["id"]))
                done += 1
                seconds += res["seconds"]
            except Exception as exc:  # noqa: BLE001 - recorded per community, the run continues
                with conn.transaction():
                    conn.execute("UPDATE aec.kg_communities SET status='FAILED', error=%s, updated_at=now() "
                                 "WHERE id=%s", (str(exc)[:500], row["id"]))
                failed += 1
    return {"summarized": done, "failed": failed, "llm_seconds": round(seconds, 1)}

"""Knowledge packs: curated drafting knowledge (rules, layers, procedures, lessons) next to the drawing graph.

A pack is ``aec-knowledge-pack/1`` JSON (written e.g. by power-cad-mcp ``scripts/export_knowledge_pack.py``):

    {"schema": "aec-knowledge-pack/1", "pack": "drafting", "fingerprint": "...",
     "nodes": [{"id", "type", "name", "props", "text", "sources"}],
     "edges": [{"src", "predicate", "dst", "evidence"}], "aliases": [[alias_type, alias, node_id]]}

``load_pack`` writes it into ``aec.kg_*`` under the project key ``kb:<pack>`` in one transaction (delete + insert),
so ``kg-build`` never touches it: kg-build only removes keys it recorded in ``aec.kg_build_state`` and packs are not
recorded there. Every node with text also becomes a DONE level-1 community (title = node name, summary = node text)
so the summary/vector legs of Graph RAG find it; the ``knowledge`` route searches the nodes directly.
``to_turtle`` renders the same graph as RDF (OWL classes per node type, object properties per predicate).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "aec-knowledge-pack/1"
KB_PREFIX = "kb:"
MAX_TEXT = 6000


class KnowledgePackError(ValueError):
    pass


def read_pack(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_pack(data)
    return data


def validate_pack(data: dict[str, Any]) -> None:
    if data.get("schema") != SCHEMA:
        raise KnowledgePackError(f"not a {SCHEMA} file (schema={data.get('schema')!r})")
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", str(data.get("pack", ""))):
        raise KnowledgePackError("pack name must be 1-40 chars of a-z 0-9 _ -")
    ids = set()
    for n in data.get("nodes", []):
        if not n.get("id") or not n.get("type") or not n.get("name"):
            raise KnowledgePackError(f"node without id/type/name: {str(n)[:120]}")
        if not str(n["id"]).startswith(KB_PREFIX):
            raise KnowledgePackError(f"pack node ids must start with {KB_PREFIX!r}: {n['id']}")
        if n["id"] in ids:
            raise KnowledgePackError(f"duplicate node id {n['id']}")
        ids.add(n["id"])
    for e in data.get("edges", []):
        if e.get("src") not in ids or e.get("dst") not in ids or not e.get("predicate"):
            raise KnowledgePackError(f"edge with unknown endpoint: {str(e)[:160]}")
    for a in data.get("aliases", []):
        if len(a) != 3 or a[2] not in ids:
            raise KnowledgePackError(f"alias with unknown node: {a}")


def project_key(data: dict[str, Any]) -> str:
    return f"{KB_PREFIX}{data['pack']}"


def search_text(node: dict[str, Any]) -> str:
    bits = [node["type"], node["name"], node.get("text") or ""]
    return re.sub(r"\s+", " ", " ".join(bits)).strip()[:2000]


def _communities(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """One level-1 community per node with text; level 0 = the pack itself (types and counts)."""
    from collections import Counter

    out = []
    types = Counter(n["type"] for n in data["nodes"])
    root = next((n for n in data["nodes"] if n["type"] == "KnowledgePack"), None)
    facts = [f"{t} {c}개" for t, c in types.most_common()]
    title0 = root["name"] if root else f"지식 팩 {data['pack']}"
    summary0 = ((root.get("text") or "") + "\n구성: " + ", ".join(facts)).strip()
    out.append({"id": f"kgc:{key}:0", "level": 0, "title": f"{title0} 개요", "summary": summary0,
                "node_ids": [root["id"]] if root else [], "facts": facts})
    for n in data["nodes"]:
        text = (n.get("text") or "").strip()
        if not text or n["type"] == "KnowledgePack":
            continue
        out.append({"id": f"kgc:{key}:1:{n['id']}", "level": 1, "title": f"[{n['type']}] {n['name']}",
                    "summary": text[:MAX_TEXT], "node_ids": [n["id"]], "facts": []})
    return out


def load_pack(db, data: dict[str, Any], *, settings=None, embed: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Replace the pack's rows in aec.kg_* atomically. Embeddings for the communities when a real model is up."""
    validate_pack(data)
    key = project_key(data)
    nodes, edges, aliases = data["nodes"], data.get("edges", []), data.get("aliases", [])
    comms = _communities(data, key)
    stats = {"project_key": key, "nodes": len(nodes), "edges": len(edges), "aliases": len(aliases),
             "communities": len(comms), "fingerprint": data.get("fingerprint"), "dry_run": dry_run}
    if dry_run:
        return stats
    vectors: dict[str, str] = {}
    model = None
    if embed and settings is not None:
        try:
            from ..embeddings import HASH_MODEL, EmbeddingService, vector_literal
            svc = EmbeddingService(settings)
            for i in range(0, len(comms), 32):
                chunk = comms[i:i + 32]
                model, vecs = svc.embed_with_model([f"{c['title']}\n{c['summary']}"[:2000] for c in chunk])
                if model == HASH_MODEL:  # no real model: summaries stay lexical-only
                    vectors, model = {}, None
                    break
                vectors.update({c["id"]: vector_literal(v) for c, v in zip(chunk, vecs)})
        except Exception as exc:  # noqa: BLE001 - embeddings are an optional acceleration
            stats["embedding_warning"] = str(exc)[:200]
            vectors, model = {}, None
    with db.connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM aec.kg_communities WHERE project_key=%s", (key,))
            conn.execute("DELETE FROM aec.kg_nodes WHERE project_key=%s", (key,))  # edges/aliases cascade
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO aec.kg_nodes(id, project_key, type, name, props, object_ids, document_ids, search_text)
                       VALUES (%s,%s,%s,%s,%s,'{}','{}',%s)""",
                    [(n["id"], key, n["type"], str(n["name"])[:500],
                      json.dumps({**n.get("props", {}), "text": (n.get("text") or "")[:MAX_TEXT],
                                  "sources": n.get("sources", [])}, ensure_ascii=False, default=str),
                      search_text(n)) for n in nodes])
                cur.executemany(
                    """INSERT INTO aec.kg_edges(src, predicate, dst, project_key, evidence) VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    [(e["src"], e["predicate"], e["dst"], key,
                      json.dumps(e.get("evidence") or {}, ensure_ascii=False, default=str)) for e in edges])
                cur.executemany(
                    "INSERT INTO aec.kg_aliases(alias_type, alias, node_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    [(a[0], str(a[1])[:500], a[2]) for a in aliases])
                cur.executemany(
                    f"""INSERT INTO aec.kg_communities(id, project_key, level, title, node_ids, facts, input_hash, summary,
                                                      model, status, embedding, embedding_model)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'knowledge-pack','DONE',%s::vector,%s)""",
                    [(c["id"], key, c["level"], c["title"][:500], c["node_ids"], json.dumps(c["facts"], ensure_ascii=False),
                      hashlib.sha256((c["title"] + c["summary"]).encode()).hexdigest(), c["summary"],
                      vectors.get(c["id"]), model if c["id"] in vectors else None) for c in comms])
        conn.commit()
    stats["embedded"] = len(vectors)
    stats["embedding_model"] = model
    return stats


def unload_pack(db, pack: str) -> dict[str, Any]:
    key = f"{KB_PREFIX}{pack}"
    with db.connect() as conn:
        with conn.transaction():
            c = conn.execute("DELETE FROM aec.kg_communities WHERE project_key=%s", (key,)).rowcount
            n = conn.execute("DELETE FROM aec.kg_nodes WHERE project_key=%s", (key,)).rowcount
        conn.commit()
    return {"project_key": key, "nodes_removed": n, "communities_removed": c}


# ------------------------------------------------------------------------------------------------ RDF / ontology

_NS = "https://aec.local/kb/"


def _iri_local(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\-.가-힣]", "_", s)


def _lit(s: Any) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return f'"{s}"'


def to_turtle(data: dict[str, Any]) -> str:
    """OWL/RDF rendering: node types -> owl:Class, predicates -> owl:ObjectProperty, nodes -> individuals."""
    validate_pack(data)
    lines = [f"@prefix kb: <{_NS}> .", "@prefix kbo: <" + _NS + "ontology#> .",
             "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
             "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
             "@prefix dcterms: <http://purl.org/dc/terms/> .", "",
             f"<{_NS}{_iri_local(data['pack'])}> a owl:Ontology ; rdfs:label {_lit('knowledge pack ' + data['pack'])} ;",
             f"    dcterms:identifier {_lit(data.get('fingerprint', ''))} .", ""]
    for t in sorted({n["type"] for n in data["nodes"]}):
        lines.append(f"kbo:{_iri_local(t)} a owl:Class ; rdfs:label {_lit(t)} .")
    for p in sorted({e["predicate"] for e in data.get("edges", [])}):
        lines.append(f"kbo:{_iri_local(p)} a owl:ObjectProperty ; rdfs:label {_lit(p)} .")
    lines.append("")
    out_edges: dict[str, list[tuple[str, str]]] = {}
    for e in data.get("edges", []):
        out_edges.setdefault(e["src"], []).append((e["predicate"], e["dst"]))
    for n in data["nodes"]:
        subj = f"kb:{_iri_local(n['id'][len(KB_PREFIX):])}"
        parts = [f"a kbo:{_iri_local(n['type'])}", f"rdfs:label {_lit(n['name'])}"]
        if n.get("text"):
            parts.append(f"rdfs:comment {_lit(n['text'][:MAX_TEXT])}")
        for s in n.get("sources", []):
            parts.append(f"dcterms:source {_lit(s)}")
        for pred, dst in out_edges.get(n["id"], []):
            parts.append(f"kbo:{_iri_local(pred)} kb:{_iri_local(dst[len(KB_PREFIX):])}")
        lines.append(subj + " " + " ;\n    ".join(parts) + " .")
    return "\n".join(lines) + "\n"

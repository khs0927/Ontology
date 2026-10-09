#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Load the ArchiOffice pack into the AEC ontology database (aec.* schema).

DRY RUN BY DEFAULT. Nothing is written and no network call is made unless both
--apply and a reachable database are present. This mirrors the repository rule that
a database import is a commit point and must be an explicit decision.

Target schema (see src/aec_intelligence/operational/migrations):
  0001_core.sql            aec.documents, aec.objects, aec.embeddings, aec.index_state
  0002_knowledge_graph.sql aec.kg_nodes, aec.kg_edges, aec.kg_aliases, aec.kg_build_state
  0003_text_vectors.sql    aec.text_vectors(model, content_hash, halfvec(1024))

Plan when --apply:
  1. one aec.documents row per library category  (project_id = ARCHIOFFICE)
  2. aec.objects rows for every vectorised entity (kind = vector kind, label = node name,
     search_text = the embed-ready text, payload = {node_id, pack, source_file})
  3. aec.text_vectors rows produced by embedding the same search_text with local bge-m3
     (content_hash = sha256(text) computed in Python, never recomputed by SQL)
  4. aec.embeddings mapping rows (object_id, model, revision, content_hash)
  5. aec.kg_nodes / aec.kg_edges / aec.kg_aliases / aec.kg_build_state
     which sql/archioffice_kg_load.sql already contains as idempotent statements

Usage:
  python load_archioffice.py                      # dry run, prints the plan
  python load_archioffice.py --apply --dsn postgresql://...
  python load_archioffice.py --apply --sql-only   # write sql/ only, no DB
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
PROJECT_KEY = "ARCHIOFFICE"
MODEL = "bge-m3"
DIM = 1024
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def plan() -> dict:
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    nodes = load_jsonl(PACK / "graph" / "kg_nodes.jsonl")
    vectors = load_jsonl(PACK / "vectors" / "vector_corpus.jsonl")
    categories = sorted({n["props"].get("code") for n in nodes
                         if n["type"] == "LibraryCategory" and n["props"].get("code")})
    return {
        "pack": manifest["pack"],
        "project_key": PROJECT_KEY,
        "documents_to_create": len(categories),
        "categories": categories,
        "objects_to_insert": len(vectors),
        "text_vectors_to_embed": len({v["content_hash"] for v in vectors}),
        "kg_nodes": len(nodes),
        "kg_edges_expected": manifest["counts"]["kg_edges"],
        "kg_aliases": manifest["counts"]["kg_aliases"],
        "embedding_model": MODEL,
        "embedding_dim": DIM,
        "ollama_url": OLLAMA,
        "sql_file": str((PACK / "sql" / "archioffice_kg_load.sql").relative_to(PACK)),
    }


def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{OLLAMA}/api/embeddings",
        data=json.dumps({"model": MODEL, "prompt": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    vec = payload.get("embedding")
    if not isinstance(vec, list) or len(vec) != DIM:
        raise RuntimeError(f"embedding shape {None if vec is None else len(vec)} != {DIM}")
    return vec


LINK_SQL = (
    "UPDATE aec.kg_nodes n SET object_ids = ARRAY[o.id], document_ids = ARRAY[o.document_id] "
    "FROM aec.objects o WHERE o.id = n.id AND n.project_key = 'ARCHIOFFICE'"
)


def validate_vectors(vectors: list[dict]) -> None:
    """Refuse an empty/invalid corpus so stale-object pruning can never wipe the project."""
    if not vectors:
        raise ValueError("vector corpus is empty; refusing to load or prune ARCHIOFFICE objects")
    ids = []
    for v in vectors:
        oid = v.get("node_id")
        if not isinstance(oid, str) or not oid.strip():
            raise ValueError(f"invalid vector row without node_id: {v.get('label')!r}")
        if not v.get("kind") or not v.get("content_hash") or not isinstance(v.get("text"), str):
            raise ValueError(f"invalid vector row {oid!r}: kind/content_hash/text required")
        ids.append(oid)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate node_id in vector corpus")


def prune_stale_objects(cur, vectors: list[dict]) -> None:
    """Delete ARCHIOFFICE objects (and their embedding mappings) absent from the corpus.

    Scoped strictly to project_id = ARCHIOFFICE. Shared aec.text_vectors rows are kept.
    """
    validate_vectors(vectors)
    keep = sorted({v["node_id"] for v in vectors})
    cur.execute(
        "DELETE FROM aec.embeddings WHERE object_id IN ("
        "SELECT id FROM aec.objects WHERE project_id = %s AND NOT (id = ANY(%s)))",
        (PROJECT_KEY, keep))
    cur.execute(
        "DELETE FROM aec.objects WHERE project_id = %s AND NOT (id = ANY(%s))",
        (PROJECT_KEY, keep))


def apply(dsn: str, sql_only: bool) -> int:
    try:
        import psycopg  # type: ignore
    except ImportError:
        print("BLOCKED: psycopg is not installed in this interpreter; cannot reach the database.",
              file=sys.stderr)
        return 3
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    nodes = load_jsonl(PACK / "graph" / "kg_nodes.jsonl")
    vectors = load_jsonl(PACK / "vectors" / "vector_corpus.jsonl")
    sql_file = PACK / "sql" / "archioffice_kg_load.sql"

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aec.documents LIMIT 1")
            cur.execute("SELECT 1 FROM aec.kg_nodes LIMIT 1")
            cur.execute("SELECT 1 FROM aec.text_vectors LIMIT 1")
            print(f"connected; applying {sql_file.name}")
            validate_vectors(vectors)
            cur.execute(sql_file.read_text(encoding="utf-8"))
            print(f"kg load committed: nodes={len(nodes)}")
            if sql_only:
                # link to pack objects that already exist (idempotent with the SQL's own link)
                cur.execute(LINK_SQL)
                conn.commit()
                return 0

            by_cat: dict[str, list[dict]] = {}
            for v in vectors:
                by_cat.setdefault(v["kind"], []).append(v)
            doc_ids: dict[str, str] = {}
            for cat in sorted(by_cat):
                did = f"{PROJECT_KEY}-{cat.upper().replace('_', '-')}-DOC"
                doc_ids[cat] = did
                cur.execute(
                    "INSERT INTO aec.documents(id,project_id,source_key,name) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    (did, PROJECT_KEY, cat, f"ArchiOffice {cat} asset table"),
                )
            seen_hash: set[str] = set()
            for v in vectors:
                oid = v["node_id"]
                did = doc_ids[v["kind"]]
                cur.execute(
                    "INSERT INTO aec.objects(id,project_id,document_id,revision,kind,discipline,"
                    "storey,label,search_text,payload,units) "
                    "VALUES (%s,%s,%s,0,%s,'ARCHIOFFICE','',%s,%s,%s::jsonb,'mm') "
                    "ON CONFLICT (id) DO UPDATE SET search_text=EXCLUDED.search_text, "
                    "label=EXCLUDED.label, payload=EXCLUDED.payload",
                    (oid, PROJECT_KEY, did, v["kind"], v["label"], v["text"],
                     json.dumps({"node_id": oid, "pack": PROJECT_KEY, "kind": v["kind"]},
                                ensure_ascii=False)),
                )
                if v["content_hash"] in seen_hash:
                    cur.execute(
                        "INSERT INTO aec.embeddings(object_id,model,revision,content_hash) "
                        "VALUES (%s,%s,0,%s) ON CONFLICT (object_id,model) DO UPDATE SET "
                        "revision=EXCLUDED.revision, content_hash=EXCLUDED.content_hash",
                        (oid, MODEL, v["content_hash"]))
                    continue
                vec = embed(v["text"])
                cur.execute(
                    "INSERT INTO aec.text_vectors(model,content_hash,embedding) "
                    "VALUES (%s,%s,%s::halfvec) ON CONFLICT (model,content_hash) DO NOTHING",
                    (MODEL, v["content_hash"], "[" + ",".join(f"{x:.6f}" for x in vec) + "]"))
                cur.execute(
                    "INSERT INTO aec.embeddings(object_id,model,revision,content_hash) "
                    "VALUES (%s,%s,0,%s) ON CONFLICT (object_id,model) DO UPDATE SET "
                        "revision=EXCLUDED.revision, content_hash=EXCLUDED.content_hash",
                    (oid, MODEL, v["content_hash"]))
                seen_hash.add(v["content_hash"])
            # objects now exist: (re)link kg nodes (the SQL ran before objects on a clean DB)
            cur.execute(LINK_SQL)
            prune_stale_objects(cur, vectors)
        conn.commit()
    print(f"APPLIED objects={len(vectors)} distinct_text_vectors={len(seen_hash)}")
    print("NOTE: this script does not run kg-summarize; run "
          "scripts/ops/graphrag.ps1 refresh if community summaries are wanted.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument("--sql-only", action="store_true", help="apply kg SQL only, skip vectors")
    ap.add_argument("--dsn", default=os.environ.get("AEC_DSN", ""))
    args = ap.parse_args()

    p = plan()
    print(json.dumps(p, ensure_ascii=False, indent=1))

    if not args.apply:
        print("\nDRY RUN - nothing written, no network call made.")
        print("to apply:  python load_archioffice.py --apply --dsn postgresql://<user>@127.0.0.1:55432/aec")
        return 0
    if not args.dsn:
        print("BLOCKED: --apply needs a DSN (or AEC_DSN). No default is guessed.", file=sys.stderr)
        return 2
    return apply(args.dsn, args.sql_only)


if __name__ == "__main__":
    raise SystemExit(main())

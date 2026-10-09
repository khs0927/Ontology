#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Load the Google Drive CAD pack into the AEC ontology database (aec.* schema).

DRY RUN BY DEFAULT. Nothing is written and no network call is made unless --apply is given.
Mirrors library/archioffice/tools/load_archioffice.py, including the ArchiOffice PR #95 lessons:
  * objects are inserted BEFORE kg nodes are linked to them (the SQL runs first on a clean DB, so the
    object_ids/document_ids relink is repeated after the objects exist)
  * embedding mappings are upserted (revision/content_hash refreshed), shared text_vectors rows are kept
  * stale cleanup is scoped strictly to project_id = GDRIVE_CAD and refuses an empty/invalid corpus

Plan when --apply:
  1. psql-free execution of sql/gdrive_cad_kg_load.sql (nodes, edges, aliases, build state, kg:p:GDRIVE_CAD
     Project node with props.pack = true)               [skip with --skip-kg when resuming]
  2. one aec.documents row per vector kind (project_id = GDRIVE_CAD)
  3. in batches (default 32, one commit per batch => resumable):
       aec.objects upsert (id == kg node id), aec.text_vectors rows embedded by Ollama bge-m3
       (/api/embed batch; text_vectors already present are never re-embedded), aec.embeddings mapping upsert
  4. relink kg_nodes.object_ids/document_ids from aec.objects, prune stale GDRIVE_CAD objects

Usage:
  python load_gdrive_cad.py                       # dry run, prints the plan
  python load_gdrive_cad.py --dry-run --check-ollama
  python load_gdrive_cad.py --apply               # DSN from --dsn / AEC_DSN / AEC_DATABASE_URL / .env
  python load_gdrive_cad.py --apply --skip-kg     # resume embedding after an interruption
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
PROJECT_KEY = "GDRIVE_CAD"
MODEL = "bge-m3"
DIM = 1024
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
SQL_FILE = "gdrive_cad_kg_load.sql"
DEFAULT_BATCH = 32

LINK_SQL = (
    "UPDATE aec.kg_nodes n SET object_ids = ARRAY[o.id], document_ids = ARRAY[o.document_id] "
    f"FROM aec.objects o WHERE o.id = n.id AND n.project_key = '{PROJECT_KEY}'"
)


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def plan() -> dict:
    manifest = json.loads((PACK / "manifest.json").read_text(encoding="utf-8"))
    nodes = load_jsonl(PACK / "graph" / "kg_nodes.jsonl")
    vectors = load_jsonl(PACK / "vectors" / "vector_corpus.jsonl")
    kinds = sorted({v["kind"] for v in vectors})
    return {
        "pack": manifest["pack"],
        "project_key": PROJECT_KEY,
        "documents_to_create": len(kinds),
        "kinds": kinds,
        "objects_to_insert": len(vectors),
        "text_vectors_to_embed_at_most": len({v["content_hash"] for v in vectors}),
        "kg_nodes": len(nodes),
        "kg_edges_expected": manifest["counts"]["kg_edges"],
        "kg_aliases": manifest["counts"]["kg_aliases"],
        "fingerprint": manifest.get("fingerprint"),
        "embedding_model": MODEL,
        "embedding_dim": DIM,
        "ollama_url": OLLAMA,
        "sql_file": f"sql/{SQL_FILE}",
    }


def post_json(url: str, body: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _check_vec(vec) -> list[float]:
    if not isinstance(vec, list) or len(vec) != DIM:
        raise RuntimeError(f"embedding shape {None if vec is None else len(vec)} != {DIM}")
    return vec


def embed_many(texts: list[str]) -> list[list[float]]:
    """Batch embed with /api/embed; fall back to /api/embeddings one by one on older Ollama."""
    try:
        payload = post_json(f"{OLLAMA}/api/embed", {"model": MODEL, "input": texts})
        vecs = payload.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise RuntimeError("batch embed returned a different number of vectors")
        return [_check_vec(v) for v in vecs]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    return [_check_vec(post_json(f"{OLLAMA}/api/embeddings", {"model": MODEL, "prompt": t}).get("embedding"))
            for t in texts]


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def validate_vectors(vectors: list[dict]) -> None:
    """Refuse an empty/invalid corpus so stale-object pruning can never wipe the project."""
    if not vectors:
        raise ValueError(f"vector corpus is empty; refusing to load or prune {PROJECT_KEY} objects")
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
    """Delete GDRIVE_CAD objects (and their embedding mappings) absent from the corpus.

    Scoped strictly to project_id = GDRIVE_CAD. Shared aec.text_vectors rows are kept.
    """
    validate_vectors(vectors)
    keep = sorted({v["node_id"] for v in vectors})
    cur.execute(
        "DELETE FROM aec.embeddings WHERE object_id IN ("
        "SELECT id FROM aec.objects WHERE project_id = %s AND NOT (id = ANY(%s)))",
        (PROJECT_KEY, keep))
    cur.execute("DELETE FROM aec.objects WHERE project_id = %s AND NOT (id = ANY(%s))", (PROJECT_KEY, keep))


def doc_id(kind: str) -> str:
    return f"{PROJECT_KEY}-{kind.upper().replace('_', '-')}-DOC"


def resolve_dsn(arg: str, env_file: str | None) -> str:
    """--dsn, AEC_DSN, AEC_DATABASE_URL, then AEC_DATABASE_URL from a .env file. Never printed."""
    for cand in (arg, os.environ.get("AEC_DSN", ""), os.environ.get("AEC_DATABASE_URL", "")):
        if cand:
            return cand
    files = [Path(env_file)] if env_file else [p / ".env" for p in [PACK.parents[1], Path.cwd()]]
    for f in files:
        if f.is_file():
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*AEC_DATABASE_URL\s*=\s*(.+?)\s*$", line)
                if m:
                    return m.group(1).strip("'\"")
    return ""


def upsert_batch(cur, batch: list[dict], doc_ids: dict[str, str], embedded_this_run: set[str]) -> tuple[int, int]:
    for v in batch:
        oid = v["node_id"]
        cur.execute(
            "INSERT INTO aec.objects(id,project_id,document_id,revision,kind,discipline,"
            "storey,label,search_text,payload,units) "
            "VALUES (%s,%s,%s,0,%s,%s,'',%s,%s,%s::jsonb,'unknown') "
            "ON CONFLICT (id) DO UPDATE SET search_text=EXCLUDED.search_text, "
            "label=EXCLUDED.label, payload=EXCLUDED.payload, discipline=EXCLUDED.discipline",
            (oid, PROJECT_KEY, doc_ids[v["kind"]], v["kind"], v.get("discipline") or "", v["label"], v["text"],
             json.dumps({"node_id": oid, "pack": PROJECT_KEY, "kind": v["kind"],
                         **({"properties": v["properties"]} if v.get("properties") else {})}, ensure_ascii=False)))
    hashes = sorted({v["content_hash"] for v in batch} - embedded_this_run)
    have: set[str] = set()
    if hashes:
        cur.execute("SELECT content_hash FROM aec.text_vectors WHERE model = %s AND content_hash = ANY(%s)",
                    (MODEL, hashes))
        have = {r[0] for r in cur.fetchall()}
    todo = [h for h in hashes if h not in have]
    text_of = {v["content_hash"]: v["text"] for v in batch}
    if todo:
        vecs = embed_many([text_of[h] for h in todo])
        for h, vec in zip(todo, vecs):
            cur.execute("INSERT INTO aec.text_vectors(model,content_hash,embedding) "
                        "VALUES (%s,%s,%s::halfvec) ON CONFLICT (model,content_hash) DO NOTHING",
                        (MODEL, h, vec_literal(vec)))
    embedded_this_run.update(hashes)
    for v in batch:
        cur.execute(
            "INSERT INTO aec.embeddings(object_id,model,revision,content_hash) VALUES (%s,%s,0,%s) "
            "ON CONFLICT (object_id,model) DO UPDATE SET "
            "revision=EXCLUDED.revision, content_hash=EXCLUDED.content_hash",
            (v["node_id"], MODEL, v["content_hash"]))
    return len(todo), len(batch)


def apply(dsn: str, skip_kg: bool, batch_size: int) -> int:
    try:
        import psycopg  # type: ignore
    except ImportError:
        print("BLOCKED: psycopg is not installed in this interpreter; cannot reach the database.", file=sys.stderr)
        return 3
    nodes = load_jsonl(PACK / "graph" / "kg_nodes.jsonl")
    vectors = load_jsonl(PACK / "vectors" / "vector_corpus.jsonl")
    validate_vectors(vectors)
    sql_file = PACK / "sql" / SQL_FILE

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM aec.documents LIMIT 1")
            cur.execute("SELECT 1 FROM aec.kg_nodes LIMIT 1")
            cur.execute("SELECT 1 FROM aec.text_vectors LIMIT 1")
        conn.commit()
        print(f"connected; objects={len(vectors)} kg_nodes={len(nodes)}")

        if not skip_kg:
            with conn.cursor() as cur:
                cur.execute(sql_file.read_text(encoding="utf-8"))
            conn.commit()
            print(f"kg load committed: nodes={len(nodes)}")

        by_kind = sorted({v["kind"] for v in vectors})
        doc_ids = {k: doc_id(k) for k in by_kind}
        with conn.cursor() as cur:
            for kind, did in doc_ids.items():
                cur.execute("INSERT INTO aec.documents(id,project_id,source_key,name) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (id) DO NOTHING",
                            (did, PROJECT_KEY, kind, f"Google Drive CAD {kind} asset table"))
        conn.commit()

        embedded: set[str] = set()
        new_vec = done = 0
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            with conn.cursor() as cur:
                n_new, n_obj = upsert_batch(cur, batch, doc_ids, embedded)
            conn.commit()  # one commit per batch: an interrupted run resumes without re-embedding
            new_vec += n_new
            done += n_obj
            if (i // batch_size) % 25 == 0 or done == len(vectors):
                print(f"  objects {done}/{len(vectors)} new_text_vectors={new_vec}", flush=True)

        with conn.cursor() as cur:
            # objects now exist: (re)link kg nodes (the SQL ran before objects on a clean DB)
            cur.execute(LINK_SQL)
            prune_stale_objects(cur, vectors)
        conn.commit()
    print(f"APPLIED objects={len(vectors)} new_text_vectors={new_vec}")
    print("NOTE: this script does not run kg-summarize; run it afterwards:\n"
          f"  Invoke-AecCli -Arguments @('kg-summarize','--project','{PROJECT_KEY}')")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only (default)")
    ap.add_argument("--skip-kg", action="store_true", help="resume: skip the kg SQL, continue with objects/vectors")
    ap.add_argument("--check-ollama", action="store_true", help="dry run only: probe Ollama for the model")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--dsn", default="")
    ap.add_argument("--env-file", default=None)
    args = ap.parse_args()
    if args.apply and args.dry_run:
        print("--apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    p = plan()
    validate_vectors(load_jsonl(PACK / "vectors" / "vector_corpus.jsonl"))
    print(json.dumps(p, ensure_ascii=False, indent=1))

    if not args.apply:
        if args.check_ollama:
            try:
                vecs = embed_many(["ping"])
                print(f"ollama ok: {MODEL} dim={len(vecs[0])}")
            except Exception as exc:  # noqa: BLE001
                print(f"ollama NOT reachable: {type(exc).__name__}: {exc}")
        print("\nDRY RUN - nothing written" + ("" if args.check_ollama else ", no network call made") + ".")
        print("to apply:  python library/gdrive_cad/tools/load_gdrive_cad.py --apply")
        return 0
    dsn = resolve_dsn(args.dsn, args.env_file)
    if not dsn:
        print("BLOCKED: --apply needs a DSN (--dsn, AEC_DSN, AEC_DATABASE_URL or .env). None is guessed.",
              file=sys.stderr)
        return 2
    return apply(dsn, args.skip_kg, max(1, args.batch))


if __name__ == "__main__":
    raise SystemExit(main())

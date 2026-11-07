#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Structural verification of the ArchiOffice pack. Read-only; exits non-zero on any finding.

Checks that are behavioural rather than decorative:
  * every JSONL line parses
  * kg_edges.src / kg_edges.dst both resolve to a kg_nodes.id
  * kg_aliases.node_id resolves
  * vector_corpus.node_id resolves
  * node ids are unique
  * vector content_hash equals sha256(text)  (the aec.text_vectors key)
  * no U+FFFD replacement characters survived decoding
  * counts agree with manifest.json

Usage: python verify_pack.py [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"FATAL {path.name}:{n}: {exc}") from exc
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE.parent))
    args = ap.parse_args()
    out = Path(args.out)
    findings: list[str] = []

    nodes = load_jsonl(out / "graph" / "kg_nodes.jsonl")
    edges = load_jsonl(out / "graph" / "kg_edges.jsonl")
    aliases = load_jsonl(out / "graph" / "kg_aliases.jsonl")
    vectors = load_jsonl(out / "vectors" / "vector_corpus.jsonl")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    ids = [n["id"] for n in nodes]
    idset = set(ids)
    if len(ids) != len(idset):
        findings.append(f"duplicate node ids: {len(ids) - len(idset)}")

    dangling_src = sum(1 for e in edges if e["src"] not in idset)
    dangling_dst = sum(1 for e in edges if e["dst"] not in idset)
    if dangling_src:
        findings.append(f"edges with unresolved src: {dangling_src}")
    if dangling_dst:
        findings.append(f"edges with unresolved dst: {dangling_dst}")

    bad_alias = sum(1 for a in aliases if a["node_id"] not in idset)
    if bad_alias:
        findings.append(f"aliases with unresolved node_id: {bad_alias}")

    bad_vec = [v for v in vectors if v["node_id"] not in idset]
    if bad_vec:
        findings.append(f"vector rows with unresolved node_id: {len(bad_vec)}")

    hash_bad = 0
    for v in vectors:
        want = hashlib.sha256(v["text"].encode("utf-8")).hexdigest()
        if want != v["content_hash"]:
            hash_bad += 1
    if hash_bad:
        findings.append(f"vector content_hash mismatches: {hash_bad}")

    model_bad = sum(1 for v in vectors if v.get("model") != "bge-m3" or v.get("dim") != 1024)
    if model_bad:
        findings.append(f"vector rows with wrong model/dim: {model_bad}")

    import gzip
    geo_count = 0
    with gzip.open(out / "assets" / "archioffice_pattern_geometry.jsonl.gz", "rt",
                   encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                geo_count += 1
    if geo_count != manifest["counts"].get("pattern_geometry"):
        findings.append(f"pattern geometry rows {geo_count} != manifest "
                        f"{manifest['counts'].get('pattern_geometry')}")

    expected = {
        "kg_nodes": len(nodes), "kg_edges": len(edges), "kg_aliases": len(aliases),
        "vector_rows": len(vectors),
    }
    for k, v in expected.items():
        if manifest["counts"].get(k) != v:
            findings.append(f"manifest {k}={manifest['counts'].get(k)} but measured {v}")

    # decoding sanity: a real Korean glyph must appear; U+FFFD must not
    all_text = "\n".join([n["search_text"] for n in nodes] + [v["text"] for v in vectors])
    if "\ufffd" in all_text:
        findings.append(f"U+FFFD replacement characters present: {all_text.count(chr(0xFFFD))}")
    korean = sum(1 for ch in all_text if "\uac00" <= ch <= "\ud7a3")
    if korean < 1000:
        findings.append(f"suspiciously few Hangul characters: {korean}")

    node_types: dict[str, int] = {}
    for n in nodes:
        node_types[n["type"]] = node_types.get(n["type"], 0) + 1
    predicates: dict[str, int] = {}
    for e in edges:
        predicates[e["predicate"]] = predicates.get(e["predicate"], 0) + 1
    vec_kinds: dict[str, int] = {}
    for v in vectors:
        vec_kinds[v["kind"]] = vec_kinds.get(v["kind"], 0) + 1

    print(json.dumps({
        "nodes": len(nodes), "edges": len(edges), "aliases": len(aliases), "vectors": len(vectors),
        "node_types": dict(sorted(node_types.items(), key=lambda kv: -kv[1])),
        "predicates": dict(sorted(predicates.items(), key=lambda kv: -kv[1])),
        "vector_kinds": dict(sorted(vec_kinds.items(), key=lambda kv: -kv[1])),
        "hangul_chars": korean,
        "findings": findings or ["NONE"],
    }, ensure_ascii=False, indent=1))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

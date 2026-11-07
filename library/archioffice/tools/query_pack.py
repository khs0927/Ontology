#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline query over the ArchiOffice pack: keyword search + 1-hop graph expansion.

This is a DB-free smoke check that the pack is queryable before it is loaded.
It does not call an LLM and does not touch Ollama; it shows the graph neighbourhood
that a Graph RAG answer would cite.

Usage:
  python query_pack.py "엘리베이터 14인승"
  python query_pack.py "벽돌 해치" --expand 2 --top 5
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


TOKEN_RE = re.compile(r"[0-9a-zA-Z]+|[가-힣]+")


def tokens(text: str) -> list[str]:
    out = []
    for m in TOKEN_RE.finditer(text.lower()):
        t = m.group(0)
        out.append(t)
        if len(t) > 2 and re.match(r"^[가-힣]+$", t):
            out.extend(t[i:i + 2] for i in range(len(t) - 1))
    return out


def score(query_tokens: list[str], text: str) -> float:
    low = text.lower()
    hits = sum(low.count(t) for t in query_tokens if t)
    return hits / (1 + (len(low) / 400.0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--expand", type=int, default=1)
    args = ap.parse_args()

    nodes = load_jsonl(PACK / "graph" / "kg_nodes.jsonl")
    edges = load_jsonl(PACK / "graph" / "kg_edges.jsonl")
    by_id = {n["id"]: n for n in nodes}

    qt = tokens(args.query)
    scored = sorted(((score(qt, n["search_text"] + " " + n["name"]), n) for n in nodes),
                    key=lambda kv: -kv[0])
    hits = [(s, n) for s, n in scored if s > 0][: args.top]
    if not hits:
        print("no match")
        return 1

    out_neighbours: dict[str, list[tuple[str, str]]] = {}
    in_neighbours: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        out_neighbours.setdefault(e["src"], []).append((e["predicate"], e["dst"]))
        in_neighbours.setdefault(e["dst"], []).append((e["predicate"], e["src"]))

    print(f"query: {args.query}\n")
    for rank, (s, n) in enumerate(hits, 1):
        print(f"[C{rank}] {n['type']}  {n['name']}   (score {s:.2f})")
        print(f"      {n['search_text'][:200]}")
        if args.expand:
            for pred, dst in out_neighbours.get(n["id"], [])[: args.expand * 3]:
                tgt = by_id.get(dst)
                print(f"      -> {pred}: {tgt['type'] + ' ' + tgt['name'] if tgt else dst}")
            for pred, src in in_neighbours.get(n["id"], [])[: args.expand * 2]:
                tgt = by_id.get(src)
                print(f"      <- {pred}: {tgt['type'] + ' ' + tgt['name'] if tgt else src}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

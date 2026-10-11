#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Structural verification of the Google Drive CAD pack. Read-only; exits non-zero on any finding.

Same checks as library/archioffice/tools/verify_pack.py (JSONL parses, edge/alias/vector ids resolve, unique node
ids, content_hash == sha256(text), no U+FFFD, counts agree with manifest.json) plus pack-specific ones:
  * Project node kg:p:GDRIVE_CAD exists with props.pack == true; build-state fingerprint contains '-pack-'
  * exactly one type='Project' node (SubProject is a separate type)
  * one Drawing node per unique md5; every Drawing hangs off a SubProject and the Project
  * discipline is a known code (LIBRARY only for asset_role == block_library), confidence < 0.7 <=> review == true <=> row in review_queue.jsonl
  * LayerStandard / BlockSpec caps; usedIn edges end on Drawing nodes
  * non-DWG/DXF formats are parse_status == unparsed_format
  * search_text bounded; no e-mail addresses or phone numbers in node/vector text

Usage: python verify_pack.py [--out DIR]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_KEY = "GDRIVE_CAD"
DISCIPLINES = {"ARCH", "STRUCT", "MECH", "PLUMB", "ELEC", "FIRE", "CIVIL", "LAND", "COMM", "INTERIOR", "GENERAL"}
LIBRARY_CODE = "LIBRARY"  # not a discipline: asset_role == block_library only
DOC_EXTS = {"pdf", "xls", "xlsx"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d{2,4}[-.\s]\d{3,4}[-.\s]\d{4}")
MAX_SEARCH = 1500


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
    review = load_jsonl(out / "assets" / "review_queue.jsonl")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    sql = (out / "sql" / "gdrive_cad_kg_load.sql").read_text(encoding="utf-8")

    ids = [n["id"] for n in nodes]
    idset = set(ids)
    byid = {n["id"]: n for n in nodes}
    if len(ids) != len(idset):
        findings.append(f"duplicate node ids: {len(ids) - len(idset)}")
    if any(n.get("project_key") != PROJECT_KEY for n in nodes):
        findings.append("node with wrong project_key")

    for side in ("src", "dst"):
        bad = sum(1 for e in edges if e[side] not in idset)
        if bad:
            findings.append(f"edges with unresolved {side}: {bad}")
    keys = [(e["src"], e["predicate"], e["dst"]) for e in edges]
    if len(set(keys)) != len(keys):
        findings.append(f"duplicate edges (src,predicate,dst): {len(keys) - len(set(keys))}")
    bad_alias = sum(1 for a in aliases if a["node_id"] not in idset)
    if bad_alias:
        findings.append(f"aliases with unresolved node_id: {bad_alias}")
    bad_vec = sum(1 for v in vectors if v["node_id"] not in idset)
    if bad_vec:
        findings.append(f"vector rows with unresolved node_id: {bad_vec}")
    if len({v["node_id"] for v in vectors}) != len(vectors):
        findings.append("duplicate node_id in vector corpus")
    hash_bad = sum(1 for v in vectors if hashlib.sha256(v["text"].encode("utf-8")).hexdigest() != v["content_hash"])
    if hash_bad:
        findings.append(f"vector content_hash mismatches: {hash_bad}")
    if sum(1 for v in vectors if v.get("model") != "bge-m3" or v.get("dim") != 1024):
        findings.append("vector rows with wrong model/dim")
    for v in vectors:
        if v["text"] != byid[v["node_id"]]["search_text"]:
            findings.append(f"vector text differs from node search_text: {v['node_id']}")
            break

    # ---- GraphRAG registration
    proj = byid.get(f"kg:p:{PROJECT_KEY}")
    if proj is None:
        findings.append("Project node kg:p:GDRIVE_CAD missing")
    elif proj["type"] != "Project" or proj["props"].get("pack") is not True:
        findings.append("Project node must be type Project with props.pack == true")
    if sum(1 for n in nodes if n["type"] == "Project") != 1:
        findings.append("expected exactly one type='Project' node")
    if "-pack-" not in manifest.get("fingerprint", "") or f"'{manifest.get('fingerprint')}'" not in sql:
        findings.append("fingerprint must contain '-pack-' and be written by the SQL")
    if not {a["alias_type"] for a in aliases} >= {"project_id", "project_name"}:
        findings.append("project_id/project_name aliases missing")
    if "UPDATE aec.kg_nodes n SET object_ids" not in sql:
        findings.append("SQL lacks the object/document relink statement")

    # ---- drawings
    drawings = [n for n in nodes if n["type"] == "Drawing"]
    md5s = [d["props"].get("md5") for d in drawings if d["props"].get("md5")]
    if len(md5s) != len(set(md5s)):
        findings.append("more than one Drawing node for the same md5 (duplicates must be duplicate_copies)")
    has_sub = {e["dst"] for e in edges if e["predicate"] == "hasDrawing" and byid.get(e["src"], {}).get("type") == "SubProject"}
    has_proj = {e["dst"] for e in edges if e["predicate"] == "hasDrawing" and e["src"] == f"kg:p:{PROJECT_KEY}"}
    for d in drawings:
        if d["id"] not in has_sub or d["id"] not in has_proj:
            findings.append(f"Drawing without SubProject/Project edge: {d['id']}")
            break
    rq = {r["node_id"] for r in review}
    for d in drawings:
        p = d["props"]
        if p["discipline"] not in DISCIPLINES | {LIBRARY_CODE}:
            findings.append(f"unknown discipline {p['discipline']!r} on {d['id']}")
            break
        if (p["discipline"] == LIBRARY_CODE) != (p.get("asset_role") == "block_library")                 or p.get("asset_role") not in ("drawing", "block_library"):
            findings.append(f"asset_role / LIBRARY code mismatch on {d['id']}")
            break
        if (p["discipline_confidence"] < 0.7) != bool(p["review"]) or (d["id"] in rq) != bool(p["review"]):
            findings.append(f"review flag / queue mismatch on {d['id']}")
            break
        if p["ext"] not in ("dwg", "dxf") and p["parse_status"] != "unparsed_format":
            findings.append(f"non-DWG format not marked unparsed_format: {d['id']}")
            break
    if len(rq) != len(review):
        findings.append("review_queue has duplicate rows")

    # ---- documents (PDF / xls / xlsx): metadata nodes, Project hasDocument, linkedTo -> Drawing
    docs = [n for n in nodes if n["type"] == "Document"]
    doc_md5 = [n["props"].get("md5") for n in docs if n["props"].get("md5")]
    if len(doc_md5) != len(set(doc_md5)):
        findings.append("more than one Document node for the same md5 (duplicates must be duplicate_copies)")
    doc_has_proj = {e["dst"] for e in edges if e["predicate"] == "hasDocument" and e["src"] == f"kg:p:{PROJECT_KEY}"}
    for n in docs:
        if n["props"].get("ext") not in DOC_EXTS:
            findings.append(f"Document with unexpected ext {n['props'].get('ext')!r}: {n['id']}")
            break
        if not n["props"].get("drive_path") or not n["props"].get("sub_project"):
            findings.append(f"Document missing drive_path/sub_project: {n['id']}")
            break
        if n["id"] not in doc_has_proj:
            findings.append(f"Document without Project hasDocument edge: {n['id']}")
            break
    n_doc_links = 0
    for e in edges:
        if e["predicate"] == "hasDocument" and (byid[e["src"]]["type"] not in ("Project", "SubProject")
                                                or byid[e["dst"]]["type"] != "Document"):
            findings.append("hasDocument edge with wrong endpoint types")
            break
        if e["predicate"] == "linkedTo":
            n_doc_links += 1
            if byid[e["src"]]["type"] != "Document" or byid[e["dst"]]["type"] != "Drawing":
                findings.append("linkedTo edge with wrong endpoint types")
                break

    # ---- caps, usedIn
    caps = manifest.get("caps", {})
    n_layers = sum(1 for n in nodes if n["type"] == "LayerStandard")
    n_blocks = sum(1 for n in nodes if n["type"] == "BlockSpec")
    if n_layers > caps.get("layers", 4000):
        findings.append(f"LayerStandard cap exceeded: {n_layers}")
    if n_blocks > caps.get("blocks", 3000):
        findings.append(f"BlockSpec cap exceeded: {n_blocks}")
    for e in edges:
        if e["predicate"] == "usedIn" and (byid[e["src"]]["type"] not in ("LayerStandard", "BlockSpec")
                                           or byid[e["dst"]]["type"] != "Drawing"):
            findings.append("usedIn edge with wrong endpoint types")
            break

    # ---- hygiene
    all_text = "\n".join([n["search_text"] for n in nodes])
    if "�" in all_text:
        findings.append(f"U+FFFD replacement characters present: {all_text.count(chr(0xFFFD))}")
    if max(len(n["search_text"]) for n in nodes) > MAX_SEARCH:
        findings.append("search_text longer than the bound")
    if EMAIL_RE.search(all_text) or PHONE_RE.search(all_text):
        findings.append("e-mail address or phone number present in node text")
    korean = sum(1 for ch in all_text if "가" <= ch <= "힣")
    if korean < 1000:
        findings.append(f"suspiciously few Hangul characters: {korean}")

    expected = {"kg_nodes": len(nodes), "kg_edges": len(edges), "kg_aliases": len(aliases),
                "vector_rows": len(vectors), "drawings": len(drawings), "review_queue": len(review),
                "layer_standards": n_layers, "block_specs": n_blocks, "documents": len(docs),
                "document_links": n_doc_links, "document_duplicates": sum(n["props"].get("duplicate_count") or 0
                                                                          for n in docs),
                "sub_projects": sum(1 for n in nodes if n["type"] == "SubProject"),
                "block_library": sum(1 for d in drawings if d["props"].get("asset_role") == "block_library")}
    for k, v in expected.items():
        if manifest["counts"].get(k) != v:
            findings.append(f"manifest {k}={manifest['counts'].get(k)} but measured {v}")

    node_types = collections.Counter(n["type"] for n in nodes)
    predicates = collections.Counter(e["predicate"] for e in edges)
    print(json.dumps({
        "nodes": len(nodes), "edges": len(edges), "aliases": len(aliases), "vectors": len(vectors),
        "review_queue": len(review), "documents": len(docs), "document_links": n_doc_links,
        "node_types": dict(node_types.most_common()),
        "predicates": dict(predicates.most_common()),
        "hangul_chars": korean,
        "findings": findings or ["NONE"],
    }, ensure_ascii=False, indent=1))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

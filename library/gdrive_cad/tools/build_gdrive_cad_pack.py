#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build the Google Drive CAD GraphRAG pack (project key GDRIVE_CAD).

Modeled on library/archioffice/tools/build_archioffice_pack.py: same output layout
(graph/, vectors/, sql/, assets/, manifest.json), same row shapes, same SQL idempotency.

Inputs (default C:\\CODE\\_data\\gdrive_cad, override with --src):
  cad_all.json   every CAD row on the Drive: path, ext, size, md5, project, mod, dup_of
  records.jsonl  deep-parse records (DWG -> DXF): layers, blocks, texts, rooms, sheet, counts.
                 Still growing: the builder is deterministic and idempotent, rerun it any time.
  inventory.json full Drive inventory (CAD + related). PDF / xls / xlsx rows become metadata-only
                 Document nodes (title from the file name, ext, size, sub_project, drive_path);
                 nothing is downloaded or parsed.

Design (C:\\CODE\\_herdr-bus\\out\\gdrive-cad-decisions.md):
  * one Drawing node per unique file (size+md5); later copies become props.duplicate_copies
  * one Document node per unique PDF/xls/xlsx (size+md5); a Document -linkedTo-> Drawing when the
    normalised stem matches the drawing's stem / sheet number / title inside the same sub-project
  * SubProject nodes per sub-project folder (type is SubProject, not Project: GraphRAG keeps exactly one
    type='Project' node per project_key, namely kg:p:GDRIVE_CAD)
  * discipline: sheet-number prefix 0.85 > folder/title keyword 0.75 > layer votes (<=0.8) > GENERAL 0.4;
    confidence < 0.7 goes to assets/review_queue.jsonl
  * sheet number/title: DXF 'sheet' values are temp names (f000...) unless source == title_block,
    otherwise parsed from the Drive file name (aec_intelligence filename_sheet_fields)
  * non-DWG formats (rvt/skp/pln/dwt/dwf): parse_status = unparsed_format
  * LayerStandard / BlockSpec are global nodes (caps 4000 / 3000) with LayerStandard -usedIn-> Drawing
  * room/drawing text goes into search_text, bounded
No file contents are stored beyond names and short text labels; e-mail addresses and phone numbers are scrubbed.

Usage:
  python build_gdrive_cad_pack.py                # skip when inputs are unchanged
  python build_gdrive_cad_pack.py --force
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK = HERE.parent
PROJECT_KEY = "GDRIVE_CAD"
PROJECT_NODE = f"kg:p:{PROJECT_KEY}"
FINGERPRINT = "gdrive-cad-pack-v1"
BUILDER_VERSION = 2
DEFAULT_SRC = Path(r"C:\CODE\_data\gdrive_cad")

MAX_LAYERS = 4000
MAX_BLOCKS = 3000
MAX_SEARCH = 1500       # chars of search_text / embedded text per node
MAX_TEXTS_CHARS = 600   # drawing text labels inside search_text
MAX_ROOMS_CHARS = 240
MAX_LAYER_CHARS = 240
MAX_DUP_COPIES = 25
REVIEW_BELOW = 0.7

DISCIPLINE_KO = {"ARCH": "건축", "STRUCT": "구조", "MECH": "기계", "PLUMB": "위생", "ELEC": "전기",
                 "FIRE": "소방", "CIVIL": "토목", "LAND": "조경", "COMM": "통신", "INTERIOR": "인테리어",
                 "GENERAL": "일반"}
DISCIPLINES = set(DISCIPLINE_KO)

# Sheet-number prefix -> discipline (decision: A/S/M/E/P/F/C/L/T/I). One-letter prefixes map directly; two-letter
# prefixes only through this table so that "SH602" or "RG1" style tokens are not read as structural sheets.
PREFIX1 = {"A": "ARCH", "S": "STRUCT", "M": "MECH", "E": "ELEC", "P": "PLUMB", "F": "FIRE", "C": "CIVIL",
           "L": "LAND", "T": "COMM", "I": "INTERIOR"}
PREFIX2 = {"AR": "ARCH", "AI": "ARCH", "ST": "STRUCT", "SD": "STRUCT", "MA": "MECH", "MC": "MECH", "ME": "MECH",
           "EL": "ELEC", "EE": "ELEC", "PL": "PLUMB", "PP": "PLUMB", "FP": "FIRE", "FF": "FIRE", "CV": "CIVIL",
           "LA": "LAND", "LS": "LAND", "TC": "COMM", "IN": "INTERIOR", "ID": "INTERIOR"}

# Ordered: the first discipline with a matching keyword wins; ARCH is last because "건축" is generic.
KEYWORDS = [
    ("FIRE", ("소방", "스프링클러", "소화", "방재", "피난")),
    ("ELEC", ("전기", "조명", "전력", "배전", "콘센트", "접지", "피뢰", "분전반")),
    ("COMM", ("통신", "방송", "cctv", "인터폰", "정보통신")),
    ("MECH", ("기계", "공조", "덕트", "환기", "냉난방", "냉동", "보일러", "공기조화", "hvac")),
    ("PLUMB", ("위생", "급수", "급탕", "오수", "배관", "배수")),
    ("CIVIL", ("토목", "옹벽", "포장", "흙막이", "측량", "하수", "부지정지")),
    ("LAND", ("조경", "식재", "수목", "녹지")),
    ("STRUCT", ("구조", "철골", "골조", "배근", "철근", "기초", "거더", "트러스")),
    ("INTERIOR", ("인테리어", "실내건축", "가구")),
    ("ARCH", ("건축", "평면도", "입면도", "단면도", "창호", "마감", "계단", "상세도")),
]

# layer_norm long discipline names (records.jsonl) -> pack codes
LAYER_DISC = {"architecture": "ARCH", "structural": "STRUCT", "mechanical": "MECH", "electrical": "ELEC",
              "plumbing": "PLUMB", "civil": "CIVIL", "fire": "FIRE", "landscape": "LAND",
              "telecom": "COMM", "communication": "COMM", "interior": "INTERIOR"}

CAD_DWG = {"dwg", "dxf"}
DOC_EXTS = {"pdf", "xls", "xlsx"}
# Repo-copy folders dropped from cad_all.json (tools/select.py); documents skip them too so sub-projects line up.
DOC_EXCLUDE_PREFIX = ("AEC-INTELLIGENCE/", "revit-mcp-guideline/")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d{2,4}[-.\s]\d{3,4}[-.\s]\d{4}")
_ORDER_PREFIX_RE = re.compile(r"^(?:\d{1,3}[_.]\s*|\d{1,3}\s+-\s+(?=[A-Za-z]{1,3}-?\d))")
SHEET_NUMBER_RE = re.compile(r"([A-Z]{1,3}-?\d{2,4}(?:-\d{1,3})?)(?![A-Z0-9])")


# --------------------------------------------------------------------------- sheet / discipline rules
def _local_filename_sheet_fields(name):
    """Same contract as aec_intelligence.operational.parsers.filename_sheet_fields (fallback copy)."""
    stem = re.sub(r"\.(dwg|dxf|pdf)$", "", Path(str(name or "")).name, flags=re.IGNORECASE).strip()
    stem = _ORDER_PREFIX_RE.sub("", stem, count=1).strip()
    match = SHEET_NUMBER_RE.match(stem.upper())
    if not match:
        return None, (stem.strip(" -_[]") or None)
    title = stem[match.end():]
    title = re.sub(r"^\s*~\s*[A-Za-z]{0,3}-?\d{1,4}", "", title)
    title = re.sub(r"[_\s\-\[\]]+", " ", title).strip()
    return match.group(1), (title or None)


def _resolve_filename_sheet_fields():
    src = PACK.parents[1] / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from aec_intelligence.operational.parsers import filename_sheet_fields  # type: ignore
        return filename_sheet_fields, "aec_intelligence.operational.parsers"
    except Exception:  # noqa: BLE001 - heavy optional deps (ezdxf, ...) may be missing
        return _local_filename_sheet_fields, "local-copy"


filename_sheet_fields, FILENAME_SHEET_SOURCE = _resolve_filename_sheet_fields()


def file_stem(path: str, ext: str) -> str:
    name = path.rsplit("/", 1)[-1]
    suffix = "." + ext if ext else ""
    if suffix and name.lower().endswith(suffix.lower()):
        name = name[: -len(suffix)]
    return name


def sheet_fields(path: str, ext: str, rec: dict | None):
    """(number, title, source). Only a title block is trusted in a parsed record (DXF stems are temp names)."""
    sh = (rec or {}).get("sheet") or {}
    if rec and rec.get("status") == "ok" and sh.get("source") == "title_block" and (sh.get("number") or sh.get("title")):
        return sh.get("number"), sh.get("title"), "title_block"
    number, title = filename_sheet_fields(file_stem(path, ext) + ".dwg")
    return number, title, "filename"


def discipline_from_sheet(number: str | None) -> str | None:
    if not number:
        return None
    m = re.match(r"^([A-Z]+)", number.upper())
    if not m:
        return None
    letters = m.group(1)
    if len(letters) == 1:
        return PREFIX1.get(letters)
    if len(letters) == 2:
        return PREFIX2.get(letters)
    return None


def discipline_from_keywords(path: str, title: str | None):
    """Title/file-name keywords first, then folders from the deepest upward."""
    segs = path.split("/")
    texts = [title or "", file_stem(path, path.rsplit(".", 1)[-1] if "." in path else "")]
    texts += list(reversed(segs[:-1]))
    for text in texts:
        low = text.lower()
        if not low:
            continue
        for code, words in KEYWORDS:
            for w in words:
                if w in low:
                    return code, w
    return None, None


def discipline_from_votes(votes: dict | None):
    tally: collections.Counter = collections.Counter()
    for k, v in (votes or {}).items():
        code = LAYER_DISC.get(str(k).lower())
        if code and isinstance(v, (int, float)) and v > 0:
            tally[code] += v
    total = sum(tally.values())
    if total < 5:
        return None, 0.0
    code, top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return code, round(min(0.8, 0.5 + 0.3 * top / total), 2)


def classify_discipline(path: str, number: str | None, title: str | None, votes: dict | None) -> dict:
    code = discipline_from_sheet(number)
    if code:
        out = {"code": code, "confidence": 0.85, "source": "sheet_prefix"}
    else:
        code, word = discipline_from_keywords(path, title)
        if code:
            out = {"code": code, "confidence": 0.75, "source": "keyword", "keyword": word}
        else:
            code, conf = discipline_from_votes(votes)
            if code:
                out = {"code": code, "confidence": conf, "source": "layer_votes"}
            else:
                out = {"code": "GENERAL", "confidence": 0.4, "source": "default"}
    out["review"] = out["confidence"] < REVIEW_BELOW
    return out


# --------------------------------------------------------------------------- helpers
def scrub_text(t: str) -> str:
    t = EMAIL_RE.sub(" ", t)
    t = PHONE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha256_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def bound(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def join_bounded(items, limit: int, sep: str = " / ") -> str:
    out, size = [], 0
    for it in items:
        add = len(it) + (len(sep) if out else 0)
        if size + add > limit:
            break
        out.append(it)
        size += add
    return sep.join(out)


def group_duplicates(rows: list[dict]) -> list[dict]:
    """One representative per unique (size, md5) file; the rest are recorded as duplicate_copies.

    inventory.json marked the first copy it saw; if that first copy was excluded afterwards (e.g. repo copies)
    the earliest remaining member becomes the representative.
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r.get("size"), r.get("md5") or r["path"])
        groups.setdefault(key, []).append(r)
    reps = []
    for members in groups.values():
        members.sort(key=lambda r: (1 if r.get("dup_of") else 0, r["path"]))
        rep = dict(members[0])
        rep.pop("dup_of", None)
        rest = [m["path"] for m in members[1:]]
        rep["_copies"] = rest
        rep["_group_paths"] = [m["path"] for m in members]
        reps.append(rep)
    reps.sort(key=lambda r: r["path"])
    return reps


def load_records(path: Path) -> tuple[dict[str, dict], int]:
    """Last record per path wins (the extractor appends); a half-written last line is tolerated."""
    recs: dict[str, dict] = {}
    bad = 0
    if not path.is_file():
        return recs, bad
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(r, dict) and r.get("path"):
                recs[r["path"]] = r
    return recs, bad


def project_of_path(path: str) -> str:
    """Sub-project grouping identical to tools/select.py (cad_all.json): first 3 path segments,
    4 under 1.회사/2.공모, the whole path when shallower."""
    segs = str(path or "").split("/")[:-1]
    if len(segs) <= 3:
        return "/".join(segs) or "(root)"
    if segs[0] == "1.회사" and segs[1] == "2.공모":
        return "/".join(segs[:4])
    return "/".join(segs[:3])


_DOC_KEY_RE = re.compile(r"[\s\-_\.\[\](){}]+")


def _doc_key(s: str) -> str:
    return _DOC_KEY_RE.sub("", str(s or "").lower())


def load_documents(path: Path) -> list[dict]:
    """PDF / xls / xlsx metadata rows from inventory.json, with a sub-project; no content is read."""
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        p, ext = r.get("path") or "", str(r.get("ext") or "").lower()
        if not p or ext not in DOC_EXTS or p.startswith(DOC_EXCLUDE_PREFIX):
            continue
        d = dict(r)
        d["ext"], d["project"] = ext, project_of_path(p)
        out.append(d)
    return out


# --------------------------------------------------------------------------- graph build
def build_graph(rows: list[dict], records: dict[str, dict], max_layers: int = MAX_LAYERS,
                max_blocks: int = MAX_BLOCKS, docs: list[dict] | None = None) -> dict:
    reps = group_duplicates(rows)
    docs = docs or []
    path_to_rep = {p: rep["path"] for rep in reps for p in rep["_group_paths"]}
    rec_by_rep: dict[str, dict] = {}
    for p, rec in records.items():
        rp = path_to_rep.get(p)
        if rp is not None:
            rec_by_rep[rp] = rec

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    aliases: list[dict] = []
    vectors: list[dict] = []
    review: list[dict] = []

    def add_node(nid, ntype, name, search_text, **props):
        nodes[nid] = {"id": nid, "project_key": PROJECT_KEY, "type": ntype, "name": name,
                      "props": props, "search_text": search_text}

    def add_edge(src, pred, dst, weight=1.0, **evidence):
        edges.append({"src": src, "predicate": pred, "dst": dst, "project_key": PROJECT_KEY,
                      "weight": weight, "evidence": evidence})

    def add_vector(nid, kind, label, discipline="", properties=None):
        text = nodes[nid]["search_text"]
        row = {"node_id": nid, "kind": kind, "label": label, "discipline": discipline,
               "model": "bge-m3", "dim": 1024, "content_hash": sha256_text(text), "text": text}
        if properties:
            row["properties"] = properties  # copied to aec.objects.payload.properties (renderer / citations)
        vectors.append(row)

    # ---- sub-projects
    subs: dict[str, dict] = {}
    for rep in reps:
        sp = rep.get("project") or "(root)"
        s = subs.setdefault(sp, {"drawings": 0, "dups": 0, "exts": collections.Counter(),
                                 "disc": collections.Counter(), "bytes": 0})
        s["drawings"] += 1
        s["dups"] += len(rep["_copies"])
        s["exts"][rep["ext"]] += 1
        s["bytes"] += int(rep.get("size") or 0)

    # ---- drawings first (we need discipline counts for the project/sub-project rows)
    drawing_rows = []
    layer_agg: dict[str, dict] = {}
    block_agg: dict[str, dict] = {}
    proj_disc: collections.Counter = collections.Counter()
    proj_exts: collections.Counter = collections.Counter()
    status_counts: collections.Counter = collections.Counter()
    for rep in reps:
        path, ext = rep["path"], rep["ext"]
        rec = rec_by_rep.get(rep["path"])
        number, title, sheet_src = sheet_fields(path, ext, rec)
        # The whole stem is not an *extra* title for search text / discipline rules (it is already the node
        # name), but the renderer still shows it as the title instead of "제목 미상".
        stem_title = title if title and title == file_stem(path, ext) else None
        if stem_title:
            title = None
        votes = (rec or {}).get("discipline_votes") if rec and rec.get("status") == "ok" else None
        disc = classify_discipline(path, number, title, votes)
        if ext not in CAD_DWG:
            status = "unparsed_format"
        elif rec is None:
            status = "metadata_only"
        elif rec.get("status") == "ok":
            status = "parsed"
        else:
            status = "parse_failed"
        status_counts[status] += 1
        stem = file_stem(path, ext)
        did = f"{PROJECT_KEY}:drw:" + (rep.get("md5") or sha1(path)[:32])
        sub_path = rep.get("project") or "(root)"
        sub_leaf = sub_path.rsplit("/", 1)[-1]
        subs[sub_path]["disc"][disc["code"]] += 1
        proj_disc[disc["code"]] += 1
        proj_exts[ext] += 1

        props = {"path": path, "file_name": path.rsplit("/", 1)[-1], "ext": ext, "size": int(rep.get("size") or 0),
                 "md5": rep.get("md5"), "mtime": (rep.get("mod") or "")[:10], "sub_project": sub_path,
                 "sheet_number": number, "title": title or stem_title, "drive_path": path,
                 "sheet_source": sheet_src,
                 "discipline": disc["code"], "discipline_confidence": disc["confidence"],
                 "discipline_source": disc["source"], "review": disc["review"], "parse_status": status,
                 "duplicate_count": len(rep["_copies"]), "duplicate_copies": rep["_copies"][:MAX_DUP_COPIES]}
        parts = [f"도면 {stem}"]
        if number:
            parts.append(f"도면번호 {number}")
        if title:
            parts.append(f"제목 {title}")
        parts.append(f"공종 {DISCIPLINE_KO[disc['code']]}")
        parts.append(f"프로젝트 {sub_leaf}")
        parts.append(f"경로 {path}")
        parts.append(f"형식 {ext.upper()}")
        if status == "unparsed_format":
            parts.append("미해석 형식(메타데이터만)")
        if rec and rec.get("status") == "ok":
            counts = rec.get("counts") or {}
            props.update(layer_count=counts.get("layer_count"), block_count=rec.get("block_defs"),
                         text_count=rec.get("text_total"), entity_count=counts.get("entity_count"),
                         units=rec.get("units"), dxf_version=rec.get("dxf_version"))
            rooms = [scrub_text(k) for k in (rec.get("rooms") or {})]
            rooms = [r for r in rooms if r]
            props["rooms"] = rooms[:25]
            texts = [scrub_text(t) for t in (rec.get("texts") or [])]
            texts = [t for t in texts if len(t) >= 2]
            lnames = [l["name"] for l in sorted(rec.get("layers") or [], key=lambda l: -(l.get("entities") or 0))
                      if l.get("name") and l["name"] != "0" and (l.get("entities") or 0) > 0]
            if rooms:
                parts.append("실: " + join_bounded(rooms, MAX_ROOMS_CHARS, ", "))
            if lnames:
                parts.append("레이어: " + join_bounded(lnames, MAX_LAYER_CHARS, ", "))
            if texts:
                parts.append("문자: " + join_bounded(texts, MAX_TEXTS_CHARS))
            # aggregate global layers / blocks (once per unique drawing)
            for l in rec.get("layers") or []:
                nm = (l.get("name") or "").strip()
                if not nm or nm.upper() in ("0", "DEFPOINTS"):
                    continue
                a = layer_agg.setdefault(nm.upper(), {"name": nm, "drawings": {}, "entities": 0,
                                                      "disc": collections.Counter(), "cls": collections.Counter(),
                                                      "conf": 0.0})
                a["drawings"][did] = a["drawings"].get(did, 0) + int(l.get("entities") or 0)
                a["entities"] += int(l.get("entities") or 0)
                a["disc"][l.get("discipline") or "unknown"] += 1
                a["cls"][l.get("element_class") or "unknown"] += 1
                a["conf"] = max(a["conf"], float(l.get("confidence") or 0))
            for b in rec.get("blocks") or []:
                nm = (b.get("name") or "").strip()
                if not nm or nm.startswith("*"):
                    continue
                a = block_agg.setdefault(nm, {"name": nm, "drawings": {}, "inserts": 0, "category": b.get("category"),
                                              "attrs": []})
                a["drawings"][did] = a["drawings"].get(did, 0) + int(b.get("inserts") or 0)
                a["inserts"] += int(b.get("inserts") or 0)
                if b.get("attrs") and not a["attrs"]:
                    a["attrs"] = list(b["attrs"])[:8]
        search_text = bound(". ".join(parts) + ".", MAX_SEARCH)
        drawing_rows.append((did, stem, props, search_text, sub_path, disc, rep))

    # ---- nodes: Project, SubProject, Drawing
    n_draw = len(drawing_rows)
    n_dup = sum(len(r[6]["_copies"]) for r in drawing_rows)
    review_n = sum(1 for r in drawing_rows if r[5]["review"])
    proj_text = ("Google Drive CAD 자산 도면 목록 프로젝트 공종 도면번호 레이어 블록 "
                 f"(도면 {n_draw}건, 중복 사본 {n_dup}건)")
    add_node(PROJECT_NODE, "Project", "Google Drive CAD", proj_text, pack=True, drawings=n_draw,
             duplicate_files=n_dup, sub_projects=len(subs), disciplines=dict(sorted(proj_disc.items())),
             file_types=dict(proj_exts.most_common()), parse_status=dict(sorted(status_counts.items())),
             review_queue=review_n, source="gdrive")
    aliases += [{"alias_type": "project_id", "alias": PROJECT_KEY, "node_id": PROJECT_NODE},
                {"alias_type": "project_name", "alias": PROJECT_KEY, "node_id": PROJECT_NODE},
                {"alias_type": "project_name", "alias": "Google Drive CAD", "node_id": PROJECT_NODE},
                {"alias_type": "project_name", "alias": "구글드라이브 CAD", "node_id": PROJECT_NODE}]

    sub_ids: dict[str, str] = {}
    for sp in sorted(subs):
        s = subs[sp]
        sid = f"{PROJECT_KEY}:sub:" + sha1(sp)[:16]
        sub_ids[sp] = sid
        leaf = sp.rsplit("/", 1)[-1]
        top = ", ".join(f"{DISCIPLINE_KO[k]} {v}" for k, v in s["disc"].most_common(4))
        text = bound(f"프로젝트 폴더 {leaf}. 경로 {sp}. 도면 {s['drawings']}건 (중복 사본 {s['dups']}건). "
                     f"공종 분포: {top}. 형식: " + ", ".join(f"{k} {v}" for k, v in s["exts"].most_common(5)) + ".",
                     MAX_SEARCH)
        add_node(sid, "SubProject", leaf, text, path=sp, drawings=s["drawings"], duplicate_files=s["dups"],
                 bytes=s["bytes"], file_types=dict(s["exts"].most_common()),
                 disciplines=dict(s["disc"].most_common()))
        add_edge(PROJECT_NODE, "hasSubProject", sid)
        add_vector(sid, "sub_project", leaf)

    for did, stem, props, text, sp, disc, rep in drawing_rows:
        add_node(did, "Drawing", stem, text, **props)
        add_edge(sub_ids[sp], "hasDrawing", did)
        add_edge(PROJECT_NODE, "hasDrawing", did)
        add_vector(did, "drawing", stem, disc["code"],
                   {k: props.get(k) for k in ("sheet_number", "title", "sub_project", "drive_path") if props.get(k)})
        if disc["review"]:
            review.append({"node_id": did, "path": props["path"], "sheet_number": props["sheet_number"],
                           "title": props["title"], "discipline": disc["code"],
                           "confidence": disc["confidence"], "source": disc["source"],
                           "parse_status": props["parse_status"]})

    # ---- global LayerStandard / BlockSpec (caps), usedIn edges to Drawings
    layers_sorted = sorted(layer_agg.values(), key=lambda a: (-len(a["drawings"]), -a["entities"], a["name"].upper()))
    for a in layers_sorted[:max_layers]:
        lid = f"{PROJECT_KEY}:layer:" + sha1(a["name"].upper())[:16]
        known = [(k, v) for k, v in a["disc"].most_common() if k != "unknown"]
        ldisc = LAYER_DISC.get(known[0][0], "GENERAL") if known else "GENERAL"
        klass = next((k for k, _ in a["cls"].most_common() if k != "unknown"), "unknown")
        text = bound(f"레이어 {a['name']}. 공종 {DISCIPLINE_KO[ldisc]}. 요소 분류 {klass}. "
                     f"사용 도면 {len(a['drawings'])}건, 객체 {a['entities']}개.", MAX_SEARCH)
        add_node(lid, "LayerStandard", a["name"], text, discipline=ldisc, element_class=klass,
                 drawings=len(a["drawings"]), entities=a["entities"], confidence=round(a["conf"], 2))
        for d in sorted(a["drawings"]):
            add_edge(lid, "usedIn", d, entities=a["drawings"][d])
        add_vector(lid, "layer_standard", a["name"], ldisc)
    blocks_sorted = sorted(block_agg.values(), key=lambda a: (-len(a["drawings"]), -a["inserts"], a["name"]))
    for a in blocks_sorted[:max_blocks]:
        bid = f"{PROJECT_KEY}:block:" + sha1(a["name"])[:16]
        extra = (f" 분류 {a['category']}." if a.get("category") else "") + \
                (" 속성 " + ", ".join(a["attrs"]) + "." if a["attrs"] else "")
        text = bound(f"블록 {a['name']}.{extra} 사용 도면 {len(a['drawings'])}건, 삽입 {a['inserts']}회.", MAX_SEARCH)
        add_node(bid, "BlockSpec", a["name"], text, drawings=len(a["drawings"]), inserts=a["inserts"],
                 category=a.get("category"), attrs=a["attrs"])
        for d in sorted(a["drawings"]):
            add_edge(bid, "usedIn", d, inserts=a["drawings"][d])
        add_vector(bid, "block_spec", a["name"], "")

    # ---- documents (PDF / xls / xlsx): metadata-only nodes, linked to drawings by stem
    doc_reps = group_duplicates(docs)
    draws_by_sub: dict[str, set] = {}
    for did, stem, props, _text, sp, _disc, _rep in drawing_rows:
        keys = {_doc_key(stem), _doc_key(props.get("sheet_number")), _doc_key(props.get("title"))}
        draws_by_sub.setdefault(sp, set()).update((k, did) for k in keys if k)
    for sp in draws_by_sub:
        draws_by_sub[sp] = sorted(draws_by_sub[sp])

    n_doc_dups = 0
    doc_exts: collections.Counter = collections.Counter()
    documents_total = len(doc_reps)
    document_links = 0
    for rep in doc_reps:
        path, ext = rep["path"], rep["ext"]
        stem = file_stem(path, ext)
        sp = rep.get("project") or project_of_path(path)
        sub_leaf = sp.rsplit("/", 1)[-1]
        linked = [did for k, did in draws_by_sub.get(sp, []) if k == _doc_key(stem)]
        n_doc_dups += len(rep["_copies"])
        document_links += len(linked)
        doc_exts[ext] += 1
        did_doc = f"{PROJECT_KEY}:doc:" + (rep.get("md5") or sha1(path)[:32])
        title = re.sub(r"\s+", " ", scrub_text(stem)).strip() or stem
        props = {"path": path, "file_name": path.rsplit("/", 1)[-1], "ext": ext,
                 "size": int(rep.get("size") or 0), "md5": rep.get("md5"),
                 "mtime": (rep.get("mod") or "")[:10], "sub_project": sp, "title": title,
                 "drive_path": path, "duplicate_count": len(rep["_copies"]),
                 "duplicate_copies": rep["_copies"][:MAX_DUP_COPIES], "linked_drawings": linked}
        parts = [f"문서 {scrub_text(stem)}", f"형식 {ext.upper()}", f"프로젝트 {scrub_text(sub_leaf)}",
                 f"경로 {scrub_text(path)}"]
        if props["size"] >= 0:
            parts.append(f"크기 {props['size']}바이트")
        if linked:
            parts.append(f"연결 도면 {len(linked)}건")
        add_node(did_doc, "Document", title, bound(". ".join(parts) + ".", MAX_SEARCH), **props)
        add_edge(PROJECT_NODE, "hasDocument", did_doc)
        if sp in sub_ids:
            add_edge(sub_ids[sp], "hasDocument", did_doc)
        for did in linked:
            add_edge(did_doc, "linkedTo", did, match="stem")
        add_vector(did_doc, "document", title, "", {"sub_project": sp, "drive_path": path, "ext": ext})

    return {"nodes": nodes, "edges": edges, "aliases": aliases, "vectors": vectors, "review": review,
            "status_counts": dict(status_counts), "layers_total": len(layer_agg), "blocks_total": len(block_agg),
            "duplicate_copies": n_dup, "path_to_rep": path_to_rep, "documents": documents_total,
            "document_dups": n_doc_dups, "document_links": document_links, "document_exts": dict(doc_exts)}


# --------------------------------------------------------------------------- SQL
def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def render_sql(nodes: dict, edges: list, aliases: list) -> str:
    lines = [
        "-- Google Drive CAD asset pack load script (idempotent).",
        f"-- project_key = {PROJECT_KEY}",
        "-- Targets the aec.* schema created by 0001_core.sql / 0002_knowledge_graph.sql.",
        "BEGIN;",
        f"DELETE FROM aec.kg_edges WHERE project_key = {q(PROJECT_KEY)};",
        f"DELETE FROM aec.kg_aliases WHERE node_id IN (SELECT id FROM aec.kg_nodes WHERE project_key = {q(PROJECT_KEY)});",
        f"DELETE FROM aec.kg_nodes WHERE project_key = {q(PROJECT_KEY)};",
    ]
    for n in nodes.values():
        lines.append(
            "INSERT INTO aec.kg_nodes(id,project_key,type,name,props,object_ids,document_ids,search_text) "
            f"VALUES ({q(n['id'])},{q(PROJECT_KEY)},{q(n['type'])},{q(n['name'])},"
            f"{q(json.dumps(n['props'], ensure_ascii=False))}::jsonb,'{{}}','{{}}',{q(n['search_text'])}) "
            "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, props=EXCLUDED.props, "
            "search_text=EXCLUDED.search_text, updated_at=now();")
    for e in edges:
        lines.append(
            "INSERT INTO aec.kg_edges(src,predicate,dst,project_key,weight,evidence) "
            f"VALUES ({q(e['src'])},{q(e['predicate'])},{q(e['dst'])},{q(PROJECT_KEY)},{e['weight']},"
            f"{q(json.dumps(e['evidence'], ensure_ascii=False))}::jsonb) "
            "ON CONFLICT (src,predicate,dst) DO UPDATE SET weight=EXCLUDED.weight, evidence=EXCLUDED.evidence;")
    for a in aliases:
        lines.append(
            "INSERT INTO aec.kg_aliases(alias_type,alias,node_id) "
            f"VALUES ({q(a['alias_type'])},{q(a['alias'])},{q(a['node_id'])}) "
            "ON CONFLICT (alias_type,alias,node_id) DO NOTHING;")
    lines.append("INSERT INTO aec.kg_build_state(project_key,fingerprint,nodes,edges,built_at) "
                 f"VALUES ({q(PROJECT_KEY)},{q(FINGERPRINT)},{len(nodes)},{len(edges)},now()) "
                 "ON CONFLICT (project_key) DO UPDATE SET fingerprint=EXCLUDED.fingerprint, "
                 "nodes=EXCLUDED.nodes, edges=EXCLUDED.edges, built_at=now();")
    lines.append("-- link pack nodes to their aec.objects rows (object id == node id) so GraphRAG can cite them")
    lines.append("UPDATE aec.kg_nodes n SET object_ids = ARRAY[o.id], document_ids = ARRAY[o.document_id] "
                 f"FROM aec.objects o WHERE o.id = n.id AND n.project_key = {q(PROJECT_KEY)};")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def input_fingerprint(src: Path, max_layers: int, max_blocks: int) -> str:
    h = hashlib.sha256()
    h.update(f"v{BUILDER_VERSION}|{max_layers}|{max_blocks}|{FINGERPRINT}".encode())
    for name in ("cad_all.json", "records.jsonl", "inventory.json"):
        p = src / name
        if p.is_file():
            h.update(name.encode())
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(PACK))
    ap.add_argument("--max-layers", type=int, default=MAX_LAYERS)
    ap.add_argument("--max-blocks", type=int, default=MAX_BLOCKS)
    ap.add_argument("--force", action="store_true", help="rebuild even when inputs are unchanged")
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)

    cad_path = src / "cad_all.json"
    if not cad_path.is_file():
        print(f"BLOCKED: {cad_path} not found", file=sys.stderr)
        return 2
    fp = input_fingerprint(src, args.max_layers, args.max_blocks)
    mpath = out / "manifest.json"
    if mpath.is_file() and not args.force:
        try:
            old = json.loads(mpath.read_text(encoding="utf-8"))
            if old.get("input_fingerprint") == fp and (out / "graph" / "kg_nodes.jsonl").is_file():
                print(f"UP TO DATE (input_fingerprint {fp[:12]}); use --force to rebuild")
                return 0
        except (OSError, json.JSONDecodeError):
            pass

    rows = json.loads(cad_path.read_text(encoding="utf-8"))
    rows = [r for r in rows if r.get("path") and r.get("ext")]
    records, bad = load_records(src / "records.jsonl")
    docs = load_documents(src / "inventory.json")
    g = build_graph(rows, records, args.max_layers, args.max_blocks, docs)
    nodes, edges, aliases, vectors, review = g["nodes"], g["edges"], g["aliases"], g["vectors"], g["review"]

    # fail loudly on a graph that GraphRAG could not load
    ids = set(nodes)
    assert len(ids) == len(nodes)
    dangling = [e for e in edges if e["src"] not in ids or e["dst"] not in ids]
    if dangling:
        print(f"FATAL: {len(dangling)} dangling edges", file=sys.stderr)
        return 1

    write_jsonl(out / "graph" / "kg_nodes.jsonl", nodes.values())
    write_jsonl(out / "graph" / "kg_edges.jsonl", edges)
    write_jsonl(out / "graph" / "kg_aliases.jsonl", aliases)
    write_jsonl(out / "vectors" / "vector_corpus.jsonl", vectors)
    write_jsonl(out / "assets" / "review_queue.jsonl", sorted(review, key=lambda r: (r["confidence"], r["path"])))
    (out / "sql").mkdir(parents=True, exist_ok=True)
    (out / "sql" / "gdrive_cad_kg_load.sql").write_text(render_sql(nodes, edges, aliases), encoding="utf-8",
                                                         newline="\n")

    node_types = collections.Counter(n["type"] for n in nodes.values())
    preds = collections.Counter(e["predicate"] for e in edges)
    discs = collections.Counter(n["props"]["discipline"] for n in nodes.values() if n["type"] == "Drawing")
    manifest = {
        "pack": "gdrive_cad",
        "project_key": PROJECT_KEY,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "input_fingerprint": fp,
        "builder_version": BUILDER_VERSION,
        "source_root": "gdrive (rclone remote; paths are Drive-relative)",
        "source_files": len(rows),
        "records_parsed": len(records),
        "records_unreadable_lines": bad,
        "filename_sheet_parser": FILENAME_SHEET_SOURCE,
        "counts": {
            "kg_nodes": len(nodes), "kg_edges": len(edges), "kg_aliases": len(aliases),
            "vector_rows": len(vectors), "drawings": node_types["Drawing"], "sub_projects": node_types["SubProject"],
            "layer_standards": node_types["LayerStandard"], "block_specs": node_types["BlockSpec"],
            "duplicate_copies": g["duplicate_copies"], "review_queue": len(review),
            "layers_seen": g["layers_total"], "blocks_seen": g["blocks_total"],
            "documents": node_types["Document"], "document_links": g["document_links"],
            "document_duplicates": g["document_dups"],
        },
        "document_formats": g["document_exts"],
        "caps": {"layers": args.max_layers, "blocks": args.max_blocks},
        "parse_status": g["status_counts"],
        "disciplines": dict(sorted(discs.items())),
        "node_types": sorted(node_types),
        "predicates": sorted(preds),
        "fingerprint": FINGERPRINT,
        "embedding": {"model": "bge-m3", "dim": 1024, "storage": "aec.text_vectors/halfvec"},
    }
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=1))
    print(f"node_types={dict(node_types)}")
    print(f"predicates={dict(preds)}")
    print(f"document_formats={g['document_exts']}")
    print(f"disciplines={dict(discs)} parse_status={g['status_counts']}")
    print(f"out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

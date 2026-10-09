#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ArchiOfficeZW local asset -> ontology pack builder.

Reads the ArchiOffice ZWCAD roaming folder read-only and emits:
  assets/    file + domain records (jsonl/json)
  graph/     aec.kg_nodes / kg_edges / kg_aliases records + JSON-LD
  vectors/   bge-m3 ready corpus (text, sha256, 1024 dim)
  sql/       idempotent load script for the aec.* schema

Nothing outside OUT_ROOT is written. The source tree is never modified.

Usage:
  python build_archioffice_pack.py [--source DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOURCE = Path.home().joinpath(
    "AppData", "Local", "Packages", "OpenAI.Codex_2p2nqsd0c76g0", "LocalCache", "Roaming", "ArchiOfficeZW"
)
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent
PROJECT_KEY = "ARCHIOFFICE"
EMBED_MODEL = "bge-m3"
EMBED_DIM = 1024

TEXT_EXT = {".ini", ".txt", ".pgp", ".lsp", ".lin", ".cfg"}
PAT_EXT = {".pat"}
VECTOR_EXT = None  # unused

# ---------------------------------------------------------------- encoding ----

def read_text(path: Path) -> tuple[str, str]:
    """Return (text, encoding). KR CAD configs are cp949; try utf-8 first."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace"), "cp949"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


_SLUG_RE = re.compile(r"[^0-9a-z가-힣]+")


def slug(value: str) -> str:
    s = _SLUG_RE.sub("-", value.strip().lower())
    return s.strip("-")


DIM_RE = re.compile(r"(\d{2,5})\s*[xX×]\s*(\d{2,5})")

# ---------------------------------------------------------------- taxonomy ----

LIB_CATEGORY = {
    "창호": ("door_window", "창호 (문·창) 라이브러리"),
    "조경": ("landscape", "조경 (수목·지피) 라이브러리"),
    "엘리베이터": ("elevator", "엘리베이터 라이브러리"),
    "화장실": ("sanitary", "화장실 위생기구 라이브러리"),
    "가구": ("furniture", "가구 라이브러리"),
    "기호": ("annotation", "기호·주석 라이브러리"),
}

VIEW_MAP = {
    "평면": ("plan", "평면도(plan) 표현"),
    "입면": ("elevation", "입면도(elevation) 표현"),
    "창입면": ("elevation", "창 입면도(elevation) 표현"),
    "3D": ("three_d", "3D 표현"),
}

HABIT_MAP = {
    "낙엽교목": ("deciduous_tree", "낙엽교목 (deciduous tree)"),
    "상록교목": ("evergreen_tree", "상록교목 (evergreen tree)"),
    "낙엽관목": ("deciduous_shrub", "낙엽관목 (deciduous shrub)"),
    "상록관목": ("evergreen_shrub", "상록관목 (evergreen shrub)"),
    "초화류,지피": ("herbaceous_groundcover", "초화류·지피 (herbaceous / groundcover)"),
}

MATERIAL_RULES = [
    ("brick_masonry", r"brick|bric|masnry|mason|블록|block|bevbric|cmu|벽돌", "벽돌·조적"),
    ("concrete", r"conc|concr|cmnt|cement|콘크리트|precast", "콘크리트"),
    ("stone", r"stone|granit|marbl|terrazzo|slate|석|대리석|화강|schist|mica|quartz|monzonite|rock|boulder|pebble|암|aggregate|ar-?conc|gran", "석재·암석"),
    ("insulation", r"insul|batt|rigid|foam|단열|보온", "단열재"),
    ("wood", r"wood|timber|lumb|plyw|목재|나무|\blog\b|cedar|\bshake\b", "목재"),
    ("metal", r"steel|metal|alum|iron|copper|bronze|금속|철|알루|\bplate\b|tread|checker|checked", "금속"),
    ("glass", r"glass|glaz|유리", "유리"),
    ("earth_ground", r"earth|ground|soil|sand|gravel|clay|흙|지반|모래|자갈|\bfill\b|\bdirt\b", "지반·토사"),
    ("water", r"water|aqua|물", "물·수계"),
    ("roofing", r"roof|shingl|tile|지붕|기와", "지붕·기와"),
    ("paving", r"paving|pav\b|asphalt|asph|보도|포장", "포장"),
    ("abstract_geometric", r"dot|grid|cross|stripe|diag|[0-9]{2,}x|circle|hexagon|triang|abstract|pattern[0-9]|diamond|star|cyl|\bgreek\b", "추상·기하"),
]
MATERIAL_LABEL = {c: label for c, _re, label in MATERIAL_RULES}


def material_class(name: str, desc: str) -> str:
    blob = f"{name} {desc}"
    for cls, rx, _label in MATERIAL_RULES:
        if re.search(rx, blob, re.IGNORECASE):
            return cls
    return "unclassified"


def classify(rel_parts: list[str]) -> dict:
    top = rel_parts[0]
    out = {"category": "other", "category_label": "기타", "subtype": "", "subtype_label": "",
           "view": "", "view_label": "", "code_prefix": "", "discipline": "ARCHIOFFICE_LIBRARY"}
    if top == "Hatch":
        out.update(category="hatch_pattern", category_label="해치 패턴", subtype="pattern_definition",
                   subtype_label="AutoCAD 해치 패턴 정의", discipline="AEC_MATERIAL_PATTERN")
        return out
    if top == "2024":
        out.update(category="zwcad_platform_config", category_label="ZWCAD 플랫폼 설정",
                   subtype=Path(rel_parts[-1]).stem, subtype_label="ZWCAD/AutoCAD 지원 파일",
                   discipline="CAD_PLATFORM_CONFIG")
        return out
    if top != "Library":
        out.update(category="archioffice_config", category_label="ArchiOffice 설정",
                   subtype=Path(rel_parts[-1]).stem, subtype_label="ArchiOffice 환경 설정",
                   discipline="AEC_OFFICE_CONFIG")
        return out

    lib = rel_parts[1] if len(rel_parts) > 1 else ""
    cat, cat_label = LIB_CATEGORY.get(lib, (slug(lib) or "root", lib))
    out.update(category=cat, category_label=cat_label)

    if lib == "창호":
        kind = rel_parts[2] if len(rel_parts) > 2 else ""
        leaf = rel_parts[3] if len(rel_parts) > 3 else ""
        out["category"] = "door" if "문" in kind else "window"
        out["category_label"] = "문(Door) 심볼" if "문" in kind else "창(Window) 심볼"
        m = re.search(r"\(([A-Z0-9]{2})\)", leaf)
        out["code_prefix"] = m.group(1) if m else ""
        for key, (code, label) in VIEW_MAP.items():
            if key in leaf:
                out.update(view=code, view_label=label)
                break
        out["subtype"] = f"{out['category']}_{out['view'] or 'unspecified'}"
        out["subtype_label"] = f"{out['category_label']} / {out['view_label'] or '뷰 미지정'}"
        return out

    if lib == "조경":
        out.update(category="landscape_plant", category_label="조경 수목·지피 심볼", view="plan",
                   view_label="평면도(plan) 표현")
        habit = ""
        for part in rel_parts:
            if part in HABIT_MAP:
                habit = part
                break
        if habit:
            code, label = HABIT_MAP[habit]
            out.update(subtype=code, subtype_label=label)
        else:
            out.update(subtype="plan_symbol", subtype_label="조경 평면 심볼 (수종 분류 없음)")
        return out

    if lib == "엘리베이터":
        seg = "/".join(rel_parts[2:])
        sub = "unspecified"
        for key, code in (("기계실 없는", "machine_room_less"), ("기어리스", "gearless"),
                          ("승객용", "passenger"), ("병원용", "hospital"), ("운구형", "stretcher")):
            if key in seg:
                sub = code
                break
        out.update(category="elevator", subtype=sub, subtype_label=f"엘리베이터 / {sub}")
        last = rel_parts[-1]
        for key, (code, label) in VIEW_MAP.items():
            if key in last:
                out.update(view=code, view_label=label)
                break
        return out

    if lib == "화장실":
        seg = "/".join(rel_parts[2:])
        sub = "urinal" if "소변기" in seg else "bathtub" if "욕조" in seg else "fixture_unit"
        out.update(category="sanitary_fixture", subtype=sub, subtype_label=f"위생기구 / {sub}")
        for key, (code, label) in VIEW_MAP.items():
            if key in seg:
                out.update(view=code, view_label=label)
                break
        return out

    if lib == "가구":
        seg = "/".join(rel_parts[2:])
        sub = "chair" if "의자" in seg else "kitchen" if "주방" in seg else "unspecified"
        out.update(category="furniture", subtype=sub, subtype_label=f"가구 / {sub}",
                   view="plan", view_label="평면도(plan) 표현")
        return out

    if lib == "기호":
        seg = "/".join(rel_parts[2:])
        sub = "direction" if "방향" in seg else "unspecified"
        out.update(category="annotation_symbol", subtype=sub, subtype_label=f"기호 / {sub}",
                   view="plan", view_label="평면도(plan) 표현")
        return out

    out["subtype"] = slug(lib) or "root"
    out["subtype_label"] = lib
    return out


# ------------------------------------------------------------------ parsers ---

def parse_pat(text: str, rel: str) -> tuple[list[dict], list[dict]]:
    patterns, failures, cur = [], [], None
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in ";#" or line.startswith("//"):
            continue
        if "\x1a" in line:  # DOS EOF marker common at end of .pat files
            continue
        if line.startswith("*"):
            body = line[1:]
            name, _, desc = body.partition(",")
            cur = {"pattern_name": name.strip(), "description": desc.strip(),
                   "definition_lines": [], "angles": []}
            patterns.append(cur)
            continue
        fields = [f.strip() for f in line.split(",") if f.strip()]
        try:
            nums = [float(f) for f in fields]
        except ValueError:
            failures.append({"line": i, "text": raw[:120], "reason": "non-numeric field"})
            continue
        if len(nums) < 5:
            failures.append({"line": i, "text": raw[:120], "reason": "fewer than 5 fields"})
            continue
        if cur is None:
            failures.append({"line": i, "text": raw[:120], "reason": "definition before header"})
            continue
        angle, ox, oy, dx, dy, *dashes = nums
        cur["definition_lines"].append({"angle": angle, "origin_x": ox, "origin_y": oy,
                                        "delta_x": dx, "delta_y": dy, "dashes": dashes})
        cur["angles"].append(angle)
    return patterns, failures


def pattern_type(p: dict) -> str:
    defs = p["definition_lines"]
    if all(not d["dashes"] for d in defs):
        return "line" if len(defs) == 1 else "multi_line"
    flat = [v for d in defs for v in d["dashes"]]
    if flat and all(v == 0 for v in flat):
        return "dot"
    if any(v < 0 for v in flat):
        return "cross_or_complex"
    return "dash"


def parse_lin(text: str) -> list[dict]:
    out, cur = [], None
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in ";#":
            continue
        if "\x1a" in line:
            continue
        if line.startswith("*"):
            body = line[1:]
            name, _, desc = body.partition(",")
            cur = {"linetype_name": name.strip(), "description": desc.strip(),
                   "element_lines": [], "line": i}
            out.append(cur)
            continue
        if cur is not None and re.match(r"^[Aa],", line):
            cur["element_lines"].append(line)
    return out


def parse_pgp(text: str) -> dict:
    """AutoCAD/ZWCAD program parameters: external commands + command aliases."""
    external, aliases = [], []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            low = line.lower()
            if "external command" in low:
                section = "external"
            elif "command alias" in low:
                section = "alias"
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        if section == "alias" or parts[1].startswith("*"):
            target = parts[1].lstrip("*").strip()
            if parts[0] and target:
                aliases.append({"alias": parts[0], "command": target})
        elif section == "external":
            external.append({"name": parts[0], "os_command": parts[1] if len(parts) > 1 else ""})
    return {"aliases": aliases, "external_commands": external}


def parse_cfg(text: str) -> dict:
    """ArchiOffice symbol placement config ([XP:...] sections)."""
    sections: dict[str, list[str]] = {}
    name = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            name = m.group(1)
            sections.setdefault(name, [])
            continue
        if name:
            sections[name].append(line)
    flat = {k: v for k, v in sections.items()}
    layer = ""
    for entry in flat.get("XP:LAYER", []):
        if entry.lower().startswith("layer name"):
            layer = entry.split("=", 1)[-1].strip()
    specs = []
    for entry in flat.get("XP:DESCRIPTION", []):
        block, _, desc = entry.partition("=")
        block, desc = block.strip(), desc.strip()
        if not block:
            continue
        dims = DIM_RE.search(desc)
        capacity = None
        cm = re.search(r"(\d+)\s*인승", desc)
        if cm:
            capacity = int(cm.group(1))
        specs.append({"block_name": block, "description": desc,
                      "capacity_persons": capacity,
                      "dimensions": ({"w": int(dims.group(1)), "h": int(dims.group(2)), "unit": "mm",
                                      "raw": dims.group(0)} if dims else None)})
    return {"sections": flat, "layer": layer, "block_specs": specs}


def parse_ini_sections(text: str) -> dict[str, list[str]]:
    sections, name = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            name = m.group(1)
            sections.setdefault(name, [])
            continue
        if name:
            sections[name].append(line)
    return sections


def parse_layers(text: str) -> list[dict]:
    layers = []
    for title, body in parse_ini_sections(text).items():
        if title.startswith("XP:"):
            continue
        rec = {"layer": title, "description": "", "color": None, "linetype": None}
        has = False
        for entry in body:
            key, _, val = entry.partition("=")
            key, val = key.strip().lower(), val.strip()
            if key == "description":
                rec["description"] = val
                has = True
            elif key == "color":
                rec["color"] = val
                has = True
            elif key == "linetype":
                rec["linetype"] = val
                has = True
        if has or title.startswith(("AA-", "AZ-", "AS-", "LP-", "WLINE")):
            layers.append(rec)
    return layers


def parse_lsp_defuns(text: str) -> list[dict]:
    return [{"function": m.group(1), "line": i}
            for i, line in enumerate(text.splitlines(), start=1)
            for m in [re.match(r"^\(defun\s+([^\s()]+)", line.strip(), re.IGNORECASE)] if m]


# ------------------------------------------------------------------ build -----

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    source = Path(args.source)
    out = Path(args.out)
    if not source.is_dir():
        print(f"source not found: {source}", file=sys.stderr)
        return 2

    for sub in ("assets", "graph", "vectors", "sql", "docs"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    aliases: list[dict] = []
    vector_rows: list[dict] = []
    file_rows: list[dict] = []
    pattern_rows: list[dict] = []
    geometry_rows: list[dict] = []
    linetype_rows: list[dict] = []
    cfg_rows: list[dict] = []
    layer_rows: list[dict] = []
    textstyle_rows: list[dict] = []
    command_rows: list[dict] = []
    setting_rows: list[dict] = []
    block_spec_rows: list[dict] = []
    hash_index: dict[str, list[str]] = {}
    pat_failures: list[dict] = []
    _vector_ids: set[str] = set()
    _edge_keys: set[tuple[str, str, str]] = set()
    _alias_keys: set[tuple[str, str, str]] = set()
    command_seen: set[str] = set()

    def add_node(nid, ntype, name, search_text, **props):
        if nid in nodes:
            return nid
        nodes[nid] = {"id": nid, "project_key": PROJECT_KEY, "type": ntype, "name": name,
                      "props": props, "search_text": search_text}
        return nid

    def add_edge(src, predicate, dst, weight=1.0, **evidence):
        key = (src, predicate, dst)
        if src in nodes and dst in nodes and key not in _edge_keys:
            _edge_keys.add(key)
            edges.append({"src": src, "predicate": predicate, "dst": dst,
                          "project_key": PROJECT_KEY, "weight": weight, "evidence": evidence})

    def add_alias(atype, alias, nid):
        key = (atype, alias, nid)
        if nid in nodes and key not in _alias_keys:
            _alias_keys.add(key)
            aliases.append({"alias_type": atype, "alias": alias, "node_id": nid})

    def add_vector(nid, kind, text, label):
        if not text or nid in _vector_ids:
            return
        _vector_ids.add(nid)
        vector_rows.append({"node_id": nid, "kind": kind, "label": label,
                            "model": EMBED_MODEL, "dim": EMBED_DIM,
                            "content_hash": sha256_text(text), "text": text})

    library_id = add_node(f"{PROJECT_KEY}:library", "Library", "ArchiOffice 도면 라이브러리",
                          "ArchiOffice 아키오피스 건축 설계 라이브러리 (ZWCAD 2024). 문·창, 조경, "
                          "엘리베이터, 화장실, 가구, 기호 심볼과 해치 패턴, 도면층 표준, 명령 설정을 포함한다.",
                          source_root=str(source), platform="ZWCAD 2024 / AutoCAD 2025",
                          pack_version="1.0")
    add_alias("library", "ArchiOffice", library_id)
    add_alias("library", "아키오피스", library_id)
    add_alias("library", "ArchiOfficeZW", library_id)

    view_ids, mat_ids, habit_ids = {}, {}, {}
    for code, label in (("plan", "평면도(plan) 표현"), ("elevation", "입면도(elevation) 표현"),
                        ("three_d", "3D 표현")):
        view_ids[code] = add_node(f"{PROJECT_KEY}:view:{code}", "ViewType", label, label, code=code)
        add_alias("view", label.split("(")[0], view_ids[code])

    # ---- walk ----------------------------------------------------------------
    all_files = sorted(p for p in source.rglob("*") if p.is_file())
    for path in all_files:
        rel = path.relative_to(source)
        rel_parts = list(rel.parts)
        rel_str = "\\".join(rel_parts)
        ext = path.suffix.lower()
        raw = path.read_bytes()
        digest = sha256_bytes(raw)
        hash_index.setdefault(digest, []).append(rel_str)
        st = path.stat()
        cls = classify(rel_parts)
        stem = path.stem
        dims = DIM_RE.search(stem)
        nominal = ({"w": int(dims.group(1)), "h": int(dims.group(2)), "unit": "mm",
                    "raw": dims.group(0)} if dims else None)

        file_rows.append({
            "asset_id": f"{PROJECT_KEY}-{cls['category']}-{slug(rel_str.rsplit('.', 1)[0])}",
            "category": cls["category"], "category_label": cls["category_label"],
            "subtype": cls["subtype"], "subtype_label": cls["subtype_label"],
            "view": cls["view"], "view_label": cls["view_label"],
            "code_prefix": cls["code_prefix"], "discipline": cls["discipline"],
            "source_name": stem, "source_file": path.name, "relative_path": rel_str,
            "source_path": str(path), "extension": ext, "size_bytes": st.st_size,
            "last_write": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "content_sha256": digest, "nominal_dimensions": nominal,
        })

        # category nodes
        cat_node = f"{PROJECT_KEY}:category:{cls['category']}"
        add_node(cat_node, "LibraryCategory", cls["category_label"],
                 f"{cls['category_label']} - {cls['discipline']}", code=cls["category"])
        add_alias("category", cls["category"], cat_node)
        add_alias("category", cls["category_label"], cat_node)
        add_edge(library_id, "hasCategory", cat_node)

        # ---- per-extension handling
        if ext in PAT_EXT:
            text, _enc = read_text(path)
            pats, fails = parse_pat(text, rel_str)
            pat_failures.extend({"file": rel_str, **f} for f in fails)
            for p in pats:
                mc = material_class(p["pattern_name"], p["description"])
                pid = f"{PROJECT_KEY}:hatch:{slug(p['pattern_name'])}"
                angles = sorted(set(p["angles"]))
                summary = (f"ArchiOffice 해치 패턴 '{p['pattern_name']}'"
                           f"{', 설명: ' + p['description'] if p['description'] else ''}"
                           f", 재료 분류: {MATERIAL_LABEL.get(mc, mc)}"
                           f", 각도: {', '.join(str(a) for a in angles)}도"
                           f", 정의선 {len(p['definition_lines'])}개"
                           f", 파일 {path.name}.")
                add_node(pid, "HatchPattern", p["pattern_name"], summary,
                         description=p["description"], material_class=mc,
                         material_label=MATERIAL_LABEL.get(mc, mc), angles=angles,
                         definition_count=len(p["definition_lines"]),
                         pattern_type=pattern_type(p), source_file=path.name,
                         source_hash=digest)
                add_edge(library_id, "hasPattern", pid)
                if mc not in mat_ids:
                    label = MATERIAL_LABEL.get(mc, mc)
                    mat_ids[mc] = add_node(f"{PROJECT_KEY}:material:{mc}", "MaterialClass", label,
                                           f"재료 분류 {label} ({mc})", code=mc)
                    add_alias("material", label, mat_ids[mc])
                add_edge(pid, "hasMaterialClass", mat_ids[mc])
                pattern_rows.append({
                    "asset_id": pid, "pattern_name": p["pattern_name"],
                    "description": p["description"], "material_class": mc,
                    "material_label": MATERIAL_LABEL.get(mc, mc), "angles": angles,
                    "pattern_type": pattern_type(p), "definition_count": len(p["definition_lines"]),
                    "source_file": path.name, "relative_path": rel_str,
                    "content_sha256": digest, "geometry": "assets/archioffice_pattern_geometry.jsonl.gz",
                    "summary_text": summary,
                })
                geometry_rows.append({"asset_id": pid, "pattern_name": p["pattern_name"],
                                      "definition_lines": p["definition_lines"]})
                add_vector(pid, "hatch_pattern", summary, p["pattern_name"])
            file_rows[-1]["notes"] = f"patterns={len(pats)} parse_failures={len(fails)}"
            continue

        if ext == ".lin":
            text, _enc = read_text(path)
            for lt in parse_lin(text):
                lid = f"{PROJECT_KEY}:linetype:{slug(lt['linetype_name'])}"
                summary = (f"ZWCAD 선종류(linetype) '{lt['linetype_name']}'"
                           f"{', 설명: ' + lt['description'] if lt['description'] else ''}"
                           f", 요소 정의 {len(lt['element_lines'])}줄, 파일 {path.name}.")
                add_node(lid, "Linetype", lt["linetype_name"], summary,
                         description=lt["description"], element_count=len(lt["element_lines"]),
                         source_file=path.name, source_hash=digest)
                add_edge(library_id, "hasLinetype", lid)
                linetype_rows.append({"asset_id": lid, "linetype_name": lt["linetype_name"],
                                      "description": lt["description"],
                                      "element_lines": lt["element_lines"],
                                      "source_file": path.name, "relative_path": rel_str,
                                      "summary_text": summary})
                add_vector(lid, "linetype", summary, lt["linetype_name"])
            file_rows[-1]["notes"] = f"linetypes={len(linetype_rows)}"
            continue

        if ext == ".pgp":
            text, _enc = read_text(path)
            parsed = parse_pgp(text)
            for a in parsed["aliases"]:
                aid = f"{PROJECT_KEY}:alias:{slug(a['alias'])}-{slug(a['command'])}"
                summary = f"CAD 명령 단축키 {a['alias']} = {a['command']} (파일 {path.name})."
                add_node(aid, "CommandAlias", f"{a['alias']} → {a['command']}", summary,
                         alias=a["alias"], command=a["command"], source_file=path.name)
                add_edge(library_id, "hasCommandAlias", aid)
                command_rows.append({"asset_id": aid, "kind": "command_alias",
                                     "alias": a["alias"], "command": a["command"],
                                     "source_file": path.name, "summary_text": summary})
                add_vector(aid, "command_alias", summary, a["alias"])
            for e in parsed["external_commands"]:
                aid = f"{PROJECT_KEY}:extcmd:{slug(e['name'])}"
                summary = f"CAD 외부 명령 '{e['name']}' → OS 명령 '{e['os_command']}' (파일 {path.name})."
                add_node(aid, "ExternalCommand", e["name"], summary,
                         os_command=e["os_command"], source_file=path.name)
                add_edge(library_id, "hasExternalCommand", aid)
                command_rows.append({"asset_id": aid, "kind": "external_command",
                                     "alias": e["name"], "command": e["os_command"],
                                     "source_file": path.name, "summary_text": summary})
                add_vector(aid, "external_command", summary, e["name"])
            file_rows[-1]["notes"] = (f"aliases={len(parsed['aliases'])} "
                                      f"external={len(parsed['external_commands'])}")
            continue

        if ext == ".cfg":
            text, enc = read_text(path)
            parsed = parse_cfg(text)
            sid = f"{PROJECT_KEY}:setfile:{slug(rel_str.rsplit('.', 1)[0])}"
            spec_txt = "; ".join(f"{s['block_name']}={s['description']}" for s in parsed["block_specs"])
            summary = (f"ArchiOffice 심볼 배치 설정 '{path.name}' ({cls['category_label']}, "
                       f"{cls['subtype_label']}"
                       f"{', 대상 도면층 ' + parsed['layer'] if parsed['layer'] else ''}"
                       f"{', 블록 사양 ' + str(len(parsed['block_specs'])) + '건' if parsed['block_specs'] else ''}).")
            add_node(sid, "SymbolSetFile", path.stem, summary, layer=parsed["layer"],
                     section_count=len(parsed["sections"]), block_spec_count=len(parsed["block_specs"]),
                     encoding=enc, source_file=path.name, source_hash=digest)
            add_edge(f"{PROJECT_KEY}:category:{cls['category']}", "hasSetFile", sid)
            if parsed["layer"]:
                lay_id = f"{PROJECT_KEY}:layer:{slug(parsed['layer'])}"
                add_node(lay_id, "LayerStandard", parsed["layer"],
                         f"도면층 '{parsed['layer']}'", code=parsed["layer"], origin="symbol-cfg")
                add_edge(sid, "placedOnLayer", lay_id)
            for s in parsed["block_specs"]:
                bid = f"{PROJECT_KEY}:blockspec:{slug(s['block_name'])}"
                bsummary = (f"ArchiOffice 블록 사양 '{s['block_name']}' = {s['description']}"
                            f" (설정 파일 {path.name})"
                            + (f", 정원 {s['capacity_persons']}인승" if s["capacity_persons"] else "")
                            + (f", 치수 {s['dimensions']['w']}x{s['dimensions']['h']}mm"
                               if s["dimensions"] else "") + ".")
                add_node(bid, "BlockSpec", s["block_name"], bsummary,
                         description=s["description"], capacity_persons=s["capacity_persons"],
                         dimensions=s["dimensions"], source_file=path.name,
                         category=cls["category"])
                add_edge(sid, "hasBlockSpec", bid)
                block_spec_rows.append({"asset_id": bid, "block_name": s["block_name"],
                                        "description": s["description"],
                                        "capacity_persons": s["capacity_persons"],
                                        "dimensions": s["dimensions"], "category": cls["category"],
                                        "source_file": path.name, "summary_text": bsummary})
                add_vector(bid, "block_spec", bsummary, s["block_name"])
            cfg_rows.append({"asset_id": sid, "relative_path": rel_str, "category": cls["category"],
                             "layer": parsed["layer"], "sections": parsed["sections"],
                             "block_specs": parsed["block_specs"], "encoding": enc,
                             "content_sha256": digest, "summary_text": summary})
            add_vector(sid, "symbol_set_file", summary, path.stem)
            file_rows[-1]["notes"] = f"layer={parsed['layer']} block_specs={len(parsed['block_specs'])}"
            continue

        if ext == ".lsp":
            text, _enc = read_text(path)
            for fn in parse_lsp_defuns(text):
                fid = f"{PROJECT_KEY}:lispfn:{slug(path.stem)}-{slug(fn['function'])}"
                summary = (f"AutoLISP 함수 '{fn['function']}' (파일 {path.name}, {fn['line']}행).")
                add_node(fid, "LispFunction", fn["function"], summary,
                         source_file=path.name, line=fn["line"])
                add_edge(library_id, "hasLispFunction", fid)
                command_rows.append({"asset_id": fid, "kind": "lisp_function",
                                     "alias": fn["function"], "command": "", "source_file": path.name,
                                     "summary_text": summary})
                add_vector(fid, "lisp_function", summary, fn["function"])
            file_rows[-1]["notes"] = f"defuns={sum(1 for r in command_rows if r['source_file'] == path.name)}"
            continue

        if ext in TEXT_EXT:
            text, enc = read_text(path)
            sections = parse_ini_sections(text)
            if path.name == "_XPLayerSet.ini":
                for lay in parse_layers(text):
                    lid = f"{PROJECT_KEY}:layer:{slug(lay['layer'])}"
                    summary = (f"ArchiOffice 도면층 표준 '{lay['layer']}'"
                               f"{' - ' + lay['description'] if lay['description'] else ''}"
                               f"{', 색상 ' + str(lay['color']) if lay['color'] else ''}"
                               f"{', 선종류 ' + str(lay['linetype']) if lay['linetype'] else ''}"
                               f" (파일 {path.name}).")
                    add_node(lid, "LayerStandard", lay["layer"], summary,
                             description=lay["description"], color=lay["color"],
                             linetype=lay["linetype"], origin="layer-standard",
                             source_file=path.name)
                    add_edge(library_id, "hasLayer", lid)
                    layer_rows.append({"asset_id": lid, "layer": lay["layer"],
                                       "description": lay["description"], "color": lay["color"],
                                       "linetype": lay["linetype"], "role": None,
                                       "source_file": path.name, "summary_text": summary})
                    add_vector(lid, "layer_standard", summary, lay["layer"])
                file_rows[-1]["notes"] = f"layers={len(layer_rows)}"
                continue
            if path.name == "_XPLayerConfig.ini":
                mapping = dict(l.split("=", 1) for l in sections.get("LAYER_NAME", []) if "=" in l)
                descs = dict(l.split("=", 1) for l in sections.get("LAYER_DESC", []) if "=" in l)
                for role, layer in mapping.items():
                    rid = f"{PROJECT_KEY}:layerrole:{slug(role)}"
                    summary = (f"ArchiOffice 도면층 역할 매핑 '{role}' → '{layer.strip()}'"
                               f"{' (' + descs.get(role, '').strip() + ')' if descs.get(role, '').strip() else ''}.")
                    add_node(rid, "LayerRole", f"{role} → {layer.strip()}", summary,
                             role=role, layer=layer.strip(), source_file=path.name)
                    add_edge(rid, "mapsToLayer", f"{PROJECT_KEY}:layer:{slug(layer.strip())}")
                    add_edge(library_id, "hasLayerRole", rid)
                    layer_rows.append({"asset_id": rid, "layer": layer.strip(),
                                       "description": descs.get(role, "").strip(), "color": None,
                                       "linetype": None, "role": role, "source_file": path.name,
                                       "summary_text": summary})
                    add_vector(rid, "layer_role", summary, role)
                file_rows[-1]["notes"] = f"layer_roles={len(mapping)}"
                continue
            if path.name in ("_XPTextConfig.ini", "_SymbolSet.ini"):
                styles = {}
                for entry in sections.get("TEXT_STYLE", []):
                    k, _, v = entry.partition("=")
                    if k.strip():
                        styles[k.strip()] = v.strip()
                height_by_key = {}
                for entry in sections.get("TEXT_HEIGHT", []):
                    k, _, v = entry.partition("=")
                    if k.strip():
                        height_by_key[k.strip()] = v.strip()
                keys = set(styles) | set(height_by_key)
                for title, body in sections.items():
                    for entry in body:
                        k, _, _v = entry.partition("=")
                        if k.strip():
                            keys.add(k.strip())
                for key in sorted(keys):
                    tid = f"{PROJECT_KEY}:textstyle:{slug(key)}"
                    style = styles.get(key, "")
                    height = height_by_key.get(key, "")
                    summary = (f"ArchiOffice 문자 스타일/높이 '{key}'"
                               f"{', 스타일 ' + style if style else ''}"
                               f"{', 높이 ' + height if height else ''} (파일 {path.name}).")
                    add_node(tid, "TextStyle", key, summary, text_style=style,
                             text_height=height, source_file=path.name)
                    add_edge(library_id, "hasTextStyle", tid)
                    textstyle_rows.append({"asset_id": tid, "key": key, "text_style": style,
                                           "text_height": height, "source_file": path.name,
                                           "summary_text": summary})
                    add_vector(tid, "text_style", summary, key)
                file_rows[-1]["notes"] = f"text_keys={len(keys)}"
                continue

            # generic settings file
            flat = {k: v for k, v in sections.items()}
            plain = [l for l in text.splitlines() if l.strip() and not l.strip().startswith((";", "#"))]
            sid = f"{PROJECT_KEY}:setting:{slug(rel_str.rsplit('.', 1)[0])}"
            body = text.strip()
            summary = (f"ArchiOffice 설정/목록 파일 '{path.name}' ({cls['category_label']}, "
                       f"{st.st_size} bytes, {len(plain)}개 항목)"
                       + (f", 내용 일부: {' / '.join(plain[:8])}" if plain and len(plain) <= 40 else "")
                       + ".")
            add_node(sid, "ConfigSetting", path.stem, summary,
                     source_file=path.name, section_count=len(flat), item_count=len(plain),
                     encoding=enc, content_sha256=digest,
                     content_excerpt="\n".join(plain[:200])[:4000])
            add_edge(library_id, "hasSetting", sid)
            setting_rows.append({"asset_id": sid, "source_file": path.name,
                                 "relative_path": rel_str, "category": cls["category"],
                                 "section_count": len(flat), "item_count": len(plain),
                                 "items": plain[:500], "encoding": enc,
                                 "content_sha256": digest, "summary_text": summary})
            add_vector(sid, "config_setting", summary, path.stem)
            file_rows[-1]["notes"] = f"sections={len(flat)} items={len(plain)} encoding={enc}"
            continue

        # ---- binary symbol files (.dwg/.dwt/.slb/.bak/.exe)
        if ext in (".dwg", ".dwt", ".slb", ".bak"):
            ntype = "SymbolSetFile" if ext == ".slb" else "LibrarySymbol"
            nid = f"{PROJECT_KEY}:symbol:{slug(rel_str.rsplit('.', 1)[0])}"
            bits = [f"ArchiOffice {cls['category_label']} 심볼 '{stem}'",
                    cls["subtype_label"], f"파일 형식 {ext.lstrip('.').upper()}"]
            if cls["view_label"]:
                bits.append(cls["view_label"])
            if cls["code_prefix"]:
                bits.append(f"코드 {cls['code_prefix']}")
            if nominal:
                bits.append(f"공칭 치수 {nominal['w']}x{nominal['h']}mm")
            bits.append(f"파일 {path.name}, {st.st_size} bytes")
            summary = ", ".join(bits) + "."
            add_node(nid, "LibrarySymbol" if ext == ".dwg" else "SymbolSetFile", stem, summary,
                     category=cls["category"], category_label=cls["category_label"],
                     subtype=cls["subtype"], subtype_label=cls["subtype_label"],
                     view=cls["view"], view_label=cls["view_label"],
                     code_prefix=cls["code_prefix"], nominal_dimensions=nominal,
                     extension=ext, size_bytes=st.st_size, source_file=path.name,
                     relative_path=rel_str, content_sha256=digest)
            add_edge(f"{PROJECT_KEY}:category:{cls['category']}",
                     "hasSymbol" if ext == ".dwg" else "hasSetFile", nid)
            if cls["view"] in view_ids:
                add_edge(nid, "inView", view_ids[cls["view"]])
            if cls["subtype"] in {v[0] for v in HABIT_MAP.values()}:
                if cls["subtype"] not in habit_ids:
                    habit_ids[cls["subtype"]] = add_node(
                        f"{PROJECT_KEY}:habit:{cls['subtype']}", "PlantHabit",
                        cls["subtype_label"], f"수목 습성 {cls['subtype_label']}", code=cls["subtype"])
                    add_alias("habit", cls["subtype_label"], habit_ids[cls["subtype"]])
                add_edge(nid, "hasHabit", habit_ids[cls["subtype"]])
            add_vector(nid, "library_symbol", summary, stem)
            file_rows[-1]["notes"] = f"dwg={ext == '.dwg'}"
            continue

        # anything else (exe / unknown)
        nid = f"{PROJECT_KEY}:file:{slug(rel_str)}"
        add_node(nid, "ConfigFile", path.name,
                 f"ArchiOffice 지원 파일 '{path.name}' ({st.st_size} bytes).",
                 source_file=path.name, size_bytes=st.st_size, content_sha256=digest)
        add_edge(library_id, "hasFile", nid)

    # ---- duplicates ----------------------------------------------------------
    duplicate_groups = [{"content_sha256": h, "count": len(v), "files": v}
                        for h, v in hash_index.items() if len(v) > 1]
    for g in duplicate_groups:
        ids = []
        for rel_str in g["files"]:
            ids.append(f"{PROJECT_KEY}:symbol:{slug(rel_str.rsplit('.', 1)[0])}")
        ids = [i for i in ids if i in nodes]
        for i in range(1, len(ids)):
            add_edge(ids[i], "duplicateOf", ids[0], content_sha256=g["content_sha256"],
                     note="byte-identical source files")

    # ---- write ---------------------------------------------------------------
    def write_jsonl(path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    _dedup_cmd, _seen_cmd = [], set()
    for _r in command_rows:
        if _r["asset_id"] in _seen_cmd:
            continue
        _seen_cmd.add(_r["asset_id"])
        _dedup_cmd.append(_r)
    command_rows = _dedup_cmd

    write_jsonl(out / "assets" / "archioffice_files.jsonl", file_rows)
    write_jsonl(out / "assets" / "archioffice_patterns.jsonl", pattern_rows)
    import gzip
    with gzip.open(out / "assets" / "archioffice_pattern_geometry.jsonl.gz", "wt",
                   encoding="utf-8", newline="\n") as fh:
        for r in geometry_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_jsonl(out / "assets" / "archioffice_linetypes.jsonl", linetype_rows)
    write_jsonl(out / "assets" / "archioffice_symbol_configs.jsonl", cfg_rows)
    write_jsonl(out / "assets" / "archioffice_layers.jsonl", layer_rows)
    write_jsonl(out / "assets" / "archioffice_textstyles.jsonl", textstyle_rows)
    write_jsonl(out / "assets" / "archioffice_commands.jsonl", command_rows)
    write_jsonl(out / "assets" / "archioffice_block_specs.jsonl", block_spec_rows)
    write_jsonl(out / "assets" / "archioffice_settings.jsonl", setting_rows)

    write_jsonl(out / "graph" / "kg_nodes.jsonl", list(nodes.values()))
    write_jsonl(out / "graph" / "kg_edges.jsonl", edges)
    write_jsonl(out / "graph" / "kg_aliases.jsonl", aliases)
    write_jsonl(out / "vectors" / "vector_corpus.jsonl", vector_rows)

    jsonld = {
        "@context": {"aec": "https://aec.local/ontology#", "name": "aec:name",
                     "type": "@type", "edges": "aec:edges"},
        "@graph": [
            {"@id": n["id"], "@type": n["type"], "name": n["name"],
             "project_key": n["project_key"], "props": n["props"]}
            for n in nodes.values()
        ] + [
            {"@id": e["src"], "edges": [{"predicate": e["predicate"], "target": e["dst"]}]}
            for e in edges[:0]
        ],
    }
    (out / "graph" / "graph.jsonld").write_text(
        json.dumps(jsonld, ensure_ascii=False, indent=1), encoding="utf-8")

    write_jsonl(out / "graph" / "graph_edges_flat.jsonl", [
        {"subject": e["src"], "predicate": e["predicate"], "object": e["dst"],
         "project_key": e["project_key"], "weight": e["weight"], "evidence": e["evidence"]}
        for e in edges])

    manifest = {
        "pack": "archioffice",
        "project_key": PROJECT_KEY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source),
        "source_files": len(file_rows),
        "source_bytes": sum(r["size_bytes"] for r in file_rows),
        "counts": {
            "kg_nodes": len(nodes), "kg_edges": len(edges), "kg_aliases": len(aliases),
            "vector_rows": len(vector_rows), "patterns": len(pattern_rows),
            "linetypes": len(linetype_rows), "layers": len(layer_rows),
            "symbol_configs": len(cfg_rows), "block_specs": len(block_spec_rows),
            "text_styles": len(textstyle_rows), "commands": len(command_rows),
            "settings": len(setting_rows), "pattern_geometry": len(geometry_rows),
        },
        "node_types": sorted({n["type"] for n in nodes.values()}),
        "predicates": sorted({e["predicate"] for e in edges}),
        "embedding": {"model": EMBED_MODEL, "dim": EMBED_DIM, "storage": "aec.text_vectors/halfvec"},
        "pat_parse_failures": len(pat_failures),
        "duplicate_content_groups": len(duplicate_groups),
        "encoding": "utf-8 first, cp949 fallback (Korean CAD configs)",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out / "assets" / "extract_diagnostics.json").write_text(
        json.dumps({"pat_parse_failures": pat_failures[:50],
                    "duplicate_content_groups": duplicate_groups}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---- SQL ---------------------------------------------------------------
    def q(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    def arr(vs) -> str:
        return "ARRAY[" + ",".join(q(v) for v in vs) + "]::text[]"

    lines = [
        "-- ArchiOffice asset pack load script (idempotent).",
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
            "search_text=EXCLUDED.search_text, updated_at=now();"
        )
    for e in edges:
        lines.append(
            "INSERT INTO aec.kg_edges(src,predicate,dst,project_key,weight,evidence) "
            f"VALUES ({q(e['src'])},{q(e['predicate'])},{q(e['dst'])},{q(PROJECT_KEY)},{e['weight']},"
            f"{q(json.dumps(e['evidence'], ensure_ascii=False))}::jsonb) "
            "ON CONFLICT (src,predicate,dst) DO UPDATE SET weight=EXCLUDED.weight, evidence=EXCLUDED.evidence;"
        )
    for a in aliases:
        lines.append(
            "INSERT INTO aec.kg_aliases(alias_type,alias,node_id) "
            f"VALUES ({q(a['alias_type'])},{q(a['alias'])},{q(a['node_id'])}) "
            "ON CONFLICT (alias_type,alias,node_id) DO NOTHING;"
        )
    lines.append(f"INSERT INTO aec.kg_build_state(project_key,fingerprint,nodes,edges,built_at) "
                 f"VALUES ({q(PROJECT_KEY)},'archioffice-pack-v1',{len(nodes)},{len(edges)},now()) "
                 "ON CONFLICT (project_key) DO UPDATE SET fingerprint=EXCLUDED.fingerprint, "
                 "nodes=EXCLUDED.nodes, edges=EXCLUDED.edges, built_at=now();")
    lines += ["-- GraphRAG registration: communities.detect() and ask.link() key off a Project node 'kg:p:<key>'.", 'INSERT INTO aec.kg_nodes(id,project_key,type,name,props,object_ids,document_ids,search_text) VALUES (\'kg:p:ARCHIOFFICE\',\'ARCHIOFFICE\',\'Project\',\'ArchiOffice\',\'{"pack": true, "drawings": 0}\'::jsonb,\'{}\',\'{}\',\'ArchiOffice 도면 라이브러리 레이어 심볼 해치 선종 명령\') ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, props=EXCLUDED.props, search_text=EXCLUDED.search_text, updated_at=now();', "INSERT INTO aec.kg_edges(project_key,src,predicate,dst) VALUES ('ARCHIOFFICE','kg:p:ARCHIOFFICE','hasLibrary','ARCHIOFFICE:library') ON CONFLICT (src,predicate,dst) DO NOTHING;", "INSERT INTO aec.kg_aliases(alias_type,alias,node_id) VALUES ('project_id','ARCHIOFFICE','kg:p:ARCHIOFFICE'),('project_name','ARCHIOFFICE','kg:p:ARCHIOFFICE'),('project_name','ArchiOffice','kg:p:ARCHIOFFICE') ON CONFLICT (alias_type,alias,node_id) DO NOTHING;"]  # GraphRAG project registration (kg:p:<key>)
    lines.append("UPDATE aec.kg_nodes n SET object_ids = ARRAY[o.id], document_ids = ARRAY[o.document_id] FROM aec.objects o WHERE o.id = n.id AND n.project_key = 'ARCHIOFFICE';")  # link nodes to aec.objects (id == node id) for GraphRAG citations
    lines.append("COMMIT;")
    (out / "sql" / "archioffice_kg_load.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=1))
    print(f"nodes={len(nodes)} edges={len(edges)} aliases={len(aliases)} vectors={len(vector_rows)}")
    print(f"pat_failures={len(pat_failures)} dup_groups={len(duplicate_groups)}")
    print(f"out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

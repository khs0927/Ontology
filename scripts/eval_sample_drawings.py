#!/usr/bin/env python3
"""Field accuracy of the operational parser on a sample of real drawings (Phase 3 acceptance).

For every DXF/DWG in ``--drawings`` the parser output (``parse_source``) is scored against two
ground truths that never go through the code under test:

* the drawing itself read directly with ezdxf (``eval_extraction._truth_from_dxf``): every
  TEXT/MTEXT/ATTRIB string, every layer and every DIMENSION with its measurement;
* a hand-written truth file (``--truth``, JSON, kept next to the private drawings, never in git):
  ``{"<file name>": {"number": "A-012" | null, "number_re": "S1\\d\\d" (optional, normalised),
                     "category": "배치도" | null, "storey": "B2" | null}}``. These labels come from
  how the office named the sheet (file name / sheet list), not from the parser.

Fields per drawing (only applicable ones count):

* ``text``      - text recall >= 0.95
* ``layers``    - layer recall >= 0.98
* ``dimensions``- dimension recall >= 0.95 and measurements within 0.5 units
* ``number``    - a title-block drawing number on some View/TitleBlock matches the truth
* ``category``  - some View carries the truth drawing category
* ``storey``    - some View carries the truth storey

Accuracy = passed fields / applicable fields over all drawings (acceptance: >= 0.90).

Usage::

    python scripts/eval_sample_drawings.py --drawings D:\\AECData\\eval20_dxf --truth D:\\AECData\\eval\\eval20-truth.json \\
        --out D:\\AECData\\eval\\sample-accuracy.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import types
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

THRESHOLDS = {"text": 0.95, "layers": 0.98, "dimensions": 0.95}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _number_key(text: str) -> str:
    return re.sub(r"[\s\-_.]", "", str(text or "")).upper()


def parse(path: Path, settings_overrides: dict | None = None) -> dict:
    from aec_intelligence.operational.parsers import parse_source

    tmp = Path(tempfile.mkdtemp(prefix="eval-sample-"))
    try:
        settings = types.SimpleNamespace(data_root=tmp, oda_executable="", dwg_converter="auto",
                                         libredwg_executable="", oda_timeout_seconds=900,
                                         **(settings_overrides or {}))
        return parse_source(path, "eval", tmp / "out", settings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _recall(hit: int, expected: int) -> float:
    return 1.0 if expected == 0 else hit / expected


def score(path: Path, truth: dict | None) -> dict:
    from eval_extraction import _truth_from_dxf

    started = time.perf_counter()
    result = parse(path)
    seconds = time.perf_counter() - started
    raw = _truth_from_dxf(path)
    objects = result["objects"]
    by_handle = defaultdict(list)
    haystack = defaultdict(set)
    for o in objects:
        h = o.get("evidence", {}).get("handle")
        if not h:
            continue
        by_handle[h].append(o)
        p = o.get("properties", {})
        if p.get("text"):
            haystack[h].add(_norm(p["text"]))
        for v in (p.get("attributes") or {}).values():
            haystack[h].add(_norm(v))

    fields: dict[str, dict] = {}
    got_layers = {o["properties"].get("name") for o in objects if o["type"] == "Layer"}
    hit = sum(layer in got_layers for layer in raw["layers"])
    fields["layers"] = {"recall": _recall(hit, len(raw["layers"])), "expected": len(raw["layers"]),
                        "misses": sorted(set(raw["layers"]) - got_layers)[:10]}
    text_miss = [t for h, t in raw["texts"] if t not in haystack.get(h, ())]
    fields["text"] = {"recall": _recall(len(raw["texts"]) - len(text_miss), len(raw["texts"])),
                      "expected": len(raw["texts"]), "misses": text_miss[:10]}
    dim_hit = 0
    for handle, value in raw["dims"].items():
        dim = next((o for o in by_handle.get(handle, ()) if o["type"] == "Dimension"), None)
        got = (dim or {}).get("properties", {}).get("measurement")
        if dim is not None and (value is None or (got is not None and abs(float(got) - value) <= 0.5)):
            dim_hit += 1
    fields["dimensions"] = {"recall": _recall(dim_hit, len(raw["dims"])), "expected": len(raw["dims"])}
    for name, threshold in THRESHOLDS.items():
        fields[name]["pass"] = fields[name]["recall"] >= threshold

    views = [o for o in objects if o["type"] == "View"]
    titles = [o for o in objects if o["type"] == "TitleBlock"]
    numbers = {v["properties"].get("drawingNumber") for v in views} | {
        (t["properties"].get("attributes") or {}).get(k) for t in titles
        for k in (t["properties"].get("attributes") or {})
        if re.sub(r"[\s._-]", "", str(k)).upper() in {"DWGNO", "DRAWINGNO", "SHEETNO", "도면번호", "도번", "DNO"}}
    numbers = {n for n in numbers if n}
    categories = {v["properties"].get("drawing_category") for v in views}
    storeys = {v["properties"].get("storey") for v in views} - {None, ""}
    truth = truth or {}
    if truth.get("number") or truth.get("number_re"):
        if truth.get("number_re"):  # regex over the normalised number (no spaces/dashes, upper case)
            pattern = re.compile(truth["number_re"])
            ok = any(pattern.fullmatch(_number_key(n)) for n in numbers)
        else:
            ok = any(_number_key(truth["number"]) == _number_key(n) for n in numbers)
        fields["number"] = {"pass": ok, "expected": truth.get("number") or truth["number_re"], "got": sorted(numbers)[:8]}
    if truth.get("category"):
        fields["category"] = {"pass": truth["category"] in categories, "expected": truth["category"],
                              "got": sorted(c for c in categories if c)}
    if truth.get("storey"):
        fields["storey"] = {"pass": truth["storey"] in storeys, "expected": truth["storey"], "got": sorted(storeys)}
    return {"file": path.name, "seconds": round(seconds, 1), "objects": len(objects),
            "views": len(views), "fields": fields, "warnings": result.get("warnings", [])[:5]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--drawings", required=True, type=Path)
    ap.add_argument("--truth", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    truth = json.loads(args.truth.read_text(encoding="utf-8")) if args.truth else {}
    files = sorted(p for p in args.drawings.iterdir() if p.suffix.lower() in (".dxf", ".dwg"))
    if args.limit:
        files = files[: args.limit]
    rows, passed, applicable = [], 0, 0
    per_field = defaultdict(lambda: [0, 0])
    for path in files:
        key = next((k for k in truth if Path(k).stem == path.stem), None)
        try:
            row = score(path, truth.get(key) if key else None)
        except Exception as exc:  # a crash is a failed drawing, not a crashed evaluation
            row = {"file": path.name, "error": f"{type(exc).__name__}: {exc}", "fields": {}}
        rows.append(row)
        for name, f in row["fields"].items():
            per_field[name][0] += bool(f["pass"])
            per_field[name][1] += 1
            passed += bool(f["pass"])
            applicable += 1
        status = " ".join(f"{n}={'ok' if f['pass'] else 'FAIL'}" for n, f in row["fields"].items())
        print(f"{path.name[:40]:40} {row.get('seconds', '-')}s {row.get('error', status)}", flush=True)
    summary = {"drawings": len(rows), "errors": sum("error" in r for r in rows),
               "field_accuracy": round(passed / applicable, 4) if applicable else None,
               "passed": passed, "applicable": applicable,
               "per_field": {k: {"passed": v[0], "applicable": v[1]} for k, v in sorted(per_field.items())}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "files": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

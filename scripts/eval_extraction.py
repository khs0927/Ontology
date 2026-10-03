#!/usr/bin/env python3
"""Measure what the operational DXF ingest (``parse_source``) extracts against ground truth.

Ground truth comes from two places that never go through the code under test:

* the DXF file itself, read directly with ezdxf (layers, TEXT/MTEXT/ATTRIB
  strings, DIMENSION measurements), and
* ``labels.json`` written by ``scripts/make_ko_fixtures.py`` (room names, the
  room each area label belongs to, the sheet title that names the storey).

Items scored per file and in total:

* ``rooms``         - every labelled Space text becomes a Space with the same roomName
* ``room_area``     - that Space carries the area written next to it
* ``layers``        - every layer used by any entity (layouts and blocks) becomes a Layer
* ``text``          - every TEXT/MTEXT/ATTRIB string is present, decoded, in some object
* ``dimensions``    - every DIMENSION becomes a Dimension object
* ``dim_value``     - and carries its measurement (within 0.5 drawing units)
* ``storey``        - plan sheets name their storey on the model View (``storey`` property)

Usage::

    python scripts/eval_extraction.py out.json [--fixtures tests/fixtures/drawings_ko] [--extra a.dxf b.dxf]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import types
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "drawings_ko"
ITEMS = ("rooms", "room_area", "layers", "text", "dimensions", "dim_value", "storey")
STOREY_RE = re.compile(r"(지하\s*\d+\s*층|B\d+F?|\d+\s*층|옥탑|지붕)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _parse(path: Path) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from aec_intelligence.operational.parsers import parse_source

    tmp = Path(tempfile.mkdtemp(prefix="eval-extract-"))
    try:
        src = tmp / "src" / path.name
        src.parent.mkdir()
        shutil.copyfile(path, src)
        settings = types.SimpleNamespace(data_root=tmp, oda_executable="", dwg_converter="auto")
        return parse_source(src, "eval", tmp / "out", settings)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _truth_from_dxf(path: Path) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from aec_intelligence.dxf import decode_dxf_text, read_dxf

    doc, _ = read_dxf(path)
    layers, texts, dims = set(), [], {}
    for layout in doc.layouts:
        for e in layout:
            layers.add(decode_dxf_text(e.dxf.get("layer", "0")))
            kind = e.dxftype()
            if kind in ("TEXT", "MTEXT"):
                value = e.plain_text()
                texts.append((str(e.dxf.handle), _norm(decode_dxf_text(value))))
            elif kind == "INSERT":
                for a in e.attribs:
                    if str(a.dxf.text).strip():
                        texts.append((str(e.dxf.handle), _norm(decode_dxf_text(a.dxf.text))))
            elif kind == "DIMENSION":
                try:
                    dims[str(e.dxf.handle)] = float(e.get_measurement())
                except Exception:
                    dims[str(e.dxf.handle)] = None
    for block in doc.blocks:
        if block.is_any_layout:
            continue
        for e in block:
            layers.add(decode_dxf_text(e.dxf.get("layer", "0")))
    return {"layers": layers, "texts": [t for t in texts if t[1]], "dims": dims}


def score_file(path: Path, golden: dict | None) -> dict:
    result = _parse(path)
    truth = _truth_from_dxf(path)
    objects = result["objects"]
    by_handle = defaultdict(list)
    for o in objects:
        h = o.get("evidence", {}).get("handle")
        if h:
            by_handle[h].append(o)
    out = {item: {"expected": 0, "hit": 0, "misses": []} for item in ITEMS}

    def tally(item, ok, miss):
        out[item]["expected"] += 1
        if ok:
            out[item]["hit"] += 1
        elif len(out[item]["misses"]) < 15:
            out[item]["misses"].append(miss)

    # layers
    got_layers = {o["properties"].get("name") for o in objects if o["type"] == "Layer"}
    for layer in sorted(truth["layers"]):
        tally("layers", layer in got_layers, layer)

    # text: the decoded string must appear in some object's text / attribute values
    haystack = defaultdict(set)
    for o in objects:
        p = o.get("properties", {})
        h = o.get("evidence", {}).get("handle")
        if p.get("text"):
            haystack[h].add(_norm(p["text"]))
        for v in (p.get("attributes") or {}).values():
            haystack[h].add(_norm(v))
    for handle, text in truth["texts"]:
        tally("text", text in haystack.get(handle, ()), {"handle": handle, "text": text})

    # dimensions
    for handle, value in truth["dims"].items():
        dim = next((o for o in by_handle.get(handle, ()) if o["type"] == "Dimension"), None)
        tally("dimensions", dim is not None, handle)
        got = (dim or {}).get("properties", {}).get("measurement")
        ok = value is not None and got is not None and abs(float(got) - value) <= 0.5
        tally("dim_value", ok, {"handle": handle, "expected": value, "got": got})

    if golden:
        areas = {e["space"]: float(re.sub(r"[^\d.]", "", e["text"]))
                 for e in golden["entities"] if e.get("role") == "area" and e.get("space")}
        for e in golden["entities"]:
            if e["class"] != "Space" or "space" not in e:
                continue
            space = next((o for o in by_handle.get(e["handle"], ()) if o["type"] == "Space"), None)
            name = (space or {}).get("properties", {}).get("roomName")
            tally("rooms", name == e["space"], {"handle": e["handle"], "expected": e["space"], "got": name})
            if e["space"] in areas:
                got = (space or {}).get("properties", {}).get("area")
                tally("room_area", got is not None and abs(got - areas[e["space"]]) < 0.05,
                      {"room": e["space"], "expected": areas[e["space"]], "got": got})
        sheet = golden["sheet"]
        found = STOREY_RE.search(sheet.get("title", "")) if sheet.get("category") == "plan" else None
        if found:
            expected = _norm(found.group(1)).replace(" ", "")
            views = [o for o in objects if o["type"] == "View" and o["properties"].get("layout_kind") == "model"]
            got = views[0]["properties"].get("storey") if views else None
            tally("storey", got == expected, {"expected": expected, "got": got})
    return {"file": path.name, "items": out, "metrics": result.get("metrics", {})}


def summarize(files: list[dict]) -> dict:
    total = {item: {"expected": 0, "hit": 0} for item in ITEMS}
    for f in files:
        for item, v in f["items"].items():
            total[item]["expected"] += v["expected"]
            total[item]["hit"] += v["hit"]
    for v in total.values():
        v["accuracy"] = round(v["hit"] / v["expected"], 4) if v["expected"] else None
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out")
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--extra", type=Path, nargs="*", default=[])
    args = ap.parse_args(argv)
    labels = json.loads((args.fixtures / "labels.json").read_text(encoding="utf-8"))
    files = [score_file(args.fixtures / name, golden) for name, golden in sorted(labels["files"].items())]
    files += [score_file(p, None) for p in args.extra]
    report = {"total": summarize(files), "files": files}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for item, v in report["total"].items():
        acc = "n/a" if v["accuracy"] is None else f"{v['accuracy']:.1%}"
        print(f"{item:11s} {v['hit']:4d}/{v['expected']:<4d} {acc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

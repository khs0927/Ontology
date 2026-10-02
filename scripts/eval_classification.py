#!/usr/bin/env python3
"""Score the repository's DXF parse + classification path against golden labels.

Runs the same path ``aec ingest-dxf`` uses (``aec_intelligence.dxf.DXFParser``
followed by ``aec_intelligence.classifier.to_cair_object``) on every fixture
listed in ``labels.json`` (written by ``scripts/make_ko_fixtures.py``) and
reports:

* per-class precision / recall / F1 for model-space entities,
* a confusion list (expected -> predicted, with example handles),
* sheet metadata accuracy (number / title / category), when the parser exposes it,
* block-definition category accuracy, when the parser exposes it.

The harness never crashes on a missing parser feature: anything it cannot find
in the parser output is counted as *unclassified* (``None``). Adjust
:func:`map_prediction` (one small function) when the parser output changes.

Usage::

    python scripts/eval_classification.py out.json \
        [--fixtures tests/fixtures/drawings_ko] [--labels .../labels.json] [--src src] [--regenerate]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "drawings_ko"
UNCLASSIFIED = None
UNCLASSIFIED_NAME = "(unclassified)"


# --------------------------------------------------------------------------- mapping
# The ONE place that translates parser output into evaluation classes.
# ``record`` is a CAIR object dict (``CAIRObject.to_dict()``) or, if the parser
# could not produce one, the normalized entity dict. Return a class name from
# labels.json ``entity_classes`` or None for "unclassified".

PARSER_LABEL_ALIASES: dict[str, str | None] = {
    "CADEntity": None,                      # current fallback label = no decision
    "Slab": "BuildingElementProxy",         # labels have no Slab class
    "Stair": "BuildingElementProxy",
    "Railing": "BuildingElementProxy",
    "Fastener": "BuildingElementProxy",     # bolts
    "Opening": None,
    "Room": "Space",
    "Text": "Annotation",
    "Symbol": "Annotation",
    "Callout": "Annotation",
    "NorthArrow": "Annotation",
    "SheetTitle": "TitleBlock",
    "FurnishingElement": "Furniture",
    "SanitaryTerminal": "Furniture",
    "Equipment": "Furniture",
    "Member": "BuildingElementProxy",
}


def map_prediction(record: dict[str, Any] | None) -> str | None:
    if not record:
        return None
    props = record.get("properties") or {}
    classification = record.get("classification") or {}
    label = (
        props.get("semantic_class")              # anticipated richer parser field
        or classification.get("label")
        or record.get("type")
    )
    if not label:
        return None
    label = str(label)
    if label.startswith("Ifc"):
        label = label[3:]
    return PARSER_LABEL_ALIASES.get(label, label)


# --------------------------------------------------------------------------- parser glue

def _import_parser(src: Path | None):
    if src and src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from aec_intelligence.dxf import DXFParser  # noqa: WPS433 (runtime import on purpose)
    try:
        from aec_intelligence.classifier import to_cair_object
    except ImportError:
        to_cair_object = None
    return DXFParser, to_cair_object


def _as_dict(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return value


def run_parser(path: Path, DXFParser, to_cair_object) -> tuple[dict[str, dict[str, Any]], Any, list[str]]:
    """Return (records by handle, raw parse result, errors)."""
    errors: list[str] = []
    try:
        result = DXFParser().parse(path)
    except Exception as exc:
        return {}, None, [f"parse failed: {exc!r}"]
    records: dict[str, dict[str, Any]] = {}
    for entity in getattr(result, "entities", None) or []:
        handle = str(getattr(entity, "handle", "") or (_as_dict(entity) or {}).get("handle", ""))
        record: dict[str, Any] | None = None
        if to_cair_object is not None:
            try:
                record = to_cair_object(entity, "eval", str(path), "0" * 64).to_dict()
            except Exception as exc:
                errors.append(f"{handle}: to_cair_object failed: {exc!r}")
        if record is None:
            record = _as_dict(entity) or {}
        records[handle] = record
    return records, result, errors


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def extract_sheet(result: Any) -> dict[str, Any] | None:
    """Find sheet metadata on the parse result, whatever the attribute ends up being called."""
    if result is None:
        return None
    candidates: list[Any] = []
    for attr in ("sheet", "sheet_info", "sheet_metadata", "title_block", "drawing", "metadata"):
        candidates.append(getattr(result, attr, None))
    as_dict = _as_dict(result)
    if isinstance(as_dict, dict):
        for key in ("sheet", "sheet_info", "sheet_metadata", "title_block", "drawing", "metadata"):
            candidates.append(as_dict.get(key))
        sheets = as_dict.get("sheets")
        if isinstance(sheets, list) and sheets:
            candidates.append(sheets[0])
    for cand in candidates:
        cand = _as_dict(cand)
        if not isinstance(cand, dict):
            continue
        sheet = {
            "number": _first(cand, "number", "sheet_number", "drawing_number", "도면번호"),
            "title": _first(cand, "title", "sheet_title", "drawing_title", "name", "도면명"),
            "category": _first(cand, "category", "sheet_category", "drawing_type", "kind"),
        }
        if any(sheet.values()):
            return sheet
    return None


def extract_block_categories(result: Any) -> dict[str, str | None]:
    """Map block name -> category from whatever block catalogue the parser exposes."""
    if result is None:
        return {}
    found: dict[str, str | None] = {}
    sources: list[Any] = [getattr(result, a, None) for a in ("block_definitions", "block_catalog", "blocks")]
    as_dict = _as_dict(result)
    if isinstance(as_dict, dict):
        sources += [as_dict.get(k) for k in ("block_definitions", "block_catalog", "blocks")]
    for src in sources:
        if isinstance(src, dict):
            items = [{"name": k, **(v if isinstance(v, dict) else {"category": v})} for k, v in src.items()]
        elif isinstance(src, (list, tuple)):
            items = [_as_dict(v) for v in src]
        else:
            continue
        for item in items:
            if isinstance(item, str):
                found.setdefault(item, None)
            elif isinstance(item, dict) and item.get("name"):
                cat = _first(item, "category", "semantic_class", "class", "label", "type")
                if cat is not None:
                    cat = PARSER_LABEL_ALIASES.get(str(cat), str(cat))
                if found.get(item["name"]) is None:
                    found[item["name"]] = cat
    return found


# --------------------------------------------------------------------------- metrics

def prf(pairs: list[tuple[str | None, str | None]], classes: list[str]) -> dict[str, Any]:
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()
    for expected, predicted in pairs:
        if expected == predicted:
            tp[expected] += 1
        else:
            fn[expected] += 1
            if predicted is not None:
                fp[predicted] += 1
    out: dict[str, Any] = {}
    seen = list(classes) + sorted({p for _, p in pairs if p is not None and p not in classes})
    for cls in seen:
        support = tp[cls] + fn[cls]
        predicted_n = tp[cls] + fp[cls]
        if support == 0 and predicted_n == 0:
            continue
        p = tp[cls] / predicted_n if predicted_n else 0.0
        r = tp[cls] / support if support else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        out[cls] = {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4),
                    "support": support, "predicted": predicted_n, "tp": tp[cls]}
    return out


def evaluate(labels: dict[str, Any], fixtures: Path, src: Path | None) -> dict[str, Any]:
    classes = list(labels.get("entity_classes", []))
    report: dict[str, Any] = {"fixtures": str(fixtures), "parser": {}, "files": {}}
    try:
        DXFParser, to_cair_object = _import_parser(src)
        report["parser"] = {"module": DXFParser.__module__, "name": getattr(DXFParser, "name", "?"),
                            "version": str(getattr(DXFParser, "version", "?")),
                            "classifier": getattr(to_cair_object, "__module__", None)}
    except Exception as exc:
        DXFParser = to_cair_object = None
        report["parser"] = {"error": f"import failed: {exc!r}"}

    pairs: list[tuple[str | None, str | None]] = []
    examples: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)
    block_pairs: list[tuple[str | None, str | None]] = []
    sheet_scores = {"number": 0, "title": 0, "category": 0, "files": 0, "exposed": 0}
    sheet_category_pairs: list[tuple[str | None, str | None]] = []

    for filename, golden in labels["files"].items():
        path = fixtures / filename
        if DXFParser is None:
            records, result, errors = {}, None, ["parser unavailable"]
        else:
            try:
                records, result, errors = run_parser(path, DXFParser, to_cair_object)
            except Exception as exc:  # last-resort guard; never abort the whole run
                records, result, errors = {}, None, [f"{exc!r}", traceback.format_exc(limit=2)]
        file_pairs = []
        missing = 0
        for ent in golden["entities"]:
            if ent.get("score") is False:
                continue
            record = records.get(ent["handle"])
            if record is None:
                missing += 1
            try:
                predicted = map_prediction(record)
            except Exception:
                predicted = None
            pair = (ent["class"], predicted)
            pairs.append(pair)
            file_pairs.append(pair)
            if ent["class"] != predicted and len(examples[pair]) < 6:
                hint = ent.get("block") or ent.get("text") or ent["layer"]
                examples[pair].append(f"{filename}#{ent['handle']} {ent['entity_type']} [{ent['layer']}] {hint}")

        # sheet metadata
        sheet_scores["files"] += 1
        got = extract_sheet(result)
        want = golden["sheet"]
        sheet_eval: dict[str, Any] = {"expected": {k: want[k] for k in ("number", "title", "category")}, "predicted": got}
        if got:
            sheet_scores["exposed"] += 1
            for key in ("number", "title", "category"):
                ok = str(got.get(key) or "").replace(" ", "") == str(want[key]).replace(" ", "")
                sheet_scores[key] += ok
        sheet_category_pairs.append((want["category"], (got or {}).get("category")))

        # block definitions
        block_map = extract_block_categories(result)
        block_rows = []
        for blk in golden["blocks"]:
            if blk.get("implicit"):
                continue
            predicted = block_map.get(blk["name"])
            block_pairs.append((blk["category"], predicted))
            block_rows.append({"name": blk["name"], "expected": blk["category"], "predicted": predicted})

        correct = sum(e == p for e, p in file_pairs)
        report["files"][filename] = {
            "sheet": sheet_eval,
            "entities": len(file_pairs),
            "correct": correct,
            "accuracy": round(correct / len(file_pairs), 4) if file_pairs else None,
            "missing_from_parser_output": missing,
            "blocks": block_rows,
            "errors": errors[:20],
        }

    total = len(pairs)
    correct = sum(e == p for e, p in pairs)
    per_class = prf(pairs, classes)
    scored = [c for c in classes if c in per_class and per_class[c]["support"]]
    confusion = [
        {"expected": e, "predicted": p if p is not None else UNCLASSIFIED_NAME, "count": n, "examples": examples.get((e, p), [])}
        for (e, p), n in sorted(Counter(pairs).items(), key=lambda kv: (-kv[1], str(kv[0])))
        if e != p
    ]
    block_total = len(block_pairs)
    report["entities"] = {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else None,
        "unclassified": sum(p is None for _, p in pairs),
        "macro_f1": round(sum(per_class[c]["f1"] for c in scored) / len(scored), 4) if scored else None,
        "per_class": per_class,
    }
    report["confusion"] = confusion
    report["sheets"] = {
        "files": sheet_scores["files"],
        "metadata_exposed": sheet_scores["exposed"],
        "number_accuracy": round(sheet_scores["number"] / sheet_scores["files"], 4),
        "title_accuracy": round(sheet_scores["title"] / sheet_scores["files"], 4),
        "category_accuracy": round(sheet_scores["category"] / sheet_scores["files"], 4),
        "category_per_class": prf(sheet_category_pairs, list(labels.get("sheet_categories", []))),
    }
    report["blocks"] = {
        "total": block_total,
        "correct": sum(e == p for e, p in block_pairs),
        "unclassified": sum(p is None for _, p in block_pairs),
        "accuracy": round(sum(e == p for e, p in block_pairs) / block_total, 4) if block_total else None,
        "per_class": prf(block_pairs, classes + ["Xref"]),
    }
    return report


def print_report(report: dict[str, Any]) -> None:
    ent = report["entities"]
    print(f"parser: {report['parser']}")
    print(f"\nENTITIES  total={ent['total']}  accuracy={ent['accuracy']}  macro-F1={ent['macro_f1']}  unclassified={ent['unclassified']}")
    print(f"{'class':<22}{'prec':>7}{'recall':>8}{'f1':>7}{'support':>9}{'pred':>6}")
    for cls, m in ent["per_class"].items():
        print(f"{cls:<22}{m['precision']:>7.2f}{m['recall']:>8.2f}{m['f1']:>7.2f}{m['support']:>9}{m['predicted']:>6}")
    print("\nCONFUSION (expected -> predicted: count)")
    for row in report["confusion"]:
        print(f"  {row['expected']:<20} -> {row['predicted']:<20} {row['count']:>4}   e.g. {row['examples'][0] if row['examples'] else ''}")
    sh = report["sheets"]
    print(f"\nSHEETS  exposed={sh['metadata_exposed']}/{sh['files']}  number={sh['number_accuracy']}  title={sh['title_accuracy']}  category={sh['category_accuracy']}")
    bl = report["blocks"]
    print(f"BLOCK DEFINITIONS  total={bl['total']}  accuracy={bl['accuracy']}  unclassified={bl['unclassified']}")
    print("\nPER FILE")
    for name, f in report["files"].items():
        err = f"  errors={len(f['errors'])}" if f["errors"] else ""
        print(f"  {name:<34} {f['correct']:>3}/{f['entities']:<3} acc={f['accuracy']}  missing={f['missing_from_parser_output']}{err}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate DXF semantic classification against golden labels.")
    ap.add_argument("out", type=Path, help="where to write the JSON report")
    ap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    ap.add_argument("--labels", type=Path, default=None, help="default: <fixtures>/labels.json")
    ap.add_argument("--src", type=Path, default=ROOT / "src", help="directory containing aec_intelligence")
    ap.add_argument("--regenerate", action="store_true", help="rebuild fixtures with make_ko_fixtures.py first")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.regenerate:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from make_ko_fixtures import generate
        generate(args.fixtures)
    labels_path = args.labels or args.fixtures / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    report = evaluate(labels, args.fixtures, args.src)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print_report(report)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

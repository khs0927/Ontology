"""Heuristic drawing semantics for vector PDF pages (CAD plot exports).

Everything here is derived from the PDF text layer and vector paths, so all building-class results are
candidates (AI_INFERRED) carrying a Classification with confidence, mirroring the DXF classifier semantics.
Coordinates stay in unrotated PDF points (the same space as Page/Annotation bboxes); only title-block
label/value reading uses the page's visual (derotated) orientation.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from ..cair import Classification
from ..classifier import _TITLE_LOOKUP, _state, title_block_fields

_NORM = re.compile(r"[\s._\-:'()\[\]/]")
DIMENSION_RE = re.compile(r"^(\d{1,3}(,\d{3})+|\d{3,6})$")
GRID_LABEL_RE = re.compile(r"^([A-Z]{1,2}|[A-Z]?\d{1,2}|[XY]\d{1,2})'?$")
SCALE_RE = re.compile(r"1\s*/\s*(\d{1,5})")
WALL_GAP = (1.5, 15.0)  # PDF points between the two faces of a wall candidate
WALL_MIN_LENGTH = 30.0
MAX_WALLS_PER_PAGE = 2000
# Caption words printed in title-block cells; never a field value.
CAPTION_RE = re.compile(r"^(?:(?:DRAWN|DRAWING|CHECKED|CHECKD|APPROVED|APPD|APPROVAL|DESIGNED|BY|NO|NUMBER|SHEET|SIZE|"
                        r"DESCRIPTION|OF|REV|REVISIONS?|ISSUE|NOTE|TITLE|NAME|DATE|SCALE|CLIENT|OWNER|PROJECT|"
                        r"ARCHITECTURE|ARCHITECTURAL|STRUCTURE|STRUCTURAL|MECHANICAL|ELECTRICAL|A\d)+|일련번호|"
                        r"제도|심사|승인|일자|건축설계|구조설계|설비설계|전기설계|특기사항)$")
FIELD_VALID = {
    "date": re.compile(r"\d{2,4}\s*[./-]\s*\d{1,2}|\d{4}"),
    "scale": re.compile(r"1\s*/\s*\d+|N\.?\s*T\.?\s*S|NONE|^\s*-\s*$", re.IGNORECASE),
    "drawingNumber": re.compile(r"^(?=.*\d)[A-Za-z가-힣]{0,6}\s*[-_.]?\s*[A-Za-z]{0,3}\s*[-_.]?\s*\d{1,4}[A-Za-z]?$"),
}


def _norm(text: str) -> str:
    return _NORM.sub("", text).upper()


def _cls(label: str, confidence: float, method: str, *evidence: str) -> dict[str, Any]:
    return Classification(label, round(confidence, 4), method, tuple(evidence), _state(confidence)).to_dict()


def _bbox(r) -> dict[str, float]:
    return {"min_x": float(r[0]), "min_y": float(r[1]), "max_x": float(r[2]), "max_y": float(r[3])}


# ---------------------------------------------------------------- text lines
def text_lines(page) -> list[dict[str, Any]]:
    """Words grouped into PDF text lines, with unrotated and visual (derotated) rectangles."""
    import pymupdf
    groups: dict[tuple[int, int], list] = defaultdict(list)
    for w in page.get_text("words"):
        if str(w[4]).strip():
            groups[(w[5], w[6])].append(w)
    matrix = page.rotation_matrix
    lines = []
    for words in groups.values():
        words.sort(key=lambda w: w[7])
        rect = pymupdf.Rect(min(w[0] for w in words), min(w[1] for w in words),
                            max(w[2] for w in words), max(w[3] for w in words))
        text = " ".join(str(w[4]) for w in words)
        lines.append({"text": text, "norm": _norm(text), "rect": rect, "visual": rect * matrix,
                      "words": words})
    return lines


# ---------------------------------------------------------------- title block
def _row_text(start: dict[str, Any], values) -> str:
    """Value text plus fragments continuing it on the same visual row (e.g. 'S' '-' '101' split into lines)."""
    parts, current = [start], start["visual"]
    h = max(current.height, 1.0)
    for _ in range(6):
        nxt = None
        for _, v in values:
            V = v["visual"]
            if v in parts or abs((V.y0 + V.y1 - current.y0 - current.y1) / 2) > h * 0.6:
                continue
            gap = V.x0 - current.x1
            if -h * 0.3 <= gap <= h * 1.5 and (nxt is None or gap < nxt[0]):
                nxt = (gap, v)
        if not nxt:
            break
        parts.append(nxt[1])
        current = nxt[1]["visual"]
    left = []
    first = start["visual"]
    for _, v in values:  # one leading fragment (prefix such as 'S' or 'S -')
        V = v["visual"]
        if v not in parts and abs((V.y0 + V.y1 - first.y0 - first.y1) / 2) <= h * 0.6 and 0 <= first.x0 - V.x1 <= h * 1.5 \
                and len(v["norm"]) <= 3:
            left.append(v)
    return " ".join(p["text"].strip() for p in [*left[:1], *parts])


def title_block(lines: list[dict[str, Any]], max_distance: float = 120.0) -> dict[str, Any] | None:
    """Pair title-block labels (DXF TITLE_BLOCK_KEYS vocabulary) with the nearest value text right of / below them."""
    labels, values = [], []
    for line in lines:
        field = _TITLE_LOOKUP.get(line["norm"])
        (labels if field else values).append((field, line))
    values = [(f, v) for f, v in values if re.search(r"[0-9A-Za-z가-힣]", v["text"]) and not CAPTION_RE.match(v["norm"])]
    found: dict[str, tuple[float, str, dict, dict]] = {}
    for field, label in labels:
        L = label["visual"]
        lh = max(L.height, 1.0)
        best = None
        valid = FIELD_VALID.get(field)
        for _, value in values:
            if valid and not valid.search(value["text"]):
                continue
            V = value["visual"]
            vh = max(V.height, 1.0)
            dy = (V.y0 + V.y1 - L.y0 - L.y1) / 2
            if V.x0 >= L.x1 - lh * 0.5 and abs(dy) < max(lh, vh) * 0.8:
                dist = V.x0 - L.x1  # same row, to the right
            elif V.x0 >= L.x1 - lh * 0.5 and 0 < dy <= 4 * max(lh, vh):
                dist = V.x0 - L.x1 + 2 * dy  # right and slightly lower (merged value cells)
            elif V.y0 >= L.y1 - lh * 0.3 and V.x0 < L.x1 + lh and V.x1 > L.x0 - lh:
                dist = (V.y0 - L.y1) * 1.5  # below, column-aligned (slightly penalised)
            else:
                continue
            dist = max(dist, 0.0)
            if dist <= max_distance and (best is None or dist < best[0]):
                best = (dist, value)
        if best and (field not in found or best[0] < found[field][0]):
            found[field] = (best[0], _row_text(best[1], values), label, best[1])
    if not found:
        return None
    attributes = {field: item[1] for field, item in found.items()}
    fields = title_block_fields(attributes)
    if not fields:
        return None
    rects = [r for item in found.values() for r in (item[2]["rect"], item[3]["rect"])]
    region = (min(r.x0 for r in rects), min(r.y0 for r in rects), max(r.x1 for r in rects), max(r.y1 for r in rects))
    evidence = {field: {"label": item[2]["text"], "value": item[1], "distance_pt": round(item[0], 2)}
                for field, item in found.items()}
    return {"fields": fields, "bbox": region, "evidence": evidence}


def scale_denominator(scale: str | None) -> int | None:
    match = SCALE_RE.search(scale or "")
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


# ---------------------------------------------------------------- vectors
def segments(drawings) -> list[tuple[float, float, float, float]]:
    out = []
    for path in drawings:
        for item in path.get("items", ()):
            if item[0] == "l":
                p, q = item[1], item[2]
                if abs(p.x - q.x) + abs(p.y - q.y) > 0.5:
                    out.append((p.x, p.y, q.x, q.y))
    return out


def _inside(region, x, y, margin=0.0) -> bool:
    return bool(region) and region[0] - margin <= x <= region[2] + margin and region[1] - margin <= y <= region[3] + margin


class _Index:
    def __init__(self, segs, cell=40.0):
        self.cell, self.grid, self.segs = cell, defaultdict(list), segs
        for i, (x0, y0, x1, y1) in enumerate(segs):
            for cx in range(int(min(x0, x1) // cell), int(max(x0, x1) // cell) + 1):
                for cy in range(int(min(y0, y1) // cell), int(max(y0, y1) // cell) + 1):
                    self.grid[(cx, cy)].append(i)

    def near(self, rect, pad):
        c = self.cell
        seen = set()
        for cx in range(int((rect[0] - pad) // c), int((rect[2] + pad) // c) + 1):
            for cy in range(int((rect[1] - pad) // c), int((rect[3] + pad) // c) + 1):
                for i in self.grid.get((cx, cy), ()):
                    if i not in seen:
                        seen.add(i)
                        yield i, self.segs[i]


def dimensions(lines, index, exclude=None):
    """Numeric texts with a parallel line (dimension line) just beside them."""
    found = []
    for line in lines:
        for w in line["words"]:
            text = str(w[4]).strip()
            if not DIMENSION_RE.match(text):
                continue
            x0, y0, x1, y1 = w[:4]
            if _inside(exclude, (x0 + x1) / 2, (y0 + y1) / 2):
                continue
            horizontal = (x1 - x0) >= (y1 - y0)
            size = (y1 - y0) if horizontal else (x1 - x0)
            along = (x1 - x0) if horizontal else (y1 - y0)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            best = None
            for _, (a, b, c, d) in index.near((x0, y0, x1, y1), size * 1.5):
                if horizontal and abs(b - d) < 0.3 and min(a, c) <= cx <= max(a, c) and abs(c - a) >= along:
                    gap = min(abs(b - y0), abs(b - y1))
                elif not horizontal and abs(a - c) < 0.3 and min(b, d) <= cy <= max(b, d) and abs(d - b) >= along:
                    gap = min(abs(a - x0), abs(a - x1))
                else:
                    continue
                if gap <= size * 1.5 and (best is None or gap < best[0]):
                    best = (gap, (a, b, c, d))
            if best:
                found.append({"text": text, "value": float(text.replace(",", "")), "bbox": (x0, y0, x1, y1),
                              "dimension_line": [round(v, 2) for v in best[1]],
                              "orientation": "horizontal" if horizontal else "vertical"})
    return found


def grid_bubbles(drawings, lines, segs, page_size):
    """Circles (all-Bezier closed paths) holding a short axis label; grid line = long segment ending at the circle."""
    words = [w for line in lines for w in line["words"]]
    long_len = 0.2 * min(page_size)
    longs = [s for s in segs if math.hypot(s[2] - s[0], s[3] - s[1]) >= long_len]
    grids: dict[str, dict[str, Any]] = {}
    for path in drawings:
        items = path.get("items", ())
        if len(items) < 2 or any(it[0] != "c" for it in items):
            continue
        r = path["rect"]
        if not (5 <= r.width <= 40 and 0.85 <= r.width / max(r.height, 1e-6) <= 1.15):
            continue
        inside = [str(w[4]).strip() for w in words
                  if r.x0 <= (w[0] + w[2]) / 2 <= r.x1 and r.y0 <= (w[1] + w[3]) / 2 <= r.y1]
        label = "".join(inside)
        if not inside or not GRID_LABEL_RE.match(label):
            continue
        cx, cy, rad = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, r.width / 2
        line = None
        for s in longs:
            for ex, ey in ((s[0], s[1]), (s[2], s[3])):
                if math.hypot(ex - cx, ey - cy) <= rad * 2.2:
                    line = s
                    break
            if line:
                break
        entry = grids.setdefault(label, {"label": label, "bubbles": [], "lines": []})
        entry["bubbles"].append([round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1)])
        if line:
            entry["lines"].append([round(v, 2) for v in line])
    return list(grids.values())


def wall_pairs(segs, exclude=None, gap_range=WALL_GAP, min_length=WALL_MIN_LENGTH):
    """Mutually-nearest parallel long segments whose outer neighbours are clearly farther (rejects hatching/tables)."""
    buckets: dict[int, list] = defaultdict(list)
    for s in segs:
        x0, y0, x1, y1 = s
        length = math.hypot(x1 - x0, y1 - y0)
        if length < min_length or _inside(exclude, (x0 + x1) / 2, (y0 + y1) / 2, 5):
            continue
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180
        key = int(round(angle * 2)) % 360
        theta = math.radians(key / 2)
        ux, uy = math.cos(theta), math.sin(theta)
        t0, t1 = sorted((x0 * ux + y0 * uy, x1 * ux + y1 * uy))
        offset = -x0 * uy + y0 * ux
        buckets[key].append((offset, t0, t1, s))
    max_gap = gap_range[1] * 2
    pairs = []
    for key, rows in buckets.items():
        rows.sort()
        n = len(rows)
        below = [None] * n  # (gap, j) nearest overlapping neighbour with larger offset
        above = [None] * n
        for i in range(n):
            oi, a0, a1, _ = rows[i]
            for j in range(i + 1, n):
                oj, b0, b1, _ = rows[j]
                gap = oj - oi
                if gap > max_gap:
                    break
                if gap < 0.3:
                    continue
                overlap = min(a1, b1) - max(a0, b0)
                if overlap < 0.6 * min(a1 - a0, b1 - b0):
                    continue
                if below[i] is None or gap < below[i][0]:
                    below[i] = (gap, j)
                if above[j] is None or gap < above[j][0]:
                    above[j] = (gap, i)
        for i in range(n):
            if not below[i]:
                continue
            gap, j = below[i]
            if not (gap_range[0] <= gap <= gap_range[1]) or not above[j] or above[j][1] != i:
                continue
            if (above[i] and above[i][0] < 2 * gap) or (below[j] and below[j][0] < 2 * gap):
                continue
            oi, a0, a1, si = rows[i]
            _, b0, b1, sj = rows[j]
            xs, ys = [si[0], si[2], sj[0], sj[2]], [si[1], si[3], sj[1], sj[3]]
            pairs.append({"faces": [[round(v, 2) for v in si], [round(v, 2) for v in sj]],
                          "thickness_pt": round(gap, 3),
                          "length_pt": round(min(a1, b1) - max(a0, b0), 2),
                          "angle_deg": key / 2, "bbox": (min(xs), min(ys), max(xs), max(ys))})
    pairs.sort(key=lambda p: -p["length_pt"])
    return pairs


def analyse_page(page, drawings) -> dict[str, Any]:
    """Title block + dimension/grid/wall candidates for one PDF page."""
    lines = text_lines(page)
    tb = title_block(lines)
    region = None
    if tb:
        x0, y0, x1, y1 = tb["bbox"]
        region = (x0 - 10, y0 - 10, x1 + 10, y1 + 10)
    segs = segments(drawings)
    index = _Index(segs)
    return {"title_block": tb, "dimensions": dimensions(lines, index, region),
            "grids": grid_bubbles(drawings, lines, segs, (page.rect.width, page.rect.height)),
            "walls": wall_pairs(segs, region), "segment_count": len(segs)}


def page_objects(analysis, page_no, name, make, warnings):
    """Turn an analyse_page() result into observation dicts via ``make(key, kind, text, bbox, state, props, extra_evidence)``."""
    out = []
    tb = analysis["title_block"]
    denominator = None
    if tb:
        fields = tb["fields"]
        denominator = scale_denominator(fields.get("scale"))
        text = " ".join(str(v) for v in fields.values())
        obj = make("titleblock", "TitleBlock", f"{name} {text}", _bbox(tb["bbox"]), "AI_INFERRED",
                   {**fields, "title_block_evidence": tb["evidence"],
                    "classification": _cls("TitleBlock", 0.8, "pdf_label_value_rules",
                                           "title-block labels: " + ", ".join(sorted(tb["evidence"])))},
                   {"method": "pdf_label_value"})
        out.append(obj)
    for k, dim in enumerate(analysis["dimensions"]):
        out.append(make(f"dim:{k}", "Dimension", dim["text"], _bbox(dim["bbox"]), "AI_INFERRED",
                        {"text": dim["text"], "measurement": dim["value"], "orientation": dim["orientation"],
                         "dimension_line": dim["dimension_line"],
                         "classification": _cls("Dimension", 0.8, "pdf_vector_rules", "numeric text",
                                                "parallel dimension line adjacent")},
                        {"method": "pdf_dimension_text"}))
    for grid in analysis["grids"]:
        confidence = 0.8 if grid["lines"] else 0.6
        xs = [v for b in grid["bubbles"] for v in (b[0], b[2])]
        ys = [v for b in grid["bubbles"] for v in (b[1], b[3])]
        out.append(make(f"grid:{grid['label']}", "Grid", f"{grid['label']} 통심 그리드",
                        _bbox((min(xs), min(ys), max(xs), max(ys))), "AI_INFERRED",
                        {"grid_label": grid["label"], "bubbles": grid["bubbles"], "grid_lines": grid["lines"],
                         "classification": _cls("Grid", confidence, "pdf_vector_rules", "circle bubble with axis label",
                                                f"grid lines attached={len(grid['lines'])}")},
                        {"method": "pdf_grid_bubble"}))
    walls = analysis["walls"]
    if len(walls) > MAX_WALLS_PER_PAGE:
        warnings.append(f"Page {page_no}: {len(walls)} wall-like line pairs; kept the {MAX_WALLS_PER_PAGE} longest.")
        walls = walls[:MAX_WALLS_PER_PAGE]
    for k, wall in enumerate(walls):
        props = {k2: wall[k2] for k2 in ("faces", "thickness_pt", "length_pt", "angle_deg")}
        confidence, reasons = 0.45, ["long parallel line pair", f"gap={wall['thickness_pt']}pt"]
        if denominator:
            mm = wall["thickness_pt"] * 25.4 / 72 * denominator
            props.update(thickness_mm=round(mm, 1), length_mm=round(wall["length_pt"] * 25.4 / 72 * denominator, 1))
            if 90 <= mm <= 600:
                confidence += 0.15
                reasons.append(f"thickness {mm:.0f}mm at 1/{denominator}")
        props["classification"] = _cls("Wall", confidence, "pdf_vector_rules", *reasons)
        out.append(make(f"wall:{k}", "Wall", "벽 후보 wall candidate", _bbox(wall["bbox"]), "AI_INFERRED", props,
                        {"method": "pdf_parallel_lines"}))
    return out

#!/usr/bin/env python3
"""Generate a deterministic set of Korean-practice DXF drawings plus golden labels.

The drawings imitate what Korean architectural/structural offices actually
deliver: mixed English (AIA-style ``A-WALL``) and Korean (``벽체``, ``창호``)
layer names, door/window blocks named after the 창호일람표 marks (``D1``,
``SD-01``, ``AW-02``, ``창-미서기``), 도곽 title blocks with Korean attribute
tags, 실명/면적 text, 통심(grid) bubbles, detail callouts, steel section
strings (``H-300x300x10x15``), anonymous/dynamic block references (``*U12``),
nested blocks, bound-xref layer names and one CP949 (``ANSI_949``) R2000 file.

Every model-space entity that is written is recorded in ``labels.json`` with
its DXF handle and the expected semantic class, so a parser can be scored
entity-by-entity (see ``scripts/eval_classification.py``).

Usage::

    python scripts/make_ko_fixtures.py [--out tests/fixtures/drawings_ko]

Output is byte-for-byte deterministic for a given ezdxf version (fixed header
metadata, fixed creation order, no randomness).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

import ezdxf
from ezdxf.entities import DXFGraphic, Insert

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "drawings_ko"
LABELS_SCHEMA = "aec-ko-drawing-labels/1"

ENTITY_CLASSES = (
    "Door", "Window", "Wall", "Space", "Column", "Beam", "SteelSection",
    "TitleBlock", "Furniture", "Annotation", "Grid", "Dimension", "BuildingElementProxy",
)
SHEET_CATEGORIES = ("plan", "elevation", "section", "detail", "structural", "schedule")


# --------------------------------------------------------------------------- helpers

class Sheet:
    """One DXF file plus the golden labels for everything written into it."""

    def __init__(
        self,
        filename: str,
        number: str,
        title: str,
        category: str,
        scale: str,
        *,
        dxfversion: str = "R2018",
        encoding: str | None = None,
        revision: str = "R0",
        discipline: str = "architecture",
    ) -> None:
        assert category in SHEET_CATEGORIES, category
        self.filename = filename
        self.sheet = {
            "number": number, "title": title, "category": category,
            "scale": scale, "revision": revision, "discipline": discipline,
        }
        self.doc = ezdxf.new(dxfversion, setup=True, units=4)  # 4 = millimeters
        # Korean AutoCAD writes $DWGCODEPAGE=ANSI_949 into every file. For
        # R2007+ the text is UTF-8 regardless; for R2000 it is really CP949.
        self.doc.encoding = encoding or "cp949"
        self.encoding = self.doc.output_encoding
        self.msp = self.doc.modelspace()
        self.labels: list[dict[str, Any]] = []
        self.block_categories: dict[str, dict[str, Any]] = {}
        self.factor = 1 / _scale_value(scale)  # model mm per paper mm
        self._style()

    # --- document setup -------------------------------------------------------
    def _style(self) -> None:
        # Typical Korean office SHX setup: romans + whgtxt big font.
        self.doc.styles.new("KOR", dxfattribs={"font": "romans.shx", "bigfont": "whgtxt.shx", "width": 0.8})
        self.doc.styles.new("굴림", dxfattribs={"font": "gulim.ttc"})

    def layer(self, name: str, color: int = 7, linetype: str = "Continuous") -> str:
        if name not in self.doc.layers:
            self.doc.layers.add(name, color=color, linetype=linetype)
        return name

    # --- labelling ------------------------------------------------------------
    def label(self, entity: DXFGraphic, cls: str | None, *, score: bool = True, **meta: Any) -> DXFGraphic:
        if score:
            assert cls in ENTITY_CLASSES, cls
        record: dict[str, Any] = {
            "handle": entity.dxf.handle,
            "entity_type": entity.dxftype(),
            "layer": entity.dxf.layer,
            "class": cls,
        }
        if not score:
            record["score"] = False
        if isinstance(entity, Insert):
            record["block"] = entity.dxf.name
        text = _entity_text(entity)
        if text is not None:
            record["text"] = text
        record.update(meta)
        self.labels.append(record)
        return entity

    def block_category(self, name: str, category: str, **meta: Any) -> None:
        self.block_categories[name] = {"name": name, "category": category, **meta}

    # --- drawing primitives that label as they go ------------------------------
    def text(self, value: str, at: tuple[float, float], layer: str, cls: str, height: float = 3.0, **meta: Any):
        e = self.msp.add_text(value, height=height * self.factor, dxfattribs={"layer": layer, "style": "KOR"})
        e.set_placement(at)
        return self.label(e, cls, **meta)

    def mtext(self, value: str, at: tuple[float, float], layer: str, cls: str, height: float = 2.5, width: float = 80, **meta: Any):
        e = self.msp.add_mtext(value, dxfattribs={"layer": layer, "style": "KOR", "char_height": height * self.factor, "width": width * self.factor})
        e.set_location(at)
        return self.label(e, cls, **meta)

    def line(self, a, b, layer: str, cls: str, **meta: Any):
        return self.label(self.msp.add_line(a, b, dxfattribs={"layer": layer}), cls, **meta)

    def pline(self, pts, layer: str, cls: str, closed: bool = False, **meta: Any):
        return self.label(self.msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer}), cls, **meta)

    def hatch(self, boundary, layer: str, cls: str, pattern: str = "ANSI31", scale: float = 1.0, **meta: Any):
        h = self.msp.add_hatch(dxfattribs={"layer": layer})
        if pattern == "SOLID":
            h.set_solid_fill()
        else:
            h.set_pattern_fill(pattern, scale=scale)
        h.paths.add_polyline_path(boundary, is_closed=True)
        return self.label(h, cls, pattern=pattern, **meta)

    def insert(self, block: str, at, layer: str, cls: str | None, *, attribs: dict[str, str] | None = None,
               rotation: float = 0.0, scale: float = 1.0, score: bool = True, **meta: Any):
        e = self.msp.add_blockref(block, at, dxfattribs={"layer": layer, "rotation": rotation, "xscale": scale, "yscale": scale})
        if attribs:
            e.add_auto_attribs(attribs)
            meta.setdefault("attribs", dict(attribs))
        return self.label(e, cls, score=score, **meta)

    def dim(self, p1, p2, base, layer: str = "A-DIMS", angle: float = 0.0, **meta: Any):
        override = {"dimtxt": 2.5, "dimasz": 2.0, "dimscale": self.factor, "dimblk": "ARCHTICK"}
        d = self.msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=angle, dimstyle="EZDXF",
                                    override=override, dxfattribs={"layer": layer})
        d.render()
        return self.label(d.dimension, "Dimension", **meta)

    def leader(self, pts, note: str, layer: str = "A-ANNO-NOTE", **meta: Any):
        ld = self.msp.add_leader(pts, dxfattribs={"layer": layer})
        self.label(ld, "Annotation", role="leader", **meta)
        tx = pts[-1]
        return self.text(note, (tx[0] + 1.0 * self.factor, tx[1]), layer, "Annotation", height=2.5, role="leader-note", **meta)

    # --- output ---------------------------------------------------------------
    def save(self, out_dir: Path) -> dict[str, Any]:
        self.doc.header["$PROJECTNAME"] = "OO동 근린생활시설 신축공사"
        path = out_dir / self.filename
        self.doc.saveas(path)
        blocks = []
        for block in self.doc.blocks:
            name = block.name
            if name.lower() in ("*model_space", "*paper_space") or name.lower().startswith("*paper_space"):
                continue
            blocks.append(self.block_categories.get(name) or _implicit_block_category(name))
        blocks.sort(key=lambda b: b["name"])
        return {
            "sheet": self.sheet,
            "dxfversion": self.doc.dxfversion,
            "encoding": self.encoding,
            "codepage": "ANSI_949",
            "entities": self.labels,
            "blocks": blocks,
        }


def _scale_value(scale: str) -> float:
    num, den = scale.split("/")
    return float(num) / float(den)


def _entity_text(entity: DXFGraphic) -> str | None:
    t = entity.dxftype()
    if t == "TEXT":
        return entity.dxf.text
    if t == "MTEXT":
        return entity.text
    return None


def _implicit_block_category(name: str) -> dict[str, Any]:
    """Blocks ezdxf creates as side effects (dimension geometry, arrow heads)."""
    if name.upper().startswith("*D"):
        return {"name": name, "category": "Dimension", "implicit": True}
    if name.startswith("_"):
        return {"name": name, "category": "Annotation", "implicit": True, "note": "arrow head"}
    raise ValueError(f"unlabelled block definition: {name}")


# --------------------------------------------------------------------------- block library
# Block geometry is drawn at real size (mm) for building elements and at paper
# size (mm on the A1 sheet) for symbols, which are inserted at the sheet factor.

def _door(blk, width: float, double: bool = False) -> None:
    leaf = width / 2 if double else width
    blk.add_line((0, 0), (0, leaf), dxfattribs={"layer": "0"})
    blk.add_arc((0, 0), leaf, 0, 90, dxfattribs={"layer": "0"})
    if double:
        blk.add_line((width, 0), (width, leaf), dxfattribs={"layer": "0"})
        blk.add_arc((width, 0), leaf, 90, 180, dxfattribs={"layer": "0"})
    blk.add_line((0, 0), (width, 0), dxfattribs={"layer": "0", "linetype": "BYBLOCK"})


def _window(blk, width: float, depth: float = 200.0, sliding: bool = False) -> None:
    for y in (0.0, depth):
        blk.add_line((0, y), (width, y))
    blk.add_line((0, 0), (0, depth))
    blk.add_line((width, 0), (width, depth))
    if sliding:  # 미서기: two overlapping sashes
        blk.add_line((0, depth * 0.4), (width * 0.55, depth * 0.4))
        blk.add_line((width * 0.45, depth * 0.6), (width, depth * 0.6))
    else:
        blk.add_line((0, depth / 2), (width, depth / 2))


def _attdefs(blk, tags: Iterable[str], height: float = 100.0, start=(0.0, -150.0), invisible: bool = True) -> None:
    x, y = start
    for i, tag in enumerate(tags):
        blk.add_attdef(tag, (x, y - i * height * 1.5), dxfattribs={"height": height, "flags": 1 if invisible else 0})


def define_door(doc, name: str, width: float, *, double: bool = False) -> None:
    blk = doc.blocks.new(name)
    _door(blk, width, double)
    _attdefs(blk, ("DOOR_NO", "규격", "W", "H"))


def define_window(doc, name: str, width: float, *, sliding: bool = False) -> None:
    blk = doc.blocks.new(name)
    _window(blk, width, sliding=sliding)
    _attdefs(blk, ("WIN_NO", "규격", "W", "H"))


def define_title_block(doc, name: str = "TITLE_A1") -> None:
    """A1 도곽 in paper mm with the usual Korean title-strip attributes."""
    blk = doc.blocks.new(name)
    blk.add_lwpolyline([(0, 0), (841, 0), (841, 594), (0, 594)], close=True)
    blk.add_lwpolyline([(10, 10), (831, 10), (831, 584), (10, 584)], close=True)
    blk.add_lwpolyline([(731, 10), (831, 10), (831, 584), (731, 584)], close=True)
    for y in (60, 90, 120, 150, 180):
        blk.add_line((731, y), (831, y))
    labels = (("프로젝트명", 560), ("도면명", 165), ("도면번호", 135), ("축척", 105), ("개정", 75), ("작성일", 45))
    for caption, y in labels:
        blk.add_text(caption, height=2.5, dxfattribs={"style": "KOR"}).set_placement((735, y + 8))
    for tag, y in (("프로젝트명", 560), ("도면명", 165), ("도면번호", 135), ("축척", 105), ("개정", 75), ("작성일", 45)):
        blk.add_attdef(tag, (760, y), dxfattribs={"height": 4.0, "style": "KOR"})
    blk.add_text("OO건축사사무소", height=5.0, dxfattribs={"style": "KOR"}).set_placement((740, 20))


def define_symbols(doc) -> None:
    gb = doc.blocks.new("GRID_BUBBLE")
    gb.add_circle((0, 0), 5)
    gb.add_attdef("GRID_NO", (0, 0), dxfattribs={"height": 3.5, "halign": 1, "valign": 2})

    na = doc.blocks.new("방위표")
    na.add_circle((0, 0), 10)
    na.add_lwpolyline([(0, 12), (-4, -6), (0, -2), (4, -6)], close=True)
    na.add_text("N", height=4).set_placement((-1.5, 14))

    sec = doc.blocks.new("SEC_MARK")
    sec.add_circle((0, 0), 6)
    sec.add_line((-6, 0), (6, 0))
    sec.add_lwpolyline([(6, 0), (14, 0), (10, 4)], close=True)
    sec.add_attdef("SEC_NO", (0, 1.5), dxfattribs={"height": 3, "halign": 1})
    sec.add_attdef("SHEET_NO", (0, -4.5), dxfattribs={"height": 2.5, "halign": 1})

    det = doc.blocks.new("DETAIL_MARK")
    det.add_circle((0, 0), 6)
    det.add_line((-6, 0), (6, 0))
    det.add_attdef("상세번호", (0, 1.5), dxfattribs={"height": 3, "halign": 1})
    det.add_attdef("도면번호", (0, -4.5), dxfattribs={"height": 2.5, "halign": 1})

    lv = doc.blocks.new("LEVEL")
    lv.add_lwpolyline([(0, 0), (-3, 4), (3, 4)], close=True)
    lv.add_line((0, 4), (25, 4))
    lv.add_attdef("EL", (5, 5), dxfattribs={"height": 2.5})


def define_furniture(doc) -> None:
    def rect(blk, w, h):
        blk.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)

    b = doc.blocks.new("변기"); rect(b, 400, 200); b.add_ellipse((200, 450), major_axis=(0, 250), ratio=0.75)
    b = doc.blocks.new("세면대"); rect(b, 550, 450); b.add_ellipse((275, 225), major_axis=(200, 0), ratio=0.7)
    b = doc.blocks.new("TOILET"); rect(b, 380, 180); b.add_ellipse((190, 420), major_axis=(0, 230), ratio=0.75)
    b = doc.blocks.new("SINK"); rect(b, 800, 500); b.add_circle((400, 250), 180)
    b = doc.blocks.new("욕조"); rect(b, 1500, 700); b.add_lwpolyline([(60, 60), (1440, 60), (1440, 640), (60, 640)], close=True)
    b = doc.blocks.new("BED_Q"); rect(b, 1500, 2000); rect(b, 1500, 400)
    b = doc.blocks.new("실외기"); rect(b, 850, 300); b.add_circle((425, 150), 120)
    # Nested: unit bathroom made of other furniture blocks.
    u = doc.blocks.new("UNIT_화장실")
    u.add_blockref("변기", (100, 100))
    u.add_blockref("세면대", (700, 100))
    u.add_blockref("욕조", (0, 1100))


def define_column(doc, name: str, size: float = 600.0) -> None:
    blk = doc.blocks.new(name)
    h = size / 2
    blk.add_lwpolyline([(-h, -h), (h, -h), (h, h), (-h, h)], close=True)
    hatch = blk.add_hatch()
    hatch.set_solid_fill()
    hatch.paths.add_polyline_path([(-h, -h), (h, -h), (h, h), (-h, h)], is_closed=True)


def define_dynamic_door(doc) -> str:
    """Emulate an AutoCAD dynamic-block reference: geometry lives in ``*U12``.

    AutoCAD writes a modified dynamic block as an anonymous ``*U`` block whose
    effective name is only recoverable through extension data. Here the
    ``AcDbBlockRepresentation``-like hint is stored as XDATA on the BLOCK_RECORD.
    """
    blk = doc.blocks.new("*U12")
    blk.block.dxf.flags |= 1  # anonymous
    _door(blk, 800)
    _attdefs(blk, ("DOOR_NO", "규격"))
    doc.appids.add("AcDbBlockRepETag")
    blk.block_record.set_xdata("AcDbBlockRepETag", [(1000, "DOOR_DYN"), (1070, 1)])
    return blk.name


# --------------------------------------------------------------------------- sheets

def _title(sheet: Sheet, at: tuple[float, float]) -> None:
    define_title_block(sheet.doc)
    s = sheet.sheet
    sheet.insert("TITLE_A1", at, "도곽", "TitleBlock", scale=sheet.factor, attribs={
        "프로젝트명": "OO동 근린생활시설 신축공사",
        "도면명": s["title"], "도면번호": s["number"], "축척": s["scale"],
        "개정": s["revision"], "작성일": "2026.09.15",
    })
    sheet.block_category("TITLE_A1", "TitleBlock")


def _grid(sheet: Sheet, xs: list[float], ys: list[float], layer: str = "A-GRID", bubble_layer: str = "A-GRID-IDEN") -> None:
    sheet.layer(layer, 1, "CENTER")
    sheet.layer(bubble_layer, 1)
    ext = 1500.0
    r = 5 * sheet.factor
    for i, x in enumerate(xs, 1):
        sheet.line((x, ys[0] - ext), (x, ys[-1] + ext), layer, "Grid", grid=f"X{i}")
        sheet.insert("GRID_BUBBLE", (x, ys[-1] + ext + r), bubble_layer, "Grid", scale=sheet.factor, attribs={"GRID_NO": f"X{i}"})
    for j, y in enumerate(ys, 1):
        sheet.line((xs[0] - ext, y), (xs[-1] + ext, y), layer, "Grid", grid=f"Y{j}")
        sheet.insert("GRID_BUBBLE", (xs[0] - ext - r, y), bubble_layer, "Grid", scale=sheet.factor, attribs={"GRID_NO": f"Y{j}"})
    sheet.block_category("GRID_BUBBLE", "Grid")


def _room(sheet: Sheet, name: str, at: tuple[float, float], area: float, layer: str = "A-AREA-IDEN") -> None:
    sheet.text(name, at, layer, "Space", height=3.5, space=name)
    sheet.text(f"{area:.1f}㎡", (at[0], at[1] - 500), layer, "Annotation", height=2.5, role="area", space=name)


def plan_sheet(filename: str, number: str, title: str, *, encoding: str | None = None,
               dxfversion: str = "R2018", full: bool = True) -> Sheet:
    s = Sheet(filename, number, title, "plan", "1/100", dxfversion=dxfversion, encoding=encoding)
    for name, color in (("A-WALL", 2), ("벽체", 2), ("WALL-CON", 8), ("A-WALL-PATT", 252), ("A-DOOR", 3),
                        ("A-DOOR-FIRE", 1), ("문", 3), ("A-GLAZ", 4), ("A-WIND", 4), ("창호", 4),
                        ("A-AREA-IDEN", 7), ("A-ANNO-NOTE", 7), ("A-ANNO-SYMB", 7), ("A-ANNO-TAG", 7),
                        ("A-DIMS", 7), ("A-FURN", 6), ("위생기구", 6), ("A-EQPM", 6), ("도곽", 7)):
        s.layer(name, color)
    define_symbols(s.doc)
    define_furniture(s.doc)

    xs, ys = [0.0, 4000.0, 8000.0, 12000.0], [0.0, 4500.0, 9000.0]
    _grid(s, xs, ys)

    # --- walls: exterior concrete (double polyline), interior masonry, pattern hatch
    t = 200.0
    s.pline([(-t / 2, -t / 2), (12000 + t / 2, -t / 2), (12000 + t / 2, 9000 + t / 2), (-t / 2, 9000 + t / 2)],
            "A-WALL", "Wall", closed=True, role="exterior-outer")
    s.pline([(t / 2, t / 2), (12000 - t / 2, t / 2), (12000 - t / 2, 9000 - t / 2), (t / 2, 9000 - t / 2)],
            "A-WALL", "Wall", closed=True, role="exterior-inner")
    s.hatch([(-t / 2, -t / 2), (12000 + t / 2, -t / 2), (12000 + t / 2, t / 2), (-t / 2, t / 2)],
            "A-WALL-PATT", "Wall", pattern="ANSI31", scale=20)
    s.line((4000 - 50, t / 2), (4000 - 50, 4500), "벽체", "Wall", role="partition")
    s.line((4000 + 50, t / 2), (4000 + 50, 4500), "벽체", "Wall", role="partition")
    s.line((8000 - 75, 4500), (8000 - 75, 9000 - t / 2), "WALL-CON", "Wall", role="core wall")
    s.line((8000 + 75, 4500), (8000 + 75, 9000 - t / 2), "WALL-CON", "Wall", role="core wall")
    s.line((t / 2, 4500 - 50), (12000 - t / 2, 4500 - 50), "벽체", "Wall")
    s.line((t / 2, 4500 + 50), (12000 - t / 2, 4500 + 50), "벽체", "Wall")

    # --- doors: block names per Korean 창호일람표 marks
    define_door(s.doc, "D1", 900); s.block_category("D1", "Door", mark="D1")
    define_door(s.doc, "문-편개", 800); s.block_category("문-편개", "Door", note="편개문 (single swing)")
    s.insert("D1", (1500, 4500 + 50), "A-DOOR", "Door", attribs={"DOOR_NO": "D1", "규격": "900x2100", "W": "900", "H": "2100"})
    s.insert("문-편개", (9000, 4500 + 50), "문", "Door", attribs={"DOOR_NO": "D2", "규격": "800x2100", "W": "800", "H": "2100"})
    if full:
        define_door(s.doc, "DOOR_900", 900); s.block_category("DOOR_900", "Door")
        define_door(s.doc, "SD-01", 1000); s.block_category("SD-01", "Door", note="steel door (강재문)")
        define_door(s.doc, "AD", 1800, double=True); s.block_category("AD", "Door", note="방화문 (fire door), 양개")
        s.insert("DOOR_900", (4000 + 50, 2000), "0", "Door", rotation=90,
                 attribs={"DOOR_NO": "D3", "규격": "900x2100", "W": "900", "H": "2100"}, note="no layer hint")
        s.insert("SD-01", (6000, -t / 2), "A-DOOR", "Door", attribs={"DOOR_NO": "SD-01", "규격": "1000x2100", "W": "1000", "H": "2100"})
        s.insert("AD", (10000, -t / 2), "A-DOOR-FIRE", "Door",
                 attribs={"DOOR_NO": "AD-01", "규격": "1800x2100 (갑종방화문)", "W": "1800", "H": "2100"},
                 note="방화문 short mark AD")
        # dynamic-block reference: anonymous *U12 on layer 0
        dyn = define_dynamic_door(s.doc)
        s.insert(dyn, (8000 + 75, 6000), "0", "Door", rotation=90, attribs={"DOOR_NO": "D4", "규격": "800x2100"},
                 note="dynamic block anonymous reference; effective name DOOR_DYN in XDATA")
        s.block_category(dyn, "Door", anonymous=True, effective_name="DOOR_DYN")
        # exploded door: raw arc + line on the door layer
        s.line((2500, 9000 - t / 2), (2500, 9000 - t / 2 - 900), "A-DOOR", "Door", note="exploded door leaf")
        a = s.msp.add_arc((2500, 9000 - t / 2), 900, 270, 360, dxfattribs={"layer": "A-DOOR"})
        s.label(a, "Door", note="exploded door swing")

    # --- windows
    define_window(s.doc, "W1", 1500); s.block_category("W1", "Window", mark="W1")
    define_window(s.doc, "창-미서기", 2400, sliding=True); s.block_category("창-미서기", "Window", note="미서기창 (sliding)")
    s.insert("W1", (1000, 9000 - t / 2), "A-GLAZ", "Window", attribs={"WIN_NO": "W1", "규격": "1500x1200", "W": "1500", "H": "1200"})
    s.insert("창-미서기", (4800, 9000 - t / 2), "창호", "Window", attribs={"WIN_NO": "W2", "규격": "2400x1500", "W": "2400", "H": "1500"})
    if full:
        define_window(s.doc, "WIN_1500", 1500); s.block_category("WIN_1500", "Window")
        define_window(s.doc, "AW-02", 1200); s.block_category("AW-02", "Window", note="알루미늄창")
        define_window(s.doc, "PW", 900); s.block_category("PW", "Window", note="PVC창")
        s.insert("WIN_1500", (12000 - t / 2, 1500), "0", "Window", rotation=90,
                 attribs={"WIN_NO": "W3", "규격": "1500x1200", "W": "1500", "H": "1200"}, note="no layer hint")
        s.insert("AW-02", (12000 - t / 2, 6000), "A-WIND", "Window", rotation=90,
                 attribs={"WIN_NO": "AW-02", "규격": "1200x1200", "W": "1200", "H": "1200"})
        s.insert("PW", (-t / 2, 6500), "A-GLAZ", "Window", rotation=90,
                 attribs={"WIN_NO": "PW-01", "규격": "900x600", "W": "900", "H": "600"})
        # exploded window: three lines on 창호 layer
        for k, y in enumerate((-t / 2, 0.0, t / 2)):
            s.line((2000, y), (3500, y), "창호", "Window", note=f"exploded window line {k + 1}")
        s.text("W1", (1700, 9000 + 600), "A-ANNO-TAG", "Annotation", height=2.5, role="window tag", refers_to="Window")
        s.text("SD-01", (6400, -900), "A-ANNO-TAG", "Annotation", height=2.5, role="door tag", refers_to="Door")

    # --- rooms (실명 + 면적). 창고/보일러실 are deliberate traps for substring rules.
    rooms = [("거실", (5500, 6800), 27.5), ("침실1", (1800, 6800), 17.6), ("주방", (10000, 6800), 17.6),
             ("현관", (10000, 2400), 8.4)]
    if full:
        rooms += [("화장실", (1800, 2400), 6.2), ("발코니", (6000, 2400), 8.8), ("창고", (2000, 1000), 3.2),
                  ("보일러실", (6000, 1000), 2.4), ("다용도실", (10000, 1000), 4.1)]
    for name, at, area in rooms:
        _room(s, name, at, area)

    # --- furniture / sanitary / equipment
    s.insert("변기", (400, 3000), "위생기구", "Furniture", note="sanitary")
    s.insert("세면대", (1300, 3600), "위생기구", "Furniture", note="sanitary")
    s.block_category("변기", "Furniture", subtype="sanitary")
    s.block_category("세면대", "Furniture", subtype="sanitary")
    s.block_category("UNIT_화장실", "Furniture", subtype="sanitary", nested=["변기", "세면대", "욕조"])
    s.block_category("욕조", "Furniture", subtype="sanitary")
    for name, sub in (("TOILET", "sanitary"), ("SINK", "sanitary"), ("BED_Q", "furniture"), ("실외기", "equipment")):
        s.block_category(name, "Furniture", subtype=sub)
    if full:
        s.insert("UNIT_화장실", (200, 200), "A-FURN", "Furniture", note="nested block (변기/세면대/욕조)")
        s.insert("TOILET", (8300, 5000), s.layer("A-PLMB", 6), "Furniture", note="sanitary")
        s.insert("SINK", (10000, 8200), "A-FURN", "Furniture", note="kitchen sink")
        s.insert("BED_Q", (1000, 5000), "A-FURN", "Furniture")
        s.insert("실외기", (12500, 3000), "A-EQPM", "Furniture", note="equipment (outdoor AC unit)")

    # --- dimensions, notes, symbols
    s.dim((0, 9000), (4000, 9000), (0, 9000 + 2500))
    s.dim((4000, 9000), (8000, 9000), (0, 9000 + 2500))
    if full:
        s.dim((8000, 9000), (12000, 9000), (0, 9000 + 2500))
        s.dim((0, 0), (0, 4500), (-2500, 0), angle=90)
        s.dim((0, 4500), (0, 9000), (-2500, 0), angle=90)
        s.mtext("{\\fGulim|b1;특기사항}\\P1. 모든 치수는 mm 단위임.\\P2. 벽체 마감은 마감표 참조.\\P3. 창호는 창호일람표 참조.",
                (14000, 8000), "A-ANNO-NOTE", "Annotation", role="general notes")
        s.insert("SEC_MARK", (6000, -2500), "A-ANNO-SYMB", "Annotation", scale=s.factor,
                 attribs={"SEC_NO": "1", "SHEET_NO": "A-301"}, role="section callout")
        s.insert("DETAIL_MARK", (1000, 9600), "A-ANNO-SYMB", "Annotation", scale=s.factor,
                 attribs={"상세번호": "1", "도면번호": "A-501"}, role="detail callout")
    s.insert("방위표", (15000, 11000), "A-ANNO-SYMB", "Annotation", scale=s.factor, role="north arrow")
    s.block_category("방위표", "Annotation", subtype="north arrow")
    s.block_category("SEC_MARK", "Annotation", subtype="section callout")
    s.block_category("DETAIL_MARK", "Annotation", subtype="detail callout")
    s.block_category("LEVEL", "Annotation", subtype="level mark")
    s.text(f"{title}  SCALE {s.sheet['scale']}", (0, -4000), "A-ANNO-NOTE", "Annotation", height=5, role="drawing title")

    if full:
        # bound-xref layer naming and an unresolved xref reference
        bound = s.layer("X-BASE$0$A-WALL", 2)
        s.line((12000 + t / 2, 9000 + t / 2), (14000, 9000 + t / 2), bound, "Wall", note="bound xref layer")
        s.doc.add_xref_def("..\\XREF\\X-BASE.dwg", "X-BASE")
        s.insert("X-BASE", (0, 0), s.layer("X-REF", 8), None, score=False, note="external reference; not an element")
        s.block_category("X-BASE", "Xref", xref=True)

    _title(s, (-5000, -8000))
    return s


def window_detail_sheet() -> Sheet:
    s = Sheet("A-501_창호상세도.dxf", "A-501", "창호상세도", "detail", "1/5")
    for name in ("A-WALL", "A-GLAZ", "A-HATCH", "A-HATCH-INSUL", "A-ANNO-NOTE", "A-ANNO-SYMB", "A-DIMS", "A-DETL", "도곽"):
        s.layer(name)
    define_symbols(s.doc)
    for n, cat, extra in (("방위표", "Annotation", {}), ("SEC_MARK", "Annotation", {}), ("DETAIL_MARK", "Annotation", {}),
                          ("GRID_BUBBLE", "Grid", {}), ("LEVEL", "Annotation", {})):
        s.block_category(n, cat, **extra)

    # concrete wall (200) with insulation (100) — section at window head/sill
    wall = [(0, 0), (200, 0), (200, 1500), (0, 1500)]
    s.pline(wall, "A-WALL", "Wall", closed=True, material="철근콘크리트")
    s.hatch(wall, "A-HATCH", "Wall", pattern="AR-CONC", scale=0.5, material="콘크리트")
    insul = [(200, 0), (300, 0), (300, 1500), (200, 1500)]
    s.pline(insul, "A-HATCH-INSUL", "BuildingElementProxy", closed=True, material="단열재")
    s.hatch(insul, "A-HATCH-INSUL", "BuildingElementProxy", pattern="ANSI37", scale=2, material="비드법보온판 2종1호 T100")
    # aluminium frame + double glazing
    frame = [(60, 1500), (140, 1500), (140, 1570), (60, 1570)]
    s.pline(frame, "A-GLAZ", "Window", closed=True, role="window frame (sill)")
    s.pline([(60, 2800), (140, 2800), (140, 2870), (60, 2870)], "A-GLAZ", "Window", closed=True, role="window frame (head)")
    s.line((94, 1570), (94, 2800), "A-GLAZ", "Window", role="glass pane")
    s.line((106, 1570), (106, 2800), "A-GLAZ", "Window", role="glass pane")
    s.line((0, 2870), (300, 2870), "A-DETL", "BuildingElementProxy", role="lintel line")

    s.leader([(100, 2200), (450, 2400), (550, 2400)], "THK24 복층유리 (6+12A+6)")
    s.leader([(140, 1540), (450, 1300), (550, 1300)], "알루미늄 창호 (불소수지도장)")
    s.leader([(250, 800), (450, 700), (550, 700)], "비드법보온판 2종1호 T100")
    s.leader([(60, 1500), (450, 1000), (550, 1000)], "실링 (실리콘, 백업재)")
    s.dim((0, 0), (300, 0), (0, -150))
    s.dim((140, 1570), (140, 2800), (700, 1570), angle=90)
    s.insert("DETAIL_MARK", (-300, 3200), "A-ANNO-SYMB", "Annotation", scale=s.factor,
             attribs={"상세번호": "1", "도면번호": "A-501"}, role="detail title mark")
    s.text("창호상세도-1  SCALE 1/5", (-150, 3150), "A-ANNO-NOTE", "Annotation", height=5, role="detail title")
    s.text("AW-02 하부 상세", (-150, 3000), "A-ANNO-NOTE", "Annotation", height=3, role="detail subtitle", refers_to="Window")
    _title(s, (-1000, -800))
    return s


def stair_detail_sheet() -> Sheet:
    s = Sheet("A-601_계단상세도.dxf", "A-601", "계단상세도", "detail", "1/20")
    for name in ("A-STRS", "A-STRS-HRAL", "A-SLAB", "A-HATCH", "A-ANNO-NOTE", "A-ANNO-SYMB", "A-DIMS", "도곽"):
        s.layer(name)
    define_symbols(s.doc)
    for n in ("방위표", "SEC_MARK", "DETAIL_MARK", "LEVEL"):
        s.block_category(n, "Annotation")
    s.block_category("GRID_BUBBLE", "Grid")

    riser, tread, n = 175.0, 280.0, 9
    pts = [(0.0, 0.0)]
    for i in range(n):
        x, y = pts[-1]
        pts += [(x, y + riser), (x + tread, y + riser)]
    s.pline(pts, "A-STRS", "BuildingElementProxy", role="stair profile (단/디딤)")
    s.line((0, -200), (n * tread, n * riser - 200), "A-STRS", "BuildingElementProxy", role="stair soffit")
    s.hatch([(0, -200), (n * tread, n * riser - 200), (n * tread, n * riser), (0, 0)], "A-HATCH",
            "BuildingElementProxy", pattern="AR-CONC", scale=1, material="철근콘크리트 계단")
    s.line((0, 900), (n * tread, n * riser + 900), "A-STRS-HRAL", "BuildingElementProxy", role="handrail (난간)")
    slab = [(-1500, -200), (0, -200), (0, 0), (-1500, 0)]
    s.pline(slab, "A-SLAB", "BuildingElementProxy", closed=True, role="landing slab")
    s.hatch(slab, "A-SLAB", "BuildingElementProxy", pattern="AR-CONC", scale=1, material="콘크리트 슬래브")
    s.leader([(140, 175), (-600, 900), (-1400, 900)], "디딤판: T30 화강석 버너구이")
    s.leader([(280, 350), (-600, 1300), (-1400, 1300)], "논슬립 (STS, 2줄)")
    s.leader([(840, 1400), (300, 2200), (-500, 2200)], "STS 난간 Ø42.7, H=900")
    s.insert("LEVEL", (-1500, 0), "A-ANNO-SYMB", "Annotation", scale=s.factor, attribs={"EL": "1FL +0"}, role="level mark")
    s.insert("LEVEL", (n * tread, n * riser), "A-ANNO-SYMB", "Annotation", scale=s.factor, attribs={"EL": "중간참 +1,575"}, role="level mark")
    s.dim((0, 0), (0, n * riser), (-300, 0), angle=90, note="단높이 175 x 9")
    s.dim((0, 0), (n * tread, 0), (0, -800), note="단너비 280 x 9")
    s.text(f"단높이 {riser:.0f} x {n} = {riser * n:,.0f}", (0, -1200), "A-ANNO-NOTE", "Annotation", height=3)
    s.insert("SEC_MARK", (n * tread + 800, 0), "A-ANNO-SYMB", "Annotation", scale=s.factor,
             attribs={"SEC_NO": "2", "SHEET_NO": "A-301"}, role="section callout")
    s.text("계단 단면상세도  SCALE 1/20", (-1500, 2800), "A-ANNO-NOTE", "Annotation", height=5, role="detail title")
    _title(s, (-4000, -3000))
    return s


def _h_profile(h: float, b: float, tw: float, tf: float, x0: float, y0: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + b, y0), (x0 + b, y0 + tf), (x0 + b / 2 + tw / 2, y0 + tf), (x0 + b / 2 + tw / 2, y0 + h - tf),
            (x0 + b, y0 + h - tf), (x0 + b, y0 + h), (x0, y0 + h), (x0, y0 + h - tf), (x0 + b / 2 - tw / 2, y0 + h - tf),
            (x0 + b / 2 - tw / 2, y0 + tf), (x0, y0 + tf)]


def steel_detail_sheet() -> Sheet:
    s = Sheet("S-301_접합부상세도.dxf", "S-301", "접합부상세도", "detail", "1/10", discipline="structure")
    for name in ("S-STEEL", "S-PLATE", "S-BOLT", "S-WELD", "S-ANNO", "S-DIMS", "A-ANNO-SYMB", "도곽"):
        s.layer(name)
    define_symbols(s.doc)
    for n in ("방위표", "SEC_MARK", "DETAIL_MARK", "LEVEL"):
        s.block_category(n, "Annotation")
    s.block_category("GRID_BUBBLE", "Grid")

    s.pline(_h_profile(300, 300, 10, 15, 0, 0), "S-STEEL", "SteelSection", closed=True, profile="H-300x300x10x15", member="column")
    s.text("H-300x300x10x15", (0, -150), "S-ANNO", "SteelSection", height=3, profile="H-300x300x10x15")
    cx = 700.0
    s.pline([(cx, 0), (cx + 50, 0), (cx + 50, 7.5), (cx + 5, 7.5), (cx + 5, 92.5), (cx + 50, 92.5), (cx + 50, 100), (cx, 100)],
            "S-STEEL", "SteelSection", closed=True, profile="C-100x50x5x7.5", member="purlin")
    s.text("C-100x50x5x7.5", (cx, -150), "S-ANNO", "SteelSection", height=3, profile="C-100x50x5x7.5")
    lx = 1100.0
    s.pline([(lx, 0), (lx + 75, 0), (lx + 75, 6), (lx + 6, 6), (lx + 6, 75), (lx, 75)], "S-STEEL", "SteelSection",
            closed=True, profile="L-75x75x6", member="brace angle")
    s.text("L-75x75x6", (lx, -150), "S-ANNO", "SteelSection", height=3, profile="L-75x75x6")
    s.pline([(-50, 320), (350, 320), (350, 340), (-50, 340)], "S-PLATE", "SteelSection", closed=True, profile="PL-20")
    s.text("SPL. PL-12 (SM355)", (400, 420), "S-ANNO", "SteelSection", height=3, profile="PL-12")
    for i, (bx, by) in enumerate(((60, 330), (120, 330), (180, 330), (240, 330))):
        c = s.msp.add_circle((bx, by), 11, dxfattribs={"layer": "S-BOLT"})
        s.label(c, "BuildingElementProxy", role="bolt", spec="M20 F10T", index=i + 1)
    s.leader([(120, 330), (300, 600), (450, 600)], "4-M20 F10T (HTB)", layer="S-ANNO")
    s.leader([(350, 330), (500, 500), (600, 500)], "◢ 6 (전둘레 필릿용접)", layer="S-WELD")
    s.dim((0, 0), (300, 0), (0, -300), layer="S-DIMS")
    s.dim((60, 330), (240, 330), (0, 450), layer="S-DIMS", note="볼트 피치 60 @ 3")
    s.insert("DETAIL_MARK", (-200, 900), "A-ANNO-SYMB", "Annotation", scale=s.factor,
             attribs={"상세번호": "3", "도면번호": "S-301"}, role="detail title mark")
    s.insert("SEC_MARK", (900, 900), "A-ANNO-SYMB", "Annotation", scale=s.factor,
             attribs={"SEC_NO": "A", "SHEET_NO": "S-301"}, role="section callout")
    s.text("기둥-보 접합부 상세 (G1 - C1)  SCALE 1/10", (-150, 850), "S-ANNO", "Annotation", height=5, role="detail title")
    s.mtext("강재: SS275 / SM355\\P고장력볼트: F10T, 표준볼트장력 적용", (1400, 600), "S-ANNO", "Annotation", role="notes")
    _title(s, (-2000, -1500))
    return s


def structural_plan_sheet() -> Sheet:
    s = Sheet("S-201_구조평면도.dxf", "S-201", "2층 구조평면도", "structural", "1/100", discipline="structure")
    for name in ("S-COLS", "기둥", "S-BEAM", "보", "S-ANNO", "S-ANNO-TAG", "S-DIMS", "도곽", "A-ANNO-SYMB"):
        s.layer(name)
    define_symbols(s.doc)
    s.block_category("방위표", "Annotation")
    for n in ("SEC_MARK", "DETAIL_MARK", "LEVEL"):
        s.block_category(n, "Annotation")
    xs, ys = [0.0, 6000.0, 12000.0], [0.0, 7000.0]
    _grid(s, xs, ys)
    define_column(s.doc, "C1"); s.block_category("C1", "Column", profile="H-300x300x10x15")
    define_column(s.doc, "C2", 500); s.block_category("C2", "Column")
    for x in xs:
        s.insert("C1", (x, 0), "S-COLS", "Column", mark="C1")
    s.insert("C2", (0, 7000), "S-COLS", "Column", mark="C2")
    s.insert("C2", (6000, 7000), "0", "Column", mark="C2", note="no layer hint")
    # one column drawn as raw polyline + solid hatch on the Korean 기둥 layer
    sq = [(11700, 6700), (12300, 6700), (12300, 7300), (11700, 7300)]
    s.pline(sq, "기둥", "Column", closed=True, mark="C2")
    s.hatch(sq, "기둥", "Column", pattern="SOLID")
    # beams / girders
    s.line((300, 0), (5700, 0), "S-BEAM", "Beam", mark="G1")
    s.line((6300, 0), (11700, 0), "S-BEAM", "Beam", mark="G1")
    s.line((300, 7000), (5700, 7000), "보", "Beam", mark="G2")
    s.line((0, 300), (0, 6700), "S-BEAM", "Beam", mark="G3")
    s.line((12000, 300), (12000, 6700), "보", "Beam", mark="G3")
    s.line((6000, 300), (6000, 6700), "S-BEAM", "Beam", mark="B1")
    s.line((3000, 0), (3000, 7000), "S-BEAM", "Beam", mark="B1", role="small beam")
    s.text("G1", (2500, 150), "S-ANNO-TAG", "Annotation", refers_to="Beam")
    s.text("G2", (2500, 7150), "S-ANNO-TAG", "Annotation", refers_to="Beam")
    s.text("B1", (6150, 3500), "S-ANNO-TAG", "Annotation", refers_to="Beam")
    s.text("C1", (150, -600), "S-ANNO-TAG", "Annotation", refers_to="Column")
    s.text("2C1", (6150, -600), "S-ANNO-TAG", "Annotation", refers_to="Column", note="floor-prefixed mark")
    # member list with steel sections
    s.text("G1 : H-588x300x12x20", (14000, 6000), "S-ANNO", "SteelSection", height=3, profile="H-588x300x12x20")
    s.text("G2 : H-500x200x10x16", (14000, 5500), "S-ANNO", "SteelSection", height=3, profile="H-500x200x10x16")
    s.text("B1 : H-400x200x8x13", (14000, 5000), "S-ANNO", "SteelSection", height=3, profile="H-400x200x8x13")
    s.text("C1 : H-300x300x10x15", (14000, 4500), "S-ANNO", "SteelSection", height=3, profile="H-300x300x10x15")
    s.text("S1 (THK150, 데크플레이트)", (3000, 3500), "S-ANNO", "Annotation", height=3, role="slab mark")
    s.dim((0, 0), (6000, 0), (0, -2000), layer="S-DIMS")
    s.dim((6000, 0), (12000, 0), (0, -2000), layer="S-DIMS")
    s.insert("방위표", (16000, 9000), "A-ANNO-SYMB", "Annotation", scale=s.factor, role="north arrow")
    s.text("2층 구조평면도  SCALE 1/100", (0, -3500), "S-ANNO", "Annotation", height=5, role="drawing title")
    _title(s, (-5000, -8000))
    return s


def elevation_sheet() -> Sheet:
    s = Sheet("A-201_정면도.dxf", "A-201", "정면도", "elevation", "1/100")
    for name in ("A-ELEV-OTLN", "A-ELEV-GLAZ", "A-ELEV-DOOR", "A-ELEV-GRND", "A-ANNO-NOTE", "A-ANNO-SYMB", "도곽"):
        s.layer(name)
    define_symbols(s.doc)
    for n in ("방위표", "SEC_MARK", "DETAIL_MARK", "LEVEL"):
        s.block_category(n, "Annotation")
    s.block_category("GRID_BUBBLE", "Grid")
    blk = s.doc.blocks.new("WIN-EL-1500")
    blk.add_lwpolyline([(0, 0), (1500, 0), (1500, 1200), (0, 1200)], close=True); blk.add_line((750, 0), (750, 1200))
    s.block_category("WIN-EL-1500", "Window", view="elevation")
    blk = s.doc.blocks.new("D-EL-현관")
    blk.add_lwpolyline([(0, 0), (1000, 0), (1000, 2400), (0, 2400)], close=True)
    s.block_category("D-EL-현관", "Door", view="elevation")

    s.pline([(0, 0), (12000, 0), (12000, 6600), (0, 6600)], "A-ELEV-OTLN", "Wall", closed=True, role="facade outline")
    s.line((-2000, 0), (14000, 0), "A-ELEV-GRND", "Annotation", role="ground line (G.L)")
    for x in (1000, 5000, 9000):
        s.insert("WIN-EL-1500", (x, 900), "A-ELEV-GLAZ", "Window")
        s.insert("WIN-EL-1500", (x, 4200), "A-ELEV-GLAZ", "Window")
    s.insert("D-EL-현관", (10500, 0), "A-ELEV-DOOR", "Door")
    for el, y in (("1FL +0", 0), ("2FL +3,300", 3300), ("RFL +6,600", 6600)):
        s.insert("LEVEL", (12500, y), "A-ANNO-SYMB", "Annotation", scale=s.factor, attribs={"EL": el}, role="level mark")
    s.leader([(3000, 3000), (-1000, 7500), (-2000, 7500)], "외벽: THK30 화강석 버너구이")
    s.leader([(6000, 5800), (8000, 7500), (9000, 7500)], "외단열 미장마감 (드라이비트)")
    s.text("정면도  SCALE 1/100", (0, -2000), "A-ANNO-NOTE", "Annotation", height=5, role="drawing title")
    _title(s, (-5000, -6000))
    return s


def section_sheet() -> Sheet:
    s = Sheet("A-301_단면도.dxf", "A-301", "단면도", "section", "1/100")
    for name in ("A-WALL", "A-SLAB", "A-HATCH", "A-ANNO-NOTE", "A-ANNO-SYMB", "A-DIMS", "도곽"):
        s.layer(name)
    define_symbols(s.doc)
    for n in ("방위표", "SEC_MARK", "DETAIL_MARK", "LEVEL"):
        s.block_category(n, "Annotation")
    s.block_category("GRID_BUBBLE", "Grid")
    for y in (0.0, 3300.0, 6600.0):
        slab = [(0, y - 150), (12000, y - 150), (12000, y), (0, y)]
        s.pline(slab, "A-SLAB", "BuildingElementProxy", closed=True, role="slab", level=y)
    s.hatch([(0, 3150), (12000, 3150), (12000, 3300), (0, 3300)], "A-HATCH", "BuildingElementProxy", pattern="AR-CONC", scale=5, role="slab")
    s.pline([(-200, -150), (0, -150), (0, 6600), (-200, 6600)], "A-WALL", "Wall", closed=True)
    s.pline([(12000, -150), (12200, -150), (12200, 6600), (12000, 6600)], "A-WALL", "Wall", closed=True)
    s.dim((12200, 0), (12200, 3300), (13500, 0), angle=90, note="층고 3,300")
    s.dim((12200, 3300), (12200, 6600), (13500, 0), angle=90)
    for el, y in (("1FL +0", 0), ("2FL +3,300", 3300), ("RFL +6,600", 6600)):
        s.insert("LEVEL", (-1500, y), "A-ANNO-SYMB", "Annotation", scale=s.factor, attribs={"EL": el}, role="level mark")
    s.text("천장고 CH=2,700", (5000, 1300), "A-ANNO-NOTE", "Annotation", height=3)
    s.text("A-A' 단면도  SCALE 1/100", (0, -2000), "A-ANNO-NOTE", "Annotation", height=5, role="drawing title")
    _title(s, (-5000, -6000))
    return s


def schedule_sheet() -> Sheet:
    s = Sheet("A-901_창호일람표.dxf", "A-901", "창호일람표", "schedule", "1/50")
    for name in ("A-TABL", "A-TABL-TEXT", "A-ANNO-NOTE", "도곽"):
        s.layer(name)
    rows = [("기호", "규격(WxH)", "형식", "재질/마감", "수량"),
            ("W1", "1500x1200", "고정창", "알루미늄 + THK24 복층유리", "4"),
            ("AW-02", "1200x1200", "미서기창", "알루미늄 (불소수지도장)", "2"),
            ("PW-01", "900x600", "여닫이창", "PVC 이중창", "1"),
            ("D1", "900x2100", "편개문", "목재문 (ABS)", "6"),
            ("SD-01", "1000x2100", "편개문", "강재문", "1"),
            ("AD-01", "1800x2100", "양개문", "갑종방화문", "1")]
    widths = [1500.0, 2000.0, 1500.0, 4000.0, 1000.0]
    rh = 600.0
    total_w = sum(widths)
    for i in range(len(rows) + 1):
        s.line((0, -i * rh), (total_w, -i * rh), "A-TABL", "Annotation", role="table rule")
    x = 0.0
    for w in [0.0] + widths:
        x += w
        s.line((x, 0), (x, -len(rows) * rh), "A-TABL", "Annotation", role="table rule")
    for r, row in enumerate(rows):
        x = 0.0
        for c, cell in enumerate(row):
            s.text(cell, (x + 100, -r * rh - rh + 200), "A-TABL-TEXT", "Annotation", height=3,
                   role="schedule header" if r == 0 else "schedule cell",
                   refers_to=None if r == 0 else ("Window" if row[0][0] in "WAP" and row[0] != "AD-01" else "Door"))
            x += widths[c]
    s.text("창호일람표  SCALE 1/50", (0, 1000), "A-ANNO-NOTE", "Annotation", height=5, role="drawing title")
    _title(s, (-3000, -9000))
    return s


SHEETS: list[Callable[[], Sheet]] = [
    lambda: plan_sheet("A-101_1층평면도.dxf", "A-101", "1층 평면도"),
    lambda: plan_sheet("A-102_2층평면도_cp949.dxf", "A-102", "2층 평면도", encoding="cp949", dxfversion="R2000", full=False),
    elevation_sheet,
    section_sheet,
    window_detail_sheet,
    stair_detail_sheet,
    steel_detail_sheet,
    structural_plan_sheet,
    schedule_sheet,
]


def generate(out_dir: Path) -> dict[str, Any]:
    """Write every fixture DXF plus ``labels.json`` into ``out_dir`` and return the labels."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True  # fixed $TDCREATE/$FINGERPRINTGUID etc.
    try:
        files: dict[str, Any] = {}
        for factory in SHEETS:
            sheet = factory()
            files[sheet.filename] = sheet.save(out_dir)
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = previous
    labels = {
        "schema": LABELS_SCHEMA,
        "generator": "scripts/make_ko_fixtures.py",
        "ezdxf_version": ezdxf.__version__,
        "entity_classes": list(ENTITY_CLASSES),
        "sheet_categories": list(SHEET_CATEGORIES),
        "notes": [
            "Every model-space entity of every file is listed under entities[].",
            "Entries with score=false (e.g. xref INSERTs) are excluded from metrics.",
            "refers_to on Annotation entries names the element a tag/schedule cell describes; it is not the expected class.",
            "blocks[] lists every block definition in the file with its expected category; implicit=true marks ezdxf side-effect blocks.",
        ],
        "files": files,
    }
    (out_dir / "labels.json").write_text(json.dumps(labels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    if argv is None and os.environ.get("PYTHONHASHSEED") != "0":
        # ezdxf orders part of the OBJECTS section by hash; pin the seed so the
        # committed DXF bytes are reproducible (handles/labels are stable anyway).
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, *sys.argv])
    labels = generate(args.out)
    n = sum(len(f["entities"]) for f in labels["files"].values())
    print(f"wrote {len(labels['files'])} DXF files, {n} labelled entities -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

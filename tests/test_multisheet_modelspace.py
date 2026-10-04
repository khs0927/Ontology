"""Modelspace files with multiple detected title blocks are split into sheet-region Views."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.operational.parsers import parse_source


def _settings(root: Path):
    return types.SimpleNamespace(
        data_root=root,
        oda_executable="",
        libredwg_executable="",
        dwg_converter="auto",
    )


def _title_block(doc):
    block = doc.blocks.new("TITLE_FRAME")
    block.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000), (0, 8000)], close=True)
    block.add_attdef("도면명", (7600, 900), dxfattribs={"height": 200})
    block.add_attdef("도면번호", (7600, 600), dxfattribs={"height": 200})
    block.add_attdef("축척", (7600, 300), dxfattribs={"height": 200})
    return block


def test_modelspace_title_blocks_create_independent_sheet_regions(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.layers.add("A-AREA-IDEN")
    _title_block(doc)
    msp = doc.modelspace()

    first = msp.add_blockref("TITLE_FRAME", (0, 0))
    first.add_auto_attribs({"도면명": "1층 평면도", "도면번호": "A-101", "축척": "1/100"})
    msp.add_text("침실 1", dxfattribs={"height": 250, "insert": (2000, 3000), "layer": "A-AREA-IDEN"})

    second = msp.add_blockref("TITLE_FRAME", (15000, 0))
    second.add_auto_attribs({"도면명": "2층 평면도", "도면번호": "A-102", "축척": "1/100"})
    msp.add_text("침실 2", dxfattribs={"height": 250, "insert": (17000, 3000), "layer": "A-AREA-IDEN"})

    source = tmp_path / "복합평면도.dxf"
    doc.saveas(source)

    result = parse_source(source, "doc_multisheet", tmp_path / "out", _settings(tmp_path))
    regions = [
        o for o in result["objects"]
        if o["type"] == "View" and o["properties"].get("view_kind") == "sheet_region"
    ]
    assert len(regions) == 2
    assert {r["properties"]["drawingNumber"] for r in regions} == {"A-101", "A-102"}
    assert {r["properties"]["storey"] for r in regions} == {"1F", "2F"}

    parent = next(
        o for o in result["objects"]
        if o["type"] == "View" and o["properties"].get("layout_kind") == "model"
    )
    assert parent["properties"]["multi_sheet"] is True
    assert parent["properties"]["sheet_region_count"] == 2
    assert "title_block" not in parent["properties"]

    spaces = {o["properties"]["roomNameNormalized"]: o for o in result["objects"] if o["type"] == "Space"}
    assert spaces["침실1"]["properties"]["storey"] == "1F"
    assert spaces["침실1"]["properties"]["drawingNumber"] == "A-101"
    assert spaces["침실2"]["properties"]["storey"] == "2F"
    assert spaces["침실2"]["properties"]["drawingNumber"] == "A-102"

    depicts = [r for r in result["relations"] if r["predicate"] == "depicts"]
    region_ids = {r["id"] for r in regions}
    assert {r["subject"] for r in depicts} <= region_ids
    assert {spaces["침실1"]["id"], spaces["침실2"]["id"]} <= {r["object"] for r in depicts}
    assert result["metrics"]["sheet_regions"] == 2

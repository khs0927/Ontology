"""Regression tests for semantic text stored inside nested DXF block definitions."""

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


def test_nested_block_room_and_area_text_are_extracted_in_wcs(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4

    label = doc.blocks.new("ROOM_LABEL")
    label.add_text("거실", dxfattribs={"height": 250, "insert": (0, 0)})
    label.add_text("25.5㎡", dxfattribs={"height": 200, "insert": (0, -500)})

    inner = doc.blocks.new("INNER")
    inner.add_blockref("ROOM_LABEL", (100, 100))

    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (200, 300))

    source = tmp_path / "1층 평면도_nested.dxf"
    top = doc.modelspace().add_blockref("OUTER", (1000, 2000))
    doc.saveas(source)

    result = parse_source(source, "doc_nested", tmp_path / "out", _settings(tmp_path))

    spaces = [o for o in result["objects"] if o["type"] == "Space"]
    living = next(o for o in spaces if o["properties"].get("roomName") == "거실")
    assert living["properties"]["area"] == pytest.approx(25.5)
    assert living["storey"] == "1F"

    nested = [
        o for o in result["objects"]
        if o.get("evidence", {}).get("method") == "recursive_insert_virtual_entities"
    ]
    assert {o["properties"].get("text") for o in nested} >= {"거실", "25.5㎡"}
    assert all(o["evidence"]["source_handle"] == str(top.dxf.handle) for o in nested)
    assert result["metrics"]["nested_text_entities"] >= 2

    derived = [
        r for r in result["relations"]
        if r["predicate"] == "derivedFrom"
        and r.get("evidence", {}).get("method") == "nested_block_text"
    ]
    assert len(derived) >= 2


def test_nested_block_attribute_room_name_becomes_space(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4

    room_tag = doc.blocks.new("ROOM_TAG")
    room_tag.add_attdef("실명", insert=(0, 0), dxfattribs={"height": 250})

    inner = doc.blocks.new("INNER_ATTR")
    nested = inner.add_blockref("ROOM_TAG", (100, 100))
    nested.add_auto_attribs({"실명": "침실 2"})

    outer = doc.blocks.new("OUTER_ATTR")
    outer.add_blockref("INNER_ATTR", (200, 300))

    source = tmp_path / "2층 평면도_nested_attr.dxf"
    doc.modelspace().add_blockref("OUTER_ATTR", (1000, 2000))
    doc.saveas(source)

    result = parse_source(source, "doc_nested_attr", tmp_path / "out", _settings(tmp_path))
    spaces = [o for o in result["objects"] if o["type"] == "Space"]
    bedroom = next(o for o in spaces if o["properties"].get("roomNameNormalized") == "침실2")
    assert bedroom["properties"]["roomName"] == "침실 2"
    assert {"침실2", "침실 2"} <= set(bedroom["properties"]["roomNameAliases"])
    assert bedroom["storey"] == "2F"

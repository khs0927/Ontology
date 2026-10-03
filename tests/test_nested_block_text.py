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
    assert living["properties"]["storey"] == "1층"

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

"""Storey filled on parsed objects from sheet/title names, layer names and level marker text."""

from __future__ import annotations

import types
from pathlib import Path

import ezdxf
import pytest

from aec_intelligence.classifier import storey_from_layer, storey_from_level_texts


@pytest.mark.parametrize("layer, expected", [
    ("A-1F-WALL", "1F"),
    ("S_B1_COL", "B1"),
    ("A-B2F-DOOR", "B2"),
    ("2층-가구", "2F"),
    ("지하1층_주차", "B1"),
    ("A-RF-PARAPET", "RF"),
    ("M.3FL.DUCT", "3F"),
])
def test_storey_from_layer(layer, expected):
    found = storey_from_layer(layer)
    assert found["storey"] == expected
    assert found["storey_source"] == "layer"
    assert found["storey_evidence"] == layer


@pytest.mark.parametrize("layer", ["0", "", None, "A-WALL", "A-1F~3F-CORE", "1F-2F-STAIR", "DIM-100", "H-300x150"])
def test_storey_from_layer_ignores_layers_without_one_storey(layer):
    assert storey_from_layer(layer) == {}


def test_storey_from_level_texts_needs_agreement():
    assert storey_from_level_texts(["1FL", "1층", "FL+0"])["storey"] == "1F"
    assert storey_from_level_texts(["B1F"])["storey_source"] == "level_text"
    assert storey_from_level_texts(["1FL", "2FL", "RF"]) == {}  # a section lists several levels
    assert storey_from_level_texts([]) == {}


def _settings(root: Path):
    return types.SimpleNamespace(data_root=root, oda_executable="", libredwg_executable="", dwg_converter="auto")


def _parse(tmp_path: Path, doc, name: str):
    from aec_intelligence.operational.parsers import parse_source

    source = tmp_path / name
    doc.saveas(source)
    return parse_source(source, "doc_storey", tmp_path / "out", _settings(tmp_path))


def _by_text(result, text):
    return next(o for o in result["objects"] if o["type"] != "View" and text in o["label"])


def _model(result):
    return next(o for o in result["objects"] if o["type"] == "View" and o["evidence"].get("layout") == "Model")


def test_objects_take_storey_from_layer_then_level_text(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for layer in ("A-2F-ANNO", "A-ANNO"):
        doc.layers.add(layer)
    msp.add_text("1FL", dxfattribs={"height": 100, "insert": (0, 0), "layer": "A-ANNO"})
    msp.add_text("거실", dxfattribs={"height": 100, "insert": (1000, 0), "layer": "A-ANNO"})
    msp.add_text("발코니", dxfattribs={"height": 100, "insert": (2000, 0), "layer": "A-2F-ANNO"})
    result = _parse(tmp_path, doc, "A-101 계획도.dxf")

    assert _model(result)["storey"] == "1F"
    assert _model(result)["properties"]["storey_source"] == "level_text"
    living = _by_text(result, "거실")
    assert living["storey"] == "1F" and living["properties"]["storey_source"] == "level_text"
    balcony = _by_text(result, "발코니")
    assert balcony["storey"] == "2F" and balcony["properties"]["storey_source"] == "layer"


def test_sheet_title_storey_fills_every_object(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    msp.add_text("지하1층 평면도", dxfattribs={"height": 500, "insert": (0, 0)})
    msp.add_text("기계실", dxfattribs={"height": 100, "insert": (1000, 0)})
    result = _parse(tmp_path, doc, "A-100.dxf")

    assert _model(result)["storey"] == "B1"
    assert _by_text(result, "기계실")["storey"] == "B1"


def test_section_with_several_levels_stays_unassigned(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for i, level in enumerate(("1FL", "2FL", "3FL", "RF")):
        msp.add_text(level, dxfattribs={"height": 100, "insert": (0, i * 3000)})
    msp.add_text("계단", dxfattribs={"height": 100, "insert": (1000, 0)})
    result = _parse(tmp_path, doc, "A-301 단면도.dxf")

    assert _model(result)["storey"] == ""
    assert _by_text(result, "계단")["storey"] == ""


def test_layout_name_names_the_storey_of_a_paper_sheet(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    layout = doc.layouts.new("3층")
    layout.add_text("창고", dxfattribs={"height": 100, "insert": (0, 0)})
    result = _parse(tmp_path, doc, "평면.dxf")

    assert _by_text(result, "창고")["storey"] == "3F"

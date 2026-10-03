"""Room-name normalization keeps existing values while improving indexed-room recall."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.classifier import room_from_text
from aec_intelligence.operational.parsers import parse_source


@pytest.mark.parametrize("text,display,normalized", [
    ("침실1", "침실1", "침실1"),
    ("침실 1", "침실 1", "침실1"),
    ("침실 01", "침실 01", "침실01"),
])
def test_indexed_room_names_keep_display_name_and_add_aliases(text, display, normalized):
    room = room_from_text(text)
    assert room["roomName"] == display
    assert room["roomNameNormalized"] == normalized
    assert normalized in room["roomNameAliases"]
    index = normalized[len("침실"):]
    assert f"침실 {index}" in room["roomNameAliases"]


@pytest.mark.parametrize("text", ["거실", "화장실", "발코니", "보일러실"])
def test_common_korean_room_names_remain_compatible(text):
    assert room_from_text(text) == {"roomName": text}


def _settings(root: Path):
    return types.SimpleNamespace(
        data_root=root,
        oda_executable="",
        libredwg_executable="",
        dwg_converter="auto",
    )


def test_space_search_text_contains_compact_and_spaced_indexed_aliases(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_text(
        "침실 1",
        dxfattribs={"height": 250, "insert": (1000, 1000), "layer": "A-AREA-IDEN"},
    )
    source = tmp_path / "1층 평면도_room_alias.dxf"
    doc.saveas(source)

    result = parse_source(source, "doc_room_alias", tmp_path / "out", _settings(tmp_path))
    space = next(o for o in result["objects"] if o["type"] == "Space")
    assert space["properties"]["roomName"] == "침실 1"
    assert space["properties"]["roomNameNormalized"] == "침실1"
    assert {"침실1", "침실 1"} <= set(space["properties"]["roomNameAliases"])
    assert "침실1" in space["search_text"]
    assert "침실 1" in space["search_text"]

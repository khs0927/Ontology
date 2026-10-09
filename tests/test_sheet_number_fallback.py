"""Sheet numbers from layout and file names when a drawing has no attributed title block."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from aec_intelligence.operational.parsers import filename_sheet_fields, layout_sheet_number


@pytest.mark.parametrize("name, expected", [
    ("A-101 1층 평면도.dwg", ("A-101", "1층 평면도")),
    ("03_C-010 흙막이 계측평면도.dxf", ("C-010", "흙막이 계측평면도")),
    ("M-357 - [ 지하1층 덕트 평면도 ].dwg", ("M-357", "지하1층 덕트 평면도")),
    ("T-18-지붕 통신 설비도.dwg", ("T-18", "지붕 통신 설비도")),
    ("S-101~132 구조평면도.dxf", ("S-101", "구조평면도")),
    ("MA-005_3층 배관도.pdf", ("MA-005", "3층 배관도")),
    ("소방-05 A동지상2층소방설비평면도.dwg", ("소방-05", "A동지상2층소방설비평면도")),
    ("통신-01 구내통신선로설비도.dwg", ("통신-01", "구내통신선로설비도")),
    ("토목-02 토공계획평면도.dwg", ("토목-02", "토공계획평면도")),
    ("지하2층평면도.dxf", (None, "지하2층평면도")),
    ("19 - 옹벽(H=2.0m)구조도.dxf", (None, "19 - 옹벽(H=2.0m)구조도")),
    ("2024 설계도.dwg", (None, "2024 설계도")),
    ("현장 KS-2019 반영본.dwg", (None, "현장 KS-2019 반영본")),  # a code inside free text is not a sheet number
])
def test_filename_sheet_fields(name, expected):
    assert filename_sheet_fields(name) == expected


def test_layout_sheet_number():
    assert layout_sheet_number("A-101") == "A-101"
    assert layout_sheet_number("S-002 구조평면도") == "S-002"
    assert layout_sheet_number("Layout1") is None
    assert layout_sheet_number("배치도") is None


ezdxf = pytest.importorskip("ezdxf")


def _settings(root: Path):
    return types.SimpleNamespace(data_root=root, oda_executable="", libredwg_executable="", dwg_converter="auto")


def _views(result):
    return {o["evidence"]["layout"]: o for o in result["objects"] if o["type"] == "View"}


def test_single_sheet_file_takes_number_from_file_name(tmp_path: Path):
    from aec_intelligence.operational.parsers import parse_source

    doc = ezdxf.new("R2018", setup=True)
    doc.modelspace().add_text("1층 평면도", dxfattribs={"height": 500, "insert": (0, 0)})
    source = tmp_path / "07_A-101 1층 평면도.dxf"
    doc.saveas(source)
    result = parse_source(source, "doc_fallback", tmp_path / "out", _settings(tmp_path))
    model = _views(result)["Model"]["properties"]
    assert model["drawingNumber"] == "A-101" and model["drawingNumber_source"] == "file_name"
    assert model["drawingTitle"] == "1층 평면도"
    assert result["objects"][0]["properties"]["file_sheet_number"] == "A-101"


def test_multi_layout_file_uses_layout_names_not_the_file_name(tmp_path: Path):
    from aec_intelligence.operational.parsers import parse_source

    doc = ezdxf.new("R2018", setup=True)
    doc.modelspace().add_line((0, 0), (1000, 0))
    for sheet in ("S-001", "S-002"):
        layout = doc.layouts.new(sheet)
        layout.add_text(f"{sheet} 구조 일반사항", dxfattribs={"height": 5, "insert": (10, 10)})
    source = tmp_path / "S-001~002 구조일반사항.dxf"
    doc.saveas(source)
    result = parse_source(source, "doc_layouts", tmp_path / "out", _settings(tmp_path))
    views = _views(result)
    assert views["S-001"]["properties"]["drawingNumber"] == "S-001"
    assert views["S-002"]["properties"]["drawingNumber"] == "S-002"
    assert views["S-002"]["properties"]["drawingNumber_source"] == "layout_name"
    # two sheets in one file: the file name's first number belongs to neither model space nor Layout1
    assert "drawingNumber" not in views["Model"]["properties"]
    assert "drawingNumber" not in views["Layout1"]["properties"]


def test_title_block_number_wins_over_file_name(tmp_path: Path):
    from aec_intelligence.operational.parsers import parse_source

    doc = ezdxf.new("R2018", setup=True)
    block = doc.blocks.new("TB")
    block.add_attdef("도면명", (0, 0))
    block.add_attdef("도면번호", (0, 10))
    ref = doc.modelspace().add_blockref("TB", (0, 0))
    ref.add_auto_attribs({"도면명": "2층 평면도", "도면번호": "A-202"})
    source = tmp_path / "A-999 옛 이름.dxf"
    doc.saveas(source)
    result = parse_source(source, "doc_tb", tmp_path / "out", _settings(tmp_path))
    model = _views(result)["Model"]["properties"]
    assert model["drawingNumber"] == "A-202" and "drawingNumber_source" not in model


def test_lowercase_latin_sheet_prefix_still_parsed():
    # c05db3a dropped the .upper() before matching; Latin prefixes stay case-insensitive (output uppercase)
    assert filename_sheet_fields("a-101 1층 평면도.dwg") == ("A-101", "1층 평면도")
    assert filename_sheet_fields("s101_구조.dwg") == ("S101", "구조")
    assert layout_sheet_number("a-101") == "A-101"

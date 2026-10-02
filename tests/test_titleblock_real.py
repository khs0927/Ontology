"""Title blocks as real sheets draw them: attribute-less frame blocks, printed labels, loose values."""

from __future__ import annotations

from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.classifier import classify, title_block_label  # noqa: E402
from aec_intelligence.dxf import DXFParser, NormalizedCADEntity  # noqa: E402


def _text(space, text, x, y, h=2.5, layer="0"):
    return space.add_text(text, height=h, dxfattribs={"layer": layer, "insert": (x, y)})


def _parse(doc, tmp_path: Path, name: str):
    path = tmp_path / name
    doc.saveas(path)
    return DXFParser().parse(path).sheet


def test_korean_labels_in_nested_frame_block_with_mtext_values_in_paperspace(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    # Label cells live in a nested block inside the frame block; no ATTDEF/ATTRIB anywhere.
    cells = doc.blocks.new("TB_CELLS")
    for label, y in (("공 사 명", 50), ("도면명", 40), ("축척", 30), ("일자", 20), ("도면번호", 10)):
        _text(cells, label, 0, y, layer="A-FORM")
    frame = doc.blocks.new("도곽_A1")
    frame.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    frame.add_blockref("TB_CELLS", (330, 0))
    psp = doc.paperspace()
    psp.add_blockref("도곽_A1", (0, 0), dxfattribs={"layer": "A-FORM"})
    psp.add_mtext(r"{\fMalgun Gothic|b0;\C1;강남 오피스 신축공사}", dxfattribs={"insert": (345, 47), "char_height": 2.5})
    psp.add_mtext(r"\A1;1층 평면도\P(FIRST FLOOR)", dxfattribs={"insert": (345, 37), "char_height": 2.5})
    _text(psp, "1 : 100", 345, 27)
    _text(psp, "2026. 09. 15", 345, 17)
    _text(psp, "A-101", 345, 7)
    sheet = _parse(doc, tmp_path, "unnamed.dxf")
    assert sheet["source"] == "title_block"
    assert sheet["number"] == "A-101"
    assert sheet["title"].startswith("1층 평면도")
    assert sheet["scale"] == "1/100"
    assert sheet["date"] == "2026.09.15"
    assert sheet["project"] == "강남 오피스 신축공사"
    assert sheet["title_block_method"].startswith("printed_labels@")


def test_english_labels_scaled_frame_values_typed_by_shape(tmp_path):
    doc = ezdxf.new("R2018")
    frame = doc.blocks.new("ZIUM_sheet_architect")
    for label, y in (("NAME OF DRAWING", 110), ("SCALE", 70), ("A1 SIZE", 65), ("DATE", 48), ("DRAWING NO.", 38)):
        _text(frame, label, 750, y, h=2.0, layer="A-FORM")
    frame.add_lwpolyline([(0, 0), (840, 0), (840, 594), (0, 594)], close=True)
    msp = doc.modelspace()
    msp.add_blockref("ZIUM_sheet_architect", (1000, 1000), dxfattribs={"layer": "A-FORM", "xscale": 2, "yscale": 2})
    _text(msp, "2층 평면도", 2510, 1212, h=5)
    _text(msp, "1 / 80", 2530, 1128, h=4)
    _text(msp, "2025. 03", 2525, 1090, h=4)
    _text(msp, "-", 2530, 1074, h=4)  # placeholder drawing number is ignored
    sheet = _parse(doc, tmp_path, "zium.dxf")
    assert sheet["source"] == "title_block"
    assert (sheet["title"], sheet["scale"], sheet["date"]) == ("2층 평면도", "1/80", "2025.03")
    assert sheet["number"] is None


def test_placeholder_frame_reads_values_on_the_frame_layer(tmp_path):
    doc = ezdxf.new("R2018")
    frame = doc.blocks.new("HS_FRAME_A3")
    frame.add_lwpolyline([(0, 0), (420, 0), (420, 297), (0, 297)], close=True)
    _text(frame, "PLACEHOLDER FRAME", 20, 280)
    msp = doc.modelspace()
    msp.add_blockref("HS_FRAME_A3", (0, 0), dxfattribs={"layer": "HS-FRAME", "xscale": 10, "yscale": 10})
    for text, y in (("SAMPLE PROJECT", 1800), ("ERECTION PLAN", 1425), ("E-001", 1050), ("1/60", 750), ("2026.10.02", 525)):
        _text(msp, text, 3900, y, h=40, layer="HS-FRAME")
    _text(msp, "X1", 500, 2000, h=40, layer="HS-GRID")
    sheet = _parse(doc, tmp_path, "sheet.dxf")
    assert sheet["source"] == "title_block"
    assert (sheet["number"], sheet["title"], sheet["scale"], sheet["date"]) == ("E-001", "ERECTION PLAN", "1/60", "2026.10.02")
    assert sheet["project"] == "SAMPLE PROJECT"
    assert sheet["title_block_method"].startswith("frame_layer_values@")


def test_inline_label_value_texts_without_frame(tmp_path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    _text(msp, "도면명 : 배치도", 0, 30)
    _text(msp, "축척: 1/200", 0, 20)
    _text(msp, "도면번호_A-001", 0, 10)
    sheet = _parse(doc, tmp_path, "x.dxf")
    assert (sheet["source"], sheet["title"], sheet["scale"], sheet["number"]) == ("title_block", "배치도", "1/200", "A-001")


def test_empty_template_and_xref_frame_stay_on_file_name(tmp_path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    for i, (label, value) in enumerate((("Sheet", "#sheet_no"), ("Scale", "#scale"), ("Project Title:", "#project_title"))):
        _text(msp, label, 0, 10 * i)
        _text(msp, value, 30, 10 * i)
    doc.add_xref_def("frame.dwg", "XREF_TITLE")
    msp.add_blockref("XREF_TITLE", (0, 0))
    sheet = _parse(doc, tmp_path, "A-201_template.dxf")
    assert sheet["source"] == "file_name"
    assert sheet["number"] == "A-201"


def test_attribute_title_block_still_wins(tmp_path):
    doc = ezdxf.new("R2018")
    block = doc.blocks.new("TITLE")
    for tag, y in (("DWG_NO", 0), ("TITLE", 10)):
        block.add_attdef(tag, (0, y))
    _text(block, "NAME OF DRAWING", -50, 10)
    insert = doc.paperspace().add_blockref("TITLE", (0, 0))
    insert.add_auto_attribs({"DWG_NO": "S-301", "TITLE": "접합부상세도"})
    sheet = _parse(doc, tmp_path, "s.dxf")
    assert (sheet["number"], sheet["title"], sheet["source"]) == ("S-301", "접합부상세도", "title_block")
    assert "title_block_method" not in sheet


def test_title_block_label_parsing():
    assert title_block_label("DRAWING NO.") == ("drawingNumber", "")
    assert title_block_label("도 면 명 :") == ("drawingTitle", "")
    assert title_block_label("공사명 : 신축공사") == ("projectName", "신축공사")
    assert title_block_label("거실 / 식당") is None


@pytest.mark.parametrize("entity_type,layer,expected", [
    ("LINE", "A-DETL", "BuildingElementProxy"),
    ("LWPOLYLINE", "S-PLATE", "SteelSection"),
    ("LWPOLYLINE", "S-STIFF", "SteelSection"),
])
def test_detail_and_plate_layer_fallbacks(entity_type, layer, expected):
    assert classify(NormalizedCADEntity("1", entity_type, layer))[0] == expected


def test_frame_block_names_are_title_blocks():
    for name in ("HS_FRAME_A3", "ZIUM_sheet_architect"):
        entity = NormalizedCADEntity("1", "INSERT", "0", properties={"block_name": name, "effective_name": name})
        assert classify(entity)[0] == "TitleBlock"

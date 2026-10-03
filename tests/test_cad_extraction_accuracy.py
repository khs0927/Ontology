"""Recall fixes for real Korean drawings: block expansion, sheet splitting, storeys, room names."""
from __future__ import annotations

from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.classifier import room_from_text  # noqa: E402
from aec_intelligence.operational import parsers  # noqa: E402
from aec_intelligence.operational.config import Settings  # noqa: E402
from aec_intelligence.operational.parsers import filename_sheet_fields, parse_source  # noqa: E402
from aec_intelligence.spatial_relations import parse_storey, storey_from_room_numbers  # noqa: E402


def _parse(tmp_path: Path, doc, name="drawing.dxf"):
    path = tmp_path / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    data_root = tmp_path / "data"
    settings = Settings(dsn="dummy", data_root=data_root, import_roots=(tmp_path,))
    return parse_source(path, "doc_t", data_root / "artifacts" / "doc_t", settings, name)


def _kind(result, kind):
    return [o for o in result["objects"] if o["type"] == kind]


def _rooms(result):
    return sorted(o["properties"]["roomName"] for o in _kind(result, "Space"))


def _text(space, value, x, y, h=5.0):
    space.add_text(value, height=h, dxfattribs={"insert": (x, y)})


def _frame(msp, x0, y0, w=840.0, h=594.0):
    msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], close=True)


# --------------------------------------------------------------------------- 1. block expansion

def test_room_texts_inside_inserted_plan_block_are_found_with_transform(tmp_path):
    doc = ezdxf.new()
    tag = doc.blocks.new("tag")
    _text(tag, "진료실", 0, 0)
    plan = doc.blocks.new("base-지상2층 평면도")
    _text(plan, "회의실", 100, 100)
    _text(plan, "복 도", 300, 100)
    plan.add_blockref("tag", (500, 100))
    doc.modelspace().add_blockref("base-지상2층 평면도", (1000, 0), dxfattribs={"xscale": 2, "yscale": 2})
    result = _parse(tmp_path, doc)
    assert _rooms(result) == ["복도", "진료실", "회의실"]
    meeting = next(o for o in _kind(result, "Space") if o["properties"]["roomName"] == "회의실")
    assert meeting["bbox"]["min_x"] == pytest.approx(1200) and meeting["bbox"]["min_y"] == pytest.approx(200)
    assert meeting["evidence"]["method"] == "block_expansion" or "virtual_of" in meeting["evidence"]
    # Storey comes from the plan block's name when the sheet title has none.
    assert {s["storey"] for s in _kind(result, "Space")} == {"2F"}
    assert [o["properties"]["storeyName"] for o in _kind(result, "Storey")] == ["2F"]


def test_block_expansion_in_paper_space(tmp_path):
    doc = ezdxf.new()
    block = doc.blocks.new("rooms")
    _text(block, "처치실", 10, 10)
    doc.layouts.new("A-101").add_blockref("rooms", (0, 0))
    assert "처치실" in _rooms(_parse(tmp_path, doc))


def test_block_expansion_survives_cycles_and_is_capped(tmp_path, monkeypatch):
    doc = ezdxf.new()
    a, b = doc.blocks.new("A"), doc.blocks.new("B")
    _text(a, "회의실", 0, 0)
    _text(b, "상담실", 0, 50)
    a.add_blockref("B", (0, 0))
    b.add_blockref("A", (0, 0))  # A -> B -> A
    doc.modelspace().add_blockref("A", (0, 0))
    result = _parse(tmp_path, doc)
    assert set(_rooms(result)) == {"상담실", "회의실"}

    big = ezdxf.new()
    block = big.blocks.new("big")
    for i in range(50):
        _text(block, f"T{i}", i * 10, 0)
    big.modelspace().add_blockref("big", (0, 0))
    monkeypatch.setattr(parsers, "MAX_EXPAND_ENTITIES", 10)
    capped = _parse(tmp_path / "cap", big)
    assert capped["metrics"]["virtual_expansion_truncated"] == 1
    assert capped["metrics"]["virtual_texts"] == 10


# --------------------------------------------------------------------------- 2. sheet splitting

SHEETS = [("A-012", "지상1층 평면도", "회의실"), ("MC-008", "지상2층 평면도", "진료실"), ("E-507", "지하1층 평면도", "기계실")]


def test_model_space_with_several_bordered_sheets_splits_per_sheet(tmp_path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    for i, (number, title, room) in enumerate(SHEETS):
        x0 = i * 1000
        _frame(msp, x0, 0)
        _text(msp, "도면번호", x0 + 650, 40)
        _text(msp, number, x0 + 710, 40)
        _text(msp, "도면명", x0 + 650, 20)
        _text(msp, title, x0 + 710, 20)
        _text(msp, room, x0 + 200, 300)
    result = _parse(tmp_path, doc, "05_전체도면.dxf")
    sheets = {s["properties"]["drawingNumber"]: s["properties"]["drawingTitle"] for s in _kind(result, "Sheet")}
    assert sheets == {number: title for number, title, _ in SHEETS}
    storeys = {s["properties"]["roomName"]: s["storey"] for s in _kind(result, "Space")}
    assert storeys == {"회의실": "1F", "진료실": "2F", "기계실": "B1"}


def test_title_block_inserts_define_sheets_and_regex_number_from_block_texts(tmp_path):
    doc = ezdxf.new()
    tb = doc.blocks.new("TITLE_A1")  # an exploded-style title block: plain texts, no attributes
    tb.add_lwpolyline([(0, 0), (840, 0), (840, 594), (0, 594)], close=True)
    _text(tb, "S-101", 700, 30, h=6)
    _text(tb, "2층 구조평면도", 600, 60, h=8)
    _text(tb, "공사명", 600, 90, h=3)
    msp = doc.modelspace()
    msp.add_blockref("TITLE_A1", (0, 0))
    msp.add_blockref("TITLE_A1", (2000, 0))
    result = _parse(tmp_path, doc, "struct.dxf")
    sheets = _kind(result, "Sheet")
    assert len(sheets) == 2
    assert {s["properties"]["drawingNumber"] for s in sheets} == {"S-101"}
    assert {s["properties"]["drawingTitle"] for s in sheets} == {"2층 구조평면도"}


def test_file_name_is_only_a_fallback_without_layout_suffix_or_numeric_prefix(tmp_path):
    assert filename_sheet_fields("03_A-101_1층평면도.dxf") == ("A-101", "1층평면도")
    assert filename_sheet_fields("12 T-18 창호도.dwg") == ("T-18", "창호도")
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (10, 0))
    _text(doc.modelspace(), "회의실", 0, 0)
    sheet = _kind(_parse(tmp_path, doc, "03_A-101_1층평면도.dxf"), "Sheet")[0]["properties"]
    assert sheet["drawingNumber"] == "A-101" and ":Model" not in sheet["drawingNumber"]


# --------------------------------------------------------------------------- 3. storey parser

@pytest.mark.parametrize("text,expected", [
    ("지하2층 평면도", ("B2", -2)), ("B1F PLAN", ("B1", -1)), ("지하B1F평면", ("B1", -1)), ("B3", ("B3", -3)),
    ("지상2층", ("2F", 2)), ("3층 평면도", ("3F", 3)), ("5F FLOOR PLAN", ("5F", 5)),
    ("지붕층 평면도", ("RF", None)), ("옥상 평면도", ("RF", None)), ("RF PLAN", ("RF", None)),
    ("옥탑층", ("PH", None)), ("PH 평면도", ("PH", None)),
    ("M-503 - [ 지상2층가스배관평면도 ]", ("2F", 2)), ("A-012 정면도", None), ("PHASE 1", None), ("T-18", None),
])
def test_parse_storey(text, expected):
    assert parse_storey(text) == expected


def test_storey_inferred_from_room_numbers(tmp_path):
    assert storey_from_room_numbers(["301", "302호", "305"])["name"] == "3F"
    assert storey_from_room_numbers(["101", "201"]) is None
    doc = ezdxf.new()
    msp = doc.modelspace()
    for i, label in enumerate(["301호 병실", "302호 병실", "303호 처치실"]):
        _text(msp, label, i * 100, 0)
    result = _parse(tmp_path, doc, "ward.dxf")
    assert {s["storey"] for s in _kind(result, "Space")} == {"3F"}


# --------------------------------------------------------------------------- 4. room names

@pytest.mark.parametrize("text,name", [
    ("복 도", "복도"), ("전 실", "전실"), ("계단실#1", "계단실"), ("진료실-1", "진료실"), ("4인실 -1", "4인실"),
    ("급수/소방 물탱크실", "급수/소방 물탱크실"), ("기계실 및 관리실", "기계실 및 관리실"), ("원무과", "원무과"),
    ("대기공간", "대기공간"), ("주차장", "주차장"), ("데크", "데크"), ("정원", "정원"), ("로비", "로비"),
    ("EPS", "EPS"), ("PS-2", "PS-2"), ("EV", "EV"), ("처치실", "처치실"), ("상담실", "상담실"), ("병실", "병실"),
    ("수술실", "수술실"), ("간호사실", "간호사실"), ("시험실", "시험실"), ("실험실", "실험실"),
])
def test_room_names_recognised(text, name):
    assert room_from_text(text)["roomName"] == name


@pytest.mark.parametrize("text", ["결과", "장소", "도장", "1층 평면도", "기계실 상세도", "SCALE 1/100", "A-101",
                                  "거실 바닥 마감", "외벽 마감 참조", "현장", "효과", "주소"])
def test_non_rooms_rejected(text):
    assert room_from_text(text) is None


def test_room_tag_and_name_text_are_one_room(tmp_path):
    doc = ezdxf.new()
    tag = doc.blocks.new("ROOMTAG")
    tag.add_attdef("ROOM_NAME", (0, 0), dxfattribs={"height": 3})
    tag.add_attdef("ROOM_NO", (0, -5), dxfattribs={"height": 3})
    msp = doc.modelspace()
    msp.add_blockref("ROOMTAG", (0, 0)).add_auto_attribs({"ROOM_NAME": "회의실", "ROOM_NO": "101"})
    _text(msp, "회의실", 0, -6, h=3)
    _text(msp, "회의실", 5000, 0, h=3)  # a second meeting room far away stays
    spaces = _kind(_parse(tmp_path, doc), "Space")
    assert len(spaces) == 2
    assert sorted(str(s["properties"].get("roomNumber")) for s in spaces) == ["101", "None"]


def test_view_captions_are_not_rooms(tmp_path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    _text(msp, "기계실", 0, 0)
    _text(msp, "평면도", 25, 0)
    _text(msp, "전기실", 0, 500)
    _text(msp, "SCALE 1/50", 0, 490)
    _text(msp, "회의실", 0, 1000)
    assert _rooms(_parse(tmp_path, doc)) == ["회의실"]

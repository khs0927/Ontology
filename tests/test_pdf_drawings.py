"""PDF drawing semantics: title block fields, vector-derived candidates, text-less pages without OCR."""
from collections import Counter
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from aec_intelligence.classifier import _state  # noqa: E402
from aec_intelligence.operational.config import Settings  # noqa: E402
from aec_intelligence.operational.parsers import parse_source  # noqa: E402


def _drawing_page(doc):
    page = doc.new_page(width=842, height=595)
    text = lambda xy, s, size=6: page.insert_text(xy, s, fontsize=size, fontname="korea")
    # Title block (bottom right): labels with values to their right.
    page.draw_rect(pymupdf.Rect(640, 500, 830, 585), width=0.5)
    for y, label, value in ((515, "도면번호", "A-101"), (530, "도면명", "1층 평면도"), (545, "축척", "1/100"),
                            (560, "날짜", "2026.05"), (575, "설계", "홍길동 건축사")):
        text((650, y), label)
        text((700, y), value)
    # Wall candidate: two 200pt faces 5.67pt apart (200mm at 1/100), isolated from other parallels.
    page.draw_line((100, 200), (300, 200), width=0.3)
    page.draw_line((100, 205.67), (300, 205.67), width=0.3)
    # Hatching ladder (equal spacing) must not become walls.
    for k in range(6):
        page.draw_line((100, 300 + 4 * k), (300, 300 + 4 * k), width=0.1)
    # Dimension: line with a numeric text just above it.
    page.draw_line((100, 120), (300, 120), width=0.2)
    text((190, 117), "6000")
    # Grid bubble "A" with a long grid line ending at the circle.
    page.draw_circle((60, 450), 8, width=0.3)
    text((57, 452), "A")
    page.draw_line((60, 442), (60, 60), width=0.2)
    text((400, 400), "일반 주석")
    return page


@pytest.fixture()
def settings(tmp_path: Path):
    return Settings(dsn="dummy", data_root=tmp_path / "data", import_roots=(tmp_path,))


def _parse(tmp_path, settings, build):
    path = tmp_path / "drawing.pdf"
    doc = pymupdf.open()
    build(doc)
    doc.save(path)
    return parse_source(path, "doc_pdf", tmp_path / "data" / "out", settings, "A-101 1층 평면도.pdf")


def test_pdf_title_block_fields_match_dxf_schema(tmp_path, settings):
    result = _parse(tmp_path, settings, _drawing_page)
    blocks = [o for o in result["objects"] if o["type"] == "TitleBlock"]
    assert len(blocks) == 1
    props = blocks[0]["properties"]
    assert props["drawingNumber"] == "A-101"
    assert props["drawingTitle"] == "1층 평면도"
    assert props["scale"] == "1/100"
    assert props["date"] == "2026.05"
    assert props["designer"].startswith("홍길동")
    assert props["classification"]["label"] == "TitleBlock"
    page = next(o for o in result["objects"] if o["type"] == "Page")
    assert page["properties"]["drawingNumber"] == "A-101"
    assert page["properties"]["title_block"] == blocks[0]["id"]
    assert page["properties"]["drawing_category"] != "기타"
    assert any(r["predicate"] == "hasTitleBlock" and r["subject"] == page["id"] for r in result["relations"])
    assert page["storey"] == "1F" and page["properties"]["storey_source"] == "title_block"
    assert {o["storey"] for o in result["objects"] if o["type"] in {"Wall", "Annotation"}} == {"1F"}


def test_pdf_vector_candidates(tmp_path, settings):
    result = _parse(tmp_path, settings, _drawing_page)
    counts = Counter(o["type"] for o in result["objects"])
    walls = [o for o in result["objects"] if o["type"] == "Wall"]
    assert len(walls) == 1, walls
    wall = walls[0]["properties"]
    assert wall["thickness_mm"] == pytest.approx(200, abs=2)
    assert wall["classification"]["confidence"] == pytest.approx(0.6)
    assert wall["classification"]["state"] == _state(0.6)
    assert walls[0]["state"] == "AI_INFERRED"
    dims = [o for o in result["objects"] if o["type"] == "Dimension"]
    assert [d["properties"]["measurement"] for d in dims] == [6000.0]
    grids = [o for o in result["objects"] if o["type"] == "Grid"]
    assert [g["properties"]["grid_label"] for g in grids] == ["A"]
    assert grids[0]["properties"]["grid_lines"]
    assert grids[0]["properties"]["classification"]["confidence"] == pytest.approx(0.8)
    assert result["metrics"]["pdf_walls"] == counts["Wall"] == 1
    assert counts["Annotation"] >= 1


def test_textless_page_warns_instead_of_failing(tmp_path, settings, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_paddle(name, *args, **kwargs):
        if name == "paddleocr":
            raise ImportError("paddleocr missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_paddle)

    def build(doc):
        _drawing_page(doc)
        blank = doc.new_page(width=842, height=595)
        blank.draw_line((10, 10), (500, 400))

    result = _parse(tmp_path, settings, build)
    pages = [o for o in result["objects"] if o["type"] == "Page"]
    assert len(pages) == 2
    assert pages[1]["properties"]["ocr_required"] is True
    assert result["metrics"]["pdf_textless_pages"] == 1
    assert any(w.startswith("OCR_REQUIRED") for w in result["warnings"])
    assert any(o["type"] == "TitleBlock" for o in result["objects"])

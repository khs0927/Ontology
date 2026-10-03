"""Regression tests for real-office DXF extraction hardening."""

from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aec_intelligence.classifier import normalize_room_name, room_from_text  # noqa: E402
from aec_intelligence.operational.config import Settings  # noqa: E402
from aec_intelligence.operational.parsers import normalize_drawing_number, parse_source  # noqa: E402


def _settings(tmp_path: Path) -> Settings:
    return Settings(dsn="dummy", data_root=tmp_path, import_roots=(tmp_path,))


def _parse(path: Path, tmp_path: Path):
    return parse_source(path, "doc_hardening", tmp_path / "out", _settings(tmp_path), path.name)


def test_room_name_variants_share_one_identity():
    assert normalize_room_name("침실 1") == "침실1"
    assert room_from_text("침실 1")["roomName"] == "침실1"
    assert room_from_text("작은 방")["roomName"] == "작은방"
    assert room_from_text("보일러 실")["roomName"] == "보일러실"


def test_drawing_number_normalization_ignores_paper_size():
    assert normalize_drawing_number("A101") == "A-101"
    assert normalize_drawing_number("S-201") == "S-201"
    assert normalize_drawing_number("sheet M_1203") == "M-1203"
    assert normalize_drawing_number("A1") is None


def test_nested_block_text_reaches_room_semantics_in_wcs(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    inner = doc.blocks.new("ROOM_LABEL")
    inner.add_text("침실 1", height=200).set_placement((100, 100))
    outer = doc.blocks.new("UNIT_A")
    outer.add_blockref("ROOM_LABEL", (1000, 1000))
    doc.modelspace().add_blockref("UNIT_A", (5000, 6000))
    path = tmp_path / "A-101_1층평면도.dxf"
    doc.saveas(path)

    result = _parse(path, tmp_path)
    spaces = [o for o in result["objects"]
              if o["type"] == "Space" and o["properties"].get("roomName") == "침실1"]
    assert len(spaces) == 1
    source_id = spaces[0]["properties"]["source_annotation"]
    source = next(o for o in result["objects"] if o["id"] == source_id)
    assert source["evidence"]["virtual_from_handle"]
    assert source["evidence"]["nested_depth"] >= 2
    assert source["bbox"]["min_x"] == pytest.approx(6100)
    assert source["bbox"]["min_y"] == pytest.approx(7100)
    assert result["metrics"]["nested_semantic_entities"] >= 2


def _title_block(doc):
    block = doc.blocks.new("TITLE_A1")
    block.add_lwpolyline([(0, 0), (1000, 0), (1000, 700), (0, 700)], close=True)
    block.add_attdef("DWG_NO", (50, 50), dxfattribs={"height": 30})
    block.add_attdef("TITLE", (50, 100), dxfattribs={"height": 30})
    return block


def test_two_title_blocks_create_two_sheet_views_and_assign_rooms(tmp_path: Path):
    doc = ezdxf.new("R2018", setup=True)
    _title_block(doc)
    msp = doc.modelspace()
    left = msp.add_blockref("TITLE_A1", (0, 0))
    left.add_auto_attribs({"DWG_NO": "A101", "TITLE": "1층 평면도"})
    right = msp.add_blockref("TITLE_A1", (2000, 0))
    right.add_auto_attribs({"DWG_NO": "A102", "TITLE": "2층 평면도"})
    msp.add_text("거실", height=80).set_placement((300, 350))
    msp.add_text("침실 1", height=80).set_placement((2300, 350))
    path = tmp_path / "package.dxf"
    doc.saveas(path)

    result = _parse(path, tmp_path)
    sheets = [o for o in result["objects"]
              if o["type"] == "View" and o["properties"].get("view_kind") == "sheet"]
    assert {o["properties"]["drawingNumber"] for o in sheets} == {"A-101", "A-102"}
    spaces = {o["properties"]["roomName"]: o for o in result["objects"] if o["type"] == "Space"}
    assert spaces["거실"]["properties"]["sheet_number"] == "A-101"
    assert spaces["침실1"]["properties"]["sheet_number"] == "A-102"
    assert result["metrics"]["sheet_views"] == 2


def test_database_and_worker_timeouts_are_configurable(monkeypatch, tmp_path: Path):
    pytest.importorskip("psycopg")
    from aec_intelligence.operational.db import Database

    monkeypatch.setenv("AEC_DB_STATEMENT_TIMEOUT_MS", "180000")
    monkeypatch.setenv("AEC_DB_CONNECT_TIMEOUT_SEC", "15")
    db = Database("postgresql://unused")
    assert db.statement_timeout_ms == 180000
    assert db.connect_timeout_sec == 15

    monkeypatch.setenv("AEC_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AEC_IMPORT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AEC_LEASE_SECONDS", "900")
    monkeypatch.setenv("AEC_MAX_ATTEMPTS", "5")
    settings = Settings.from_env()
    assert settings.lease_seconds == 900
    assert settings.max_attempts == 5

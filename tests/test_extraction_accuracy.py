"""Extraction accuracy on the Korean fixtures: rooms+areas, dimensions, storeys (scripts/eval_extraction.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("ezdxf")

from aec_intelligence.operational.parsers import storey_from_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eval_extraction", ROOT / "scripts" / "eval_extraction.py")
evalmod = importlib.util.module_from_spec(spec)
sys.modules["eval_extraction"] = evalmod
spec.loader.exec_module(evalmod)


@pytest.mark.parametrize("text,expected", [
    ("1층 평면도", "1층"), ("A-102_2층평면도_cp949", "2층"), ("지하2층 평면도", "B2"), ("B1F PLAN", "B1"),
    ("3F FLOOR PLAN", "3층"), ("옥탑층 평면도", "옥탑"), ("SCALE 1/100", None), ("정면도", None), ("PHOTO", None),
])
def test_storey_from_text(text, expected):
    assert storey_from_text(text) == expected


def test_every_scored_item_is_fully_extracted(tmp_path):
    evalmod.main([str(tmp_path / "report.json")])
    import json

    total = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))["total"]
    assert {item: v["accuracy"] for item, v in total.items()} == {item: 1.0 for item in evalmod.ITEMS}


def test_dimension_carries_measurement_and_override():
    import ezdxf

    from aec_intelligence.dxf import _normalize_entity

    doc = ezdxf.new()
    msp = doc.modelspace()
    plain = msp.add_linear_dim(base=(0, 2), p1=(0, 0), p2=(3300, 0)).render().dimension
    custom = msp.add_linear_dim(base=(0, 2), p1=(0, 0), p2=(2700, 0), text="CH=<>").render().dimension
    a, b = _normalize_entity(plain).properties, _normalize_entity(custom).properties
    assert a["measurement"] == pytest.approx(3300) and a["text"] == "3300" and "text_override" not in a
    assert b["text_override"] == "CH=<>" and b["text"] == "CH=2700"

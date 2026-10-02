import pytest

from aec_intelligence.classifier import normalize_storey, storey_from, storey_tokens


@pytest.mark.parametrize("text, expected", [
    ("1층 평면도", {"1F"}),
    ("지상 5층 평면도", {"5F"}),
    ("12F PLAN", {"12F"}),
    ("3FL_평면도_rev2", {"3F"}),
    ("지하1층 평면도", {"B1"}),
    ("B2F 주차장 평면도", {"B2"}),
    ("지붕 평면도", {"RF"}),
    ("옥탑층 평면도", {"RF"}),
    ("1층 및 지붕 평면도", {"1F", "RF"}),
    # Ranges, typical floors, drawing numbers, ceiling heights and steel sizes name no single storey.
    ("1~3층 평면도", set()),
    ("기준층 평면도", set()),
    ("A-201 단면도", set()),
    ("층고 3500", set()),
    ("H-300x150x6.5x9", set()),
])
def test_storey_tokens(text, expected):
    assert storey_tokens(text) == expected


def test_storey_from_skips_ambiguous_candidates_in_priority_order():
    assert storey_from(("title_block", "1~3층 평면도"), ("file_name", "2F 평면도.dwg")) == {
        "storey": "2F", "storey_source": "file_name", "storey_evidence": "2F 평면도.dwg"}
    assert storey_from(("title_block", "1층 및 지붕 평면도")) == {}
    assert storey_from(("title_block", ""), ("file_name", "배치도.dwg")) == {}


@pytest.mark.parametrize("value, expected", [
    ("2층", "2F"), ("2f", "2F"), ("2", "2F"), ("B1", "B1"), ("b1f", "B1"), ("지하2층", "B2"),
    ("Roof", "RF"), ("옥상", "RF"), ("  ", None), (None, None), ("PIT", "PIT"),
])
def test_normalize_storey_for_queries(value, expected):
    assert normalize_storey(value) == expected

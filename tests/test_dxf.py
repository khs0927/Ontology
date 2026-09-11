from pathlib import Path

import pytest

from aec_intelligence.classifier import classify
from aec_intelligence.dxf import DXFParser


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_fixture_dxf_parse_extracts_expected_counts_and_handles():
    result = DXFParser().parse(FIXTURE)
    assert result.dxf_version == "AC1015"
    assert result.units == "millimeters"
    assert result.counts["entity_count"] == 7
    assert result.counts["layer_count"] >= 5
    assert result.counts["text_count"] == 1
    assert result.counts["unsupported_count"] == 0
    assert {entity.handle for entity in result.entities} == {"A01", "A02", "A03", "A04", "A05", "A06", "A07"}


def test_classifier_keeps_low_confidence_objects_in_review_state():
    parsed = DXFParser().parse(FIXTURE)
    labels = {classify(entity)[0] for entity in parsed.entities}
    assert {"Wall", "Door", "Window", "Column", "Annotation"}.issubset(labels)
    unknown = next(entity for entity in parsed.entities if entity.entity_type == "CIRCLE")
    assert classify(unknown)[1].state == "ACCEPT_WITH_WARNING"  # A-COLS is an explicit evidence signal.


def test_missing_dxf_is_a_hard_failure():
    with pytest.raises(FileNotFoundError):
        DXFParser().parse(FIXTURE.with_name("missing.dxf"))


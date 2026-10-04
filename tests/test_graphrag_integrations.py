"""Cross-repo contracts (ArchOntos subject refs / rule export, hs-steel-cad section catalog). Pure, no DB."""

import json

import pytest

from aec_intelligence.operational.graphrag.integrations import (
    SECTION_HANDOFF_SCHEMA,
    catalog_spec_key,
    load_rules,
    load_section_handoffs,
    match_section,
    parser_revision_id,
    subject_ref,
    validate_subject_ref,
)
from aec_intelligence.operational.graphrag.resolve import canonical_section

H = "a" * 64


@pytest.mark.parametrize("spec,shape,family,key", [
    ("[150x75x6.5x10", "[", "CHANNEL", "C-150x75x6.5x10"),
    ("ㅁ150x150x6", "ㅁ", "SQ-PIPE", "SHS-150x150x6"),
    ("ㅁ40x20x2.3", "ㅁ", "SQ-PIPE", "RHS-40x20x2.3"),
    ("Φ60.5x3.2", "%%C", "STEEL-PIPE", "PIPE-60.5x3.2"),
    ("F50x6", "F", "FLAT-BAR", "FB-6x50"),
    ("H100x50x5x7", "H", "H-BEAM", "H-100x50x5x7"),
    ("L25x25x3", "L", "ANGLE", "L-25x25x3"),
    ("PL-9x350", "PL-", "PLATE", "PL-9x350"),
])
def test_catalog_spec_key_matches_drawing_canonical_form(spec, shape, family, key):
    assert catalog_spec_key(spec, shape, family) == key


def test_plates_and_flat_bars_are_thickness_first():
    assert canonical_section("PL-300x20") == canonical_section("PL-20x300") == "PL-20x300"
    assert canonical_section("FB-50x2.3") == "FB-2.3x50"
    assert canonical_section("H-300x150x6.5x9") == "H-300x150x6.5x9"  # other families keep their order


def test_match_section_exact_nominal_computed_and_ambiguous():
    cat = {"H-350x350x12x19": {"spec": "H350x350x12x19"}, "H-400x200x8x13": {"spec": "a"},
           "H-400x200x9x14": {"spec": "b"}, "L-50x50x6": {"spec": "L50x50x6"}}
    assert match_section("L-50x50x6", cat)[1] == "exact"
    entry, how = match_section("H-350x350", cat)
    assert how == "nominal" and entry["spec"] == "H350x350x12x19"
    assert match_section("H-400x200", cat) == (None, None)  # two candidates -> never guess
    assert match_section("H-400x200x8", cat)[0]["spec"] == "a"  # flange thickness omitted, unique
    assert match_section("H-350", cat) == (None, None)  # a single dimension is never a section
    plate, how = match_section("PL-20x300", cat)
    assert how == "computed" and plate["unit_weight_kg_m"] == pytest.approx(47.1)
    assert match_section("PIPE-50", cat) == (None, None)  # nominal pipe size: no OD -> no match


def test_section_handoff_is_validated(tmp_path):
    good = {"schema": SECTION_HANDOFF_SCHEMA, "family": "H-BEAM", "validation_status": "PASS",
            "source_sha256": "b" * 64, "contract_digest": "c" * 64,
            "rows": [{"spec": "H100x50x5x7", "shape": "H", "dimensions_mm": [100, 50, 5, 7, 8, 0],
                      "unit_weight_kg_m": 9.3, "paint_area_m2_m": 0.4, "family": "H-BEAM"}]}
    (tmp_path / "h.json").write_text(json.dumps(good), encoding="utf-8")
    bad = dict(good, validation_status="FAIL", rows=[dict(good["rows"][0], spec="H200x100x5.5x8")])
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    (tmp_path / "other.json").write_text('{"schema": "something-else/1"}', encoding="utf-8")
    cat = load_section_handoffs(tmp_path)
    assert list(cat) == ["H-100x50x5x7"]
    assert cat["H-100x50x5x7"]["source_sha256"] == "b" * 64 and cat["H-100x50x5x7"]["source"] == "hs-steel-cad"


def test_subject_ref_matches_archontos_contract():
    ref = subject_ref("P-1", object_id="obj1", document={"id": "doc1", "revision": 2, "source_hash": H})
    assert validate_subject_ref(ref) == []
    assert ref["source_id"] == ref["source_byte_revision_id"] == H
    assert ref["parser_revision_id"] == parser_revision_id("doc1", 2, H) != parser_revision_id("doc1", 3, H)
    no_hash = subject_ref("P-1", document={"id": "doc1", "source_hash": None})
    assert "source_id" not in no_hash and validate_subject_ref(no_hash) == []
    assert validate_subject_ref({"project_id": "P", "source_id": H})  # trio must come together
    assert validate_subject_ref({"project_id": "P", "source_id": "X" * 64, "source_byte_revision_id": H,
                                 "parser_revision_id": H})
    assert validate_subject_ref({"schema": "other/1", "project_id": "P"})
    assert validate_subject_ref({"project_id": ""})


def test_load_rules_accepts_export_and_legacy_and_reports_bad_refs(tmp_path):
    export = {"schema": "archontos-rule-export/1", "rules": [
        {"rule_id": "R1", "version_label": "2024-01", "title": "직통계단", "logic_expr": {"rule": {}},
         "applies_to": [{"schema": "archontos-aec-subject-ref/1", "project_id": "P"},
                        {"project_id": "P", "source_id": H}]},
        {"rule_id": "R2", "space_uses": ["계단실 "]},
        {"title": "no id"}]}
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    rules, problems = load_rules(path)
    assert [r["id"] for r in rules] == ["R1", "R2"]
    assert len(rules[0]["subjects"]) == 1 and rules[1]["space_uses"] == ["계단실"]
    assert any("applies_to[1]" in p for p in problems) and any("rule_id missing" in p for p in problems)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps([{"id": "L1", "applies_to": ["복도"]}], ensure_ascii=False), encoding="utf-8")
    assert load_rules(legacy)[0][0]["space_uses"] == ["복도"]
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"schema": "x/9", "rules": []}', encoding="utf-8")
    assert load_rules(wrong) == ([], ["unsupported rules schema 'x/9'"])

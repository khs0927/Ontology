"""Space promotion, layer conventions and geometric relations on the Korean drawing set."""

from collections import Counter
from pathlib import Path

import pytest

from aec_intelligence.cair import CAIRSnapshot
from aec_intelligence.classifier import refine_with_context, to_cair_object
from aec_intelligence.dxf import DXFParser
from aec_intelligence.ontology import snapshot_to_turtle
from aec_intelligence.ontology_model import PROPERTY_NAMES, undeclared_terms
from aec_intelligence.spatial_relations import build_spatial_relations, storey_from_sheet

FIXTURES = Path(__file__).parent / "fixtures" / "drawings_ko"


def _sheet(prefix: str):
    paths = sorted(FIXTURES.glob(f"{prefix}_*.dxf"))
    if not paths:
        pytest.skip("Korean fixture set not present")
    parsed = DXFParser().parse(paths[0])
    objects = [to_cair_object(entity, f"T-{prefix}", str(paths[0]), "0" * 64) for entity in parsed.entities]
    refine_with_context(objects, parsed.entities)
    derived, relations = build_spatial_relations(objects, parsed.entities, f"T-{prefix}", parsed.sheet, parsed.units)
    return parsed, objects, derived, relations


def test_room_labels_become_spaces_with_area_storey_and_sheet():
    _, objects, derived, relations = _sheet("A-101")
    spaces = [obj for obj in objects if obj.type == "Space"]
    assert len(spaces) == 9
    assert all(obj.classification.label == "Space" and obj.properties["roomName"] for obj in spaces)
    by_name = {obj.properties["roomName"]: obj for obj in spaces}
    assert by_name["거실"].properties["area"] == 27.5
    storey = next(obj for obj in derived if obj.type == "Storey")
    assert storey.properties["storeyName"] == "1F"
    assert any(obj.type == "Sheet" for obj in derived)
    counts = Counter(relation.predicate for relation in relations)
    assert counts["hasSpace"] == counts["onStorey"] == 9
    assert counts["hostedBy"] >= 14
    assert {relation.predicate for relation in relations} <= PROPERTY_NAMES
    ids = {obj.id: obj for obj in objects}
    contained = {ids[r.object].properties.get("block_name"): ids[r.subject].properties["roomName"]
                 for r in relations if r.predicate == "containsElement" and r.subject in ids}
    # Bed in the bedroom, WC fixture in the WC; the TOILET east of the 거실 wall line goes to 주방, not 거실.
    assert contained["BED_Q"] == "침실1" and contained["변기"] == "화장실" and contained["TOILET"] == "주방"


def test_openings_are_hosted_by_walls_only():
    _, objects, _, relations = _sheet("A-101")
    types = {obj.id: obj.type for obj in objects}
    hosted = [r for r in relations if r.predicate == "hostedBy"]
    assert hosted and all(types[r.subject] in {"Door", "Window"} and types[r.object] == "Wall" for r in hosted)
    assert all(r.provenance["method"] == "opening_point_on_wall_segment" for r in hosted)


def test_layer_conventions_and_steel_outlines():
    _, objects, _, _ = _sheet("S-301")
    steel = [obj for obj in objects if obj.source.layer == "S-STEEL"]
    assert steel and all(obj.type == "SteelSection" and obj.properties["sectionDesignation"] for obj in steel)
    assert {obj.properties["sectionDesignation"] for obj in steel} >= {"H-300x300x10x15", "L-75x75x6"}
    _, elevation, _, _ = _sheet("A-201")
    layers = {obj.source.layer: obj.type for obj in elevation}
    assert layers["A-ELEV-OTLN"] == "Wall" and layers["A-ELEV-GRND"] == "Annotation"
    _, section, _, _ = _sheet("A-301")
    hatch = next(obj for obj in section if obj.source.layer == "A-HATCH")
    assert hatch.type == "Slab" and hatch.classification.method == "hatch_fill_of_outline"


def test_turtle_export_declares_spaces_and_data_properties():
    _, objects, derived, relations = _sheet("A-101")
    turtle = snapshot_to_turtle(CAIRSnapshot("T-A-101", objects + derived, relations))
    assert turtle.count("a aec:Space ;") == 9
    assert 'aec:area "27.5"^^xsd:decimal' in turtle and 'aec:roomName "거실"' in turtle
    assert undeclared_terms(turtle) == {"classes": [], "predicates": []}


def test_storey_parsing():
    assert storey_from_sheet({"category": "plan", "title": "지하2층 평면도"})["name"] == "B2"
    assert storey_from_sheet({"category": "plan", "title": "2층 평면도"})["name"] == "2F"
    assert storey_from_sheet({"category": "elevation", "title": "정면도"}) is None

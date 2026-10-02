from pathlib import Path

import pytest

from aec_intelligence.classifier import RULES
from aec_intelligence.compliance import audit_repository
from aec_intelligence.formats import GIS_SEMANTIC_MAP, IFC_RELATION_PREDICATES, IFC_SEMANTIC_TYPES
from aec_intelligence.ontology_alignment import ONTOLOGY_FILES
from aec_intelligence.ontology_model import CLASS_NAMES, CLASSES, MODULE_FILES, PROPERTIES, PROPERTY_NAMES, undeclared_terms
from aec_intelligence.pipeline import DXFIngestionPipeline


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


def test_every_emitted_object_type_is_declared():
    emitted = {label for label, *_ in RULES}
    emitted |= {"Annotation", "Dimension", "CADEntity", "Document", "View", "Page", "IFCEntity", "GISFeature"}
    emitted |= set(IFC_SEMANTIC_TYPES.values()) | set(GIS_SEMANTIC_MAP.values())
    assert emitted - CLASS_NAMES == set()


def test_every_emitted_relation_predicate_is_declared():
    emitted = set(IFC_RELATION_PREDICATES.values()) | {"contains", "containsElement", "relatedTo"}
    assert emitted - PROPERTY_NAMES == set()


def test_vocabulary_references_resolve_and_names_are_unique():
    assert len(CLASS_NAMES) == len(CLASSES)
    assert len(PROPERTY_NAMES) == len(PROPERTIES)
    for term in CLASSES:
        assert term.parent is None or term.parent in CLASS_NAMES, term
        assert term.label_ko, term
    for prop in PROPERTIES:
        for ref in (prop.domain, prop.range):
            assert ref is None or ref.startswith("xsd:") or ref in CLASS_NAMES, prop
        assert prop.parent is None or prop.parent in PROPERTY_NAMES, prop
        assert prop.inverse is None or prop.inverse in PROPERTY_NAMES, prop


def test_rendered_modules_are_valid_turtle():
    rdflib = pytest.importorskip("rdflib")
    for path in MODULE_FILES.values():
        graph = rdflib.Graph().parse(data=ONTOLOGY_FILES[path], format="turtle")
        assert len(graph) > 0
    owl = rdflib.Namespace("http://www.w3.org/2002/07/owl#")
    building = rdflib.Graph().parse(data=ONTOLOGY_FILES[MODULE_FILES["building"]], format="turtle")
    assert (rdflib.URIRef("https://example.org/aec#SteelMember"), rdflib.RDF.type, owl.Class) in building


def test_dxf_export_uses_only_declared_terms_and_audit_records_it(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-VOCAB")
    export = tmp_path / "projects" / "AEC-VOCAB" / "04_ONTOLOGY" / "project.ttl"
    assert undeclared_terms(export.read_text(encoding="utf-8")) == {"classes": [], "predicates": []}
    assert audit_repository(tmp_path).checks["ontology_exports_use_declared_terms"]


def test_undeclared_terms_are_reported_as_audit_warning(tmp_path: Path):
    DXFIngestionPipeline(tmp_path).ingest(FIXTURE, "AEC-VOCAB-GAP")
    export = tmp_path / "projects" / "AEC-VOCAB-GAP" / "04_ONTOLOGY" / "project.ttl"
    export.write_text(export.read_text(encoding="utf-8").replace("a aec:Wall", "a aec:Wal"), encoding="utf-8")
    audit = audit_repository(tmp_path)
    assert not audit.checks["ontology_exports_use_declared_terms"]
    assert any("Wal" in warning for warning in audit.warnings)


STEEL = """@prefix aec: <https://example.org/aec#> .
<aec://object/b1> a aec:SteelMember ;
    aec:projectId "P" ;
    aec:sourceFile "s.dxf" ;
    aec:classificationConfidence 0.9 ;
    aec:classificationState "ACCEPT_WITH_WARNING" ;
    aec:hasGeometry <aec://geometry/b1> {section} .
<aec://section/h400> a aec:SteelSection ; aec:sectionDesignation "{designation}" .
<aec://object/b1> aec:onStorey <aec://object/{storey}> .
<aec://object/s1> a aec:Storey .
<aec://object/w1> a aec:Wall .
"""


def _violation_paths(data):
    from aec_intelligence.shacl import validate_turtle

    report = validate_turtle(data)
    return report.conforms, {row["path"].rsplit("#", 1)[-1] for row in report.violations}


def test_steel_member_with_valid_section_and_storey_conforms():
    pytest.importorskip("pyshacl")
    conforms, paths = _violation_paths(STEEL.format(section="; aec:hasSection <aec://section/h400>", designation="H-400x200x8x13", storey="s1"))
    assert conforms, paths


def test_steel_shapes_reject_missing_section_bad_designation_and_wrong_storey():
    pytest.importorskip("pyshacl")
    conforms, paths = _violation_paths(STEEL.format(section="", designation="400 beam", storey="w1"))
    assert not conforms
    assert {"hasSection", "sectionDesignation", "onStorey"} <= paths

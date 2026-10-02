"""Single source of truth for the AEC ontology vocabulary.

Classes and properties are declared once here and rendered to OWL Turtle files
under ``global/ontology``. Everything the parsers emit as a CAIR object type or
relation predicate must be declared here; ``undeclared_terms`` and the audit
gate check exported project graphs against it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


AEC_NS = "https://example.org/aec#"


@dataclass(frozen=True)
class Term:
    name: str
    parent: str | None
    label_ko: str
    comment: str
    module: str


@dataclass(frozen=True)
class Property:
    name: str
    kind: str  # "object" or "data"
    domain: str | None
    range: str | None
    label_ko: str
    comment: str
    module: str
    characteristics: tuple[str, ...] = ()
    inverse: str | None = None
    parent: str | None = None


def _c(name, parent, ko, comment, module):
    return Term(name, parent, ko, comment, module)


CLASSES: tuple[Term, ...] = (
    # core
    _c("CAIRObject", None, "CAIR 객체", "Any object in a Canonical AEC Intermediate Representation snapshot.", "core"),
    _c("Project", "CAIRObject", "프로젝트", "A design project; the root of a CAIR snapshot.", "core"),
    _c("IFCEntity", "CAIRObject", "IFC 미분류 엔티티", "IFC product whose type has no semantic mapping yet.", "core"),
    _c("CADEntity", "CAIRObject", "CAD 미분류 엔티티", "CAD entity that no rule classified; kept for evidence.", "core"),
    # building: spatial structure
    _c("SpatialElement", "CAIRObject", "공간 요소", "Spatial container (site, building, storey, space, zone).", "building"),
    _c("Site", "SpatialElement", "대지", "Site containing one or more buildings.", "building"),
    _c("Building", "SpatialElement", "건물", "A building.", "building"),
    _c("Storey", "SpatialElement", "층", "A building storey / level.", "building"),
    _c("Space", "SpatialElement", "실", "A room or bounded space.", "building"),
    _c("Zone", "SpatialElement", "구역", "A grouping of spaces (fire zone, unit, department).", "building"),
    # building: elements
    _c("Element", "CAIRObject", "건축 부재", "Physical building element.", "building"),
    _c("Wall", "Element", "벽", "Wall.", "building"),
    _c("Door", "Element", "문", "Door.", "building"),
    _c("Window", "Element", "창", "Window.", "building"),
    _c("Slab", "Element", "슬래브", "Floor or roof slab.", "building"),
    _c("Roof", "Element", "지붕", "Roof.", "building"),
    _c("Stair", "Element", "계단", "Stair.", "building"),
    _c("Ramp", "Element", "경사로", "Ramp.", "building"),
    _c("Opening", "Element", "개구부", "Opening in a host element.", "building"),
    _c("Furniture", "Element", "가구", "Furniture.", "building"),
    _c("BuildingElementProxy", "Element", "기타 부재", "Element without a more specific type.", "building"),
    _c("StructuralMember", "Element", "구조 부재", "Load-bearing member.", "building"),
    _c("Column", "StructuralMember", "기둥", "Column.", "building"),
    _c("Beam", "StructuralMember", "보", "Beam or girder.", "building"),
    _c("Brace", "StructuralMember", "가새", "Bracing member.", "building"),
    _c("Foundation", "StructuralMember", "기초", "Footing or foundation.", "building"),
    # building: steel
    _c("SteelMember", "StructuralMember", "철골 부재", "Rolled or built-up steel member; has a section designation.", "building"),
    _c("SteelPlate", "Element", "강판", "Steel plate (base plate, gusset, stiffener).", "building"),
    _c("Bolt", "Element", "볼트", "Bolt or anchor bolt.", "building"),
    _c("Connection", "Element", "접합부", "Joint connecting structural members.", "building"),
    _c("SteelSection", None, "형강 단면", "Section profile, e.g. H-400x200x8x13.", "building"),
    _c("Material", None, "재료", "Material (e.g. SS275, C24).", "building"),
    # GIS
    _c("GISFeature", "CAIRObject", "GIS 피처", "Geospatial feature without a more specific type.", "building"),
    _c("Parcel", "GISFeature", "필지", "Land parcel.", "building"),
    _c("Road", "GISFeature", "도로", "Road or street.", "building"),
    _c("WaterBody", "GISFeature", "수체", "Water body.", "building"),
    _c("OpenSpace", "GISFeature", "오픈스페이스", "Park or open space.", "building"),
    _c("Vegetation", "GISFeature", "식생", "Tree or vegetation.", "building"),
    # drawing
    _c("Document", "CAIRObject", "도면 파일", "Source document (DWG/DXF/PDF/IFC file).", "drawing"),
    _c("Sheet", "CAIRObject", "시트", "A drawing sheet (paper-space layout with a title block).", "drawing"),
    _c("View", "CAIRObject", "뷰", "A layout or view inside a document.", "drawing"),
    _c("Page", "CAIRObject", "페이지", "A page of a PDF/SVG reference document.", "drawing"),
    _c("Revision", "CAIRObject", "개정", "A revision of a document.", "drawing"),
    _c("TitleBlock", "CAIRObject", "표제란", "Title block carrying drawing number, scale, date.", "drawing"),
    _c("Layer", "CAIRObject", "레이어", "CAD layer.", "drawing"),
    _c("BlockDefinition", "CAIRObject", "블록", "CAD block definition.", "drawing"),
    _c("Annotation", "CAIRObject", "주석", "Text or leader annotation.", "drawing"),
    _c("Dimension", "Annotation", "치수", "Dimension annotation.", "drawing"),
    _c("Grid", "CAIRObject", "그리드", "Structural grid line or axis.", "drawing"),
)


def _p(name, kind, domain, range_, ko, comment, module, characteristics=(), inverse=None, parent=None):
    return Property(name, kind, domain, range_, ko, comment, module, tuple(characteristics), inverse, parent)


PROPERTIES: tuple[Property, ...] = (
    # core data properties written by the Turtle exporter
    _p("projectId", "data", None, "xsd:string", "프로젝트 ID", "Owning project id.", "core"),
    _p("cairSnapshot", "data", "Project", "xsd:string", "CAIR 스냅샷", "Snapshot id the project graph came from.", "core"),
    _p("schemaVersion", "data", "Project", "xsd:string", "스키마 버전", "CAIR schema version.", "core"),
    _p("sourceHandle", "data", "CAIRObject", "xsd:string", "원본 핸들", "CAD handle or IFC GlobalId in the source file.", "core"),
    _p("sourceLayer", "data", "CAIRObject", "xsd:string", "원본 레이어", "Layer name in the source file.", "core"),
    _p("sourceFile", "data", None, "xsd:string", "원본 파일", "Source file path.", "core"),
    _p("sourceHash", "data", None, "xsd:string", "원본 해시", "SHA-256 of the source file.", "core"),
    _p("classificationConfidence", "data", "CAIRObject", "xsd:decimal", "분류 신뢰도", "Classifier confidence in [0,1].", "core"),
    _p("classificationState", "data", "CAIRObject", "xsd:string", "분류 상태", "Review state of the classification.", "core"),
    _p("geometryRef", "object", "CAIRObject", None, "형상 참조", "Reference to external geometry.", "core"),
    _p("hasGeometry", "object", "CAIRObject", None, "형상", "Geometry node of the object.", "core", parent="geometryRef"),
    _p("derivedFrom", "object", None, None, "파생 원본", "Object or artifact this was derived from.", "core"),
    # generic relations already emitted by parsers
    _p("contains", "object", None, None, "포함", "Generic containment (document contains view, aggregate contains part).", "core"),
    _p("containsElement", "object", None, "CAIRObject", "요소 포함", "Project or spatial element contains an object.", "core", parent="contains"),
    _p("relatedTo", "object", None, None, "관련", "Untyped relation kept for evidence.", "core", ["owl:SymmetricProperty"]),
    # building
    _p("hasBuilding", "object", "Site", "Building", "건물 보유", "Site has building.", "building", parent="contains"),
    _p("hasStorey", "object", "Building", "Storey", "층 보유", "Building has storey.", "building", parent="contains"),
    _p("hasSpace", "object", "SpatialElement", "Space", "실 보유", "Storey or zone has space.", "building", parent="contains"),
    _p("onStorey", "object", "CAIRObject", "Storey", "소속 층", "Element or space is located on a storey.", "building", ["owl:FunctionalProperty"]),
    _p("adjacentTo", "object", "SpatialElement", "SpatialElement", "인접", "Spaces share a boundary.", "building", ["owl:SymmetricProperty"]),
    _p("bounds", "object", "Element", "Space", "경계", "Element bounds a space.", "building"),
    _p("hostedBy", "object", "Element", "Element", "호스트", "Opening, door or window is hosted by a wall or slab.", "building"),
    _p("connects", "object", "Connection", "StructuralMember", "접합 대상", "Connection joins members.", "building"),
    _p("hasSection", "object", "SteelMember", "SteelSection", "단면", "Member uses section profile.", "building"),
    _p("hasMaterial", "object", "CAIRObject", "Material", "재료", "Object is made of material.", "building"),
    _p("hasPropertySet", "object", "CAIRObject", None, "속성 집합", "IFC property set attached to an object.", "building"),
    _p("sectionDesignation", "data", "SteelSection", "xsd:string", "단면 표기", "Normalized designation, e.g. H-400x200x8x13.", "building"),
    _p("roomName", "data", "Space", "xsd:string", "실명", "Room name as written on the drawing.", "building"),
    _p("roomNumber", "data", "Space", "xsd:string", "실 번호", "Room number.", "building"),
    _p("area", "data", "Space", "xsd:decimal", "면적", "Area in square metres.", "building"),
    _p("elevation", "data", "Storey", "xsd:decimal", "레벨", "Storey elevation in metres.", "building"),
    # drawing
    _p("hasSheet", "object", "Document", "Sheet", "시트 보유", "Document has sheet.", "drawing", parent="contains"),
    _p("hasRevision", "object", "Document", "Revision", "개정 보유", "Document has revision.", "drawing"),
    _p("hasTitleBlock", "object", "Sheet", "TitleBlock", "표제란", "Sheet carries title block.", "drawing", ["owl:FunctionalProperty"]),
    _p("onLayer", "object", "CAIRObject", "Layer", "레이어", "Object drawn on layer.", "drawing"),
    _p("depicts", "object", "View", "CAIRObject", "표현", "View or sheet depicts an object.", "drawing", inverse="depictedIn"),
    _p("depictedIn", "object", "CAIRObject", "View", "표현된 뷰", "Object appears in view.", "drawing", inverse="depicts"),
    _p("drawingNumber", "data", "TitleBlock", "xsd:string", "도면 번호", "Drawing number, e.g. A-101.", "drawing"),
    _p("drawingTitle", "data", "TitleBlock", "xsd:string", "도면명", "Drawing title.", "drawing"),
    _p("scale", "data", "TitleBlock", "xsd:string", "축척", "Scale, e.g. 1/100.", "drawing"),
    _p("revisionLabel", "data", "Revision", "xsd:string", "개정 표기", "Revision label, e.g. R2.", "drawing"),
)

MODULE_FILES = {
    "core": "core/aec-core.ttl",
    "building": "building/aec-building.ttl",
    "drawing": "drawing/aec-drawing.ttl",
}

CLASS_NAMES = frozenset(term.name for term in CLASSES)
PROPERTY_NAMES = frozenset(prop.name for prop in PROPERTIES)

_HEADER = """@prefix aec: <https://example.org/aec#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""


def _q(text: str, lang: str | None = None) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"@{lang}' if lang else f'"{escaped}"'


def _ref(name: str) -> str:
    return name if ":" in name else f"aec:{name}"


def render_module(module: str) -> str:
    lines = [_HEADER, f"<https://example.org/aec/{module}> a owl:Ontology ;",
             f"    rdfs:label {_q('AEC ' + module + ' vocabulary', 'en')} .", ""]
    for term in (t for t in CLASSES if t.module == module):
        parts = [f"aec:{term.name} a owl:Class"]
        if term.parent:
            parts.append(f"rdfs:subClassOf {_ref(term.parent)}")
        parts += [f"rdfs:label {_q(term.label_ko, 'ko')}", f"rdfs:comment {_q(term.comment, 'en')}"]
        lines.append(" ;\n    ".join(parts) + " .")
    lines.append("")
    for prop in (p for p in PROPERTIES if p.module == module):
        kind = "owl:ObjectProperty" if prop.kind == "object" else "owl:DatatypeProperty"
        parts = [f"aec:{prop.name} a {', '.join((kind, *prop.characteristics))}"]
        if prop.parent:
            parts.append(f"rdfs:subPropertyOf {_ref(prop.parent)}")
        if prop.domain:
            parts.append(f"rdfs:domain {_ref(prop.domain)}")
        if prop.range:
            parts.append(f"rdfs:range {_ref(prop.range)}")
        if prop.inverse:
            parts.append(f"owl:inverseOf {_ref(prop.inverse)}")
        parts += [f"rdfs:label {_q(prop.label_ko, 'ko')}", f"rdfs:comment {_q(prop.comment, 'en')}"]
        lines.append(" ;\n    ".join(parts) + " .")
    return "\n".join(lines).rstrip() + "\n"


_TYPE_RE = re.compile(r"\ba\s+aec:([A-Za-z_][A-Za-z0-9_]*)")
_PRED_RE = re.compile(r"^\S+\s+aec:([A-Za-z_][A-Za-z0-9_]*)\s+<", re.MULTILINE)


def undeclared_terms(turtle: str) -> dict[str, list[str]]:
    """Class and relation names used in an exported project graph but not declared in the vocabulary."""
    classes = sorted({name for name in _TYPE_RE.findall(turtle) if name not in CLASS_NAMES})
    predicates = sorted({name for name in _PRED_RE.findall(turtle) if name not in PROPERTY_NAMES})
    return {"classes": classes, "predicates": predicates}

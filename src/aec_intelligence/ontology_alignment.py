"""Explicit, reviewable alignment between CAIR and external vocabularies."""

from __future__ import annotations

import json
from pathlib import Path


ONTOLOGY_FILES: dict[str, str] = {
    "core/aec-core.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:CAIRObject a rdfs:Class .
aec:Project a rdfs:Class ; rdfs:subClassOf aec:CAIRObject .
aec:geometryRef a rdfs:Property .
aec:classificationConfidence a rdfs:Property .
aec:sourceFile a rdfs:Property .
aec:derivedFrom a rdfs:Property .
""",
    "building/bot-alignment.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix bot: <https://w3id.org/bot#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:Site rdfs:subClassOf bot:Site .
aec:Building rdfs:subClassOf bot:Building .
aec:Storey rdfs:subClassOf bot:Storey .
aec:Space rdfs:subClassOf bot:Space .
aec:Wall rdfs:subClassOf bot:Element .
aec:Door rdfs:subClassOf bot:Element .
aec:Window rdfs:subClassOf bot:Element .
aec:Column rdfs:subClassOf bot:Element .
aec:Beam rdfs:subClassOf bot:Element .
aec:Slab rdfs:subClassOf bot:Element .
""",
    "geometry/omg-fog-alignment.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix omg: <https://w3id.org/omg#> .
@prefix fog: <https://w3id.org/fog#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:Geometry rdfs:subClassOf omg:Geometry .
aec:geometryRef rdfs:subPropertyOf omg:hasGeometry .
aec:CADGeometry rdfs:subClassOf aec:Geometry .
aec:CADGeometry fog:represents aec:CAIRObject .
""",
    "gis/geosparql-alignment.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix geo: <http://www.opengis.net/ont/geosparql#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:GISFeature rdfs:subClassOf geo:Feature .
aec:GISGeometry rdfs:subClassOf geo:Geometry .
aec:geometryRef rdfs:subPropertyOf geo:hasGeometry .
aec:crs rdfs:subPropertyOf geo:coordinateReferenceSystem .
""",
    "provenance/prov-o-alignment.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:Provenance rdfs:subClassOf prov:Entity .
aec:derivedFrom rdfs:subPropertyOf prov:wasDerivedFrom .
aec:sourceFile rdfs:subPropertyOf prov:value .
""",
    "classification/bsdd-alignment.ttl": """@prefix aec: <https://example.org/aec#> .
@prefix bsdd: <https://identifier.buildingsmart.org/uri/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

aec:Classification a rdfs:Class .
aec:bsddClassUri a rdfs:Property .
aec:bsddClassUri rdfs:seeAlso bsdd: .
""",
}


def write_ontology_alignment(repository_root: str | Path, overwrite: bool = False) -> list[Path]:
    """Materialize only the small alignment layer; external ontologies stay external."""
    root = Path(repository_root).resolve() / "global" / "ontology"
    paths: list[Path] = []
    for relative, content in ONTOLOGY_FILES.items():
        path = root / relative
        if overwrite or not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content.strip() + "\n", encoding="utf-8")
        paths.append(path)
    manifest_path = root / "alignment-manifest.json"
    if overwrite or not manifest_path.exists():
        manifest_path.write_text(json.dumps(alignment_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths.append(manifest_path)
    return paths


def alignment_manifest() -> dict[str, object]:
    return {
        "schema_version": "0.1.0",
        "policy": "CAIR remains canonical; alignments are reviewable mappings, not source geometry storage",
        "vocabularies": {
            "BOT": "https://w3id.org/bot#",
            "OMG": "https://w3id.org/omg#",
            "FOG": "https://w3id.org/fog#",
            "GeoSPARQL": "http://www.opengis.net/ont/geosparql#",
            "PROV-O": "http://www.w3.org/ns/prov#",
            "bSDD": "https://identifier.buildingsmart.org/uri/",
        },
        "files": sorted(ONTOLOGY_FILES),
    }

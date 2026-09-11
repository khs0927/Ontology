from pathlib import Path
import json

import pytest

from aec_intelligence.formats import AdapterUnavailable, GISParseResult, GISParser, IFCParseResult, IFCParser, NormalizedGISFeature, normalize_gis_to_cair, normalize_ifc_to_cair
from aec_intelligence.graph import snapshot_to_jsonld, write_graph_exports
from aec_intelligence.query import HybridQueryRouter
from aec_intelligence.cair import CAIRSnapshot


def test_geojson_parser_preserves_geometry_and_crs(tmp_path: Path):
    path = tmp_path / "site.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:5179"}}, "features": [{"type": "Feature", "id": "parcel-1", "geometry": {"type": "Polygon", "coordinates": [[[1, 2], [3, 2], [3, 4], [1, 2]]]}, "properties": {"kind": "parcel"}}]}), encoding="utf-8")
    result = GISParser().parse(path)
    assert result.crs == "EPSG:5179"
    assert result.features[0].feature_id == "parcel-1"
    assert result.features[0].bbox == [1.0, 2.0, 3.0, 4.0]


def test_ifc_adapter_reports_missing_dependency_or_missing_file():
    with pytest.raises((AdapterUnavailable, FileNotFoundError)):
        IFCParser().parse(Path("missing.ifc"))


def test_ifc_normalizer_maps_schema_types_and_relationships_to_cair():
    result = IFCParseResult(
        "model.ifc",
        "IFC4",
        entities=[
            {"id": 1, "global_id": "project-guid", "ifc_type": "IfcProject", "name": "Demo"},
            {"id": 2, "global_id": "wall-guid", "ifc_type": "IfcWall", "name": "Wall 1"},
        ],
        relationships=[{"subject": "project-guid", "predicate": "contains", "object": "wall-guid", "ifc_relation": "IfcRelAggregates"}],
    )
    snapshot = normalize_ifc_to_cair(result, "P-IFC", source_hash="abc")
    assert {obj.type for obj in snapshot.objects} == {"Project", "Wall"}
    assert snapshot.objects[1].id.startswith("aec://project/P-IFC/")
    assert snapshot.objects[1].geometry_ref.startswith("ifc://project/P-IFC/")
    assert snapshot.relations[0].predicate == "contains"
    assert snapshot.objects[1].provenance.source_format == "IFC"


def test_ifc_fixture_is_authoritatively_parsed_and_normalized():
    result = IFCParser().parse(Path(__file__).parents[1] / "fixtures" / "simple_house.ifc")
    snapshot = normalize_ifc_to_cair(result, "P-IFC-FIXTURE")
    assert result.schema == "IFC4"
    assert len(result.relationships) == 4
    assert result.geometry_context_count == 0
    assert result.product_representation_count == 0
    assert result.shape_representation_count == 0
    assert snapshot.metadata["geometry"]["representation_context_count"] == 0
    assert {obj.type for obj in snapshot.objects} == {"Project", "Site", "Building", "Storey", "Space", "Wall", "Door", "Window"}
    assert len(snapshot.relations) == 4
    assert snapshot.status == "SUCCESS"


def test_ifc_geometry_fixture_extracts_mesh_outside_cair_objects():
    ground_truth = json.loads((Path(__file__).parents[1] / "fixtures" / "known-ground-truth.json").read_text(encoding="utf-8"))["ifc_geometry"]
    result = IFCParser().parse(Path(__file__).parents[1] / "fixtures" / "simple_house_geometry.ifc")
    snapshot = normalize_ifc_to_cair(result, "P-IFC-GEOMETRY")
    assert result.schema == ground_truth["schema"]
    assert result.geometry_context_count == ground_truth["geometry_context_count"]
    assert result.product_representation_count == ground_truth["product_representation_count"]
    assert result.shape_representation_count == ground_truth["shape_representation_count"]
    assert len(result.geometry_rows) == ground_truth["extracted_geometry_count"]
    row = result.geometry_rows[0]
    assert row["geometry"]["kind"] == "mesh"
    assert len(row["geometry"]["vertices"]) // 3 == ground_truth["mesh_vertex_count"]
    assert len(row["geometry"]["faces"]) // 3 == ground_truth["mesh_face_count"]
    assert snapshot.metadata["geometry"]["extracted_geometry_count"] == 1
    summary = result.to_dict()["geometry_rows"][0]
    assert summary["geometry"] == {"kind": "mesh", "vertex_count": 8, "face_count": 12}
    assert "vertices" not in summary["geometry"]


def test_geojson_fixture_is_normalized_with_external_geometry():
    path = Path(__file__).parents[1] / "fixtures" / "site.geojson"
    result = GISParser().parse(path)
    snapshot = normalize_gis_to_cair(result, "P-GIS-FIXTURE")
    assert result.crs == "EPSG:5179"
    assert {obj.type for obj in snapshot.objects} == {"Building", "Parcel"}
    assert all(obj.geometry_ref.startswith("gis://feature/") for obj in snapshot.objects)
    assert all("coordinates" not in obj.properties for obj in snapshot.objects)


def test_gis_normalizer_keeps_geometry_outside_cair_object():
    result = GISParseResult(
        "site.geojson",
        "EPSG:5179",
        [NormalizedGISFeature("parcel-1", "Polygon", {"type": "Polygon", "coordinates": [[[1, 2], [3, 2], [3, 4], [1, 2]]]}, {"kind": "parcel"}, [1, 2, 3, 4])],
    )
    snapshot = normalize_gis_to_cair(result, "P-GIS")
    obj = snapshot.objects[0]
    assert obj.type == "Parcel"
    assert obj.geometry_ref == "gis://feature/parcel-1"
    assert obj.bbox == {"min_x": 1.0, "min_y": 2.0, "max_x": 3.0, "max_y": 4.0}
    assert "coordinates" not in obj.properties
    assert result.geometry_index()[0]["geometry"]["type"] == "Polygon"


def test_graph_and_query_router_are_portable():
    snapshot = CAIRSnapshot("P1")
    graph = snapshot_to_jsonld(snapshot)
    assert graph["@context"]["aec"].startswith("https://")
    assert HybridQueryRouter().plan("계단과 가까운 출입문").route == "KNOWLEDGE_GRAPH"
    assert HybridQueryRouter().plan("면적 500m² 이상 공간").route == "STRUCTURED"


def test_graph_exports_include_drive_portable_interchange_formats(tmp_path: Path):
    snapshot = CAIRSnapshot("P-GRAPH-EXPORT")
    outputs = write_graph_exports(snapshot, tmp_path)
    assert {"jsonld", "nodes", "relationships", "nodes_csv", "relationships_csv", "graphml"} <= outputs.keys()
    assert all(path.is_file() for path in outputs.values())
    assert "id,project_id,type,geometry_ref,classification_json,properties_json" in (tmp_path / "nodes.csv").read_text(encoding="utf-8")
    assert "subject,predicate,object,confidence,provenance_json" in (tmp_path / "relationships.csv").read_text(encoding="utf-8")
    assert (tmp_path / "project.graphml").read_text(encoding="utf-8").startswith("<?xml")
    if "nodes_parquet" in outputs:
        assert "relationships_parquet" in outputs

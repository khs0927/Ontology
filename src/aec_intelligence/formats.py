"""Optional BIM/GIS adapters with portable normalized results."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .cair import CAIRObject, CAIRRelation, CAIRSnapshot, Classification, Provenance, SourceRef, stable_object_id


class AdapterUnavailable(RuntimeError):
    """Raised when a format adapter needs an optional package or external tool."""


@dataclass
class IFCParseResult:
    source_file: str
    schema: str
    entities: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    geometry_context_count: int = 0
    product_representation_count: int = 0
    shape_representation_count: int = 0
    geometry_rows: list[dict[str, Any]] = field(default_factory=list)
    parser: str = "IfcOpenShell"
    parser_version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        geometry_summaries = []
        for row in self.geometry_rows:
            geometry = row.get("geometry") or {}
            properties = dict(row.get("properties") or {})
            geometry_summaries.append({
                "source_id": row.get("source_id"),
                "bbox": row.get("bbox"),
                "geometry": {"kind": geometry.get("kind"), **{key: properties[key] for key in ("vertex_count", "face_count") if key in properties}},
                "properties": properties,
            })
        return {
            "source_file": self.source_file,
            "schema": self.schema,
            "entity_count": len(self.entities),
            "entities": self.entities,
            "relationship_count": len(self.relationships),
            "relationships": self.relationships,
            "geometry": {
                "representation_context_count": self.geometry_context_count,
                "product_representation_count": self.product_representation_count,
                "shape_representation_count": self.shape_representation_count,
                "extracted_geometry_count": len(self.geometry_rows),
            },
            # Keep parser responses lightweight; full mesh payloads stay in
            # the external geometry index and portable geometry table.
            "geometry_rows": geometry_summaries,
            "warnings": self.warnings,
            "parser": self.parser,
            "parser_version": self.parser_version,
        }


class IFCParser:
    name = "IfcOpenShell"

    def parse(self, path: str | Path) -> IFCParseResult:
        try:
            import ifcopenshell
        except ImportError as exc:
            raise AdapterUnavailable("IfcOpenShell is not installed; install the [bim] extra") from exc
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        model = ifcopenshell.open(str(source))
        entities = []
        for element in model:
            entity = {
                "id": int(element.id()),
                "ifc_type": element.is_a(),
                "global_id": getattr(element, "GlobalId", None),
                "name": getattr(element, "Name", None),
                "description": getattr(element, "Description", None),
                "object_type": getattr(element, "ObjectType", None),
                "predefined_type": getattr(element, "PredefinedType", None),
                "tag": getattr(element, "Tag", None),
            }
            entities.append(entity)
        relationships: list[dict[str, Any]] = []
        relationship_predicates = {
            "IfcRelAggregates": "contains",
            "IfcRelContainedInSpatialStructure": "contains",
            "IfcRelSpaceBoundary": "bounds",
            "IfcRelAssociatesMaterial": "hasMaterial",
            "IfcRelDefinesByProperties": "hasPropertySet",
        }
        for relation in model:
            relation_type = relation.is_a()
            predicate = relationship_predicates.get(relation_type)
            if not predicate:
                continue
            subject = getattr(relation, "RelatingObject", None) or getattr(relation, "RelatingStructure", None) or getattr(relation, "RelatingMaterial", None)
            related = getattr(relation, "RelatedObjects", None) or getattr(relation, "RelatedBuildingElement", None) or []
            if subject is None:
                continue
            if not isinstance(related, (list, tuple)):
                related = [related]
            for target in related:
                if target is not None:
                    relationships.append({"subject": str(getattr(subject, "GlobalId", None) or subject.id()), "predicate": predicate, "object": str(getattr(target, "GlobalId", None) or target.id()), "ifc_relation": relation_type})
        products = model.by_type("IfcProduct")
        product_representation_count = sum(1 for product in products if getattr(product, "Representation", None) is not None)
        geometry_rows: list[dict[str, Any]] = []
        geometry_warnings: list[str] = []
        represented_products = [product for product in products if getattr(product, "Representation", None) is not None]
        try:
            import ifcopenshell.geom

            settings = ifcopenshell.geom.settings()
            # Keep world placement and return coordinates in the IFC file's
            # declared units (rather than silently converting to metres).
            settings.set("use-world-coords", True)
            settings.set("convert-back-units", True)
            for product in represented_products:
                source_id = str(getattr(product, "GlobalId", None) or product.id())
                try:
                    shape = ifcopenshell.geom.create_shape(settings, product)
                    triangulation = shape.geometry
                    vertices = [float(value) for value in triangulation.verts]
                    faces = [int(value) for value in triangulation.faces]
                    if len(vertices) < 9 or len(faces) < 3:
                        geometry_warnings.append(f"IFC product {source_id} produced insufficient triangulated geometry")
                        continue
                    points = [[vertices[index], vertices[index + 1], vertices[index + 2]] for index in range(0, len(vertices), 3)]
                    geometry_rows.append({
                        "source_id": source_id,
                        "geometry": {"kind": "mesh", "vertices": vertices, "faces": faces},
                        "bbox": {
                            "min_x": min(point[0] for point in points),
                            "min_y": min(point[1] for point in points),
                            "min_z": min(point[2] for point in points),
                            "max_x": max(point[0] for point in points),
                            "max_y": max(point[1] for point in points),
                            "max_z": max(point[2] for point in points),
                        },
                        "properties": {
                            "geometry_type": "IfcOpenShellTriangulation",
                            "vertex_count": len(vertices) // 3,
                            "face_count": len(faces) // 3,
                            "source_units": "IFC declared units",
                        },
                    })
                except Exception as exc:
                    geometry_warnings.append(f"IFC geometry extraction failed for {source_id}: {exc}")
        except ImportError:
            if represented_products:
                geometry_warnings.append("IfcOpenShell geometry module is unavailable; semantic IFC data was retained without extracted geometry")
        return IFCParseResult(
            str(source),
            getattr(model, "schema", "unknown"),
            entities,
            relationships=relationships,
            geometry_context_count=len(model.by_type("IfcGeometricRepresentationContext")),
            product_representation_count=product_representation_count,
            shape_representation_count=len(model.by_type("IfcShapeRepresentation")),
            geometry_rows=geometry_rows,
            warnings=geometry_warnings,
        )

    def parse_to_cair(self, path: str | Path, project_id: str, artifact_id: str | None = None, source_hash: str | None = None) -> CAIRSnapshot:
        return normalize_ifc_to_cair(self.parse(path), project_id, artifact_id, source_hash)


IFC_SEMANTIC_TYPES = {
    "IfcProject": "Project",
    "IfcSite": "Site",
    "IfcBuilding": "Building",
    "IfcBuildingStorey": "Storey",
    "IfcSpace": "Space",
    "IfcZone": "Zone",
    "IfcWall": "Wall",
    "IfcWallStandardCase": "Wall",
    "IfcDoor": "Door",
    "IfcWindow": "Window",
    "IfcColumn": "Column",
    "IfcBeam": "Beam",
    "IfcSlab": "Slab",
    "IfcRoof": "Roof",
    "IfcStair": "Stair",
    "IfcRamp": "Ramp",
    "IfcFurniture": "Furniture",
    "IfcBuildingElementProxy": "BuildingElementProxy",
}


def normalize_ifc_to_cair(result: IFCParseResult, project_id: str, artifact_id: str | None = None, source_hash: str | None = None) -> CAIRSnapshot:
    """Map IFC schema objects to CAIR without embedding IFC mesh geometry.

    The IFC GlobalId (or stable numeric entity id fallback) is the source key.
    Geometry remains an external reference so the same CAIR object can be
    consumed by a graph, table, or geometry runtime.
    """
    objects: list[CAIRObject] = []
    source_to_cair: dict[str, str] = {}
    unknown_types: list[str] = []
    for entity in result.entities:
        ifc_type = str(entity.get("ifc_type") or "IfcEntity")
        # Relationship instances are represented in CAIR as CAIRRelation
        # records below, not as semantic building objects.
        if ifc_type.startswith("IfcRel"):
            continue
        source_id = str(entity.get("global_id") or entity.get("id"))
        semantic_type = IFC_SEMANTIC_TYPES.get(ifc_type, "IFCEntity")
        if semantic_type == "IFCEntity":
            unknown_types.append(ifc_type)
        object_id = stable_object_id(project_id, "IFCEntity", "IFC", source_id)
        source_to_cair[source_id] = object_id
        properties = {
            "ifc_type": ifc_type,
            "name": entity.get("name"),
            "description": entity.get("description"),
            "object_type": entity.get("object_type"),
            "predefined_type": entity.get("predefined_type"),
            "tag": entity.get("tag"),
        }
        properties = {key: value for key, value in properties.items() if value is not None}
        confidence = 1.0 if semantic_type != "IFCEntity" else 0.65
        classification = Classification(
            label=f"ifc:{ifc_type}",
            confidence=confidence,
            method="ifc-schema-type",
            evidence=(f"IFC entity declaration: {ifc_type}",),
            state="AUTO_ACCEPT" if confidence > 0.95 else "REQUIRES_REVIEW",
        )
        objects.append(CAIRObject(
            id=object_id,
            project_id=project_id,
            type=semantic_type,
            source=SourceRef(result.source_file, "IFC", source_id, artifact_id=artifact_id),
            geometry_ref=f"ifc://project/{project_id}/{source_id}",
            properties=properties,
            classification=classification,
            provenance=Provenance(result.source_file, source_id, "IFC", source_hash, result.parser, result.parser_version, agent="aec-ifc-ingest"),
        ))

    relations: list[CAIRRelation] = []
    for relation in result.relationships:
        subject = source_to_cair.get(str(relation.get("subject")))
        target = source_to_cair.get(str(relation.get("object")))
        if subject and target:
            relations.append(CAIRRelation(subject, str(relation.get("predicate") or "relatedTo"), target, 1.0, {"parser": result.parser, "ifc_relation": relation.get("ifc_relation")}))
    warnings = list(result.warnings)
    if unknown_types:
        warnings.append(f"unmapped IFC types require review: {sorted(set(unknown_types))}")
    return CAIRSnapshot(
        project_id=project_id,
        objects=objects,
        relations=relations,
        status="SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS",
        metadata={
            "source_format": "IFC",
            "schema": result.schema,
            "parser": result.parser,
            "parser_version": result.parser_version,
            "unmapped_ifc_types": sorted(set(unknown_types)),
            "geometry": {
                "representation_context_count": result.geometry_context_count,
                "product_representation_count": result.product_representation_count,
                "shape_representation_count": result.shape_representation_count,
                "extracted_geometry_count": len(result.geometry_rows),
            },
        },
    )


@dataclass
class NormalizedGISFeature:
    feature_id: str
    geometry_type: str | None
    geometry: dict[str, Any] | None
    properties: dict[str, Any]
    bbox: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"feature_id": self.feature_id, "geometry_type": self.geometry_type, "geometry": self.geometry, "properties": self.properties, "bbox": self.bbox}


@dataclass
class GISParseResult:
    source_file: str
    crs: str | None
    features: list[NormalizedGISFeature]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"source_file": self.source_file, "crs": self.crs, "feature_count": len(self.features), "features": [feature.to_dict() for feature in self.features], "warnings": self.warnings}

    def geometry_index(self) -> list[dict[str, Any]]:
        """Return geometry-bearing rows kept outside the semantic CAIR objects."""
        return [
            {
                "geometry_ref": f"gis://feature/{feature.feature_id}",
                "feature_id": feature.feature_id,
                "geometry_type": feature.geometry_type,
                "geometry": feature.geometry,
                "bbox": feature.bbox,
                "crs": self.crs,
            }
            for feature in self.features
        ]


def _coordinate_points(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    if value and isinstance(value[0], (int, float)):
        return [[float(value[0]), float(value[1]), float(value[2]) if len(value) > 2 else 0.0]]
    points: list[list[float]] = []
    for child in value:
        points.extend(_coordinate_points(child))
    return points


class GISParser:
    """Pure-JSON GeoJSON baseline; GDAL/GeoPandas can replace this adapter later."""

    name = "GeoJSON standard-library adapter"

    def parse(self, path: str | Path) -> GISParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() in {".gpkg", ".shp"}:
            return self._parse_with_geopandas(source)
        if source.suffix.lower() not in {".geojson", ".json"}:
            raise AdapterUnavailable("GeoJSON, GPKG, and SHP are supported; install the [gis] extra for GPKG/SHP")
        data = json.loads(source.read_text(encoding="utf-8"))
        crs_value = self._read_crs(data)
        features = data.get("features", []) if data.get("type") == "FeatureCollection" else [data]
        normalized = []
        for index, feature in enumerate(features):
            geometry = feature.get("geometry") or {}
            points = _coordinate_points(geometry.get("coordinates"))
            bbox = feature.get("bbox")
            if bbox is None and points:
                bbox = [min(point[0] for point in points), min(point[1] for point in points), max(point[0] for point in points), max(point[1] for point in points)]
            feature_id = feature.get("id", index)
            normalized.append(NormalizedGISFeature(str(feature_id), geometry.get("type"), geometry or None, feature.get("properties") or {}, bbox))
        return GISParseResult(str(source), crs_value, normalized)

    @staticmethod
    def _read_crs(data: dict[str, Any]) -> str | None:
        value = data.get("crs")
        if isinstance(value, dict):
            properties = value.get("properties") or {}
            return properties.get("name") or properties.get("href")
        return data.get("coordinateReferenceSystem") if isinstance(data.get("coordinateReferenceSystem"), str) else None

    def _parse_with_geopandas(self, source: Path) -> GISParseResult:
        try:
            import geopandas as gpd
        except ImportError as exc:
            raise AdapterUnavailable("GeoPandas/GDAL is not installed; install the [gis] extra for GPKG/SHP") from exc
        frame = gpd.read_file(source)
        crs_value = frame.crs.to_string() if frame.crs is not None else None
        features: list[NormalizedGISFeature] = []
        for index, row in frame.iterrows():
            geometry = row.geometry.__geo_interface__ if row.geometry is not None else None
            points = _coordinate_points((geometry or {}).get("coordinates"))
            bbox = [float(value) for value in row.geometry.bounds] if row.geometry is not None else None
            properties = {str(key): value for key, value in row.drop(labels=["geometry"]).items() if value is not None}
            features.append(NormalizedGISFeature(str(index), (geometry or {}).get("type"), geometry, properties, bbox))
        return GISParseResult(str(source), crs_value, features)

    def parse_to_cair(self, path: str | Path, project_id: str, artifact_id: str | None = None, source_hash: str | None = None) -> CAIRSnapshot:
        return normalize_gis_to_cair(self.parse(path), project_id, artifact_id, source_hash)


GIS_SEMANTIC_KEYS = ("type", "kind", "category", "class", "landuse", "building_type", "feature_type")
GIS_SEMANTIC_MAP = {
    "building": "Building",
    "parcel": "Parcel",
    "road": "Road",
    "street": "Road",
    "wall": "Wall",
    "door": "Door",
    "window": "Window",
    "water": "WaterBody",
    "park": "OpenSpace",
    "tree": "Vegetation",
}


def normalize_gis_to_cair(result: GISParseResult, project_id: str, artifact_id: str | None = None, source_hash: str | None = None) -> CAIRSnapshot:
    """Map GIS features to CAIR while keeping coordinates in the geometry index."""
    objects: list[CAIRObject] = []
    warnings = list(result.warnings)
    for feature in result.features:
        semantic_value: str | None = None
        semantic_key: str | None = None
        for key in GIS_SEMANTIC_KEYS:
            value = feature.properties.get(key)
            if value is not None:
                semantic_value = str(value)
                semantic_key = key
                break
        semantic_type = GIS_SEMANTIC_MAP.get((semantic_value or "").lower(), "GISFeature")
        if semantic_type == "GISFeature":
            confidence = 0.6
            method = "geometry-type"
            evidence = (f"geometry type: {feature.geometry_type or 'unknown'}",)
            state = "REQUIRES_REVIEW"
        else:
            confidence = 0.9
            method = "property-semantic"
            evidence = (f"property {semantic_key}={semantic_value}",)
            state = "ACCEPT_WITH_WARNING"
        object_id = stable_object_id(project_id, "GISFeature", "GEOJSON", feature.feature_id)
        bbox = {}
        if feature.bbox and len(feature.bbox) >= 4:
            bbox = {"min_x": float(feature.bbox[0]), "min_y": float(feature.bbox[1]), "max_x": float(feature.bbox[2]), "max_y": float(feature.bbox[3])}
        properties = {"geometry_type": feature.geometry_type, "source_crs": result.crs, **feature.properties}
        objects.append(CAIRObject(
            id=object_id,
            project_id=project_id,
            type=semantic_type,
            source=SourceRef(result.source_file, "GIS", feature.feature_id, artifact_id=artifact_id),
            geometry_ref=f"gis://feature/{feature.feature_id}",
            bbox=bbox,
            placement={"crs": result.crs} if result.crs else {},
            properties=properties,
            classification=Classification(f"gis:{semantic_value or feature.geometry_type or 'feature'}", confidence, method, evidence, state),
            provenance=Provenance(result.source_file, feature.feature_id, "GIS", source_hash, "GISParser", "0.1.0", agent="aec-gis-ingest"),
        ))
    return CAIRSnapshot(
        project_id=project_id,
        objects=objects,
        relations=[],
        status="SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS",
        metadata={"source_format": "GIS", "crs": result.crs, "geometry_index_count": len(result.features)},
    )

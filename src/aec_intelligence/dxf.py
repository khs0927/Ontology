"""DXF normalization adapter.

`ezdxf` is the authoritative parser when installed. The adapter returns a
format-neutral normalized entity model so later CAIR, ontology, and storage
layers do not depend on ezdxf objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Optional at architecture level; installed in the current environment.
    import ezdxf
except ImportError:  # pragma: no cover - exercised by dependency failure tests
    ezdxf = None


SUPPORTED_ENTITY_TYPES = {
    "LINE", "XLINE", "RAY", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE",
    "ELLIPSE", "SPLINE", "HATCH", "SOLID", "POINT", "TEXT", "MTEXT",
    "DIMENSION", "LEADER", "MLEADER", "INSERT", "3DFACE", "MESH", "3DSOLID",
}

INSUNITS = {
    0: "unitless", 1: "inches", 2: "feet", 3: "miles", 4: "millimeters",
    5: "centimeters", 6: "meters", 7: "kilometers", 8: "microinches",
    9: "mils", 10: "yards", 11: "angstroms", 12: "nanometers", 13: "microns",
    14: "decimeters", 15: "decameters", 16: "hectometers", 17: "gigameters",
    18: "astronomical-units", 19: "light-years", 20: "parsecs",
}


@dataclass
class NormalizedCADEntity:
    handle: str
    entity_type: str
    layer: str
    geometry: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    bbox: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "entity_type": self.entity_type,
            "layer": self.layer,
            "geometry": self.geometry,
            "properties": self.properties,
            "bbox": self.bbox,
        }


@dataclass
class DXFParseResult:
    source_file: str
    dxf_version: str
    units: str
    entities: list[NormalizedCADEntity]
    layers: list[str]
    blocks: list[str]
    extents: dict[str, float]
    unsupported_entity_types: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "entity_count": len(self.entities),
            "layer_count": len(self.layers),
            "block_count": len(self.blocks),
            "text_count": sum(e.entity_type in {"TEXT", "MTEXT"} for e in self.entities),
            "dimension_count": sum(e.entity_type in {"DIMENSION", "LEADER", "MLEADER"} for e in self.entities),
            "insert_count": sum(e.entity_type == "INSERT" for e in self.entities),
            "unsupported_count": len(self.unsupported_entity_types),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "dxf_version": self.dxf_version,
            "units": self.units,
            "counts": self.counts,
            "layers": self.layers,
            "blocks": self.blocks,
            "extents": self.extents,
            "unsupported_entity_types": self.unsupported_entity_types,
            "warnings": self.warnings,
        }


def _point(value: Any) -> list[float]:
    return [float(value[0]), float(value[1]), float(value[2]) if len(value) > 2 else 0.0]


def _bbox_from_points(points: list[list[float]]) -> dict[str, float]:
    if not points:
        return {}
    return {
        "min_x": min(p[0] for p in points),
        "min_y": min(p[1] for p in points),
        "min_z": min(p[2] if len(p) > 2 else 0.0 for p in points),
        "max_x": max(p[0] for p in points),
        "max_y": max(p[1] for p in points),
        "max_z": max(p[2] if len(p) > 2 else 0.0 for p in points),
    }


def _circle_bbox(center: list[float], radius: float) -> dict[str, float]:
    return _bbox_from_points([
        [center[0] - radius, center[1] - radius, center[2]],
        [center[0] + radius, center[1] + radius, center[2]],
    ])


def _safe_attr(entity: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(entity.dxf, name, default)
    except (AttributeError, TypeError):
        return default


def _normalize_entity(entity: Any) -> NormalizedCADEntity:
    entity_type = entity.dxftype()
    handle = str(_safe_attr(entity, "handle", "unknown"))
    layer = str(_safe_attr(entity, "layer", "0"))
    geometry: dict[str, Any] = {"kind": entity_type.lower()}
    properties: dict[str, Any] = {}
    bbox: dict[str, float] = {}

    if entity_type in {"LINE", "XLINE", "RAY"}:
        start = _point(_safe_attr(entity, "start", [0, 0, 0]))
        end = _point(_safe_attr(entity, "end", [0, 0, 0]))
        geometry.update({"start": start, "end": end})
        bbox = _bbox_from_points([start, end])
    elif entity_type == "LWPOLYLINE":
        raw = entity.get_points("xyseb")
        points = [[float(row[0]), float(row[1]), 0.0] for row in raw]
        geometry.update({"kind": "polyline", "points": points, "closed": bool(entity.closed)})
        bbox = _bbox_from_points(points)
        properties["vertex_count"] = len(points)
    elif entity_type == "POLYLINE":
        points = [_point(vertex.dxf.location) for vertex in entity.vertices]
        geometry.update({"kind": "polyline", "points": points, "closed": bool(_safe_attr(entity, "flags", 0) & 1)})
        bbox = _bbox_from_points(points)
        properties["vertex_count"] = len(points)
    elif entity_type in {"CIRCLE", "ARC", "ELLIPSE"}:
        center = _point(_safe_attr(entity, "center", [0, 0, 0]))
        radius = float(_safe_attr(entity, "radius", 0.0) or 0.0)
        geometry.update({"center": center, "radius": radius})
        if entity_type == "ARC":
            geometry.update({"start_angle": float(_safe_attr(entity, "start_angle", 0.0)), "end_angle": float(_safe_attr(entity, "end_angle", 0.0))})
        bbox = _circle_bbox(center, radius)
    elif entity_type == "POINT":
        location = _point(_safe_attr(entity, "location", [0, 0, 0]))
        geometry["location"] = location
        bbox = _bbox_from_points([location])
    elif entity_type in {"TEXT", "MTEXT"}:
        location = _point(_safe_attr(entity, "insert", [0, 0, 0]))
        text = str(entity.text if entity_type == "MTEXT" else _safe_attr(entity, "text", ""))
        geometry.update({"location": location})
        properties.update({"text": text, "height": float(_safe_attr(entity, "height", 0.0) or 0.0), "rotation": float(_safe_attr(entity, "rotation", 0.0) or 0.0)})
        bbox = _bbox_from_points([location])
    elif entity_type == "INSERT":
        location = _point(_safe_attr(entity, "insert", [0, 0, 0]))
        geometry.update({"location": location})
        properties.update({
            "block_name": str(_safe_attr(entity, "name", "")),
            "rotation": float(_safe_attr(entity, "rotation", 0.0) or 0.0),
            "xscale": float(_safe_attr(entity, "xscale", 1.0) or 1.0),
            "yscale": float(_safe_attr(entity, "yscale", 1.0) or 1.0),
            "zscale": float(_safe_attr(entity, "zscale", 1.0) or 1.0),
        })
        bbox = _bbox_from_points([location])
    else:
        for name in ("start", "end", "insert", "location", "center"):
            value = _safe_attr(entity, name)
            if value is not None:
                try:
                    geometry[name] = _point(value)
                    bbox = _bbox_from_points([geometry[name]])
                    break
                except (TypeError, IndexError):
                    pass

    for name in ("color", "linetype", "lineweight", "thickness"):
        value = _safe_attr(entity, name)
        if value is not None:
            try:
                properties[name] = value.dxf.name if hasattr(value, "dxf") else value
            except AttributeError:
                properties[name] = value
    return NormalizedCADEntity(handle, entity_type, layer, geometry, properties, bbox)


class DXFParser:
    name = "ezdxf"
    version = getattr(ezdxf, "__version__", "unavailable")

    def parse(self, path: str | Path) -> DXFParseResult:
        if ezdxf is None:
            raise RuntimeError("ezdxf is required for DXF ingestion; install the [cad] extra")
        source_path = Path(path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        doc = ezdxf.readfile(str(source_path))
        entities: list[NormalizedCADEntity] = []
        unsupported: set[str] = set()
        warnings: list[str] = []
        for entity in doc.modelspace():
            entity_type = entity.dxftype()
            if entity_type not in SUPPORTED_ENTITY_TYPES:
                unsupported.add(entity_type)
            try:
                entities.append(_normalize_entity(entity))
            except Exception as exc:  # preserve partial ingest and report the failure
                warnings.append(f"entity {getattr(entity.dxf, 'handle', 'unknown')} normalization failed: {exc}")

        header_extents = {}
        for key, target in (("$EXTMIN", "min"), ("$EXTMAX", "max")):
            value = doc.header.get(key)
            if value is not None:
                point = _point(value)
                header_extents[f"{target}_x"], header_extents[f"{target}_y"], header_extents[f"{target}_z"] = point
        boxes = [entity.bbox for entity in entities if entity.bbox]
        geometric_extents = {
            "min_x": min(box["min_x"] for box in boxes),
            "min_y": min(box["min_y"] for box in boxes),
            "min_z": min(box["min_z"] for box in boxes),
            "max_x": max(box["max_x"] for box in boxes),
            "max_y": max(box["max_y"] for box in boxes),
            "max_z": max(box["max_z"] for box in boxes),
        } if boxes else {}
        if header_extents and geometric_extents:
            geometric_extents["header"] = header_extents
        layers = sorted(str(layer.dxf.name) for layer in doc.layers)
        blocks = sorted(str(block.name) for block in doc.blocks)
        units_code = int(doc.header.get("$INSUNITS", 0) or 0)
        return DXFParseResult(
            source_file=str(source_path),
            dxf_version=str(doc.dxfversion),
            units=INSUNITS.get(units_code, f"code-{units_code}"),
            entities=entities,
            layers=layers,
            blocks=blocks,
            extents=geometric_extents,
            unsupported_entity_types=sorted(unsupported),
            warnings=warnings,
        )


"""Deterministic 2D spatial relations for DXF sheets.

Derives graph edges from drawing geometry, using only predicates declared in
``ontology_model``:

* ``Door|Window hostedBy Wall``      opening insertion point on (or within tolerance of) a wall segment
* ``Space containsElement Element``  element inside a room-boundary polygon, else nearest room label
                                     reachable without crossing a wall line
* ``Storey hasSpace Space`` / ``Space onStorey Storey``  storey parsed from the sheet title (1층, B1F ...)
* ``Sheet depicts Space`` / ``Sheet hasTitleBlock TitleBlock``  from sheet metadata

It also completes text-derived objects: room labels pick up the area label written under
them, and steel profile outlines pick up the section designation of the nearest label
(an outline without one is demoted to ``BuildingElementProxy`` so the SteelSection SHACL
shape keeps holding). Every edge carries ``provenance.method`` and the measured distance.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Sequence

from .cair import CAIRObject, CAIRRelation, Classification, Provenance, SourceRef, stable_object_id

Point = tuple[float, float]
Segment = tuple[Point, Point]

OPENING_TYPES = {"Door", "Window", "Opening"}
HOST_TYPES = {"Wall"}
# Element classes that are located in a room (openings are hosted by walls instead).
ROOM_CONTENT_TYPES = {"Furniture", "Column", "Stair", "Elevator", "Slab", "BuildingElementProxy", "Bolt", "Beam", "Ramp"}
_UNIT_SCALE = {"millimeters": 1.0, "centimeters": 0.1, "meters": 0.001, "inches": 1 / 25.4, "feet": 1 / 304.8}
_ROOM_BOUNDARY_LAYER = re.compile(r"area|room|space|실경계|실영역", re.IGNORECASE)
_AREA_TEXT = re.compile(r"^\s*(\d{1,5}(?:[.,]\d{1,3})?)\s*(?:㎡|m2|m²|sqm)\s*$", re.IGNORECASE)
_STOREY_RE = (
    (re.compile(r"지하\s*(\d{1,2})\s*층"), lambda m: (f"B{m.group(1)}", -int(m.group(1)))),
    (re.compile(r"\bB(\d{1,2})F?\b", re.IGNORECASE), lambda m: (f"B{m.group(1)}", -int(m.group(1)))),
    (re.compile(r"(\d{1,3})\s*층"), lambda m: (f"{m.group(1)}F", int(m.group(1)))),
    (re.compile(r"\b(\d{1,3})F\b", re.IGNORECASE), lambda m: (f"{m.group(1)}F", int(m.group(1)))),
    (re.compile(r"지붕\s*층|옥탑|\bRF\b|ROOF", re.IGNORECASE), lambda m: ("RF", None)),
)


# --------------------------------------------------------------------------- geometry

def _xy(value: Any) -> Point | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
    return None


def entity_segments(geometry: dict[str, Any]) -> list[Segment]:
    kind = geometry.get("kind")
    if kind == "line":
        start, end = _xy(geometry.get("start")), _xy(geometry.get("end"))
        return [(start, end)] if start and end else []
    if kind == "polyline":
        points = [p for p in (_xy(v) for v in geometry.get("points") or []) if p]
        segments = list(zip(points, points[1:]))
        if geometry.get("closed") and len(points) > 2:
            segments.append((points[-1], points[0]))
        return segments
    return []


def representative_point(geometry: dict[str, Any], bbox: dict[str, float]) -> Point | None:
    for key in ("location", "insert", "center", "start"):
        point = _xy(geometry.get(key))
        if point:
            return point
    if bbox and {"min_x", "max_x", "min_y", "max_y"} <= bbox.keys():
        return (bbox["min_x"] + bbox["max_x"]) / 2, (bbox["min_y"] + bbox["max_y"]) / 2
    return None


def point_segment_distance(p: Point, segment: Segment) -> float:
    (x1, y1), (x2, y2) = segment
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / length2))
    return math.hypot(p[0] - (x1 + t * dx), p[1] - (y1 + t * dy))


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(s1: Segment, s2: Segment) -> bool:
    """Proper crossing (touching endpoints do not count, so a label on a wall line is not 'behind' it)."""
    a, b = s1
    c, d = s2
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def point_in_polygon(p: Point, polygon: Sequence[Point]) -> bool:
    inside = False
    x, y = p
    for (x1, y1), (x2, y2) in zip(polygon, list(polygon[1:]) + [polygon[0]]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _polygon_area(polygon: Sequence[Point]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(polygon, list(polygon[1:]) + [polygon[0]]))) / 2


def _bbox_distance(p: Point, bbox: dict[str, float]) -> float:
    dx = max(bbox["min_x"] - p[0], 0.0, p[0] - bbox["max_x"])
    dy = max(bbox["min_y"] - p[1], 0.0, p[1] - bbox["max_y"])
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------- sheet metadata

def storey_from_sheet(sheet: dict[str, Any] | None) -> dict[str, Any] | None:
    """Storey of a plan sheet from its title ('1층 평면도' -> 1F, '지하1층' -> B1). Only plans carry a storey."""
    if not sheet or sheet.get("category") not in {"plan", "structural"}:
        return None
    title = str(sheet.get("title") or "")
    for pattern, build in _STOREY_RE:
        found = pattern.search(title)
        if found:
            name, level = build(found)
            return {"name": name, "level": level, "evidence": f"sheet title: {title}"}
    return None


# --------------------------------------------------------------------------- builder

def _rel(subject: str, predicate: str, obj: str, confidence: float, method: str, **extra: Any) -> CAIRRelation:
    provenance = {"method": method, "agent": "aec-spatial-relations"}
    provenance.update({k: (round(v, 1) if isinstance(v, float) else v) for k, v in extra.items()})
    return CAIRRelation(subject, predicate, obj, round(max(0.0, min(confidence, 1.0)), 4), provenance)


def _derived_object(project_id: str, label: str, key: str, template: CAIRObject, confidence: float,
                    evidence: Iterable[str], properties: dict[str, Any], method: str) -> CAIRObject:
    source = SourceRef(template.source.file, template.source.format, key, None, template.source.artifact_id)
    prov = template.provenance
    provenance = Provenance(prov.source_file if prov else template.source.file, key, prov.source_format if prov else "DXF",
                            prov.source_hash if prov else None, prov.parser if prov else "aec", prov.parser_version if prov else "",
                            transformation=method)
    return CAIRObject(
        id=stable_object_id(project_id, label, "DXF", key),
        project_id=project_id,
        type=label,
        source=source,
        geometry_ref=None,
        properties={**properties, "derived_by": method},
        classification=Classification(label, confidence, method, tuple(evidence), "ACCEPT_WITH_WARNING"),
        provenance=provenance,
    )


def build_spatial_relations(objects: list[CAIRObject], entities: Sequence[Any], project_id: str,
                            sheet: dict[str, Any] | None = None, units: str | None = None,
                            require_title_block: bool = True) -> tuple[list[CAIRObject], list[CAIRRelation]]:
    """Return (derived objects, relations). ``objects`` and ``entities`` are index aligned; objects are
    completed in place (area / section designation). Output order is deterministic (source order).

    Sheet and Storey objects need sheet metadata read from a title block unless ``require_title_block``
    is false (the operational path, whose sheet title may come from drawing title text or the file name)."""
    scale = _UNIT_SCALE.get(str(units or "").lower(), 1.0)
    host_tolerance = 300.0 * scale      # half a thick wall plus frame offset, in drawing units
    label_tolerance = 800.0 * scale     # room-area / section label offset under its name or outline
    pairs = list(zip(objects, entities))
    relations: list[CAIRRelation] = []
    derived: list[CAIRObject] = []

    walls: list[tuple[CAIRObject, list[Segment]]] = []
    for obj, entity in pairs:
        if obj.type in HOST_TYPES:
            segments = entity_segments(entity.geometry)
            if segments:
                walls.append((obj, segments))
    wall_segments = [segment for _, segments in walls for segment in segments]

    # 1. openings hosted by walls
    for obj, entity in pairs:
        if obj.type not in OPENING_TYPES or entity.entity_type in {"TEXT", "MTEXT"}:
            continue
        point = representative_point(entity.geometry, entity.bbox)
        if point is None or not walls:
            continue
        best: tuple[float, int, CAIRObject] | None = None
        for index, (wall, segments) in enumerate(walls):
            distance = min(point_segment_distance(point, s) for s in segments)
            if distance <= host_tolerance and (best is None or (distance, index) < best[:2]):
                best = (distance, index, wall)
        if best:
            distance, _, wall = best
            relations.append(_rel(obj.id, "hostedBy", wall.id, 0.9 - 0.3 * distance / host_tolerance,
                                  "opening_point_on_wall_segment", distance=distance, tolerance=host_tolerance))

    # 2. room labels: area text under the name
    spaces = [(obj, entity) for obj, entity in pairs if obj.type == "Space"]
    area_labels = [(obj, entity, float(m.group(1).replace(",", "."))) for obj, entity in pairs
                   if obj.type == "Annotation" and entity.entity_type in {"TEXT", "MTEXT"}
                   and (m := _AREA_TEXT.match(str(entity.properties.get("text") or "")))]
    used_area: set[str] = set()
    for space, entity in spaces:
        anchor = representative_point(entity.geometry, entity.bbox)
        if anchor is None or "area" in space.properties:
            continue
        candidates = []
        for label, label_entity, value in area_labels:
            point = representative_point(label_entity.geometry, label_entity.bbox)
            if label.id in used_area or point is None:
                continue
            dy, dx = anchor[1] - point[1], abs(anchor[0] - point[0])
            if 0 < dy <= label_tolerance and dx <= label_tolerance:
                candidates.append((math.hypot(dx, dy), label.source.entity_id or "", label, value))
        if candidates:
            _, _, label, value = min(candidates, key=lambda c: c[:2])
            used_area.add(label.id)
            if value > 0:
                space.properties["area"] = value
                space.properties["area_label"] = label.id
                relations.append(_rel(label.id, "relatedTo", space.id, 0.8, "area_label_below_room_name"))

    # 3. elements located in spaces
    boundaries: list[tuple[CAIRObject, list[Point]]] = []
    for obj, entity in pairs:
        if entity.geometry.get("kind") == "polyline" and entity.geometry.get("closed") and _ROOM_BOUNDARY_LAYER.search(entity.layer or ""):
            polygon = [p for p in (_xy(v) for v in entity.geometry.get("points") or []) if p]
            if len(polygon) >= 3:
                boundaries.append((obj, polygon))
    space_points = [(space, representative_point(entity.geometry, entity.bbox)) for space, entity in spaces]
    space_points = [(space, point) for space, point in space_points if point]
    # A boundary polygon containing a room label is that room's region.
    regions: list[tuple[CAIRObject, list[Point]]] = []
    for space, point in space_points:
        containing = [(_polygon_area(poly), obj.source.entity_id or "", poly) for obj, poly in boundaries if point_in_polygon(point, poly)]
        if containing:
            regions.append((space, min(containing, key=lambda c: c[:2])[2]))
    if space_points:
        for obj, entity in pairs:
            if obj.type not in ROOM_CONTENT_TYPES:
                continue
            point = representative_point(entity.geometry, entity.bbox)
            if point is None:
                continue
            inside = [space for space, polygon in regions if point_in_polygon(point, polygon)]
            if inside:
                relations.append(_rel(inside[0].id, "containsElement", obj.id, 0.9, "point_in_room_boundary"))
                continue
            visible = []
            for space, label_point in space_points:
                sight = (point, label_point)
                if any(segments_cross(sight, segment) for segment in wall_segments):
                    continue
                visible.append((math.hypot(point[0] - label_point[0], point[1] - label_point[1]), space.source.entity_id or "", space))
            if visible:
                distance, _, space = min(visible, key=lambda c: c[:2])
                relations.append(_rel(space.id, "containsElement", obj.id, 0.7, "nearest_room_label_without_wall_crossing",
                                      distance=distance))

    # 4. steel profile outlines take the designation of the nearest section label
    section_labels = [(obj, entity) for obj, entity in pairs
                      if obj.type == "SteelSection" and entity.entity_type in {"TEXT", "MTEXT"} and obj.properties.get("sectionDesignation")]
    for obj, entity in pairs:
        if obj.type != "SteelSection" or entity.entity_type in {"TEXT", "MTEXT"} or obj.properties.get("sectionDesignation"):
            continue
        candidates = []
        if entity.bbox:
            size = max(entity.bbox.get("max_x", 0) - entity.bbox.get("min_x", 0), entity.bbox.get("max_y", 0) - entity.bbox.get("min_y", 0))
            for label, label_entity in section_labels:
                point = representative_point(label_entity.geometry, label_entity.bbox)
                if point is None:
                    continue
                distance = _bbox_distance(point, entity.bbox)
                if distance <= max(label_tolerance / 2, size):
                    candidates.append((distance, label.source.entity_id or "", label))
        if candidates:
            distance, _, label = min(candidates, key=lambda c: c[:2])
            obj.properties["sectionDesignation"] = label.properties["sectionDesignation"]
            obj.properties["section_label"] = label.id
            relations.append(_rel(obj.id, "relatedTo", label.id, 0.8, "section_label_near_profile", distance=distance))
        else:
            # Without a designation the outline cannot satisfy aec:SteelSectionShape; keep it as a generic element.
            obj.properties["semantic_class"] = "SteelSection"
            obj.type = "BuildingElementProxy"
            if obj.classification:
                c = obj.classification
                obj.classification = Classification("BuildingElementProxy", min(c.confidence, 0.6), c.method,
                                                    (*c.evidence, "no section designation label near outline"), "REQUIRE_VALIDATION")

    # 5. sheet and storey from sheet metadata
    if objects and sheet and (sheet.get("source") == "title_block" or (not require_title_block and sheet.get("title"))):
        template = objects[0]
        number = str(sheet.get("number") or "").strip()
        sheet_key = f"sheet:{number or template.source.file}"
        sheet_obj = _derived_object(
            project_id, "Sheet", sheet_key, template, 0.9 if sheet.get("source") == "title_block" else 0.75,
            (f"sheet metadata from {sheet.get('source')}", f"number={number}", f"title={sheet.get('title')}"),
            {k: v for k, v in {"drawingNumber": number or None, "drawingTitle": sheet.get("title"), "scale": sheet.get("scale"),
                               "drawingCategory": sheet.get("drawing_category"), "revisionLabel": sheet.get("revision")}.items() if v},
            "sheet_metadata")
        derived.append(sheet_obj)
        relations.append(_rel(f"aec://project/{project_id}", "containsElement", sheet_obj.id, sheet_obj.classification.confidence, "sheet_metadata"))
        for obj in objects:
            if obj.type == "TitleBlock":
                relations.append(_rel(sheet_obj.id, "hasTitleBlock", obj.id, 0.9, "title_block_on_sheet"))
                break
        for space, _ in spaces:
            relations.append(_rel(sheet_obj.id, "depicts", space.id, 0.9, "space_label_on_sheet"))
        storey = storey_from_sheet(sheet)
        if storey:
            storey_obj = _derived_object(
                project_id, "Storey", f"storey:{storey['name']}", template, 0.8, (storey["evidence"],),
                {"storeyName": storey["name"], **({"storeyIndex": storey["level"]} if storey["level"] is not None else {})},
                "storey_from_sheet_title")
            derived.append(storey_obj)
            relations.append(_rel(f"aec://project/{project_id}", "containsElement", storey_obj.id, 0.8, "storey_from_sheet_title"))
            relations.append(_rel(sheet_obj.id, "depicts", storey_obj.id, 0.8, "storey_from_sheet_title"))
            for space, _ in spaces:
                relations.append(_rel(storey_obj.id, "hasSpace", space.id, 0.8, "space_label_on_storey_plan"))
                relations.append(_rel(space.id, "onStorey", storey_obj.id, 0.8, "space_label_on_storey_plan"))
    return derived, relations

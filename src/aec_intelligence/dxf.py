"""DXF normalization adapter.

`ezdxf` is the authoritative parser when installed. The adapter returns a
format-neutral normalized entity model so later CAIR, ontology, and storage
layers do not depend on ezdxf objects.
"""

from __future__ import annotations

import re
import weakref
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
    # Sheet metadata (title block / file name) and the block library with a semantic category per definition.
    sheet: dict[str, Any] | None = None
    block_definitions: list[dict[str, Any]] = field(default_factory=list)

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
            "sheet": self.sheet,
            "block_definitions": self.block_definitions,
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


_EFFECTIVE_NAMES: "weakref.WeakKeyDictionary[Any, dict[str, str]]" = weakref.WeakKeyDictionary()


def block_effective_name(block_record: Any) -> str | None:
    """Name of the dynamic block an anonymous ``*U`` representation was generated from.

    AutoCAD stores the source BLOCK_RECORD handle in the ``AcDbBlockRepBTag`` XDATA of
    the anonymous block record; returns ``None`` when the block is not such a representation.
    """
    try:
        if not block_record.has_xdata("AcDbBlockRepBTag"):
            return None
        for code, value in block_record.get_xdata("AcDbBlockRepBTag"):
            if code == 1005:
                source = block_record.doc.entitydb.get(str(value))
                if source is not None and source.dxf.hasattr("name"):
                    return str(source.dxf.name)
    except Exception:
        return None
    return None


def effective_block_name(insert: Any) -> str:
    """Effective (user-visible) block name of an INSERT; dynamic-block ``*U`` references resolve to their source."""
    name = str(_safe_attr(insert, "name", ""))
    doc = getattr(insert, "doc", None)
    if not name.startswith("*") or doc is None:
        return name
    try:
        cache = _EFFECTIVE_NAMES.setdefault(doc, {})
    except TypeError:
        cache = {}
    if name not in cache:
        resolved = None
        try:
            block = doc.blocks.get(name)
            if block is not None:
                resolved = block_effective_name(block.block_record)
        except Exception:
            resolved = None
        cache[name] = resolved or name
    return cache[name]


def insert_attributes(insert: Any) -> dict[str, str]:
    """ATTRIB tag -> value; repeated tags get a ``#n`` suffix so no value is lost."""
    result: dict[str, str] = {}
    try:
        attribs = list(insert.attribs)
    except Exception:
        return result
    for attrib in attribs:
        tag = decode_dxf_text(_safe_attr(attrib, "tag", "") or "").strip() or "ATTRIB"
        try:
            value = decode_dxf_text(attrib.plain_text())
        except Exception:
            value = decode_dxf_text(_safe_attr(attrib, "text", ""))
        key, n = tag, 2
        while key in result:
            key, n = f"{tag}#{n}", n + 1
        result[key] = value
    return result


def _normalize_entity(entity: Any) -> NormalizedCADEntity:
    entity_type = entity.dxftype()
    handle = str(_safe_attr(entity, "handle", "unknown"))
    layer = decode_dxf_text(_safe_attr(entity, "layer", "0"))
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
    elif entity_type in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        location = _point(_safe_attr(entity, "insert", [0, 0, 0]))
        raw = str(entity.text if entity_type == "MTEXT" else _safe_attr(entity, "text", ""))
        text = raw
        try:  # strip MTEXT inline formatting (\P paragraphs, {\fArial;...}) and TEXT %%-codes
            text = entity.plain_text() if entity_type == "MTEXT" else str(entity.plain_text())
        except Exception:
            pass
        text = decode_dxf_text(text)
        geometry.update({"location": location})
        height = (_safe_attr(entity, "char_height") if entity_type == "MTEXT" else None) or _safe_attr(entity, "height", 0.0)
        properties.update({"text": text, "height": float(height or 0.0), "rotation": float(_safe_attr(entity, "rotation", 0.0) or 0.0)})
        if text != raw:
            properties["raw_text"] = raw
        if entity_type in {"ATTRIB", "ATTDEF"}:
            properties["tag"] = str(_safe_attr(entity, "tag", ""))
        bbox = _bbox_from_points([location])
    elif entity_type == "INSERT":
        location = _point(_safe_attr(entity, "insert", [0, 0, 0]))
        geometry.update({"location": location})
        name = str(_safe_attr(entity, "name", ""))
        properties.update({
            "block_name": name,
            "rotation": float(_safe_attr(entity, "rotation", 0.0) or 0.0),
            "xscale": float(_safe_attr(entity, "xscale", 1.0) or 1.0),
            "yscale": float(_safe_attr(entity, "yscale", 1.0) or 1.0),
            "zscale": float(_safe_attr(entity, "zscale", 1.0) or 1.0),
        })
        # Effective (dynamic-block source / decoded) name; equals block_name for ordinary blocks.
        properties["effective_name"] = decode_dxf_text(effective_block_name(entity)) or name
        attributes = insert_attributes(entity)
        if attributes:
            properties["attributes"] = attributes
        bbox = _bbox_from_points([location])
    elif entity_type == "DIMENSION":
        location = _point(_safe_attr(entity, "defpoint", [0, 0, 0]))
        geometry["location"] = location
        bbox = _bbox_from_points([location])
        try:
            properties["dimtype"] = int(entity.dimtype)
            properties["measurement"] = float(entity.get_measurement())
        except Exception:  # angular/ordinate variants may not expose one scalar measurement
            pass
        override = decode_dxf_text(_safe_attr(entity, "text", "") or "")
        if override and override != "<>":
            properties["text_override"] = override
        if "measurement" in properties:
            value = properties["measurement"]
            shown = f"{value:g}" if abs(value - round(value)) > 1e-9 else str(int(round(value)))
            properties["text"] = override.replace("<>", shown) if override and override.strip() else shown
        elif override:
            properties["text"] = override
    elif entity_type == "HATCH":
        properties["pattern_name"] = str(_safe_attr(entity, "pattern_name", "") or "")
        properties["solid_fill"] = bool(_safe_attr(entity, "solid_fill", 0))
        try:  # boundary extents: what the fill covers (lets a generic hatch inherit the outline it fills)
            from ezdxf import bbox as ezdxf_bbox
            box = ezdxf_bbox.extents([entity])
            if box.has_data:
                bbox = _bbox_from_points([_point(box.extmin), _point(box.extmax)])
        except Exception:
            pass
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


def read_dxf(path: str | Path) -> tuple[Any, list[str]]:
    """Read a DXF, falling back to ``ezdxf.recover`` for damaged or legacy files.

    ezdxf decodes pre-R2007 files with the ``$DWGCODEPAGE`` header (ANSI_949 -> cp949 for
    Korean drawings). When the strict reader fails, the recover reader repairs structure and
    encoding problems; its fixes and errors are returned as warnings so they stay auditable.
    """
    if ezdxf is None:
        raise RuntimeError("ezdxf is required for DXF ingestion; install the [cad] extra")
    warnings: list[str] = []
    try:
        doc = ezdxf.readfile(str(path))
    except IOError:
        raise
    except Exception as exc:
        from ezdxf import recover

        warnings.append(f"DXF strict read failed ({type(exc).__name__}: {exc}); recovered with ezdxf.recover")
        doc, auditor = recover.readfile(str(path))
        warnings.extend(f"recover fix: {fix.message}" for fix in list(auditor.fixes)[:50])
        warnings.extend(f"recover error: {error.message}" for error in list(auditor.errors)[:50])
    codepage = str(doc.header.get("$DWGCODEPAGE", "") or "")
    if doc.dxfversion < "AC1021":
        sniffed = _sniff_korean_encoding(path, str(doc.encoding))
        if sniffed:
            warnings.append(f"$DWGCODEPAGE={codepage or 'missing'} but text bytes are {sniffed}; re-read as {sniffed}")
            doc = ezdxf.readfile(str(path), encoding=sniffed)
        elif codepage and codepage.upper() != "ANSI_1252":
            warnings.append(f"Legacy DXF decoded with $DWGCODEPAGE={codepage} ({doc.encoding})")
    return doc, warnings


_HANGUL_RE = re.compile("[\uac00-\ud7a3]")


def _sniff_korean_encoding(path: str | Path, encoding: str) -> str | None:
    """Detect CP949 bytes in a legacy DXF whose header claims a Western code page."""
    if encoding.lower().replace("-", "") not in {"cp1252", "windows1252", "latin1", "iso88591", "ascii"}:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not any(byte >= 0x80 for byte in data):
        return None
    try:
        decoded = data.decode("cp949")
    except UnicodeDecodeError:
        return None
    non_ascii = sum(1 for ch in decoded if ord(ch) >= 0x80)
    hangul = len(_HANGUL_RE.findall(decoded))
    return "cp949" if hangul >= 2 and hangul >= 0.6 * non_ascii else None


def decode_dxf_text(value: Any) -> str:
    """Decode ``\\U+AC70``-style escapes that legacy DXF writers use for characters outside the code page."""
    text = str(value)
    if "\\U+" in text or "\\u+" in text:
        try:
            from ezdxf.lldxf.encoding import decode_dxf_unicode

            return decode_dxf_unicode(text)
        except Exception:
            return re.sub(r"\\[Uu]\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    return text


class DXFParser:
    name = "ezdxf"
    version = getattr(ezdxf, "__version__", "unavailable")

    def parse(self, path: str | Path) -> DXFParseResult:
        if ezdxf is None:
            raise RuntimeError("ezdxf is required for DXF ingestion; install the [cad] extra")
        source_path = Path(path).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        doc, warnings = read_dxf(source_path)
        entities: list[NormalizedCADEntity] = []
        unsupported: set[str] = set()
        model_entities: list[NormalizedCADEntity] = []
        for space in doc.layouts:
            # Model space first; paper-space entities (title blocks, sheet notes) carry their layout name.
            for entity in space:
                entity_type = entity.dxftype()
                if entity_type not in SUPPORTED_ENTITY_TYPES:
                    unsupported.add(entity_type)
                try:
                    normalized = _normalize_entity(entity)
                except Exception as exc:  # preserve partial ingest and report the failure
                    warnings.append(f"entity {getattr(entity.dxf, 'handle', 'unknown')} normalization failed: {exc}")
                    continue
                if space.is_modelspace:
                    model_entities.append(normalized)
                else:
                    normalized.properties["layout"] = str(space.name)
                entities.append(normalized)

        header_extents = {}
        for key, target in (("$EXTMIN", "min"), ("$EXTMAX", "max")):
            value = doc.header.get(key)
            if value is not None:
                point = _point(value)
                header_extents[f"{target}_x"], header_extents[f"{target}_y"], header_extents[f"{target}_z"] = point
        boxes = [entity.bbox for entity in model_entities if entity.bbox]
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
            sheet=sheet_metadata(entities, source_path.stem),
            block_definitions=block_definitions(doc),
        )


def block_definitions(doc: Any) -> list[dict[str, Any]]:
    """Every non-layout block definition with its effective name, ATTDEFs and semantic category."""
    from .classifier import classify

    rows: list[dict[str, Any]] = []
    for block in list(doc.blocks):  # snapshot, see parsers.catalog_blocks
        if block.is_any_layout:
            continue
        dxf_name = str(block.name)
        name = decode_dxf_text(dxf_name)
        flags = int(block.block.dxf.get("flags", 0) or 0)
        is_xref = bool(flags & 4 or flags & 8)
        effective = decode_dxf_text(block_effective_name(block.block_record) or dxf_name)
        attdefs = [{"tag": decode_dxf_text(e.dxf.get("tag", "")), "prompt": decode_dxf_text(e.dxf.get("prompt", "")),
                    "default": decode_dxf_text(e.dxf.get("text", ""))} for e in block if e.dxftype() == "ATTDEF"]
        if is_xref:
            category: str | None = "Xref"
        elif name.upper().startswith("*D"):
            category = "Dimension"
        else:
            probe = NormalizedCADEntity(name, "INSERT", "0", properties={
                "block_name": name, "effective_name": effective,
                "attributes": {a["tag"]: a["default"] or a["tag"] for a in attdefs}})
            label = classify(probe)[0]
            category = None if label == "CADEntity" else label
        rows.append({"name": name, "effective_name": effective, "category": category, "is_xref": is_xref,
                     "is_anonymous": name.startswith("*") or bool(flags & 1), "attribute_defs": attdefs,
                     "entity_count": len(block)})
    return rows


_SHEET_NUMBER_RE = re.compile(r"^([A-Z]{1,3}-?\d{2,4}[A-Z]?)")


def sheet_metadata(entities: list[NormalizedCADEntity], file_stem: str) -> dict[str, Any]:
    """Sheet number/title/scale from the title-block INSERT (paper space preferred), else from the file name."""
    from .classifier import drawing_category, title_block_fields

    fields: dict[str, str] = {}
    source = "file_name"
    inserts = [e for e in entities if e.entity_type == "INSERT" and e.properties.get("attributes")]
    for entity in sorted(inserts, key=lambda e: "layout" not in e.properties):
        found = title_block_fields(entity.properties["attributes"])
        if found:
            fields, source = found, "title_block"
            break
    number = fields.get("drawingNumber") or (m.group(1) if (m := _SHEET_NUMBER_RE.match(file_stem)) else None)
    title = fields.get("drawingTitle") or (re.sub(r"^[A-Z]{1,3}-?\d{2,4}[A-Z]?[_\s-]*", "", file_stem).split("_")[0] or None)
    category = drawing_category(("title_block", fields.get("drawingTitle", "")), ("file_name", file_stem))
    return {"number": number, "title": title, "scale": fields.get("scale"), "revision": fields.get("revisionLabel"),
            "date": fields.get("date"), "category": category["drawing_category_group"],
            "drawing_category": category["drawing_category"], "drawing_category_en": category["drawing_category_en"],
            "source": source}


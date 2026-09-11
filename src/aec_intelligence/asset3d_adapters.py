"""Metadata-first adapters for common 3D exchange assets.

These parsers establish file identity and lightweight geometry evidence. They
do not infer BIM semantics from a mesh or B-Rep file; semantic authority stays
with IFC/CAIR or an explicit application adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import struct
from typing import Any


class Asset3DAdapterUnavailable(RuntimeError):
    """Reserved for optional application-backed 3D adapters."""


@dataclass
class Asset3DParseResult:
    source_file: str
    source_format: str
    status: str = "SUCCESS"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_format": self.source_format,
            "status": self.status,
            "metadata": self.metadata,
            "warnings": self.warnings,
        }


def _bbox(points: list[tuple[float, float, float]]) -> dict[str, float]:
    if not points:
        return {}
    return {
        "min_x": min(point[0] for point in points),
        "min_y": min(point[1] for point in points),
        "min_z": min(point[2] for point in points),
        "max_x": max(point[0] for point in points),
        "max_y": max(point[1] for point in points),
        "max_z": max(point[2] for point in points),
    }


class OBJParser:
    name = "OBJ text mesh adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> Asset3DParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".obj":
            raise ValueError("OBJParser accepts .obj files")
        vertices: list[tuple[float, float, float]] = []
        face_count = 0
        group_count = 0
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4:
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
            elif parts[0] == "f":
                face_count += 1
            elif parts[0] in {"g", "o"}:
                group_count += 1
        return Asset3DParseResult(
            str(source),
            "OBJ",
            metadata={
                "parser": self.name,
                "parser_version": self.parser_version,
                "vertex_count": len(vertices),
                "face_count": face_count,
                "group_count": group_count,
                "bbox": _bbox(vertices),
                "semantic_cair_generated": False,
            },
        )


class STLParser:
    name = "STL binary/ASCII mesh adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> Asset3DParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".stl":
            raise ValueError("STLParser accepts .stl files")
        raw = source.read_bytes()
        binary = len(raw) >= 84
        facet_count = 0
        points: list[tuple[float, float, float]] = []
        if binary:
            declared = struct.unpack_from("<I", raw, 80)[0]
            binary = len(raw) == 84 + declared * 50
            if binary:
                facet_count = declared
                for offset in range(84, len(raw), 50):
                    values = struct.unpack_from("<12f", raw, offset)
                    points.extend((tuple(values[index:index + 3]) for index in (3, 6, 9)))
        if not binary:
            text = raw.decode("utf-8", errors="replace")
            facet_count = len(re.findall(r"(?im)^\s*facet\s+normal\b", text))
            for match in re.finditer(
                r"(?im)^\s*vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)",
                text,
            ):
                points.append(tuple(float(value) for value in match.groups()))
        return Asset3DParseResult(
            str(source),
            "STL",
            metadata={
                "parser": self.name,
                "parser_version": self.parser_version,
                "encoding": "binary" if binary else "ascii",
                "facet_count": facet_count,
                "vertex_count": len(points),
                "bbox": _bbox(points),
                "semantic_cair_generated": False,
            },
        )


def _gltf_metadata(document: dict[str, Any], parser: str, parser_version: str) -> dict[str, Any]:
    meshes = document.get("meshes") or []
    primitives = sum(len(mesh.get("primitives") or []) for mesh in meshes)
    return {
        "parser": parser,
        "parser_version": parser_version,
        "scene_count": len(document.get("scenes") or []),
        "node_count": len(document.get("nodes") or []),
        "mesh_count": len(meshes),
        "primitive_count": primitives,
        "accessor_count": len(document.get("accessors") or []),
        "buffer_view_count": len(document.get("bufferViews") or []),
        "semantic_cair_generated": False,
    }


class GLTFParser:
    name = "glTF JSON/GLB structure adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> Asset3DParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower()
        if suffix == ".gltf":
            document = json.loads(source.read_text(encoding="utf-8"))
            source_format = "GLTF"
        elif suffix == ".glb":
            raw = source.read_bytes()
            if len(raw) < 20 or raw[:4] != b"glTF":
                raise ValueError("invalid GLB header")
            version, declared_length = struct.unpack_from("<II", raw, 4)
            if version != 2 or declared_length > len(raw):
                raise ValueError("unsupported GLB version or length")
            document: dict[str, Any] | None = None
            offset = 12
            while offset + 8 <= len(raw):
                chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
                offset += 8
                chunk = raw[offset:offset + chunk_length]
                offset += chunk_length
                if chunk_type == 0x4E4F534A:
                    document = json.loads(chunk.decode("utf-8").rstrip(" \\x00"))
                    break
            if document is None:
                raise ValueError("GLB JSON chunk is missing")
            source_format = "GLB"
        else:
            raise ValueError("GLTFParser accepts .gltf or .glb files")
        metadata = _gltf_metadata(document, self.name, self.parser_version)
        if suffix == ".glb":
            metadata.update({"glb_version": version, "declared_byte_length": declared_length})
        return Asset3DParseResult(str(source), source_format, metadata=metadata)


class STEPParser:
    name = "STEP Part-21 structure adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> Asset3DParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in {".step", ".stp"}:
            raise ValueError("STEPParser accepts .step or .stp files")
        text = source.read_text(encoding="utf-8", errors="replace")
        entity_types = re.findall(r"#\d+\s*=\s*([A-Z][A-Z0-9_]*)\s*\(", text.upper())
        counts: dict[str, int] = {}
        for entity_type in entity_types:
            counts[entity_type] = counts.get(entity_type, 0) + 1
        return Asset3DParseResult(
            str(source),
            "STEP",
            metadata={
                "parser": self.name,
                "parser_version": self.parser_version,
                "part21_entity_count": len(entity_types),
                "entity_counts": dict(sorted(counts.items())),
                "solid_entity_count": sum(counts.get(name, 0) for name in ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS")),
                "advanced_face_count": counts.get("ADVANCED_FACE", 0),
                "semantic_cair_generated": False,
            },
        )


def parse_3d_asset(path: str | Path) -> Asset3DParseResult:
    """Dispatch STEP/STL/OBJ/GLB/glTF by extension with explicit boundaries."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix in {".step", ".stp"}:
        return STEPParser().parse(source)
    if suffix == ".stl":
        return STLParser().parse(source)
    if suffix == ".obj":
        return OBJParser().parse(source)
    if suffix in {".glb", ".gltf"}:
        return GLTFParser().parse(source)
    raise ValueError("3D parser accepts .step, .stp, .stl, .obj, .glb, or .gltf")

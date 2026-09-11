"""Headless FreeCAD hand-off for CAIR-derived geometry.

The FreeCAD result is an explicitly derived review model.  CAIR remains the
authoritative source, while every FreeCAD object carries the CAIR identity,
geometry reference, source entity, and derivation policy as properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .application_adapters import FreeCADApplicationAdapter
from .blender_adapter import _scene_payload
from .repository import RepositoryLayout
from .storage import LocalArtifactStore


@dataclass
class FreeCADSceneResult:
    status: str
    project_id: str
    executable: str | None
    fcstd_path: str | None = None
    step_path: str | None = None
    object_count: int = 0
    geometry_count: int = 0
    solid_geometry_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class FreeCADNativeIFCProbeResult:
    """Evidence from FreeCAD's NativeIFC importer.

    This probe is deliberately separate from :class:`FreeCADSceneAdapter`:
    the latter creates a derived CAIR review model, while this result reports
    what FreeCAD itself created from an IFC source.  A semantic IFC import
    without a non-null shape is therefore never reported as a geometry
    success.
    """

    status: str
    source: str
    executable: str | None
    object_count: int = 0
    shape_object_count: int = 0
    non_null_shape_count: int = 0
    solid_object_count: int = 0
    objects: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _freecad_script() -> str:
    return r'''
import FreeCAD as App, Part, json, math, re, sys

input_path, fcstd_path, step_path = sys.argv[-3:]
payload = json.load(open(input_path, encoding="utf-8"))
height_mm = 3000.0
solid_mode = bool(payload.get("solid_mode", False))
doc = App.newDocument("AEC_CAIR_DERIVED")
created = 0
solid_geometry_count = 0
geometry_refs = []
objects = []

def vec(coords, z_offset=0.0):
    values = list(coords or [0.0, 0.0, 0.0])
    return App.Vector(float(values[0]), float(values[1]), float(values[2] if len(values) > 2 else 0.0) + z_offset)

def sampled_points(geometry):
    kind = geometry.get("kind")
    if kind == "line":
        return [geometry.get("start", [0, 0, 0]), geometry.get("end", [0, 0, 0])]
    if kind == "polyline":
        return geometry.get("points", [])
    if kind == "arc":
        center = geometry.get("center", [0, 0, 0])
        radius = float(geometry.get("radius", 0.0))
        start = math.radians(float(geometry.get("start_angle", 0.0)))
        end = math.radians(float(geometry.get("end_angle", 0.0)))
        while end < start:
            end += 2.0 * math.pi
        return [[center[0] + radius * math.cos(start + (end - start) * i / 24.0),
                 center[1] + radius * math.sin(start + (end - start) * i / 24.0),
                 center[2] if len(center) > 2 else 0.0] for i in range(25)]
    if kind == "circle":
        center = geometry.get("center", [0, 0, 0])
        radius = float(geometry.get("radius", 0.0))
        return [[center[0] + radius * math.cos(2.0 * math.pi * i / 32.0),
                 center[1] + radius * math.sin(2.0 * math.pi * i / 32.0),
                 center[2] if len(center) > 2 else 0.0] for i in range(32)]
    if kind == "mesh":
        vertices = geometry.get("vertices", [])
        return [vertices[index:index + 3] for index in range(0, len(vertices), 3) if len(vertices[index:index + 3]) == 3]
    return []

def polygon(points, z_offset=0.0, closed=False):
    values = [vec(point, z_offset) for point in points]
    if closed and values and values[0].distanceToPoint(values[-1]) > 0.000001:
        values.append(values[0])
    return Part.makePolygon(values) if len(values) >= 2 else Part.Shape()

def wireframe(points, closed=False):
    if len(points) < 2:
        return Part.Shape()
    pieces = [polygon(points, 0.0, closed), polygon(points, height_mm, closed)]
    for point in points:
        pieces.append(Part.makeLine(vec(point), vec(point, height_mm)))
    return Part.makeCompound(pieces)

def mesh_wireframe(geometry):
    vertices = geometry.get("vertices", [])
    faces = geometry.get("faces", [])
    points = [vec(vertices[index:index + 3]) for index in range(0, len(vertices), 3) if len(vertices[index:index + 3]) == 3]
    pieces = []
    for index in range(0, len(faces), 3):
        face = faces[index:index + 3]
        if len(face) != 3 or any(int(vertex_index) < 0 or int(vertex_index) >= len(points) for vertex_index in face):
            continue
        triangle = [points[int(vertex_index)] for vertex_index in face]
        triangle.append(triangle[0])
        pieces.append(Part.makePolygon(triangle))
    return Part.makeCompound(pieces) if pieces else Part.Shape()

def solid_extrusion(points):
    if len(points) < 3:
        return Part.Shape()
    values = [vec(point) for point in points]
    if values[0].distanceToPoint(values[-1]) > 0.000001:
        values.append(values[0])
    if len(values) < 4:
        return Part.Shape()
    try:
        wire = Part.makePolygon(values)
        return Part.Face(wire).extrude(App.Vector(0, 0, height_mm))
    except Exception:
        return Part.Shape()

def annotation_marker(location):
    center = vec(location)
    size = 120.0
    return Part.makeCompound([
        Part.makeLine(center + App.Vector(-size, 0, 0), center + App.Vector(size, 0, 0)),
        Part.makeLine(center + App.Vector(0, -size, 0), center + App.Vector(0, size, 0)),
    ])

def source_planar_bbox(items):
    points = []
    for item in items:
        geometry = item.get("geometry") or {}
        if geometry.get("kind") == "text":
            points.extend([geometry.get("location", [0.0, 0.0, 0.0])])
        else:
            points.extend(sampled_points(geometry))
    if not points:
        return {}
    return {
        "min_x": min(float(point[0]) for point in points),
        "min_y": min(float(point[1]) for point in points),
        "max_x": max(float(point[0]) for point in points),
        "max_y": max(float(point[1]) for point in points),
    }

source_bbox = source_planar_bbox(payload.get("objects", []))

for index, item in enumerate(payload.get("objects", []), start=1):
    geometry = item.get("geometry", {})
    kind = geometry.get("kind")
    points = sampled_points(geometry)
    closed = bool(geometry.get("closed", False) or kind == "circle")
    solid_candidate = solid_mode and kind == "polyline" and bool(geometry.get("closed", False))
    shape = annotation_marker(geometry.get("location", [0, 0, 0])) if kind == "text" else (mesh_wireframe(geometry) if kind == "mesh" else (solid_extrusion(points) if solid_candidate else wireframe(points, closed)))
    is_solid = solid_candidate and not shape.isNull() and len(shape.Solids) > 0
    if solid_candidate and not is_solid:
        shape = wireframe(points, closed)
    if shape.isNull():
        continue
    if is_solid:
        solid_geometry_count += 1
    source_entity = str(item.get("source_entity_id") or "unknown")
    label = re.sub(r"[^A-Za-z0-9_]", "_", str(item.get("type") or "AECObject") + "_" + source_entity) + "_" + str(index)
    obj = doc.addObject("Part::Feature", label)
    obj.Label = str(item.get("type") or "AECObject") + " / " + source_entity
    obj.Shape = shape
    properties = {
        "aec_id": item.get("id"),
        "aec_type": item.get("type"),
        "geometry_ref": item.get("geometry_ref"),
        "source_entity_id": item.get("source_entity_id"),
        "source_format": item.get("source_format"),
        "source_snapshot": payload.get("source_snapshot"),
        "source_units": "millimeters",
        "representation_mode": "solid" if is_solid else "wireframe",
        "derivation": "CAIR closed-planar-polyline extrusion; default height 3000 mm; not authoritative" if is_solid else "CAIR geometry wireframe; default height 3000 mm; not authoritative",
    }
    for name, value in properties.items():
        obj.addProperty("App::PropertyString", name, "AEC")
        setattr(obj, name, "" if value is None else str(value))
    objects.append(obj)
    created += 1
    if item.get("geometry_ref"):
        geometry_refs.append(item["geometry_ref"])

metadata = doc.addObject("App::FeaturePython", "AEC_CAIR_METADATA")
for name, value in {
    "project_id": payload.get("project_id"),
    "source_snapshot": payload.get("source_snapshot"),
    "schema_version": payload.get("schema_version"),
    "policy": "Derived review geometry only; CAIR and raw source remain authoritative; solid mode is limited to closed planar polylines",
}.items():
    metadata.addProperty("App::PropertyString", name, "AEC")
    setattr(metadata, name, "" if value is None else str(value))
doc.recompute()
doc.saveAs(fcstd_path)
Part.export(objects, step_path)
App.closeDocument(doc.Name)
reopened = App.openDocument(fcstd_path)
fcstd_objects = len(reopened.Objects)
App.closeDocument(reopened.Name)
step_shape = Part.read(step_path)
if step_shape.isNull():
    raise RuntimeError("FreeCAD Part.read returned a null STEP shape")
step_bbox = {
    "min_x": float(step_shape.BoundBox.XMin),
    "min_y": float(step_shape.BoundBox.YMin),
    "max_x": float(step_shape.BoundBox.XMax),
    "max_y": float(step_shape.BoundBox.YMax),
}
planar_deviation = {
    key: abs(step_bbox[key] - source_bbox[key])
    for key in source_bbox
} if source_bbox else {}
planar_max_deviation = max(planar_deviation.values(), default=0.0)
print(json.dumps({"objects": created, "fcstd_objects": fcstd_objects, "solid_geometry_count": solid_geometry_count, "step_imported": True, "step_edges": len(step_shape.Edges), "step_solids": len(step_shape.Solids), "geometry_refs": geometry_refs, "source_bbox": source_bbox, "step_bbox": step_bbox, "planar_bbox_deviation": planar_deviation, "planar_max_deviation": planar_max_deviation, "planar_bbox_match": planar_max_deviation <= 0.001, "fcstd_path": fcstd_path, "step_path": step_path, "solid_mode": solid_mode}))
'''


def _native_ifc_probe_script() -> str:
    """Return a headless FreeCAD script for the NativeIFC import boundary."""

    return r'''
import FreeCAD as App, json, sys, traceback

input_path = sys.argv[-1]
result = {
    "import_strategy": 2,
    "shape_mode": 0,
    "objects": [],
    "import_error": None,
}
documents_before = set(App.listDocuments().keys())
doc = None
try:
    from nativeifc import ifc_import
    doc = App.newDocument("AEC_NATIVE_IFC_PROBE")
    ifc_import.insert(
        input_path,
        doc.Name,
        strategy=2,
        shapemode=0,
        switchwb=False,
        silent=True,
        singledoc=False,
    )
    doc.recompute()
except Exception as exc:
    result["import_error"] = repr(exc)
    result["traceback"] = traceback.format_exc(limit=8)

for document in list(App.listDocuments().values()):
    for obj in document.Objects:
        item = {
            "doc": document.Name,
            "name": obj.Name,
            "type": obj.TypeId,
            "label": obj.Label,
            "has_shape": hasattr(obj, "Shape"),
            "is_null": None,
            "solids": 0,
            "faces": 0,
            "edges": 0,
        }
        if item["has_shape"]:
            try:
                item["is_null"] = bool(obj.Shape.isNull())
                item["solids"] = len(obj.Shape.Solids)
                item["faces"] = len(obj.Shape.Faces)
                item["edges"] = len(obj.Shape.Edges)
            except Exception as exc:
                item["shape_error"] = repr(exc)
        result["objects"].append(item)

result["documents"] = [document.Name for document in App.listDocuments().values()]
result["object_count"] = len(result["objects"])
result["shape_object_count"] = sum(1 for item in result["objects"] if item["has_shape"])
result["non_null_shape_count"] = sum(1 for item in result["objects"] if item["has_shape"] and item["is_null"] is False)
result["solid_object_count"] = sum(1 for item in result["objects"] if item.get("solids", 0) > 0)
print("NATIVE_IFC_RESULT " + json.dumps(result, ensure_ascii=True))

for document in list(App.listDocuments().values()):
    if document.Name not in documents_before:
        try:
            App.closeDocument(document.Name)
        except Exception:
            pass
'''


def probe_native_ifc(
    source: str | Path,
    executable: str | Path | None = None,
    repository_root: str | Path | None = None,
    timeout: int = 300,
) -> FreeCADNativeIFCProbeResult:
    """Run a truthful, headless NativeIFC geometry probe.

    The IFC is copied to an ASCII staging path because FreeCAD 1.1 on this
    Windows host can terminate while opening non-ASCII paths.  The source is
    never rewritten and no CAIR artifact is generated by this operation.
    """

    source_path = Path(source).resolve()
    if source_path.suffix.lower() != ".ifc":
        return FreeCADNativeIFCProbeResult("FAILED", str(source_path), None, errors=["native IFC probe requires an .ifc source"])
    if not source_path.is_file():
        return FreeCADNativeIFCProbeResult("FAILED", str(source_path), None, errors=[f"IFC source was not found: {source_path}"])

    application = FreeCADApplicationAdapter().probe(str(executable) if executable else None)
    if application.status != "AVAILABLE":
        return FreeCADNativeIFCProbeResult("REQUIRES_CONFIGURATION", str(source_path), application.executable, warnings=list(application.warnings))

    root = Path(repository_root).resolve() if repository_root else source_path.parent / ".aec-native-ifc-probes"
    source_sha256_before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    digest = source_sha256_before[:12]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_path.stem).strip("-") or "source"
    report_root = root / "runtime" / "external" / "freecad-runs" / "native-ifc-probes" / f"{slug}-{digest}"
    report_root.mkdir(parents=True, exist_ok=True)
    # Keep every path passed into FreeCAD ASCII-only on Windows.  The
    # repository itself may live under a localized/non-ASCII workspace name.
    run_root = Path.home() / "Documents" / "freecad" / "freecad_mcp_work" / "aec-intelligence-native-ifc" / f"{slug}-{digest}"
    run_root.mkdir(parents=True, exist_ok=True)
    staged_source = run_root / "input.ifc"
    script_path = run_root / "probe-native-ifc.py"
    shutil.copy2(source_path, staged_source)
    script_path.write_text(_native_ifc_probe_script(), encoding="utf-8")
    command = [application.executable, "-c", f"exec(open(r'{script_path}', encoding='utf-8').read())", "--pass", str(staged_source)]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return FreeCADNativeIFCProbeResult("FAILED", str(source_path), application.executable, errors=[str(exc)], checks={"command": command, "staged_source": str(staged_source)})

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    marker = next((line for line in reversed(stdout.splitlines()) if line.strip().startswith("NATIVE_IFC_RESULT ")), "")
    report: dict[str, Any] = {}
    if marker:
        try:
            report = json.loads(marker.split("NATIVE_IFC_RESULT ", 1)[1])
        except json.JSONDecodeError:
            report = {}
    objects = list(report.get("objects") or [])
    non_null_shape_count = int(report.get("non_null_shape_count", 0) or 0)
    checks = {
        "returncode": completed.returncode,
        "nativeifc_report_found": bool(marker),
        "import_strategy": report.get("import_strategy", 2),
        "shape_mode": report.get("shape_mode", 0),
        "object_count": int(report.get("object_count", len(objects)) or 0),
        "shape_object_count": int(report.get("shape_object_count", 0) or 0),
        "non_null_shape_count": non_null_shape_count,
        "solid_object_count": int(report.get("solid_object_count", 0) or 0),
        "documents": report.get("documents", []),
        "staged_source": str(staged_source),
        "command": command,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "authoritative_source_sha256_before": source_sha256_before,
        "authoritative_source_sha256_after": hashlib.sha256(source_path.read_bytes()).hexdigest() if source_path.is_file() else None,
        "authoritative_source_unchanged": source_path.is_file() and hashlib.sha256(source_path.read_bytes()).hexdigest() == source_sha256_before,
        "policy": "NativeIFC evidence only; canonical CAIR and raw IFC remain authoritative",
    }
    report_path = report_root / "native-ifc-report.json"
    report_path.write_text(json.dumps({"source": str(source_path), "executable": application.executable, "status": "PENDING", "objects": objects, "checks": checks, "errors": report.get("import_error")}, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []
    if completed.returncode != 0:
        errors.append(f"FreeCAD native IFC probe exited with return code {completed.returncode}")
    if not marker:
        errors.append("FreeCAD native IFC probe did not return a machine-readable report")
    if report.get("import_error"):
        errors.append(str(report["import_error"]))
    if not checks["authoritative_source_unchanged"]:
        errors.append("authoritative IFC source changed during the native probe")
    if not errors and non_null_shape_count == 0:
        errors.append("FreeCAD NativeIFC importer produced no non-null shape")
    status = "SUCCESS" if not errors else "FAILED"
    report_path.write_text(json.dumps({"source": str(source_path), "executable": application.executable, "status": status, "objects": objects, "checks": checks, "errors": errors, "warnings": warnings}, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return FreeCADNativeIFCProbeResult(status, str(source_path), application.executable, int(report.get("object_count", len(objects)) or 0), int(report.get("shape_object_count", 0) or 0), non_null_shape_count, int(report.get("solid_object_count", 0) or 0), objects, errors, warnings, {**checks, "report_path": str(report_path)})


class FreeCADSceneAdapter:
    """Create reviewable FreeCAD FCStd/STEP outputs from project CAIR.

    The default is a wireframe review model.  ``solid_mode`` is an explicit
    derived representation limited to closed planar CAIR polylines.
    """

    def __init__(self, executable: str | Path | None = None):
        self.executable = str(executable) if executable else None

    def export_project(self, repository_root: str | Path, project_id: str, timeout: int = 300, solid_mode: bool = False) -> FreeCADSceneResult:
        root = Path(repository_root).resolve()
        project = RepositoryLayout(root).project(project_id).ensure()
        probe = FreeCADApplicationAdapter().probe(self.executable)
        if probe.status != "AVAILABLE":
            return FreeCADSceneResult("REQUIRES_CONFIGURATION", project_id, probe.executable, warnings=list(probe.warnings))
        try:
            payload, object_count, geometry_count = _scene_payload(project.path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return FreeCADSceneResult("FAILED", project_id, probe.executable, errors=[str(exc)])

        run_root = root / "runtime" / "external" / "freecad-runs" / project_id
        run_root.mkdir(parents=True, exist_ok=True)
        input_path = run_root / "cair-scene-input.json"
        script_path = run_root / "build-cair-scene.py"
        snapshot = str(payload.get("source_snapshot") or "snapshot")
        snapshot_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", snapshot).strip("-") or "snapshot"
        mode_suffix = "-solid" if solid_mode else ""
        fcstd_path = project.path / "06_MODELS" / "FREECAD" / f"cair-derived-scene-{snapshot_slug}{mode_suffix}.FCStd"
        step_path = project.path / "02_DERIVED" / "BIM" / "STEP" / f"cair-derived-scene-{snapshot_slug}{mode_suffix}.step"
        payload["solid_mode"] = bool(solid_mode)
        solid_candidate_count = sum(
            1
            for item in payload.get("objects", [])
            if (item.get("geometry") or {}).get("kind") == "polyline"
            and bool((item.get("geometry") or {}).get("closed", False))
        )
        store = LocalArtifactStore(root)
        existing = {
            artifact_type: next(
                (
                    record
                    for record in store.find(project_id=project_id, artifact_type=artifact_type, source_snapshot=snapshot)
                    if record.metadata.get("solid_mode", False) == bool(solid_mode)
                    if (root / record.local_path).is_file()
                ),
                None,
            )
            for artifact_type in ("DERIVED/FREECAD/FCSTD", "DERIVED/BIM/STEP")
        }
        if all(existing.values()):
            canonical_paths = {artifact_type: str(root / record.local_path) for artifact_type, record in existing.items()}
            stored_solid_count = max(
                int(record.metadata.get("solid_geometry_count", 0) or 0)
                for record in existing.values()
            )
            return FreeCADSceneResult(
                "SUCCESS",
                project_id,
                probe.executable,
                canonical_paths["DERIVED/FREECAD/FCSTD"],
                canonical_paths["DERIVED/BIM/STEP"],
                object_count,
                geometry_count,
                solid_geometry_count=stored_solid_count,
                checks={"reused": True, "source_snapshot": snapshot, "solid_mode": bool(solid_mode), "created_objects": geometry_count, "solid_geometry_count": stored_solid_count, "step_solids": stored_solid_count if solid_mode else 0, "canonical_paths": canonical_paths},
            )

        input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        script_path.write_text(_freecad_script(), encoding="utf-8")
        fcstd_path.parent.mkdir(parents=True, exist_ok=True)
        step_path.parent.mkdir(parents=True, exist_ok=True)
        # FreeCAD 1.1 on this Windows host terminates when its own document
        # writer receives a non-ASCII path.  Keep the process-facing staging
        # paths ASCII, then copy the completed files into the canonical local
        # repository path from the host process.  This does not alter CAIR or
        # raw source artifacts.
        stage_root = Path.home() / "Documents" / "freecad" / "freecad_mcp_work" / "aec-intelligence-runs" / project_id
        stage_root.mkdir(parents=True, exist_ok=True)
        staged_input = stage_root / "cair-scene-input.json"
        staged_script = stage_root / "build-cair-scene.py"
        staged_fcstd = stage_root / f"cair-derived-scene-{snapshot_slug}.FCStd"
        staged_step = stage_root / f"cair-derived-scene-{snapshot_slug}.step"
        shutil.copy2(input_path, staged_input)
        shutil.copy2(script_path, staged_script)
        command = [probe.executable, "-c", f"exec(open(r'{staged_script}', encoding='utf-8').read())", "--pass", str(staged_input), str(staged_fcstd), str(staged_step)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return FreeCADSceneResult("FAILED", project_id, probe.executable, str(fcstd_path), str(step_path), object_count, geometry_count, errors=[str(exc)], checks={"command": command})

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        report_line = next((line for line in reversed(stdout.splitlines()) if line.strip().startswith("{")), "")
        report: dict[str, Any] = {}
        if report_line:
            try:
                report = json.loads(report_line)
            except json.JSONDecodeError:
                report = {}
        checks = {
            "returncode": completed.returncode,
            "fcstd_stage_exists": staged_fcstd.is_file() and staged_fcstd.stat().st_size > 0,
            "step_stage_exists": staged_step.is_file() and staged_step.stat().st_size > 0,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "created_objects": report.get("objects", 0),
            "fcstd_objects": report.get("fcstd_objects", 0),
            "solid_geometry_count": report.get("solid_geometry_count", 0),
            "step_imported": report.get("step_imported", False),
            "step_edges": report.get("step_edges", 0),
            "step_solids": report.get("step_solids", 0),
            "source_bbox": report.get("source_bbox", {}),
            "step_bbox": report.get("step_bbox", {}),
            "planar_bbox_deviation": report.get("planar_bbox_deviation", {}),
            "planar_max_deviation": report.get("planar_max_deviation"),
            "planar_bbox_match": report.get("planar_bbox_match", False),
            "solid_mode": bool(solid_mode),
            "solid_candidate_count": solid_candidate_count,
            "source_units": "millimeters",
            "derivation": "CAIR closed-planar-polyline extrusion; default height 3000 mm; not authoritative" if solid_mode else "CAIR geometry wireframe; default height 3000 mm; not authoritative",
        }
        solid_contract_ok = (
            not solid_mode
            or (report.get("solid_geometry_count", 0) == solid_candidate_count and report.get("step_solids", 0) >= solid_candidate_count and solid_candidate_count > 0)
        )
        if completed.returncode != 0 or not checks["fcstd_stage_exists"] or not checks["step_stage_exists"] or report.get("objects", 0) != geometry_count or report.get("fcstd_objects", 0) < geometry_count or not report.get("step_imported", False) or not report.get("planar_bbox_match", False) or not solid_contract_ok:
            return FreeCADSceneResult("FAILED", project_id, probe.executable, str(fcstd_path), str(step_path), object_count, geometry_count, solid_geometry_count=int(report.get("solid_geometry_count", 0) or 0), errors=["FreeCAD did not produce the expected derived FCStd and STEP outputs"], checks=checks)

        shutil.copy2(staged_fcstd, fcstd_path)
        shutil.copy2(staged_step, step_path)
        checks["fcstd_exists"] = fcstd_path.is_file() and fcstd_path.stat().st_size > 0
        checks["step_exists"] = step_path.is_file() and step_path.stat().st_size > 0

        records = {
            "DERIVED/FREECAD/FCSTD": store.put(fcstd_path, project_id, "DERIVED/FREECAD/FCSTD", relative_destination=str(fcstd_path.relative_to(root)), application="FreeCAD", representation="CAIR solid review model" if solid_mode else "CAIR geometry wireframe review model", source_snapshot=snapshot, source_geometry_count=geometry_count, solid_mode=bool(solid_mode), solid_geometry_count=int(report.get("solid_geometry_count", 0) or 0), step_solids=int(report.get("step_solids", 0) or 0)),
            "DERIVED/BIM/STEP": store.put(step_path, project_id, "DERIVED/BIM/STEP", relative_destination=str(step_path.relative_to(root)), application="FreeCAD", representation="CAIR solid exchange" if solid_mode else "CAIR geometry wireframe exchange", source_snapshot=snapshot, source_geometry_count=geometry_count, solid_mode=bool(solid_mode), solid_geometry_count=int(report.get("solid_geometry_count", 0) or 0), step_solids=int(report.get("step_solids", 0) or 0)),
        }
        for record in records.values():
            store.annotate(record.artifact_id, source_snapshot=snapshot, source_geometry_count=geometry_count, solid_mode=bool(solid_mode), solid_geometry_count=int(report.get("solid_geometry_count", 0) or 0), step_solids=int(report.get("step_solids", 0) or 0), derivation="CAIR closed-planar-polyline extrusion; default height 3000 mm; not authoritative" if solid_mode else "CAIR geometry wireframe; default height 3000 mm; not authoritative")
        canonical_paths = {artifact_type: str(root / record.local_path) for artifact_type, record in records.items()}
        checks["canonical_paths"] = canonical_paths
        return FreeCADSceneResult("SUCCESS", project_id, probe.executable, canonical_paths["DERIVED/FREECAD/FCSTD"], canonical_paths["DERIVED/BIM/STEP"], object_count, geometry_count, solid_geometry_count=int(report.get("solid_geometry_count", 0) or 0), checks=checks)

"""Headless Blender hand-off for CAIR geometry previews.

The adapter keeps CAIR and its geometry index authoritative. Blender receives
only a derived visualization scene with source identity and unit metadata
attached to every object; it never rewrites raw CAD or CAIR artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

from .application_adapters import BlenderApplicationAdapter
from .repository import RepositoryLayout
from .storage import LocalArtifactStore


@dataclass
class BlenderSceneResult:
    status: str
    project_id: str
    executable: str | None
    blend_path: str | None = None
    glb_path: str | None = None
    preview_path: str | None = None
    object_count: int = 0
    geometry_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _source_entity_id(obj: dict[str, Any]) -> str | None:
    source = obj.get("source") or {}
    return source.get("entity_id") or source.get("source_object")


def _scene_payload(project_path: Path) -> tuple[dict[str, Any], int, int]:
    cair_path = project_path / "03_CAIR" / "project-cair.json"
    geometry_path = project_path / "03_CAIR" / "geometry-index.jsonl"
    if not cair_path.is_file() or not geometry_path.is_file():
        raise FileNotFoundError("project CAIR and geometry index are required")
    cair = json.loads(cair_path.read_text(encoding="utf-8"))
    geometry_rows = _read_jsonl(geometry_path)
    geometry_by_handle = {
        str(row.get("handle") or row.get("source_entity_id") or row.get("source_id") or row.get("feature_id")): row
        for row in geometry_rows
    }
    objects = []
    for obj in cair.get("objects", []):
        handle = _source_entity_id(obj)
        geometry = geometry_by_handle.get(str(handle)) if handle else None
        if geometry is None:
            continue
        geometry_value = geometry.get("geometry")
        if not isinstance(geometry_value, dict) or not geometry_value:
            # Spatial/semantic IFC objects can be valid CAIR objects without
            # carrying renderable geometry. Keep them in CAIR/KG, but do not
            # ask a visual application to create a phantom object.
            continue
        objects.append(
            {
                "id": obj.get("id"),
                "type": obj.get("type"),
                "source_format": (obj.get("source") or {}).get("format"),
                "source_entity_id": handle,
                "geometry_ref": obj.get("geometry_ref"),
                "geometry": geometry_value,
                "bbox": geometry.get("bbox") or {},
                "properties": geometry.get("properties") or {},
                "source_file": (obj.get("source") or {}).get("file"),
            }
        )
    payload = {
        "project_id": cair.get("project_id"),
        "schema_version": cair.get("schema_version"),
        "source_snapshot": cair.get("snapshot_id"),
        "source_unit_scale": 0.001,
        "objects": objects,
    }
    return payload, len(cair.get("objects", [])), len(objects)


def _blender_script() -> str:
    return r'''
import bpy, json, math, sys

input_path, blend_path, glb_path, preview_path = sys.argv[-4:]
payload = json.load(open(input_path, encoding="utf-8"))
scale = float(payload.get("source_unit_scale", 1.0))

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
for datablocks in (bpy.data.curves, bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
    for datablock in list(datablocks):
        if datablock.users == 0:
            datablocks.remove(datablock)

collection = bpy.data.collections.new("AEC_CAIR_DERIVED")
bpy.context.scene.collection.children.link(collection)
created = 0
geometry_refs = []
all_xy = []

def points_for(geometry):
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
    return []

def mesh_data_for(geometry):
    raw_vertices = geometry.get("vertices", [])
    points = [raw_vertices[index:index + 3] for index in range(0, len(raw_vertices), 3) if len(raw_vertices[index:index + 3]) == 3]
    faces = []
    raw_faces = geometry.get("faces", [])
    for index in range(0, len(raw_faces), 3):
        face = [int(value) for value in raw_faces[index:index + 3]]
        if len(face) == 3 and all(0 <= value < len(points) for value in face):
            faces.append(face)
    return points, faces

for item in payload.get("objects", []):
    geometry = item.get("geometry", {})
    data_block = None
    if geometry.get("kind") == "text":
        curve = bpy.data.curves.new("AEC_CAIR_TEXT", type="FONT")
        curve.body = str(item.get("properties", {}).get("text", "AEC"))
        curve.size = max(float(item.get("properties", {}).get("height", 250.0)) * scale, 0.01)
        location = geometry.get("location", [0, 0, 0])
        points = []
        all_xy.append((float(location[0]) * scale, float(location[1]) * scale))
        data_block = curve
    elif geometry.get("kind") == "mesh":
        mesh_points, mesh_faces = mesh_data_for(geometry)
        if not mesh_points or not mesh_faces:
            continue
        mesh = bpy.data.meshes.new("AEC_CAIR_MESH")
        mesh.from_pydata(
            [(float(point[0]) * scale, float(point[1]) * scale, float(point[2]) * scale) for point in mesh_points],
            [],
            mesh_faces,
        )
        mesh.update()
        data_block = mesh
        for point in mesh_points:
            all_xy.append((float(point[0]) * scale, float(point[1]) * scale))
    else:
        points = points_for(geometry)
        if len(points) < 2:
            continue
        curve = bpy.data.curves.new("AEC_CAIR_CURVE", type="CURVE")
        curve.dimensions = "3D"
        curve.bevel_depth = 0.01
        curve.bevel_resolution = 2
        spline = curve.splines.new("POLY")
        spline.points.add(len(points) - 1)
        for point, coords in zip(spline.points, points):
            point.co = ((float(coords[0]) * scale), (float(coords[1]) * scale), (float(coords[2]) * scale if len(coords) > 2 else 0.0), 1.0)
            all_xy.append((float(coords[0]) * scale, float(coords[1]) * scale))
        spline.use_cyclic_u = bool(geometry.get("closed", False) or geometry.get("kind") == "circle")
        data_block = curve
    obj = bpy.data.objects.new(str(item.get("id") or "AECObject"), data_block)
    if geometry.get("kind") == "text":
        obj.location = (float(location[0]) * scale, float(location[1]) * scale, float(location[2]) * scale if len(location) > 2 else 0.0)
    obj["aec_id"] = item.get("id")
    obj["aec_type"] = item.get("type")
    obj["geometry_ref"] = item.get("geometry_ref")
    obj["source_entity_id"] = item.get("source_entity_id")
    obj["source_format"] = item.get("source_format")
    obj["source_unit_scale"] = scale
    collection.objects.link(obj)
    created += 1
    if item.get("geometry_ref"):
        geometry_refs.append(item["geometry_ref"])

bpy.context.scene["aec_project_id"] = payload.get("project_id")
bpy.context.scene["aec_source_snapshot"] = payload.get("source_snapshot")
bpy.context.scene["aec_schema_version"] = payload.get("schema_version")
bpy.context.scene["aec_source_unit_scale"] = scale
bpy.context.scene.render.engine = "BLENDER_WORKBENCH"
bpy.context.scene.render.resolution_x = 1000
bpy.context.scene.render.resolution_y = 700
bpy.context.scene.render.resolution_percentage = 100
if all_xy:
    min_x = min(point[0] for point in all_xy)
    max_x = max(point[0] for point in all_xy)
    min_y = min(point[1] for point in all_xy)
    max_y = max(point[1] for point in all_xy)
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    extent = max(max_x - min_x, max_y - min_y, 1.0)
    camera_data = bpy.data.cameras.new("AEC_CAIR_CAMERA")
    camera = bpy.data.objects.new("AEC_CAIR_CAMERA", camera_data)
    camera.location = (center_x, center_y, extent * 1.8)
    camera.rotation_euler = (0.0, 0.0, 0.0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = extent * 1.25
    bpy.context.scene.collection.objects.link(camera)
    bpy.context.scene.camera = camera
bpy.context.scene.render.filepath = preview_path
bpy.context.scene.world.color = (0.035, 0.035, 0.035)
bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=blend_path)
bpy.ops.object.select_all(action="SELECT")
bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB", use_selection=True)
print(json.dumps({"objects": created, "geometry_refs": geometry_refs, "blend_path": blend_path, "glb_path": glb_path}))
'''


class BlenderSceneAdapter:
    """Create a reviewable Blender/GLB visualization from a project CAIR."""

    def __init__(self, executable: str | Path | None = None):
        self.executable = str(executable) if executable else None

    def export_project(self, repository_root: str | Path, project_id: str, timeout: int = 300) -> BlenderSceneResult:
        root = Path(repository_root).resolve()
        project = RepositoryLayout(root).project(project_id).ensure()
        probe = BlenderApplicationAdapter().probe(self.executable)
        if probe.status != "AVAILABLE":
            return BlenderSceneResult("REQUIRES_CONFIGURATION", project_id, probe.executable, warnings=list(probe.warnings))
        try:
            payload, object_count, geometry_count = _scene_payload(project.path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return BlenderSceneResult("FAILED", project_id, probe.executable, errors=[str(exc)])

        run_root = root / "runtime" / "external" / "blender-runs" / project_id
        run_root.mkdir(parents=True, exist_ok=True)
        input_path = run_root / "cair-scene-input.json"
        script_path = run_root / "build-cair-scene.py"
        snapshot = str(payload.get("source_snapshot") or "snapshot")
        snapshot_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", snapshot).strip("-") or "snapshot"
        blend_path = project.path / "06_MODELS" / "BLENDER" / f"cair-derived-scene-{snapshot_slug}.blend"
        glb_path = project.path / "02_DERIVED" / "BIM" / "GLB" / f"cair-derived-scene-{snapshot_slug}.glb"
        preview_path = project.path / "02_DERIVED" / "PREVIEWS" / f"cair-derived-scene-{snapshot_slug}.png"
        store = LocalArtifactStore(root)
        existing = {
            artifact_type: next(
                (
                    record
                    for record in store.find(project_id=project_id, artifact_type=artifact_type, source_snapshot=snapshot)
                    if Path(root / record.local_path).is_file()
                ),
                None,
            )
            for artifact_type in ("DERIVED/BLENDER/BLEND", "DERIVED/BIM/GLB", "DERIVED/PREVIEWS/PNG")
        }
        if all(existing.values()):
            canonical_paths = {artifact_type: str(root / record.local_path) for artifact_type, record in existing.items()}
            return BlenderSceneResult(
                "SUCCESS",
                project_id,
                probe.executable,
                canonical_paths["DERIVED/BLENDER/BLEND"],
                canonical_paths["DERIVED/BIM/GLB"],
                canonical_paths["DERIVED/PREVIEWS/PNG"],
                object_count,
                geometry_count,
                checks={"reused": True, "source_snapshot": snapshot, "source_unit_scale": payload["source_unit_scale"], "created_objects": geometry_count, "canonical_paths": canonical_paths},
            )
        input_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        script_path.write_text(_blender_script(), encoding="utf-8")
        command = [probe.executable, "--background", "--factory-startup", "--python", str(script_path), "--", str(input_path), str(blend_path), str(glb_path), str(preview_path)]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return BlenderSceneResult("FAILED", project_id, probe.executable, str(blend_path), str(glb_path), str(preview_path), object_count, geometry_count, errors=[str(exc)], checks={"command": command})

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
            "blend_exists": blend_path.is_file() and blend_path.stat().st_size > 0,
            "glb_exists": glb_path.is_file() and glb_path.stat().st_size > 0,
            "preview_exists": preview_path.is_file() and preview_path.stat().st_size > 0,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "source_unit_scale": payload["source_unit_scale"],
            "created_objects": report.get("objects", 0),
        }
        if completed.returncode != 0 or not checks["blend_exists"] or not checks["glb_exists"] or not checks["preview_exists"] or report.get("objects", 0) != geometry_count:
            return BlenderSceneResult("FAILED", project_id, probe.executable, str(blend_path), str(glb_path), str(preview_path), object_count, geometry_count, errors=["Blender did not produce the expected derived scene outputs"], checks=checks)

        records = {
            "DERIVED/BLENDER/BLEND": store.put(blend_path, project_id, "DERIVED/BLENDER/BLEND", relative_destination=str(blend_path.relative_to(root)), application="Blender", representation="CAIR geometry visualization", source_snapshot=snapshot, source_geometry_count=geometry_count),
            "DERIVED/BIM/GLB": store.put(glb_path, project_id, "DERIVED/BIM/GLB", relative_destination=str(glb_path.relative_to(root)), application="Blender", representation="CAIR geometry visualization", source_snapshot=snapshot, source_geometry_count=geometry_count),
            "DERIVED/PREVIEWS/PNG": store.put(preview_path, project_id, "DERIVED/PREVIEWS/PNG", relative_destination=str(preview_path.relative_to(root)), application="Blender", representation="CAIR geometry visual QA", source_snapshot=snapshot, source_geometry_count=geometry_count),
        }
        for record in records.values():
            store.annotate(record.artifact_id, source_snapshot=snapshot, source_geometry_count=geometry_count)
        canonical_paths = {artifact_type: str(root / record.local_path) for artifact_type, record in records.items()}
        checks["canonical_paths"] = canonical_paths
        return BlenderSceneResult("SUCCESS", project_id, probe.executable, canonical_paths["DERIVED/BLENDER/BLEND"], canonical_paths["DERIVED/BIM/GLB"], canonical_paths["DERIVED/PREVIEWS/PNG"], object_count, geometry_count, checks=checks)

"""Local repository layout and manifest helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .ontology_alignment import write_ontology_alignment


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ProjectLayout:
    root: Path
    project_id: str

    @property
    def path(self) -> Path:
        return self.root / "projects" / self.project_id

    def ensure(self) -> "ProjectLayout":
        directories = [
            "00_MANIFEST",
            "01_RAW/CAD/DWG",
            "01_RAW/CAD/DXF",
            "01_RAW/CAD/SVG",
            "01_RAW/BIM/IFC",
            "01_RAW/BIM/STEP",
            "01_RAW/BIM/STL",
            "01_RAW/BIM/OBJ",
            "01_RAW/BIM/GLB",
            "01_RAW/BIM/GLTF",
            "01_RAW/BIM/RVT_EXPORT",
            "01_RAW/GIS/GPKG",
            "01_RAW/GIS/SHP",
            "01_RAW/GIS/GEOJSON",
            "01_RAW/GIS/DEM",
            "01_RAW/GIS/GEOTIFF",
            "01_RAW/DOCUMENTS/PDF",
            "01_RAW/DOCUMENTS/XLSX",
            "01_RAW/DOCUMENTS/DOCX",
            "01_RAW/IMAGES",
            "01_RAW/REFERENCES",
            "02_DERIVED/CAD/DXF",
            "02_DERIVED/CAD/SVG",
            "02_DERIVED/CAD/PNG",
            "02_DERIVED/BIM/IFC",
            "02_DERIVED/BIM/STEP",
            "02_DERIVED/BIM/GLB",
            "02_DERIVED/GIS/GEOJSON",
            "02_DERIVED/GIS/GPKG",
            "02_DERIVED/PREVIEWS",
            "02_DERIVED/THUMBNAILS",
            "03_CAIR/snapshots",
            "04_ONTOLOGY/diffs",
            "05_GRAPH",
            "06_MODELS/FREECAD",
            "06_MODELS/BLENDER",
            "07_GIS",
            "08_DOCUMENTS",
            "09_DESIGN",
            "10_ITERATIONS",
            "11_VALIDATION/parsing",
            "11_VALIDATION/semantic",
            "11_VALIDATION/geometry",
            "11_VALIDATION/visual",
            "11_VALIDATION/cross-format",
            "12_REPORTS",
            "13_AGENT_MEMORY",
            "90_ARCHIVE",
        ]
        for directory in directories:
            (self.path / directory).mkdir(parents=True, exist_ok=True)
        return self

    @property
    def manifest_path(self) -> Path:
        return self.path / "00_MANIFEST" / "project-manifest.json"

    def write_manifest(self, name: str, **extra: Any) -> Path:
        self.ensure()
        manifest = {
            "project_id": self.project_id,
            "name": name,
            "created_at": extra.pop("created_at", utc_now()),
            "status": extra.pop("status", "ACTIVE"),
            "schema_version": extra.pop("schema_version", "0.1.0"),
            "source_artifacts": extra.pop("source_artifacts", []),
            **extra,
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.manifest_path

    def read_manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class RepositoryLayout:
    root: Path

    def ensure(self) -> "RepositoryLayout":
        directories = [
            "projects",
            "global/00_GLOBAL",
            "global/03_KNOWLEDGE_GRAPH",
            "global/07_DESIGN_KNOWLEDGE/strategies",
            "global/07_DESIGN_KNOWLEDGE/problems",
            "global/07_DESIGN_KNOWLEDGE/solutions",
            "global/07_DESIGN_KNOWLEDGE/design-moves",
            "global/07_DESIGN_KNOWLEDGE/precedents",
            "global/07_DESIGN_KNOWLEDGE/lessons",
            "global/07_DESIGN_KNOWLEDGE/metrics",
            "global/07_DESIGN_KNOWLEDGE/failures",
            "global/07_DESIGN_KNOWLEDGE/design-patterns",
            "global/ontology/core",
            "global/ontology/building",
            "global/ontology/geometry",
            "global/ontology/gis",
            "global/ontology/provenance",
            "global/ontology/classification",
            "global/ontology/design",
            "global/ontology/cad",
            "global/ontology/project",
            "global/registry",
            "global/mappings",
            "global/schemas/cair",
            "global/schemas/mcp",
            "global/schemas/adapters",
            "schemas/cair",
            "schemas/mcp",
            "schemas/adapters",
            "runtime",
            "fixtures",
            "docs",
        ]
        for directory in directories:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self._ensure_packaged_cair_schema()
        write_ontology_alignment(self.root)
        return self

    def _ensure_packaged_cair_schema(self) -> None:
        """Materialize the versioned CAIR contract in a fresh repository.

        A repository must remain self-describing even when it is initialized
        outside the source checkout (for example in a temporary ingest job).
        The checked-in global schema is the canonical copy; this packaged copy
        is only the bootstrap source for a new repository.
        """
        resource_root = files("aec_intelligence").joinpath("resources")
        for filename in ("cair-v0.1.schema.json", "cair-tables-v0.1.schema.json"):
            target = self.root / "global" / "schemas" / "cair" / filename
            if target.exists():
                continue
            packaged = resource_root.joinpath(filename)
            target.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")

    def project(self, project_id: str) -> ProjectLayout:
        return ProjectLayout(self.root, project_id)

    @property
    def runtime_registry_path(self) -> Path:
        return self.root / "runtime" / "aec_registry.sqlite3"

    @property
    def global_manifest_path(self) -> Path:
        return self.root / "global" / "00_GLOBAL" / "global-manifest.json"

    def write_global_manifest(self, **extra: Any) -> Path:
        self.ensure()
        manifest = {
            "schema_version": "0.1.0",
            "ontology_version": "0.1.0",
            "total_projects": 0,
            "total_objects": 0,
            "total_artifacts": 0,
            "latest_global_snapshot": None,
            "project_registry": "global/00_GLOBAL/global-project-registry.jsonl",
            "last_ingest": None,
            "last_sync": None,
            **extra,
        }
        self.global_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.global_manifest_path

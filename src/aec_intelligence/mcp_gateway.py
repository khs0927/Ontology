"""Transport-neutral MCP-style gateway for the AEC semantic services.

This module deliberately contains no web server or connector credentials. It
defines stable tool contracts that an MCP transport can expose later while
keeping all work inside the existing CAIR and repository boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from .compliance import audit_repository
from .blender_adapter import BlenderSceneAdapter
from .freecad_adapter import FreeCADSceneAdapter, probe_native_ifc
from .dashboard import write_dashboard
from .application_adapters import QGISApplicationAdapter, default_application_adapters
from .document_adapters import DocumentAdapterUnavailable, parse_reference_document
from .asset3d_adapters import parse_3d_asset
from .raster_adapters import RasterAdapterUnavailable, parse_raster
from .cair import CAIRSnapshot, utc_now
from .dxf import DXFParser
from .dwg import select_dwg_converter
from .format_pipeline import SemanticFormatIngestionPipeline
from .formats import GISParser, IFCParser
from .graph import write_graph_exports
from .ontology import write_turtle
from .pipeline import DXFIngestionPipeline
from .query import HybridQueryRouter
from .registry import ArtifactRegistry
from .rebuild import rebuild_runtime_from_repository
from .rebuild import rebuild_global_indexes_from_projects
from .rebuild import rebuild_runtime_from_drive
from .rebuild import refresh_derived_exports
from .retrieval import CrossProjectRetriever, write_memory_packages
from .repository import RepositoryLayout
from .storage import GoogleDriveArtifactStore, LocalArtifactStore
from .validation_engine import validate_project, validate_repository, write_validation_report
from .iterations import IterationManager
from .intelligence_bridge import export_hydradb_projection, graph_backend_plan, inspect_code_context, preview_hydradb_projection
from .visual_reasoning import NvidiaCosmosVision, architectural_object_prompt, image_sha256, prepare_visual_input


def _dwg_converter(arguments: dict[str, Any]):
    """Select the DWG converter honoring tool arguments, then AEC_DWG_CONVERTER / AEC_*_EXECUTABLE env settings."""
    import os
    mode = arguments.get("dwg_converter") or os.getenv("AEC_DWG_CONVERTER", "auto")
    oda = arguments.get("oda_executable") or os.getenv("AEC_ODA_EXECUTABLE") or None
    libre = arguments.get("libredwg_executable") or os.getenv("AEC_LIBREDWG_EXECUTABLE") or None
    return select_dwg_converter(mode, oda, libre)


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.input_schema}


class MCPGateway:
    """Expose read/ingest/rebuild operations as deterministic tool calls."""

    def __init__(self, repository_root: str | Path, drive_client: Any | None = None, drive_root_id: str | None = None):
        self.repository_root = Path(repository_root).resolve()
        config_path = self.repository_root / "global" / "00_GLOBAL" / "drive-repository.json"
        configured_root_id = drive_root_id
        if configured_root_id is None and config_path.is_file():
            try:
                configured_root_id = json.loads(config_path.read_text(encoding="utf-8")).get("root_folder_id")
            except (json.JSONDecodeError, OSError):
                configured_root_id = None
        self.drive_client = drive_client
        self.drive_store = GoogleDriveArtifactStore(self.repository_root, client=drive_client, root_folder_id=configured_root_id)
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "aec.audit": self._audit,
            "aec.ingest_dxf": self._ingest_dxf,
            "aec.query_plan": self._query_plan,
            "aec.context_inspect_code": self._context_inspect_code,
            "aec.graph_backend_plan": self._graph_backend_plan,
            "aec.graph_hydradb_preview": self._graph_hydradb_preview,
            "aec.graph_hydradb_export": self._graph_hydradb_export,
            "aec.rebuild_runtime": self._rebuild_runtime,
            "aec.get_object": self._get_object,
            "aec.validate_project": self._validate_project,
            "aec.validate_repository": self._validate_repository,
            "aec.query_global_memory": self._query_global_memory,
            "aec.find_similar_projects": self._find_similar_projects,
            "aec.build_memory_packages": self._build_memory_packages,
            "aec.create_design_iteration": self._create_design_iteration,
            "aec.compare_iterations": self._compare_iterations,
            "aec.promote_iteration": self._promote_iteration,
            "aec.build_dashboard": self._build_dashboard,
            "aec.ingest_project": self._ingest_project,
            "aec.ingest_file": self._ingest_file,
            "aec.parse_cad": self._parse_cad,
            "aec.parse_ifc": self._parse_ifc,
            "aec.parse_gis": self._parse_gis,
            "aec.parse_reference_document": self._parse_reference_document,
            "aec.parse_3d_asset": self._parse_3d_asset,
            "aec.parse_raster": self._parse_raster,
            "aec.build_cair": self._build_cair,
            "aec.classify_objects": self._classify_objects,
            "aec.build_ontology": self._build_ontology,
            "aec.build_project_graph": self._build_project_graph,
            "aec.query_project": self._query_project,
            "aec.open_in_freecad": self._open_in_freecad,
            "aec.probe_native_ifc": self._probe_native_ifc,
            "aec.open_in_blender": self._open_in_blender,
            "aec.open_in_qgis": self._open_in_qgis,
            "aec.probe_qgis_runtime": self._probe_qgis_runtime,
            "aec.register_drive_repository": self._register_drive_repository,
            "aec.create_project_repository": self._create_project_repository,
            "aec.upload_project_source": self._upload_project_source,
            "aec.sync_project_to_drive": self._sync_project_to_drive,
            "aec.sync_global_memory": self._sync_global_memory,
            "aec.download_project_context": self._download_project_context,
            "aec.find_project_artifacts": self._find_project_artifacts,
            "aec.find_global_artifact": self._find_global_artifact,
            "aec.save_ontology": self._save_ontology,
            "aec.load_ontology": self._load_ontology,
            "aec.publish_cair_snapshot": self._publish_cair_snapshot,
            "aec.publish_graph_snapshot": self._publish_graph_snapshot,
            "aec.rebuild_project_from_drive": self._rebuild_project_from_drive,
            "aec.rebuild_global_memory_from_drive": self._rebuild_global_memory_from_drive,
            "aec.refresh_derived_exports": self._refresh_derived_exports,
            "aec.operational_search": self._operational_search,
            "aec.operational_ingest": self._operational_ingest,
            "aec.operational_get_object": self._operational_get_object,
            "aec.element_catalog": self._element_catalog,
            "aec.find_elements": self._find_elements,
            "aec.block_catalog": self._block_catalog,
            "aec.drawing_index": self._drawing_index,
            "aec.element_context": self._element_context,
            "aec.graph_rag_query": self._graph_rag_query,
            "aec.explain_path": self._explain_path,
            "aec.visual_inspect_artifact": self._visual_inspect_artifact,
            "aec.visual_validate_drive_project": self._visual_validate_drive_project,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [definition.to_dict() for definition in TOOL_DEFINITIONS]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if name not in self._handlers:
            return {"status": "FAILED", "error": f"unknown tool: {name}"}
        try:
            return self._handlers[name](arguments or {})
        except (KeyError, TypeError, ValueError, FileNotFoundError) as exc:
            return {"status": "FAILED", "error": str(exc)}
        except Exception as exc:  # transport must not turn an internal failure into a false success
            return {"status": "FAILED", "error": f"internal gateway error: {exc}"}

    def _audit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return audit_repository(self.repository_root).to_dict()

    def _visual_validate_path(
        self,
        source: Path,
        project_id: str,
        *,
        preview: Path | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Create advisory visual evidence without changing canonical CAIR."""

        client = NvidiaCosmosVision.from_env()
        if not client.enabled:
            return {
                "status": "REQUIRES_CONFIGURATION",
                "provider": "nvidia",
                "model": client.model,
                "canonical": False,
                "authority": "visual_advisory",
                "error": "NVIDIA_API_KEY is not set and no local NVIDIA_COSMOS_ENDPOINT is configured",
            }

        try:
            image, mime_type, visual_source = prepare_visual_input(source, preview)
            evidence = client.analyze_image(
                image,
                mime_type,
                architectural_object_prompt(source.name, context),
            )
        except Exception as exc:
            return {
                "status": "FAILED",
                "provider": "nvidia",
                "model": client.model,
                "canonical": False,
                "authority": "visual_advisory",
                "error": str(exc),
            }

        project = RepositoryLayout(self.repository_root).project(project_id).ensure()
        report_path = project.path / "11_VALIDATION" / "visual" / f"{source.stem}-nvidia-cosmos.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "schema": "aec-visual-observation/1",
            "status": evidence.get("status", "SUCCESS"),
            "provider": "nvidia",
            "model": client.model,
            "project_id": project_id,
            "source": str(source),
            "visual_source": str(visual_source),
            "image_sha256": image_sha256(image),
            "authority": "visual_advisory",
            "canonical": False,
            "may_mutate_cair": False,
            "evidence": evidence,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        store = LocalArtifactStore(self.repository_root)
        record = store.put(
            report_path,
            project_id,
            "DERIVED/VISUAL_OBSERVATION",
            relative_destination=report_path.relative_to(self.repository_root).as_posix(),
            provider="nvidia",
            model=client.model,
            authority="visual_advisory",
        )
        ArtifactRegistry(RepositoryLayout(self.repository_root).runtime_registry_path).register_artifact(record)
        return {
            "status": report["status"],
            "provider": "nvidia",
            "model": client.model,
            "authority": "visual_advisory",
            "canonical": False,
            "report_path": str(report_path),
            "artifact": record.to_dict(),
            "objects": evidence.get("objects", []),
            "candidate_relations": evidence.get("candidate_relations", []),
            "quality_issues": evidence.get("quality_issues", []),
        }

    def _visual_validate_ingest(
        self,
        source: Path,
        project_id: str,
        ingest_result: dict[str, Any],
        context: str | None = None,
    ) -> dict[str, Any]:
        outputs = ingest_result.get("outputs") or {}
        preview_value = outputs.get("preview")
        preview = Path(preview_value) if isinstance(preview_value, str) and preview_value else None
        return self._visual_validate_path(
            source,
            project_id,
            preview=preview,
            context=context or (
                "Drive-first ingestion validation: identify visible AEC objects, "
                "candidate relations, and ambiguous/low-quality evidence"
            ),
        )

    def _visual_inspect_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments["source"])).resolve()
        if not source.is_file():
            return {"status": "NOT_FOUND", "source": str(source)}
        project_id = str(arguments["project_id"])
        preview_value = arguments.get("preview")
        preview = Path(str(preview_value)).resolve() if preview_value else None
        return self._visual_validate_path(
            source,
            project_id,
            preview=preview,
            context=arguments.get("context"),
        )

    def _visual_validate_drive_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect already-visual Drive artifacts without reparsing canonical CAD."""

        store_or_status = self._drive_store_or_status()
        if isinstance(store_or_status, dict):
            return store_or_status
        client = NvidiaCosmosVision.from_env()
        if not client.enabled:
            return {
                "status": "REQUIRES_CONFIGURATION",
                "provider": "nvidia",
                "model": client.model,
                "error": "Set NVIDIA_API_KEY before sending Drive-derived visual evidence to NVIDIA",
            }

        project_id = str(arguments["project_id"])
        limit = max(1, min(int(arguments.get("limit", 20)), 100))
        include_derived = bool(arguments.get("include_derived", False))
        supported = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".svg"}
        candidates = []
        for item in self._remote_project_files(store_or_status, project_id):
            relative = str(
                item.get("relative_path")
                or item.get("local_path")
                or item.get("path")
                or ""
            ).replace("\\", "/")
            if Path(relative).suffix.lower() not in supported:
                continue
            if not include_derived and "/01_RAW/" not in relative:
                continue
            candidates.append(item)
        candidates = candidates[:limit]

        reports = []
        for item in candidates:
            try:
                record = store_or_status.materialize_remote(item)
                local_path = self.repository_root / record.local_path
                reports.append(
                    self._visual_validate_path(
                        local_path,
                        project_id,
                        context=str(arguments.get("context") or "Google Drive collection validation"),
                    )
                )
            except Exception as exc:
                reports.append({"status": "FAILED", "source": item.get("name"), "error": str(exc)})

        synced = None
        if bool(arguments.get("sync_reports", True)) and reports:
            synced = self._sync_project_to_drive({"project_id": project_id})
        return {
            "status": (
                "SUCCESS"
                if all(row.get("status") != "FAILED" for row in reports)
                else "SUCCESS_WITH_WARNINGS"
            ),
            "project_id": project_id,
            "processed": len(reports),
            "reports": reports,
            "drive_sync": synced,
        }

    def _ingest_dxf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments["source"]))
        project_id = str(arguments["project_id"])
        result = DXFIngestionPipeline(self.repository_root).ingest(source, project_id, arguments.get("name"), bool(arguments.get("force", False)))
        return {"status": result.validation.status, "project_id": result.project_id, "objects": len(result.snapshot.objects), "snapshot_id": result.snapshot.snapshot_id, "outputs": result.outputs}

    @staticmethod
    def _format_ingest_result(result: Any) -> dict[str, Any]:
        status = getattr(result, "status", None) or (result.validation.status if getattr(result, "validation", None) is not None else "SUCCESS")
        payload = {"status": status, "project_id": result.project_id, "source_format": getattr(result, "source_format", "DXF"), "objects": len(result.snapshot.objects), "relations": len(result.snapshot.relations), "snapshot_id": result.snapshot.snapshot_id, "outputs": result.outputs}
        if getattr(result, "skipped", False):
            payload["skipped"] = True
        if getattr(result, "validation", None) is not None:
            payload["validation"] = result.validation.to_dict()
        return payload

    def _ingest_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run Drive-first provenance when a connector is configured."""

        source = Path(str(arguments["source"])).resolve()
        project_id = str(arguments["project_id"])
        drive_configured = self.drive_store.client is not None and bool(self.drive_store.root_folder_id)
        if drive_configured:
            uploaded = self._upload_project_source({**arguments, "source": str(source), "project_id": project_id})
            if uploaded.get("status") != "SYNCED":
                return {"status": uploaded.get("status", "FAILED"), "project_id": project_id, "source": str(source), "drive_upload": uploaded}
        result = self._ingest_file_authoritative(arguments)
        accepted = {"SUCCESS", "SUCCESS_WITH_WARNINGS", "PARTIAL"}
        visual_requested = bool(arguments.get("visual_validate", drive_configured))
        if visual_requested and result.get("status") in accepted:
            result = {
                **result,
                "visual_validation": self._visual_validate_ingest(
                    source,
                    project_id,
                    result,
                    arguments.get("visual_context"),
                ),
            }
        if drive_configured and result.get("status") in accepted:
            result = {
                **result,
                "drive_sync": self._sync_project_to_drive({"project_id": project_id}),
            }
        return result

    def _ingest_file_authoritative(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments["source"])).resolve()
        project_id = str(arguments["project_id"])
        force = bool(arguments.get("force", False))
        name = arguments.get("name")
        if source.suffix.lower() in {".svg", ".pdf"}:
            return self._ingest_reference_document(source, project_id, name, force)
        if source.suffix.lower() in {".step", ".stp", ".stl", ".obj", ".glb", ".gltf"}:
            return self._ingest_3d_asset(source, project_id, name, force)
        if source.suffix.lower() in {".tif", ".tiff", ".dem"}:
            return self._ingest_raster_asset(source, project_id, name, force)
        if source.suffix.lower() == ".dxf":
            return self._format_ingest_result(DXFIngestionPipeline(self.repository_root).ingest(source, project_id, name, force))
        if source.suffix.lower() == ".dwg":
            output_dir = self.repository_root / ".cache" / "converted" / project_id
            conversion = _dwg_converter(arguments).convert_to_dxf(source, output_dir)
            if conversion.status != "SUCCESS" or not conversion.output:
                return {"status": conversion.status, "project_id": project_id, "source_format": "DWG", "conversion": conversion.to_dict()}
            pipeline = DXFIngestionPipeline(self.repository_root)
            raw_record = pipeline.store.put(
                source,
                project_id,
                "CAD/DWG",
                format="DWG",
                role="RAW_SOURCE",
                conversion_authority="ODA File Converter",
                conversion_status="SUCCESS",
            )
            pipeline.registry.register_artifact(raw_record)
            result = pipeline.ingest(Path(conversion.output), project_id, name, force)
            pipeline.store.link_source(result.source_artifact.artifact_id, raw_record.artifact_id)
            project = RepositoryLayout(self.repository_root).project(project_id).ensure()
            manifest = project.read_manifest()
            manifest.update({
                "source_format": "DWG",
                "authoritative_source_artifact": raw_record.artifact_id,
                "converted_source_artifact": result.source_artifact.artifact_id,
                "conversion": conversion.to_dict(),
                "source_artifacts": list(dict.fromkeys([raw_record.artifact_id, *manifest.get("source_artifacts", [])])),
            })
            project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            payload = self._format_ingest_result(result)
            payload.update({
                "source_format": "DWG",
                "conversion": conversion.to_dict(),
                "authoritative_source": raw_record.to_dict(),
                "converted_source": result.source_artifact.to_dict(),
            })
            return payload
        result = SemanticFormatIngestionPipeline(self.repository_root).ingest(source, project_id, name, force)
        return self._format_ingest_result(result)

    def _ingest_reference_document(self, source: Path, project_id: str, name: str | None, force: bool) -> dict[str, Any]:
        try:
            parsed = parse_reference_document(source)
        except DocumentAdapterUnavailable as exc:
            return {"status": "REQUIRES_DEPENDENCY", "project_id": project_id, "source_format": source.suffix.upper().lstrip("."), "error": str(exc)}
        project = RepositoryLayout(self.repository_root).project(project_id).ensure()
        store = LocalArtifactStore(self.repository_root)
        registry = ArtifactRegistry(RepositoryLayout(self.repository_root).runtime_registry_path)
        source_type = "CAD/SVG" if source.suffix.lower() == ".svg" else "DOCUMENTS/PDF"
        source_record = store.put(source, project_id, source_type, format=parsed.source_format, role="RAW_REFERENCE")
        registry.register_project(project_id, name or source.stem, utc_now())
        registry.register_artifact(source_record)
        report_path = project.path / "11_VALIDATION" / "parsing" / f"{source.stem}-{parsed.source_format.lower()}-reference-report.json"
        if project.manifest_path.is_file():
            manifest = project.read_manifest()
        else:
            project.write_manifest(name or source.stem)
            manifest = project.read_manifest()
        if not force and manifest.get("last_reference_hash") == source_record.sha256 and report_path.is_file():
            return {
                "status": parsed.status,
                "project_id": project_id,
                "source_format": parsed.source_format,
                "skipped": True,
                "source_artifact": source_record.to_dict(),
                "reference": parsed.to_dict(),
                "outputs": {"reference_report": str(report_path)},
                "semantic_cair_generated": False,
            }
        report = parsed.to_dict()
        report["policy"] = "Reference-document evidence only; source remains authoritative; no semantic CAIR snapshot generated by this route"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_record = store.put(
            report_path,
            project_id,
            "DERIVED/REFERENCE-DOCUMENT",
            relative_destination=str(report_path.relative_to(self.repository_root)),
            source_artifact_id=source_record.artifact_id,
            source_format=parsed.source_format,
        )
        registry.register_artifact(report_record)
        manifest.update({
            "source_format": parsed.source_format,
            "source_artifacts": list(dict.fromkeys([*manifest.get("source_artifacts", []), source_record.artifact_id])),
            "derived_artifacts": list(dict.fromkeys([*manifest.get("derived_artifacts", []), report_record.artifact_id])),
            "last_reference_hash": source_record.sha256,
            "last_ingest": utc_now(),
            "ingest_status": parsed.status,
            "reference_reports": list(dict.fromkeys([*manifest.get("reference_reports", []), str(report_path.relative_to(self.repository_root))])),
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
        return {
            "status": parsed.status,
            "project_id": project_id,
            "source_format": parsed.source_format,
            "source_artifact": source_record.to_dict(),
            "reference": parsed.to_dict(),
            "outputs": {"reference_report": str(report_path)},
            "semantic_cair_generated": False,
            "global_rebuild": global_rebuild,
        }

    def _ingest_3d_asset(self, source: Path, project_id: str, name: str | None, force: bool) -> dict[str, Any]:
        parsed = parse_3d_asset(source)
        project = RepositoryLayout(self.repository_root).project(project_id).ensure()
        store = LocalArtifactStore(self.repository_root)
        registry = ArtifactRegistry(RepositoryLayout(self.repository_root).runtime_registry_path)
        source_type = f"BIM/{parsed.source_format}"
        source_record = store.put(source, project_id, source_type, format=parsed.source_format, role="RAW_EXCHANGE")
        registry.register_project(project_id, name or source.stem, utc_now())
        registry.register_artifact(source_record)
        report_path = project.path / "11_VALIDATION" / "parsing" / f"{source.stem}-{parsed.source_format.lower()}-3d-report.json"
        manifest = project.read_manifest() if project.manifest_path.is_file() else None
        if manifest is None:
            project.write_manifest(name or source.stem)
            manifest = project.read_manifest()
        if not force and manifest.get("last_3d_hash") == source_record.sha256 and report_path.is_file():
            return {
                "status": parsed.status,
                "project_id": project_id,
                "source_format": parsed.source_format,
                "skipped": True,
                "source_artifact": source_record.to_dict(),
                "exchange": parsed.to_dict(),
                "outputs": {"exchange_report": str(report_path)},
                "semantic_cair_generated": False,
            }
        report = parsed.to_dict()
        report["policy"] = "3D exchange evidence only; source remains authoritative; no semantic CAIR snapshot generated by this route"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_record = store.put(
            report_path,
            project_id,
            "DERIVED/3D-METADATA",
            relative_destination=str(report_path.relative_to(self.repository_root)),
            source_artifact_id=source_record.artifact_id,
            source_format=parsed.source_format,
            representation="exchange metadata evidence",
        )
        registry.register_artifact(report_record)
        manifest.update({
            "source_format": parsed.source_format,
            "source_artifacts": list(dict.fromkeys([*manifest.get("source_artifacts", []), source_record.artifact_id])),
            "derived_artifacts": list(dict.fromkeys([*manifest.get("derived_artifacts", []), report_record.artifact_id])),
            "last_3d_hash": source_record.sha256,
            "last_ingest": utc_now(),
            "ingest_status": parsed.status,
            "exchange_reports": list(dict.fromkeys([*manifest.get("exchange_reports", []), str(report_path.relative_to(self.repository_root))])),
            "semantic_cair_generated": False,
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
        return {
            "status": parsed.status,
            "project_id": project_id,
            "source_format": parsed.source_format,
            "source_artifact": source_record.to_dict(),
            "exchange": parsed.to_dict(),
            "outputs": {"exchange_report": str(report_path)},
            "semantic_cair_generated": False,
            "global_rebuild": global_rebuild,
        }

    def _ingest_raster_asset(self, source: Path, project_id: str, name: str | None, force: bool) -> dict[str, Any]:
        try:
            parsed = parse_raster(source)
        except RasterAdapterUnavailable as exc:
            return {"status": "REQUIRES_DEPENDENCY", "project_id": project_id, "source_format": source.suffix.upper().lstrip("."), "error": str(exc)}
        project = RepositoryLayout(self.repository_root).project(project_id).ensure()
        store = LocalArtifactStore(self.repository_root)
        registry = ArtifactRegistry(RepositoryLayout(self.repository_root).runtime_registry_path)
        source_type = "GIS/GEOTIFF" if parsed.source_format == "GeoTIFF" else "GIS/DEM"
        source_record = store.put(source, project_id, source_type, format=parsed.source_format, role="RAW_RASTER")
        registry.register_project(project_id, name or source.stem, utc_now())
        registry.register_artifact(source_record)
        report_path = project.path / "11_VALIDATION" / "parsing" / f"{source.stem}-{parsed.source_format.lower()}-raster-report.json"
        manifest = project.read_manifest() if project.manifest_path.is_file() else None
        if manifest is None:
            project.write_manifest(name or source.stem)
            manifest = project.read_manifest()
        if not force and manifest.get("last_raster_hash") == source_record.sha256 and report_path.is_file():
            return {
                "status": parsed.status,
                "project_id": project_id,
                "source_format": parsed.source_format,
                "skipped": True,
                "source_artifact": source_record.to_dict(),
                "raster": parsed.to_dict(),
                "outputs": {"raster_report": str(report_path)},
                "semantic_cair_generated": False,
            }
        report = parsed.to_dict()
        report["policy"] = "Raster evidence only; source remains authoritative; no semantic CAIR snapshot generated by this route"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_record = store.put(
            report_path,
            project_id,
            "DERIVED/RASTER-METADATA",
            relative_destination=str(report_path.relative_to(self.repository_root)),
            source_artifact_id=source_record.artifact_id,
            source_format=parsed.source_format,
            representation="raster metadata evidence",
        )
        registry.register_artifact(report_record)
        manifest.update({
            "source_format": parsed.source_format,
            "source_artifacts": list(dict.fromkeys([*manifest.get("source_artifacts", []), source_record.artifact_id])),
            "derived_artifacts": list(dict.fromkeys([*manifest.get("derived_artifacts", []), report_record.artifact_id])),
            "last_raster_hash": source_record.sha256,
            "last_ingest": utc_now(),
            "ingest_status": parsed.status,
            "raster_reports": list(dict.fromkeys([*manifest.get("raster_reports", []), str(report_path.relative_to(self.repository_root))])),
            "semantic_cair_generated": False,
        })
        project.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
        return {
            "status": parsed.status,
            "project_id": project_id,
            "source_format": parsed.source_format,
            "source_artifact": source_record.to_dict(),
            "raster": parsed.to_dict(),
            "outputs": {"raster_report": str(report_path)},
            "semantic_cair_generated": False,
            "global_rebuild": global_rebuild,
        }

    def _ingest_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sources = arguments.get("sources") or ([arguments["source"]] if arguments.get("source") else [])
        if not sources:
            return {"status": "FAILED", "error": "ingest_project requires source or sources"}
        results = [self._ingest_file({**arguments, "source": source}) for source in sources]
        statuses = [result.get("status") for result in results]
        status = "FAILED" if any(value == "FAILED" for value in statuses) else ("SUCCESS_WITH_WARNINGS" if any(value == "SUCCESS_WITH_WARNINGS" for value in statuses) else "SUCCESS")
        return {"status": status, "project_id": str(arguments["project_id"]), "results": results}

    def _parse_cad(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = Path(str(arguments["source"])).resolve()
        if source.suffix.lower() == ".dxf":
            parsed = DXFParser().parse(source)
            return {"status": "SUCCESS", "source_format": "DXF", "parse": parsed.to_dict()}
        if source.suffix.lower() == ".dwg":
            conversion = _dwg_converter(arguments).convert_to_dxf(source, self.repository_root / ".cache" / "converted" / "parse")
            if conversion.status != "SUCCESS" or not conversion.output:
                return {"status": conversion.status, "source_format": "DWG", "conversion": conversion.to_dict()}
            parsed = DXFParser().parse(Path(conversion.output))
            return {"status": "SUCCESS", "source_format": "DWG", "conversion": conversion.to_dict(), "parse": parsed.to_dict()}
        return {"status": "FAILED", "error": "parse_cad accepts .dxf or .dwg"}

    def _parse_ifc(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = IFCParser().parse(Path(str(arguments["source"])).resolve())
        return {"status": "SUCCESS", "source_format": "IFC", "parse": parsed.to_dict()}

    def _parse_gis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = GISParser().parse(Path(str(arguments["source"])).resolve())
        return {"status": "SUCCESS", "source_format": "GIS", "parse": parsed.to_dict()}

    def _parse_reference_document(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_reference_document(Path(str(arguments["source"])).resolve())
        except DocumentAdapterUnavailable as exc:
            return {"status": "REQUIRES_DEPENDENCY", "source_format": Path(str(arguments["source"])).suffix.upper().lstrip("."), "error": str(exc)}
        return parsed.to_dict()

    def _parse_3d_asset(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_3d_asset(Path(str(arguments["source"])).resolve())
        return parsed.to_dict()

    def _parse_raster(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = parse_raster(Path(str(arguments["source"])).resolve())
        except RasterAdapterUnavailable as exc:
            return {"status": "REQUIRES_DEPENDENCY", "source_format": Path(str(arguments["source"])).suffix.upper().lstrip("."), "error": str(exc)}
        return parsed.to_dict()

    def _build_cair(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._ingest_file(arguments)

    def _classify_objects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._ingest_file(arguments)
        if result.get("status") in {"FAILED", "REQUIRES_CONFIGURATION"}:
            return result
        project_id = result["project_id"]
        snapshot_path = self.repository_root / "projects" / project_id / "03_CAIR" / "project-cair.json"
        snapshot = CAIRSnapshot.from_dict(json.loads(snapshot_path.read_text(encoding="utf-8")))
        return {**result, "classifications": [{"id": obj.id, "type": obj.type, "classification": obj.classification.to_dict() if obj.classification else None} for obj in snapshot.objects]}

    def _snapshot_for_project(self, project_id: str) -> CAIRSnapshot:
        path = self.repository_root / "projects" / project_id / "03_CAIR" / "project-cair.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        return CAIRSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _build_ontology(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        path = write_turtle(self._snapshot_for_project(project_id), self.repository_root / "projects" / project_id / "04_ONTOLOGY" / "project.ttl")
        return {"status": "SUCCESS", "project_id": project_id, "path": str(path)}

    def _build_project_graph(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        outputs = write_graph_exports(self._snapshot_for_project(project_id), self.repository_root / "projects" / project_id / "05_GRAPH")
        return {"status": "SUCCESS", "project_id": project_id, "outputs": {key: str(value) for key, value in outputs.items()}}

    def _query_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments["question"])
        project_id = str(arguments["project_id"])
        return CrossProjectRetriever(self.repository_root).query_global_memory(question, int(arguments.get("top_k", 10)), project_id)

    def _open_application(self, application: str, arguments: dict[str, Any]) -> dict[str, Any]:
        adapter = default_application_adapters()[application]
        status = adapter.probe(arguments.get("executable"))
        project_id = arguments.get("project_id")
        manifest_path = self.repository_root / "projects" / str(project_id) / "06_MODELS" / application.upper() / "cair-exchange.json" if project_id else None
        if application == "QGIS" and project_id:
            manifest_path = self.repository_root / "projects" / str(project_id) / "07_GIS" / "cair-exchange.json"
        if application == "Blender" and arguments.get("export_scene"):
            if not project_id:
                return {"status": "FAILED", "application": application, "error": "project_id is required when export_scene is true"}
            scene = BlenderSceneAdapter(arguments.get("executable")).export_project(self.repository_root, str(project_id))
            global_rebuild = None
            if scene.status == "SUCCESS":
                global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
            return {**status.to_dict(), "exchange_manifest": str(manifest_path) if manifest_path and manifest_path.is_file() else None, "action": "derived_scene_export", "scene": scene.to_dict(), "global_rebuild": global_rebuild}
        if application == "FreeCAD" and arguments.get("export_scene"):
            if not project_id:
                return {"status": "FAILED", "application": application, "error": "project_id is required when export_scene is true"}
            scene = FreeCADSceneAdapter(arguments.get("executable")).export_project(self.repository_root, str(project_id), solid_mode=bool(arguments.get("solid_mode", False)))
            global_rebuild = None
            if scene.status == "SUCCESS":
                global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
            return {**status.to_dict(), "exchange_manifest": str(manifest_path) if manifest_path and manifest_path.is_file() else None, "action": "derived_scene_export", "scene": scene.to_dict(), "global_rebuild": global_rebuild}
        return {**status.to_dict(), "exchange_manifest": str(manifest_path) if manifest_path and manifest_path.is_file() else None, "action": "probe_only; application launch requires an explicit reviewed adapter"}

    def _open_in_freecad(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._open_application("FreeCAD", arguments)

    def _probe_native_ifc(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Probe FreeCAD NativeIFC without changing CAIR or raw sources."""

        return probe_native_ifc(
            arguments["source"],
            arguments.get("executable"),
            self.repository_root,
            int(arguments.get("timeout", 300)),
        ).to_dict()

    def _open_in_blender(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._open_application("Blender", arguments)

    def _open_in_qgis(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._open_application("QGIS", arguments)

    def _probe_qgis_runtime(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return QGISApplicationAdapter().probe_runtime(
            arguments.get("executable"),
            arguments.get("source"),
            arguments.get("output"),
            float(arguments.get("distance", 1.0)),
            int(arguments.get("timeout", 120)),
        ).to_dict()

    def _register_drive_repository(self, arguments: dict[str, Any]) -> dict[str, Any]:
        root_folder_id = str(arguments["root_folder_id"])
        path = self.repository_root / "global" / "00_GLOBAL" / "drive-repository.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"root_folder_id": root_folder_id, "provider": "Google Drive", "configured_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.drive_store.root_folder_id = root_folder_id
        return {"status": "CONFIGURED", "root_folder_id": root_folder_id, "path": str(path), "client_injected": self.drive_store.client is not None}

    def _create_project_repository(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        layout = RepositoryLayout(self.repository_root).ensure().project(project_id).ensure()
        if not layout.manifest_path.is_file():
            layout.write_manifest(str(arguments.get("name", project_id)))
        return {"status": "SUCCESS", "project_id": project_id, "path": str(layout.path), "manifest": str(layout.manifest_path)}

    def _drive_configuration_required(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "REQUIRES_CONFIGURATION", "provider": "Google Drive", "error": "GoogleDriveClient injection is required for this operation; canonical local CAIR remains available"}

    def _drive_store_or_status(self) -> GoogleDriveArtifactStore | dict[str, Any]:
        if self.drive_store.client is None or not self.drive_store.root_folder_id:
            return self._drive_configuration_required({})
        return self.drive_store

    @staticmethod
    def _source_artifact_type(source: Path) -> str:
        mapping = {
            ".dwg": "CAD/DWG",
            ".dxf": "CAD/DXF",
            ".svg": "CAD/SVG",
            ".ifc": "BIM/IFC",
            ".step": "BIM/STEP",
            ".stp": "BIM/STEP",
            ".stl": "BIM/STL",
            ".obj": "BIM/OBJ",
            ".glb": "BIM/GLB",
            ".gltf": "BIM/GLTF",
            ".geojson": "GIS/GEOJSON",
            ".json": "GIS/GEOJSON",
            ".gpkg": "GIS/GPKG",
            ".shp": "GIS/SHP",
            ".tif": "GIS/GEOTIFF",
            ".tiff": "GIS/GEOTIFF",
            ".dem": "GIS/DEM",
            ".pdf": "DOCUMENTS/PDF",
        }
        return mapping.get(source.suffix.lower(), "REFERENCES")

    def _upload_project_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        store_or_status = self._drive_store_or_status()
        if isinstance(store_or_status, dict):
            return store_or_status
        source = Path(str(arguments["source"])).resolve()
        project_id = str(arguments["project_id"])
        layout = RepositoryLayout(self.repository_root).project(project_id).ensure()
        if not layout.manifest_path.is_file():
            layout.write_manifest(str(arguments.get("name") or project_id))
        record = store_or_status.put(
            source,
            project_id,
            str(arguments.get("artifact_type") or self._source_artifact_type(source)),
            format=source.suffix.upper().lstrip("."),
            role="RAW_SOURCE",
        )
        sync = store_or_status.sync(project_id=project_id)
        return {"status": sync["status"], "project_id": project_id, "artifact": record.to_dict(), "sync": sync}

    def _sync_project_to_drive(self, arguments: dict[str, Any]) -> dict[str, Any]:
        store_or_status = self._drive_store_or_status()
        if isinstance(store_or_status, dict):
            return store_or_status
        project_id = str(arguments["project_id"])
        layout = RepositoryLayout(self.repository_root).project(project_id).ensure()
        known_paths = {record.local_path.replace("\\", "/") for record in store_or_status.list(project_id)}
        for path in sorted(file for file in layout.path.rglob("*") if file.is_file()):
            relative = path.relative_to(self.repository_root).as_posix()
            if relative in known_paths:
                continue
            project_relative = path.relative_to(layout.path)
            artifact_type = f"PROJECT/{project_relative.parent.as_posix()}"
            store_or_status.put(path, project_id, artifact_type, relative_destination=relative, role="PROJECT_CANONICAL")
            known_paths.add(relative)
        sync = store_or_status.sync(project_id=project_id)
        return {"status": sync["status"], "project_id": project_id, "sync": sync}

    def _sync_global_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        store_or_status = self._drive_store_or_status()
        if isinstance(store_or_status, dict):
            return store_or_status
        global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
        global_roots = (
            self.repository_root / "global" / "00_GLOBAL",
            self.repository_root / "global" / "03_KNOWLEDGE_GRAPH",
            self.repository_root / "global" / "07_DESIGN_KNOWLEDGE",
            self.repository_root / "global" / "08_VALIDATION",
            self.repository_root / "global" / "09_AGENT_MEMORY",
            self.repository_root / "global" / "ontology",
            self.repository_root / "global" / "schemas",
            self.repository_root / "global" / "10_EXPORTS",
        )
        registered = []
        for root in global_roots:
            if not root.is_dir():
                continue
            for path in sorted(file for file in root.rglob("*") if file.is_file()):
                relative = path.relative_to(self.repository_root).as_posix()
                artifact_type = f"GLOBAL/{path.parent.relative_to(self.repository_root / 'global').as_posix()}"
                registered.append(store_or_status.put(path, "GLOBAL", artifact_type, relative_destination=relative, role="GLOBAL_CANONICAL").artifact_id)
        sync = store_or_status.sync(project_id="GLOBAL")
        return {"status": sync["status"], "registered": registered, "sync": sync, "global_rebuild": global_rebuild}

    def _remote_project_files(self, store: GoogleDriveArtifactStore, project_id: str) -> list[dict[str, Any]]:
        remote = store.list_remote(project_id)
        filtered = []
        for item in remote:
            relative = str(item.get("relative_path") or item.get("local_path") or item.get("path") or "").replace("\\", "/")
            item_project = str(item.get("project_id") or "")
            if item_project == project_id or relative.startswith(f"projects/{project_id}/"):
                filtered.append(item)
        return filtered

    def _download_project_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        store_or_status = self._drive_store_or_status()
        if isinstance(store_or_status, dict):
            return store_or_status
        project_id = str(arguments["project_id"])
        remote_files = self._remote_project_files(store_or_status, project_id)
        remote_files = [item for item in remote_files if "/13_AGENT_MEMORY/" in (str(item.get("relative_path") or item.get("local_path") or item.get("path") or "").replace("\\", "/"))]
        if not remote_files:
            return {"status": "NOT_FOUND", "project_id": project_id, "error": "no remote project context artifacts found"}
        records = [store_or_status.materialize_remote(item) for item in remote_files]
        return {"status": "SUCCESS", "project_id": project_id, "artifacts": [record.to_dict() for record in records]}

    def _rebuild_project_from_drive(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        return rebuild_runtime_from_drive(self.repository_root, self.drive_client, self.drive_store.root_folder_id, project_id)

    def _rebuild_global_memory_from_drive(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return rebuild_runtime_from_drive(self.repository_root, self.drive_client, self.drive_store.root_folder_id)

    def _refresh_derived_exports(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Backfill expanded tables/graphs from canonical CAIR without reparsing."""

        return refresh_derived_exports(self.repository_root, arguments.get("project_id"))

    def _find_project_artifacts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        records = LocalArtifactStore(self.repository_root).list(str(arguments["project_id"]))
        artifact_type = arguments.get("artifact_type")
        if artifact_type:
            records = [record for record in records if record.artifact_type == artifact_type]
        return {"status": "SUCCESS", "project_id": str(arguments["project_id"]), "artifacts": [record.to_dict() for record in records]}

    def _find_global_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.repository_root / "global" / "00_GLOBAL" / "global-artifact-registry.jsonl"
        needle = str(arguments.get("artifact_id", ""))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []
        matches = [row for row in rows if not needle or row.get("artifact_id") == needle or needle.lower() in str(row.get("filename", "")).lower()]
        return {"status": "SUCCESS", "artifacts": matches}

    def _save_ontology(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return {"status": "FAILED", "error": "save_ontology requires non-empty Turtle content"}
        path = self.repository_root / "projects" / project_id / "04_ONTOLOGY" / str(arguments.get("filename", "project.ttl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"status": "SUCCESS", "path": str(path)}

    def _load_ontology(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        path = self.repository_root / "projects" / project_id / "04_ONTOLOGY" / str(arguments.get("filename", "project.ttl"))
        if not path.is_file():
            return {"status": "NOT_FOUND", "path": str(path)}
        return {"status": "SUCCESS", "path": str(path), "content": path.read_text(encoding="utf-8")}

    def _publish_cair_snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        snapshot = self._snapshot_for_project(project_id)
        return {"status": "SUCCESS", "project_id": project_id, "snapshot_id": snapshot.snapshot_id, "path": str(self.repository_root / "projects" / project_id / "03_CAIR" / "snapshots" / snapshot.snapshot_id / "project-cair.json")}

    def _publish_graph_snapshot(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        path = self.repository_root / "projects" / project_id / "05_GRAPH" / "project.jsonld"
        if not path.is_file():
            return {"status": "NOT_FOUND", "path": str(path)}
        return {"status": "SUCCESS", "project_id": project_id, "path": str(path)}

    def _context_inspect_code(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return inspect_code_context(self.repository_root, arguments)

    def _graph_backend_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return graph_backend_plan(self.repository_root, arguments)

    def _graph_hydradb_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return preview_hydradb_projection(self.repository_root, arguments)

    def _graph_hydradb_export(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("graph_hydradb_export accepts no arguments; output is fixed under runtime/hydradb")
        return export_hydradb_projection(self.repository_root)

    def _query_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments["question"])
        return HybridQueryRouter().plan(question).__dict__

    def _rebuild_runtime(self, arguments: dict[str, Any]) -> dict[str, Any]:
        target = arguments.get("target")
        report = rebuild_runtime_from_repository(self.repository_root, target)
        return report.to_dict()

    def _get_object(self, arguments: dict[str, Any]) -> dict[str, Any]:
        object_id = str(arguments["object_id"])
        path = self.repository_root / "global" / "00_GLOBAL" / "global-object-registry.jsonl"
        if not path.exists():
            return {"status": "NOT_FOUND", "object": None}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("id") == object_id:
                return {"status": "SUCCESS", "object": json.loads(line)}
        return {"status": "NOT_FOUND", "object": None}

    def _validate_project(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        report = validate_project(self.repository_root, project_id)
        path = write_validation_report(report, self.repository_root / "projects" / project_id / "11_VALIDATION" / "cross-format" / "project-validation.json")
        return {**report.to_dict(), "report_path": str(path)}

    def _validate_repository(self, arguments: dict[str, Any]) -> dict[str, Any]:
        report = validate_repository(self.repository_root)
        path = write_validation_report(report, self.repository_root / "global" / "08_VALIDATION" / "repository-validation.json")
        return {**report.to_dict(), "report_path": str(path)}

    def _query_global_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return CrossProjectRetriever(self.repository_root).query_global_memory(str(arguments["question"]), int(arguments.get("top_k", 10)), arguments.get("project_id"))

    def _find_similar_projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = str(arguments["project_id"])
        return {"project_id": project_id, "hits": CrossProjectRetriever(self.repository_root).find_similar_projects(project_id, int(arguments.get("top_k", 5)))}

    def _build_memory_packages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        outputs = write_memory_packages(self.repository_root)
        return {"status": "SUCCESS", "outputs": {key: str(path) for key, path in outputs.items()}}

    def _create_design_iteration(self, arguments: dict[str, Any]) -> dict[str, Any]:
        record = IterationManager(self.repository_root).create(
            str(arguments["project_id"]),
            str(arguments["reason"]),
            arguments.get("changes", []),
            arguments.get("constraints", []),
            str(arguments.get("agent", "aec-agent")),
            arguments.get("artifacts", []),
            str(arguments.get("validation_status", "REQUIRES_REVIEW")),
            arguments.get("metrics_before"),
            arguments.get("metrics_after"),
        )
        return {"status": "SUCCESS", "iteration": record.to_dict()}

    def _compare_iterations(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"status": "SUCCESS", "comparison": IterationManager(self.repository_root).compare(str(arguments["project_id"]), str(arguments["left_iteration"]), str(arguments["right_iteration"]))}

    def _promote_iteration(self, arguments: dict[str, Any]) -> dict[str, Any]:
        record = IterationManager(self.repository_root).promote(str(arguments["project_id"]), str(arguments["iteration_id"]), str(arguments["approved_by"]))
        global_rebuild = rebuild_global_indexes_from_projects(self.repository_root)
        memory_outputs = write_memory_packages(self.repository_root)
        runtime = rebuild_runtime_from_repository(self.repository_root)
        return {
            "status": "SUCCESS",
            "iteration": record.to_dict(),
            "global_rebuild": global_rebuild,
            "memory_outputs": {key: str(path) for key, path in memory_outputs.items()},
            "runtime": runtime.to_dict(),
        }

    def _build_dashboard(self, arguments: dict[str, Any]) -> dict[str, Any]:
        output_directory = arguments.get("output_directory")
        outputs = write_dashboard(self.repository_root, output_directory)
        data = json.loads(outputs["data"].read_text(encoding="utf-8"))
        return {"status": data["status"], "outputs": {key: str(path) for key, path in outputs.items()}, "metrics": data["metrics"]}

    def _operational_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .operational.config import Settings
        from .operational.db import Database
        from .operational.search import SearchRouter
        from .classifier import normalize_storey
        settings = Settings.from_env()
        router = SearchRouter(Database(settings.dsn), settings)
        return router.search(
            query=str(arguments["query"]),
            project_id=arguments.get("project_id"),
            kind=arguments.get("kind"),
            storey=normalize_storey(arguments.get("storey")),
            top_k=int(arguments.get("top_k", 10)),
        ).to_dict()

    def _operational_ingest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .operational.config import Settings
        from .operational.db import Database
        from .operational.parsers import SUPPORTED
        import hashlib
        settings = Settings.from_env()
        db = Database(settings.dsn)
        target = Path(str(arguments["path"])).resolve()
        if not target.exists():
            return {"status": "FAILED", "error": f"path not found: {target}"}
        files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED]
        project_id = str(arguments.get("project_id", "P-DEFAULT"))
        discipline = str(arguments.get("discipline", "ARCH"))
        queue = str(arguments.get("queue", "cad"))
        job_ids = []
        for f in files:
            mtime = f.stat().st_mtime
            dedup = hashlib.sha256(f"{project_id}|{f}|{mtime}".encode("utf-8")).hexdigest()
            row = db.enqueue({"source": str(f), "name": f.name, "project_id": project_id, "discipline": discipline, "queue": queue}, dedup)
            job_ids.append(str(row["id"]))
        return {"status": "ENQUEUED", "enqueued_count": len(job_ids), "job_ids": job_ids}

    def _operational_get_object(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from .operational.config import Settings
        from .operational.db import Database
        settings = Settings.from_env()
        db = Database(settings.dsn)
        object_id = str(arguments["object_id"])
        with db.connect() as conn:
            obj = conn.execute("SELECT o.*, d.name as document_name, ST_AsGeoJSON(o.bounds)::jsonb as bounds_geojson FROM aec.objects o JOIN aec.documents d ON o.document_id = d.id WHERE o.id = %s", (object_id,)).fetchone()
            if not obj:
                return {"status": "NOT_FOUND", "object": None}
            rels = conn.execute("SELECT * FROM aec.relations WHERE project_id = %s AND (subject = %s OR object = %s)", (obj["project_id"], object_id, object_id)).fetchall()
        return {"status": "SUCCESS", "object": dict(obj), "relations": [dict(r) for r in rels]}

    # --- Operational element catalog (read-only, Postgres) ---------------------------
    def _catalog_call(self, function_name: str, **kwargs: Any) -> dict[str, Any]:
        import os
        dsn = os.getenv("AEC_DATABASE_URL", "").strip()
        if not dsn:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": "AEC_DATABASE_URL is not set; the element catalog reads the operational database"}
        try:
            import psycopg
            from .operational import catalog
            from .operational.db import Database
        except ImportError as exc:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": f"operational dependencies missing: {exc}"}
        try:
            result = getattr(catalog, function_name)(Database(dsn), **kwargs)
        except psycopg.OperationalError as exc:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": f"operational database unreachable: {str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__}"}
        if result is None:
            return {"status": "NOT_FOUND", "error": f"object not found: {kwargs.get('object_id')}"}
        return {"status": "SUCCESS", **json.loads(json.dumps(result, ensure_ascii=False, default=str))}

    def _graphrag_call(self, function_name: str, **kwargs: Any) -> dict[str, Any]:
        import os
        dsn = os.getenv("AEC_DATABASE_URL", "").strip()
        if not dsn:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": "AEC_DATABASE_URL is not set; Graph RAG reads the operational database"}
        try:
            import psycopg
            from .operational.db import Database
            from .operational.graphrag import ask
        except ImportError as exc:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": f"operational dependencies missing: {exc}"}
        try:
            result = getattr(ask, function_name)(Database(dsn), **kwargs)
        except psycopg.OperationalError as exc:
            return {"status": "REQUIRES_CONFIGURATION", "provider": "PostgreSQL", "error": f"operational database unreachable: {str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__}"}
        if result is None:
            return {"status": "NOT_FOUND", "error": f"node not found: {kwargs.get('node_id')}"}
        return {"status": "SUCCESS", **json.loads(json.dumps(result, ensure_ascii=False, default=str))}

    def _graph_rag_query(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments["question"]).strip()
        if not question:
            raise ValueError("question must not be empty")
        return self._graphrag_call("graph_rag_query", question=question, **self._pick(arguments, "project", "top_k", "generate"))

    def _explain_path(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._graphrag_call("explain_node", node_id=str(arguments["node_id"]), **self._pick(arguments, "limit"))

    @staticmethod
    def _pick(arguments: dict[str, Any], *keys: str) -> dict[str, Any]:
        return {key: arguments[key] for key in keys if arguments.get(key) is not None}

    def _element_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._catalog_call("element_catalog", **self._pick(arguments, "project_id"))

    def _find_elements(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._catalog_call("find_elements", **self._pick(arguments, "kind", "project_id", "document_id", "drawing_category", "layer", "block_name", "text", "bbox", "state", "storey", "include_properties", "limit", "cursor"))

    def _block_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._catalog_call("block_catalog", **self._pick(arguments, "project_id", "name_like", "limit", "cursor"))

    def _drawing_index(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._catalog_call("drawing_index", **self._pick(arguments, "project_id", "category", "limit", "cursor"))

    def _element_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._catalog_call("element_context", object_id=str(arguments["object_id"]), **self._pick(arguments, "hops", "limit"))


TOOL_DEFINITIONS = (
    MCPToolDefinition("aec.audit", "Audit a repository against the Global AEC framework guardrails", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.ingest_dxf", "Ingest a DXF source into a project CAIR snapshot", {"type": "object", "required": ["source", "project_id"], "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "name": {"type": ["string", "null"]}, "force": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.query_plan", "Choose the CAIR, graph, geometry, or global-memory query route", {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.context_inspect_code", "Use Jevgrep as a read-only code-context selector; external source egress is denied unless explicitly approved", {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}, "root": {"type": ["string", "null"]}, "excludes": {"type": "array", "items": {"type": "string"}}, "command_prefix": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "allow_source_egress": {"type": "boolean"}, "timeout": {"type": "number", "minimum": 1, "maximum": 300}}, "additionalProperties": False}),
    MCPToolDefinition("aec.graph_backend_plan", "Choose the graph accelerator by required capability while keeping CAIR canonical", {"type": "object", "properties": {"local_only": {"type": "boolean"}, "requires_sparql": {"type": "boolean"}, "object_store_durability": {"type": "boolean"}, "distributed_compute": {"type": "boolean"}, "neo4j_protocol": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.graph_hydradb_preview", "Render a HydraDB/OpenCypher projection in memory without writing canonical or runtime files", {"type": "object", "properties": {"max_statements": {"type": "integer", "minimum": 0, "maximum": 200}}, "additionalProperties": False}),
    MCPToolDefinition("aec.graph_hydradb_export", "Write a rebuildable HydraDB/OpenCypher seed only under runtime/hydradb", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.rebuild_runtime", "Rebuild a disposable runtime registry from canonical global indexes", {"type": "object", "properties": {"target": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.get_object", "Retrieve one object from the global CAIR object index", {"type": "object", "required": ["object_id"], "properties": {"object_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.validate_project", "Run integrated cross-format and artifact validation for one project", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.validate_repository", "Run integrated validation for all projects and write the global report", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.query_global_memory", "Search CAIR-derived cross-project semantic memory", {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}, "top_k": {"type": "integer", "minimum": 0}, "project_id": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.find_similar_projects", "Find projects with similar CAIR semantic fingerprints", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "top_k": {"type": "integer", "minimum": 0}}, "additionalProperties": False}),
    MCPToolDefinition("aec.build_memory_packages", "Build rebuildable project and global agent context packages", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.create_design_iteration", "Create a versioned design-memory iteration without overwriting source artifacts", {"type": "object", "required": ["project_id", "reason"], "properties": {"project_id": {"type": "string"}, "reason": {"type": "string"}, "changes": {"type": "array", "items": {"type": "string"}}, "constraints": {"type": "array", "items": {"type": "string"}}, "agent": {"type": "string"}, "artifacts": {"type": "array", "items": {"type": "string"}}, "validation_status": {"type": "string"}, "metrics_before": {"type": ["object", "null"]}, "metrics_after": {"type": ["object", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.compare_iterations", "Compare two design-memory iterations", {"type": "object", "required": ["project_id", "left_iteration", "right_iteration"], "properties": {"project_id": {"type": "string"}, "left_iteration": {"type": "string"}, "right_iteration": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.promote_iteration", "Promote a successfully validated iteration with explicit approval metadata", {"type": "object", "required": ["project_id", "iteration_id", "approved_by"], "properties": {"project_id": {"type": "string"}, "iteration_id": {"type": "string"}, "approved_by": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.build_dashboard", "Build a source-backed repository status dashboard artifact", {"type": "object", "properties": {"output_directory": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.ingest_project", "Ingest one or more project sources through the format adapters", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "source": {"type": ["string", "null"]}, "sources": {"type": "array", "items": {"type": "string"}}, "name": {"type": ["string", "null"]}, "force": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.ingest_file", "Dispatch one source file to its authoritative format ingestion pipeline; when Drive is configured, optional NVIDIA visual observations remain non-canonical", {"type": "object", "required": ["source", "project_id"], "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "name": {"type": ["string", "null"]}, "force": {"type": "boolean"}, "oda_executable": {"type": ["string", "null"]}, "visual_validate": {"type": "boolean"}, "visual_context": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_cad", "Parse DXF or convert DWG through the explicitly configured ODA boundary", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}, "oda_executable": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_ifc", "Parse IFC with IfcOpenShell and return normalized parser evidence", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_gis", "Parse GeoJSON, GPKG, or SHP with CRS preservation", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_reference_document", "Inspect SVG or PDF drawing reference metadata without generating semantic CAIR geometry", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_3d_asset", "Inspect STEP, STL, OBJ, GLB, or glTF exchange metadata without inferring semantic CAIR", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.parse_raster", "Inspect GeoTIFF or DEM dimensions, CRS, transform, bounds, and value evidence without generating semantic CAIR", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.build_cair", "Build a CAIR snapshot from an authoritative source file", {"type": "object", "required": ["source", "project_id"], "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "name": {"type": ["string", "null"]}, "force": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.classify_objects", "Return persisted CAIR classifications and evidence for a source", {"type": "object", "required": ["source", "project_id"], "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "name": {"type": ["string", "null"]}, "force": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.build_ontology", "Build the project Turtle ontology from CAIR", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.build_project_graph", "Build portable JSON-LD and graph tables from CAIR", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.query_project", "Search one project through the CAIR-derived retrieval route", {"type": "object", "required": ["project_id", "question"], "properties": {"project_id": {"type": "string"}, "question": {"type": "string"}, "top_k": {"type": "integer", "minimum": 0}}, "additionalProperties": False}),
    MCPToolDefinition("aec.open_in_freecad", "Probe FreeCAD and optionally export a derived CAIR FCStd/STEP review model", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}, "executable": {"type": ["string", "null"]}, "export_scene": {"type": "boolean"}, "solid_mode": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.probe_native_ifc", "Run FreeCAD NativeIFC on an IFC source and require a non-null shape for geometry success", {"type": "object", "required": ["source"], "properties": {"source": {"type": "string"}, "executable": {"type": ["string", "null"]}, "timeout": {"type": "integer", "minimum": 1}}, "additionalProperties": False}),
    MCPToolDefinition("aec.open_in_blender", "Probe Blender and optionally export a derived CAIR scene and GLB", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}, "executable": {"type": ["string", "null"]}, "export_scene": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.open_in_qgis", "Probe the configured QGIS adapter and expose its CAIR exchange manifest", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}, "executable": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.probe_qgis_runtime", "Validate an explicit headless QGIS runtime and optionally run native:buffer to a GeoPackage", {"type": "object", "properties": {"executable": {"type": ["string", "null"]}, "source": {"type": ["string", "null"]}, "output": {"type": ["string", "null"]}, "distance": {"type": "number", "minimum": 0}, "timeout": {"type": "integer", "minimum": 1}}, "additionalProperties": False}),
    MCPToolDefinition("aec.register_drive_repository", "Register a Google Drive root identifier without changing sharing permissions", {"type": "object", "required": ["root_folder_id"], "properties": {"root_folder_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.create_project_repository", "Create the standard local project repository layout", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "name": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.upload_project_source", "Upload a project source through an injected Drive client", {"type": "object", "required": ["project_id", "source"], "properties": {"project_id": {"type": "string"}, "source": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.sync_project_to_drive", "Synchronize project artifacts through an injected Drive client", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.sync_global_memory", "Synchronize global registries and memory through an injected Drive client", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.download_project_context", "Download project context through an injected Drive client", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.find_project_artifacts", "Find locally indexed artifacts for one project", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "artifact_type": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.find_global_artifact", "Find an artifact in the canonical global registry", {"type": "object", "properties": {"artifact_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.save_ontology", "Save explicitly supplied project Turtle content", {"type": "object", "required": ["project_id", "content"], "properties": {"project_id": {"type": "string"}, "content": {"type": "string"}, "filename": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.load_ontology", "Load a project Turtle ontology artifact", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "filename": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.publish_cair_snapshot", "Resolve the latest immutable CAIR snapshot for a project", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.publish_graph_snapshot", "Resolve the latest portable graph snapshot for a project", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.rebuild_project_from_drive", "Rebuild a project from Drive through an injected client", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.rebuild_global_memory_from_drive", "Rebuild global memory from Drive through an injected client", {"type": "object", "properties": {}, "additionalProperties": False}),
    MCPToolDefinition("aec.refresh_derived_exports", "Backfill derived CAIR tables and graph interchange without rewriting canonical CAIR", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.operational_search", "Execute multi-stage hybrid search across PostgreSQL and Apache AGE", {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "project_id": {"type": ["string", "null"]}, "kind": {"type": ["string", "null"]}, "storey": {"type": ["string", "null"], "description": "Storey such as 2F, 2층, B1, 지하1층, RF"}, "top_k": {"type": "integer"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.operational_ingest", "Enqueue CAD, PDF, or IFC sources into the operational database worker queue", {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "project_id": {"type": ["string", "null"]}, "discipline": {"type": ["string", "null"]}, "queue": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.operational_get_object", "Retrieve an architectural object and its relations from PostgreSQL and Apache AGE", {"type": "object", "required": ["object_id"], "properties": {"object_id": {"type": "string"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.element_catalog", "Read first: table of contents of parsed drawings in the operational database - element counts by kind (with Korean aliases), drawing category, layer, block, relation predicate and project", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.find_elements", "List parsed elements with CAD handles, layer, block name, attributes, bbox and source evidence; filter by kind (Door/문/창호...), drawing_category (평면도/상세도...), storey (2F/2층/B1/지하1층/RF), layer, block_name, text, bbox; paginate with cursor", {"type": "object", "properties": {"kind": {"type": ["string", "array", "null"], "items": {"type": "string"}}, "project_id": {"type": ["string", "null"]}, "document_id": {"type": ["string", "null"]}, "drawing_category": {"type": ["string", "null"]}, "layer": {"type": ["string", "null"]}, "block_name": {"type": ["string", "null"]}, "text": {"type": ["string", "null"]}, "bbox": {"type": ["array", "string", "null"], "items": {"type": "number"}}, "state": {"type": ["string", "null"]}, "storey": {"type": ["string", "null"], "description": "Storey such as 2F, 2층, B1, 지하1층, RF"}, "include_properties": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.block_catalog", "CAD block library aggregated by name across drawings: definitions, instance counts, attribute tags, layers, xref/anonymous flags and what the instances were classified as", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}, "name_like": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.drawing_index", "Sheet index: documents and layouts with drawing category, title-block number/title/scale and per-sheet element counts", {"type": "object", "properties": {"project_id": {"type": ["string", "null"]}, "category": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}, "cursor": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.graph_rag_query", "Graph RAG question answering over the ingested drawings (Korean): routes to keyword / vector / knowledge-graph traversal / project summaries and answers with the local LLM only from retrieved context; every claim cites document, sheet/layout, object ids and bbox, and unsupported questions are refused", {"type": "object", "required": ["question"], "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2000}, "project": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 30}, "generate": {"type": "boolean"}}, "additionalProperties": False}),
    MCPToolDefinition("aec.explain_path", "One knowledge-graph node (kg:...) with its typed incoming/outgoing edges, to explain the graph path behind a Graph RAG answer", {"type": "object", "required": ["node_id"], "properties": {"node_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "additionalProperties": False}),
    MCPToolDefinition("aec.element_context", "Neighbourhood of one element through relations in both directions (contains, instanceOf, hasTitleBlock, hasSection ...), 1-2 hops, confirmed against the AGE graph", {"type": "object", "required": ["object_id"], "properties": {"object_id": {"type": "string"}, "hops": {"type": "integer", "minimum": 1, "maximum": 2}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, "additionalProperties": False}),
    MCPToolDefinition("aec.visual_inspect_artifact", "Use NVIDIA Cosmos Reason to create non-canonical VisualObservation/CandidateObject evidence for one local drawing image, PDF, SVG, or CAD preview", {"type": "object", "required": ["source", "project_id"], "properties": {"source": {"type": "string"}, "project_id": {"type": "string"}, "preview": {"type": ["string", "null"]}, "context": {"type": ["string", "null"]}}, "additionalProperties": False}),
    MCPToolDefinition("aec.visual_validate_drive_project", "Materialize a bounded set of visual Google Drive project artifacts, inspect them with NVIDIA Cosmos Reason, and sync advisory reports without changing CAIR truth", {"type": "object", "required": ["project_id"], "properties": {"project_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "include_derived": {"type": "boolean"}, "sync_reports": {"type": "boolean"}, "context": {"type": ["string", "null"]}}, "additionalProperties": False}),
)

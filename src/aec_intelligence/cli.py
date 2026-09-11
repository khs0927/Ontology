"""Command-line entrypoint for the first milestone."""

from __future__ import annotations

import argparse
from pathlib import Path
import json

from .compliance import audit_repository
from .dashboard import write_dashboard
from .application_adapters import QGISApplicationAdapter, default_application_adapters
from .formats import GISParser, IFCParser
from .freecad_adapter import probe_native_ifc
from .mcp_gateway import MCPGateway
from .retrieval import write_memory_packages
from .pipeline import DXFIngestionPipeline
from .rebuild import rebuild_global_indexes_from_projects, refresh_derived_exports
from .repository import RepositoryLayout
from .validation_engine import validate_project, validate_repository, write_validation_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aec", description="AEC Semantic Operating System prototype")
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="create repository architecture")
    init.add_argument("path", nargs="?", default=None, help="optional repository path")
    ingest = subparsers.add_parser("ingest-dxf", help="ingest a DXF into CAIR")
    ingest.add_argument("source")
    ingest.add_argument("--project-id", required=True)
    ingest.add_argument("--name", default=None)
    ingest.add_argument("--force", action="store_true", help="create a new snapshot even when the source hash is unchanged")
    ingest_file = subparsers.add_parser("ingest-file", help="ingest IFC, GIS, CAD, reference, 3D, or raster sources through the format boundary")
    ingest_file.add_argument("source")
    ingest_file.add_argument("--project-id", required=True)
    ingest_file.add_argument("--name", default=None)
    ingest_file.add_argument("--force", action="store_true")
    parse_ifc = subparsers.add_parser("parse-ifc", help="parse IFC with IfcOpenShell")
    parse_ifc.add_argument("source")
    parse_gis = subparsers.add_parser("parse-gis", help="parse GeoJSON/GPKG/SHP with CRS preservation")
    parse_gis.add_argument("source")
    parse_reference = subparsers.add_parser("parse-reference", help="inspect SVG/PDF reference metadata without generating CAIR")
    parse_reference.add_argument("source")
    parse_3d = subparsers.add_parser("parse-3d", help="inspect STEP/STL/OBJ/GLB/glTF exchange metadata without generating CAIR")
    parse_3d.add_argument("source")
    parse_raster = subparsers.add_parser("parse-raster", help="inspect GeoTIFF/DEM raster evidence without generating CAIR")
    parse_raster.add_argument("source")
    probe = subparsers.add_parser("probe-apps", help="probe configured FreeCAD, Blender, and QGIS executables")
    probe.add_argument("--freecad", default=None, help="explicit FreeCAD executable path")
    probe.add_argument("--blender", default=None, help="explicit Blender executable path")
    probe.add_argument("--qgis", default=None, help="explicit QGIS executable path")
    qgis_runtime = subparsers.add_parser("probe-qgis-runtime", help="validate an explicit headless QGIS runtime and optional buffer processing")
    qgis_runtime.add_argument("--qgis", required=True, help="explicit qgis_process.exe or official qgis_process wrapper path")
    qgis_runtime.add_argument("--source", default=None, help="optional vector source for a native:buffer acceptance run")
    qgis_runtime.add_argument("--output", default=None, help="optional GeoPackage output for the native:buffer acceptance run")
    qgis_runtime.add_argument("--distance", type=float, default=1.0)
    qgis_runtime.add_argument("--timeout", type=int, default=120)
    native_ifc = subparsers.add_parser("probe-native-ifc", help="probe FreeCAD NativeIFC geometry import without changing CAIR")
    native_ifc.add_argument("source")
    native_ifc.add_argument("--freecad", default=None, help="explicit FreeCAD executable path")
    native_ifc.add_argument("--timeout", type=int, default=300)
    subparsers.add_parser("audit", help="check repository against framework guardrails")
    validate = subparsers.add_parser("validate", help="run integrated project or repository validation")
    validate.add_argument("--project-id", default=None)
    subparsers.add_parser("memory", help="build rebuildable project and global agent-memory packages")
    subparsers.add_parser("rebuild-global", help="rebuild derived global JSONL indexes from present projects")
    refresh = subparsers.add_parser("refresh-derived", help="backfill derived CAIR tables and graph interchange from canonical CAIR")
    refresh.add_argument("--project-id", default=None)
    dashboard = subparsers.add_parser("dashboard", help="build source-backed repository dashboard artifacts")
    dashboard.add_argument("--output-directory", default=None)
    op = subparsers.add_parser("operational", help="PostgreSQL and Apache AGE operational commands")
    op.add_argument("operational_args", nargs=argparse.REMAINDER, help="operational subcommands and arguments")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.path if args.command == "init" and args.path else args.root).resolve()
    if args.command == "init":
        layout = RepositoryLayout(root).ensure()
        layout.write_global_manifest()
        print(json.dumps({"status": "INITIALIZED", "root": str(root)}, ensure_ascii=False))
        return 0
    if args.command == "ingest-dxf":
        result = DXFIngestionPipeline(root).ingest(args.source, args.project_id, args.name, args.force)
        print(json.dumps({"status": result.validation.status, "project_id": result.project_id, "objects": len(result.snapshot.objects), "outputs": result.outputs, "validation": result.validation.to_dict()}, ensure_ascii=False, indent=2))
        return 0 if result.validation.status != "FAILED" else 2
    if args.command == "ingest-file":
        result = MCPGateway(root).call_tool("aec.ingest_file", {"source": str(Path(args.source).resolve()), "project_id": args.project_id, "name": args.name, "force": args.force})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 2
    if args.command == "parse-ifc":
        result = IFCParser().parse(args.source)
        print(json.dumps({"status": "SUCCESS", "parse": result.to_dict()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "parse-gis":
        result = GISParser().parse(args.source)
        print(json.dumps({"status": "SUCCESS", "parse": result.to_dict()}, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"parse-reference", "parse-3d", "parse-raster"}:
        tool_name = {
            "parse-reference": "aec.parse_reference_document",
            "parse-3d": "aec.parse_3d_asset",
            "parse-raster": "aec.parse_raster",
        }[args.command]
        result = MCPGateway(root).call_tool(tool_name, {"source": str(Path(args.source).resolve())})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 2
    if args.command == "probe-apps":
        executable_paths = {"FreeCAD": args.freecad, "Blender": args.blender, "QGIS": args.qgis}
        result = {name: adapter.probe(executable_paths[name]).to_dict() for name, adapter in default_application_adapters().items()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "probe-qgis-runtime":
        result = QGISApplicationAdapter().probe_runtime(args.qgis, args.source, args.output, args.distance, args.timeout)
        print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
        return 0 if result.status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 2
    if args.command == "probe-native-ifc":
        result = probe_native_ifc(args.source, args.freecad, root, args.timeout)
        # Keep the CLI JSON portable on legacy Windows console encodings.
        print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
        return 0 if result.status == "SUCCESS" else 2
    if args.command == "audit":
        report = audit_repository(root)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.status != "FAIL" else 3
    if args.command == "validate":
        if args.project_id:
            report = validate_project(root, args.project_id)
            report_path = write_validation_report(report, root / "projects" / args.project_id / "11_VALIDATION" / "cross-format" / "project-validation.json")
        else:
            report = validate_repository(root)
            report_path = write_validation_report(report, root / "global" / "08_VALIDATION" / "repository-validation.json")
        payload = report.to_dict()
        if report_path:
            payload["report_path"] = str(report_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if report.status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 4
    if args.command == "memory":
        outputs = write_memory_packages(root)
        print(json.dumps({"status": "SUCCESS", "outputs": {key: str(path) for key, path in outputs.items()}}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "rebuild-global":
        print(json.dumps(rebuild_global_indexes_from_projects(root), ensure_ascii=False, indent=2))
        return 0
    if args.command == "refresh-derived":
        result = refresh_derived_exports(root, args.project_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 6
    if args.command == "dashboard":
        outputs = write_dashboard(root, args.output_directory)
        data = json.loads(outputs["data"].read_text(encoding="utf-8"))
        print(json.dumps({"status": data["status"], "metrics": data["metrics"], "outputs": {key: str(path) for key, path in outputs.items()}}, ensure_ascii=False, indent=2))
        return 0 if data["status"] in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} else 5
    if args.command == "operational":
        from .operational.cli import main as op_main
        op_main(args.operational_args)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CAIR hand-off boundaries for FreeCAD, Blender, and QGIS."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable

from .cair import CAIRSnapshot


@dataclass(frozen=True)
class ApplicationAdapterStatus:
    application: str
    status: str
    executable: str | None = None
    capabilities: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"application": self.application, "status": self.status, "executable": self.executable, "capabilities": list(self.capabilities), "warnings": list(self.warnings)}


@dataclass(frozen=True)
class QGISRuntimeProbeResult:
    """Machine-readable evidence from an explicit headless QGIS runtime."""

    application: str
    status: str
    executable: str | None = None
    version: str | None = None
    algorithms: tuple[str, ...] = ()
    processing: dict[str, Any] | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "application": self.application,
            "status": self.status,
            "executable": self.executable,
            "version": self.version,
            "algorithms": list(self.algorithms),
            "processing": self.processing,
            "checks": self.checks,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass
class ApplicationExchangeManifest:
    application: str
    project_id: str
    schema_version: str
    objects: list[dict[str, Any]]
    source_snapshot: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"application": self.application, "project_id": self.project_id, "schema_version": self.schema_version, "source_snapshot": self.source_snapshot, "objects": self.objects, "warnings": self.warnings, "policy": "CAIR identity and geometry references are exchanged; raw source geometry remains in the project artifact store"}


class ExecutableApplicationAdapter:
    def __init__(self, application: str, commands: Iterable[str], capabilities: Iterable[str]):
        self.application = application
        self.commands = tuple(commands)
        self.capabilities = tuple(capabilities)

    def probe(self, executable: str | None = None) -> ApplicationAdapterStatus:
        explicit = Path(executable).resolve() if executable else None
        resolved = str(explicit) if explicit and explicit.is_file() else (next((shutil.which(command) for command in self.commands if shutil.which(command)), None) if explicit is None else None)
        if resolved:
            return ApplicationAdapterStatus(self.application, "AVAILABLE", resolved, self.capabilities)
        return ApplicationAdapterStatus(self.application, "REQUIRES_CONFIGURATION", None, self.capabilities, (f"{self.application} executable was not found on PATH; configure an explicit executable path",))


class FreeCADApplicationAdapter(ExecutableApplicationAdapter):
    def __init__(self):
        super().__init__("FreeCAD", ("FreeCADCmd", "FreeCADCmd.exe"), ("CAIR geometry reference import", "IFC export", "BIM object creation"))


class BlenderApplicationAdapter(ExecutableApplicationAdapter):
    def __init__(self):
        super().__init__("Blender", ("blender", "blender.exe"), ("CAIR scene import", "GLB/GLTF export", "design iteration"))


class QGISApplicationAdapter(ExecutableApplicationAdapter):
    def __init__(self):
        super().__init__("QGIS", ("qgis_process", "qgis_process.exe", "qgis", "qgis.exe"), ("GIS layer import", "CRS-aware geometry", "GeoPackage export"))

    @staticmethod
    def _cmd_quote(value: str) -> str:
        if '"' in value:
            raise ValueError("QGIS command arguments cannot contain a double quote")
        return f'"{value}"'

    @staticmethod
    def _command(executable: str, *arguments: str) -> list[str]:
        """Build a safe command for qgis_process.exe or its environment wrapper."""
        path = Path(executable)
        if path.suffix.lower() in {".bat", ".cmd"}:
            # The official Windows package's wrapper initializes the QGIS/GDAL
            # environment. Keep the reviewed executable path explicit while
            # avoiding shell interpolation of user-controlled arguments.
            return ["cmd.exe", "/d", "/c", "call", QGISApplicationAdapter._cmd_quote(str(path)), *(QGISApplicationAdapter._cmd_quote(argument) for argument in arguments)]
        return [str(path), *arguments]

    def _run(self, executable: str, arguments: Iterable[str], timeout: int) -> subprocess.CompletedProcess[str]:
        command = self._command(executable, *tuple(arguments))
        # Passing a pre-built command line is intentional here: Python's
        # Windows argv quoting escapes the quotes needed by `cmd /c` when a
        # batch wrapper path contains spaces. All wrapper arguments are quoted
        # above and double quotes are rejected.
        invocation: str | list[str] = " ".join(command) if Path(executable).suffix.lower() in {".bat", ".cmd"} else command
        return subprocess.run(
            invocation,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def run_processing(self, executable: str | None, algorithm: str, parameters: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
        """Run one explicit qgis_process algorithm without changing CAIR."""
        status = self.probe(executable)
        if status.status != "AVAILABLE" or not status.executable:
            return {"status": status.status, "executable": status.executable, "warnings": list(status.warnings)}
        flags: list[str] = []
        for key, value in parameters.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            flags.append(f"--{key}={value}")
        command = self._command(status.executable, "run", algorithm, *flags)
        try:
            completed = self._run(status.executable, ("run", algorithm, *flags), timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "FAILED", "executable": status.executable, "algorithm": algorithm, "command": command, "errors": [str(exc)]}
        output = parameters.get("OUTPUT")
        output_exists = bool(output and Path(str(output)).is_file())
        result = {
            "status": "SUCCESS" if completed.returncode == 0 and (not output or output_exists) else "FAILED",
            "executable": status.executable,
            "algorithm": algorithm,
            "command": command,
            "returncode": completed.returncode,
            "output": str(output) if output is not None else None,
            "output_exists": output_exists,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
        if result["status"] == "FAILED":
            result["errors"] = ["QGIS processing did not produce the requested output"] if completed.returncode == 0 else [f"QGIS processing exited with return code {completed.returncode}"]
        return result

    def probe_runtime(self, executable: str | None = None, source: str | None = None, output: str | None = None, distance: float = 1.0, timeout: int = 120) -> QGISRuntimeProbeResult:
        """Validate QGIS itself and optionally execute a deterministic buffer."""
        status = self.probe(executable)
        if status.status != "AVAILABLE" or not status.executable:
            return QGISRuntimeProbeResult("QGIS", status.status, status.executable, warnings=status.warnings)
        errors: list[str] = []
        warnings: list[str] = []
        checks: dict[str, Any] = {"version_command": False, "processing_list": False}
        version: str | None = None
        algorithms: list[str] = []
        try:
            version_run = self._run(status.executable, ("--version",), timeout)
            version_text = "\n".join(part for part in (version_run.stdout, version_run.stderr) if part)
            version = next((line.strip() for line in version_text.splitlines() if line.strip().startswith("QGIS ")), None)
            checks["version_command"] = version_run.returncode == 0 and version is not None
            listing = self._run(status.executable, ("list",), timeout)
            listing_text = "\n".join(part for part in (listing.stdout, listing.stderr) if part)
            checks["processing_list"] = listing.returncode == 0
            algorithms = [algorithm for algorithm in ("native:buffer", "gdal:buffervectors") if algorithm in listing_text]
            if not checks["version_command"]:
                errors.append("QGIS --version did not return a recognizable version")
            if not checks["processing_list"]:
                errors.append(f"QGIS processing list exited with return code {listing.returncode}")
            if "native:buffer" not in algorithms:
                warnings.append("QGIS native:buffer was not advertised by the processing provider")
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
        processing = None
        if source is not None:
            if output is None:
                errors.append("QGIS processing validation requires an explicit output path")
            else:
                processing = self.run_processing(status.executable, "native:buffer", {"INPUT": source, "DISTANCE": distance, "SEGMENTS": 5, "DISSOLVE": True, "OUTPUT": output}, timeout)
                if processing.get("status") != "SUCCESS":
                    errors.extend(str(item) for item in processing.get("errors", []))
        result_status = "FAILED" if errors else ("SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS")
        return QGISRuntimeProbeResult("QGIS", result_status, status.executable, version, tuple(algorithms), processing, checks, tuple(warnings), tuple(errors))


def default_application_adapters() -> dict[str, ExecutableApplicationAdapter]:
    return {"FreeCAD": FreeCADApplicationAdapter(), "Blender": BlenderApplicationAdapter(), "QGIS": QGISApplicationAdapter()}


def write_application_exchange_manifest(snapshot: CAIRSnapshot, application: str, path: str | Path, object_ids: Iterable[str] | None = None) -> Path:
    """Write a semantic-only hand-off manifest for an application adapter."""
    selected = set(object_ids) if object_ids is not None else None
    objects = []
    for obj in snapshot.objects:
        if selected is not None and obj.id not in selected:
            continue
        objects.append({"id": obj.id, "type": obj.type, "geometry_ref": obj.geometry_ref, "classification": obj.classification.to_dict() if obj.classification else None, "source_format": obj.source.format})
    manifest = ApplicationExchangeManifest(application, snapshot.project_id, snapshot.schema_version, objects, snapshot.snapshot_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target

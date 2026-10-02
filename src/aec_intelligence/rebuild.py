"""Rebuild runtime indexes from portable repository files."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .cair import CAIRSnapshot
from .graph import write_global_graph_exports, write_graph_exports
from .registry import ArtifactRegistry
from .repository import RepositoryLayout
from .storage import ArtifactRecord, GoogleDriveArtifactStore, LocalArtifactStore
from .tables import write_portable_tables


@dataclass
class RebuildReport:
    status: str
    database_path: str
    projects: int
    artifacts: int
    objects: int
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]], key: str) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sorted(rows, key=lambda row: str(row.get(key, "")))),
        encoding="utf-8",
    )


def write_global_registry_parquets(repository_root: str | Path) -> dict[str, Path]:
    """Write optional Parquet mirrors for the portable global JSONL registries."""

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        # Parquet is an optional mirror; JSON mirrors and graph exports below
        # must still be written without pyarrow.
        pa = pq = None
    root = Path(repository_root).resolve()
    global_root = root / "global" / "00_GLOBAL"
    specs = {
        "global-project-registry": ("global-project-registry.jsonl", ("project_id", "name", "status", "last_ingest")),
        "global-artifact-registry": ("global-artifact-registry.jsonl", ("artifact_id", "project_id", "artifact_type", "filename", "local_path", "sha256", "size", "status", "version", "metadata_json", "security_classification")),
        "global-object-registry": ("global-object-registry.jsonl", ("id", "project_id", "type", "classification_json", "geometry_ref")),
        "global-relations": ("global-relations.jsonl", ("key", "subject", "predicate", "object", "confidence", "provenance_json")),
        "global-provenance": ("global-provenance.jsonl", ("object_id", "source_file", "source_object", "source_format", "source_hash", "parser", "parser_version", "timestamp", "agent", "transformation", "derived_from_json")),
    }
    outputs: dict[str, Path] = {}
    for stem, (jsonl_name, columns) in specs.items():
        rows = _read_jsonl(global_root / jsonl_name)
        json_mirror = global_root / f"{stem}.json"
        json_mirror.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        outputs[f"{stem}_json"] = json_mirror
        normalized: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            if "metadata_json" in columns:
                value["metadata_json"] = json.dumps(value.pop("metadata", {}) or {}, ensure_ascii=False, sort_keys=True)
            if "classification_json" in columns:
                value["classification_json"] = json.dumps(value.pop("classification", None), ensure_ascii=False, sort_keys=True)
            if "provenance_json" in columns:
                value["provenance_json"] = json.dumps(value.pop("provenance", {}) or {}, ensure_ascii=False, sort_keys=True)
            if "derived_from_json" in columns:
                value["derived_from_json"] = json.dumps(value.pop("derived_from", []) or [], ensure_ascii=False, sort_keys=True)
            normalized.append(value)
        if pq is None:
            continue
        path = global_root / f"{stem}.parquet"
        pq.write_table(pa.table({column: [row.get(column) for row in normalized] for column in columns}), path)
        outputs[stem] = path
    snapshots = []
    for project_dir in sorted(path for path in (root / "projects").glob("*") if path.is_dir()):
        cair_path = project_dir / "03_CAIR" / "project-cair.json"
        if not cair_path.is_file():
            continue
        try:
            snapshots.append(CAIRSnapshot.from_dict(json.loads(cair_path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    outputs.update(write_global_graph_exports(snapshots, root / "global" / "03_KNOWLEDGE_GRAPH"))
    return outputs


def refresh_derived_exports(repository_root: str | Path, project_id: str | None = None) -> dict[str, Any]:
    """Backfill current derived interchange files from existing canonical CAIR.

    This maintenance operation never reparses raw sources and never rewrites
    ``project-cair.json``. It exists so repositories created before a derived
    export contract was extended can be brought forward safely.
    """

    repository = RepositoryLayout(Path(repository_root).resolve()).ensure()
    store = LocalArtifactStore(repository.root)
    project_dirs = sorted(path for path in (repository.root / "projects").glob("*") if path.is_dir())
    if project_id:
        project_dirs = [path for path in project_dirs if path.name == project_id]
    if not project_dirs:
        return {"status": "NOT_FOUND", "project_id": project_id, "projects": 0, "files": 0, "warnings": ["no matching project directory"]}

    refreshed_projects = 0
    refreshed_files = 0
    warnings: list[str] = []
    for project_dir in project_dirs:
        cair_path = project_dir / "03_CAIR" / "project-cair.json"
        if not cair_path.is_file():
            warnings.append(f"{project_dir.name}: canonical CAIR is missing")
            continue
        try:
            snapshot = CAIRSnapshot.from_dict(json.loads(cair_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            warnings.append(f"{project_dir.name}: canonical CAIR could not be read: {exc}")
            continue

        table_root = project_dir / "03_CAIR" / "tables"
        source_geometry_rows = _read_jsonl(project_dir / "03_CAIR" / "geometry-index.jsonl")
        if not source_geometry_rows:
            # Older packages may have retained only the already-normalized
            # table copy. Reuse it rather than replacing valid references with
            # an empty geometry table during maintenance.
            source_geometry_rows = _read_jsonl(table_root / "geometry_index.jsonl") or _read_jsonl(table_root / "geometry-index.jsonl")
        geometry_refs_by_source = {
            str(obj.source.entity_id): obj.geometry_ref
            for obj in snapshot.objects
            if obj.geometry_ref and obj.source.entity_id
        }
        geometry_rows = []
        for row in source_geometry_rows:
            geometry_row = dict(row)
            source_entity_id = row.get("handle") or row.get("source_entity_id") or row.get("source_id") or row.get("feature_id")
            if source_entity_id is not None and str(source_entity_id) in geometry_refs_by_source:
                # Preserve the CAIR reference already established by the
                # authoritative ingest; do not synthesize a replacement URI.
                geometry_row["geometry_ref"] = geometry_refs_by_source[str(source_entity_id)]
            geometry_rows.append(geometry_row)
        table_export = write_portable_tables(
            snapshot,
            table_root,
            geometry_rows=geometry_rows,
            artifact_rows=[record.to_dict() for record in store.list(project_dir.name)],
        )
        cair_manifest_path = project_dir / "03_CAIR" / "cair-manifest.json"
        cair_manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": snapshot.schema_version,
                    "project_id": snapshot.project_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "status": snapshot.status,
                    "object_count": len(snapshot.objects),
                    "relation_count": len(snapshot.relations),
                    "canonical_cair": "project-cair.json",
                    "geometry_index": "geometry-index.jsonl",
                    "tables": {key: str(path.name) for key, path in table_export.files.items()},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        graph_paths = write_graph_exports(snapshot, project_dir / "05_GRAPH")
        manifest_path = project_dir / "00_MANIFEST" / "project-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        source_artifact_id = next(iter(manifest.get("source_artifacts", [])), None)
        derived_ids = list(manifest.get("derived_artifacts") or [])
        output_paths: list[tuple[str, Path]] = [("CAIR_MANIFEST", cair_manifest_path)]
        output_paths.extend((f"TABLE_{key.upper().replace('.', '_').replace('-', '_')}", path) for key, path in table_export.files.items())
        output_paths.extend((key.upper(), path) for key, path in graph_paths.items())
        for artifact_type, output_path in output_paths:
            record = store.put(
                output_path,
                project_dir.name,
                f"DERIVED/{artifact_type}",
                relative_destination=output_path.relative_to(repository.root).as_posix(),
                source_artifact_id=source_artifact_id,
            )
            if record.artifact_id not in derived_ids:
                derived_ids.append(record.artifact_id)
            refreshed_files += 1
        if manifest_path.is_file():
            manifest.update(
                {
                    "derived_artifacts": derived_ids,
                    "portable_table_format": table_export.format,
                    "portable_table_warnings": table_export.warnings,
                    "derived_refresh": "CAIR_ONLY_BACKFILL",
                }
            )
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        refreshed_projects += 1

    return {
        "status": "SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS",
        "project_id": project_id,
        "projects": refreshed_projects,
        "files": refreshed_files,
        "warnings": warnings,
    }


def rebuild_global_indexes_from_projects(repository_root: str | Path) -> dict[str, Any]:
    """Rebuild global JSONL indexes from projects that are present locally.

    A project moved out of ``projects/`` (for example an isolated smoke run)
    must not remain discoverable through global retrieval or a Drive snapshot.
    The operation only rewrites derived global indexes; project CAIR and raw
    artifacts remain untouched.
    """
    repository = RepositoryLayout(Path(repository_root).resolve()).ensure()
    global_root = repository.root / "global" / "00_GLOBAL"
    project_dirs = sorted(path for path in (repository.root / "projects").glob("*") if path.is_dir())
    project_ids = {path.name for path in project_dirs}

    project_rows = [row for row in _read_jsonl(global_root / "global-project-registry.jsonl") if row.get("project_id") in project_ids]
    known_project_ids = {row.get("project_id") for row in project_rows}
    for path in project_dirs:
        manifest_path = path / "00_MANIFEST" / "project-manifest.json"
        if path.name in known_project_ids or not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        project_rows.append(
            {
                "project_id": path.name,
                "name": manifest.get("name", path.name),
                "status": manifest.get("status", "ACTIVE"),
                "last_ingest": manifest.get("last_ingest", manifest.get("created_at")),
            }
        )

    # Keep global artifacts that were materialized from Drive.  They are not
    # project rows, but dropping them here would make the next runtime rebuild
    # forget the canonical global graph, memory, and registry exports.
    artifact_rows = [
        record.to_dict()
        for record in LocalArtifactStore(repository.root).list()
        if record.project_id in project_ids or record.project_id == "GLOBAL"
    ]
    object_rows = [row for row in _read_jsonl(global_root / "global-object-registry.jsonl") if row.get("project_id") in project_ids]
    object_ids = {row.get("id") for row in object_rows}
    def relation_is_local(row: dict[str, Any]) -> bool:
        values = (row.get("subject"), row.get("object"))
        return all(
            isinstance(value, str)
            and any(value.startswith(f"aec://project/{project_id}") for project_id in project_ids)
            for value in values
        )

    relation_rows = []
    for project_dir in project_dirs:
        graph_path = project_dir / "05_GRAPH" / "relationships.jsonl"
        for row in _read_jsonl(graph_path):
            if not relation_is_local(row):
                continue
            relation_rows.append({"key": f"{row['subject']}|{row['predicate']}|{row['object']}", **row})
    if not relation_rows:
        relation_rows = [row for row in _read_jsonl(global_root / "global-relations.jsonl") if relation_is_local(row)]
    provenance_rows = [row for row in _read_jsonl(global_root / "global-provenance.jsonl") if row.get("object_id") in object_ids]

    _write_jsonl(global_root / "global-project-registry.jsonl", project_rows, "project_id")
    _write_jsonl(global_root / "global-artifact-registry.jsonl", artifact_rows, "artifact_id")
    _write_jsonl(global_root / "global-object-registry.jsonl", object_rows, "id")
    _write_jsonl(global_root / "global-relations.jsonl", relation_rows, "key")
    _write_jsonl(global_root / "global-provenance.jsonl", provenance_rows, "object_id")

    last_ingest = max((str(row.get("last_ingest") or "") for row in project_rows), default=None)
    repository.write_global_manifest(
        total_projects=len(project_rows),
        total_objects=len(object_rows),
        total_artifacts=len(artifact_rows),
        last_ingest=last_ingest,
        latest_ingest_status="SUCCESS" if project_rows else None,
    )
    write_global_registry_parquets(repository.root)
    return {
        "status": "SUCCESS",
        "projects": len(project_rows),
        "artifacts": len(artifact_rows),
        "objects": len(object_rows),
        "relations": len(relation_rows),
        "provenance": len(provenance_rows),
    }


def rebuild_runtime_from_repository(repository_root: str | Path, target_database: str | Path | None = None) -> RebuildReport:
    repository = RepositoryLayout(Path(repository_root).resolve()).ensure()
    global_root = repository.root / "global" / "00_GLOBAL"
    target = Path(target_database).resolve() if target_database else repository.runtime_registry_path
    target.parent.mkdir(parents=True, exist_ok=True)
    registry = ArtifactRegistry(target)
    warnings: list[str] = []
    projects = _read_jsonl(global_root / "global-project-registry.jsonl")
    artifacts = _read_jsonl(global_root / "global-artifact-registry.jsonl")
    objects = _read_jsonl(global_root / "global-object-registry.jsonl")
    for project in projects:
        registry.register_project(project["project_id"], project.get("name", project["project_id"]), project.get("last_ingest", ""), project.get("status", "ACTIVE"))
    for value in artifacts:
        try:
            registry.register_artifact(ArtifactRecord(**value))
        except TypeError as exc:
            warnings.append(f"artifact {value.get('artifact_id', '?')} skipped: {exc}")
    for value in objects:
        registry.register_object(value["id"], value["project_id"], value["type"], value.get("classification"), value.get("geometry_ref"))
    registry.close()
    return RebuildReport("SUCCESS_WITH_WARNINGS" if warnings else "SUCCESS", str(target), len(projects), len(artifacts) - len(warnings), len(objects), warnings)


def rebuild_runtime_from_drive(
    repository_root: str | Path,
    drive_client: Any | None = None,
    drive_root_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Materialize Drive artifacts and rebuild disposable runtime memory.

    Google Drive remains a persistent artifact source; this function only
    writes the local cache and derived runtime indexes. It deliberately keeps
    connector details behind the injected client's ``list_files`` and
    ``download`` contract.
    """

    root = Path(repository_root).resolve()
    store = GoogleDriveArtifactStore(root, client=drive_client, root_folder_id=drive_root_id)
    if store.client is None or not store.root_folder_id:
        return {
            "status": "REQUIRES_CONFIGURATION",
            "provider": "Google Drive",
            "error": "GoogleDriveClient injection and root folder ID are required",
        }
    remote_files = store.list_remote(project_id)
    if project_id:
        remote_files = [
            item
            for item in remote_files
            if str(item.get("project_id") or "") == project_id
            or str(item.get("relative_path") or item.get("local_path") or item.get("path") or "").replace("\\", "/").startswith(f"projects/{project_id}/")
        ]
    if not remote_files:
        return {"status": "NOT_FOUND", "project_id": project_id, "error": "no remote artifacts found"}
    records = [store.materialize_remote(item) for item in remote_files]
    global_rebuild = rebuild_global_indexes_from_projects(root)
    runtime = rebuild_runtime_from_repository(root)
    from .retrieval import write_memory_packages
    memory = write_memory_packages(root)
    if project_id:
        from .validation_engine import validate_project
        validation = validate_project(root, project_id)
    else:
        from .validation_engine import validate_repository
        validation = validate_repository(root)
    return {
        "status": validation.status,
        "project_id": project_id,
        "restored_artifact_count": len(records),
        "global_rebuild": global_rebuild,
        "runtime": runtime.to_dict(),
        "memory_outputs": {key: str(path) for key, path in memory.items()},
        "validation": validation.to_dict(),
    }

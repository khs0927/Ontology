"""ArtifactStore abstraction with local and Google Drive-ready adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Protocol


SECURITY_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ArtifactRecord:
    artifact_id: str
    project_id: str
    artifact_type: str
    filename: str
    local_path: str
    sha256: str
    size: int
    created_at: str = field(default_factory=utc_now)
    status: str = "ACTIVE"
    version: int = 1
    source_artifact_id: str | None = None
    storage_provider: str = "LocalArtifactStore"
    storage_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    security_classification: str = "INTERNAL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "project_id": self.project_id,
            "artifact_type": self.artifact_type,
            "filename": self.filename,
            "local_path": self.local_path,
            "sha256": self.sha256,
            "size": self.size,
            "created_at": self.created_at,
            "status": self.status,
            "version": self.version,
            "source_artifact_id": self.source_artifact_id,
            "storage_provider": self.storage_provider,
            "storage_id": self.storage_id,
            "metadata": self.metadata,
            "security_classification": self.security_classification,
        }


class ArtifactStore(Protocol):
    def put(self, source: str | Path, project_id: str, artifact_type: str, **metadata: Any) -> ArtifactRecord: ...
    def get(self, artifact_id: str) -> Path: ...
    def exists(self, artifact_id: str) -> bool: ...
    def list(self, project_id: str | None = None) -> list[ArtifactRecord]: ...
    def find(self, **criteria: Any) -> list[ArtifactRecord]: ...
    def hash(self, source: str | Path) -> str: ...
    def version(self, artifact_id: str) -> int: ...
    def archive(self, artifact_id: str) -> ArtifactRecord: ...
    def get_metadata(self, artifact_id: str) -> ArtifactRecord: ...
    def annotate(self, artifact_id: str, **metadata: Any) -> ArtifactRecord: ...
    def link_source(self, artifact_id: str, source_artifact_id: str) -> ArtifactRecord: ...
    def create_folder(self, relative_path: str) -> Path: ...
    def sync(self, **kwargs: Any) -> dict[str, Any]: ...


class LocalArtifactStore:
    """Filesystem artifact store used as the deterministic local baseline."""

    provider_name = "LocalArtifactStore"

    def __init__(self, repository_root: str | Path):
        self.root = Path(repository_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_root = self.root / "runtime" / "artifact-index"
        self.index_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def hash(self, source: str | Path) -> str:
        return self._sha256(Path(source).resolve())

    def _meta_path(self, artifact_id: str) -> Path:
        return self.index_root / f"{artifact_id}.json"

    def _load(self, artifact_id: str) -> ArtifactRecord:
        path = self._meta_path(artifact_id)
        if not path.exists():
            raise KeyError(f"artifact not found: {artifact_id}")
        return ArtifactRecord(**json.loads(path.read_text(encoding="utf-8")))

    def _save(self, record: ArtifactRecord) -> None:
        self._meta_path(record.artifact_id).write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def put(self, source: str | Path, project_id: str, artifact_type: str, **metadata: Any) -> ArtifactRecord:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        source_hash = self.hash(source_path)
        existing = self.find(project_id=project_id, artifact_type=artifact_type, sha256=source_hash)
        if existing:
            return existing[0]

        project_slug = re.sub(r"[^A-Za-z0-9]+", "-", project_id).strip("-").upper() or "PROJECT"
        type_slug = re.sub(r"[^A-Za-z0-9]+", "-", artifact_type).strip("-").upper() or "ARTIFACT"
        artifact_id = f"AEC-ART-{project_slug}-{type_slug}-{source_hash[:16].upper()}"
        relative_destination = metadata.pop("relative_destination", None)
        source_artifact_id = metadata.pop("source_artifact_id", None)
        security_classification = str(metadata.pop("security_classification", "INTERNAL")).upper()
        if security_classification not in SECURITY_CLASSIFICATIONS:
            raise ValueError(f"unsupported security classification: {security_classification}")
        if relative_destination:
            destination = (self.root / relative_destination).resolve()
            if self.root not in destination.parents:
                raise ValueError("artifact destination must remain inside repository root")
        else:
            type_parts = [part for part in artifact_type.upper().split("/") if part]
            destination = self.root / "projects" / project_id / "01_RAW" / Path(*type_parts) / source_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() != source_path:
            shutil.copy2(source_path, destination)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            project_id=project_id,
            artifact_type=artifact_type,
            filename=source_path.name,
            local_path=str(destination.relative_to(self.root)),
            sha256=source_hash,
            size=destination.stat().st_size,
            source_artifact_id=source_artifact_id,
            storage_provider=self.provider_name,
            storage_id=artifact_id,
            metadata=metadata,
            security_classification=security_classification,
        )
        self._save(record)
        return record

    def get(self, artifact_id: str) -> Path:
        record = self._load(artifact_id)
        path = self.root / record.local_path
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def exists(self, artifact_id: str) -> bool:
        try:
            return self.get(artifact_id).is_file()
        except (KeyError, FileNotFoundError):
            return False

    def list(self, project_id: str | None = None) -> list[ArtifactRecord]:
        records = []
        for metadata_path in sorted(self.index_root.glob("AEC-ART-*.json")):
            record = ArtifactRecord(**json.loads(metadata_path.read_text(encoding="utf-8")))
            if project_id is None or record.project_id == project_id:
                records.append(record)
        return records

    def find(self, **criteria: Any) -> list[ArtifactRecord]:
        return [
            record
            for record in self.list(criteria.pop("project_id", None))
            if all(getattr(record, key, record.metadata.get(key)) == value for key, value in criteria.items())
        ]

    def version(self, artifact_id: str) -> int:
        return self._load(artifact_id).version

    def archive(self, artifact_id: str) -> ArtifactRecord:
        record = self._load(artifact_id)
        record.status = "ARCHIVED"
        self._save(record)
        return record

    def get_metadata(self, artifact_id: str) -> ArtifactRecord:
        return self._load(artifact_id)

    def annotate(self, artifact_id: str, **metadata: Any) -> ArtifactRecord:
        """Add derived metadata without changing artifact bytes or identity."""
        record = self._load(artifact_id)
        record.metadata.update(metadata)
        self._save(record)
        return record

    def link_source(self, artifact_id: str, source_artifact_id: str) -> ArtifactRecord:
        """Record the immutable upstream artifact without changing file bytes."""
        record = self._load(artifact_id)
        self._load(source_artifact_id)
        record.source_artifact_id = source_artifact_id
        self._save(record)
        return record

    def create_folder(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError("folder must remain inside repository root")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def sync(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "LOCAL_ONLY", "provider": self.provider_name, **kwargs}


class GoogleDriveClient(Protocol):
    """Minimal client contract; an MCP or Google API implementation can satisfy it."""

    def upload(self, path: Path, parent_id: str | None, name: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def download(self, file_id: str, destination: Path) -> None: ...


class GoogleDriveArtifactStore(LocalArtifactStore):
    """Drive-backed adapter with local cache and explicit client injection.

    The adapter never assumes credentials or a particular MCP. Without a client,
    all local operations still work and sync returns a clear blocked status.
    """

    provider_name = "GoogleDriveArtifactStore"

    def __init__(self, repository_root: str | Path, client: GoogleDriveClient | None = None, root_folder_id: str | None = None):
        super().__init__(repository_root)
        self.client = client
        self.root_folder_id = root_folder_id

    @staticmethod
    def _relative_path(remote_file: dict[str, Any]) -> str:
        value = remote_file.get("relative_path") or remote_file.get("local_path") or remote_file.get("path")
        if not value:
            raise ValueError("remote Drive file is missing relative_path/local_path/path")
        return str(value).replace("\\", "/").lstrip("/")

    @staticmethod
    def _artifact_type_from_relative(relative_path: str) -> str:
        parts = Path(relative_path).parts
        if len(parts) >= 4 and parts[0] == "projects":
            if parts[2] == "01_RAW":
                return "/".join(parts[3:-1]) or "REFERENCES"
            if parts[2] == "02_DERIVED":
                return "/".join(["DERIVED", *parts[3:-1]]) or "DERIVED"
            return "/".join(parts[2:-1]) or "PROJECT"
        if parts and parts[0] == "global":
            return "/".join(["GLOBAL", *parts[1:-1]]) or "GLOBAL"
        return "/".join(parts[:-1]) or "ARTIFACT"

    def list_remote(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """List remote files through an injected client-owned index contract.

        The client may expose ``list_files(parent_id=..., project_id=...)`` or
        a compatible positional ``list_files`` method. The connector remains
        replaceable; the local store never imports a Google SDK.
        """
        if self.client is None or not self.root_folder_id:
            raise RuntimeError("GoogleDriveClient and root_folder_id are required for remote listing")
        list_files = getattr(self.client, "list_files", None)
        if list_files is None:
            raise RuntimeError("injected GoogleDriveClient must provide list_files for restore/search")
        try:
            result = list_files(parent_id=self.root_folder_id, project_id=project_id)
        except TypeError:
            result = list_files(self.root_folder_id, project_id)
        if isinstance(result, dict):
            result = result.get("files") or result.get("results") or []
        return [dict(item) for item in result]

    def materialize_remote(self, remote_file: dict[str, Any]) -> ArtifactRecord:
        """Download one remote file into its canonical relative path.

        Remote metadata is copied into the local artifact record so a restored
        cache can be synchronized again without losing Drive identity.
        """
        if self.client is None:
            raise RuntimeError("GoogleDriveClient is required for remote materialization")
        file_id = remote_file.get("id") or remote_file.get("file_id")
        if not file_id:
            raise ValueError("remote Drive file is missing id")
        relative_path = self._relative_path(remote_file)
        destination = (self.root / relative_path).resolve()
        if self.root not in destination.parents:
            raise ValueError("remote artifact destination must remain inside repository root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download(str(file_id), destination)
        parts = Path(relative_path).parts
        project_id = str(remote_file.get("project_id") or (parts[1] if len(parts) > 1 and parts[0] == "projects" else "GLOBAL"))
        artifact_type = str(remote_file.get("artifact_type") or self._artifact_type_from_relative(relative_path))
        record = self.put(
            destination,
            project_id,
            artifact_type,
            relative_destination=relative_path,
            source_format=remote_file.get("source_format") or remote_file.get("format"),
            restored_from_drive=True,
        )
        record.storage_id = str(file_id)
        record.metadata.update({
            "google_drive_file_id": str(file_id),
            "google_drive_folder_id": remote_file.get("parent_id") or remote_file.get("folder_id") or self.root_folder_id,
            "google_drive_sha256": remote_file.get("sha256") or remote_file.get("google_drive_sha256") or record.sha256,
            "google_drive_mime_type": remote_file.get("mime_type") or remote_file.get("google_drive_mime_type") or mimetypes.guess_type(record.filename)[0] or "application/octet-stream",
            "google_drive_size": int(remote_file.get("size") or remote_file.get("google_drive_size") or record.size),
        })
        for remote_key, metadata_key in (("modified_time", "google_drive_modified_time"), ("source_visibility_status", "google_drive_source_visibility_status")):
            if remote_file.get(remote_key):
                record.metadata[metadata_key] = remote_file[remote_key]
        self._save(record)
        return record

    def sync(self, **kwargs: Any) -> dict[str, Any]:
        if self.client is None or not self.root_folder_id:
            return {
                "status": "REQUIRES_CONFIGURATION",
                "provider": self.provider_name,
                "reason": "GoogleDriveClient and root_folder_id are not configured",
            }
        uploaded = []
        for record in self.list(kwargs.get("project_id")):
            if record.metadata.get("google_drive_file_id"):
                continue
            parent_id = kwargs.get("parent_id", self.root_folder_id)
            result = self.client.upload(self.get(record.artifact_id), parent_id, record.filename, record.to_dict())
            record.storage_id = result.get("id")
            record.metadata.update(
                {
                    "google_drive_file_id": record.storage_id,
                    "google_drive_folder_id": result.get("parent_id") or result.get("folder_id") or parent_id,
                    "google_drive_sha256": record.sha256,
                    "google_drive_mime_type": result.get("mime_type") or mimetypes.guess_type(record.filename)[0] or "application/octet-stream",
                    "google_drive_size": int(result.get("size") or record.size),
                    "google_drive_synced_at": utc_now(),
                }
            )
            modified_time = result.get("modified_time") or result.get("modifiedTime")
            if modified_time:
                record.metadata["google_drive_modified_time"] = modified_time
            visibility = result.get("source_visibility_status")
            if visibility:
                record.metadata["google_drive_source_visibility_status"] = visibility
            self._save(record)
            uploaded.append(record.artifact_id)
        return {"status": "SYNCED", "provider": self.provider_name, "uploaded": uploaded}

from pathlib import Path

from aec_intelligence.storage import GoogleDriveArtifactStore, LocalArtifactStore


def test_local_artifact_store_is_idempotent_and_archivable(tmp_path: Path):
    source = tmp_path / "source.dxf"
    source.write_text("source", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "repo")
    first = store.put(source, "P1", "CAD/DXF")
    second = store.put(source, "P1", "CAD/DXF")
    assert first.artifact_id == second.artifact_id
    assert store.exists(first.artifact_id)
    assert store.get(first.artifact_id).read_text(encoding="utf-8") == "source"
    assert first.security_classification == "INTERNAL"
    assert store.archive(first.artifact_id).status == "ARCHIVED"


def test_local_artifact_store_links_upstream_without_changing_bytes(tmp_path: Path):
    source = tmp_path / "source.dwg"
    converted = tmp_path / "converted.dxf"
    source.write_text("dwg", encoding="utf-8")
    converted.write_text("dxf", encoding="utf-8")
    store = LocalArtifactStore(tmp_path / "repo")
    upstream = store.put(source, "P1", "CAD/DWG")
    derived = store.put(converted, "P1", "CAD/DXF")
    linked = store.link_source(derived.artifact_id, upstream.artifact_id)
    assert linked.source_artifact_id == upstream.artifact_id
    assert store.get(derived.artifact_id).read_text(encoding="utf-8") == "dxf"


def test_google_drive_store_is_explicit_when_not_configured(tmp_path: Path):
    result = GoogleDriveArtifactStore(tmp_path / "repo").sync()
    assert result["status"] == "REQUIRES_CONFIGURATION"


def test_google_drive_store_uses_injected_client_idempotently(tmp_path: Path):
    class FakeDriveClient:
        def __init__(self):
            self.uploads = []

        def upload(self, path: Path, parent_id: str | None, name: str, metadata: dict):
            self.uploads.append((path, parent_id, name, metadata))
            return {"id": "drive-file-1", "parent_id": parent_id, "mime_type": "application/dxf", "size": path.stat().st_size, "modified_time": "2026-09-02T02:00:00Z", "source_visibility_status": "not_shared"}

        def download(self, file_id: str, destination: Path) -> None:
            raise NotImplementedError

    source = tmp_path / "source.dxf"
    source.write_text("source", encoding="utf-8")
    client = FakeDriveClient()
    store = GoogleDriveArtifactStore(tmp_path / "repo", client=client, root_folder_id="drive-root")
    record = store.put(source, "P1", "CAD/DXF")
    first = store.sync(project_id="P1")
    second = store.sync(project_id="P1")
    assert first == {"status": "SYNCED", "provider": "GoogleDriveArtifactStore", "uploaded": [record.artifact_id]}
    assert second == {"status": "SYNCED", "provider": "GoogleDriveArtifactStore", "uploaded": []}
    assert len(client.uploads) == 1
    metadata = store.get_metadata(record.artifact_id).metadata
    assert metadata["google_drive_file_id"] == "drive-file-1"
    assert metadata["google_drive_folder_id"] == "drive-root"
    assert metadata["google_drive_sha256"] == record.sha256
    assert metadata["google_drive_mime_type"] == "application/dxf"
    assert metadata["google_drive_size"] == source.stat().st_size
    assert metadata["google_drive_modified_time"] == "2026-09-02T02:00:00Z"
    assert metadata["google_drive_source_visibility_status"] == "not_shared"
    assert metadata["google_drive_synced_at"].endswith("Z")

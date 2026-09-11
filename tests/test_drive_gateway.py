from pathlib import Path

from aec_intelligence.mcp_gateway import MCPGateway


FIXTURE = Path(__file__).parents[1] / "fixtures" / "simple_house.dxf"


class FakeDriveClient:
    """Deterministic injected client covering the Drive adapter contract."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.counter = 0

    def upload(self, path: Path, parent_id: str | None, name: str, metadata: dict):
        self.counter += 1
        file_id = f"drive-file-{self.counter}"
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "parent_id": parent_id,
            "relative_path": metadata["local_path"],
            "project_id": metadata["project_id"],
            "artifact_type": metadata["artifact_type"],
            "source_format": (metadata.get("metadata") or {}).get("format"),
            "sha256": metadata["sha256"],
            "size": path.stat().st_size,
            "mime_type": "application/octet-stream",
            "source_visibility_status": "not_shared",
            "content": path.read_bytes(),
        }
        return {key: value for key, value in self.files[file_id].items() if key != "content"}

    def list_files(self, parent_id: str | None = None, project_id: str | None = None):
        values = []
        for item in self.files.values():
            if project_id is not None and item["project_id"] != project_id:
                continue
            values.append({key: value for key, value in item.items() if key != "content"})
        return values

    def download(self, file_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[file_id]["content"])


def test_injected_drive_sync_and_global_cold_restore(tmp_path: Path):
    source_root = tmp_path / "source-repository"
    source_gateway = MCPGateway(source_root)
    ingested = source_gateway.call_tool(
        "aec.ingest_file",
        {"source": str(FIXTURE), "project_id": "P-DRIVE-COLD", "name": "Drive Cold Restore"},
    )
    assert ingested["status"] == "SUCCESS"
    assert source_gateway.call_tool("aec.build_memory_packages", {})["status"] == "SUCCESS"
    assert source_gateway.call_tool("aec.validate_repository", {})["status"] == "SUCCESS"

    client = FakeDriveClient()
    connected = MCPGateway(source_root, drive_client=client, drive_root_id="drive-root")
    project_sync = connected.call_tool("aec.sync_project_to_drive", {"project_id": "P-DRIVE-COLD"})
    assert project_sync["status"] == "SYNCED"
    global_sync = connected.call_tool("aec.sync_global_memory", {})
    assert global_sync["status"] == "SYNCED"
    assert client.files
    assert all(item["source_visibility_status"] == "not_shared" for item in client.files.values())

    restore_root = tmp_path / "restored-repository"
    restored = MCPGateway(restore_root, drive_client=client, drive_root_id="drive-root")
    restored_result = restored.call_tool("aec.rebuild_global_memory_from_drive", {})
    assert restored_result["status"] in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}
    assert restored_result["restored_artifact_count"] == len(client.files)
    assert restored.call_tool("aec.query_global_memory", {"question": "Wall", "top_k": 1})["hits"]
    assert (restore_root / "projects" / "P-DRIVE-COLD" / "03_CAIR" / "project-cair.json").is_file()

    context = restored.call_tool("aec.download_project_context", {"project_id": "P-DRIVE-COLD"})
    assert context["status"] == "SUCCESS"
    assert context["artifacts"]


def test_injected_drive_source_upload_is_idempotent(tmp_path: Path):
    client = FakeDriveClient()
    gateway = MCPGateway(tmp_path / "repo", drive_client=client, drive_root_id="drive-root")
    first = gateway.call_tool("aec.upload_project_source", {"project_id": "P-UPLOAD", "source": str(FIXTURE)})
    second = gateway.call_tool("aec.upload_project_source", {"project_id": "P-UPLOAD", "source": str(FIXTURE)})
    assert first["status"] == "SYNCED"
    assert first["sync"]["uploaded"]
    assert second["status"] == "SYNCED"
    assert second["sync"]["uploaded"] == []
    assert len(client.files) == 1


def test_configured_drive_ingest_uploads_source_before_derived_outputs(tmp_path: Path):
    client = FakeDriveClient()
    gateway = MCPGateway(tmp_path / "repo", drive_client=client, drive_root_id="drive-root")
    result = gateway.call_tool("aec.ingest_file", {"project_id": "P-DRIVE-FIRST", "source": str(FIXTURE)})
    assert result["status"] == "SUCCESS"
    assert result["drive_sync"]["status"] == "SYNCED"
    relative_paths = {item["relative_path"].replace("\\", "/") for item in client.files.values()}
    assert "projects/P-DRIVE-FIRST/01_RAW/CAD/DXF/simple_house.dxf" in relative_paths
    assert "projects/P-DRIVE-FIRST/03_CAIR/project-cair.json" in relative_paths
    assert "projects/P-DRIVE-FIRST/05_GRAPH/project.graphml" in relative_paths

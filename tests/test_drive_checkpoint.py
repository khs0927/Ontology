import json
from pathlib import Path

import pytest

from aec_intelligence.operational.drive_checkpoint import digest, encoded, fetch, publish, recovery_verified, remote_root


class MemoryDrive:
    def __init__(self):
        self.files = {}
        self.corrupt = None
        self.fail = None

    def put(self, local, remote):
        if self.fail == local.name:
            raise RuntimeError("upload interrupted")
        if remote in self.files and self.files[remote] != local.read_bytes():
            raise RuntimeError("immutable collision")
        self.files[remote] = local.read_bytes()

    def get(self, remote, local):
        data = self.files[remote]
        if self.corrupt == Path(remote).name:
            data += b"corrupt"
        local.write_bytes(data)


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "input.dump"
    path.write_bytes(b"PGDMP" + b"fixture data" * 100)
    return path


def report(archive):
    return {"dump_sha256": digest(archive), "result": "MATCH", "restore_exit": 0, "restore_error_lines": 0,
            "tables_live": 2, "tables_restored": 2, "rows_live": 4, "rows_restored": 4,
            "indexes_live": 1, "indexes_restored": 1,
            **{field: [] for field in ("differing_tables", "missing_indexes", "unexpected_indexes",
                                      "invalid_indexes", "structure_differences")}}


def test_round_trip_remote_bytes_and_metadata_are_verified(tmp_path, archive):
    drive = MemoryDrive()
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(encoded({"cursor": "source-10", "pending": 21, "writer_pc": "old-pc"}))
    result = publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive, checkpoint)
    assert result["cloud_readback_verified"] is True
    assert result["manifest"]["status"] == "BACKUP_VERIFIED"
    destination = tmp_path / "new-pc"
    restored = fetch(result["remote"], destination, drive)
    assert digest(destination / "database.dump") == digest(archive)
    assert restored["writer_handoff_verified"] is False
    assert json.loads((destination / "checkpoint.json").read_text())["pending"] == 21


def test_exact_dump_restore_proof_upgrades_archive_status(tmp_path, archive):
    proof = tmp_path / "report.json"
    proof.write_bytes(encoded(report(archive)))
    drive = MemoryDrive()
    result = publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive, restore_report=proof)
    assert result["manifest"]["status"] == "RECOVERY_VERIFIED"
    assert fetch(result["remote"], tmp_path / "restored", drive)["manifest"]["status"] == "RECOVERY_VERIFIED"


@pytest.mark.parametrize("field,value", [
    ("dump_sha256", "0" * 64), ("result", "DIFF"), ("restore_exit", 1), ("restore_error_lines", 1),
    ("tables_live", 0), ("rows_restored", 3), ("invalid_indexes", ["idx"]), ("missing_indexes", ["idx"]),
    ("unexpected_indexes", ["idx"]), ("structure_differences", ["constraint"]), ("differing_tables", ["table"]),
])
def test_recovery_gate_rejects_incomplete_evidence(archive, field, value):
    proof = report(archive)
    proof[field] = value
    assert recovery_verified(proof, digest(archive)) is False


@pytest.mark.parametrize("name", ["database.dump", "manifest.json"])
def test_corrupt_cloud_readback_never_publishes_ready(tmp_path, archive, name):
    drive = MemoryDrive()
    drive.corrupt = name
    with pytest.raises(ValueError, match="checksum"):
        publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive)
    assert not any(key.endswith("/READY.json") for key in drive.files)


def test_interrupted_upload_cannot_be_fetched_as_complete(tmp_path, archive):
    drive = MemoryDrive()
    drive.fail = "manifest.json"
    with pytest.raises(RuntimeError):
        publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive)
    assert not any(key.endswith("/READY.json") for key in drive.files)


def test_corrupt_download_leaves_no_restore_directory(tmp_path, archive):
    drive = MemoryDrive()
    result = publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive)
    drive.corrupt = "database.dump"
    destination = tmp_path / "new-pc"
    with pytest.raises(ValueError, match="checksum"):
        fetch(result["remote"], destination, drive)
    assert not destination.exists()


@pytest.mark.parametrize("member", ["../outside", "/outside", "C:/outside", "..\\outside", "READY.json",
                                    "nested/database.dump"])
def test_manifest_paths_cannot_escape_or_replace_control_files(tmp_path, archive, member):
    drive = MemoryDrive()
    result = publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive)
    manifest = result["manifest"]
    manifest["files"][0]["name"] = member
    manifest_bytes = encoded(manifest)
    drive.files[result["remote"] + "/manifest.json"] = manifest_bytes
    import hashlib
    drive.files[result["remote"] + "/READY.json"] = encoded({
        "schema": manifest["schema"], "package_id": manifest["package_id"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()})
    with pytest.raises(ValueError):
        fetch(result["remote"], tmp_path / "new-pc", drive)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("remote", ["G:\\archive", "drive:", "drive:../archive", "drive:archive/../escape",
                                    "drive:/", "drive:archive//snapshots", "drive:archive\nother"])
def test_mounts_and_unscoped_remotes_are_rejected(remote):
    with pytest.raises(ValueError):
        remote_root(remote)


@pytest.mark.parametrize("metadata", [{"token": "secret"}, {"nested": [{"password": "secret"}]},
                                      {"text": "postgresql://aec:password@localhost/aec"}])
def test_credential_metadata_is_not_uploaded(tmp_path, archive, metadata):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(encoded(metadata))
    drive = MemoryDrive()
    with pytest.raises(ValueError, match="credential"):
        publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive, checkpoint)
    assert drive.files == {}


def test_existing_destination_is_never_replaced(tmp_path, archive):
    drive = MemoryDrive()
    result = publish(archive, tmp_path / "staging", "drive:archive/checkpoints", drive)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "original").write_text("preserve")
    with pytest.raises(FileExistsError):
        fetch(result["remote"], destination, drive)
    assert (destination / "original").read_text() == "preserve"


def test_backup_header_check_does_not_load_archive_into_memory(tmp_path, monkeypatch):
    from aec_intelligence.operational.backup import run_backup

    class Result:
        returncode = 0
        stderr = b""

    def fake_dump(command, stdout, stderr):
        stdout.write(b"PGDMPfixture")
        return Result()

    monkeypatch.setattr("aec_intelligence.operational.backup.subprocess.run", fake_dump)
    monkeypatch.setattr(Path, "read_bytes", lambda path: pytest.fail("whole dump loaded into memory"))
    assert run_backup("postgresql://localhost/aec", tmp_path, docker_container="fake")["bytes"] == 12

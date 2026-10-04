"""Immutable, read-back verified Drive archives; never operates on a live database.

Use a configured rclone Drive remote, not a Drive for Desktop mount, so a successful
publication means remote bytes were read back. Writer handoff is a separate gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from typing import Protocol
from uuid import uuid4


SCHEMA = "aec-drive-checkpoint/1"
MAX_JSON = 16 * 1024 * 1024
FORBIDDEN_KEYS = {"password", "passwd", "token", "api_key", "apikey", "secret", "authorization", "dsn"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def read_json(path: Path) -> dict:
    if path.stat().st_size > MAX_JSON:
        raise ValueError("JSON exceeds checkpoint metadata size limit")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("checkpoint metadata must be an object")
    return value


def no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ValueError("checkpoint metadata contains a credential field")
            no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            no_credentials(item)
    elif isinstance(value, str) and re.search(r"(?i)(?:postgres(?:ql)?://[^\s/@]+:[^\s/@]+@|bearer\s+\S+)", value):
        raise ValueError("checkpoint metadata contains credentials")


def safe_member(name: str) -> str:
    if not isinstance(name, str) or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("unsafe checkpoint member")
    parts = PurePosixPath(name)
    if parts.is_absolute() or any(part in {".", ".."} for part in name.split("/")):
        raise ValueError("unsafe checkpoint member")
    if len(parts.parts) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", name):
        raise ValueError("unsafe checkpoint member")
    return name


class Transport(Protocol):
    def put(self, local: Path, remote: str): ...
    def get(self, remote: str, local: Path): ...


class RcloneTransport:
    def __init__(self, executable: str = "rclone", timeout: int = 3600):
        self.executable = executable
        self.timeout = timeout

    def _copy(self, source: str, target: str, immutable: bool):
        command = [self.executable, "copyto", source, target, "--retries", "3", "--low-level-retries", "3"]
        if immutable:
            command += ["--immutable"]
        result = subprocess.run(command, capture_output=True, timeout=self.timeout)
        if result.returncode:
            # No config, credentials or arbitrary stderr is echoed into durable reports.
            raise RuntimeError(f"rclone copy failed (exit {result.returncode})")

    def put(self, local: Path, remote: str):
        self._copy(str(local), remote, True)

    def get(self, remote: str, local: Path):
        self._copy(remote, str(local), False)


def remote_root(remote: str) -> str:
    # Deliberately reject a local/mounted path and bare remote roots.
    if not re.fullmatch(r"[A-Za-z0-9_-]+:[^\\\x00\r\n]+", remote):
        raise ValueError("a configured rclone remote with an explicit archive subtree is required")
    remote_name, path = remote.split(":", 1)
    if len(remote_name) == 1 or path.startswith("/") or any(p in {"", ".", ".."} for p in path.split("/")):
        raise ValueError("unsafe or unscoped remote subtree")
    return remote


def recovery_verified(report: dict, dump_sha: str) -> bool:
    return (
        report.get("dump_sha256") == dump_sha
        and report.get("result") == "MATCH"
        and type(report.get("restore_exit")) is int and report["restore_exit"] == 0
        and type(report.get("restore_error_lines")) is int and report["restore_error_lines"] == 0
        and type(report.get("tables_live")) is int and report["tables_live"] > 0
        and type(report.get("tables_restored")) is int
        and report.get("tables_restored") == report["tables_live"]
        and type(report.get("rows_live")) in (int, float) and report["rows_live"] >= 0
        and type(report.get("rows_restored")) in (int, float)
        and report.get("rows_live") == report.get("rows_restored")
        and type(report.get("indexes_live")) is int and report["indexes_live"] >= 0
        and type(report.get("indexes_restored")) is int
        and report["indexes_live"] == report["indexes_restored"]
        and all(report.get(field) == [] for field in (
            "differing_tables", "missing_indexes", "unexpected_indexes", "invalid_indexes", "structure_differences"
        ))
    )


def publish(dump: Path, staging_root: Path, remote: str, transport: Transport,
            checkpoint: Path | None = None, restore_report: Path | None = None,
            source_commit: str | None = None) -> dict:
    remote = remote_root(remote)
    dump = dump.resolve(strict=True)
    if not dump.is_file() or dump.suffix != ".dump":
        raise ValueError("input must be a completed .dump archive")
    with dump.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise ValueError("input is not a PostgreSQL custom archive")
    if source_commit is not None and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full Git SHA")
    staging_root.mkdir(parents=True, exist_ok=True)
    package_id = uuid4().hex
    package = staging_root.resolve() / package_id
    package.mkdir()  # Never overwrite a previous local package.
    before = (dump.stat().st_size, dump.stat().st_mtime_ns, digest(dump))
    shutil.copyfile(dump, package / "database.dump")
    after = (dump.stat().st_size, dump.stat().st_mtime_ns, digest(dump))
    if before != after or digest(package / "database.dump") != before[2]:
        raise ValueError("archive changed during publication")
    status = "BACKUP_VERIFIED"
    for path, name in ((checkpoint, "checkpoint.json"), (restore_report, "restore-report.json")):
        if path:
            metadata = read_json(path)
            no_credentials(metadata)
            (package / name).write_bytes(encoded(metadata))
            if name == "restore-report.json":
                if not recovery_verified(metadata, before[2]):
                    raise ValueError("restore report must prove this exact dump fully restored")
                status = "RECOVERY_VERIFIED"
    files = [{"name": path.name, "sha256": digest(path), "bytes": path.stat().st_size}
             for path in sorted(package.iterdir())]
    manifest = {"schema": SCHEMA, "package_id": package_id, "status": status,
                "source_commit": source_commit, "files": files, "writer_handoff_verified": False}
    manifest_path = package / "manifest.json"
    manifest_path.write_bytes(encoded(manifest))
    target = f"{remote}/{package_id}"
    # Data and manifest first; completion marker only after remote read-back of every byte.
    with tempfile.TemporaryDirectory(dir=staging_root, prefix="readback-") as folder:
        for path in [*(package / item["name"] for item in files), manifest_path]:
            transport.put(path, f"{target}/{path.name}")
            fetched = Path(folder) / path.name
            transport.get(f"{target}/{path.name}", fetched)
            if fetched.stat().st_size != path.stat().st_size or digest(fetched) != digest(path):
                raise ValueError("remote read-back checksum mismatch")
    ready = package / "READY.json"
    ready.write_bytes(encoded({"schema": SCHEMA, "package_id": package_id,
                               "manifest_sha256": digest(manifest_path)}))
    transport.put(ready, f"{target}/READY.json")
    with tempfile.TemporaryDirectory(dir=staging_root, prefix="ready-") as folder:
        fetched = Path(folder) / "READY.json"
        transport.get(f"{target}/READY.json", fetched)
        if digest(fetched) != digest(ready):
            raise ValueError("completion marker read-back mismatch")
    return {"remote": target, "manifest": manifest, "local_package": str(package), "cloud_readback_verified": True}


def fetch(remote: str, destination: Path, transport: Transport) -> dict:
    remote = remote_root(remote)
    package_id = remote.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{32}", package_id):
        raise ValueError("fetch requires an exact published package ID")
    if destination.exists():
        raise FileExistsError("refusing to replace an existing restore directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="checkpoint-fetch-") as folder:
        package = Path(folder)
        for name in ("READY.json", "manifest.json"):
            transport.get(f"{remote}/{name}", package / name)
        ready, manifest = read_json(package / "READY.json"), read_json(package / "manifest.json")
        if (ready.get("schema") != SCHEMA or manifest.get("schema") != SCHEMA
                or ready.get("package_id") != package_id or manifest.get("package_id") != package_id
                or ready.get("manifest_sha256") != digest(package / "manifest.json")):
            raise ValueError("invalid or incomplete checkpoint manifest")
        files = manifest.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 3:
            raise ValueError("invalid checkpoint files")
        names = set()
        for item in files:
            name = safe_member(item["name"])
            if name not in {"database.dump", "checkpoint.json", "restore-report.json"} or name in names:
                raise ValueError("unexpected or duplicate checkpoint member")
            names.add(name)
            if (not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
                    or type(item.get("bytes")) is not int or item["bytes"] < 1):
                raise ValueError("invalid checkpoint digest or size")
            path = package / name
            transport.get(f"{remote}/{name}", path)
            if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                raise ValueError("download checksum mismatch")
            if name.endswith(".json"):
                no_credentials(read_json(path))
        if "database.dump" not in names:
            raise ValueError("database archive missing")
        with (package / "database.dump").open("rb") as source:
            if source.read(5) != b"PGDMP":
                raise ValueError("invalid database archive")
        if manifest.get("status") not in {"BACKUP_VERIFIED", "RECOVERY_VERIFIED"}:
            raise ValueError("unknown checkpoint status")
        if manifest.get("writer_handoff_verified") is not False:
            raise ValueError("archive cannot authorize writer handoff")
        if manifest["status"] == "RECOVERY_VERIFIED" and (
            "restore-report.json" not in names or not recovery_verified(
                read_json(package / "restore-report.json"), digest(package / "database.dump")
            )
        ):
            raise ValueError("invalid recovery evidence")
        # Rename on the same volume only after complete validation. Never touches a database.
        os.rename(package, destination)
    return {"directory": str(destination), "manifest": manifest, "download_verified": True,
            "writer_handoff_verified": False}


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rclone", default="rclone")
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("publish")
    upload.add_argument("--dump", type=Path, required=True)
    upload.add_argument("--staging", type=Path, required=True)
    upload.add_argument("--remote", required=True)
    upload.add_argument("--checkpoint", type=Path)
    upload.add_argument("--restore-report", type=Path)
    upload.add_argument("--source-commit")
    download = commands.add_parser("fetch")
    download.add_argument("--remote", required=True)
    download.add_argument("--destination", type=Path, required=True)
    parsed = parser.parse_args(args)
    transport = RcloneTransport(parsed.rclone)
    if parsed.command == "publish":
        result = publish(parsed.dump, parsed.staging, parsed.remote, transport, parsed.checkpoint,
                         parsed.restore_report, parsed.source_commit)
    else:
        result = fetch(parsed.remote, parsed.destination, transport)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

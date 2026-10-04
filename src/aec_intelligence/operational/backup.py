"""pg_dump / pg_restore of the aec database with bounded, prefix-scoped rotation.

Dumps are written to a local (or Google Drive for Desktop) folder as custom-format archives:
``<prefix>YYYYMMDDTHHMMSSZ.dump``. Rotation only ever deletes files in that one folder whose
name matches the prefix + timestamp pattern, so unrelated files are never touched.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PREFIX = "aec-db-"
SUFFIX = ".dump"


def _pattern(prefix: str) -> re.Pattern:
    return re.compile(re.escape(prefix) + r"\d{8}T\d{6}Z" + re.escape(SUFFIX) + r"$")


def backup_name(prefix: str = DEFAULT_PREFIX, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}{stamp}{SUFFIX}"


def list_backups(target: str | Path, prefix: str = DEFAULT_PREFIX) -> list[Path]:
    folder = Path(target)
    if not folder.is_dir():
        return []
    pattern = _pattern(prefix)
    return sorted(p for p in folder.iterdir() if p.is_file() and pattern.fullmatch(p.name))


def rotate(target: str | Path, keep: int = 14, prefix: str = DEFAULT_PREFIX) -> list[Path]:
    """Delete all but the newest ``keep`` dumps (by the UTC stamp in the name). Returns deleted paths."""
    if keep < 1:
        raise ValueError("keep must be >= 1")
    folder = Path(target).resolve()
    backups = list_backups(folder, prefix)
    deleted = []
    for path in backups[:-keep]:
        if path.resolve().parent != folder:  # paranoia: never leave the backups folder
            continue
        path.unlink()
        deleted.append(path)
    return deleted


def _dsn_parts(dsn: str) -> dict[str, str]:
    parsed = urlparse(dsn)
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    return {"user": parsed.username or query.get("user", "aec"),
            "dbname": (parsed.path or "/aec").lstrip("/") or "aec"}


def _tool(name: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    found = shutil.which(name)
    if found:
        return found
    for version in ("17", "16"):
        candidate = Path(f"/usr/lib/postgresql/{version}/bin/{name}")
        if candidate.exists():
            return str(candidate)
    return None


def run_backup(dsn: str, target: str | Path, keep: int = 14, prefix: str = DEFAULT_PREFIX,
               docker_container: str | None = None, pg_dump: str | None = None,
               docker: str = "docker", now: datetime | None = None) -> dict:
    folder = Path(target)
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / backup_name(prefix, now)
    partial = final.with_name(final.name + ".partial")
    executable = None if docker_container else _tool("pg_dump", pg_dump)
    if executable:
        command = [executable, "-Fc", "-Z", "6", "--dbname", dsn]
    else:
        if not docker_container:
            raise RuntimeError("pg_dump not found on PATH; pass --docker-container aec-db")
        parts = _dsn_parts(dsn)
        command = [docker, "exec", docker_container, "pg_dump", "-U", parts["user"], "-d", parts["dbname"],
                   "-Fc", "-Z", "6"]
    try:
        with open(partial, "wb") as sink:
            proc = subprocess.run(command, stdout=sink, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-2000:]}")
        with partial.open("rb") as archive:
            if archive.read(5) != b"PGDMP":
                raise RuntimeError("pg_dump produced no custom-format archive")
        os.replace(partial, final)
    finally:
        if partial.exists():
            partial.unlink()
    deleted = rotate(folder, keep, prefix)
    return {"backup": str(final), "bytes": final.stat().st_size, "deleted": [str(p) for p in deleted],
            "kept": [str(p) for p in list_backups(folder, prefix)]}


# Parallel index builds (pgvector HNSW, btree) allocate dynamic shared memory in /dev/shm; Docker's
# default 64 MB is too small at ~1 GB DB size ("could not resize shared memory segment"), and with
# --exit-on-error the whole restore fails. Serial builds are slower but need no shared memory.
RESTORE_PGOPTIONS = "-c max_parallel_maintenance_workers=0"


def restore_command(dsn: str, dump: Path, docker_container: str | None, executable: str | None,
                    docker: str = "docker", clean: bool = True) -> tuple[list[str], dict | None]:
    """The pg_restore argv plus the environment for a local run (None = inherit, docker passes -e)."""
    flags = ["--no-owner", "--exit-on-error"] + (["--clean", "--if-exists"] if clean else [])
    if executable:
        env = dict(os.environ)
        env["PGOPTIONS"] = (env.get("PGOPTIONS", "") + " " + RESTORE_PGOPTIONS).strip()
        return [executable, *flags, "--dbname", dsn, str(dump)], env
    parts = _dsn_parts(dsn)
    return [docker, "exec", "-i", "-e", f"PGOPTIONS={RESTORE_PGOPTIONS}", docker_container or "",
            "pg_restore", "-U", parts["user"], "-d", parts["dbname"], *flags], None


def run_restore(dsn: str, dump: str | Path, yes: bool = False, docker_container: str | None = None,
                pg_restore: str | None = None, docker: str = "docker", clean: bool = True) -> dict:
    if not yes:
        raise PermissionError("restore overwrites database objects; re-run with --yes")
    dump = Path(dump)
    if not dump.is_file():
        raise FileNotFoundError(dump)
    executable = None if docker_container else _tool("pg_restore", pg_restore)
    if not executable and not docker_container:
        raise RuntimeError("pg_restore not found on PATH; pass --docker-container aec-db")
    command, env = restore_command(dsn, dump, docker_container, executable, docker=docker, clean=clean)
    if executable:
        proc = subprocess.run(command, stderr=subprocess.PIPE, env=env)
    else:
        with open(dump, "rb") as source:
            proc = subprocess.run(command, stdin=source, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-2000:]}")
    return {"restored": str(dump), "database": _dsn_parts(dsn)["dbname"]}

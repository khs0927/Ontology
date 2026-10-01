"""Deterministic code-change inventory for derived code intelligence.

Filesystem/Git source is authoritative. The snapshot is a rebuildable runtime
index only; it never writes source, CAIR, project, or global canonical data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


DEFAULT_EXTENSIONS = frozenset({
    ".py", ".cs", ".cpp", ".c", ".h", ".hpp",
    ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rs", ".go",
    ".toml", ".yaml", ".yml",
})
DEFAULT_EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "runtime", "dist", "build",
    "bin", "obj", "__pycache__", ".pytest_cache", ".mypy_cache",
})
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class CodeFileRecord:
    path: str
    sha256: str
    size: int
    provenance: str = "filesystem"
    evidence: str = "EXTRACTED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CodeSnapshot:
    schema: str
    root: str
    fingerprint: str
    files: tuple[CodeFileRecord, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "root": self.root,
            "fingerprint": self.fingerprint,
            "files": [row.to_dict() for row in self.files],
            "warnings": list(self.warnings),
            "canonical": False,
            "rebuildable": True,
        }


def _safe_runtime_target(root: Path, target: str | Path | None) -> Path:
    runtime_root = (root / "runtime" / "code-intelligence").resolve()
    destination = (runtime_root / "snapshot.json") if target is None else Path(target).resolve()
    try:
        destination.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("code-intelligence snapshots must remain under runtime/code-intelligence/") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _iter_source_files(
    root: Path,
    extensions: frozenset[str],
    excluded_dirs: frozenset[str],
) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in excluded_dirs for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() not in extensions:
            continue
        yield path


def scan_code_tree(
    repository_root: str | Path,
    *,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    excluded_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> CodeSnapshot:
    root = Path(repository_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be positive")

    normalized_extensions = frozenset(
        value.lower() if str(value).startswith(".") else f".{str(value).lower()}"
        for value in extensions
        if str(value).strip()
    )
    if not normalized_extensions:
        raise ValueError("at least one source extension is required")
    excluded = frozenset(str(value) for value in excluded_dirs if str(value))

    records: list[CodeFileRecord] = []
    warnings: list[str] = []
    for path in _iter_source_files(root, normalized_extensions, excluded):
        size = path.stat().st_size
        relative = path.relative_to(root).as_posix()
        if size > max_file_bytes:
            warnings.append(f"skipped oversized source file: {relative} ({size} bytes)")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(CodeFileRecord(relative, digest, size))

    fingerprint_material = "\n".join(f"{row.path}:{row.sha256}:{row.size}" for row in records)
    fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
    return CodeSnapshot(
        schema="ontology-code-snapshot/1",
        root=str(root),
        fingerprint=fingerprint,
        files=tuple(records),
        warnings=tuple(warnings),
    )


def snapshot_from_dict(value: dict[str, Any]) -> CodeSnapshot:
    if value.get("schema") != "ontology-code-snapshot/1":
        raise ValueError("unsupported code snapshot schema")
    rows = tuple(
        CodeFileRecord(
            path=str(row["path"]),
            sha256=str(row["sha256"]),
            size=int(row["size"]),
            provenance=str(row.get("provenance", "filesystem")),
            evidence=str(row.get("evidence", "EXTRACTED")),
        )
        for row in value.get("files", [])
    )
    return CodeSnapshot(
        schema="ontology-code-snapshot/1",
        root=str(value.get("root") or ""),
        fingerprint=str(value.get("fingerprint") or ""),
        files=rows,
        warnings=tuple(str(item) for item in value.get("warnings", [])),
    )


def diff_code_snapshots(before: CodeSnapshot | None, after: CodeSnapshot) -> dict[str, Any]:
    previous = {row.path: row for row in before.files} if before else {}
    current = {row.path: row for row in after.files}

    added = sorted(current.keys() - previous.keys())
    deleted = sorted(previous.keys() - current.keys())
    modified = sorted(
        path
        for path in current.keys() & previous.keys()
        if current[path].sha256 != previous[path].sha256
        or current[path].size != previous[path].size
    )
    unchanged = len(current) - len(added) - len(modified)
    changed = bool(added or deleted or modified)
    return {
        "schema": "ontology-code-diff/1",
        "before_fingerprint": before.fingerprint if before else None,
        "after_fingerprint": after.fingerprint,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged,
        "changed": changed,
        "derived_code_graph_stale": changed,
        "source_of_truth": "filesystem",
        "canonical": False,
    }


def load_code_snapshot(
    repository_root: str | Path,
    target: str | Path | None = None,
) -> CodeSnapshot | None:
    root = Path(repository_root).expanduser().resolve()
    path = _safe_runtime_target(root, target)
    if not path.is_file():
        return None
    return snapshot_from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_code_snapshot(
    repository_root: str | Path,
    snapshot: CodeSnapshot,
    target: str | Path | None = None,
) -> Path:
    root = Path(repository_root).expanduser().resolve()
    path = _safe_runtime_target(root, target)
    path.write_text(
        json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def refresh_code_snapshot(repository_root: str | Path) -> dict[str, Any]:
    root = Path(repository_root).expanduser().resolve()
    previous = load_code_snapshot(root)
    current = scan_code_tree(root)
    diff = diff_code_snapshots(previous, current)
    target = save_code_snapshot(root, current)
    return {
        "status": "SUCCESS",
        "snapshot": str(target),
        "file_count": len(current.files),
        "warnings": list(current.warnings),
        "diff": diff,
        "canonical_mutation": False,
        "next_action": "rebuild derived code graph" if diff["changed"] else "none",
    }

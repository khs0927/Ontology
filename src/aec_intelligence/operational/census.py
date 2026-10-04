"""File census, census-driven enqueueing, multi-process workers and reporting.

The census walks large (cloud-backed, Korean-named, Windows) drawing trees once, records a
content hash per drawing, and lets every later step (dedup, enqueue, reporting) work from
``census.jsonl`` instead of re-walking the slow drive.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import multiprocessing as mp
import os
import re
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_EXTENSIONS = (".dwg", ".dxf", ".ifc", ".pdf")
SKIP_PATTERNS = ("*.bak", "*.sv$", "~$*", "*.dwl", "*.dwl2", "*.tmp", "~*.tmp", "*.ac$")
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", ".tmp.drivedownload", ".tmp.driveupload",
             ".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"}
CHUNK = 1024 * 1024

# DWG/DXF "AC10xx" signature -> AutoCAD release (format family).
DWG_VERSIONS = {
    "MC0.0": "R1.0", "AC1.2": "R1.2", "AC1.40": "R1.40", "AC1.50": "R2.05", "AC2.10": "R2.10",
    "AC1001": "R2.22", "AC1002": "R2.5", "AC1003": "R2.6", "AC1004": "R9", "AC1006": "R10",
    "AC1009": "R11/R12", "AC1012": "R13", "AC1014": "R14", "AC1015": "2000-2002",
    "AC1018": "2004-2006", "AC1021": "2007-2009", "AC1024": "2010-2012", "AC1027": "2013-2017",
    "AC1032": "2018+",
}
_DXF_VERSION = re.compile(rb"\$ACADVER\s*\r?\n\s*1\s*\r?\n\s*(AC\d{4})")

# FILE_ATTRIBUTE_OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS: cloud placeholder not on disk.
_PLACEHOLDER_ATTRS = 0x1000 | 0x40000 | 0x400000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fs(path: str) -> str:
    """Path usable for OS calls: long-path prefix on Windows so >260 char paths still open."""
    if os.name == "nt" and len(path) >= 240 and not path.startswith("\\\\?\\"):
        absolute = os.path.abspath(path)
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC\\" + absolute[2:]
        return "\\\\?\\" + absolute
    return path


def is_temp_file(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, pattern) for pattern in SKIP_PATTERNS)


def dwg_release(signature: str | None) -> str | None:
    return DWG_VERSIONS.get(signature or "", None) if signature else None


def sniff_version(ext: str, head: bytes) -> str | None:
    """Return the AC10xx signature from a DWG header or a DXF $ACADVER entry."""
    if ext == ".dwg":
        raw = head[:6]
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        for key in sorted(DWG_VERSIONS, key=len, reverse=True):
            if text.startswith(key):
                return key
        return text if re.fullmatch(r"AC\d{4}", text) else None
    if ext == ".dxf":
        match = _DXF_VERSION.search(head)
        return match.group(1).decode() if match else None
    return None


def hash_file(path: str, ext: str) -> tuple[str, str | None]:
    digest = hashlib.sha256()
    head = b""
    with open(_fs(path), "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            if len(head) < 65536:
                head += chunk[: 65536 - len(head)]
            digest.update(chunk)
    return digest.hexdigest(), sniff_version(ext, head)


def _excluder(patterns: Iterable[str]):
    """Predicate for ``--exclude``: a pattern with a path separator is a path prefix (``G:\\a\\b``),
    anything else a case-insensitive glob on the file/folder name (``hillside_villa*``)."""
    prefixes, globs = [], []
    for pattern in patterns or ():
        pattern = unicodedata.normalize("NFC", str(pattern).strip())
        if not pattern:
            continue
        if "/" in pattern or "\\" in pattern:
            prefixes.append(os.path.normcase(os.path.abspath(pattern)).rstrip("/\\"))
        else:
            globs.append(pattern.lower())

    def excluded(path: str) -> bool:
        name = unicodedata.normalize("NFC", os.path.basename(path)).lower()
        if any(fnmatch.fnmatchcase(name, g) for g in globs):
            return True
        norm = os.path.normcase(unicodedata.normalize("NFC", path))
        return any(norm == p or norm.startswith(p + os.sep) for p in prefixes)
    return excluded


def walk(root: str, errors: list[dict[str, str]], excluded=None) -> Iterator[tuple[str, os.stat_result]]:
    """Iterative os.scandir walk; never raises on unreadable folders, never follows links."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(_fs(current)) as entries:
                children = []
                for entry in entries:
                    path = os.path.join(current, entry.name)
                    if excluded is not None and excluded(path):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRS:
                                children.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            yield path, entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        errors.append({"path": path, "error": f"{type(exc).__name__}: {exc}"})
                stack.extend(sorted(children, reverse=True))
        except OSError as exc:
            errors.append({"path": current, "error": f"{type(exc).__name__}: {exc}"})


def _relative(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        rel = path
    return rel.replace("\\", "/")


def _load_previous(jsonl: Path) -> dict[str, tuple[int, int]]:
    """path -> (size, mtime_ns) for completed rows; truncates a torn last line from a crash."""
    previous: dict[str, tuple[int, int]] = {}
    if not jsonl.exists():
        return previous
    good = 0
    with open(jsonl, "rb") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                break
            good += len(line)
            if row.get("status") in ("ok", "skipped_temp", "inventory"):  # errors/placeholders are retried
                previous[row["path"]] = (row.get("size"), row.get("mtime_ns"))
    if good != jsonl.stat().st_size:
        with open(jsonl, "r+b") as handle:
            handle.truncate(good)
    return previous


def _hash_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["sha256"], row["dwg_version"] = hash_file(row["path"], row["ext"])
        row["dwg_release"] = dwg_release(row["dwg_version"])
    except OSError as exc:
        row["status"], row["error"] = "error", f"{type(exc).__name__}: {exc}"
    return row


@dataclass
class CensusResult:
    out: Path
    summary: dict[str, Any] = field(default_factory=dict)


def run_census(roots: Iterable[str | Path], out: str | Path, extensions: Iterable[str] = DEFAULT_EXTENSIONS,
               resume: bool = False, flush_every: int = 100, hash_placeholders: bool = True,
               progress: Any = None, only_folders: Iterable[str] = (), exclude: Iterable[str] = (),
               inventory_extensions: Iterable[str] = (), hash_workers: int = 1) -> CensusResult:
    """Walk ``roots`` (or only ``root/<folder>`` for each of ``only_folders``) and record every drawing.

    Paths stay relative to the root so the top-level folder (= project) is the same whether a pilot
    folder or the whole drive is scanned. Rows outside the scanned scope are kept on resume.
    """
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    # Inventory-only formats (e.g. .rvt/.skp) are listed with size/mtime but never read: hashing a
    # cloud placeholder would download it for nothing.
    inventory = {e.lower() if e.startswith(".") else "." + e.lower() for e in inventory_extensions}
    exts |= inventory
    jsonl = out_dir / "census.jsonl"
    previous = _load_previous(jsonl) if resume else {}
    if not resume and jsonl.exists():
        jsonl.unlink()
    walk_errors: list[dict[str, str]] = []
    other_ext: Counter = Counter()
    seen: set[str] = set()
    scopes: list[str] = []
    counters = Counter()
    started = time.time()
    excluded = _excluder(exclude) if exclude else None
    # Cloud drives (Google Drive for desktop) stream each file with high per-file latency: measured on
    # the PC, 1 reader got 0.4 MB/s while 6 parallel readers got ~20 MB/s. Hash with a thread pool.
    pool = ThreadPoolExecutor(max_workers=max(1, int(hash_workers)), thread_name_prefix="census-hash")
    inflight: set = set()
    with open(jsonl, "a", encoding="utf-8", newline="\n") as sink:
        pending = 0

        def emit(row):
            nonlocal pending
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            counters["recorded"] += 1
            pending += 1
            if pending >= flush_every:
                sink.flush()
                pending = 0
                if progress:
                    progress(counters["walked"], row["path"])

        def drain(limit):
            nonlocal inflight
            while len(inflight) > limit:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for future in done:
                    emit(future.result())

        try:
            for root, scope in _scopes(roots, only_folders):
                root_str = root
                scopes.append(scope)
                for path, st in walk(scope, walk_errors, excluded):
                    name = os.path.basename(path)
                    ext = os.path.splitext(name)[1].lower()
                    temp = is_temp_file(name)
                    if ext not in exts and not temp:
                        other_ext[ext or "(none)"] += 1
                        continue
                    if path in seen:  # overlapping roots
                        continue
                    seen.add(path)
                    counters["walked"] += 1
                    if previous.get(path) == (st.st_size, st.st_mtime_ns):
                        counters["resumed"] += 1
                        continue
                    rel = _relative(path, root_str)
                    parts = rel.split("/")
                    attrs = getattr(st, "st_file_attributes", 0)
                    row: dict[str, Any] = {
                        "path": path, "rel_path": rel, "root": root_str,
                        "top_folder": parts[0] if len(parts) > 1 else "",
                        "name": name, "ext": ext, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                        "placeholder": bool(attrs & _PLACEHOLDER_ATTRS),
                        "sha256": None, "dwg_version": None, "dwg_release": None, "status": "ok", "error": None,
                    }
                    if temp:
                        row["status"] = "skipped_temp"
                    elif ext in inventory:
                        row["status"] = "inventory"
                    elif row["placeholder"] and not hash_placeholders:
                        row["status"] = "placeholder"
                    else:
                        inflight.add(pool.submit(_hash_row, row))
                        drain(4 * max(1, int(hash_workers)))
                        continue
                    emit(row)
            drain(0)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    summary = finalize(out_dir, seen, other_ext, walk_errors, scopes, exts)
    summary["run"] = {"walked": counters["walked"], "recorded_this_run": counters["recorded"],
                      "resumed_unchanged": counters["resumed"], "seconds": round(time.time() - started, 1)}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(summary_markdown(summary), encoding="utf-8")
    return CensusResult(out_dir, summary)


def _scopes(roots, only_folders) -> list[tuple[str, str]]:
    pairs = []
    for root in roots:
        root_str = os.path.abspath(str(root))
        if only_folders:
            pairs.extend((root_str, os.path.join(root_str, folder)) for folder in only_folders)
        else:
            pairs.append((root_str, root_str))
    return pairs


def _in_scopes(path: str, scopes: Iterable[str]) -> bool:
    return any(path == scope or path.startswith(scope.rstrip("/\\") + os.sep) for scope in scopes)


def iter_census(path: str | Path) -> Iterator[dict[str, Any]]:
    path = Path(path)
    if path.is_dir():
        path = path / "census.jsonl"
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    return


CSV_FIELDS = ["path", "rel_path", "top_folder", "name", "ext", "size", "mtime", "sha256",
              "dwg_version", "dwg_release", "placeholder", "status", "error"]


def finalize(out_dir: Path, seen: set[str], other_ext: Counter, walk_errors: list, roots: list[str],
             exts: set[str]) -> dict[str, Any]:
    """Compact census.jsonl (last row per path, only paths still present), write CSV, build summary."""
    jsonl = out_dir / "census.jsonl"
    last: dict[str, int] = {}
    for index, row in enumerate(iter_census(jsonl)):
        last[row["path"]] = index
    tmp = out_dir / "census.jsonl.tmp"
    by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_folder: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_version: Counter = Counter()
    by_status: Counter = Counter()
    first_path: dict[str, str] = {}
    dup_groups: dict[str, list[str]] = {}
    dup_bytes = 0
    total = [0, 0]
    with open(tmp, "w", encoding="utf-8", newline="\n") as sink, \
            open(out_dir / "census.csv", "w", encoding="utf-8-sig", newline="") as csv_handle:
        writer = csv.DictWriter(csv_handle, CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(iter_census(jsonl)):
            if last.get(row["path"]) != index or (row["path"] not in seen and _in_scopes(row["path"], roots)):
                continue
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            writer.writerow(row)
            by_status[row["status"]] += 1
            if row["status"] == "skipped_temp":
                continue
            size = row.get("size") or 0
            total[0] += 1; total[1] += size
            by_ext[row["ext"]][0] += 1; by_ext[row["ext"]][1] += size
            folder = row["top_folder"] or "(root)"
            by_folder[folder][0] += 1; by_folder[folder][1] += size
            if row["ext"] in (".dwg", ".dxf"):
                by_version[f'{row["ext"]} {row.get("dwg_version") or "unknown"} {row.get("dwg_release") or ""}'.strip()] += 1
            sha = row.get("sha256")
            if sha:
                if sha in first_path:
                    dup_groups.setdefault(sha, [first_path[sha]]).append(row["path"])
                    dup_bytes += size
                else:
                    first_path[sha] = row["path"]
    os.replace(tmp, jsonl)
    groups = sorted(dup_groups.items(), key=lambda item: -len(item[1]))
    return {
        "generated_at": _now(), "roots": roots, "extensions": sorted(exts),
        "files": total[0], "bytes": total[1], "unique_contents": len(first_path),
        "by_status": dict(by_status),
        "skipped_temp": by_status.get("skipped_temp", 0),
        "by_extension": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(by_ext.items())},
        "by_top_folder": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(by_folder.items())},
        "by_dwg_version": dict(by_version.most_common()),
        "duplicates": {"groups": len(groups), "redundant_files": sum(len(p) - 1 for _, p in groups),
                       "redundant_bytes": dup_bytes,
                       "top_groups": [{"sha256": s, "paths": p} for s, p in groups[:200]]},
        "other_extensions_ignored": dict(other_ext.most_common(50)),
        "walk_errors": walk_errors[:500], "walk_error_count": len(walk_errors),
    }


def _mb(value: int) -> str:
    return f"{value / 1048576:,.1f} MB"


def summary_markdown(summary: dict[str, Any]) -> str:
    lines = ["# 도면 전수조사 (census) 요약", "", f"- 생성: {summary['generated_at']}",
             f"- 루트: {', '.join(summary['roots'])}",
             f"- 대상 파일: {summary['files']:,}개 / {_mb(summary['bytes'])}",
             f"- 고유 내용(sha256): {summary['unique_contents']:,}개",
             f"- 중복 그룹: {summary['duplicates']['groups']:,} (중복 파일 {summary['duplicates']['redundant_files']:,}개,"
             f" {_mb(summary['duplicates']['redundant_bytes'])})",
             f"- 임시/백업 파일(제외): {summary['skipped_temp']:,}개",
             f"- 상태별: {json.dumps(summary['by_status'], ensure_ascii=False)}",
             f"- 폴더 접근 오류: {summary['walk_error_count']:,}건", ""]
    if "run" in summary:
        lines += [f"- 이번 실행: {json.dumps(summary['run'], ensure_ascii=False)}", ""]
    lines += ["## 확장자별", "", "| 확장자 | 파일 | 용량 |", "|---|---:|---:|"]
    lines += [f"| {k} | {v['files']:,} | {_mb(v['bytes'])} |" for k, v in summary["by_extension"].items()]
    lines += ["", "## 최상위 폴더(프로젝트)별", "", "| 폴더 | 파일 | 용량 |", "|---|---:|---:|"]
    lines += [f"| {k} | {v['files']:,} | {_mb(v['bytes'])} |" for k, v in summary["by_top_folder"].items()]
    lines += ["", "## DWG/DXF 버전별", "", "| 버전 | 파일 |", "|---|---:|"]
    lines += [f"| {k} | {v:,} |" for k, v in summary["by_dwg_version"].items()]
    lines += ["", "## 중복 그룹 (상위 20)", ""]
    for group in summary["duplicates"]["top_groups"][:20]:
        lines.append(f"- `{group['sha256'][:12]}` x{len(group['paths'])}: " + " | ".join(group["paths"][:5]))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------
# enqueue-census
# --------------------------------------------------------------------------------------------

DISCIPLINE_KEYWORDS = [
    # (discipline, Korean substrings, ASCII tokens)
    ("STR", ("구조",), ("STR", "STRUCT", "STRUCTURAL", "STRUCTURE")),
    ("MEP", ("기계", "설비", "위생", "공조", "소방", "배관"), ("MEP", "MECH", "MECHANICAL", "HVAC", "PLUMBING", "FIRE", "FP")),
    ("ELEC", ("전기", "통신", "조명"), ("ELEC", "ELECTRICAL", "ELE", "TEL")),
    ("CIVIL", ("토목",), ("CIVIL", "CIV")),
    ("LAND", ("조경",), ("LAND", "LANDSCAPE", "LS")),
    ("INT", ("인테리어", "실내"), ("INT", "INTERIOR")),
    ("ARCH", ("건축", "평면", "입면", "단면", "상세", "창호", "평면도"), ("ARCH", "ARC", "ARCHITECTURE", "ARCHITECTURAL")),
]
_TOKEN = re.compile(r"[A-Za-z]+")


def guess_discipline(rel_path: str, default: str = "ARCH") -> str:
    """Deepest path segment (file name first) that names a discipline wins."""
    segments = [s for s in unicodedata.normalize("NFC", rel_path).replace("\\", "/").split("/") if s]
    for segment in reversed(segments):
        tokens = {t.upper() for t in _TOKEN.findall(segment)}
        for discipline, korean, ascii_tokens in DISCIPLINE_KEYWORDS:
            if any(k in segment for k in korean) or tokens & set(ascii_tokens):
                return discipline
    return default


def project_id_for(folder: str, prefix: str = "P-") -> str:
    """Stable, readable project id from a folder name (Korean kept; separators collapsed)."""
    name = unicodedata.normalize("NFC", folder or "").strip()
    slug = re.sub(r"[^\w.\-]+", "-", name, flags=re.UNICODE).strip("-.") or "ROOT"
    return prefix + slug[:120]


def document_id_for(sha256: str) -> str:
    return "doc_" + sha256[:24]


def _under_roots(path: str, roots: Iterable[Path]) -> bool:
    candidate = Path(path).resolve()
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def project_folder(row: dict[str, Any], project_root: str | None = None, depth: int = 1) -> str:
    """Folder that names the row's project: the first ``depth`` folders below ``project_root``
    (default: the census root, i.e. its ``top_folder``); the root's own name for files directly in it."""
    if project_root is None and depth == 1:
        return row.get("top_folder") or os.path.basename(row.get("root", "").rstrip("/\\"))
    root = project_root or row.get("root", "")
    parts = _relative(row["path"], root).split("/")[:-1]
    if parts and parts[0] == "..":  # not below project_root: fall back to the census grouping
        return row.get("top_folder") or os.path.basename(row.get("root", "").rstrip("/\\"))
    return "/".join(parts[:max(0, depth)]) or os.path.basename(root.rstrip("/\\"))


def plan_jobs(census: str | Path, extensions: Iterable[str] | None = None, only_folders: Iterable[str] = (),
              prefix: str = "P-", default_discipline: str = "ARCH", project_root: str | None = None,
              project_depth: int = 1, priority: int | None = None) -> list[dict[str, Any]]:
    """One job per unique sha256; the lexicographically first path is canonical, the rest are aliases.

    Jobs come newest first (``mtime`` of the newest copy), and carry ``priority``/``mtime`` so workers
    claim live projects before old archives (see ``Database.claim``).
    """
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions} if extensions else None
    only = {unicodedata.normalize("NFC", f) for f in only_folders}
    groups: dict[str, dict[str, Any]] = {}
    for row in iter_census(census):
        if row.get("status") != "ok" or not row.get("sha256"):
            continue
        if exts and row["ext"] not in exts:
            continue
        if only and unicodedata.normalize("NFC", row.get("top_folder") or "") not in only:
            continue
        group = groups.get(row["sha256"])
        if group is None:
            groups[row["sha256"]] = {"canonical": row, "aliases": []}
        elif row["path"] < group["canonical"]["path"]:
            group["aliases"].append(group["canonical"]["path"])
            group["canonical"] = row
        else:
            group["aliases"].append(row["path"])
    newest: dict[str, str] = {}
    for sha, group in groups.items():
        newest[sha] = group["canonical"].get("mtime") or ""
    for row in iter_census(census):
        if row.get("sha256") in newest and (row.get("mtime") or "") > newest[row["sha256"]]:
            newest[row["sha256"]] = row["mtime"]
    jobs = []
    for sha, group in sorted(groups.items(), key=lambda item: (newest[item[0]], item[1]["canonical"]["path"]),
                             reverse=True):
        row = group["canonical"]
        folder = project_folder(row, project_root, project_depth)
        extra = {"mtime": newest[sha]}
        if priority is not None:
            extra["priority"] = int(priority)
        jobs.append({**extra,
            "source": row["path"], "name": row["name"], "project_id": project_id_for(folder, prefix),
            "document_id": document_id_for(sha), "revision": 0,
            "discipline": guess_discipline(row["rel_path"], default_discipline),
            "sha256": sha, "size": row.get("size"), "rel_path": row["rel_path"], "top_folder": folder,
            "dwg_version": row.get("dwg_version"), "aliases": sorted(group["aliases"]),
        })
    return jobs


def enqueue_census(db, census: str | Path, queue: str = "cad", limit: int | None = None,
                   only_folders: Iterable[str] = (), extensions: Iterable[str] | None = None,
                   import_roots: Iterable[Path] | None = None, requeue_failed: bool = False,
                   prefix: str = "P-", default_discipline: str = "ARCH", dry_run: bool = False,
                   project_root: str | None = None, project_depth: int = 1,
                   priority: int | None = None) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    jobs = plan_jobs(census, extensions, only_folders, prefix, default_discipline, project_root, project_depth,
                     priority)
    roots = [Path(r).resolve() for r in import_roots] if import_roots else None
    stats = Counter()
    outside: list[str] = []
    selected = []
    for job in jobs:
        if roots is not None and not _under_roots(job["source"], roots):
            stats["outside_import_roots"] += 1
            outside.append(job["source"])
            continue
        selected.append({**job, "queue": queue})
        if limit and len(selected) >= limit:
            break
    if dry_run:
        return {"planned": len(selected), **stats, "jobs": selected, "outside_examples": outside[:20]}
    with db.connect() as conn:
        for job in selected:
            row = conn.execute("""INSERT INTO aec.jobs(id,dedup_key,payload) VALUES(%s,%s,%s)
                ON CONFLICT(dedup_key) DO UPDATE SET
                  payload = aec.jobs.payload || jsonb_build_object('aliases', EXCLUDED.payload->'aliases'),
                  updated_at = now()
                RETURNING (xmax = 0) AS inserted, state""",
                (uuid.uuid4(), job["sha256"], Jsonb(job))).fetchone()
            if row["inserted"]:
                stats["enqueued"] += 1
            else:
                stats["already_present"] += 1
                if row["state"] == "FAILED" and requeue_failed:
                    conn.execute("""UPDATE aec.jobs SET state='QUEUED',attempts=0,error=NULL,stage='queued',
                        progress=0,updated_at=now() WHERE dedup_key=%s AND state='FAILED'""", (job["sha256"],))
                    stats["requeued_failed"] += 1
    return {"planned": len(selected), "unique_contents": len(jobs), **stats,
            "aliased_paths": sum(len(j["aliases"]) for j in selected), "outside_examples": outside[:20]}


# --------------------------------------------------------------------------------------------
# bulk-census: every source of a sources.json, in priority order, enqueueing as it goes
# --------------------------------------------------------------------------------------------

def parse_min_free(value: str | dict | None) -> dict[str, float]:
    """``"C:\\=12;D:\\=50"`` (or a dict) -> {path: GB}: minimum free space to keep on each drive."""
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): float(v) for k, v in value.items()}
    pairs = (item.rsplit("=", 1) for item in str(value).split(";") if "=" in item)
    return {path.strip(): float(gb) for path, gb in pairs if path.strip()}


def wait_for_disk(min_free: dict[str, float], log: Any = print, poll: float = 300.0, sleep=time.sleep) -> float:
    """Block while any drive is below its free-space floor (e.g. a cloud-drive cache filling C:).

    Returns the seconds waited. A drive that cannot be queried is ignored.
    """
    import shutil

    waited = 0.0
    while True:
        low = []
        for path, floor in min_free.items():
            try:
                free = shutil.disk_usage(path).free / 1024 ** 3
            except OSError:
                continue
            if free < floor:
                low.append(f"{path} {free:.1f} GB < {floor:g} GB")
        if not low:
            return waited
        if waited == 0:
            log(f"[disk] pausing: {', '.join(low)}")
        sleep(poll)
        waited += poll


def load_bulk_config(path: str | Path) -> dict[str, Any]:
    """sources.json (kept next to the data, never in git: it names private folders)::

        {"out": "D:/AECData/census-v3", "extensions": ".dwg,.dxf,.pdf,.ifc",
         "inventory_extensions": ".rvt,.skp", "ingest_extensions": ".dwg,.dxf,.pdf",
         "exclude": ["hillside_villa*"], "min_free_gb": {"C:/": 12, "D:/": 50}, "hash_workers": 6,
         "sources": [{"name": "01-live", "roots": ["G:/drive/live"], "priority": 10,
                      "project_root": "G:/drive/live", "project_depth": 2, "prefix": "P-",
                      "exclude": ["G:/drive/live/old"]}, ...]}

    Sources run in list order; ``priority`` (lower first) orders the worker queue across sources.
    """
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    names = [s.get("name") for s in config.get("sources", [])]
    if not names or any(not n for n in names) or len(set(names)) != len(names):
        raise ValueError("sources.json needs a non-empty 'sources' list with unique 'name's")
    for source in config["sources"]:
        if not source.get("roots"):
            raise ValueError(f"source {source['name']}: 'roots' is empty")
    return config


def bulk_import_roots(config: dict[str, Any]) -> list[str]:
    return sorted({os.path.abspath(r) for s in config["sources"] for r in s["roots"]})


def run_bulk_census(db, config_path: str | Path, enqueue_every: float = 600.0, refresh: bool = False,
                    log: Any = print) -> dict[str, Any]:
    """Census every source (resumable), enqueueing its unique drawings while the census is still running.

    Re-running is safe: finished sources are skipped (``refresh`` re-walks them with ``resume``), the
    census of an interrupted source resumes from its jsonl, and jobs are deduplicated by sha256.
    Progress is kept in ``<out>/state.json``.
    """
    config = load_bulk_config(config_path)
    out_root = Path(config["out"])
    out_root.mkdir(parents=True, exist_ok=True)
    state_path = out_root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    exts = [e.strip() for e in str(config.get("extensions", ",".join(DEFAULT_EXTENSIONS))).split(",") if e.strip()]
    inventory = [e.strip() for e in str(config.get("inventory_extensions", "")).split(",") if e.strip()]
    min_free = parse_min_free(config.get("min_free_gb"))
    ingest = [e.strip() for e in str(config.get("ingest_extensions", ".dwg,.dxf,.pdf")).split(",") if e.strip()]

    def save():
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)

    for source in config["sources"]:
        name = source["name"]
        entry = state.setdefault(name, {})
        if entry.get("done") and not refresh:
            log(f"[bulk] {name}: done at {entry.get('finished_at')}, skipping")
            continue
        out = out_root / name

        def enqueue(final: bool, source=source, out=out, entry=entry, name=name) -> dict[str, Any]:
            if not (out / "census.jsonl").exists():
                return {}
            result = enqueue_census(db, out, queue=config.get("queue", "cad"), extensions=ingest,
                                    import_roots=[Path(r) for r in bulk_import_roots(config)],
                                    prefix=source.get("prefix", "P-"),
                                    default_discipline=source.get("default_discipline", "ARCH"),
                                    project_root=source.get("project_root"),
                                    project_depth=int(source.get("project_depth", 1)),
                                    priority=int(source.get("priority", 100)))
            result.pop("outside_examples", None)
            entry["enqueue"] = result
            entry["enqueued_at"] = _now()
            save()
            log(f"[bulk] {name}: {'final' if final else 'partial'} enqueue {json.dumps(result, ensure_ascii=False)}")
            return result

        last = [time.monotonic()]
        logged = [0]

        def progress(count, path, enqueue=enqueue, last=last, logged=logged, name=name):
            if min_free:
                wait_for_disk(min_free, log)
            if time.monotonic() - last[0] >= enqueue_every:
                last[0] = time.monotonic()
                try:
                    enqueue(False)
                except Exception as exc:  # the census must not die because the DB blinked
                    log(f"[bulk] {name}: partial enqueue failed: {exc}")
            if count // 1000 > logged[0]:
                logged[0] = count // 1000
                log(f"[bulk] {name}: {count:,} files walked")

        entry.update(started_at=entry.get("started_at") or _now(), done=False)
        save()
        log(f"[bulk] {name}: census {source['roots']} -> {out}")
        result = run_census(source["roots"], out, exts, resume=True, flush_every=50,
                            hash_placeholders=bool(source.get("hash_placeholders", True)), progress=progress,
                            exclude=[*config.get("exclude", []), *source.get("exclude", [])],
                            inventory_extensions=inventory,
                            hash_workers=int(source.get("hash_workers", config.get("hash_workers", 1))))
        entry["census"] = {"files": result.summary.get("files"), "unique_contents": result.summary.get("unique_contents"),
                           "by_status": result.summary.get("by_status"), "run": result.summary.get("run")}
        enqueue(True)
        entry.update(done=True, finished_at=_now())
        save()
    return state


# --------------------------------------------------------------------------------------------
# run-workers
# --------------------------------------------------------------------------------------------

def pending_jobs(db, queue: str, max_attempts: int) -> int:
    with db.connect() as conn:
        return conn.execute("""SELECT count(*) AS n FROM aec.jobs
            WHERE COALESCE(payload->>'queue','cad')=%s AND
            ((state='QUEUED' AND attempts<%s) OR state='RUNNING')""", (queue, max_attempts)).fetchone()["n"]


def worker_stop_file(settings) -> Path:
    """Drain switch for host workers: while this file exists, workers finish their current job and exit, and
    ``run-workers`` / ``bulk-run.ps1 -Role workers`` do not start new ones (scripts/ops/stop-workers.ps1)."""
    return Path(os.getenv("AEC_WORKER_STOP_FILE") or (Path(settings.data_root) / "bulk" / "STOP-WORKERS"))


def worker_stop_reason(settings) -> str | None:
    """Why a worker should exit before claiming another job (None = keep going).

    Stopping the scheduled task kills only the ``run-workers`` parent; spawn children used to keep claiming
    jobs with the old code (seen on the PC after a deploy). A child now exits once its parent is gone."""
    if worker_stop_file(settings).exists():
        return "stop file present"
    parent = mp.parent_process()
    if parent is not None and not parent.is_alive():
        return "parent process exited"
    return None


def worker_process(settings, queue: str, poll: float, index: int) -> int:
    """Top-level (picklable) target for spawn: drain the queue, exit when nothing is left, when the stop file
    appears or when the parent ``run-workers`` process is gone (checked between jobs, never mid-job)."""
    import logging

    from .db import Database
    from .worker import IngestionWorker, quiet_noisy_loggers

    logging.basicConfig(level=logging.INFO, format=f"[w{index}] %(asctime)s %(levelname)s %(message)s")
    quiet_noisy_loggers()
    db = Database(settings.dsn)
    worker = IngestionWorker(db, settings, queue=queue, worker_id=f"census-{os.getpid()}-{index}")
    min_free = parse_min_free(os.getenv("AEC_MIN_FREE_GB"))
    processed = 0
    while True:
        reason = worker_stop_reason(settings)
        if reason:
            logging.getLogger(__name__).warning("worker %s exiting after %d jobs: %s", index, processed, reason)
            return processed
        if min_free:
            wait_for_disk(min_free, logging.getLogger(__name__).warning)
        try:
            if worker.run_once():
                processed += 1
                continue
            if pending_jobs(db, queue, settings.max_attempts) == 0:
                return processed
        except Exception as exc:  # DB hiccup: back off, keep going
            logging.getLogger(__name__).warning("worker loop error: %s", exc)
        time.sleep(poll)


def ensure_project_graphs(db, queue: str) -> list[str]:
    """Create the AGE graph and its labels for every queued project before workers start.

    Two workers projecting the first documents of the same new project would otherwise race on
    ``create_graph`` / implicit label creation and one job would fail with "already exists".
    """
    from .db import graph_name

    created = []
    with db.connect() as conn:
        projects = [r["p"] for r in conn.execute("""SELECT DISTINCT payload->>'project_id' AS p FROM aec.jobs
            WHERE COALESCE(payload->>'queue','cad')=%s AND state IN ('QUEUED','RUNNING')""", (queue,))]
    for project in projects:
        if not project:
            continue
        graph = graph_name(project)
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name=%s", (graph,)).fetchone():
                conn.execute("SELECT create_graph(%s)", (graph,))
                created.append(project)
            gid = conn.execute("SELECT graphid FROM ag_catalog.ag_graph WHERE name=%s", (graph,)).fetchone()["graphid"]
            labels = {r["name"] for r in conn.execute("SELECT name FROM ag_catalog.ag_label WHERE graph=%s", (gid,))}
            if "Entity" not in labels:
                conn.execute("SELECT create_vlabel(%s, 'Entity')", (graph,))
            if "Rel" not in labels:
                conn.execute("SELECT create_elabel(%s, 'Rel')", (graph,))
    return created


def run_workers(settings, processes: int = 2, queue: str = "cad", poll: float = 2.0) -> dict[str, Any]:
    from .db import Database

    db = Database(settings.dsn)
    with db.connect() as conn:
        started = conn.execute("SELECT now() AS t").fetchone()["t"]
    if worker_stop_file(settings).exists():
        return {**queue_summary(db, queue, since=started), "stopped": "stop file present"}
    ensure_project_graphs(db, queue)
    context = mp.get_context("spawn")
    workers = [context.Process(target=worker_process, args=(settings, queue, poll, i), daemon=False)
               for i in range(max(1, processes))]
    for proc in workers:
        proc.start()
    try:
        for proc in workers:
            proc.join()
    except KeyboardInterrupt:
        for proc in workers:
            proc.terminate()
        raise
    return queue_summary(db, queue, since=started)


def queue_summary(db, queue: str, since=None) -> dict[str, Any]:
    params: list[Any] = [queue]
    where = "COALESCE(payload->>'queue','cad')=%s"
    if since is not None:
        where += " AND updated_at >= %s"
        params.append(since)
    with db.connect() as conn:
        states = {r["state"]: r["n"] for r in conn.execute(
            f"SELECT state, count(*) AS n FROM aec.jobs WHERE {where} GROUP BY state", params)}
        failed = [{"job_id": str(r["id"]), "source": r["source"], "error": r["error"]} for r in conn.execute(
            f"""SELECT id, payload->>'source' AS source, error FROM aec.jobs WHERE {where} AND state='FAILED'
            ORDER BY updated_at""", params)]
    return {"queue": queue, "succeeded": states.get("SUCCEEDED", 0), "failed": states.get("FAILED", 0),
            "queued": states.get("QUEUED", 0), "running": states.get("RUNNING", 0), "failed_jobs": failed}


# --------------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------------

def build_report(db, census: str | Path | None = None, projects: Iterable[str] = (),
                 prefix: str = "P-") -> dict[str, Any]:
    projects = list(projects)
    filt, params = ("WHERE project_id = ANY(%s)", [projects]) if projects else ("", [])
    report: dict[str, Any] = {"generated_at": _now(), "projects": {}}
    with db.connect() as conn:
        conn.execute("SET statement_timeout = '300s'")
        for row in conn.execute(f"SELECT project_id, kind, count(*) AS n FROM aec.objects {filt} "
                                "GROUP BY project_id, kind ORDER BY project_id, kind", params):
            report["projects"].setdefault(row["project_id"], {"kinds": {}, "documents": 0})["kinds"][row["kind"]] = row["n"]
        for row in conn.execute(f"SELECT project_id, count(*) AS n FROM aec.documents {filt} GROUP BY project_id", params):
            report["projects"].setdefault(row["project_id"], {"kinds": {}, "documents": 0})["documents"] = row["n"]
        jfilt = "WHERE payload->>'project_id' = ANY(%s)" if projects else ""
        report["jobs"] = {r["state"]: r["n"] for r in conn.execute(
            f"SELECT state, count(*) AS n FROM aec.jobs {jfilt} GROUP BY state", params)}
        report["failed_jobs"] = [
            {"job_id": str(r["id"]), "project_id": r["project_id"], "source": r["source"], "error": r["error"]}
            for r in conn.execute(f"""SELECT id, payload->>'project_id' AS project_id, payload->>'source' AS source,
                error FROM aec.jobs {jfilt + (' AND' if jfilt else 'WHERE')} state='FAILED' ORDER BY updated_at DESC
                LIMIT 1000""", params)]
    if census:
        per_project: dict[str, set] = defaultdict(set)
        for row in iter_census(census):
            if row.get("status") == "ok" and row.get("sha256"):
                folder = row.get("top_folder") or os.path.basename(row.get("root", "").rstrip("/\\"))
                per_project[project_id_for(folder, prefix)].add(row["sha256"][:24])
        for project, docs in per_project.items():
            if projects and project not in projects:
                continue
            entry = report["projects"].setdefault(project, {"kinds": {}, "documents": 0})
            entry["census_unique_files"] = len(docs)
        report["census_unique_files"] = sum(e.get("census_unique_files", 0) for e in report["projects"].values())
    report["documents_ingested"] = sum(e["documents"] for e in report["projects"].values())
    return report


def report_markdown(report: dict[str, Any]) -> str:
    kinds = sorted({k for e in report["projects"].values() for k in e["kinds"]})
    lines = ["# AEC 적재 현황 리포트", "", f"- 생성: {report['generated_at']}",
             f"- 적재 문서: {report['documents_ingested']:,}"]
    if "census_unique_files" in report:
        lines.append(f"- census 고유 파일: {report['census_unique_files']:,}")
    lines += [f"- 작업 상태: {json.dumps(report['jobs'], ensure_ascii=False)}", "",
              "## 프로젝트별 객체 수", "",
              "| 프로젝트 | 문서 | census | " + " | ".join(kinds) + " |",
              "|---|---:|---:|" + "---:|" * len(kinds)]
    for project, entry in sorted(report["projects"].items()):
        lines.append(f"| {project} | {entry['documents']} | {entry.get('census_unique_files', '-')} | "
                     + " | ".join(str(entry["kinds"].get(k, 0)) for k in kinds) + " |")
    lines += ["", f"## 실패 작업 ({len(report['failed_jobs'])})", ""]
    lines += [f"- `{j['project_id']}` {j['source']}: {j['error']}" for j in report["failed_jobs"][:200]]
    return "\n".join(lines) + "\n"

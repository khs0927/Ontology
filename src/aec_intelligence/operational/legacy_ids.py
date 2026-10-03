"""Find documents stored under the legacy ``doc_<file stem>`` id and optionally re-ingest them.

Before the ad-hoc ingestion fix, REST/CLI jobs had no ``document_id`` and the worker used
``doc_<stem>``. Different files sharing a stem (``A/평면도.dwg`` and ``B/평면도.dwg``) were projected
onto one document: the later ingestion replaced the earlier one's objects and relations, while
``aec.documents.source_key`` kept the *first* file's path.

This tool works offline from the immutable snapshot files (``<AEC_DATA_ROOT>/snapshots/<doc>/rev-N.json``),
which record the real ``source_key`` of every revision. Nothing is changed unless ``--apply`` is given.

    python -m aec_intelligence.operational.legacy_ids                      # dry run: report only
    python -m aec_intelligence.operational.legacy_ids --json report.json   # machine-readable report
    python -m aec_intelligence.operational.legacy_ids --apply              # enqueue re-ingest jobs

``--apply`` only *enqueues* ingestion jobs (content-addressed ids, inside ``AEC_IMPORT_ROOTS``) for
source files that still exist; it never deletes. After the new documents are indexed, review the
printed cleanup SQL and run it yourself to drop the legacy rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONTENT_ID = re.compile(r"^doc_[0-9a-f]{24}$")


def is_legacy_id(document_id: str) -> bool:
    return document_id.startswith("doc_") and not CONTENT_ID.match(document_id)


@dataclass
class LegacyDocument:
    document_id: str
    revisions: int = 0
    sources: dict[str, list[int]] = field(default_factory=dict)  # source_key -> revisions
    projects: dict[str, str] = field(default_factory=dict)  # source_key -> project_id (latest)
    hashes: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    @property
    def collided(self) -> bool:
        return len(self.sources) > 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["collided"] = self.collided
        return data


def _revision(path: Path) -> int:
    match = re.fullmatch(r"rev-(\d+)\.json", path.name)
    return int(match.group(1)) if match else -1


def scan(data_root: Path) -> list[LegacyDocument]:
    snapshots = Path(data_root) / "snapshots"
    found: list[LegacyDocument] = []
    if not snapshots.is_dir():
        return found
    for doc_dir in sorted(p for p in snapshots.iterdir() if p.is_dir() and is_legacy_id(p.name)):
        doc = LegacyDocument(doc_dir.name)
        for rev_file in sorted(doc_dir.glob("rev-*.json"), key=_revision):
            try:
                snap = json.loads(rev_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc.unreadable.append(rev_file.name)
                continue
            doc.revisions += 1
            key = str(snap.get("source_key") or "")
            doc.sources.setdefault(key, []).append(int(snap.get("revision", _revision(rev_file))))
            if snap.get("project_id"):
                doc.projects[key] = str(snap["project_id"])
            if snap.get("source_hash"):
                doc.hashes.append(str(snap["source_hash"]))
        found.append(doc)
    return found


def cleanup_sql(document_ids: list[str]) -> str:
    """Review-only SQL that removes legacy rows (children first). Never executed by this tool."""
    if not document_ids:
        return ""
    ids = ", ".join("'" + d.replace("'", "''") + "'" for d in document_ids)
    tables = ["aec.embeddings e USING aec.objects o", "aec.relations", "aec.objects", "aec.index_state",
              "aec.snapshots", "aec.documents"]
    lines = ["BEGIN;"]
    for table in tables:
        if table.startswith("aec.embeddings"):
            lines.append(f"DELETE FROM {table} WHERE e.object_id = o.id AND o.document_id IN ({ids});")
        elif table == "aec.documents":
            lines.append(f"DELETE FROM {table} WHERE id IN ({ids});")
        else:
            lines.append(f"DELETE FROM {table} WHERE document_id IN ({ids});")
    lines.append("-- Check the counts above, then COMMIT; (or ROLLBACK;). Graph (AGE) vertices are rebuilt on re-index.")
    return "\n".join(lines)


def plan_reingest(docs: list[LegacyDocument], allowed) -> tuple[list[tuple[Path, str]], list[dict[str, str]]]:
    """Existing source files (with their original project id) to re-ingest, plus skipped sources."""
    files: list[tuple[Path, str]] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()
    for doc in docs:
        for key in doc.sources:
            if not key:
                skipped.append({"document_id": doc.document_id, "source": key, "reason": "no source_key"})
                continue
            try:
                path = allowed(key)
            except OSError:
                skipped.append({"document_id": doc.document_id, "source": key, "reason": "file missing"})
                continue
            except ValueError:
                skipped.append({"document_id": doc.document_id, "source": key, "reason": "outside AEC_IMPORT_ROOTS"})
                continue
            if path not in seen:
                seen.add(path)
                files.append((path, doc.projects.get(key, "")))
    return files, skipped


def main(argv: list[str] | None = None, *, settings=None, database_factory=None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", type=Path, help="defaults to AEC_DATA_ROOT")
    parser.add_argument("--json", type=Path, help="write the report as JSON")
    parser.add_argument("--apply", action="store_true", help="enqueue re-ingest jobs (never deletes)")
    parser.add_argument("--project-id", default=None,
                        help="override the project id (default: the one recorded in the snapshot, else P-DEFAULT)")
    parser.add_argument("--discipline", default="")
    parser.add_argument("--queue", default="cad", choices=["cad", "ocr"])
    args = parser.parse_args(argv)

    if settings is None:
        from .config import Settings

        settings = Settings.from_env()
    data_root = args.data_root or settings.data_root
    docs = scan(data_root)
    collided = [d for d in docs if d.collided]
    files, skipped = plan_reingest(docs, settings.allowed_source)

    print(f"legacy doc_<stem> documents: {len(docs)} (collided: {len(collided)}) under {data_root}", file=out)
    for d in docs:
        flag = "COLLIDED" if d.collided else "legacy"
        print(f"  [{flag}] {d.document_id}: {d.revisions} revision(s)", file=out)
        for key, revs in d.sources.items():
            print(f"      rev {','.join(map(str, revs))}: {key or '<unknown>'}", file=out)
        if d.unreadable:
            print(f"      unreadable: {', '.join(d.unreadable)}", file=out)
    print(f"re-ingestable source files: {len(files)}; skipped: {len(skipped)}", file=out)
    for s in skipped:
        print(f"  skip {s['source'] or '<unknown>'} ({s['reason']})", file=out)

    if args.json:
        args.json.write_text(json.dumps({
            "data_root": str(data_root),
            "documents": [d.to_dict() for d in docs],
            "reingest": [{"source": str(f), "project_id": proj} for f, proj in files],
            "skipped": skipped,
            "cleanup_sql": cleanup_sql([d.document_id for d in docs]),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.apply:
        print("dry run: nothing changed (use --apply to enqueue re-ingest jobs).", file=out)
        return 0

    from .ingest_jobs import ingest_job
    from .parsers import SUPPORTED

    if database_factory is None:
        from .db import Database

        database_factory = Database
    db = database_factory(settings.dsn)
    enqueued = 0
    for path, recorded_project in files:
        if path.suffix.lower() not in SUPPORTED:
            print(f"  skip {path} (unsupported type)", file=out)
            continue
        project_id = args.project_id or recorded_project or "P-DEFAULT"
        payload, dedup_key = ingest_job(path, project_id=project_id, discipline=args.discipline, queue=args.queue)
        db.enqueue(payload, dedup_key)
        enqueued += 1
        print(f"  enqueued {path} -> {payload['document_id']}", file=out)
    print(f"enqueued {enqueued} job(s). After they finish, review and run this cleanup SQL yourself:", file=out)
    print(cleanup_sql([d.document_id for d in docs]), file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

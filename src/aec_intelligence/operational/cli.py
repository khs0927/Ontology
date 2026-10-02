"""CLI subcommands for operational AEC PostgreSQL + AGE pipeline."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import Settings
from .db import Database
from .parsers import SUPPORTED
from .search import SearchRouter
from .worker import IngestionWorker


def redact_dsn(dsn: str) -> str:
    """Return the DSN with any password replaced, so it can be printed."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        if parts.password is not None:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
            return urlunsplit(parts._replace(netloc=netloc))
        return dsn
    return " ".join("password=***" if t.startswith("password=") else t for t in dsn.split())


def main(args=None):
    parser = argparse.ArgumentParser(prog="aec operational", description="AEC Operational Services CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # init-db
    init_parser = subparsers.add_parser("init-db", help="Initialize PostgreSQL schema and extensions")

    # worker
    worker_parser = subparsers.add_parser("worker", help="Run background ingestion worker")
    worker_parser.add_argument("--queue", default="cad", choices=["cad", "ocr"], help="Queue name")
    worker_parser.add_argument("--once", action="store_true", help="Process a single job and exit")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Run REST API and web dashboard")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve_parser.add_argument("--port", type=int, default=8000, help="Bind port")

    # ingest
    ingest_parser = subparsers.add_parser("ingest", help="Enqueue file or directory for ingestion")
    ingest_parser.add_argument("path", help="Path to file or directory")
    ingest_parser.add_argument("--project-id", default="P-DEFAULT", help="Project ID")
    ingest_parser.add_argument("--discipline", default="ARCH", help="Discipline")
    ingest_parser.add_argument("--queue", default="cad", choices=["cad", "ocr"], help="Queue name")

    # search
    search_parser = subparsers.add_parser("search", help="Execute hybrid search")
    search_parser.add_argument("query", help="Search query string")
    search_parser.add_argument("--project-id", default=None, help="Filter by project ID")
    search_parser.add_argument("--kind", default=None, help="Filter by object kind")
    search_parser.add_argument("--top-k", type=int, default=5, help="Number of results")

    _add_batch_commands(subparsers)

    parsed = parser.parse_args(args)
    settings = Settings.from_env()
    db = Database(settings.dsn)

    if parsed.subcommand in BATCH_COMMANDS:
        return BATCH_COMMANDS[parsed.subcommand](parsed, settings, db)

    if parsed.subcommand == "init-db":
        print(f"Initializing database at {redact_dsn(settings.dsn)}...")
        db.initialize()
        print("Database schema and extensions initialized successfully.")

    elif parsed.subcommand == "worker":
        worker = IngestionWorker(db, settings, queue=parsed.queue)
        if parsed.once:
            processed = worker.run_once()
            print(f"Processed single job: {processed}")
        else:
            print(f"Starting worker on queue '{parsed.queue}'...")
            try:
                worker.run_forever()
            except KeyboardInterrupt:
                print("Worker stopped by user.")

    elif parsed.subcommand == "serve":
        import uvicorn
        print(f"Starting AEC Intelligence API & Dashboard on {parsed.host}:{parsed.port}...")
        uvicorn.run("aec_intelligence.operational.api:create_app", factory=True, host=parsed.host, port=parsed.port)

    elif parsed.subcommand == "ingest":
        target = Path(parsed.path).resolve()
        if not target.exists():
            print(f"Error: Path does not exist: {target}", file=sys.stderr)
            sys.exit(1)

        files = []
        if target.is_file():
            files.append(target)
        else:
            for p in target.rglob("*"):
                if p.is_file() and p.suffix.lower() in SUPPORTED:
                    files.append(p)

        enqueued = 0
        for f in files:
            mtime = f.stat().st_mtime
            dedup_key = hashlib.sha256(f"{parsed.project_id}|{f}|{mtime}".encode("utf-8")).hexdigest()
            payload = {
                "source": str(f),
                "name": f.name,
                "project_id": parsed.project_id,
                "discipline": parsed.discipline,
                "queue": parsed.queue,
            }
            row = db.enqueue(payload, dedup_key)
            print(f"Enqueued: {f.name} -> Job {row['id']}")
            enqueued += 1
        print(f"Total enqueued files: {enqueued}")

    elif parsed.subcommand == "search":
        router = SearchRouter(db, settings)
        res = router.search(
            query=parsed.query,
            project_id=parsed.project_id,
            kind=parsed.kind,
            top_k=parsed.top_k,
        )
        print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))


def _split(values):
    out = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def _emit(data, out_dir=None, stem=None, markdown=None):
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{stem}.json").write_text(text, encoding="utf-8")
        if markdown is not None:
            (out / f"{stem}.md").write_text(markdown, encoding="utf-8")
    try:
        print(text)
    except UnicodeEncodeError:  # legacy Windows console code page
        print(text.encode("ascii", "backslashreplace").decode("ascii"))


def _add_batch_commands(subparsers):
    p = subparsers.add_parser("census", help="Walk drawing folders; hash, version and dedupe every file")
    p.add_argument("roots", nargs="+", help="Root folder(s), e.g. 'G:\\내 드라이브\\프로젝트'")
    p.add_argument("--root", action="append", default=[], help="Additional root (repeatable)")
    p.add_argument("--out", required=True, help="Output folder for census.jsonl/csv and summary")
    p.add_argument("--extensions", action="append", default=None,
                   help="Comma separated, default .dwg,.dxf,.ifc,.pdf")
    p.add_argument("--resume", action="store_true", help="Keep rows of unchanged files from an earlier run")
    p.add_argument("--flush-every", type=int, default=100)
    p.add_argument("--only-folder", action="append", default=[],
                   help="Scan only <root>/<folder> (repeatable); project grouping stays relative to the root")
    p.add_argument("--skip-placeholders", action="store_true",
                   help="Do not hash cloud-only placeholder files (avoids downloading them)")

    p = subparsers.add_parser("enqueue-census", help="Enqueue one ingestion job per unique file in a census")
    p.add_argument("census", help="census.jsonl or the census output folder")
    p.add_argument("--queue", default="cad")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--only-folder", action="append", default=[], help="Top-level folder name (repeatable)")
    p.add_argument("--extensions", action="append", default=None)
    p.add_argument("--project-prefix", default="P-")
    p.add_argument("--default-discipline", default="ARCH")
    p.add_argument("--requeue-failed", action="store_true", help="Put FAILED jobs of these files back in the queue")
    p.add_argument("--ignore-import-roots", action="store_true",
                   help="Do not require sources to be under AEC_IMPORT_ROOTS")
    p.add_argument("--dry-run", action="store_true")

    p = subparsers.add_parser("run-workers", help="Run N worker processes until the queue is drained")
    p.add_argument("-n", "--processes", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    p.add_argument("--queue", default="cad")
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--out", default=None, help="Also write run-summary.json here")

    p = subparsers.add_parser("report", help="Per-project object counts, failed jobs, census coverage")
    p.add_argument("--out", required=True)
    p.add_argument("--census", default=None, help="census.jsonl (or folder) to compare against")
    p.add_argument("--project", action="append", default=[], help="Limit to project id (repeatable)")
    p.add_argument("--project-prefix", default="P-")

    p = subparsers.add_parser("backup", help="pg_dump -Fc the aec database into a folder and rotate")
    p.add_argument("--target", required=True, help="Backups folder, e.g. 'G:\\내 드라이브\\AEC-INTELLIGENCE\\backups'")
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--prefix", default="aec-db-")
    p.add_argument("--docker-container", default=None, help="Run pg_dump inside this container (e.g. aec-db)")
    p.add_argument("--pg-dump", default=None, help="Explicit pg_dump executable")

    p = subparsers.add_parser("restore", help="pg_restore a dump into AEC_DATABASE_URL (destructive)")
    p.add_argument("dump")
    p.add_argument("--yes", action="store_true", help="Confirm overwriting database objects")
    p.add_argument("--database-url", default=None, help="Target database (default AEC_DATABASE_URL)")
    p.add_argument("--docker-container", default=None)
    p.add_argument("--pg-restore", default=None)
    p.add_argument("--no-clean", action="store_true", help="Do not drop existing objects first")


def _cmd_census(parsed, settings, db):
    from .census import DEFAULT_EXTENSIONS, run_census

    def progress(count, path):
        print(f"  ... {count:,} files ({path})", file=sys.stderr, flush=True)

    result = run_census([*parsed.roots, *parsed.root], parsed.out,
                        _split(parsed.extensions) or DEFAULT_EXTENSIONS, resume=parsed.resume,
                        flush_every=parsed.flush_every, hash_placeholders=not parsed.skip_placeholders,
                        progress=progress, only_folders=parsed.only_folder)
    summary = {k: v for k, v in result.summary.items() if k not in ("duplicates", "walk_errors")}
    summary["duplicate_groups"] = result.summary["duplicates"]["groups"]
    summary["outputs"] = [str(result.out / n) for n in ("census.jsonl", "census.csv", "summary.json", "summary.md")]
    _emit(summary)


def _cmd_enqueue(parsed, settings, db):
    from .census import enqueue_census

    result = enqueue_census(db, parsed.census, queue=parsed.queue, limit=parsed.limit,
                            only_folders=parsed.only_folder, extensions=_split(parsed.extensions) or None,
                            import_roots=None if parsed.ignore_import_roots else settings.import_roots,
                            requeue_failed=parsed.requeue_failed, prefix=parsed.project_prefix,
                            default_discipline=parsed.default_discipline, dry_run=parsed.dry_run)
    if result.get("outside_import_roots"):
        print("WARNING: some files are outside AEC_IMPORT_ROOTS and were skipped "
              f"({result['outside_import_roots']}). Add the drive root to AEC_IMPORT_ROOTS.", file=sys.stderr)
    _emit(result)


def _cmd_run_workers(parsed, settings, db):
    from .census import run_workers

    summary = run_workers(settings, processes=parsed.processes, queue=parsed.queue, poll=parsed.poll)
    _emit(summary, parsed.out, "run-summary" if parsed.out else None)
    return summary


def _cmd_report(parsed, settings, db):
    from .census import build_report, report_markdown

    report = build_report(db, parsed.census, parsed.project, parsed.project_prefix)
    _emit({k: report[k] for k in ("generated_at", "documents_ingested", "jobs") if k in report}
          | {"census_unique_files": report.get("census_unique_files"), "projects": len(report["projects"]),
             "failed_jobs": len(report["failed_jobs"])})
    out = Path(parsed.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(report_markdown(report), encoding="utf-8")


def _cmd_backup(parsed, settings, db):
    from .backup import run_backup

    _emit(run_backup(settings.dsn, parsed.target, keep=parsed.keep, prefix=parsed.prefix,
                     docker_container=parsed.docker_container, pg_dump=parsed.pg_dump))


def _cmd_restore(parsed, settings, db):
    from .backup import run_restore

    if not parsed.yes:
        print("Refusing to restore without --yes (this drops and recreates database objects).", file=sys.stderr)
        sys.exit(2)
    _emit(run_restore(parsed.database_url or settings.dsn, parsed.dump, yes=True,
                      docker_container=parsed.docker_container, pg_restore=parsed.pg_restore,
                      clean=not parsed.no_clean))


BATCH_COMMANDS = {
    "census": _cmd_census, "enqueue-census": _cmd_enqueue, "run-workers": _cmd_run_workers,
    "report": _cmd_report, "backup": _cmd_backup, "restore": _cmd_restore,
}


if __name__ == "__main__":
    main()

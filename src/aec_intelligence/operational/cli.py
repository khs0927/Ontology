"""CLI subcommands for operational AEC PostgreSQL + AGE pipeline."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .config import Settings
from .db import Database
from .parsers import SUPPORTED
from .search import SearchRouter
from .worker import IngestionWorker


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

    parsed = parser.parse_args(args)
    settings = Settings.from_env()
    db = Database(settings.dsn)

    if parsed.subcommand == "init-db":
        print(f"Initializing database at {settings.dsn}...")
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


if __name__ == "__main__":
    main()

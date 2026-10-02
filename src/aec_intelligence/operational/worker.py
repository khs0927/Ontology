"""Stateless background worker for CAD, PDF, and IFC ingestion jobs with lease management."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .embeddings import index_snapshot_embeddings
from .parsers import parse_source
from .census import _fs as long_path


logger = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(self, db: Database, settings: Settings, queue: str = "cad", worker_id: str | None = None):
        self.db = db
        self.settings = settings
        self.queue = queue
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> bool:
        """Attempts to claim and process a single queued job. Returns True if a job was processed."""
        job = self.db.claim(
            owner=self.worker_id,
            lease_seconds=self.settings.lease_seconds,
            max_attempts=self.settings.max_attempts,
            queue=self.queue,
        )
        if not job:
            return False

        job_id = job["id"]
        payload = job.get("payload") or {}
        logger.info(f"Worker {self.worker_id} claimed job {job_id} on queue {self.queue}")

        heartbeat_stop = threading.Event()

        def _heartbeat_loop():
            interval = max(self.settings.lease_seconds // 3, 5)
            while not heartbeat_stop.wait(interval):
                alive = self.db.heartbeat(job_id, self.worker_id, self.settings.lease_seconds)
                if not alive:
                    break

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        try:
            result = self.process_job(job)
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            self.db.finish(job_id, self.worker_id, result=result, error=None)
            logger.info(f"Job {job_id} succeeded")
            return True
        except Exception as exc:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception(f"Job {job_id} failed: {error_msg}")
            self.db.finish(job_id, self.worker_id, result=None, error=error_msg)
            return True

    def run_forever(self, poll_interval: float = 2.0) -> None:
        """Continuously polls for and executes jobs until stop() is called."""
        logger.info(f"Starting worker loop for {self.worker_id} on queue {self.queue}")
        while not self._stop_event.is_set():
            processed = self.run_once()
            if not processed:
                time.sleep(poll_interval)

    def process_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        source_path_str = payload.get("source")
        if not source_path_str:
            raise ValueError("Job payload missing 'source' file path")

        # Long-path prefix on Windows: census hashes >260-char paths with it, so the worker must too.
        source_path = Path(long_path(str(Path(source_path_str).resolve())))
        if not source_path.is_file():
            raise FileNotFoundError(f"Source file does not exist: {source_path}")

        project_id = str(payload.get("project_id", "default_project"))
        doc_id = str(payload.get("document_id") or f"doc_{source_path.stem}")
        doc_name = str(payload.get("name") or source_path.name)
        revision = int(payload.get("revision", 0))

        # Output artifact and preview directory
        output_dir = self.settings.data_root / "artifacts" / doc_id / f"rev-{revision}"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Parse CAD / PDF / IFC / Raster
        parsed = parse_source(
            source=source_path,
            doc=doc_id,
            output=output_dir,
            settings=self.settings,
            source_name=doc_name,
        )

        # Build snapshot dictionary
        source_hash = ""
        try:
            import hashlib
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except Exception:
            source_hash = "unknown"

        snapshot = {
            "document_id": doc_id,
            "project_id": project_id,
            "source_key": str(source_path),
            "name": doc_name,
            "revision": revision,
            "source_hash": source_hash,
            "objects": parsed.get("objects", []),
            "relations": parsed.get("relations", []),
            "units": parsed.get("units", "unknown"),
            "discipline": payload.get("discipline", ""),
            "warnings": parsed.get("warnings", []),
            "metrics": parsed.get("metrics", {}),
        }

        # Write immutable snapshot JSON file
        snapshot_dir = self.settings.data_root / "snapshots" / doc_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = snapshot_dir / f"rev-{revision}.json"
        snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        relative_snapshot_path = str(snapshot_file.relative_to(self.settings.data_root))

        # Project to PostgreSQL tables & Apache AGE graph
        with self.db.connect() as conn:
            self.db.project(conn, snapshot, relative_snapshot_path)
            # Index pgvector embeddings
            indexed_embeddings = index_snapshot_embeddings(conn, snapshot, self.settings)

            # Record ingestion metrics
            metrics_payload = {
                "job_id": str(job["id"]),
                "document_id": doc_id,
                "project_id": project_id,
                "objects_count": len(snapshot["objects"]),
                "relations_count": len(snapshot["relations"]),
                "embeddings_count": indexed_embeddings,
                "raw_metrics": parsed.get("metrics", {}),
            }
            conn.execute(
                "INSERT INTO aec.metrics(kind, payload) VALUES (%s, %s)",
                ("ingestion_completed", json.dumps(metrics_payload)),
            )

        return {
            "status": "SUCCESS",
            "document_id": doc_id,
            "revision": revision,
            "snapshot_path": relative_snapshot_path,
            "objects_count": len(snapshot["objects"]),
            "relations_count": len(snapshot["relations"]),
            "warnings": parsed.get("warnings", []),
        }

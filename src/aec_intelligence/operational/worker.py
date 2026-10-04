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
from .embeddings import HASH_MODEL, EmbeddingEndpointError, EmbeddingService, index_snapshot_embeddings
from .parsers import parse_source
from .census import _fs as long_path


logger = logging.getLogger(__name__)


class _OncePerMessage(logging.Filter):
    """Let each distinct message through once. ezdxf logs "no default font found" for every text
    entity of a drawing that uses a missing font (23k lines in one bulk-ingest hour)."""

    def __init__(self, limit: int = 1000):
        super().__init__()
        self.limit, self.seen = limit, set()

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, record.getMessage())
        if key in self.seen:
            return False
        if len(self.seen) < self.limit:
            self.seen.add(key)
        return True


def quiet_noisy_loggers() -> None:
    """Deduplicate third-party per-entity warnings (idempotent)."""
    for name in ("ezdxf",):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _OncePerMessage) for f in lg.filters):
            lg.addFilter(_OncePerMessage())


def source_sha256(source_path: Path, payload: dict[str, Any] | None = None, chunk_size: int = 1 << 20) -> str:
    """Return the source hash, preferring the census-provided ``sha256`` in the job payload.

    Falls back to streaming the file in chunks so large CAD files are never loaded whole.
    """
    payload = payload or {}
    provided = payload.get("sha256")
    if isinstance(provided, str) and len(provided) == 64:
        # Reuse the census hash only while the file still has the size census saw;
        # a Drive sync after census must not leave a stale hash in provenance.
        try:
            current_size = os.stat(source_path).st_size
        except OSError:
            current_size = None
        if payload.get("size") is not None and current_size == payload.get("size"):
            return provided.lower()
    import hashlib
    try:
        digest = hashlib.sha256()
        with open(source_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return "unknown"


class IngestionWorker:
    def __init__(self, db: Database, settings: Settings, queue: str = "cad", worker_id: str | None = None):
        self.db = db
        self.settings = settings
        self.queue = queue
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _renew_lease(self, job_id, retries: int = 3, retry_delay: float = 1.0) -> bool:
        """Renew one job lease, retrying transient database errors without hiding lease loss."""
        retries = max(1, int(retries))
        for attempt in range(1, retries + 1):
            try:
                alive = self.db.heartbeat(job_id, self.worker_id, self.settings.lease_seconds)
            except Exception as exc:
                if attempt >= retries:
                    logger.error(
                        "Worker %s could not renew lease for job %s after %s attempts: %s",
                        self.worker_id, job_id, retries, exc,
                    )
                    return False
                logger.warning(
                    "Worker %s heartbeat error for job %s (attempt %s/%s): %s",
                    self.worker_id, job_id, attempt, retries, exc,
                )
                time.sleep(max(0.0, retry_delay))
                continue
            if not alive:
                logger.error("Worker %s lost lease for job %s", self.worker_id, job_id)
                return False
            return True
        return False

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
        logger.info(f"Worker {self.worker_id} claimed job {job_id} on queue {self.queue}")

        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def _heartbeat_loop():
            interval = max(self.settings.lease_seconds // 3, 5)
            while not heartbeat_stop.wait(interval):
                if not self._renew_lease(job_id):
                    lease_lost.set()
                    break

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        try:
            result = self.process_job(job)
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            if lease_lost.is_set():
                logger.error(
                    "Job %s completed processing after its lease was lost; leaving it for safe retry",
                    job_id,
                )
                return True
            finalized = self.db.finish(job_id, self.worker_id, result=result, error=None)
            if not finalized:
                logger.error(
                    "Job %s completed processing but could not finalize its lease; leaving it for safe retry",
                    job_id,
                )
                return True
            logger.info(f"Job {job_id} succeeded")
            return True
        except Exception as exc:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception(f"Job {job_id} failed: {error_msg}")
            marked_failed = self.db.finish(job_id, self.worker_id, result=None, error=error_msg)
            if not marked_failed:
                logger.error("Job %s failure could not be finalized because its lease is no longer owned", job_id)
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
        # source_key keeps the plain resolved path; only OS access uses the prefixed one.
        resolved_path = Path(source_path_str).resolve()
        source_path = Path(long_path(str(resolved_path)))
        if not source_path.is_file():
            raise FileNotFoundError(f"Source file does not exist: {source_path}")

        project_id = str(payload.get("project_id", "default_project"))
        doc_id = str(payload.get("document_id") or f"doc_{resolved_path.stem}")
        doc_name = str(payload.get("name") or resolved_path.name)
        revision = int(payload.get("revision", 0))

        # Output artifact and preview directory
        output_dir = self.settings.data_root / "artifacts" / doc_id / f"rev-{revision}"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Hash first: the DWG->DXF cache is keyed by it, so a retry or a copy of the same drawing in
        # another folder reuses the conversion instead of running ODA again.
        source_hash = source_sha256(source_path, payload)

        # Parse CAD / PDF / IFC / Raster
        parsed = parse_source(
            source=source_path,
            doc=doc_id,
            output=output_dir,
            settings=self.settings,
            source_name=doc_name,
            source_hash=source_hash if source_hash != "unknown" else None,
        )

        snapshot = {
            "document_id": doc_id,
            "project_id": project_id,
            "source_key": str(resolved_path),
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
        with self.db.connect(
            statement_timeout_seconds=self.settings.ingest_statement_timeout_seconds
        ) as conn:
            self.db.project(conn, snapshot, relative_snapshot_path)
            # Index pgvector embeddings. A configured endpoint that fails never writes placeholder
            # vectors. By default the objects are kept and their vectors stay pending: they are exactly
            # the rows `python -m aec_intelligence.operational.cli reembed` selects (no vector of the active model). The drawing is
            # searchable lexically and through the graph meanwhile. AEC_EMBEDDING_STRICT=1 fails the job.
            embedder = EmbeddingService(self.settings)
            embedding_error = None
            try:
                with conn.transaction():
                    indexed_embeddings = index_snapshot_embeddings(conn, snapshot, self.settings, service=embedder)
            except EmbeddingEndpointError as exc:
                if self.settings.embedding_strict:
                    raise
                indexed_embeddings = 0
                embedding_error = str(exc)
                logger.warning("Job %s: embeddings pending (%s); run `python -m aec_intelligence.operational.cli reembed` once the "
                               "endpoint is back", job["id"], exc)
                snapshot["warnings"].append(f"embeddings pending: {exc}")

            # Record ingestion metrics
            metrics_payload = {
                "job_id": str(job["id"]),
                "document_id": doc_id,
                "project_id": project_id,
                "objects_count": len(snapshot["objects"]),
                "relations_count": len(snapshot["relations"]),
                "embeddings_count": indexed_embeddings,
                "embedding_model": embedder.active_model(),
                "embeddings_degraded": embedder.active_model() == HASH_MODEL,
                "embeddings_pending": embedding_error is not None,
                "embedding_error": embedding_error,
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
            "embedding_model": embedder.active_model(),
            "embeddings_degraded": embedder.active_model() == HASH_MODEL,
            "embeddings_pending": embedding_error is not None,
            "embedding_error": embedding_error,
            "warnings": snapshot["warnings"],
        }

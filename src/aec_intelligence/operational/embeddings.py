"""1,024-dimensional embedding generator and pgvector indexer for AEC objects.

Vectors are only comparable when the same model produced them. Every vector is
therefore stored with the name of the model that actually produced it:

* the configured remote model (``AEC_EMBEDDING_MODEL``, default ``BAAI/bge-m3``)
  when ``AEC_EMBEDDING_URL`` answered — e.g. the ``embeddings`` compose service
  running Hugging Face text-embeddings-inference (OpenAI-compatible
  ``/v1/embeddings``; the native ``/embed`` route is accepted too);
* ``HASH_MODEL`` for the deterministic offline fallback, produced **only** when
  ``AEC_EMBEDDING_URL`` is unset (offline mode).

A configured endpoint never degrades silently. If the remote fails after the
retries, or answers with a payload that cannot be stored as a real vector (wrong
dimension, wrong count, non-finite component, zero vector), ``embed_with_model``
raises ``EmbeddingEndpointError`` and writes nothing: a placeholder must never be
stored as if a model had produced it. Offline hash vectors are labelled
``HASH_MODEL``, are never ranked against real vectors, and every consumer
surfaces the model that was actually used (see ``worker.py``, ``search.py``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .netguard import is_local_endpoint, opener_for, remote_allowed


EMBEDDING_DIM = 1024
HASH_MODEL = "hash-sha256-1024-v1"
DEFAULT_BATCH_SIZE = 32
DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
CIRCUIT_COOLDOWN_SECONDS = 30.0

# endpoint -> monotonic time until which the remote is considered down
_CIRCUIT: dict[str, float] = {}
_CIRCUIT_LOCK = threading.Lock()


class EmbeddingEndpointError(RuntimeError):
    """The remote embedding service did not return usable vectors."""


def _deterministic_hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Fallback deterministic unit vector generated from text token hashes.

    Ensures full offline operation, test repeatability, and graceful degradation
    when remote embedding endpoints are not configured. Stored under HASH_MODEL.
    """
    vec = [0.0] * dim
    tokens = text.lower().split()
    if not tokens:
        tokens = [text.lower()]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i in range(0, min(len(digest), 32), 2):
            idx = int.from_bytes(digest[i : i + 2], "big") % dim
            val = ((digest[i] - 128) / 128.0)
            vec[idx] += val

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 1e-9:
        vec = [round(x / norm, 6) for x in vec]
    else:
        vec[0] = 1.0
    return vec


def _validate_remote_vector(vec: list[Any]) -> list[float]:
    """Coerce one remote vector to float and reject what a vector column cannot hold.

    Magnitude is left alone: ``aec.embeddings`` is indexed with
    ``vector_cosine_ops`` and every query uses the cosine operator, so a
    non-unit vector is comparable and rescaling it would be a silent transform.
    A zero vector and any non-finite component are rejected, because cosine
    distance against them is undefined and the row would poison the index.
    """
    values = [float(v) for v in vec]
    if not all(math.isfinite(v) for v in values):
        raise EmbeddingEndpointError("model returned a non-finite component (NaN/Inf)")
    if math.sqrt(sum(v * v for v in values)) <= 1e-9:
        raise EmbeddingEndpointError("model returned a zero vector; cosine distance is undefined")
    return values


def _env_number(name: str, default: float, cast=float):
    try:
        return cast(os.getenv(name, "") or default)
    except ValueError:
        return default


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(float(v)) for v in vec) + "]"


def _urlopen(req: urllib.request.Request, timeout: float):
    """Proxy-less for local endpoints (netguard.opener_for); a seam for transport tests."""
    return opener_for(req.full_url).open(req, timeout=timeout)


class EmbeddingService:
    def __init__(self, settings: Settings, *, batch_size: int | None = None, timeout: float | None = None,
                 retries: int | None = None, backoff: float = 0.5):
        self.settings = settings
        self.model_name = settings.embedding_model or "BAAI/bge-m3"
        self.endpoint = (settings.embedding_url or "").rstrip("/")
        self.batch_size = max(1, int(batch_size or _env_number("AEC_EMBEDDING_BATCH_SIZE", DEFAULT_BATCH_SIZE, int)))
        self.timeout = float(timeout or _env_number("AEC_EMBEDDING_TIMEOUT", DEFAULT_TIMEOUT))
        self.retries = max(1, int(retries or _env_number("AEC_EMBEDDING_RETRIES", DEFAULT_RETRIES, int)))
        self.backoff = backoff
        self.last_error: str | None = None

    # -- model identity -------------------------------------------------
    @property
    def remote_configured(self) -> bool:
        return bool(self.endpoint)

    def active_model(self) -> str:
        """Model name new vectors are expected to carry (the remote model when one is configured)."""
        return self.model_name if self.endpoint else HASH_MODEL

    # -- public API -----------------------------------------------------
    def embed_text(self, text: str) -> list[float]:
        return self.embed_with_model([text])[1][0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return self.embed_with_model(texts)[1]

    def embed_with_model(self, texts: list[str]) -> tuple[str, list[list[float]]]:
        """Embed all texts with ONE model and return (model_name, vectors).

        Offline (``AEC_EMBEDDING_URL`` unset) returns deterministic hash vectors
        labelled ``HASH_MODEL``. With an endpoint configured, a failure or an
        unusable payload raises ``EmbeddingEndpointError`` instead: relabelling
        the batch to the hash model would store a placeholder as if a real model
        had produced it, and the caller could not tell the two apart.
        """
        if not texts:
            return self.active_model(), []
        if not self.endpoint:
            return HASH_MODEL, [_deterministic_hash_vector(t, EMBEDDING_DIM) for t in texts]
        if self._circuit_open():
            self.last_error = "embedding endpoint circuit open after recent failures"
            raise EmbeddingEndpointError(f"{self.endpoint}: {self.last_error}; no vectors were written")
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), self.batch_size):
                vectors.extend(self._call_with_retries(texts[start:start + self.batch_size]))
        except EmbeddingEndpointError as exc:
            self.last_error = str(exc)
            self._trip_circuit()
            raise
        return self.model_name, vectors

    # -- remote ---------------------------------------------------------
    def _circuit_open(self) -> bool:
        with _CIRCUIT_LOCK:
            return _CIRCUIT.get(self.endpoint, 0.0) > time.monotonic()

    def _trip_circuit(self) -> None:
        with _CIRCUIT_LOCK:
            _CIRCUIT[self.endpoint] = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS

    def _call_with_retries(self, texts: list[str]) -> list[list[float]]:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return self._call_remote_endpoint(texts)
            except urllib.error.HTTPError as exc:
                last = exc
                if 400 <= exc.code < 500 and exc.code not in (408, 413, 429):
                    break  # a malformed request will not succeed on retry
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, EmbeddingEndpointError) as exc:
                last = exc
            if attempt + 1 < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise EmbeddingEndpointError(f"{self.endpoint}: {type(last).__name__}: {last}")

    def _call_remote_endpoint(self, texts: list[str]) -> list[list[float]]:
        native = self.endpoint.endswith("/embed")
        if native:
            url, body = self.endpoint, {"inputs": texts, "truncate": True}
        else:
            url = self.endpoint if self.endpoint.endswith("/embeddings") else f"{self.endpoint}/v1/embeddings"
            body = {"input": texts, "model": self.model_name}
        if not is_local_endpoint(url) and not remote_allowed("AEC_EMBEDDING_ALLOW_REMOTE"):
            # Object text from private drawings must not leave the PC by a typo in AEC_EMBEDDING_URL.
            raise EmbeddingEndpointError(f"refusing non-local embedding endpoint {urlparse(url).hostname!r} "
                                         "(set AEC_EMBEDDING_ALLOW_REMOTE=1 to opt in)")
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with _urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):  # TEI native /embed
            vectors = data
        else:
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            vectors = [item["embedding"] for item in items]
        if len(vectors) != len(texts):
            raise EmbeddingEndpointError(f"expected {len(texts)} vectors, got {len(vectors)}")
        for vec in vectors:
            if len(vec) != EMBEDDING_DIM:
                raise EmbeddingEndpointError(f"model returned {len(vec)} dimensions; aec.embeddings requires {EMBEDDING_DIM}")
        return [_validate_remote_vector(vec) for vec in vectors]


def _embeddable(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [obj for obj in objects if obj.get("search_text") and obj.get("type") not in ("CADEntity",)]


def index_snapshot_embeddings(conn, snapshot: dict[str, Any], settings: Settings,
                              service: EmbeddingService | None = None) -> int:
    """Computes and stores pgvector embeddings for the meaningful objects of a snapshot."""
    doc_id = snapshot["document_id"]
    rev = snapshot["revision"]
    service = service or EmbeddingService(settings)

    objects = _embeddable(snapshot.get("objects", []))
    if not objects:
        return 0

    models: set[str] = set()
    count = 0
    # Chunk per DB round to bound request size. A remote failure propagates and fails the job;
    # chunks are never relabelled to the hash model mid-run.
    chunk = max(service.batch_size * 8, 1)
    for start in range(0, len(objects), chunk):
        part = objects[start:start + chunk]
        model, vectors = service.embed_with_model([obj["search_text"] for obj in part])
        models.add(model)
        rows = []
        for obj, vec in zip(part, vectors):
            content_hash = hashlib.sha256(obj["search_text"].encode("utf-8")).hexdigest()
            rows.append((obj["id"], model, rev, content_hash, vector_literal(vec)))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO aec.embeddings(object_id, model, revision, content_hash, embedding)
                   VALUES (%s, %s, %s, %s, %s::vector)
                   ON CONFLICT(object_id, model) DO UPDATE
                   SET revision = EXCLUDED.revision,
                       content_hash = EXCLUDED.content_hash,
                       embedding = EXCLUDED.embedding""",
                rows,
            )
        count += len(rows)

    conn.execute(
        """UPDATE aec.index_state
           SET embedding_revision = %s, embedding_model = %s
           WHERE document_id = %s""",
        (rev, ",".join(sorted(models)), doc_id),
    )
    return count


_STALE_SCOPE = """FROM aec.embeddings e JOIN aec.objects o ON o.id = e.object_id
               WHERE (%(p)s::text IS NULL OR o.project_id = %(p)s) AND e.model <> %(m)s
                 AND EXISTS (SELECT 1 FROM aec.embeddings t WHERE t.object_id = e.object_id AND t.model = %(m)s)"""


def reindex_embeddings(db, settings: Settings, project_id: str | None = None, *, batch_size: int | None = None,
                       dry_run: bool = False, delete_stale: bool = False,
                       progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Re-embed objects that lack a vector from the active model (e.g. after a hash fallback or a model change).

    Superseded hash-fallback vectors are removed only once the active model's vectors
    are written, so a failed run leaves the previous state intact. With ``delete_stale``
    rows of any other model are dropped, but only for objects that already have a vector
    of the active model. ``dry_run`` only counts what would happen. Every chunk is committed
    on its own, so progress survives an interruption and is visible to searches immediately;
    ``progress(written, pending)`` is called after each chunk.
    """
    service = EmbeddingService(settings, batch_size=batch_size)
    target = service.active_model()
    written, skipped, deleted = 0, 0, 0
    params = {"p": project_id, "m": target}
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT o.id, o.revision, o.kind AS type, o.search_text FROM aec.objects o
               WHERE (%(p)s::text IS NULL OR o.project_id = %(p)s) AND o.kind <> 'CADEntity' AND o.search_text <> ''
                 AND NOT EXISTS (SELECT 1 FROM aec.embeddings e WHERE e.object_id = o.id AND e.model = %(m)s)
               ORDER BY o.id""",
            params,
        ).fetchall()
        if dry_run:
            stale = conn.execute("SELECT e.model, count(*) AS n " + _STALE_SCOPE + " GROUP BY e.model", params).fetchall()
            return {"model": target, "dry_run": True, "pending": len(rows),
                    "stale_by_model": {r["model"]: r["n"] for r in stale}, "written": 0, "skipped": 0, "deleted": 0,
                    "error": None}
        step = service.batch_size * 8
        for start in range(0, len(rows), step):
            part = rows[start:start + step]
            model, vectors = service.embed_with_model([r["search_text"] for r in part])
            if model != target:
                skipped += len(rows) - start
                break
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO aec.embeddings(object_id, model, revision, content_hash, embedding)
                       VALUES (%s,%s,%s,%s,%s::vector) ON CONFLICT(object_id, model) DO UPDATE
                       SET revision=EXCLUDED.revision, content_hash=EXCLUDED.content_hash, embedding=EXCLUDED.embedding""",
                    [(r["id"], model, r["revision"], hashlib.sha256(r["search_text"].encode("utf-8")).hexdigest(),
                      vector_literal(v)) for r, v in zip(part, vectors)],
                )
                if model != HASH_MODEL:  # drop superseded offline vectors only; never discard real ones
                    cur.execute("DELETE FROM aec.embeddings WHERE object_id = ANY(%s) AND model = %s",
                                ([r["id"] for r in part], HASH_MODEL))
            conn.commit()  # each chunk is durable: an interrupted run resumes where it stopped
            written += len(part)
            if progress is not None:
                progress(written, len(rows))
        if delete_stale:
            deleted = conn.execute(
                "DELETE FROM aec.embeddings d USING (SELECT e.object_id, e.model " + _STALE_SCOPE + ") s "
                "WHERE d.object_id = s.object_id AND d.model = s.model", params).rowcount
    return {"model": target, "dry_run": False, "pending": len(rows), "written": written, "skipped": skipped,
            "deleted": deleted, "error": service.last_error}

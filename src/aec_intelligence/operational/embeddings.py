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
import struct
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
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
# An ingest job waits at most this long per chunk for interactive queries (it holds its transaction).
INGEST_MAX_YIELD_SECONDS = 30.0

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


def _halfvec_roundtrip(values: list[float]) -> list[float]:
    """The values as they read back from the fp16 (halfvec) storage column."""
    halves: list[float] = []
    for value in values:
        try:
            halves.append(struct.unpack("<e", struct.pack("<e", value))[0])
        except (OverflowError, struct.error) as exc:
            raise EmbeddingEndpointError(f"component {value!r} does not fit the fp16 storage column") from exc
    return halves


def _validate_remote_vector(vec: list[Any]) -> list[float]:
    """Coerce one remote vector to float and reject what the storage column cannot hold.

    Magnitude is left alone: ``aec.embeddings`` is indexed with
    ``vector_cosine_ops`` and every query uses the cosine operator, so a
    non-unit vector is comparable and rescaling it would be a silent transform.
    A zero vector and any non-finite component are rejected, because cosine
    distance against them is undefined and the row would poison the index.

    The check runs on the fp16 round trip as well, because ``aec.text_vectors.embedding``
    is a ``halfvec``: a vector small enough to round to zeros there would be stored as
    exactly the zero vector this function exists to refuse.
    """
    values = [float(v) for v in vec]
    if not all(math.isfinite(v) for v in values):
        raise EmbeddingEndpointError("model returned a non-finite component (NaN/Inf)")
    if math.sqrt(sum(v * v for v in values)) <= 1e-9:
        raise EmbeddingEndpointError("model returned a zero vector; cosine distance is undefined")
    halves = _halfvec_roundtrip(values)
    if not all(math.isfinite(h) for h in halves):
        raise EmbeddingEndpointError("model returned a component that overflows fp16 storage")
    if math.sqrt(sum(h * h for h in halves)) <= 1e-9:
        raise EmbeddingEndpointError("vector rounds to zero in fp16 storage; cosine distance is undefined")
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
    def circuit_open(self) -> bool:
        """True while this endpoint is short-circuited after recent failures (process-global)."""
        return self._circuit_open()

    def _circuit_open(self) -> bool:
        with _CIRCUIT_LOCK:
            return _CIRCUIT.get(self.endpoint, 0.0) > time.monotonic()

    def reset_circuit(self) -> None:
        """Forget recent failures of this endpoint (a caller that backs off itself retries at once)."""
        with _CIRCUIT_LOCK:
            _CIRCUIT.pop(self.endpoint, None)
        self.last_error = None

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


def endpoint_health(settings: Settings) -> dict[str, Any]:
    """Read-only embedding readiness for health endpoints: no network call, no database access.

    A degraded semantic stage used to be invisible from outside: ``/healthz`` answered ``ok`` while
    every query silently fell back to the lexical stage (or to offline hash vectors).
    """
    service = EmbeddingService(settings)
    circuit = service.circuit_open()
    return {
        "configured": service.remote_configured,
        "model": service.active_model(),
        "circuit_open": circuit,
        "degraded": (not service.remote_configured) or circuit,
    }


def text_hash(text: str) -> str:
    """Key of a text vector: sha256 of the exact text that was embedded (aec.embeddings.content_hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Storage layout (migration 0003): one halfvec per distinct (model, text) in aec.text_vectors; aec.embeddings
# maps each object to the text vector it uses. Identical texts (CAD labels repeat on every sheet) are
# embedded and stored once; fp16 keeps bge-m3 cosine ranking while halving the bytes.
def known_text_hashes(conn, model: str, hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    rows = conn.execute("SELECT content_hash FROM aec.text_vectors WHERE model = %s AND content_hash = ANY(%s)",
                        (model, list(hashes))).fetchall()
    return {r["content_hash"] if isinstance(r, dict) else r[0] for r in rows}


def write_text_vectors(conn, model: str, vectors: dict[str, list[float]]) -> None:
    if not vectors:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO aec.text_vectors(model, content_hash, embedding) VALUES (%s, %s, %s::halfvec)
               ON CONFLICT (model, content_hash) DO NOTHING""",
            [(model, h, vector_literal(v)) for h, v in vectors.items()],
        )


def write_mappings(conn, model: str, rows: list[tuple[str, int, str]]) -> None:
    """rows: (object_id, revision, content_hash); the text vectors must already exist."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO aec.embeddings(object_id, model, revision, content_hash) VALUES (%s, %s, %s, %s)
               ON CONFLICT (object_id, model) DO UPDATE
               SET revision = EXCLUDED.revision, content_hash = EXCLUDED.content_hash""",
            [(oid, model, rev, h) for oid, rev, h in rows],
        )


def embed_missing_texts(conn, service: "EmbeddingService", texts: dict[str, str], vector_conn=None) -> tuple[str, int]:
    """Embed only the texts (hash -> text) that have no vector of the active model yet; returns (model, n_embedded).

    A remote failure raises EmbeddingEndpointError before anything is written for these texts. Vectors
    go through ``vector_conn`` when given (an autocommit connection: content-addressed rows are safe to
    commit early, and two workers holding uncommitted inserts of the same texts in different orders
    would otherwise deadlock), else through ``conn``.
    """
    model = service.active_model()
    known = known_text_hashes(conn, model, sorted(texts))
    missing = {h: texts[h] for h in sorted(texts) if h not in known}
    if not missing:
        return model, 0
    hashes = list(missing)
    model, vectors = service.embed_with_model([missing[h] for h in hashes])
    write_text_vectors(vector_conn if vector_conn is not None else conn, model, dict(zip(hashes, vectors)))
    return model, len(hashes)


@contextmanager
def _autocommit_connection(settings: Settings):
    """A short-lived autocommit connection to the same database (None when no DSN is configured)."""
    dsn = getattr(settings, "dsn", "") or ""
    if not dsn:
        yield None
        return
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10) as side:
        yield side


def index_snapshot_embeddings(conn, snapshot: dict[str, Any], settings: Settings,
                              service: EmbeddingService | None = None, gate=None) -> int:
    """Stores the text vectors (deduplicated) and object mappings for the meaningful objects of a snapshot.

    Between chunks the ingest yields to interactive queries (``gate``, default from the environment;
    see priority.py); the per-call cap keeps a busy API from stalling the job beyond its lease."""
    from .priority import InteractiveGate

    gate = gate if gate is not None else InteractiveGate.from_env(max_wait_cap=INGEST_MAX_YIELD_SECONDS)
    doc_id = snapshot["document_id"]
    rev = snapshot["revision"]
    service = service or EmbeddingService(settings)

    objects = _embeddable(snapshot.get("objects", []))
    if not objects:
        return 0

    models: set[str] = set()
    count = 0
    # Chunk per DB round to bound request size. A remote failure propagates (the worker stores the
    # objects as pending); chunks are never relabelled to the hash model mid-run.
    chunk = max(service.batch_size * 8, 1)
    with _autocommit_connection(settings) as side:
        for start in range(0, len(objects), chunk):
            part = objects[start:start + chunk]
            gate.wait_turn(conn)
            hashes = [text_hash(obj["search_text"]) for obj in part]
            model, _ = embed_missing_texts(conn, service, dict(zip(hashes, (obj["search_text"] for obj in part))),
                                           vector_conn=side)
            models.add(model)
            write_mappings(conn, model, sorted((obj["id"], rev, h) for obj, h in zip(part, hashes)))
            count += len(part)

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


def _refresh_embedding_state(conn, model: str, *, document_ids: list[str] | None = None,
                             project_id: str | None = None) -> None:
    """Advance metadata only after every searchable object has a vector of its current revision.

    Chunk commits can complete one document before another. Reconcile that complete document without
    advertising a partly embedded document as ready; also repair metadata when a resume finds no work.
    """
    conn.execute(
        """UPDATE aec.index_state s SET embedding_revision=d.revision, embedding_model=%(m)s
           FROM aec.documents d WHERE s.document_id=d.id
             AND (%(docs)s::text[] IS NULL OR d.id=ANY(%(docs)s))
             AND (%(p)s::text IS NULL OR d.project_id=%(p)s)
             AND (s.embedding_model IS DISTINCT FROM %(m)s OR s.embedding_revision <> d.revision)
             AND EXISTS (SELECT 1 FROM aec.objects o WHERE o.document_id=d.id
                         AND o.kind <> 'CADEntity' AND o.search_text <> '')
             AND NOT EXISTS (SELECT 1 FROM aec.objects o WHERE o.document_id=d.id
                 AND o.kind <> 'CADEntity' AND o.search_text <> ''
                 AND NOT EXISTS (SELECT 1 FROM aec.embeddings e WHERE e.object_id=o.id
                                 AND e.model=%(m)s AND e.revision=o.revision))""",
        {"m": model, "docs": document_ids, "p": project_id},
    )


def _retriable_db_error(exc: Exception) -> bool:
    """A statement timeout, lock timeout or deadlock: the connection survives a rollback; retry the chunk."""
    try:
        from psycopg import errors
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (errors.QueryCanceled, errors.LockNotAvailable, errors.DeadlockDetected))


def reindex_embeddings(db, settings: Settings, project_id: str | None = None, *, batch_size: int | None = None,
                       dry_run: bool = False, delete_stale: bool = False,
                       progress: Callable[[int, int], None] | None = None, chunk_retries: int = 0,
                       max_backoff: float = 300.0, pause: float = 0.0, timeout: float | None = None,
                       sleep: Callable[[float], None] = time.sleep,
                       on_retry: Callable[[int, float, str], None] | None = None,
                       gate=None, hours: tuple[int, int] | None = None,
                       clock_now: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Give every embeddable object a vector of the active model (after a hash fallback, a model change or
    an ingest that ran while the endpoint was down).

    Pending objects are grouped by text: each distinct text is embedded once (and not at all when a vector
    for it already exists), then every object with that text is mapped to it. Superseded hash-fallback
    mappings are removed only once the active model's mapping is written, so a failed run leaves the
    previous state intact. With ``delete_stale`` mappings of any other model are dropped, but only for
    objects that already have one of the active model. ``dry_run`` only counts what would happen. Every
    chunk is committed on its own, so progress survives an interruption and is visible to searches
    immediately; ``progress(written, pending)`` is called after each chunk (counted in objects).

    ``chunk_retries`` > 0 makes a long run survive a statement timeout or deadlock on the vector insert (the
    session uses the ingest statement timeout, 300 s) and a busy or restarting endpoint (Ollama shared with
    the ingest workers, a model reload, a PC under memory pressure): a failed chunk is retried after
    an exponential backoff (5 s, 10 s, ... capped at ``max_backoff``), the endpoint circuit is reset
    first, and only ``chunk_retries`` consecutive failures end the run with ``error`` set (the
    chunks written so far stay committed). ``pause`` sleeps between chunks to leave the GPU/CPU to
    interactive work. With the default 0 the first failure raises ``EmbeddingEndpointError``.

    Interactive priority (``priority.py``): before every chunk the run waits while the API served a
    query recently (``gate``, default ``InteractiveGate.from_env``), and with ``hours`` (e.g. (22, 7))
    it stops cleanly once the local time leaves that window (``stopped`` is set, nothing is lost).
    """
    from .priority import InteractiveGate, in_window

    gate = gate if gate is not None else InteractiveGate.from_env(sleep=sleep)
    stopped: str | None = None
    service = EmbeddingService(settings, batch_size=batch_size, timeout=timeout)
    target = service.active_model()
    written, skipped, deleted, retried, embedded = 0, 0, 0, 0, 0
    failure: str | None = None
    params = {"p": project_id, "m": target}
    if not dry_run and not in_window(hours, clock_now() if clock_now else None):
        # Checked before the (large) pending-object scan: a scheduled run outside the window is a no-op.
        return {"model": target, "dry_run": False, "pending": None, "distinct_texts": None, "embedded_texts": 0,
                "written": 0, "skipped": 0, "deleted": 0, "retried_chunks": 0, "complete": False, "error": None,
                "stopped": f"outside the re-embed hours {hours[0]:02d}-{hours[1]:02d}; the next run resumes",
                "yielded_seconds": 0.0, "yields": 0}
    from .db import Database

    # Vector inserts go into an HNSW index; on a slow disk (cold cache, a checkpoint, a concurrent ingest)
    # one chunk can exceed the 30 s interactive default. Batch headroom like ingest.
    batch_timeout = getattr(settings, "ingest_statement_timeout_seconds", None)
    session = (db.connect(statement_timeout_seconds=batch_timeout) if isinstance(db, Database) and batch_timeout
               else db.connect())
    with session as conn:
        rows = conn.execute(
            """SELECT o.id, o.document_id, o.revision, o.kind AS type, o.search_text FROM aec.objects o
               WHERE (%(p)s::text IS NULL OR o.project_id = %(p)s) AND o.kind <> 'CADEntity' AND o.search_text <> ''
                 AND NOT EXISTS (SELECT 1 FROM aec.embeddings e WHERE e.object_id = o.id AND e.model = %(m)s
                                 AND e.revision = o.revision)
               ORDER BY o.id""",
            params,
        ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(text_hash(r["search_text"]), []).append(r)
        if dry_run:
            stale = conn.execute("SELECT e.model, count(*) AS n " + _STALE_SCOPE + " GROUP BY e.model", params).fetchall()
            known = 0
            hashes = list(groups)
            for start in range(0, len(hashes), 5000):
                known += len(known_text_hashes(conn, target, hashes[start:start + 5000]))
            return {"model": target, "dry_run": True, "pending": len(rows), "distinct_texts": len(groups),
                    "texts_to_embed": len(groups) - known,
                    "stale_by_model": {r["model"]: r["n"] for r in stale}, "written": 0, "skipped": 0, "deleted": 0,
                    "error": None}
        hashes = list(groups)
        step = service.batch_size * 8
        for start in range(0, len(hashes), step):
            part = hashes[start:start + step]
            if not in_window(hours, clock_now() if clock_now else None):
                stopped = f"outside the re-embed hours {hours[0]:02d}-{hours[1]:02d}; the next run resumes"
                skipped += sum(len(groups[h]) for h in hashes[start:])
                break
            if start and pause > 0:
                sleep(pause)
            gate.wait_turn(conn)
            failures = 0
            while True:
                try:
                    model, n = embed_missing_texts(conn, service, {h: groups[h][0]["search_text"] for h in part})
                    break
                except Exception as exc:  # noqa: BLE001 - only endpoint errors and DB timeouts are retried
                    if not isinstance(exc, EmbeddingEndpointError) and not _retriable_db_error(exc):
                        raise
                    conn.rollback()
                    failures += 1
                    if failures > chunk_retries:
                        if chunk_retries <= 0:
                            raise
                        failure = f"stopped after {failures} failed attempts on one chunk: {exc}"
                        break
                    retried += 1
                    wait = min(max_backoff, 5.0 * (2 ** (failures - 1)))
                    if on_retry is not None:
                        on_retry(failures, wait, str(exc))
                    sleep(wait)
                    service.reset_circuit()
            if failure:
                break
            if model != target:
                skipped += sum(len(groups[h]) for h in hashes[start:])
                break
            objs = [r for h in part for r in groups[h]]
            write_mappings(conn, model, [(r["id"], r["revision"], text_hash(r["search_text"])) for r in objs])
            if model != HASH_MODEL:  # drop superseded offline mappings only; never discard real ones
                conn.execute("DELETE FROM aec.embeddings WHERE object_id = ANY(%s) AND model = %s",
                             ([r["id"] for r in objs], HASH_MODEL))
            _refresh_embedding_state(conn, model, document_ids=sorted({r["document_id"] for r in objs}))
            conn.commit()  # each chunk and its completed-document metadata are durable (resumable)
            embedded += n
            written += len(objs)
            if progress is not None:
                progress(written, len(rows))
        # The project-wide refresh also runs after a productive, fully successful pass: a document whose
        # objects were embedded by an earlier run never appears in any chunk, so waiting for a run with
        # nothing pending left it carrying a stale marker (36 of 547 documents in the live database).
        if not rows or (failure is None and not skipped and not stopped):
            _refresh_embedding_state(conn, target, project_id=project_id)
        if delete_stale:
            deleted = conn.execute(
                "DELETE FROM aec.embeddings d USING (SELECT e.object_id, e.model " + _STALE_SCOPE + ") s "
                "WHERE d.object_id = s.object_id AND d.model = s.model", params).rowcount
            conn.commit()
    return {"model": target, "dry_run": False, "pending": len(rows), "distinct_texts": len(groups),
            "embedded_texts": embedded, "written": written, "skipped": skipped,
            "deleted": deleted, "retried_chunks": retried, "complete": failure is None and not skipped,
            "error": failure or service.last_error, "stopped": stopped,
            "yielded_seconds": round(gate.waited_total, 1), "yields": gate.yields}


def vectors_gc(db, *, batch: int = 5000, max_batches: int = 1000, min_age_seconds: float = 3600,
               dry_run: bool = False) -> dict[str, Any]:
    """Delete text vectors that no object maps to any more (re-ingested or deleted drawings, replaced
    hash-fallback vectors). Vectors younger than ``min_age_seconds`` are kept: an ingest writes its vectors
    before its mappings commit. Batched; a batch that races a concurrent ingest (foreign-key violation) is
    rolled back and the run stops, to be repeated later. ``dry_run`` counts the same set without deleting
    anything, so a caller can preview the irreversible step before taking it."""
    removed, error = 0, None
    with db.connect() as conn:
        for _ in range(max_batches):
            try:
                if dry_run:
                    removed = len(conn.execute(
                        """SELECT t.model, t.content_hash FROM aec.text_vectors t
                           WHERE t.created_at < now() - make_interval(secs => %s)
                             AND NOT EXISTS (SELECT 1 FROM aec.embeddings e
                                             WHERE e.model = t.model AND e.content_hash = t.content_hash)
                           LIMIT %s""",
                        (min_age_seconds, batch)).fetchall())
                    break
                n = conn.execute(
                    """DELETE FROM aec.text_vectors tv USING (
                           SELECT t.model, t.content_hash FROM aec.text_vectors t
                           WHERE t.created_at < now() - make_interval(secs => %s)
                             AND NOT EXISTS (SELECT 1 FROM aec.embeddings e
                                             WHERE e.model = t.model AND e.content_hash = t.content_hash)
                           LIMIT %s) dead
                       WHERE tv.model = dead.model AND tv.content_hash = dead.content_hash""",
                    (min_age_seconds, batch)).rowcount
                conn.commit()
            except Exception as exc:  # psycopg.errors.ForeignKeyViolation: a mapping was added meanwhile
                conn.rollback()
                error = f"{type(exc).__name__}: {exc}"
                break
            removed += n
            if n < batch:
                break
        left = conn.execute("SELECT count(*) AS n FROM aec.text_vectors").fetchone()
    return {"removed": removed, "text_vectors": left["n"] if isinstance(left, dict) else left[0], "error": error}

"""1,024-dimensional embedding generator and pgvector indexer for AEC objects."""

from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Iterable

from .config import Settings


EMBEDDING_DIM = 1024


def _deterministic_hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Fallback deterministic unit vector generated from text token hashes.

    Ensures full offline operation, test repeatability, and graceful degradation
    when GPU or remote embedding endpoints are not configured.
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

    # Normalize to unit length
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 1e-9:
        vec = [round(x / norm, 6) for x in vec]
    else:
        vec[0] = 1.0
    return vec


class EmbeddingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_name = settings.embedding_model or "BAAI/bge-m3"
        self.endpoint = (settings.embedding_url or "").rstrip("/")

    def embed_text(self, text: str) -> list[float]:
        results = self.embed_batch([text])
        return results[0] if results else _deterministic_hash_vector(text, EMBEDDING_DIM)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        # 1. If remote endpoint configured, try calling remote service
        if self.endpoint:
            try:
                return self._call_remote_endpoint(texts)
            except Exception:
                # Fallback to local deterministic if remote is unreachable
                pass

        # 2. Try fastembed if installed
        try:
            from fastembed import TextEmbedding  # type: ignore

            model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
            embeddings = list(model.embed(texts))
            result = []
            for emb in embeddings:
                raw = list(emb)
                if len(raw) < EMBEDDING_DIM:
                    raw.extend([0.0] * (EMBEDDING_DIM - len(raw)))
                elif len(raw) > EMBEDDING_DIM:
                    raw = raw[:EMBEDDING_DIM]
                norm = math.sqrt(sum(x * x for x in raw)) or 1.0
                result.append([round(x / norm, 6) for x in raw])
            return result
        except (ImportError, Exception):
            pass

        # 3. Deterministic hash fallback
        return [_deterministic_hash_vector(t, EMBEDDING_DIM) for t in texts]

    def _call_remote_endpoint(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.endpoint}/v1/embeddings" if not self.endpoint.endswith("/embeddings") else self.endpoint
        payload = json.dumps({"input": texts, "model": self.model_name}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in items]


def index_snapshot_embeddings(conn, snapshot: dict[str, Any], settings: Settings) -> int:
    """Computes and updates pgvector embeddings for all meaningful objects in a snapshot."""
    doc_id = snapshot["document_id"]
    rev = snapshot["revision"]
    service = EmbeddingService(settings)

    objects = [
        obj for obj in snapshot.get("objects", [])
        if obj.get("search_text") and obj.get("type") not in ("CADEntity",)
    ]
    if not objects:
        return 0

    texts = [obj["search_text"] for obj in objects]
    vectors = service.embed_batch(texts)

    count = 0
    for obj, vec in zip(objects, vectors):
        vec_str = "[" + ",".join(str(v) for v in vec) + "]"
        content_hash = hashlib.sha256(obj["search_text"].encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO aec.embeddings(object_id, model, revision, content_hash, embedding)
               VALUES (%s, %s, %s, %s, %s::vector)
               ON CONFLICT(object_id, model) DO UPDATE
               SET revision = EXCLUDED.revision,
                   content_hash = EXCLUDED.content_hash,
                   embedding = EXCLUDED.embedding""",
            (obj["id"], service.model_name, rev, content_hash, vec_str),
        )
        count += 1

    conn.execute(
        """UPDATE aec.index_state
           SET embedding_revision = %s, embedding_model = %s
           WHERE document_id = %s""",
        (rev, service.model_name, doc_id),
    )
    return count

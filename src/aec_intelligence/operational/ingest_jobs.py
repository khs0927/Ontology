"""Job payloads for ad-hoc ingestion (REST ``POST /v1/ingestions`` and ``aec-ops ingest``).

Census-planned jobs already carry a content-addressed ``document_id``. Ad-hoc jobs used to omit it,
so the worker fell back to ``doc_<file stem>``: two different files that share a stem (``A/평면도.dwg``
and ``B/평면도.dwg``, or ``plan.dwg`` and ``plan.pdf``) were projected onto the same document and the
second ingestion deleted the first one's objects and relations. Ad-hoc jobs now use the same
content-addressed id as the census pipeline, so identical bytes converge and different files never
collide.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .census import document_id_for

_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_job(path: Path, *, project_id: str, discipline: str, queue: str) -> tuple[dict[str, Any], str]:
    """Return ``(payload, dedup_key)`` for one source file."""
    resolved = Path(path).resolve()
    sha = file_sha256(resolved)
    mtime = resolved.stat().st_mtime
    dedup_key = hashlib.sha256(f"{project_id}|{resolved}|{mtime}".encode("utf-8")).hexdigest()
    payload = {
        "source": str(resolved),
        "name": resolved.name,
        "project_id": project_id,
        "discipline": discipline,
        "queue": queue,
        "document_id": document_id_for(sha),
        "sha256": sha,
        "revision": 0,
    }
    return payload, dedup_key


def ingest_jobs(files: Iterable[Path], *, project_id: str, discipline: str, queue: str) -> list[tuple[dict[str, Any], str]]:
    return [ingest_job(f, project_id=project_id, discipline=discipline, queue=queue) for f in files]

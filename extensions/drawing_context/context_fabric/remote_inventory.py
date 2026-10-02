"""Read-only remote inventory verification for derived RAG providers.

RAGFlow can be fully verified because its reviewed API exposes dataset document
lists and complete per-document chunk lists. LightRAG v1.5.7 currently exposes
document status/counts through the public REST API but not a complete chunk-ID
inventory, so its verifier is deliberately partial and can never claim complete
remote verification.

No function in this module writes remote state or canonical CAIR data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from .compare import build_index_snapshot, projection_content_digest
from .lightrag_http import LightRagHttpAdapter
from .ragflow_http import RagflowHttpAdapter


_BINDING_METADATA_FIELDS = (
    "canonical_id",
    "source_id",
    "revision_id",
    "project_id",
    "sha256",
    "state",
)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fixture_external_ids(fixture: dict[str, Any]) -> tuple[str, ...]:
    projection = fixture.get("projection") if isinstance(fixture, dict) else None
    if not isinstance(projection, list):
        raise ValueError("benchmark fixture requires projection")
    values: list[str] = []
    for row in projection:
        if not isinstance(row, dict):
            raise ValueError("benchmark projection rows must be objects")
        external_id = row.get("external_id")
        if not isinstance(external_id, str) or not external_id.strip():
            raise ValueError("benchmark projection rows require external_id")
        values.append(external_id)
    if len(values) != len(set(values)):
        raise ValueError("benchmark projection external_id values must be unique")
    return tuple(sorted(values))


def _remote_chunk_content(row: dict[str, Any]) -> str | None:
    for key in ("content", "content_with_weight", "text"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return None


def _ragflow_data(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise RuntimeError("RAGFlow inventory response must be an object")
    code = response.get("code", 0)
    if code not in {0, "0", None}:
        raise RuntimeError(f"RAGFlow inventory API error {code}: {response.get('message')}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("RAGFlow inventory response has no data object")
    return data


def _list_ragflow_documents(
    adapter: RagflowHttpAdapter,
    *,
    page_size: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        path = (
            f"/api/v1/datasets/{adapter.config.dataset_id}/documents"
            f"?page={page}&page_size={page_size}"
        )
        data = _ragflow_data(adapter.sender("GET", path, None))
        docs = data.get("docs")
        if not isinstance(docs, list):
            raise RuntimeError("RAGFlow document inventory has no docs list")
        for raw in docs:
            if not isinstance(raw, dict):
                raise RuntimeError("RAGFlow document inventory row must be an object")
            doc_id = raw.get("id")
            if not isinstance(doc_id, str) or not doc_id:
                raise RuntimeError("RAGFlow document inventory row has no id")
            if doc_id in seen:
                raise RuntimeError("RAGFlow document inventory returned duplicate ids")
            seen.add(doc_id)
            rows.append(raw)
        if len(docs) < page_size:
            return rows
    raise RuntimeError("RAGFlow document inventory exceeded pagination safety limit")


def _list_ragflow_chunks(
    adapter: RagflowHttpAdapter,
    document_id: str,
    *,
    page_size: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        path = (
            f"/api/v1/datasets/{adapter.config.dataset_id}/documents/"
            f"{quote(document_id, safe='')}/chunks?page={page}&page_size={page_size}"
        )
        data = _ragflow_data(adapter.sender("GET", path, None))
        chunks = data.get("chunks")
        if not isinstance(chunks, list):
            raise RuntimeError("RAGFlow chunk inventory has no chunks list")
        for raw in chunks:
            if not isinstance(raw, dict):
                raise RuntimeError("RAGFlow chunk inventory row must be an object")
            chunk_id = raw.get("id") or raw.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise RuntimeError("RAGFlow chunk inventory row has no id")
            if chunk_id in seen:
                raise RuntimeError("RAGFlow chunk inventory returned duplicate ids")
            remote_doc = raw.get("document_id") or raw.get("doc_id")
            if remote_doc is not None and remote_doc != document_id:
                raise RuntimeError("RAGFlow chunk inventory crossed document boundary")
            seen.add(chunk_id)
            rows.append(raw)
        total = data.get("total")
        if isinstance(total, int) and total >= 0 and len(rows) >= total:
            return rows
        if len(chunks) < page_size:
            return rows
    raise RuntimeError("RAGFlow chunk inventory exceeded pagination safety limit")


def verify_ragflow_remote_inventory(
    adapter: RagflowHttpAdapter,
    fixture: dict[str, Any],
    *,
    current_revision_by_source: dict[str, str],
    current_sha256_by_source: dict[str, str],
    page_size: int = 100,
    max_pages: int = 1000,
) -> dict[str, Any]:
    """Fully enumerate a dedicated RAGFlow dataset and compare it to bindings."""
    if not adapter.config.enabled:
        raise RuntimeError("RAGFlow HTTP adapter is disabled")
    if not 1 <= page_size <= 100:
        raise ValueError("RAGFlow inventory page_size must be between 1 and 100")
    if not 1 <= max_pages <= 10000:
        raise ValueError("RAGFlow max_pages must be between 1 and 10000")

    bindings = adapter.registry.snapshot()
    if not bindings:
        raise ValueError("RAGFlow remote inventory requires non-empty local bindings")
    if any(row.get("dataset_id") != adapter.config.dataset_id for row in bindings):
        raise ValueError("RAGFlow bindings span a dataset other than the configured benchmark dataset")

    expected_docs = sorted({row["document_id"] for row in bindings})
    expected_chunks = sorted(row["chunk_id"] for row in bindings)
    expected_external = tuple(sorted(row["external_id"] for row in bindings))
    fixture_external = _fixture_external_ids(fixture)

    documents = _list_ragflow_documents(
        adapter,
        page_size=page_size,
        max_pages=max_pages,
    )
    remote_docs = sorted(row["id"] for row in documents)

    remote_chunk_rows: dict[str, dict[str, Any]] = {}
    remote_chunk_count_by_document: dict[str, int] = {}
    for document_id in remote_docs:
        document_chunks = _list_ragflow_chunks(
            adapter,
            document_id,
            page_size=page_size,
            max_pages=max_pages,
        )
        remote_chunk_count_by_document[document_id] = len(document_chunks)
        for row in document_chunks:
            chunk_id = row.get("id") or row.get("chunk_id")
            if chunk_id in remote_chunk_rows:
                raise RuntimeError(
                    "RAGFlow remote inventory duplicated a chunk across documents"
                )
            remote_chunk_rows[chunk_id] = row
    remote_chunks = sorted(remote_chunk_rows)

    missing_docs = sorted(set(expected_docs) - set(remote_docs))
    extra_docs = sorted(set(remote_docs) - set(expected_docs))

    not_done_document_ids: list[str] = []
    document_chunk_count_mismatch_ids: list[str] = []
    for document in documents:
        document_id = document["id"]
        run_status = str(document.get("run", "")).upper()
        if run_status not in {"3", "DONE"}:
            not_done_document_ids.append(document_id)
        reported_chunk_count = document.get("chunk_count")
        actual_chunk_count = remote_chunk_count_by_document.get(document_id, 0)
        if (
            type(reported_chunk_count) is not int
            or reported_chunk_count < 0
            or reported_chunk_count != actual_chunk_count
        ):
            document_chunk_count_mismatch_ids.append(document_id)
    missing_chunks = sorted(set(expected_chunks) - set(remote_chunks))
    extra_chunks = sorted(set(remote_chunks) - set(expected_chunks))
    binding_fixture_mismatch = expected_external != fixture_external

    fixture_content_by_external: dict[str, str] = {}
    fixture_metadata_by_external: dict[str, dict[str, str]] = {}
    for row in fixture["projection"]:
        content = row.get("content")
        if not isinstance(content, str):
            raise ValueError("benchmark projection rows require string content")
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("benchmark projection rows require metadata")
        normalized_metadata: dict[str, str] = {}
        for field in _BINDING_METADATA_FIELDS:
            value = metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"benchmark projection metadata requires non-empty {field}"
                )
            normalized_metadata[field] = value
        external_id = row["external_id"]
        fixture_content_by_external[external_id] = content
        fixture_metadata_by_external[external_id] = normalized_metadata

    fixture_source_state: dict[str, tuple[str, str]] = {}
    inconsistent_fixture_source_ids: set[str] = set()
    for metadata in fixture_metadata_by_external.values():
        source_id = metadata["source_id"]
        pair = (metadata["revision_id"], metadata["sha256"])
        previous = fixture_source_state.get(source_id)
        if previous is not None and previous != pair:
            inconsistent_fixture_source_ids.add(source_id)
        fixture_source_state[source_id] = pair

    canonical_freshness_mismatch_source_ids: list[str] = []
    missing_current_source_ids: list[str] = []
    for source_id, (revision_id, sha256) in sorted(fixture_source_state.items()):
        current_revision = current_revision_by_source.get(source_id)
        current_sha256 = current_sha256_by_source.get(source_id)
        if current_revision is None or current_sha256 is None:
            missing_current_source_ids.append(source_id)
            continue
        if current_revision != revision_id or current_sha256 != sha256:
            canonical_freshness_mismatch_source_ids.append(source_id)

    current_source_state_digest = _digest(
        [
            {
                "source_id": source_id,
                "revision_id": current_revision_by_source[source_id],
                "sha256": current_sha256_by_source[source_id],
            }
            for source_id in sorted(fixture_source_state)
            if source_id in current_revision_by_source
            and source_id in current_sha256_by_source
        ]
    )

    binding_metadata_mismatch_external_ids: list[str] = []
    for binding in bindings:
        external_id = binding["external_id"]
        expected_metadata = fixture_metadata_by_external.get(external_id)
        if expected_metadata is None:
            binding_metadata_mismatch_external_ids.append(external_id)
            continue
        actual_metadata = {
            field: binding.get(field)
            for field in _BINDING_METADATA_FIELDS
        }
        if actual_metadata != expected_metadata:
            binding_metadata_mismatch_external_ids.append(external_id)

    binding_by_chunk = {row["chunk_id"]: row for row in bindings}
    content_mismatch_chunk_ids: list[str] = []
    content_missing_chunk_ids: list[str] = []
    verified_remote_content: list[dict[str, str]] = []
    for chunk_id in sorted(set(expected_chunks) & set(remote_chunks)):
        binding = binding_by_chunk[chunk_id]
        external_id = binding["external_id"]
        expected_content = fixture_content_by_external.get(external_id)
        remote_content = _remote_chunk_content(remote_chunk_rows[chunk_id])
        if expected_content is None:
            content_mismatch_chunk_ids.append(chunk_id)
            continue
        if remote_content is None:
            content_missing_chunk_ids.append(chunk_id)
            continue
        if remote_content != expected_content:
            content_mismatch_chunk_ids.append(chunk_id)
            continue
        verified_remote_content.append(
            {"external_id": external_id, "content": remote_content}
        )

    remote_projection_content_digest = (
        _digest(sorted(verified_remote_content, key=lambda row: row["external_id"]))
        if len(verified_remote_content) == len(expected_chunks)
        else None
    )
    expected_projection_content_digest = projection_content_digest(fixture)
    content_digest_mismatch = (
        remote_projection_content_digest is not None
        and remote_projection_content_digest != expected_projection_content_digest
    )

    exact = not (
        missing_docs
        or extra_docs
        or missing_chunks
        or extra_chunks
        or not_done_document_ids
        or document_chunk_count_mismatch_ids
        or binding_fixture_mismatch
        or inconsistent_fixture_source_ids
        or canonical_freshness_mismatch_source_ids
        or missing_current_source_ids
        or binding_metadata_mismatch_external_ids
        or content_mismatch_chunk_ids
        or content_missing_chunk_ids
        or content_digest_mismatch
    )

    base_snapshot = build_index_snapshot(
        fixture,
        provider_index_id=adapter.config.dataset_id,
        indexed_external_ids=expected_external,
        isolated_namespace=True,
    )
    snapshot = dict(base_snapshot)
    if exact:
        snapshot.update(
            {
                "assurance": "remote-readback-complete",
                "remote_inventory_verified": True,
                "remote_document_count": len(remote_docs),
                "remote_chunk_count": len(remote_chunks),
                "remote_document_ids_digest": _digest(remote_docs),
                "remote_chunk_ids_digest": _digest(remote_chunks),
                "remote_projection_content_digest": remote_projection_content_digest,
                "canonical_freshness_verified": True,
                "current_source_state_digest": current_source_state_digest,
                "processing_completion_verified": True,
                "deployment_identity_verified": False,
            }
        )

    return {
        "schema": "drawing-context-rag-remote-inventory/1",
        "provider": "ragflow",
        "status": "VERIFIED" if exact else "BLOCKED",
        "provider_index_id": adapter.config.dataset_id,
        "expected_document_count": len(expected_docs),
        "remote_document_count": len(remote_docs),
        "expected_chunk_count": len(expected_chunks),
        "remote_chunk_count": len(remote_chunks),
        "missing_document_ids": missing_docs,
        "extra_document_ids": extra_docs,
        "missing_chunk_ids": missing_chunks,
        "extra_chunk_ids": extra_chunks,
        "not_done_document_ids": sorted(not_done_document_ids),
        "document_chunk_count_mismatch_ids": sorted(
            document_chunk_count_mismatch_ids
        ),
        "processing_completion_verified": not (
            not_done_document_ids or document_chunk_count_mismatch_ids
        ),
        "binding_fixture_mismatch": binding_fixture_mismatch,
        "binding_metadata_mismatch_external_ids": sorted(
            set(binding_metadata_mismatch_external_ids)
        ),
        "inconsistent_fixture_source_ids": sorted(inconsistent_fixture_source_ids),
        "missing_current_source_ids": missing_current_source_ids,
        "canonical_freshness_mismatch_source_ids": (
            canonical_freshness_mismatch_source_ids
        ),
        "canonical_freshness_verified": not (
            inconsistent_fixture_source_ids
            or canonical_freshness_mismatch_source_ids
            or missing_current_source_ids
        ),
        "current_source_state_digest": current_source_state_digest,
        "deployment_identity_verified": False,
        "content_mismatch_chunk_ids": content_mismatch_chunk_ids,
        "content_missing_chunk_ids": content_missing_chunk_ids,
        "expected_projection_content_digest": expected_projection_content_digest,
        "remote_projection_content_digest": remote_projection_content_digest,
        "content_digest_mismatch": content_digest_mismatch,
        "index_snapshot": snapshot,
        "remote_inventory_verified": exact,
        "read_only": True,
        "canonical_mutation": False,
    }


def _flatten_lightrag_documents(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the v1.5.x /documents status response without inventing chunks."""
    if not isinstance(response, dict):
        raise RuntimeError("LightRAG document inventory response must be an object")
    statuses = response.get("statuses")
    if isinstance(statuses, dict):
        rows: list[dict[str, Any]] = []
        for value in statuses.values():
            if not isinstance(value, list):
                raise RuntimeError("LightRAG document status bucket must be a list")
            rows.extend(row for row in value if isinstance(row, dict))
        return rows
    documents = response.get("documents")
    if isinstance(documents, list):
        return [row for row in documents if isinstance(row, dict)]
    raise RuntimeError("LightRAG /documents response has no supported document collection")


def inspect_lightrag_remote_inventory(
    adapter: LightRagHttpAdapter,
) -> dict[str, Any]:
    """Read public document inventory but never claim complete chunk verification."""
    if not adapter.config.enabled:
        raise RuntimeError("LightRAG adapter is disabled")
    if not adapter._auth_verified:
        adapter.verify_credentials()

    documents = _flatten_lightrag_documents(adapter.sender("GET", "/documents", None))
    document_ids: list[str] = []
    observed_chunk_count = 0
    missing_chunk_counts = 0

    for row in documents:
        doc_id = row.get("id")
        if not isinstance(doc_id, str) or not doc_id:
            raise RuntimeError("LightRAG document inventory row has no id")
        document_ids.append(doc_id)
        count = row.get("chunks_count")
        if isinstance(count, int) and count >= 0:
            observed_chunk_count += count
        else:
            missing_chunk_counts += 1

    if len(document_ids) != len(set(document_ids)):
        raise RuntimeError("LightRAG document inventory returned duplicate ids")

    local_bindings = adapter.registry.snapshot()
    return {
        "schema": "drawing-context-rag-remote-inventory/1",
        "provider": "lightrag",
        "status": "PARTIAL",
        "reason": (
            "LightRAG v1.5.7 public /documents inventory exposes document ids and "
            "chunk counts but not the complete chunk-id set required for exact "
            "remote corpus proof."
        ),
        "remote_document_count": len(document_ids),
        "remote_document_ids_digest": _digest(sorted(document_ids)),
        "reported_chunk_count": observed_chunk_count,
        "documents_without_chunk_count": missing_chunk_counts,
        "local_bound_chunk_count": len(local_bindings),
        "remote_inventory_verified": False,
        "production_adoption_eligible": False,
        "read_only": True,
        "canonical_mutation": False,
    }

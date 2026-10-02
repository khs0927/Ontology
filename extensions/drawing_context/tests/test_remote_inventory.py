from __future__ import annotations

import pytest

from context_fabric.lightrag_http import (
    LightRagBinding,
    LightRagBindingRegistry,
    LightRagHttpAdapter,
    LightRagHttpConfig,
)
from context_fabric.ragflow_http import (
    RagflowBinding,
    RagflowBindingRegistry,
    RagflowHttpAdapter,
    RagflowHttpConfig,
)
from context_fabric.remote_inventory import (
    inspect_lightrag_remote_inventory,
    verify_ragflow_remote_inventory,
)


class PathSender:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        value = self.responses.get((method, path))
        if value is None:
            raise AssertionError(f"unexpected request: {method} {path}")
        if isinstance(value, Exception):
            raise value
        return value


def fixture():
    return {
        "schema": "drawing-context-rag-benchmark/1",
        "provider": "neutral",
        "canonical_mutation": False,
        "projection": [
            {
                "external_id": "ctx-1",
                "content": "door",
                "metadata": {
                    "canonical_id": "door-1",
                    "source_id": "source-1",
                    "revision_id": "rev-1",
                    "project_id": "P1",
                    "sha256": "a" * 64,
                    "state": "HUMAN_VERIFIED",
                },
            }
        ],
        "cases": [{"case_id": "q1", "query": "door"}],
    }


def ragflow_config():
    return RagflowHttpConfig(
        enabled=True,
        base_url="http://127.0.0.1:9380",
        api_key="test-key",
        dataset_id="dataset-1",
        release="v0.27.2",
        release_image_digest="sha256:" + "c" * 64,
    )


def ragflow_registry():
    registry = RagflowBindingRegistry()
    registry.bind(
        RagflowBinding(
            external_id="ctx-1",
            canonical_id="door-1",
            source_id="source-1",
            revision_id="rev-1",
            project_id="P1",
            sha256="a" * 64,
            state="HUMAN_VERIFIED",
            dataset_id="dataset-1",
            document_id="doc-1",
            chunk_id="chunk-1",
        )
    )
    return registry


def test_ragflow_complete_readback_upgrades_snapshot_to_verified():
    sender = PathSender(
        {
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents?page=1&page_size=100",
            ): {"code": 0, "data": {"docs": [{"id": "doc-1", "chunk_count": 1}]}},
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/doc-1/chunks?page=1&page_size=100",
            ): {
                "code": 0,
                "data": {
                    "chunks": [{"id": "chunk-1", "document_id": "doc-1", "content": "door"}],
                    "total": 1,
                },
            },
        }
    )
    adapter = RagflowHttpAdapter(
        ragflow_config(),
        registry=ragflow_registry(),
        sender=sender,
    )

    result = verify_ragflow_remote_inventory(adapter, fixture())

    assert result["status"] == "VERIFIED"
    assert result["remote_inventory_verified"] is True
    assert result["read_only"] is True
    assert result["canonical_mutation"] is False
    snapshot = result["index_snapshot"]
    assert snapshot["assurance"] == "remote-readback-complete"
    assert snapshot["remote_inventory_verified"] is True
    assert snapshot["remote_document_count"] == 1
    assert snapshot["remote_chunk_count"] == 1
    assert all(call[0] == "GET" for call in sender.calls)


def test_ragflow_extra_remote_chunk_blocks_complete_proof():
    sender = PathSender(
        {
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents?page=1&page_size=100",
            ): {"code": 0, "data": {"docs": [{"id": "doc-1", "chunk_count": 2}]}},
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/doc-1/chunks?page=1&page_size=100",
            ): {
                "code": 0,
                "data": {
                    "chunks": [
                        {"id": "chunk-1", "document_id": "doc-1"},
                        {"id": "unknown", "document_id": "doc-1", "content": "extra"},
                    ],
                    "total": 2,
                },
            },
        }
    )
    adapter = RagflowHttpAdapter(
        ragflow_config(),
        registry=ragflow_registry(),
        sender=sender,
    )

    result = verify_ragflow_remote_inventory(adapter, fixture())

    assert result["status"] == "BLOCKED"
    assert result["remote_inventory_verified"] is False
    assert result["extra_chunk_ids"] == ["unknown"]
    assert result["index_snapshot"]["assurance"] == "caller-attested-local-view"


def test_ragflow_unknown_document_blocks_complete_proof_before_claiming_success():
    sender = PathSender(
        {
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents?page=1&page_size=100",
            ): {
                "code": 0,
                "data": {
                    "docs": [
                        {"id": "doc-1", "chunk_count": 1},
                        {"id": "extra-doc", "chunk_count": 0},
                    ]
                },
            },
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/doc-1/chunks?page=1&page_size=100",
            ): {"code": 0, "data": {"chunks": [{"id": "chunk-1", "content": "door"}], "total": 1}},
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/extra-doc/chunks?page=1&page_size=100",
            ): {"code": 0, "data": {"chunks": [], "total": 0}},
        }
    )
    adapter = RagflowHttpAdapter(
        ragflow_config(),
        registry=ragflow_registry(),
        sender=sender,
    )
    result = verify_ragflow_remote_inventory(adapter, fixture())
    assert result["status"] == "BLOCKED"
    assert result["extra_document_ids"] == ["extra-doc"]


def lightrag_config():
    return LightRagHttpConfig(
        enabled=True,
        base_url="http://127.0.0.1:9621",
        api_key="test-key",
        release="v1.5.7",
        release_image_digest="sha256:" + "d" * 64,
    )


def lightrag_registry():
    registry = LightRagBindingRegistry()
    registry.bind(
        LightRagBinding(
            external_id="ctx-1",
            canonical_id="door-1",
            source_id="source-1",
            revision_id="rev-1",
            project_id="P1",
            sha256="a" * 64,
            state="HUMAN_VERIFIED",
            chunk_id="chunk-1",
        )
    )
    return registry


def test_lightrag_public_inventory_is_explicitly_partial():
    sender = PathSender(
        {
            ("GET", "/auth/verify"): {"authenticated": True},
            (
                "GET",
                "/documents",
            ): {
                "statuses": {
                    "processed": [
                        {
                            "id": "doc-1",
                            "status": "processed",
                            "chunks_count": 1,
                            "file_path": "fixture.txt",
                        }
                    ]
                }
            },
        }
    )
    adapter = LightRagHttpAdapter(
        lightrag_config(),
        registry=lightrag_registry(),
        sender=sender,
    )

    result = inspect_lightrag_remote_inventory(adapter)

    assert result["status"] == "PARTIAL"
    assert result["remote_inventory_verified"] is False
    assert result["production_adoption_eligible"] is False
    assert result["reported_chunk_count"] == 1
    assert result["local_bound_chunk_count"] == 1
    assert all(call[0] == "GET" for call in sender.calls)


def test_lightrag_malformed_document_inventory_fails_closed():
    sender = PathSender(
        {
            ("GET", "/auth/verify"): {"authenticated": True},
            ("GET", "/documents"): {"unexpected": []},
        }
    )
    adapter = LightRagHttpAdapter(
        lightrag_config(),
        registry=lightrag_registry(),
        sender=sender,
    )
    with pytest.raises(RuntimeError, match="supported document collection"):
        inspect_lightrag_remote_inventory(adapter)


def test_ragflow_same_chunk_id_with_changed_content_blocks_proof():
    sender = PathSender(
        {
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents?page=1&page_size=100",
            ): {"code": 0, "data": {"docs": [{"id": "doc-1", "chunk_count": 1}]}},
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/doc-1/chunks?page=1&page_size=100",
            ): {
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "id": "chunk-1",
                            "document_id": "doc-1",
                            "content": "tampered remote content",
                        }
                    ],
                    "total": 1,
                },
            },
        }
    )
    adapter = RagflowHttpAdapter(
        ragflow_config(),
        registry=ragflow_registry(),
        sender=sender,
    )

    result = verify_ragflow_remote_inventory(adapter, fixture())

    assert result["status"] == "BLOCKED"
    assert result["remote_inventory_verified"] is False
    assert result["content_mismatch_chunk_ids"] == ["chunk-1"]
    assert result["index_snapshot"]["assurance"] == "caller-attested-local-view"


def test_ragflow_missing_remote_chunk_content_blocks_proof():
    sender = PathSender(
        {
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents?page=1&page_size=100",
            ): {"code": 0, "data": {"docs": [{"id": "doc-1", "chunk_count": 1}]}},
            (
                "GET",
                "/api/v1/datasets/dataset-1/documents/doc-1/chunks?page=1&page_size=100",
            ): {
                "code": 0,
                "data": {
                    "chunks": [{"id": "chunk-1", "document_id": "doc-1"}],
                    "total": 1,
                },
            },
        }
    )
    adapter = RagflowHttpAdapter(
        ragflow_config(),
        registry=ragflow_registry(),
        sender=sender,
    )

    result = verify_ragflow_remote_inventory(adapter, fixture())

    assert result["status"] == "BLOCKED"
    assert result["content_missing_chunk_ids"] == ["chunk-1"]

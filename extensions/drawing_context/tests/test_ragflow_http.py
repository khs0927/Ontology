from __future__ import annotations

from collections import deque

import pytest

from context_fabric.benchmark import BenchmarkCase, evaluate_benchmark, promotion_decision
from context_fabric.ragflow_http import (
    RagflowBindingRegistry,
    RagflowHttpAdapter,
    RagflowHttpConfig,
)


DIGEST = "sha256:" + "b" * 64


def config(**overrides):
    values = {
        "enabled": True,
        "base_url": "http://127.0.0.1:9380",
        "api_key": "ragflow-test-key",
        "dataset_id": "dataset-1",
        "release": "v0.27.2",
        "release_image_digest": DIGEST,
        "timeout_seconds": 10,
    }
    values.update(overrides)
    return RagflowHttpConfig(**values)


def row(*, external="ctx-1", canonical="door-1", source="source-1", revision="rev-1", state="HUMAN_VERIFIED"):
    return {
        "external_id": external,
        "content": "공장 화장실 출입문",
        "metadata": {
            "canonical_id": canonical,
            "source_id": source,
            "revision_id": revision,
            "project_id": "P1",
            "sha256": "a" * 64,
            "state": state,
        },
    }


class FakeSender:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def test_enabled_profile_requires_https_for_remote_and_fixed_release_digest():
    with pytest.raises(ValueError, match="requires https"):
        config(base_url="http://ragflow.example.com")
    with pytest.raises(ValueError, match="pinned"):
        config(release="v0.27.3")
    with pytest.raises(ValueError, match="digest"):
        config(release_image_digest="latest")
    disabled = RagflowHttpConfig(enabled=False)
    assert disabled.enabled is False


def test_add_projection_creates_derived_remote_binding_without_canonical_mutation():
    sender = FakeSender([
        {"code": 0, "data": {"chunk": {"id": "chunk-1"}}},
    ])
    registry = RagflowBindingRegistry()
    adapter = RagflowHttpAdapter(config(), registry=registry, sender=sender)

    result = adapter.add_projection("doc-1", [row()])

    assert result["status"] == "SUCCESS"
    assert result["canonical_mutation"] is False
    binding = registry.get_chunk("chunk-1")
    assert binding is not None
    assert binding.external_id == "ctx-1"
    assert binding.canonical_id == "door-1"
    assert binding.source_id == "source-1"
    assert binding.revision_id == "rev-1"
    assert binding.state == "HUMAN_VERIFIED"
    method, path, body = sender.calls[0]
    assert method == "POST"
    assert path == "/api/v1/datasets/dataset-1/documents/doc-1/chunks"
    assert body["content"] == "공장 화장실 출입문"


def test_search_rehydrates_provenance_from_local_binding_registry():
    sender = FakeSender([
        {"code": 0, "data": {"chunk": {"id": "chunk-1"}}},
        {
            "code": 0,
            "data": {
                "chunks": [
                    {
                        "id": "chunk-1",
                        "content": "remote content",
                    }
                ]
            },
        },
    ])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    adapter.add_projection("doc-1", [row()])

    result = adapter.search("화장실 출입문", top_k=5)

    assert result["unmapped_remote_hits"] == 0
    hit = result["hits"][0]
    assert hit["external_id"] == "ctx-1"
    assert hit["metadata"] == {
        "canonical_id": "door-1",
        "source_id": "source-1",
        "revision_id": "rev-1",
        "project_id": "P1",
        "sha256": "a" * 64,
        "state": "HUMAN_VERIFIED",
    }
    assert sender.calls[1][1] == "/api/v1/datasets/dataset-1/search"
    assert sender.calls[1][2]["page_size"] == 5


def test_unmapped_remote_hit_is_never_given_invented_provenance_and_fails_gate():
    sender = FakeSender([
        {
            "code": 0,
            "data": {
                "chunks": [
                    {
                        "id": "unknown-chunk",
                        "content": "looks relevant",
                    }
                ]
            },
        },
    ])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    result = adapter.search("door")

    assert result["unmapped_remote_hits"] == 1
    assert result["hits"][0]["metadata"] == {}
    metrics = evaluate_benchmark(
        [
            BenchmarkCase(
                "q1",
                "door",
                ("door-1",),
                frozenset({"source-1"}),
            )
        ],
        {"q1": {"hits": result["hits"]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["provenance_metadata_coverage"] == 0.0
    assert promotion_decision(metrics)["status"] == "BLOCKED"


def test_revision_replace_adds_new_chunks_before_deleting_old_revision():
    sender = FakeSender([
        {"code": 0, "data": {"chunk": {"id": "old-chunk"}}},
        {"code": 0, "data": {"chunk": {"id": "new-chunk"}}},
        {"code": 0, "data": True},
    ])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    adapter.add_projection("doc-1", [row()])
    replaced = adapter.replace_revision(
        "doc-1",
        source_id="source-1",
        old_revision_id="rev-1",
        new_rows=[row(external="ctx-2", revision="rev-2")],
    )

    assert replaced["status"] == "SUCCESS"
    assert replaced["deleted_old"] == 1
    assert adapter.registry.get_chunk("old-chunk") is None
    new_binding = adapter.registry.get_chunk("new-chunk")
    assert new_binding is not None
    assert new_binding.revision_id == "rev-2"
    assert [call[0] for call in sender.calls] == ["POST", "POST", "DELETE"]


def test_source_purge_groups_remote_deletes_and_clears_bindings():
    sender = FakeSender([
        {"code": 0, "data": {"chunk": {"id": "chunk-a"}}},
        {"code": 0, "data": {"chunk": {"id": "chunk-b"}}},
        {"code": 0, "data": True},
    ])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    adapter.add_projection(
        "doc-1",
        [
            row(external="ctx-a", canonical="a"),
            row(external="ctx-b", canonical="b"),
        ],
    )
    result = adapter.purge_source("source-1")
    assert result["deleted"] == 2
    assert adapter.registry.snapshot() == []
    assert sender.calls[-1] == (
        "DELETE",
        "/api/v1/datasets/dataset-1/documents/doc-1/chunks",
        {"chunk_ids": ["chunk-a", "chunk-b"]},
    )


def test_add_projection_rolls_back_new_remote_chunks_on_partial_failure():
    sender = FakeSender([
        {"code": 0, "data": {"chunk": {"id": "chunk-a"}}},
        RuntimeError("remote failed"),
        {"code": 0, "data": True},
    ])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    with pytest.raises(RuntimeError, match="remote failed"):
        adapter.add_projection(
            "doc-1",
            [
                row(external="ctx-a", canonical="a"),
                row(external="ctx-b", canonical="b"),
            ],
        )
    assert adapter.registry.snapshot() == []
    assert sender.calls[-1][0] == "DELETE"


def test_disabled_adapter_refuses_network_actions():
    adapter = RagflowHttpAdapter(RagflowHttpConfig(enabled=False), sender=FakeSender([]))
    with pytest.raises(RuntimeError, match="disabled"):
        adapter.search("door")


def test_projection_is_validated_before_first_network_call():
    sender = FakeSender([])
    adapter = RagflowHttpAdapter(config(), sender=sender)
    bad = row()
    del bad["metadata"]["sha256"]
    with pytest.raises(ValueError, match="provenance"):
        adapter.add_projection("doc-1", [bad])
    assert sender.calls == []

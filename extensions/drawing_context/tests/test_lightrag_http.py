from __future__ import annotations

from collections import deque
import json
from pathlib import Path

import pytest

from context_fabric.benchmark import BenchmarkCase, evaluate_benchmark, promotion_decision
from context_fabric.lightrag_http import (
    LightRagBindingRegistry,
    LightRagHttpAdapter,
    LightRagHttpConfig,
)


def config(**overrides):
    values = {
        "enabled": True,
        "base_url": "http://127.0.0.1:9621",
        "api_key": "light-test-key",
        "release": "v1.5.7",
        "release_image_digest": "sha256:" + "a" * 64,
        "timeout_seconds": 20,
    }
    values.update(overrides)
    return LightRagHttpConfig(**values)


def row(*, external="ctx-1", canonical="door-1", source="source-1", revision="rev-1"):
    return {
        "external_id": external,
        "content": "공장 화장실 출입문",
        "metadata": {
            "canonical_id": canonical,
            "source_id": source,
            "revision_id": revision,
            "project_id": "P1",
            "sha256": "b" * 64,
            "state": "HUMAN_VERIFIED",
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
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return value


def query_response(*chunks):
    return {
        "status": "success",
        "message": "ok",
        "data": {
            "entities": [],
            "relationships": [],
            "chunks": list(chunks),
            "references": [],
        },
        "metadata": {"query_mode": "mix"},
    }


def test_enabled_config_requires_https_for_remote_auth_and_pinned_release():
    with pytest.raises(ValueError, match="requires https"):
        config(base_url="http://lightrag.example.com")
    with pytest.raises(ValueError, match="API key"):
        config(api_key="")
    with pytest.raises(ValueError, match="pinned"):
        config(release="v1.5.8")
    with pytest.raises(ValueError, match="digest"):
        config(release_image_digest="latest")


def test_search_verifies_credentials_before_query_and_rehydrates_provenance():
    sender = FakeSender([
        {"authenticated": True},
        query_response({"chunk_id": "chunk-1", "content": "remote content"}),
    ])
    adapter = LightRagHttpAdapter(config(), sender=sender)
    adapter.bind_projection(row(), chunk_id="chunk-1")

    result = adapter.search(
        "화장실 출입문",
        top_k=5,
        allowed_source_ids={"source-1"},
        current_revision_by_source={"source-1": "rev-1"},
    )

    assert sender.calls[0] == ("GET", "/auth/verify", None)
    assert sender.calls[1][0:2] == ("POST", "/query/data")
    assert sender.calls[1][2]["chunk_top_k"] == 5
    assert result["blocked_remote_hits"] == {
        "unmapped": 0,
        "unauthorized": 0,
        "stale_revision": 0,
    }
    assert result["hits"][0]["external_id"] == "ctx-1"
    assert result["hits"][0]["metadata"]["canonical_id"] == "door-1"
    assert result["hits"][0]["metadata"]["revision_id"] == "rev-1"


def test_production_search_never_returns_unmapped_unauthorized_or_stale_content():
    sender = FakeSender([
        {"authenticated": True},
        query_response(
            {"chunk_id": "allowed", "content": "allowed body"},
            {"chunk_id": "other", "content": "secret other source"},
            {"chunk_id": "stale", "content": "stale revision body"},
            {"chunk_id": "unknown", "content": "unmapped secret"},
        ),
    ])
    adapter = LightRagHttpAdapter(config(), sender=sender)
    adapter.bind_projection(row(external="allowed", canonical="door-1"), chunk_id="allowed")
    adapter.bind_projection(row(external="other", canonical="door-2", source="source-2"), chunk_id="other")
    adapter.bind_projection(row(external="stale", canonical="door-3", revision="rev-old"), chunk_id="stale")

    result = adapter.search(
        "door",
        top_k=10,
        allowed_source_ids={"source-1"},
        current_revision_by_source={"source-1": "rev-1"},
    )

    assert [hit["external_id"] for hit in result["hits"]] == ["allowed"]
    assert result["blocked_remote_hits"] == {
        "unmapped": 1,
        "unauthorized": 1,
        "stale_revision": 1,
    }
    rendered = json.dumps(result, ensure_ascii=False)
    assert "secret other source" not in rendered
    assert "stale revision body" not in rendered
    assert "unmapped secret" not in rendered


def test_diagnostic_unmapped_hit_has_empty_metadata_and_fails_shared_gate():
    sender = FakeSender([
        {"authenticated": True},
        query_response({"chunk_id": "unknown", "content": "looks relevant"}),
    ])
    adapter = LightRagHttpAdapter(config(), sender=sender)
    result = adapter.benchmark_search("door")

    assert result["unmapped_remote_hits"] == 1
    assert result["hits"][0]["metadata"] == {}
    metrics = evaluate_benchmark(
        [BenchmarkCase("q1", "door", ("door-1",), frozenset({"source-1"}))],
        {"q1": {"hits": result["hits"]}},
        current_revision_by_source={"source-1": "rev-1"},
    )
    assert metrics["provenance_metadata_coverage"] == 0.0
    assert promotion_decision(metrics)["status"] == "BLOCKED"


def test_binding_registry_is_runtime_only_and_roundtrips(tmp_path):
    registry = LightRagBindingRegistry()
    adapter = LightRagHttpAdapter(config(), registry=registry, sender=FakeSender([]))
    adapter.bind_projection(row(), chunk_id="chunk-1")

    saved = registry.save_runtime(tmp_path)
    assert saved["canonical_mutation"] is False
    assert Path(saved["path"]).parts[-3:] == ("runtime", "lightrag", "bindings.json")
    assert not (tmp_path / "global" / "lightrag").exists()

    restored = LightRagBindingRegistry.load_runtime(tmp_path)
    assert restored.snapshot() == registry.snapshot()

    with pytest.raises(ValueError, match="runtime/lightrag"):
        registry.save_runtime(tmp_path, tmp_path / "global" / "bindings.json")


def test_binding_rejects_bad_provenance_before_network():
    sender = FakeSender([])
    adapter = LightRagHttpAdapter(config(), sender=sender)
    bad = row()
    del bad["metadata"]["sha256"]
    with pytest.raises(ValueError, match="provenance"):
        adapter.bind_projection(bad, chunk_id="chunk-1")
    assert sender.calls == []


def test_disabled_adapter_refuses_network():
    adapter = LightRagHttpAdapter(LightRagHttpConfig(enabled=False), sender=FakeSender([]))
    with pytest.raises(RuntimeError, match="disabled"):
        adapter.benchmark_search("door")


def test_query_rejects_failed_or_malformed_response():
    sender = FakeSender([
        {"authenticated": True},
        {"status": "failure", "message": "bad query", "data": {}, "metadata": {}},
    ])
    adapter = LightRagHttpAdapter(config(), sender=sender)
    with pytest.raises(RuntimeError, match="bad query"):
        adapter.benchmark_search("door")

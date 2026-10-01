from __future__ import annotations

from collections import deque

from context_fabric.benchmark import BenchmarkCase
from context_fabric.ragflow_deploy import (
    RagflowDeploymentProfile,
    deployment_commands,
    evaluate_preflight,
    parse_version,
)
from context_fabric.ragflow_http import (
    RagflowBinding,
    RagflowBindingRegistry,
    RagflowHttpConfig,
)
from context_fabric.ragflow_live import run_live_benchmark


def test_preflight_accepts_supported_x86_host():
    report = evaluate_preflight(
        {
            "platform": "linux",
            "machine": "x86_64",
            "cpu_cores": 8,
            "ram_gb": 32,
            "disk_free_gb": 200,
            "docker_version": "27.3.1",
            "compose_version": "v2.30.3",
            "vm_max_map_count": 262144,
            "image_repo_digest": "infiniflow/ragflow@sha256:" + "a" * 64,
        }
    )
    assert report["status"] == "READY"
    assert report["blockers"] == []
    assert report["mutates_host"] is False


def test_preflight_blocks_insufficient_resources_and_prebuilt_arm():
    report = evaluate_preflight(
        {
            "platform": "linux",
            "machine": "arm64",
            "cpu_cores": 2,
            "ram_gb": 8,
            "disk_free_gb": 20,
            "docker_version": "23.0.0",
            "compose_version": "2.20.0",
            "vm_max_map_count": 65530,
            "image_repo_digest": None,
        }
    )
    assert report["status"] == "BLOCKED"
    for name in [
        "cpu",
        "ram",
        "disk",
        "docker",
        "compose",
        "prebuilt_architecture",
        "vm_max_map_count",
    ]:
        assert name in report["blockers"]


def test_windows_without_vm_probe_requires_manual_check_not_false_success():
    report = evaluate_preflight(
        {
            "platform": "windows",
            "machine": "amd64",
            "cpu_cores": 8,
            "ram_gb": 32,
            "disk_free_gb": 100,
            "docker_version": "27.3.1",
            "compose_version": "2.30.3",
            "vm_max_map_count": None,
            "image_repo_digest": None,
        }
    )
    assert report["status"] == "READY_WITH_MANUAL_CHECKS"
    assert report["manual_checks"]


def test_versions_and_commands_are_pinned_to_upstream_release():
    assert parse_version("Docker version 27.3.1") == (27, 3, 1)
    assert parse_version("v2.26.1") == (2, 26, 1)
    commands = deployment_commands("runtime/ragflow-upstream")
    rendered = "\n".join(commands)
    assert "v0.27.2" in rendered
    assert "infiniflow/ragflow:v0.27.2" in rendered
    assert "down -v" not in rendered
    assert "git clone https://github.com/infiniflow/ragflow.git" in rendered


def test_binding_registry_runtime_roundtrip_is_rebuildable(tmp_path):
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
    report = registry.save_runtime(tmp_path)
    assert report["canonical_mutation"] is False
    restored = RagflowBindingRegistry.load_runtime(tmp_path)
    assert restored.snapshot() == registry.snapshot()


class FakeBenchmarkAdapter:
    def __init__(self):
        self.config = RagflowHttpConfig(
            enabled=True,
            base_url="http://127.0.0.1:9380",
            api_key="test-api-key",
            dataset_id="dataset-1",
            release="v0.27.2",
            release_image_digest="sha256:" + "b" * 64,
        )
        self.calls = []

    def benchmark_search(self, question, *, top_k=5):
        self.calls.append((question, top_k))
        return {
            "status": "SUCCESS",
            "hits": [
                {
                    "external_id": "ctx-1",
                    "content": "공장 화장실 출입문",
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
        }


def test_live_benchmark_runner_is_read_only_and_uses_existing_gate():
    adapter = FakeBenchmarkAdapter()
    ticks = deque([10.0, 10.125])
    result = run_live_benchmark(
        adapter,
        [
            BenchmarkCase(
                case_id="door",
                query="화장실 출입문",
                expected_canonical_ids=("door-1",),
                allowed_source_ids=frozenset({"source-1"}),
            )
        ],
        current_revision_by_source={"source-1": "rev-1"},
        k=5,
        clock=lambda: ticks.popleft(),
    )
    assert result["read_only"] is True
    assert result["canonical_mutation"] is False
    assert result["promotion"]["status"] == "PASS"
    assert result["metrics"]["latency"]["p95_ms"] == 125.0
    assert adapter.calls == [("화장실 출입문", 5)]


def test_deployment_profile_matches_reviewed_official_minimums():
    profile = RagflowDeploymentProfile()
    assert profile.release == "v0.27.2"
    assert profile.image == "infiniflow/ragflow:v0.27.2"
    assert profile.minimum_cpu_cores == 4
    assert profile.minimum_ram_gb == 16
    assert profile.minimum_disk_gb == 50
    assert profile.minimum_docker == (24, 0, 0)
    assert profile.minimum_compose == (2, 26, 1)

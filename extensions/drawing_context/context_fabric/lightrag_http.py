"""Fail-closed LightRAG comparison sidecar.

LightRAG is a derived retrieval provider, never canonical. The adapter uses the
structured /query/data endpoint and accepts a hit only when its remote chunk_id
is locally bound to an existing drawing-context projection. It never invents
provenance from file_path or content.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


_REQUIRED_METADATA = (
    "canonical_id",
    "source_id",
    "revision_id",
    "project_id",
    "sha256",
    "state",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class LightRagHttpConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    release: str = "v1.5.7"
    release_image_digest: str = ""
    timeout_seconds: float = 20.0

    def __post_init__(self):
        if not self.enabled:
            return
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LightRAG base_url must be absolute http(s)")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if parsed.scheme != "https" and not loopback:
            raise ValueError("remote LightRAG requires https")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in LightRAG URL")
        if not self.api_key.strip():
            raise ValueError("enabled LightRAG requires an API key")
        if self.release != "v1.5.7":
            raise ValueError("LightRAG comparison profile is pinned to v1.5.7")
        if not _IMAGE_DIGEST.fullmatch(self.release_image_digest):
            raise ValueError("enabled LightRAG requires a pinned sha256 image digest")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("LightRAG timeout must be between 1 and 120 seconds")


@dataclass(frozen=True)
class LightRagBinding:
    external_id: str
    canonical_id: str
    source_id: str
    revision_id: str
    project_id: str
    sha256: str
    state: str
    chunk_id: str

    def __post_init__(self):
        for name in (
            "external_id",
            "canonical_id",
            "source_id",
            "revision_id",
            "project_id",
            "state",
            "chunk_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("LightRagBinding fields must be non-empty strings")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("LightRagBinding sha256 must be lowercase SHA-256 hex")

    @classmethod
    def from_projection(cls, row: dict[str, Any], *, chunk_id: str) -> "LightRagBinding":
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("projection row requires metadata")
        if any(not isinstance(metadata.get(k), str) or not metadata[k].strip() for k in _REQUIRED_METADATA):
            raise ValueError("projection row lost required provenance metadata")
        if not _SHA256.fullmatch(metadata["sha256"]):
            raise ValueError("projection row has invalid sha256 metadata")
        external_id = row.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            raise ValueError("projection row requires external_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError("LightRAG chunk_id must be non-empty")
        return cls(
            external_id=external_id,
            canonical_id=metadata["canonical_id"],
            source_id=metadata["source_id"],
            revision_id=metadata["revision_id"],
            project_id=metadata["project_id"],
            sha256=metadata["sha256"],
            state=metadata["state"],
            chunk_id=chunk_id,
        )

    def benchmark_metadata(self) -> dict[str, str]:
        return {
            "canonical_id": self.canonical_id,
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "project_id": self.project_id,
            "sha256": self.sha256,
            "state": self.state,
        }


_BINDING_SCHEMA = "drawing-context-lightrag-bindings/1"


def _binding_path(repository_root: str | Path, target: str | Path | None = None, *, create_parent: bool = False) -> Path:
    root = Path(repository_root).resolve()
    runtime_root = (root / "runtime" / "lightrag").resolve()
    destination = runtime_root / "bindings.json" if target is None else Path(target).resolve()
    try:
        destination.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("LightRAG binding registry must remain under runtime/lightrag/") from exc
    if create_parent:
        destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


class LightRagBindingRegistry:
    def __init__(self):
        self._by_chunk: dict[str, LightRagBinding] = {}

    def bind(self, binding: LightRagBinding) -> None:
        current = self._by_chunk.get(binding.chunk_id)
        if current is not None and current != binding:
            raise ValueError("LightRAG chunk id is already bound to another projection")
        self._by_chunk[binding.chunk_id] = binding

    def get_chunk(self, chunk_id: str) -> LightRagBinding | None:
        return self._by_chunk.get(chunk_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(row) for row in sorted(self._by_chunk.values(), key=lambda row: row.chunk_id)]

    def save_runtime(self, repository_root: str | Path, target: str | Path | None = None) -> dict[str, Any]:
        destination = _binding_path(repository_root, target, create_parent=True)
        payload = {"schema": _BINDING_SCHEMA, "canonical": False, "bindings": self.snapshot()}
        temp = destination.with_name(destination.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, destination)
        return {"status": "SUCCESS", "path": str(destination), "count": len(payload["bindings"]), "canonical_mutation": False}

    @classmethod
    def load_runtime(cls, repository_root: str | Path, target: str | Path | None = None) -> "LightRagBindingRegistry":
        destination = _binding_path(repository_root, target)
        registry = cls()
        if not destination.exists():
            return registry
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("LightRAG binding registry contains invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema") != _BINDING_SCHEMA or payload.get("canonical") is not False:
            raise ValueError("unsupported or canonical LightRAG binding registry")
        rows = payload.get("bindings")
        if not isinstance(rows, list):
            raise ValueError("LightRAG binding registry requires a bindings array")
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError("LightRAG binding row must be an object")
            try:
                registry.bind(LightRagBinding(**raw))
            except TypeError as exc:
                raise ValueError("LightRAG binding row has an invalid shape") from exc
        return registry


JsonSender = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class UrllibJsonSender:
    def __init__(self, config: LightRagHttpConfig):
        self.config = config

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        payload = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/json", "X-API-Key": self.config.api_key}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict):
            raise RuntimeError("LightRAG returned a non-object JSON response")
        return value


class LightRagHttpAdapter:
    def __init__(
        self,
        config: LightRagHttpConfig,
        *,
        registry: LightRagBindingRegistry | None = None,
        sender: JsonSender | None = None,
    ):
        self.config = config
        self.registry = registry or LightRagBindingRegistry()
        self.sender = sender or UrllibJsonSender(config)
        self._auth_verified = False

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("LightRAG adapter is disabled")

    def verify_credentials(self) -> dict[str, Any]:
        self._require_enabled()
        result = self.sender("GET", "/auth/verify", None)
        self._auth_verified = True
        return {"status": "SUCCESS", "canonical_mutation": False, "provider": "lightrag", "response": result}

    def bind_projection(self, row: dict[str, Any], *, chunk_id: str) -> LightRagBinding:
        binding = LightRagBinding.from_projection(row, chunk_id=chunk_id)
        self.registry.bind(binding)
        return binding

    def _query_chunks(self, query: str, *, top_k: int, mode: str) -> list[dict[str, Any]]:
        self._require_enabled()
        if not self._auth_verified:
            self.verify_credentials()
        if not query.strip():
            raise ValueError("LightRAG query must not be empty")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if mode not in {"local", "global", "hybrid", "naive", "mix"}:
            raise ValueError("unsupported LightRAG query mode")
        response = self.sender(
            "POST",
            "/query/data",
            {
                "query": query,
                "mode": mode,
                "top_k": top_k,
                "chunk_top_k": top_k,
                "include_references": True,
            },
        )
        if response.get("status") != "success":
            raise RuntimeError(f"LightRAG query failed: {response.get('message', 'unknown failure')}")
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("chunks"), list):
            raise RuntimeError("LightRAG query/data response has no chunks array")
        return [row for row in data["chunks"] if isinstance(row, dict)][:top_k]

    def benchmark_search(self, query: str, *, top_k: int = 5, mode: str = "mix") -> dict[str, Any]:
        chunks = self._query_chunks(query, top_k=top_k, mode=mode)
        hits = []
        unmapped = 0
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id")
            binding = self.registry.get_chunk(chunk_id) if isinstance(chunk_id, str) else None
            if binding is None:
                unmapped += 1
                hits.append({
                    "external_id": f"lightrag:{chunk_id or 'unknown'}",
                    "content": chunk.get("content", ""),
                    "metadata": {},
                })
                continue
            hits.append({
                "external_id": binding.external_id,
                "content": chunk.get("content", ""),
                "metadata": binding.benchmark_metadata(),
            })
        return {
            "provider": "lightrag",
            "hits": hits,
            "unmapped_remote_hits": unmapped,
            "canonical_mutation": False,
        }

    def search(
        self,
        query: str,
        *,
        top_k: int,
        allowed_source_ids: set[str],
        current_revision_by_source: dict[str, str],
        mode: str = "mix",
    ) -> dict[str, Any]:
        chunks = self._query_chunks(query, top_k=top_k, mode=mode)
        hits = []
        blocked = {"unmapped": 0, "unauthorized": 0, "stale_revision": 0}
        for chunk in chunks:
            chunk_id = chunk.get("chunk_id")
            binding = self.registry.get_chunk(chunk_id) if isinstance(chunk_id, str) else None
            if binding is None:
                blocked["unmapped"] += 1
                continue
            if binding.source_id not in allowed_source_ids:
                blocked["unauthorized"] += 1
                continue
            if current_revision_by_source.get(binding.source_id) != binding.revision_id:
                blocked["stale_revision"] += 1
                continue
            hits.append({
                "external_id": binding.external_id,
                "content": chunk.get("content", ""),
                "metadata": binding.benchmark_metadata(),
            })
        return {
            "provider": "lightrag",
            "hits": hits,
            "blocked_remote_hits": blocked,
            "canonical_mutation": False,
        }

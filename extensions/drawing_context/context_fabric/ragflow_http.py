"""Version-pinned RAGFlow HTTP sidecar contract.

RAGFlow is never canonical. Remote dataset/document/chunk IDs are bound to
existing drawing-context projection IDs in a local derived registry. Search
results without a known binding stay untrusted so benchmark gates can reject
them instead of silently inventing provenance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ipaddress
import json
import re
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
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
class RagflowHttpConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    dataset_id: str = ""
    release: str = "v0.27.2"
    release_image_digest: str = ""
    timeout_seconds: float = 20.0

    def __post_init__(self):
        if not self.enabled:
            return
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RAGFlow base_url must be absolute http(s)")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if parsed.scheme != "https" and not loopback:
            raise ValueError("remote RAGFlow requires https")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in RAGFlow URL")
        if not self.api_key.strip():
            raise ValueError("enabled RAGFlow requires an API key")
        if not self.dataset_id.strip():
            raise ValueError("enabled RAGFlow requires a dataset_id")
        if self.release != "v0.27.2":
            raise ValueError("RAGFlow API profile is pinned to v0.27.2; review before upgrading")
        if not _IMAGE_DIGEST.fullmatch(self.release_image_digest):
            raise ValueError("enabled RAGFlow requires a pinned sha256 release image digest")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("RAGFlow timeout must be between 1 and 120 seconds")


@dataclass(frozen=True)
class RagflowBinding:
    external_id: str
    canonical_id: str
    source_id: str
    revision_id: str
    project_id: str
    sha256: str
    dataset_id: str
    document_id: str
    chunk_id: str

    @classmethod
    def from_projection(
        cls,
        row: dict[str, Any],
        *,
        dataset_id: str,
        document_id: str,
        chunk_id: str,
    ) -> "RagflowBinding":
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("projection row requires metadata")
        if any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in _REQUIRED_METADATA):
            raise ValueError("projection row lost required provenance metadata")
        if not _SHA256.fullmatch(metadata["sha256"]):
            raise ValueError("projection row has invalid sha256 metadata")
        external_id = row.get("external_id")
        if not isinstance(external_id, str) or not external_id:
            raise ValueError("projection row requires external_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError("RAGFlow response requires chunk id")
        return cls(
            external_id=external_id,
            canonical_id=metadata["canonical_id"],
            source_id=metadata["source_id"],
            revision_id=metadata["revision_id"],
            project_id=metadata["project_id"],
            sha256=metadata["sha256"],
            dataset_id=dataset_id,
            document_id=document_id,
            chunk_id=chunk_id,
        )

    def benchmark_metadata(self) -> dict[str, str]:
        return {
            "canonical_id": self.canonical_id,
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "project_id": self.project_id,
            "sha256": self.sha256,
            "state": "REMOTE_DERIVED",
        }


class RagflowBindingRegistry:
    """In-memory derived binding registry.

    Persistence, if added later, must live under runtime/ and remain rebuildable.
    """

    def __init__(self):
        self._by_chunk: dict[str, RagflowBinding] = {}
        self._by_external: dict[str, RagflowBinding] = {}

    def bind(self, binding: RagflowBinding) -> None:
        current_chunk = self._by_chunk.get(binding.chunk_id)
        if current_chunk is not None and current_chunk != binding:
            raise ValueError("remote chunk id is already bound to another projection")
        current_external = self._by_external.get(binding.external_id)
        if current_external is not None and current_external != binding:
            raise ValueError("projection external_id is already bound to another remote chunk")
        self._by_chunk[binding.chunk_id] = binding
        self._by_external[binding.external_id] = binding

    def get_chunk(self, chunk_id: str) -> RagflowBinding | None:
        return self._by_chunk.get(chunk_id)

    def remove_chunk(self, chunk_id: str) -> RagflowBinding | None:
        binding = self._by_chunk.pop(chunk_id, None)
        if binding is not None:
            self._by_external.pop(binding.external_id, None)
        return binding

    def for_source(self, source_id: str) -> list[RagflowBinding]:
        return sorted(
            (row for row in self._by_chunk.values() if row.source_id == source_id),
            key=lambda row: (row.document_id, row.chunk_id),
        )

    def for_revision(self, source_id: str, revision_id: str) -> list[RagflowBinding]:
        return [
            row for row in self.for_source(source_id)
            if row.revision_id == revision_id
        ]

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(row) for row in sorted(self._by_chunk.values(), key=lambda row: row.chunk_id)]


JsonSender = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


class UrllibJsonSender:
    def __init__(self, config: RagflowHttpConfig):
        self.config = config

    def __call__(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        base = self.config.base_url.rstrip("/") + "/"
        url = urljoin(base, path.lstrip("/"))
        payload = None if body is None else json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, method=method.upper(), headers=headers)
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"RAGFlow HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"RAGFlow connection failed: {exc.reason}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RAGFlow returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("RAGFlow response must be a JSON object")
        return value


class RagflowHttpAdapter:
    def __init__(
        self,
        config: RagflowHttpConfig,
        registry: RagflowBindingRegistry | None = None,
        sender: JsonSender | None = None,
    ):
        self.config = config
        self.registry = registry or RagflowBindingRegistry()
        self.sender = sender or UrllibJsonSender(config)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise RuntimeError("RAGFlow HTTP adapter is disabled")

    @staticmethod
    def _data(response: dict[str, Any]) -> Any:
        code = response.get("code", 0)
        if code not in {0, "0", None}:
            raise RuntimeError(f"RAGFlow API error {code}: {response.get('message')}")
        if "data" not in response:
            raise RuntimeError("RAGFlow response has no data field")
        return response["data"]

    def _add_one(self, document_id: str, row: dict[str, Any]) -> RagflowBinding:
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("projection row requires non-empty content")
        # Validate provenance before any network call.
        metadata = row.get("metadata")
        if not isinstance(metadata, dict) or any(
            not isinstance(metadata.get(key), str) or not metadata[key].strip()
            for key in _REQUIRED_METADATA
        ):
            raise ValueError("projection row lost required provenance metadata")
        if not _SHA256.fullmatch(metadata["sha256"]):
            raise ValueError("projection row has invalid sha256 metadata")

        path = (
            f"/api/v1/datasets/{self.config.dataset_id}/documents/"
            f"{document_id}/chunks"
        )
        response = self.sender(
            "POST",
            path,
            {
                "content": content,
                "important_keywords": [
                    metadata["canonical_id"],
                    metadata["source_id"],
                ],
            },
        )
        data = self._data(response)
        if isinstance(data, dict) and isinstance(data.get("chunk"), dict):
            chunk_id = data["chunk"].get("id")
        elif isinstance(data, dict):
            chunk_id = data.get("id")
        else:
            chunk_id = None
        binding = RagflowBinding.from_projection(
            row,
            dataset_id=self.config.dataset_id,
            document_id=document_id,
            chunk_id=chunk_id,
        )
        self.registry.bind(binding)
        return binding

    def add_projection(
        self,
        document_id: str,
        rows: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_enabled()
        if not document_id.strip():
            raise ValueError("document_id must be non-empty")
        created: list[RagflowBinding] = []
        try:
            for row in rows:
                created.append(self._add_one(document_id, row))
        except Exception:
            # Best-effort sidecar rollback. Canonical data was never touched.
            chunk_ids = [row.chunk_id for row in created]
            if chunk_ids:
                try:
                    self.delete_chunks(document_id, chunk_ids)
                except Exception:
                    pass
            raise
        return {
            "status": "SUCCESS",
            "created": [asdict(row) for row in created],
            "canonical_mutation": False,
        }

    def search(
        self,
        question: str,
        *,
        top_k: int = 5,
        use_kg: bool = False,
        metadata_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        if not question.strip():
            raise ValueError("question must be non-empty")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        body: dict[str, Any] = {
            "question": question,
            "page": 1,
            "page_size": top_k,
            "knn_top_k": max(top_k, 64),
            "use_kg": bool(use_kg),
        }
        if metadata_filter is not None:
            body["meta_data_filter"] = metadata_filter
        response = self.sender(
            "POST",
            f"/api/v1/datasets/{self.config.dataset_id}/search",
            body,
        )
        data = self._data(response)
        chunks = data.get("chunks") if isinstance(data, dict) else None
        if not isinstance(chunks, list):
            raise RuntimeError("RAGFlow search response has no chunks list")

        hits: list[dict[str, Any]] = []
        unmapped = 0
        for rank, chunk in enumerate(chunks[:top_k], 1):
            if not isinstance(chunk, dict):
                raise RuntimeError("RAGFlow search chunk must be an object")
            chunk_id = chunk.get("id") or chunk.get("chunk_id")
            binding = self.registry.get_chunk(chunk_id) if isinstance(chunk_id, str) else None
            content = (
                chunk.get("content")
                or chunk.get("content_with_weight")
                or chunk.get("text")
                or ""
            )
            if binding is None:
                unmapped += 1
                hits.append(
                    {
                        "external_id": f"ragflow-unmapped:{chunk_id or rank}",
                        "content": str(content),
                        "metadata": {},
                        "remote_unmapped": True,
                    }
                )
                continue
            hits.append(
                {
                    "external_id": binding.external_id,
                    "content": str(content),
                    "metadata": binding.benchmark_metadata(),
                    "remote_chunk_id": binding.chunk_id,
                }
            )
        return {
            "status": "SUCCESS",
            "provider": "ragflow",
            "release": self.config.release,
            "hits": hits,
            "unmapped_remote_hits": unmapped,
            "canonical_mutation": False,
        }

    def delete_chunks(
        self,
        document_id: str,
        chunk_ids: Iterable[str],
    ) -> dict[str, Any]:
        self._require_enabled()
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {
                "status": "SUCCESS",
                "deleted": 0,
                "canonical_mutation": False,
            }
        if any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in ids):
            raise ValueError("chunk ids must be non-empty strings")
        response = self.sender(
            "DELETE",
            (
                f"/api/v1/datasets/{self.config.dataset_id}/documents/"
                f"{document_id}/chunks"
            ),
            {"chunk_ids": ids},
        )
        self._data(response)
        for chunk_id in ids:
            self.registry.remove_chunk(chunk_id)
        return {
            "status": "SUCCESS",
            "deleted": len(ids),
            "canonical_mutation": False,
        }

    def replace_revision(
        self,
        document_id: str,
        *,
        source_id: str,
        old_revision_id: str,
        new_rows: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_enabled()
        rows = list(new_rows)
        if not rows:
            raise ValueError("revision replacement requires new projection rows")
        for row in rows:
            metadata = row.get("metadata") if isinstance(row, dict) else None
            if not isinstance(metadata, dict) or metadata.get("source_id") != source_id:
                raise ValueError("replacement rows must belong to the requested source")
            if metadata.get("revision_id") == old_revision_id:
                raise ValueError("replacement rows must describe a new revision")

        old = self.registry.for_revision(source_id, old_revision_id)
        if any(row.document_id != document_id for row in old):
            raise ValueError("old revision spans another RAGFlow document")
        created = self.add_projection(document_id, rows)["created"]
        if old:
            self.delete_chunks(document_id, [row.chunk_id for row in old])
        return {
            "status": "SUCCESS",
            "created": created,
            "deleted_old": len(old),
            "canonical_mutation": False,
        }

    def purge_source(self, source_id: str) -> dict[str, Any]:
        self._require_enabled()
        rows = self.registry.for_source(source_id)
        by_document: dict[str, list[str]] = {}
        for row in rows:
            by_document.setdefault(row.document_id, []).append(row.chunk_id)
        deleted = 0
        for document_id, chunk_ids in by_document.items():
            deleted += self.delete_chunks(document_id, chunk_ids)["deleted"]
        return {
            "status": "SUCCESS",
            "source_id": source_id,
            "deleted": deleted,
            "canonical_mutation": False,
        }

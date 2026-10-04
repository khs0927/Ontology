"""Embedding transport contract: fail closed, never relabel a batch to the hash model.

No socket is opened: ``embeddings._urlopen`` is monkeypatched with a recorder
that replays a scripted body. A configured endpoint that cannot produce usable
1024-dimensional vectors must raise, because the alternative is storing a
placeholder as if a model had produced it.
"""

from __future__ import annotations

import json
import math
import urllib.error
from pathlib import Path

import pytest

from aec_intelligence.operational import embeddings as emb
from aec_intelligence.operational.config import Settings

ENDPOINT = "http://embeddings:8080"  # compose service name: a local endpoint


def make_settings(tmp_path: Path, *, url: str = "", model: str = "BAAI/bge-m3") -> Settings:
    return Settings(dsn="postgresql://unused/unused", data_root=tmp_path, import_roots=(tmp_path,),
                    embedding_url=url, embedding_model=model)


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


class Recorder:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append({"url": req.full_url, "body": body, "timeout": timeout})
        result = self.responder(len(self.calls) - 1, body)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, str):
            payload = result.encode("utf-8")
        elif isinstance(result, (bytes, bytearray)):
            payload = bytes(result)
        else:
            payload = json.dumps(result).encode("utf-8")
        return FakeResponse(payload)


def install(monkeypatch, responder) -> Recorder:
    rec = Recorder(responder)
    monkeypatch.setattr(emb, "_urlopen", rec)
    return rec


def unit_vector(dim: int = emb.EMBEDDING_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[0] = 1.0
    return vec


def openai_body(vectors) -> dict:
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    for name in ("AEC_EMBEDDING_URL", "AEC_EMBEDDING_MODEL", "AEC_EMBEDDING_BATCH_SIZE",
                 "AEC_EMBEDDING_TIMEOUT", "AEC_EMBEDDING_RETRIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(emb, "_CIRCUIT", {})
    monkeypatch.setattr(emb.time, "sleep", lambda *_: None)
    yield
    emb._CIRCUIT.clear()


def test_offline_service_reports_the_hash_model_and_never_raises(tmp_path):
    service = emb.EmbeddingService(make_settings(tmp_path))
    assert service.active_model() == emb.HASH_MODEL
    model, vectors = service.embed_with_model(["벽체 Revit Wall"])
    assert model == emb.HASH_MODEL
    assert len(vectors) == 1 and len(vectors[0]) == emb.EMBEDDING_DIM
    assert vectors[0] == emb._deterministic_hash_vector("벽체 Revit Wall")


def test_configured_endpoint_failure_raises_instead_of_storing_hash_vectors(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: urllib.error.URLError("connection refused"))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT), retries=2)
    with pytest.raises(emb.EmbeddingEndpointError):
        service.embed_with_model(["문"])
    assert service.last_error and "connection refused" in service.last_error
    # the service keeps naming the configured model: the batch was never relabelled
    assert service.active_model() == "BAAI/bge-m3"


def test_open_circuit_raises_and_stops_sending_requests(monkeypatch, tmp_path):
    rec = install(monkeypatch, lambda i, body: urllib.error.URLError("down"))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT), retries=1)
    with pytest.raises(emb.EmbeddingEndpointError):
        service.embed_with_model(["a"])
    calls = len(rec.calls)
    with pytest.raises(emb.EmbeddingEndpointError, match="circuit open"):
        emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT)).embed_with_model(["b"])
    assert len(rec.calls) == calls


def test_dimension_mismatch_raises(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: openai_body([[0.5] * 768]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="768"):
        service.embed_with_model(["墙体"])


def test_vector_count_mismatch_raises(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: openai_body([unit_vector()]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="expected 2 vectors, got 1"):
        service.embed_with_model(["a", "b"])


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_non_finite_component_raises(monkeypatch, tmp_path, bad):
    vector = [0.0] * emb.EMBEDDING_DIM
    vector[3] = float(bad)
    install(monkeypatch, lambda i, body: json.dumps(openai_body([vector])))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="non-finite"):
        service.embed_with_model(["x"])


def test_zero_vector_raises(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: openai_body([[0.0] * emb.EMBEDDING_DIM]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="zero vector"):
        service.embed_with_model(["x"])


def test_non_unit_vector_is_kept_because_cosine_ignores_magnitude(monkeypatch, tmp_path):
    # aec.embeddings is HNSW-indexed with vector_cosine_ops and queried with <=>,
    # so rescaling a non-unit vector would be a silent transform of the model output.
    big = [0.0] * emb.EMBEDDING_DIM
    big[0] = 3.0
    install(monkeypatch, lambda i, body: openai_body([big]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    model, vectors = service.embed_with_model(["x"])
    assert model == "BAAI/bge-m3"
    assert vectors[0][0] == 3.0 and all(isinstance(v, float) for v in vectors[0])


def test_successful_call_returns_the_remote_model_and_float_vectors(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: openai_body([unit_vector() for _ in body["input"]]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    model, vectors = service.embed_with_model(["a", "b"])
    assert model == "BAAI/bge-m3" and len(vectors) == 2
    assert all(math.isfinite(v) for v in vectors[0])


def test_transport_failure_is_retried_before_it_raises(monkeypatch, tmp_path):
    def responder(i, body):
        if i < 2:
            return urllib.error.URLError("temporary")
        return openai_body([unit_vector() for _ in body["input"]])

    rec = install(monkeypatch, responder)
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT), retries=3, backoff=0.01)
    model, vectors = service.embed_with_model(["복구됨"])
    assert model == "BAAI/bge-m3" and len(vectors) == 1 and len(rec.calls) == 3


def test_empty_input_reports_active_model_without_a_request(monkeypatch, tmp_path):
    rec = install(monkeypatch, lambda i, body: pytest.fail("no request expected"))
    online = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    assert online.embed_with_model([]) == ("BAAI/bge-m3", [])
    offline = emb.EmbeddingService(make_settings(tmp_path))
    assert offline.embed_with_model([]) == (emb.HASH_MODEL, [])
    assert rec.calls == []


def test_non_local_endpoint_is_refused_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("AEC_EMBEDDING_ALLOW_REMOTE", raising=False)
    calls = []
    monkeypatch.setattr(emb, "_urlopen", lambda req, timeout=None: calls.append(req.full_url))
    service = emb.EmbeddingService(make_settings(tmp_path, url="https://api.example.com"), retries=1)
    with pytest.raises(emb.EmbeddingEndpointError, match="refusing non-local"):
        service.embed_batch(["2층 평면도"])
    assert calls == []  # nothing was sent


def test_local_endpoints_bypass_proxies():
    from aec_intelligence.operational import netguard

    assert netguard.is_local_endpoint("http://127.0.0.1:11434")
    assert netguard.is_local_endpoint("http://host.docker.internal:11434")
    assert netguard.is_local_endpoint("http://10.0.0.5:8080")
    assert not netguard.is_local_endpoint("http://127.0.0.1@8.8.8.8/")  # userinfo trick
    assert not netguard.is_local_endpoint("file:///etc/passwd")
    assert not netguard.is_local_endpoint("http://8.8.8.8/v1/embeddings")
    direct = netguard.opener_for("http://127.0.0.1:11434")
    assert not any(type(h).__name__ == "ProxyHandler" and h.proxies for h in direct.handlers)


def test_vector_that_rounds_to_fp16_zeros_is_rejected(monkeypatch, tmp_path):
    # float32 norm is ~3e-7, so every component rounds to 0 in halfvec: the row would
    # be stored as the zero vector this guard exists to refuse.
    tiny = [1e-8] * emb.EMBEDDING_DIM
    install(monkeypatch, lambda i, body: openai_body([tiny]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="fp16"):
        service.embed_with_model(["x"])


def test_component_that_overflows_fp16_is_rejected(monkeypatch, tmp_path):
    big = [0.0] * emb.EMBEDDING_DIM
    big[0] = 70000.0  # above the fp16 maximum of 65504
    install(monkeypatch, lambda i, body: openai_body([big]))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT))
    with pytest.raises(emb.EmbeddingEndpointError, match="fp16"):
        service.embed_with_model(["x"])


def test_ordinary_vector_survives_the_fp16_roundtrip():
    values = [0.5, -0.25, 1.0, 1e-3] + [0.0] * (emb.EMBEDDING_DIM - 4)
    halves = emb._halfvec_roundtrip(values)
    assert halves[:4] == pytest.approx(values[:4], rel=1e-3)
    assert sum(h * h for h in halves) > 0


def test_endpoint_health_reports_offline_degradation(tmp_path):
    assert emb.endpoint_health(make_settings(tmp_path)) == {
        "configured": False, "model": emb.HASH_MODEL, "circuit_open": False, "degraded": True}


def test_endpoint_health_flags_an_open_circuit(monkeypatch, tmp_path):
    install(monkeypatch, lambda i, body: urllib.error.URLError("down"))
    service = emb.EmbeddingService(make_settings(tmp_path, url=ENDPOINT), retries=1)
    with pytest.raises(emb.EmbeddingEndpointError):
        service.embed_with_model(["a"])
    assert emb.endpoint_health(make_settings(tmp_path, url=ENDPOINT)) == {
        "configured": True, "model": "BAAI/bge-m3", "circuit_open": True, "degraded": True}


def test_healthz_surfaces_the_embedding_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from aec_intelligence.operational import auth
    from aec_intelligence.operational.api import create_app

    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    client = TestClient(create_app(make_settings(tmp_path)))
    body = client.get("/healthz").json()
    assert body["status"] == "ok"  # liveness is unchanged: a degraded stage must not restart the API
    assert body["embeddings"]["degraded"] is True
    assert body["embeddings"]["model"] == emb.HASH_MODEL

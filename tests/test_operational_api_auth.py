"""Opt-in bearer-token auth for the operational API (AEC_API_TOKEN). No PostgreSQL needed."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aec_intelligence.operational import api as api_module
from aec_intelligence.operational.auth import (
    BearerTokenMiddleware,
    api_token_from_env,
    is_open_path,
)
from aec_intelligence.operational.config import Settings

TOKEN = "correct-horse-battery-staple"


class FakeDatabase:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connect(self):
        raise RuntimeError("db reached")  # marks "request got past auth"
        yield


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None, cors: str | None = None):
    monkeypatch.setattr(api_module, "Database", FakeDatabase)
    if token is None:
        monkeypatch.delenv("AEC_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AEC_API_TOKEN", token)
    if cors is None:
        monkeypatch.delenv("AEC_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("AEC_CORS_ORIGINS", cors)
    settings = Settings(dsn="dummy", data_root=tmp_path / "data", import_roots=(tmp_path.resolve(),))
    return TestClient(api_module.create_app(settings), raise_server_exceptions=False)


def test_without_token_env_api_stays_open(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, None)
    # Past auth: the fake DB raises → 500, not 401.
    assert client.get("/v1/stats").status_code == 500
    assert client.get("/healthz").status_code == 200


def test_blank_token_env_is_treated_as_unset(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, "   ")
    assert client.get("/v1/stats").status_code == 500


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": "Bearer"},
        {"Authorization": TOKEN},
    ],
)
def test_token_required_when_configured(tmp_path, monkeypatch, headers):
    client = _client(tmp_path, monkeypatch, TOKEN)
    for method, path in [("GET", "/v1/stats"), ("POST", "/v1/search"), ("POST", "/v1/ingestions"),
                         ("GET", "/v1/elements"), ("POST", "/v1/reviews")]:
        res = client.request(method, path, headers=headers, json={})
        assert res.status_code == 401, (method, path, res.status_code)
        assert res.json() == {"detail": "Missing or invalid bearer token"}
        assert res.headers["www-authenticate"].startswith("Bearer")


def test_correct_token_passes(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, f"  {TOKEN}\n")  # env value is trimmed
    assert client.get("/v1/stats", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 500
    assert client.get("/v1/stats", headers={"Authorization": f"bearer   {TOKEN} "}).status_code == 500
    # Validation still runs after auth (no DB needed for a bad body).
    res = client.post("/v1/ingestions", headers={"Authorization": f"Bearer {TOKEN}"}, json={"path": "x", "queue": "nope"})
    assert res.status_code == 422


def test_health_and_dashboard_shell_stay_open(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, TOKEN)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/dashboard").status_code == 200
    assert "apiFetch" in client.get("/").text


def test_cors_preflight_is_not_blocked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, TOKEN, cors="http://localhost:5173")
    res = client.options(
        "/v1/stats",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "authorization"},
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
    unauth = client.get("/v1/stats", headers={"Origin": "http://localhost:5173"})
    assert unauth.status_code == 401


def test_helpers():
    assert api_token_from_env("") is None
    assert api_token_from_env(" t ") == "t"
    assert is_open_path("/static/app.js") and is_open_path("/healthz")
    assert not is_open_path("/v1/stats") and not is_open_path("/staticx") and not is_open_path("/healthz/x")
    with pytest.raises(ValueError):
        BearerTokenMiddleware(lambda *a: None, "")


def test_power_cad_header_format_matches(tmp_path, monkeypatch):
    """power-cad-mcp (Python urllib and C# HttpClient) sends exactly ``Authorization: Bearer <token>``."""
    client = _client(tmp_path, monkeypatch, TOKEN)
    res = client.get("/v1/elements", headers={"Authorization": "Bearer " + TOKEN, "User-Agent": "power-cad-mcp"})
    assert res.status_code != 401


def test_websocket_scope_requires_the_token():
    import asyncio

    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    mw = BearerTokenMiddleware(app, token=TOKEN)
    asyncio.run(mw({"type": "websocket", "path": "/ws", "headers": []}, receive, send))
    assert reached == [] and sent == [{"type": "websocket.close", "code": 1008}]
    good = [(b"authorization", f"Bearer {TOKEN}".encode())]
    asyncio.run(mw({"type": "websocket", "path": "/ws", "headers": good}, receive, send))
    assert reached == ["websocket"]

"""Opt-in bearer-token authentication for the operational REST API.

``AEC_API_TOKEN`` unset or blank → no authentication (previous behaviour; keep the API on loopback).
``AEC_API_TOKEN`` set → every request needs ``Authorization: Bearer <token>`` except the liveness
probe and the static dashboard shell (``/healthz``, ``/``, ``/dashboard``, ``/static/*``), which hold no
data. The dashboard asks for the token and sends it on its API calls. CORS preflight (``OPTIONS``)
is passed through so the CORS middleware can answer it.

power-cad-mcp sends ``POWERCAD_ONTOLOGY_TOKEN`` in exactly this header format, so set both variables
to the same value.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

TOKEN_ENV = "AEC_API_TOKEN"
OPEN_PATHS = frozenset({"/healthz", "/", "/dashboard"})
OPEN_PREFIXES = ("/static/",)

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def api_token_from_env(value: str | None = None) -> str | None:
    raw = os.getenv(TOKEN_ENV) if value is None else value
    token = (raw or "").strip()
    return token or None


def _bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, raw in headers:
        if name.lower() == b"authorization":
            scheme, _, credentials = raw.decode("latin-1").strip().partition(" ")
            if scheme.lower() == "bearer" and credentials.strip():
                return credentials.strip()
            return None
    return None


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


class BearerTokenMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware) so streaming/background tasks are unaffected."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("BearerTokenMiddleware needs a non-empty token")
        self.app = app
        self._expected = token.encode("utf-8")

    def _authorized(self, scope: Scope) -> bool:
        presented = _bearer(scope.get("headers") or [])
        if presented is None:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self._expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind == "websocket":
            # No websocket routes today; any future one is covered by the same token instead of open.
            if self._authorized(scope):
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        if kind != "http" or scope.get("method") == "OPTIONS" or is_open_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": "Missing or invalid bearer token"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer realm="aec-intelligence"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

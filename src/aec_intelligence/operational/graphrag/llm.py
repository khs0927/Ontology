"""Local LLM client (Ollama /api/chat). Drawings are private, so only a local endpoint is allowed by default.

``AEC_LLM_URL`` (default http://127.0.0.1:11434), ``AEC_LLM_MODEL`` (default qwen3:8b),
``AEC_LLM_TIMEOUT_SECONDS`` (default 120), ``AEC_LLM_KEEP_ALIVE`` (default 10m: how long Ollama keeps the
model loaded after the last call; 5 GB of RAM/VRAM on a 15 GB PC), ``AEC_LLM_LOAD_TIMEOUT_SECONDS``
(default 600, for ``warm()``: a cold load can take minutes under memory pressure). A non-loopback / non-private host is refused unless
``AEC_LLM_ALLOW_REMOTE=1`` (explicit opt-in), so a typo can never ship drawing text to a cloud service.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from ..netguard import LOCAL_HOSTNAMES, is_local_endpoint  # noqa: F401  (re-exported)

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    timeout: float = 120.0
    allow_remote: bool = False
    # One fixed context size for every call: Ollama reloads the model when num_ctx changes.
    num_ctx: int = 6144
    keep_alive: str = "10m"
    load_timeout: float = 600.0

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(url=os.getenv("AEC_LLM_URL") or DEFAULT_URL, model=os.getenv("AEC_LLM_MODEL") or DEFAULT_MODEL,
                   timeout=float(os.getenv("AEC_LLM_TIMEOUT_SECONDS") or 120),
                   num_ctx=int(os.getenv("AEC_LLM_NUM_CTX") or 6144),
                   keep_alive=os.getenv("AEC_LLM_KEEP_ALIVE") or "10m",
                   load_timeout=float(os.getenv("AEC_LLM_LOAD_TIMEOUT_SECONDS") or 600),
                   allow_remote=os.getenv("AEC_LLM_ALLOW_REMOTE", "").strip().lower() in {"1", "true", "yes"})


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LocalLLM:
    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()
        if not self.config.allow_remote and not is_local_endpoint(self.config.url):
            raise LLMError(f"refusing non-local LLM endpoint {urlparse(self.config.url).hostname!r} "
                           "(set AEC_LLM_ALLOW_REMOTE=1 to opt in)")

    @property
    def model(self) -> str:
        return self.config.model

    def warm(self) -> float:
        """Load the model (same num_ctx as chat, so no reload follows) and return the seconds it took.

        Batch jobs call this once with the long load timeout; otherwise the first chat of a run waits
        for a cold load inside the normal per-call timeout and fails on a busy PC.
        """
        body = {"model": self.config.model, "prompt": "", "stream": False, "keep_alive": self.config.keep_alive,
                "options": {"num_ctx": self.config.num_ctx}}
        req = urllib.request.Request(self.config.url.rstrip("/") + "/api/generate",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.config.load_timeout) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"LLM model load failed: {exc}") from exc
        return round(time.monotonic() - started, 2)

    def chat(self, system: str, user: str, *, max_tokens: int = 700, temperature: float = 0.0) -> dict:
        """One non-streaming chat turn; returns {"text", "model", "seconds", "eval_count"}."""
        body = {
            "model": self.config.model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": self.config.num_ctx, "seed": 7},
            "keep_alive": self.config.keep_alive,
        }
        req = urllib.request.Request(self.config.url.rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        started = time.monotonic()
        try:
            # No proxy handler: a system/env HTTP proxy must never see drawing text bound for the local LLM.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.config.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM endpoint failed: {exc}") from exc
        text = _THINK_RE.sub("", (data.get("message") or {}).get("content") or "").strip()
        return {"text": text, "model": data.get("model") or self.config.model,
                "seconds": round(time.monotonic() - started, 2), "eval_count": data.get("eval_count")}

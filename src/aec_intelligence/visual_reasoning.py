"""Read-only NVIDIA Cosmos visual observations for AEC artifacts.

This module intentionally produces advisory evidence only. It never mutates
canonical CAIR, source artifacts, legal authority, or parser classifications.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1/chat/completions"
DEFAULT_MODEL = "nvidia/cosmos-reason2-2b"


class NvidiaCosmosVision:
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 1400,
        timeout: float = 90.0,
    ):
        self.endpoint = endpoint
        self.api_key = (api_key or "").strip()
        self.model = model
        self.max_tokens = max(128, min(int(max_tokens), 4096))
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "NvidiaCosmosVision":
        endpoint = os.getenv("NVIDIA_COSMOS_ENDPOINT", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT
        configured_model = os.getenv("NVIDIA_COSMOS_MODEL", "").strip()
        if configured_model:
            model = configured_model
        elif "integrate.api.nvidia.com" in endpoint.lower():
            model = "nvidia/cosmos-reason2-8b"
        else:
            model = DEFAULT_MODEL
        api_key = os.getenv("NVIDIA_API_KEY", "").strip() or None
        try:
            max_tokens = int(os.getenv("NVIDIA_COSMOS_MAX_TOKENS", "1400"))
        except ValueError:
            max_tokens = 1400
        return cls(endpoint=endpoint, api_key=api_key, model=model, max_tokens=max_tokens)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self.endpoint.startswith("http://127.0.0.1") or self.endpoint.startswith("http://localhost")

    def analyze_image(self, image: bytes, mime_type: str, prompt: str) -> dict[str, Any]:
        if not self.enabled:
            return {
                "status": "REQUIRES_CONFIGURATION",
                "provider": "nvidia",
                "model": self.model,
                "error": "Start the Cosmos Reason2 NIM locally, or configure NVIDIA_COSMOS_ENDPOINT for a remote NIM. NVIDIA_API_KEY is only bearer auth for remote endpoints.",
            }

        data_uri = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a read-only AEC visual observation engine. "
                        "Return only final structured JSON. Do not authorize mutations, "
                        "do not invent hidden geometry, and do not turn visual guesses into canonical truth."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"NVIDIA API HTTP {exc.code}: {detail[:800]}") from exc
        except urlerror.URLError as exc:
            raise RuntimeError(f"NVIDIA API unavailable: {exc.reason}") from exc

        root = json.loads(response_body)
        choices = root.get("choices") or []
        content = ((choices[0].get("message") or {}).get("content") if choices else None)
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("NVIDIA API response did not contain assistant content")
        return parse_final_json(content)


def parse_final_json(text: str) -> dict[str, Any]:
    """Discard model reasoning traces and retain only final evidence."""

    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first >= 0 and last > first:
        try:
            value = json.loads(cleaned[first : last + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    return {"summary": cleaned}


def prepare_visual_input(source: Path, preview: Path | None = None) -> tuple[bytes, str, Path]:
    """Rasterize one visual artifact without changing the source."""

    target = preview if preview is not None and preview.is_file() else source
    suffix = target.suffix.lower()
    mime_by_suffix = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    if suffix in mime_by_suffix:
        return target.read_bytes(), mime_by_suffix[suffix], target

    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PDF visual validation requires the 'vision' or 'pdf' extra (PyMuPDF)") from exc
        with fitz.open(target) as document:
            if not document.page_count:
                raise RuntimeError("PDF has no pages")
            page = document.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            return pix.tobytes("png"), "image/png", target

    if suffix == ".svg":
        try:
            import cairosvg
        except ImportError as exc:
            raise RuntimeError("SVG/CAD-preview visual validation requires the 'vision' extra (CairoSVG)") from exc
        return cairosvg.svg2png(bytestring=target.read_bytes()), "image/png", target

    raise RuntimeError(f"no visual adapter for {target.suffix or '<no extension>'}")


def architectural_object_prompt(source_name: str, context: str | None = None) -> str:
    return f"""
Inspect this AEC drawing/image as visual evidence only.
Source: {source_name}
Context: {context or "Google Drive ingestion validation"}

Return JSON only:
{{
  "document_type": "floor_plan|section|elevation|detail|schedule|site_plan|photo|unknown",
  "summary": "short final observation",
  "objects": [
    {{
      "object_type": "Wall|Door|Window|Column|Beam|Slab|Stair|Room|Grid|Dimension|Text|Hatch|Furniture|Equipment|Other",
      "label": "visible label/name if any",
      "bbox_norm": [0.0,0.0,1.0,1.0],
      "confidence": 0.0,
      "visible_evidence": "what is visible"
    }}
  ],
  "candidate_relations": [
    {{
      "subject_index": 0,
      "predicate": "adjacent_to|inside|opens_to|aligned_with|overlaps|supports|unknown",
      "object_index": 1,
      "confidence": 0.0,
      "visible_evidence": "visible basis"
    }}
  ],
  "quality_issues": [
    {{
      "kind": "illegible|overlap|cropped|low_resolution|ambiguous|other",
      "description": "visible issue"
    }}
  ]
}}

bbox_norm is [x_min,y_min,x_max,y_max] normalized to 0..1 image coordinates.
Do not infer hidden topology, exact dimensions, legal compliance, or authoritative object identity.
When uncertain, lower confidence and use Other/unknown.
"""


def image_sha256(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()

"""Small, versioned contracts between canonical sources, search and Power CAD."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class SourceRevision:
    account: str
    corpus: str
    file_id: str
    project_id: str
    revision: str
    sha256: str
    name: str
    format: str
    parser: str
    parser_version: str
    units: str

    def __post_init__(self):
        for key, value in asdict(self).items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SourceRevision.{key} must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("sha256 must describe captured bytes, not a folder name or Drive version")

    @property
    def source_id(self):
        # A source may have separate authorized memberships; do not dedup permissions.
        return digest([self.account, self.corpus, self.file_id, self.project_id])

    @property
    def revision_id(self):
        return digest([self.source_id, self.revision, self.sha256,
                       self.parser, self.parser_version])

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class EmbeddingSpace:
    model: str
    model_revision: str
    dimensions: int
    normalization: str
    modality: str

    def __post_init__(self):
        if not self.model or not self.model_revision or type(self.dimensions) is not int or self.dimensions < 1:
            raise ValueError("A pinned model revision and positive dimensions are mandatory")
        if self.normalization not in {"l2", "none"} or self.modality not in {"text", "image"}:
            raise ValueError("Unknown embedding normalization or modality")

    @property
    def namespace(self):
        return digest(asdict(self))

    def require_same(self, other):
        if self != other:
            raise ValueError("Embedding space mismatch: reindex instead of padding or silently falling back")


def verify_live_candidate(candidate, live):
    """Read-only guard; caller supplies fresh native observations from a trusted adapter.

    This does not execute CAD and is not an authorization token for mutation.
    A full handle-to-DWG mapping must be independently established by the native adapter.
    """
    locator = candidate["locator"]
    source = candidate["source"]
    reasons = []
    for key in ("source_id", "revision", "sha256", "layout", "handle", "instance_path"):
        expected = source.get(key) if key in {"revision", "sha256"} else locator.get(key)
        if expected is None or live.get(key) != expected:
            reasons.append(f"mismatch:{key}")
    if not locator.get("handle") or not locator.get("layout"):
        reasons.append("missing_cad_locator")
    if source["format"].lower() not in {"dwg", "dxf"}:
        reasons.append("not_cad_source")
    if live.get("native_mapping_verified") is not True:
        reasons.append("native_mapping_not_verified")
    if live.get("document_dirty") is not False:
        reasons.append("dirty_or_unknown_document")
    if source["units"] == "unknown" or live.get("units") != source["units"]:
        reasons.append("unknown_or_mismatched_units")
    if not live.get("fingerprint"):
        reasons.append("missing_live_fingerprint")
    return {
        "schema": "power-cad-context-validation/1",
        "status": "VERIFIED_FOR_REVIEW" if not reasons else "REQUIRES_REVIEW",
        "reasons": reasons,
        "candidate_id": candidate["id"],
        "fingerprint": live.get("fingerprint") if not reasons else None,
        "may_execute_mutation": False,
    }

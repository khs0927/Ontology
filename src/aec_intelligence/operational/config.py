from dataclasses import dataclass
from pathlib import Path
import os


def split_roots(value: str, pathsep: str | None = None) -> list[str]:
    """Split AEC_IMPORT_ROOTS on ';' and os.pathsep, never breaking Windows drive letters (``C:\\``, ``C:/``)."""
    seps = {";", pathsep or os.pathsep}
    parts, current = [], ""
    for i, ch in enumerate(value):
        is_drive = ch == ":" and len(current.strip()) == 1 and current.strip().isalpha() and value[i + 1:i + 2] in ("\\", "/")
        if ch in seps and not is_drive:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer setting from the environment; a malformed or too-small value is a startup error.

    Silently falling back would hide a typo such as ``AEC_LEASE_SECONDS=5m`` until a lease expired.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    dsn: str
    data_root: Path
    import_roots: tuple[Path, ...]
    embedding_url: str = ""
    rag_url: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = ""
    oda_executable: str = ""
    lease_seconds: int = 300
    max_attempts: int = 3
    dwg_converter: str = "auto"
    libredwg_executable: str = ""
    # Long SQL/AGE projection statements for large drawings need more headroom than interactive queries.
    ingest_statement_timeout_seconds: int = 300
    # Embedding endpoint failure during ingest: False (default) stores objects and leaves their vectors
    # pending for `aec operational reembed`; True fails the job (the pre-2026-10 behaviour).
    embedding_strict: bool = False
    # DWG -> DXF conversions are cached by source sha256 so re-ingest/retry never converts twice.
    dxf_cache_dir: Path | None = None
    oda_timeout_seconds: int = 900

    @classmethod
    def from_env(cls):
        root = Path(os.getenv("AEC_DATA_ROOT", "D:/AECData" if os.name == "nt" else "/data")).resolve()
        return cls(
            os.getenv("AEC_DATABASE_URL", "postgresql://aec:change-me@localhost:55432/aec"), root,
            tuple(Path(p).resolve() for p in split_roots(os.getenv("AEC_IMPORT_ROOTS", str(root / "imports")))),
            os.getenv("AEC_EMBEDDING_URL", ""), os.getenv("AEC_RAG_URL", ""),
            os.getenv("AEC_EMBEDDING_MODEL", "BAAI/bge-m3"), os.getenv("AEC_EMBEDDING_REVISION", ""),
            os.getenv("AEC_ODA_EXECUTABLE", ""),
            dwg_converter=os.getenv("AEC_DWG_CONVERTER", "auto"),
            libredwg_executable=os.getenv("AEC_LIBREDWG_EXECUTABLE", ""),
            lease_seconds=env_int("AEC_LEASE_SECONDS", 300, minimum=15),
            max_attempts=env_int("AEC_MAX_ATTEMPTS", 3, minimum=1),
            ingest_statement_timeout_seconds=env_int("AEC_INGEST_STATEMENT_TIMEOUT_SECONDS", 300, minimum=0),
            embedding_strict=env_flag("AEC_EMBEDDING_STRICT"),
            dxf_cache_dir=Path(os.getenv("AEC_DXF_CACHE_DIR") or (root / "dxf-cache")).resolve(),
            oda_timeout_seconds=env_int("AEC_ODA_TIMEOUT_SECONDS", 900, minimum=10),
        )

    def dxf_cache(self) -> Path:
        return self.dxf_cache_dir or (self.data_root / "dxf-cache")

    def allowed_source(self, value: str) -> Path:
        path = Path(value).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in self.import_roots):
            raise ValueError("Source is outside configured import roots")
        return path

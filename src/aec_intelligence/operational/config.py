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
        )

    def allowed_source(self, value: str) -> Path:
        path = Path(value).resolve(strict=True)
        if not any(path.is_relative_to(root) for root in self.import_roots):
            raise ValueError("Source is outside configured import roots")
        return path

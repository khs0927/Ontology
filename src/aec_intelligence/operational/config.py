from dataclasses import dataclass
from pathlib import Path
import os


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
            tuple(Path(p).resolve() for p in os.getenv("AEC_IMPORT_ROOTS", str(root / "imports")).split(";") if p),
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

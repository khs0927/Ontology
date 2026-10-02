"""Safe ODA DWG conversion adapter; no converter is bundled or assumed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


@dataclass
class DWGConversionResult:
    status: str
    source: str
    output: str | None
    converter: str
    errors: list[str]
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ODAConverter:
    """Run ODA only when explicitly configured; never overwrite the source."""

    name = "ODA File Converter"

    def __init__(self, executable: str | Path | None = None):
        self.executable = Path(executable).resolve() if executable else None

    @staticmethod
    def _requires_ascii_staging(*paths: Path) -> bool:
        """Return whether the Windows ODA process needs ASCII-only staging."""
        return any(str(path).encode("ascii", errors="ignore") != str(path).encode("utf-8") for path in paths)

    def convert_to_dxf(self, source: str | Path, output_dir: str | Path) -> DWGConversionResult:
        source_path = Path(source).resolve()
        target_dir = Path(output_dir).resolve()
        if source_path.suffix.lower() != ".dwg":
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["source is not a .dwg file"], {})
        if not source_path.is_file():
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["source file does not exist"], {})
        executable = self.executable or shutil.which("ODAFileConverter")
        if not executable:
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["ODAFileConverter is not configured or installed"], {})
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_path.stem}.dxf"
        if output_path.resolve() == source_path:
            raise ValueError("converter output cannot overwrite source")
        stage_root: Path | None = None
        run_source = source_path
        run_output_dir = target_dir
        if self._requires_ascii_staging(source_path, target_dir):
            # ODA 27.1.0 on Windows can return success while silently
            # skipping paths containing non-ASCII characters.  Stage both
            # input and output, then copy only the verified result back.
            stage_root = Path(tempfile.mkdtemp(prefix="aec-oda-"))
            run_source_dir = stage_root / "input"
            run_output_dir = stage_root / "output"
            run_source_dir.mkdir()
            run_output_dir.mkdir()
            run_source = run_source_dir / "source.dwg"
            shutil.copy2(source_path, run_source)
        command = [str(executable), str(run_source.parent), str(run_output_dir), "ACAD2018", "DXF", "0", "1"]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True, errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            if stage_root:
                shutil.rmtree(stage_root, ignore_errors=True)
            return DWGConversionResult("FAILED", str(source_path), None, self.name, [str(exc)], {"command": command})
        checks = {"returncode": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]}
        produced_path = run_output_dir / f"{run_source.stem}.dxf"
        if stage_root and completed.returncode == 0 and produced_path.is_file() and produced_path.stat().st_size > 0:
            shutil.copy2(produced_path, output_path)
        checks["ascii_staging"] = stage_root is not None
        checks["produced_path"] = str(produced_path)
        success = completed.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0
        if stage_root:
            shutil.rmtree(stage_root, ignore_errors=True)
        if not success:
            return DWGConversionResult("FAILED", str(source_path), str(output_path), self.name, ["ODA conversion did not produce a non-empty DXF"], checks)
        checks.update({"output_size": output_path.stat().st_size})
        return DWGConversionResult("SUCCESS", str(source_path), str(output_path), self.name, [], checks)


class LibreDWGConverter:
    """No-CAD fallback using GNU LibreDWG ``dwg2dxf``; never overwrites the source.

    LibreDWG output is lower fidelity than ODA (some objects/proxies may be
    skipped), so callers should prefer ODA when it is configured.
    """

    name = "LibreDWG dwg2dxf"

    def __init__(self, executable: str | Path | None = None):
        self.executable = Path(executable).resolve() if executable else None

    def resolve_executable(self) -> str | None:
        if self.executable:
            return str(self.executable) if self.executable.is_file() else None
        return shutil.which("dwg2dxf")

    def convert_to_dxf(self, source: str | Path, output_dir: str | Path) -> DWGConversionResult:
        source_path = Path(source).resolve()
        target_dir = Path(output_dir).resolve()
        if source_path.suffix.lower() != ".dwg":
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["source is not a .dwg file"], {})
        if not source_path.is_file():
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["source file does not exist"], {})
        executable = self.resolve_executable()
        if not executable:
            return DWGConversionResult("FAILED", str(source_path), None, self.name, ["dwg2dxf (LibreDWG) is not configured or installed"], {})
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_path.stem}.dxf"
        if output_path.resolve() == source_path:
            raise ValueError("converter output cannot overwrite source")
        if output_path.exists():
            output_path.unlink()
        command = [executable, "-y", "-o", str(output_path), str(source_path)]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True, errors="replace", timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return DWGConversionResult("FAILED", str(source_path), None, self.name, [str(exc)], {"command": command})
        checks: dict[str, Any] = {"returncode": completed.returncode, "stdout": completed.stdout[-2000:], "stderr": completed.stderr[-2000:]}
        # dwg2dxf can return non-zero on recoverable read warnings while still
        # writing a usable DXF; success requires a non-empty file ending in EOF.
        success = output_path.is_file() and output_path.stat().st_size > 0 and _dxf_has_eof(output_path)
        if not success:
            return DWGConversionResult("FAILED", str(source_path), str(output_path), self.name, ["LibreDWG conversion did not produce a complete DXF"], checks)
        checks["output_size"] = output_path.stat().st_size
        return DWGConversionResult("SUCCESS", str(source_path), str(output_path), self.name, [], checks)


def _dxf_has_eof(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 64))
        return b"EOF" in handle.read()


def select_dwg_converter(mode: str = "auto", oda_executable: str | Path | None = None, libredwg_executable: str | Path | None = None):
    """Pick a DWG converter. ``auto`` prefers ODA and falls back to LibreDWG."""
    mode = (mode or "auto").strip().lower()
    if mode == "oda":
        return ODAConverter(oda_executable)
    if mode == "libredwg":
        return LibreDWGConverter(libredwg_executable)
    if mode != "auto":
        raise ValueError(f"unknown DWG converter mode: {mode!r} (expected auto, oda or libredwg)")
    if oda_executable or shutil.which("ODAFileConverter"):
        return ODAConverter(oda_executable)
    libre = LibreDWGConverter(libredwg_executable)
    if libre.resolve_executable():
        return libre
    return ODAConverter(oda_executable)

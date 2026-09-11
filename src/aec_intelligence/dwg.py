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
            completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=300)
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

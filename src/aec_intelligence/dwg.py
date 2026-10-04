"""Safe ODA DWG conversion adapter; no converter is bundled or assumed."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
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

    def __init__(self, executable: str | Path | None = None, timeout_seconds: int = 900,
                 output_version: str = "ACAD2018"):
        self.executable = Path(executable).resolve() if executable else None
        self.timeout_seconds = int(timeout_seconds)
        self.output_version = output_version

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
            return DWGConversionResult("FAILED", str(source_path), None, self.name, [
                "ODAFileConverter is not configured or installed. DWG needs conversion on the host: run the "
                "host worker (scripts/ops/run-pipeline.ps1) with AEC_ODA_EXECUTABLE set, or pre-fill the DXF "
                "cache with `python -m aec_intelligence.operational.cli convert-dwg`, then retry the job"], {})
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / f"{source_path.stem}.dxf"
        if output_path.resolve() == source_path:
            raise ValueError("converter output cannot overwrite source")
        # ODA converts *every* drawing in its input folder (the CLI has no single-file mode, and the
        # filter argument is a wildcard). Running it on the source's own folder converted all of a
        # Drive folder's DWGs for each job. The source is therefore always staged alone in a temp
        # folder; this also covers ODA 27.1.0 silently skipping non-ASCII paths on Windows.
        stage_root = Path(tempfile.mkdtemp(prefix="aec-oda-"))
        try:
            run_source_dir = stage_root / "input"
            run_output_dir = stage_root / "output"
            run_source_dir.mkdir()
            run_output_dir.mkdir()
            run_source = run_source_dir / "source.dwg"
            shutil.copy2(source_path, run_source)
            command = [str(executable), str(run_source_dir), str(run_output_dir), self.output_version, "DXF", "0", "1",
                       "*.DWG"]
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True, errors="replace",
                                           timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                return DWGConversionResult("FAILED", str(source_path), None, self.name, [
                    f"ODA conversion exceeded {self.timeout_seconds}s (AEC_ODA_TIMEOUT_SECONDS)"], {"command": command})
            except (OSError, subprocess.SubprocessError) as exc:
                return DWGConversionResult("FAILED", str(source_path), None, self.name, [str(exc)], {"command": command})
            checks = {"returncode": completed.returncode, "stdout": completed.stdout[-2000:],
                      "stderr": completed.stderr[-2000:], "ascii_staging": True, "staged": True,
                      "output_version": self.output_version}
            produced_path = run_output_dir / "source.dxf"
            checks["produced_path"] = str(produced_path)
            if completed.returncode == 0 and produced_path.is_file() and produced_path.stat().st_size > 0:
                shutil.copy2(produced_path, output_path)
            else:
                return DWGConversionResult("FAILED", str(source_path), str(output_path), self.name,
                                           ["ODA conversion did not produce a non-empty DXF"], checks)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
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


def select_dwg_converter(mode: str = "auto", oda_executable: str | Path | None = None,
                         libredwg_executable: str | Path | None = None, oda_timeout_seconds: int = 900):
    """Pick a DWG converter. ``auto`` prefers ODA and falls back to LibreDWG."""
    mode = (mode or "auto").strip().lower()
    if mode == "oda":
        return ODAConverter(oda_executable, oda_timeout_seconds)
    if mode == "libredwg":
        return LibreDWGConverter(libredwg_executable)
    if mode != "auto":
        raise ValueError(f"unknown DWG converter mode: {mode!r} (expected auto, oda or libredwg)")
    if oda_executable or shutil.which("ODAFileConverter"):
        return ODAConverter(oda_executable, oda_timeout_seconds)
    libre = LibreDWGConverter(libredwg_executable)
    if libre.resolve_executable():
        return libre
    return ODAConverter(oda_executable, oda_timeout_seconds)


def _converter_key(converter) -> str:
    """Cache key part: converter family and output format (ODA and LibreDWG output differ)."""
    if isinstance(converter, ODAConverter):
        return f"oda-{converter.output_version.lower()}"
    if isinstance(converter, LibreDWGConverter):
        return "libredwg"
    return type(converter).__name__.lower()


def cached_dxf_path(cache_dir: str | Path, source_sha256: str, key: str) -> Path:
    digest = source_sha256.lower()
    return Path(cache_dir) / digest[:2] / f"{digest}.{key}.dxf"


def convert_dwg_cached(converter, source: str | Path, output_dir: str | Path, cache_dir: str | Path | None,
                       source_sha256: str | None) -> DWGConversionResult:
    """Convert through a content-addressed cache: ``<cache>/<sha[:2]>/<sha>.<converter>.dxf``.

    A hit returns the cached DXF without running the converter (retries, re-ingest, the same drawing
    copied into several folders). A miss converts into ``output_dir`` and then publishes the DXF to
    the cache atomically (temp file + rename), so a crashed run never leaves a truncated cache entry.
    Without a sha256 or a cache directory this is a plain conversion.
    """
    valid_sha = isinstance(source_sha256, str) and len(source_sha256) == 64 and all(
        c in "0123456789abcdefABCDEF" for c in source_sha256)
    if not cache_dir or not valid_sha:
        return converter.convert_to_dxf(source, output_dir)
    key = _converter_key(converter)
    cached = cached_dxf_path(cache_dir, source_sha256, key)
    # Own converter's entry first, then any ODA entry: ODA output is the highest fidelity, and a host
    # pre-conversion (cli convert-dwg) must be usable by a container that has no ODA of its own.
    candidates = [cached, *sorted(cached.parent.glob(f"{source_sha256.lower()}.oda-*.dxf"))]
    for hit in candidates:
        if hit.is_file() and hit.stat().st_size > 0:
            return DWGConversionResult("SUCCESS", str(Path(source).resolve()), str(hit), getattr(converter, "name", key),
                                       [], {"cache": "hit", "cache_path": str(hit), "output_size": hit.stat().st_size})
    result = converter.convert_to_dxf(source, output_dir)
    result.checks = dict(result.checks or {})
    result.checks["cache"] = "miss"
    if result.status == "SUCCESS" and result.output:
        try:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_name(cached.name + f".tmp-{os.getpid()}")
            shutil.copy2(result.output, tmp)
            os.replace(tmp, cached)
            result.checks["cache_path"] = str(cached)
        except OSError as exc:  # a full or read-only cache must not fail the conversion itself
            result.checks["cache_error"] = str(exc)
    return result

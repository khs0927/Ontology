from pathlib import Path
import hashlib
import json
import shutil
import subprocess

import pytest

from aec_intelligence.dwg import ODAConverter
from aec_intelligence.dxf import DXFParser


def test_oda_converter_stages_non_ascii_workspace_paths(monkeypatch, tmp_path: Path):
    source_dir = tmp_path / "온톨로지"
    source_dir.mkdir()
    source = source_dir / "simple_house.dwg"
    source.write_bytes(b"realistic-dwg-fixture")
    output_dir = source_dir / ".cache" / "converted"
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        staged_output = Path(command[2]) / "source.dxf"
        staged_output.write_bytes(b"oda-dxf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("aec_intelligence.dwg.subprocess.run", fake_run)
    result = ODAConverter("C:/oda/ODAFileConverter.exe").convert_to_dxf(source, output_dir)

    assert result.status == "SUCCESS"
    assert result.checks["ascii_staging"] is True
    assert output_dir.joinpath("simple_house.dxf").read_bytes() == b"oda-dxf"
    assert commands
    assert all("온톨로지" not in part for part in commands[0][1:3])
    assert source.read_bytes() == b"realistic-dwg-fixture"


def test_real_dwg_fixture_matches_known_ground_truth_when_oda_is_available(tmp_path: Path):
    executable = shutil.which("ODAFileConverter") or Path(r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe")
    if not Path(executable).is_file():
        pytest.skip("ODA File Converter is not configured")

    source = Path(__file__).parents[1] / "fixtures" / "simple_house.dwg"
    ground_truth = json.loads((source.parent / "known-ground-truth.json").read_text(encoding="utf-8"))["dwg"]
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    result = ODAConverter(executable).convert_to_dxf(source, tmp_path / "converted")
    parsed = DXFParser().parse(result.output) if result.output else None

    assert result.status == "SUCCESS"
    assert parsed is not None
    assert before_hash == ground_truth["sha256"]
    assert parsed.counts["entity_count"] == ground_truth["converted_entity_count"]
    assert parsed.counts["layer_count"] == ground_truth["converted_layer_count"]
    assert parsed.counts["block_count"] == ground_truth["converted_block_count"]
    assert parsed.counts["text_count"] == ground_truth["converted_text_count"]
    assert parsed.counts["unsupported_count"] == ground_truth["unsupported_entity_count"]
    assert hashlib.sha256(source.read_bytes()).hexdigest().upper() == before_hash

from pathlib import Path

from aec_intelligence.cli import main


def test_cli_mirrors_reference_3d_and_raster_boundaries(tmp_path: Path, capsys):
    obj = tmp_path / "triangle.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    dem = tmp_path / "terrain.dem"
    dem.write_text("ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\n5\n", encoding="ascii")
    root = Path(__file__).parents[1]

    assert main(["--root", str(tmp_path), "parse-reference", str(root / "fixtures" / "simple_house.svg")]) == 0
    assert main(["--root", str(tmp_path), "parse-3d", str(obj)]) == 0
    assert main(["--root", str(tmp_path), "parse-raster", str(dem)]) == 0
    assert "semantic_cair_generated" in capsys.readouterr().out


def test_cli_probe_apps_accepts_explicit_executable_paths(tmp_path: Path, capsys):
    freecad = tmp_path / "freecadcmd.exe"
    blender = tmp_path / "blender.exe"
    freecad.write_bytes(b"configured")
    blender.write_bytes(b"configured")

    assert main(["probe-apps", "--freecad", str(freecad), "--blender", str(blender)]) == 0
    output = capsys.readouterr().out
    assert '"status": "AVAILABLE"' in output
    assert '"status": "REQUIRES_CONFIGURATION"' in output


def test_cli_native_ifc_probe_reports_missing_source_without_guessing_runtime(tmp_path: Path, capsys):
    assert main(["--root", str(tmp_path), "probe-native-ifc", str(tmp_path / "missing.ifc")]) == 2
    output = capsys.readouterr().out
    assert '"status": "FAILED"' in output
    assert "IFC source was not found" in output

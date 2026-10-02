from pathlib import Path
import shutil

import pytest

from aec_intelligence.dwg import LibreDWGConverter, ODAConverter, select_dwg_converter

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "simple_house.dwg"


def test_select_converter_modes(monkeypatch):
    monkeypatch.setattr("aec_intelligence.dwg.shutil.which", lambda name: "/usr/bin/dwg2dxf" if name == "dwg2dxf" else None)
    assert isinstance(select_dwg_converter("auto"), LibreDWGConverter)
    assert isinstance(select_dwg_converter("auto", oda_executable="/x/ODAFileConverter"), ODAConverter)
    assert isinstance(select_dwg_converter("oda"), ODAConverter)
    with pytest.raises(ValueError):
        select_dwg_converter("bogus")


def test_libredwg_reports_missing_executable(monkeypatch, tmp_path):
    monkeypatch.setattr("aec_intelligence.dwg.shutil.which", lambda name: None)
    result = LibreDWGConverter().convert_to_dxf(FIXTURE, tmp_path)
    assert result.status == "FAILED"
    assert "not configured" in result.errors[0]


@pytest.mark.skipif(shutil.which("dwg2dxf") is None, reason="LibreDWG dwg2dxf not installed")
def test_libredwg_converts_fixture_readable_by_ezdxf(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    before = FIXTURE.read_bytes()
    result = LibreDWGConverter().convert_to_dxf(FIXTURE, tmp_path)
    assert result.status == "SUCCESS", result.checks
    document = ezdxf.readfile(result.output)
    types = {entity.dxftype() for entity in document.modelspace()}
    assert {"LINE", "TEXT"} <= types
    assert FIXTURE.read_bytes() == before

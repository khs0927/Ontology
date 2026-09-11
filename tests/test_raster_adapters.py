import json
from pathlib import Path

from aec_intelligence.mcp_gateway import MCPGateway
from aec_intelligence.raster_adapters import RasterParser, parse_raster


def test_ascii_dem_preserves_extent_and_elevation_evidence(tmp_path: Path):
    dem = tmp_path / "site.dem"
    dem.write_text(
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner 100\n"
        "yllcorner 200\n"
        "cellsize 10\n"
        "NODATA_value -9999\n"
        "1 2 3\n"
        "4 5 -9999\n",
        encoding="ascii",
    )
    result = RasterParser().parse(dem)
    assert result.source_format == "DEM"
    assert result.status == "SUCCESS_WITH_WARNINGS"
    assert result.metadata["bounds"] == {"left": 100.0, "bottom": 200.0, "right": 130.0, "top": 220.0}
    assert result.metadata["value_statistics"]["min"] == 1.0
    assert result.metadata["value_statistics"]["max"] == 5.0
    assert result.metadata["semantic_cair_generated"] is False


def test_dem_fixture_matches_known_ground_truth():
    root = Path(__file__).parents[1]
    result = RasterParser().parse(root / "fixtures" / "simple_terrain.dem")
    ground_truth = json.loads((root / "fixtures" / "known-ground-truth.json").read_text(encoding="utf-8"))["dem"]
    assert result.metadata["width"] == ground_truth["width"]
    assert result.metadata["height"] == ground_truth["height"]
    bounds = result.metadata["bounds"]
    assert [bounds["left"], bounds["bottom"], bounds["right"], bounds["top"]] == ground_truth["bounds"]
    assert result.metadata["value_statistics"]["sample_count"] == ground_truth["valid_value_count"]


def test_geotiff_structural_fallback_and_mcp_contract(tmp_path: Path):
    from PIL import Image

    tif = tmp_path / "site.tif"
    Image.new("L", (4, 3), color=7).save(tif)
    result = parse_raster(tif)
    assert result.source_format == "GeoTIFF"
    assert result.metadata["width"] == 4
    assert result.metadata["height"] == 3
    assert result.metadata["semantic_cair_generated"] is False

    gateway = MCPGateway(tmp_path)
    called = gateway.call_tool("aec.parse_raster", {"source": str(tif)})
    assert called["source_format"] == "GeoTIFF"
    definition = next(item for item in gateway.list_tools() if item["name"] == "aec.parse_raster")
    assert definition["inputSchema"]["required"] == ["source"]


def test_ingest_file_registers_raster_without_fabricating_cair(tmp_path: Path):
    dem = tmp_path / "terrain.dem"
    dem.write_text("ncols 1\nnrows 1\nxllcorner 0\nyllcorner 0\ncellsize 1\n5\n", encoding="ascii")
    gateway = MCPGateway(tmp_path)
    result = gateway.call_tool("aec.ingest_file", {"source": str(dem), "project_id": "P-DEM-REFERENCE"})
    assert result["status"] == "SUCCESS_WITH_WARNINGS"
    assert result["semantic_cair_generated"] is False
    assert Path(result["outputs"]["raster_report"]).is_file()
    assert not (tmp_path / "projects" / "P-DEM-REFERENCE" / "03_CAIR" / "project-cair.json").exists()
    repeated = gateway.call_tool("aec.ingest_file", {"source": str(dem), "project_id": "P-DEM-REFERENCE"})
    assert repeated["skipped"] is True

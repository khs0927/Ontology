from pathlib import Path

import pytest

from aec_intelligence.document_adapters import PDFParser, SVGParser, parse_reference_document
from aec_intelligence.mcp_gateway import MCPGateway


def test_svg_reference_parser_preserves_drawing_structure():
    result = SVGParser().parse(Path(__file__).parents[1] / "fixtures" / "simple_house.svg")
    assert result.status == "SUCCESS"
    assert result.source_format == "SVG"
    assert result.metadata["view_box"] == [0.0, 0.0, 10000.0, 7000.0]
    assert result.metadata["geometry_element_count"] == 4
    assert result.metadata["text_count"] == 1
    assert result.metadata["semantic_cair_generated"] is False


def test_pdf_reference_parser_reports_pages_text_and_drawings(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "simple_house.pdf"
    document = fitz.open()
    page = document.new_page(width=500, height=300)
    page.insert_text((40, 60), "SIMPLE HOUSE")
    page.draw_rect(fitz.Rect(20, 20, 450, 250))
    document.save(str(path))
    document.close()

    result = PDFParser().parse(path)
    assert result.status == "SUCCESS"
    assert result.source_format == "PDF"
    assert result.metadata["page_count"] == 1
    assert result.metadata["text_char_count"] > 0
    assert result.metadata["drawing_count"] >= 1
    assert result.metadata["semantic_cair_generated"] is False


def test_reference_document_dispatch_and_mcp_contract(tmp_path: Path):
    svg = Path(__file__).parents[1] / "fixtures" / "simple_house.svg"
    direct = parse_reference_document(svg)
    assert direct.source_format == "SVG"
    result = MCPGateway(tmp_path).call_tool("aec.parse_reference_document", {"source": str(svg.resolve())})
    assert result["status"] == "SUCCESS"
    assert result["source_format"] == "SVG"
    definition = next(item for item in MCPGateway(tmp_path).list_tools() if item["name"] == "aec.parse_reference_document")
    assert definition["inputSchema"]["required"] == ["source"]


def test_ingest_file_registers_reference_without_fabricating_cair(tmp_path: Path):
    svg = Path(__file__).parents[1] / "fixtures" / "simple_house.svg"
    gateway = MCPGateway(tmp_path)
    result = gateway.call_tool(
        "aec.ingest_file",
        {"source": str(svg.resolve()), "project_id": "P-SVG-REFERENCE", "name": "SVG Reference"},
    )
    assert result["status"] == "SUCCESS"
    assert result["semantic_cair_generated"] is False
    assert result["reference"]["source_format"] == "SVG"
    report = Path(result["outputs"]["reference_report"])
    assert report.is_file()
    project = tmp_path / "projects" / "P-SVG-REFERENCE"
    assert not (project / "03_CAIR" / "project-cair.json").exists()
    repeated = gateway.call_tool(
        "aec.ingest_file",
        {"source": str(svg.resolve()), "project_id": "P-SVG-REFERENCE", "name": "SVG Reference"},
    )
    assert repeated["skipped"] is True

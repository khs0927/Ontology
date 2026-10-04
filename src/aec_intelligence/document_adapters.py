"""Reference-document adapters for SVG and PDF drawing evidence.

These adapters deliberately stop at a compact document report.  A PDF/SVG
reference is not silently promoted to a semantic CAIR model; the original
asset remains authoritative and the report can be attached to a project
manifest or used by a later visual comparison workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


class DocumentAdapterUnavailable(RuntimeError):
    """Raised when a reference-document adapter needs an unavailable package."""


@dataclass
class ReferenceParseResult:
    source_file: str
    source_format: str
    status: str = "SUCCESS"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_format": self.source_format,
            "status": self.status,
            "metadata": self.metadata,
            "warnings": self.warnings,
            "parser": self.metadata.get("parser"),
            "parser_version": self.metadata.get("parser_version"),
        }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class SVGParser:
    """Inspect SVG drawing structure without flattening it into CAIR."""

    name = "SVG XML adapter"
    parser_version = "0.1.0"
    geometry_elements = {"path", "line", "polyline", "polygon", "rect", "circle", "ellipse"}

    def parse(self, path: str | Path) -> ReferenceParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".svg":
            raise ValueError("SVGParser accepts .svg files")
        root = ET.parse(source).getroot()
        counts: dict[str, int] = {}
        for element in root.iter():
            name = _local_name(element.tag)
            counts[name] = counts.get(name, 0) + 1
        view_box = None
        raw_view_box = root.attrib.get("viewBox")
        if raw_view_box:
            try:
                values = [float(value) for value in raw_view_box.replace(",", " ").split()]
                if len(values) == 4:
                    view_box = values
            except ValueError:
                pass
        metadata = {
            "parser": self.name,
            "parser_version": self.parser_version,
            "root_element": _local_name(root.tag),
            "width": root.attrib.get("width"),
            "height": root.attrib.get("height"),
            "view_box": view_box,
            "element_count": sum(counts.values()),
            "geometry_element_count": sum(counts.get(name, 0) for name in self.geometry_elements),
            "text_count": counts.get("text", 0),
            "element_counts": counts,
            "semantic_cair_generated": False,
        }
        return ReferenceParseResult(str(source), "SVG", metadata=metadata)


class PDFParser:
    """Extract compact page/text/drawing evidence using PyMuPDF when available."""

    name = "PyMuPDF PDF drawing adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> ReferenceParseResult:
        try:
            import fitz
        except ImportError as exc:
            raise DocumentAdapterUnavailable("PyMuPDF is not installed; install the PDF document extra") from exc
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".pdf":
            raise ValueError("PDFParser accepts .pdf files")
        document = fitz.open(str(source))
        page_sizes: list[dict[str, float]] = []
        text_block_count = 0
        text_char_count = 0
        drawing_count = 0
        image_count = 0
        try:
            for page in document:
                page_sizes.append({"width": float(page.rect.width), "height": float(page.rect.height)})
                blocks = page.get_text("blocks")
                text_block_count += len(blocks)
                text_char_count += sum(len(str(block[4])) for block in blocks if len(block) > 4)
                drawing_count += len(page.get_drawings())
                image_count += len(page.get_images(full=True))
        finally:
            document.close()
        metadata = {
            "parser": self.name,
            "parser_version": self.parser_version,
            "page_count": len(page_sizes),
            "page_sizes": page_sizes,
            "text_block_count": text_block_count,
            "text_char_count": text_char_count,
            "drawing_count": drawing_count,
            "image_count": image_count,
            "semantic_cair_generated": False,
        }
        return ReferenceParseResult(str(source), "PDF", metadata=metadata)


def parse_reference_document(path: str | Path) -> ReferenceParseResult:
    """Dispatch one SVG/PDF reference asset to its explicit adapter."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".svg":
        return SVGParser().parse(source)
    if suffix == ".pdf":
        return PDFParser().parse(source)
    raise ValueError("reference parser accepts .svg or .pdf")

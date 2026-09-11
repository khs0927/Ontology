"""GeoTIFF and DEM evidence adapters with an explicit non-semantic boundary.

Raster pixels and elevation values remain source geometry/evidence. This module
does not create CAIR objects or infer land-use/building semantics from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any


class RasterAdapterUnavailable(RuntimeError):
    """Raised when a raster format needs an optional reader."""


@dataclass
class RasterParseResult:
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
        }


def _result(source: Path, source_format: str, metadata: dict[str, Any], warnings: list[str] | None = None) -> RasterParseResult:
    warning_list = list(warnings or [])
    return RasterParseResult(
        source_file=str(source),
        source_format=source_format,
        status="SUCCESS_WITH_WARNINGS" if warning_list else "SUCCESS",
        metadata={**metadata, "semantic_cair_generated": False},
        warnings=warning_list,
    )


class RasterParser:
    name = "GeoTIFF/DEM raster evidence adapter"
    parser_version = "0.1.0"

    def parse(self, path: str | Path) -> RasterParseResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower()
        if suffix not in {".tif", ".tiff", ".dem"}:
            raise ValueError("RasterParser accepts .tif, .tiff, or .dem files")
        if suffix == ".dem":
            ascii_result = self._parse_ascii_dem(source)
            if ascii_result is not None:
                return ascii_result
            return self._parse_with_rasterio(source, "DEM")
        return self._parse_geotiff(source)

    def _parse_geotiff(self, source: Path) -> RasterParseResult:
        try:
            return self._parse_with_rasterio(source, "GeoTIFF")
        except RasterAdapterUnavailable:
            # PIL provides a safe structural fallback, but cannot verify CRS or
            # GeoTIFF transforms. The limitation is returned explicitly.
            try:
                from PIL import Image
            except ImportError as exc:
                raise RasterAdapterUnavailable("Rasterio or Pillow is not installed; install the [gis] extra for GeoTIFF/DEM") from exc
            try:
                with Image.open(source) as image:
                    metadata = {
                        "parser": self.name,
                        "parser_version": self.parser_version,
                        "reader": "Pillow structural fallback",
                        "width": int(image.width),
                        "height": int(image.height),
                        "band_count": len(image.getbands()),
                        "mode": image.mode,
                        "crs": None,
                        "crs_status": "unverified_without_rasterio",
                        "transform": None,
                        "bounds": None,
                    }
            except Exception as exc:
                raise ValueError(f"invalid GeoTIFF: {exc}") from exc
            return _result(source, "GeoTIFF", metadata, ["Rasterio is not installed; GeoTIFF CRS, transform, and geospatial bounds were not verified"])

    def _parse_with_rasterio(self, source: Path, source_format: str) -> RasterParseResult:
        try:
            import rasterio
        except ImportError as exc:
            raise RasterAdapterUnavailable("Rasterio is not installed; install the [gis] extra for GeoTIFF/DEM CRS-preserving reads") from exc
        with rasterio.open(source) as dataset:
            crs = dataset.crs.to_string() if dataset.crs is not None else None
            transform = [float(value) for value in tuple(dataset.transform)[:6]]
            bounds = {
                "left": float(dataset.bounds.left),
                "bottom": float(dataset.bounds.bottom),
                "right": float(dataset.bounds.right),
                "top": float(dataset.bounds.top),
            }
            sample_height = min(int(dataset.height), 1024)
            sample_width = min(int(dataset.width), 1024)
            values = dataset.read(1, out_shape=(sample_height, sample_width), masked=True)
            valid = [float(value) for value in values.compressed()]
            metadata = {
                "parser": self.name,
                "parser_version": self.parser_version,
                "reader": "rasterio",
                "driver": dataset.driver,
                "width": int(dataset.width),
                "height": int(dataset.height),
                "band_count": int(dataset.count),
                "dtype": str(dataset.dtypes[0]) if dataset.dtypes else None,
                "crs": crs,
                "crs_status": "declared" if crs else "not_declared",
                "transform": transform,
                "bounds": bounds,
                "nodata": dataset.nodata,
                "value_statistics": {
                    "sampled": len(valid) != int(dataset.width) * int(dataset.height),
                    "sample_count": len(valid),
                    "min": min(valid) if valid else None,
                    "max": max(valid) if valid else None,
                    "mean": fmean(valid) if valid else None,
                },
            }
            warnings = ["raster has no CRS declaration"] if crs is None else []
            return _result(source, source_format, metadata, warnings)

    def _parse_ascii_dem(self, source: Path) -> RasterParseResult | None:
        text = source.read_text(encoding="utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        headers: dict[str, str] = {}
        header_count = 0
        for line in lines[:12]:
            parts = line.split(maxsplit=1)
            if len(parts) != 2 or parts[0].lower() not in {
                "ncols", "nrows", "xllcorner", "xllcenter", "yllcorner", "yllcenter", "cellsize", "nodata_value"
            }:
                break
            headers[parts[0].lower()] = parts[1]
            header_count += 1
        required = {"ncols", "nrows", "cellsize"}
        if not required.issubset(headers):
            return None
        try:
            ncols = int(float(headers["ncols"]))
            nrows = int(float(headers["nrows"]))
            cellsize = float(headers["cellsize"])
            x_origin = float(headers.get("xllcorner", headers.get("xllcenter", "0")))
            y_origin = float(headers.get("yllcorner", headers.get("yllcenter", "0")))
            nodata = float(headers["nodata_value"]) if "nodata_value" in headers else None
            values = [float(token) for line in lines[header_count:] for token in line.split()]
        except ValueError as exc:
            raise ValueError(f"invalid ASCII DEM header or values: {exc}") from exc
        valid = [value for value in values if nodata is None or value != nodata]
        warnings = ["ASCII DEM does not declare a CRS"]
        metadata = {
            "parser": self.name,
            "parser_version": self.parser_version,
            "reader": "ESRI ASCII Grid baseline",
            "encoding": "ascii-grid",
            "width": ncols,
            "height": nrows,
            "band_count": 1,
            "crs": None,
            "crs_status": "not_declared",
            "transform": [cellsize, 0.0, x_origin, 0.0, -cellsize, y_origin + nrows * cellsize],
            "bounds": {
                "left": x_origin,
                "bottom": y_origin,
                "right": x_origin + ncols * cellsize,
                "top": y_origin + nrows * cellsize,
            },
            "nodata": nodata,
            "value_statistics": {
                "sampled": False,
                "sample_count": len(valid),
                "min": min(valid) if valid else None,
                "max": max(valid) if valid else None,
                "mean": fmean(valid) if valid else None,
            },
        }
        return _result(source, "DEM", metadata, warnings)


def parse_raster(path: str | Path) -> RasterParseResult:
    return RasterParser().parse(path)

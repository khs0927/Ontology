# Repository architecture and CAIR v0.1

## Boundaries

```text
DWG --ODA--> DXF --ezdxf--> normalized CAD entities --rules--┐
IFC --------IfcOpenShell--> normalized IFC entities --------┼--> CAIR
GIS ----GeoJSON/GDAL------> normalized GIS features --------┘      ├─ portable tables
                                                                  ├─ Turtle ontology
                                                                  ├─ JSON-LD graph
                                                                  ├─ geometry index / SVG preview
                                                                  └─ rebuildable runtime registry
STEP/STL/OBJ/GLB/glTF --metadata evidence--> immutable exchange artifact/report
GeoTIFF/DEM ------------raster evidence-----> immutable raster artifact/report
```

`CAIR` is the canonical semantic boundary. DXF entities are never written directly to ontology classes. Each object retains a source-scoped handle, artifact ID, layer, geometry reference, classification confidence/evidence, and provenance.

## Local layout

- `projects/<project-id>/01_RAW` — original source artifacts; never overwritten.
- `projects/<project-id>/02_DERIVED` — previews and derived exchange files.
- `projects/<project-id>/03_CAIR` — snapshot, normalized geometry index, and portable tables.
- `projects/<project-id>/04_ONTOLOGY` — project ABox Turtle export.
- `projects/<project-id>/11_VALIDATION` — parse and quality reports.
- `global/00_GLOBAL` — rebuildable cross-project registries and manifest.
- `runtime/` — SQLite artifact/project index; not canonical memory.

## Storage policy

`ArtifactStore` is a protocol. `LocalArtifactStore` is the first implementation and `GoogleDriveArtifactStore` is a cache-plus-client adapter. The latter requires an injected MCP/API client and never invents Drive IDs. Google Drive is therefore replaceable without coupling CAIR to a connector.

Portable JSON/JSONL is available in the base runtime. Parquet is an optional derived output adapter requiring `pyarrow`. IFC uses IfcOpenShell when installed; GeoJSON has a standard-library baseline and GPKG/SHP use GeoPandas/GDAL when installed. DuckDB, RDFLib/Oxigraph, and Neo4j remain runtime adapters.

STEP, STL, OBJ, GLB, and glTF are exchange formats rather than semantic
authorities in this baseline. Their adapters record structure and geometry
evidence, preserve the immutable source, and emit parsing reports; they do not
invent CAIR semantics from mesh or B-Rep content.

GeoTIFF and DEM are likewise evidence sources in the baseline. Rasterio-backed
reads preserve CRS, affine transforms, bounds, nodata, and sampled value
statistics; missing geospatial dependencies result in explicit fallback or
dependency status rather than fabricated spatial semantics.

## Format-to-CAIR rule

IFC GlobalId or entity id and GIS feature id are converted to stable `aec://` object identifiers. Semantic properties become CAIR type/classification/properties; raw IFC geometry and GIS coordinates stay in a geometry reference/index. Every normalized object retains source format, source object, parser, hash when available, and transformation provenance.

## Ontology alignment

The checked-in alignment layer is under `global/ontology/`. It maps CAIR concepts to BOT, OMG, FOG, GeoSPARQL, PROV-O, and bSDD namespaces. These are reviewable alignment declarations; they do not make external ontologies or geometry bulk canonical data.

## Existing SQLite assets

The pre-existing `autocad_data.db` and `bld_ledger_storage.db` were inspected read-only. Both are integrity-valid and empty. The first contains CAD-oriented tables (`cad_elements`, `text_patterns`, `operation_history`); the second contains a Korean parcel/building ledger table (`bld_ledger`). They are preserved as raw user assets and intentionally not reused as runtime registry files. Future adapters can map CAD handles and ledger parcel identifiers into CAIR with provenance.

## Quality states

Classification is evidence-based and not an authoritative edit to the source drawing:

- `AUTO_ACCEPT` — confidence above 0.95
- `ACCEPT_WITH_WARNING` — confidence 0.75–0.95
- `REQUIRE_VALIDATION` — confidence below 0.75

Ingestion reports `SUCCESS`, `SUCCESS_WITH_WARNINGS`, `PARTIAL`, `FAILED`, or `REQUIRES_REVIEW` and stores parse counts, extents, unsupported types, warnings, and quality scores.

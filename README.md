# AEC Semantic Operating System — Prototype

This repository is the first executable slice of the Global AEC Semantic Design Intelligence Framework.

The initial milestone implements the requested sequence:

1. Repository architecture
2. CAIR v0.1 schema
3. DXF ingestion prototype with semantic classification, provenance, validation, and ontology export
4. IFC/GIS semantic adapters that normalize into CAIR and keep geometry external, including optional IfcOpenShell mesh extraction
5. SVG/PDF reference-document parsing and non-fabricating reference ingest
6. Portable CAIR tables with JSONL canonical output, optional Parquet acceleration, properties, source/application mappings, and explicit table manifests
7. Transport-neutral MCP gateway and semantic exchange manifests for FreeCAD, Blender, and QGIS
8. Integrated validation, cross-project retrieval, versioned design memory, and a source-backed dashboard artifact
9. Validated ODA-backed DWG ingestion plus headless Blender and FreeCAD derived review outputs with CAIR-linked provenance
10. IFC mesh hand-off to FreeCAD/Blender review outputs and explicit opt-in FreeCAD solid review for closed planar CAIR polylines
11. STEP/STL/OBJ/GLB/glTF exchange metadata parsing and non-semantic, idempotent source registration
12. GeoTIFF/DEM raster evidence parsing with CRS/transform/bounds preservation when Rasterio is available
13. Rebuildable supplementary vector-memory index alongside structured and graph retrieval
14. Explicit headless FreeCAD NativeIFC geometry probe with non-null-shape acceptance and ASCII staging
14. Portable project/global graph interchange in JSON-LD, JSONL, CSV, GraphML, and optional Parquet

The design keeps the following boundaries explicit:

```text
Original artifacts → ArtifactStore → normalized format entities → CAIR → tables / ontology / graph exports
```

Google Drive is modeled as a replaceable persistent `ArtifactStore` adapter. The local implementation is usable without credentials. The two pre-existing SQLite files are preserved as user assets and are not used as the AEC runtime database because both are currently empty and have unrelated schemas.

## Quick start

```powershell
python -m pip install -e ".[cad,bim,gis,pdf,semantic,parquet,runtime,dev]"
python -m aec_intelligence.cli init .
python -m aec_intelligence.cli ingest-dxf fixtures/simple_house.dxf --project-id AEC-2026-000001 --name "Simple House"
python -m aec_intelligence.cli parse-3d projects/AEC-2026-000004/02_DERIVED/BIM/GLB/<file>.glb
python -m aec_intelligence.cli parse-raster fixtures/simple_terrain.dem
python -m aec_intelligence.cli probe-apps --freecad "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe" --blender runtime/external/blender-5.2.0/blender.exe
python -m aec_intelligence.cli probe-native-ifc fixtures/simple_house_geometry.ifc --freecad "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe"
python -m aec_intelligence.cli validate
python -m aec_intelligence.cli memory
python -m aec_intelligence.cli dashboard
python -m aec_intelligence.cli rebuild-global
python -m aec_intelligence.cli refresh-derived
python -m pytest -q
```

If the package is not installed, set `PYTHONPATH=src` before invoking the module.

The ingestion command writes a project manifest, a CAIR JSON snapshot, a separate geometry index, portable CAIR tables, a Turtle ontology export, a validation report, and a deterministic SVG preview under `projects/<project-id>/`.

`validate` runs the integrated repository gate. `memory` rebuilds project/global
agent context from CAIR registries. `rebuild-global` reconstructs derived global
indexes from projects currently present locally. `refresh-derived` backfills
new tables and graph interchange from existing canonical CAIR without reparsing
raw sources or rewriting `project-cair.json`. `dashboard` creates a self-contained HTML
dashboard and JSON data under `global/10_EXPORTS/dashboard` by default. Design
iterations live under `projects/<project-id>/10_ITERATIONS/` and are never used
to overwrite source or CAIR artifacts. Each iteration stores a compact CAIR
object/relation snapshot for object-level diff categories; promotion requires
explicit approval and refreshes global/runtime/agent-memory derived indexes.
The same non-destructive refresh is available as the MCP tool
`aec.refresh_derived_exports`.

## Runtime and persistence

The repository uses a local-first layout. Runtime SQLite and DuckDB are under
`runtime/` and are rebuildable. Canonical source and derived artifacts are under
`projects/` and are never overwritten by ingestion. The Python Google Drive
adapter remains connector-neutral, while the connected Drive workflow has been
verified externally with in-place updates and no sharing changes.

When a connector client is injected into `MCPGateway`, the Drive tools can
upload/synchronize project and global artifacts and rebuild the portable local
runtime from remote files. No client is assumed by default.

Portable tables always include JSONL. If `pyarrow` is installed, Parquet is emitted as a derived acceleration format and the table manifest records both formats. Project graphs also emit CSV/GraphML and optional Parquet companions. Runtime indexes remain rebuildable from canonical JSON/JSONL.

## Current limitations

- ODA DWG conversion is implemented through the safe adapter; ODA File Converter 27.1.0 was explicitly configured on this host and converted the supplied DWG successfully. Windows paths containing non-ASCII workspace names are handled through ASCII staging, while unconfigured hosts still receive an explicit failure report rather than a silent fallback.
- IFC and GeoJSON adapters are implemented; GPKG/SHP use GeoPandas when the `[gis]` extra is installed. DuckDB, Parquet, RDFLib, PyOxigraph, and an ephemeral Neo4j runtime were exercised successfully. Blender 5.2 and FreeCAD 1.1.2 headless CAIR review exports are validated; QGIS 3.44.13 LTR `qgis_process` version/provider discovery and `native:buffer` GeoPackage processing are validated through an explicit user-local wrapper. The semantic-only fixture IFC is kept as a no-geometry case.
- Geometry-bearing IFC products are additionally extracted by IfcOpenShell into the external mesh geometry index and can be handed to FreeCAD/Blender review adapters. `probe-native-ifc` validates the FreeCAD NativeIFC path independently and requires at least one non-null imported shape; on this host the Wall imports as one solid with 12 edges and 6 faces.
- STEP/STL/OBJ/GLB/glTF adapters currently record exchange-structure evidence and can register immutable sources plus parsing reports through `aec.ingest_file`; they intentionally do not infer semantic CAIR objects from geometry alone.
- GeoTIFF/DEM raster adapters record dimensions, bands, CRS, affine transform, bounds, nodata, and bounded value statistics without fabricating semantic CAIR; this host has no Rasterio, so GeoTIFF uses a structural fallback and CRS is explicitly unverified while ASCII DEM remains supported.
- DXF semantic classification is evidence-based rules, not a final AI decision. Confidence and evidence are persisted.

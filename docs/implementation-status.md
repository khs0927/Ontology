# Implementation status

Completed sequential phases:

- Repository architecture and manifest helpers.
- CAIR v0.1 dataclasses, stable global IDs, schema JSON, provenance, classifications, and relationships.
- Local artifact registry and Google Drive-ready `ArtifactStore` boundary.
- DXF normalization using `ezdxf` with entity counts, layers, blocks, units, extents, and unsupported-type reporting.
- Evidence-based wall/door/window/column/beam/slab/grid/annotation/dimension classification.
- CAIR JSON snapshot, geometry index, Turtle export, SVG preview, validation report, and global registry outputs.
- Portable JSON-LD graph, node/relationship exports, CSV/GraphML interchange, optional Parquet node/relationship tables, hybrid query routing, and rebuildable runtime registry.
- ODA DWG converter boundary with explicit configuration failure instead of silent fallback, including ASCII staging for Windows paths with non-ASCII workspace names.
- ODA File Converter 27.1.0 configured explicitly and validated on the real `fixtures/simple_house.dwg` and supplied DWG: conversion, 149-entity DXF parse, CAIR normalization, provenance, and non-destructive source retention.
- Real DWG fixture ground truth is checked automatically, including SHA-256, entity/layer/block/text counts, unsupported-entity count, and original-source immutability.
- IFC semantic normalization boundary with IfcOpenShell parsing when installed.
- IFC parser evidence now reports representation-context, product-representation, and shape-representation counts so semantic-only fixtures cannot be mistaken for geometry-ready IFC.
- Geometry-bearing IFC products are now extracted through the optional IfcOpenShell geometry module into an external mesh geometry index with declared-unit coordinates, world placement, vertex/face counts, and explicit extraction warnings.
- GIS GeoJSON normalization with CRS preservation and optional GeoPandas GPKG/SHP adapter.
- SVG and PDF reference-document adapters now preserve drawing element/page/text/drawing/image metadata; a reference ingest registers the immutable source and report without fabricating a semantic CAIR snapshot.
- STEP/STL/OBJ/GLB/glTF exchange adapters now preserve format-specific structure evidence; `aec.parse_3d_asset` is semantic-neutral and `aec.ingest_file` registers the immutable source plus an idempotent parsing report without fabricating a semantic CAIR snapshot.
- GeoTIFF/DEM raster evidence adapters now preserve dimensions, bands, CRS status, affine transform, bounds, nodata, and bounded value statistics; `aec.parse_raster` and `aec.ingest_file` keep raster evidence outside semantic CAIR, with an explicit Rasterio dependency boundary and ASCII DEM baseline.
- The CLI now mirrors the transport-neutral boundaries with `parse-reference`, `parse-3d`, and `parse-raster`, returning the same explicit status and non-semantic evidence contracts as MCP.
- IFC/GIS repository ingestion pipeline with immutable CAIR snapshots, geometry indexes, portable tables, ontology/graph exports, and integrated validation.
- Portable CAIR tables: objects, relations, properties, classifications, provenance, geometry index, artifacts, source mappings, and application mappings; JSONL canonical with optional Parquet, hyphenated interchange aliases, and a `cair-manifest.json` table contract.
- Explicit BOT/OMG/FOG/GeoSPARQL/PROV-O/bSDD alignment files and repository compliance gate.
- Optional runtime adapter boundaries for DuckDB, RDFLib, PyOxigraph, and Neo4j, plus deterministic Neo4j seed export; missing dependencies/configuration are explicit statuses.
- Neo4j runtime materialization with idempotent MERGE of global projects, objects, relations, and provenance; verified against an ephemeral Neo4j 5.26.30 container and a Wall query.
- Transport-neutral MCP gateway with high-level project/file ingest, CAD/IFC/GIS parse, CAIR/classification, ontology/graph, query, application probe, storage boundary, validation, non-destructive derived-export refresh, iteration, and dashboard tool contracts.
- MCP newline-delimited JSON-RPC stdio server (`aec-mcp`) with initialize, tools/list, tools/call, ping, notifications, batch handling, and subprocess tests.
- Localhost-only MCP Streamable HTTP JSON transport (`aec-mcp-http`) with Origin, media-type, protocol-version, body-size, and bind-address guardrails.
- FreeCAD/Blender/QGIS semantic exchange manifests and executable probes; no software installation or silent application substitution. FreeCAD headless FCStd/STEP and Blender headless BLEND/GLB/PNG exports preserve CAIR identity and provenance. QGIS also has an explicit headless runtime probe for version/provider discovery and optional `native:buffer` GeoPackage processing.
- Integrated project/repository validation engine covering manifest, CAIR, portable tables, global IDs, provenance, geometry references, parse counts, ontology, graph, and application exchange outputs.
- Cross-project CAIR retrieval, similar-project fingerprints, source mapping, and rebuildable project/global agent-memory packages including JSONL/Parquet indexes and summaries.
- Agent-memory packages now include a deterministic 64-dimensional lexical vector index in JSONL plus optional Parquet form; it supplements graph/structured retrieval and is never used as the sole geometry authority.
- Versioned design iterations with non-destructive reasoning/diff manifests, portable CAIR object/relation snapshots, and explicit added/deleted/reclassified/property/geometry/moved/resized/relationship diff categories. Promotion is approval-gated and automatically rebuilds global indexes, runtime registry, and project/global agent-memory packages.
- Source-backed, dependency-free HTML/JSON repository dashboard generated from canonical registries and validation reports.
- Canonical global-index rebuild command that removes projects no longer present under `projects/` without deleting their archived smoke-run files.
- Non-destructive `refresh-derived` maintenance command backfills expanded CAIR table and graph interchange exports from existing canonical CAIR without reparsing or rewriting authoritative source/CAIR files.
- Google Drive connector sync and read-back verified for canonical registries, validation, memory, dashboard, project-memory artifacts, Blender/FreeCAD runtime outputs, and an isolated Drive-only cold restore with stable IDs and no sharing changes.
- The injected Google Drive artifact adapter now records Drive file/folder IDs, local SHA-256, MIME type, size, Drive modified time when returned, sync time, and returned visibility metadata while preserving content-addressed idempotency.
- MCP Drive operations now use an optional injected client for project/global upload, synchronization, context download, and full portable cold restore; the restore path materializes remote artifacts, rebuilds global indexes/runtime/memory, and validates the restored query path while remaining explicit when no client is configured. The public `rebuild_runtime_from_drive` API now preserves Drive-materialized `GLOBAL` artifacts across subsequent global rebuilds.
- Configured file ingestion is Drive-first: the immutable source is uploaded before authoritative parsing, then the derived CAIR/graph/validation package is synchronized after successful ingest; missing Drive configuration remains an explicit status rather than a silent local-only claim.
- Global rebuilds now emit machine-readable registry mirrors and graph interchange under `global/00_GLOBAL` and `global/03_KNOWLEDGE_GRAPH`, while memory rebuilds derive `strategies`, `problems`, `solutions`, `design-moves`, `precedents`, `lessons`, `metrics`, `failures`, and `design-patterns` under `global/07_DESIGN_KNOWLEDGE` from iteration manifests.
- Fixture, unit, integration, idempotency, failure, table, adapter, and framework compliance tests.
- Cross-format fixture acceptance compares shared Wall/Door/Window semantics across DXF, ODA-derived DWG, and IFC; FreeCAD round-trip acceptance measures planar STEP bounding-box deviation and records the explicit wireframe/solid representation policy.
- FreeCAD now supports an explicit opt-in solid review mode: closed planar CAIR polylines are extruded into separate FCStd/STEP derived artifacts, while the default wireframe route and authoritative CAIR/raw sources remain unchanged. The fixture acceptance path produced two STEP solids with zero planar bounding-box deviation and is exposed through `aec.open_in_freecad.solid_mode`.
- FreeCAD NativeIFC geometry import is now an explicit headless acceptance boundary: `probe-native-ifc` and `aec.probe_native_ifc` use `strategy=2` and `shapemode=0`, stage Windows process paths as ASCII, require a machine-readable report with a non-null shape, and preserve the authoritative IFC/CAIR sources. The current FreeCAD 1.1.2 probe imports the geometry-bearing fixture's Wall as one solid with 12 edges and 6 faces; semantic-only IFC remains a truthful no-geometry failure.

Remaining environment-bound phases:

1. Bind the local MCP gateway to a selected hosted MCP app only after product discovery and SPEC validation, if a hosted UI is required.
2. Application executable paths remain host configuration, not canonical repository state. On this host QGIS 3.44.13 LTR `qgis_process` runtime/provider discovery and `native:buffer` GeoPackage processing are validated through the explicit user-local wrapper. Blender 5.2 portable headless export and FreeCAD 1.1.2 headless FCStd/STEP wireframe export have been validated for CAIR snapshots, including planar bounding-box deviation. IFC mesh review export and NativeIFC import are both validated through explicit paths; the native probe requires non-null geometry and never promotes semantic-only IFC to geometry.

Every phase is guarded by `aec audit`: CAIR schema presence, canonical/runtime separation, project manifests, artifact lifecycle, global ids/confidence bounds, ontology alignments, and portable table presence.

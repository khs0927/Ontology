# Application adapter boundary

FreeCAD, Blender, and QGIS consume CAIR through a semantic exchange manifest.
The manifest carries stable `aec://` identities, semantic types,
classifications, source format, and geometry references; it does not copy raw
geometry into the semantic payload.

Each adapter probes only the configured executable. Missing PATH entries return
`REQUIRES_CONFIGURATION`; the adapter does not install software, launch a
terminal, or silently substitute another application.

For deterministic host checks, `aec probe-apps` accepts explicit `--freecad`,
`--blender`, and `--qgis` paths. On the current host, the explicit FreeCAD
1.1.2, Blender 5.2, and user-local QGIS 3.44.13 LTR paths return `AVAILABLE`.
The QGIS path is the official Windows `qgis_process` wrapper, so its packaged
GDAL/PROJ environment is initialized without changing PATH globally. The
connected FreeCAD MCP status is healthy in subprocess mode, while TCP GUI mode
remains an explicit separate capability and is not assumed.

`aec probe-qgis-runtime --qgis <wrapper>` executes `--version` and `list`,
requiring a recognizable QGIS version and a successful processing provider
listing. It can additionally run `native:buffer` against an explicit vector
source and GeoPackage output. The current host passed this end-to-end check
with `fixtures/site.geojson`, `native:buffer`, and a non-empty GeoPackage
output. Runtime validation is evidence only: it does not create semantic CAIR
objects or replace the CAIR/raw-source authority boundary. The same check is
available as `aec.probe_qgis_runtime` through the MCP gateway.

The validated headless paths are explicit derived exports: Blender produces a
CAIR-linked .blend, GLB, and visual QA PNG; FreeCAD produces a CAIR-linked
FCStd and STEP wireframe. Geometry-bearing IFC products can flow through the
same external mesh geometry index into both review adapters. FreeCAD also has
an explicit opt-in solid mode for closed planar CAIR polylines only. These
outputs are review/exchange artifacts, not authoritative CAD/BIM source and do
not overwrite raw files or CAIR.

The separate `probe-native-ifc` CLI command and `aec.probe_native_ifc` MCP tool
exercise FreeCAD's NativeIFC importer directly. They require a non-null imported
shape for geometry success, preserve the IFC/CAIR authority boundary, and use
ASCII-only process staging when the repository path contains non-ASCII Windows
characters.

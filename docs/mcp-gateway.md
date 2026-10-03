# MCP gateway boundary

`aec_intelligence.mcp_gateway.MCPGateway` is the transport-neutral Phase 13
boundary. It exposes stable tool definitions for audit, DXF ingest, query-route
planning, runtime rebuild, object retrieval, integrated validation, global
memory retrieval, similar-project search, memory-package rebuild, non-destructive
derived-export refresh, design iteration management, and dashboard generation. A future MCP server or app can
wrap these handlers without moving semantic logic into the transport layer.

The gateway returns explicit `FAILED`, `NOT_FOUND`, and adapter statuses. It
does not contain credentials or move semantic logic into the transport layer.
Its rebuild operations write derived global indexes and memory packages under
`global/`; the disposable runtime database remains rebuildable from those
portable artifacts.

`aec.parse_reference_document` inspects SVG/PDF drawing evidence. `aec.ingest_file`
routes those reference assets to a source registration and parse-report path
without inventing CAIR semantics when no semantic model is available.

`aec.parse_3d_asset` inspects STEP, STL, OBJ, GLB, and glTF exchange evidence.
The same `aec.ingest_file` boundary registers these immutable 3D sources and
their parsing reports under the project repository, with idempotent hash-based
repeat handling. Mesh/B-Rep evidence is not promoted to semantic CAIR objects
without an authoritative semantic source or an explicit application adapter.

`aec.parse_raster` inspects GeoTIFF and DEM dimensions, bands, CRS status,
affine transform, bounds, nodata, and bounded value statistics. Raster ingest
uses the same immutable-source/report policy and never turns pixels or
elevation values into semantic CAIR objects. Rasterio is the CRS-preserving
reader; an explicit structural fallback is used for GeoTIFF when Rasterio is
not installed, and ASCII-grid DEM is supported without that dependency.

Design iteration tools store a compact CAIR object/relation state in each
iteration manifest. `aec.compare_iterations` reports object-level additions,
deletions, reclassification, property changes, geometry changes, supported
move/resize evidence, and relationship changes. `aec.promote_iteration` first
writes explicit approval metadata, then rebuilds global indexes, the disposable
runtime registry, and project/global agent-memory packages from canonical files.

Project CAIR exports include JSONL tables for objects, relations, properties,
classifications, provenance, geometry, artifacts, source mappings, and honest
application-reference mappings. They also expose optional Parquet companions
and hyphenated interchange aliases. Project graphs include JSON-LD, JSONL,
CSV, GraphML, and optional Parquet node/relationship exports; the global graph
uses the same portable formats.

The Drive tools accept an injected connector client through `MCPGateway` and
execute upload, project/global synchronization, context download, and
project/global cold-restore operations when both the client and root folder ID
are configured. The injected client keeps connector-specific API details
outside the CAIR core; without it, the same tools return explicit
`REQUIRES_CONFIGURATION`. The public `rebuild_runtime_from_drive` path
materializes remote artifacts, rebuilds global indexes/runtime/memory, and
validates the restored repository. When Drive is configured for ingest, raw
source upload is attempted before authoritative parsing and the derived
package is synchronized after success.

`aec.refresh_derived_exports` provides the same safe backfill boundary through
MCP as the `refresh-derived` CLI command. It reads existing canonical CAIR and
preserves raw sources and `project-cair.json` byte-for-byte while regenerating
expanded CAIR tables and portable project graph exports.

`aec.probe_native_ifc` runs FreeCAD's NativeIFC importer against an explicit IFC
source without generating or changing CAIR. It uses the deterministic full-shape
import options, stages process-facing files under an ASCII-only runtime path on
Windows, and returns `SUCCESS` only when the importer produces at least one
non-null FreeCAD shape; semantic-only IFC therefore returns an explicit failure.

`aec.probe_qgis_runtime` validates an explicit headless QGIS wrapper with
`--version` and `list`. With optional source/output arguments it also executes
the `native:buffer` processing algorithm and verifies the requested GeoPackage
exists. The result is runtime evidence only; QGIS does not become a semantic
authority and the operation does not modify CAIR or raw source files.

The semantic gateway is intentionally separated from any hosted Skybridge UI:
that is a product-facing app decision requiring its own discovery and
`SPEC.md`.

`aec-mcp --root <repository>` now provides a local newline-delimited JSON-RPC
stdio transport for the same gateway. It supports `initialize`, `ping`,
`tools/list`, `tools/call`, notifications, and batch input without writing
anything except valid protocol messages to stdout.

`aec-mcp-http --root <repository>` provides a localhost-only stateless
Streamable HTTP JSON transport at `/mcp`. It accepts POST requests with the
required JSON/SSE `Accept` pair, validates `Origin`, requires
`MCP-Protocol-Version` after initialization, returns 405 for GET (SSE is not
enabled), and never binds to a non-localhost host through the CLI.

The dashboard is generated by `aec.build_dashboard` or
`aec dashboard`. It writes `dashboard-data.json` and `aec-dashboard.html` under
`global/10_EXPORTS/dashboard` by default. The data is derived from canonical
global registries plus validation reports; it contains no synthetic KPI data.

## Operational element catalog (agents / power-cad-mcp)

Once drawings are parsed into the operational Postgres stack, agents discover
elements without being told their names. All tools are read-only, need
`AEC_DATABASE_URL`, and return `REQUIRES_CONFIGURATION` when it is unset or the
database is unreachable. The same queries are served over REST.

| MCP tool | REST | Purpose |
| --- | --- | --- |
| `aec.element_catalog` | `GET /v1/catalog?project_id=` | Read first: counts by kind (with Korean aliases), drawing category, layer, block, relation predicate, project |
| `aec.find_elements` | `GET /v1/elements?kind=문&drawing_category=평면도&layer=A-*&block_name=&text=&bbox=x,y,X,Y&limit=&cursor=` | Elements with CAD handle, layout, source path, layer, block name, attributes and bbox |
| `aec.block_catalog` | `GET /v1/blocks?name_like=DOOR*` | Block library across drawings: instances, attribute tags, xref/anonymous, what instances were classified as |
| `aec.drawing_index` | `GET /v1/drawings?category=상세도` | Sheets with title-block number/title/scale, category and per-sheet element counts |
| `aec.element_context` | `GET /v1/elements/{id}/context?hops=2` | Relations both ways (contains, instanceOf, hasTitleBlock, hasSection ...) confirmed against AGE |

`kind` accepts canonical names or Korean/English aliases (`문`, `창호`, `도곽`,
`Door,Window`); `drawing_category` accepts `평면도` or `plan`. Pagination is
keyset based: pass the returned `next_cursor` back unchanged.

### Real embeddings

`docker compose --profile embeddings up -d` starts Hugging Face
text-embeddings-inference serving `BAAI/bge-m3` (1024 dimensions, matching
`aec.embeddings vector(1024)`); api and worker default `AEC_EMBEDDING_URL` to
`http://embeddings:80` (set it to an empty string to force the offline model).
The client batches 32 texts per request (`AEC_EMBEDDING_BATCH_SIZE`) and retries
with backoff (`AEC_EMBEDDING_RETRIES`, `AEC_EMBEDDING_TIMEOUT`).

With an endpoint configured, a failure or an unusable payload (wrong dimension or
count, non-finite component, zero vector) fails the call and the job: the batch is
never relabelled to the hash model, because a placeholder stored as a real vector
cannot be told apart from one a model produced. Only when `AEC_EMBEDDING_URL` is
unset are deterministic vectors stored under `hash-sha256-1024-v1` (offline mode,
not semantic), and the ingest result reports `embedding_model` and
`embeddings_degraded`. Search skips the vector stage entirely when the query
vector would be the hash model or the endpoint is down, and says so in `warnings`;
`operational.embeddings.reindex_embeddings()` backfills real vectors for rows that
only have hash vectors.

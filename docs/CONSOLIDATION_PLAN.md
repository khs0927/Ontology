# Consolidation plan: Ontology-platform (product) + Ontology (ingestion/adapters)

> Same file in both repositories: `khs0927/Ontology-platform/docs/CONSOLIDATION_PLAN.md` and
> `khs0927/Ontology/docs/CONSOLIDATION_PLAN.md`. Written 2026-11-07 against Ontology-platform `main`
> @ `a89a9f2` and Ontology `master` @ `d81598f`. **Phase 1 deletes nothing.**

## 요약 (Korean)

- **제품은 Ontology-platform**(Sion API, 그래프/증거 DB, GraphRAG, 검토, 규정)입니다. **Ontology**(`aec_intelligence`)는
  DXF/DWG/IFC/PDF/래스터 수집과 CAIR 정규화를 맡는 **수집·어댑터 패키지**가 됩니다.
- Ontology는 2026-10-08에 이미 `packages/aec`로 subtree 병합되었습니다. 그 뒤 **양쪽에서 따로 수정**되어 소스가 갈라졌습니다
  (아래 "Drift"). 이것이 가장 큰 위험입니다.
- 1단계(이번 변경): 계약 `aec-cair-snapshot/1` + 얇은 어댑터 `sion_cair.ingest` + `[aec]` extra + 양쪽 계약 테스트.
  Ontology 수집 결과(CAIR)가 Sion 그래프/증거 모델로 들어가는 것을 테스트로 고정했습니다. 코드는 삭제하지 않았습니다.
- 2단계: 드리프트를 해소합니다(Ontology에만 있는 변경을 `packages/aec`로 이식). 그다음 **Ontology `master`를 동결**합니다. 이후 단계 순서는 아래 표에 있습니다.

## 1. Where things are today

| Concern | khs0927/Ontology (`aec_intelligence`) | khs0927/Ontology-platform (Sion) |
|---|---|---|
| Canonical store | JSON/JSONL "repository" (`projects/<id>/03_CAIR/project-cair.json`, `global/*.jsonl`), optional PostgreSQL+pgvector+AGE operational DB (`operational/`) | PostgreSQL/SQLite via SQLAlchemy (`sion_api.models`: Entity, Relation, Evidence, Artifact, Embedding, Outbox), Drive snapshots (`sion_drive_store`) |
| Schema / ontology | CAIR dataclasses (`cair.py`, `CAIR_SCHEMA_VERSION 0.1.0`), `resources/cair-v0.1.schema.json`, `cair-shapes.ttl`, `global/ontology/*` (building/drawing/geometry/gis/provenance) | `migrations/*.sql` (entity/relation type vocabulary), `ontology/core`, `ontology/validation` (SHACL), `schemas/` (SketchUp/BIM assets), `sion_core.contracts` (JSON Schemas for bridged repos), ArchOntos domain (`packages/regulation`) |
| DXF ingestion | `dxf.py` (`DXFParser`, ezdxf, blocks/attributes/title block, sheet metadata), `pipeline.py` (`DXFIngestionPipeline` → CAIR, spatial relations, tables, TTL, graph exports, SVG) | `sion_cad.reader` (the one DXF reader: ezdxf strict → recover, CP949, text fallback), `sion_cad.dxf` (shallow: TEXT/INSERT/LINE/LWPOLYLINE → Concept/Deliverable), `god_cad` (GOD-CAD analysis) |
| IFC ingestion | `formats.py` (`IFCParser`, IfcOpenShell, spatial `contains`, geometry counts) via `format_pipeline.py` | `sion_bim.ifc` (IfcOpenShell or STEP line scanner, GlobalId + Name only) |
| Other formats | DWG (`dwg.py`, LibreDWG/ODA), PDF drawings, raster/DEM, GIS, 3D assets, documents, FreeCAD/Blender/QGIS adapters | documents (md/txt/csv/DOCX/PDF), SketchUp assets/collections, map import |
| Evidence model | CAIR `Provenance` + `Classification(state=REQUIRES_REVIEW/ACCEPT/AUTO_ACCEPT)` per object; `validation_engine.py`; evidence history in `extensions/external_capabilities` | `Evidence` rows on relations/entities (`verification_state` unverified → human_verified/rejected), review queue (`sion_api.review`), advisory memory, ArchOntos evidence/assertions |
| GraphRAG | `operational/graphrag/*` (KG, communities, ask with refusal, knowledge packs, eval) on pgvector; `retrieval.py` (file based) | `sion_graphrag` (LightRAG projection, Apache AGE projection, review report), vector search in `sion_api.vector_repository`, open PR #106 `embedhub` |
| MCP | `mcp_gateway.py` + `mcp_stdio.py` + `mcp_http.py` (`aec.*` tools; `aec-mcp`, `aec-mcp-http`) | no Sion knowledge MCP server yet (see `tests/test_mcp_review_e2e.py`); `sion_cair.adapter` is an MCP **client** of `aec-mcp`; `tools/jev-mcp`, `tools/grokbot-mcp-bridge` |
| HTTP API | `operational/api.py` (`/v1/*`, used by power-cad-mcp, contract §1) | `sion_api` (`/api/v1/*`, including `/api/v1/aec/query` for power-cad, contract §2) |

### Drift between Ontology `master` and `packages/aec` (the subtree)

`packages/aec` was imported around 2026-10-08 (platform PR #26). The import came from a filter-repo copy without binary
blobs, so **commit hashes differ and `git subtree pull` will not apply cleanly**. Since then:

- **Only in Ontology `master`**: Ontology #95 (ArchiOffice pack load review), #97/#98 (`library/gdrive_cad` GraphRAG pack, doc and backup fixes),
  #99 (netguard: never trust DNS for the local-endpoint check), and #100. #100 integrates drafts #80, #83, #84 and #86–#91: read-only bridge
  fail-closed contract and evidence history, worker readiness, bulk scheduling with append-only ingest metrics, and drawing-context CI.
  Files: `library/gdrive_cad/**`, `library/archioffice/tools/*`, `extensions/external_capabilities/readonly_bridges/*`,
  `operational/{worker,catalog,cli,graphrag/*}.py`, `scripts/ops/*.ps1`, tests `test_archioffice_pack_tools`, `test_gdrive_cad_pack_tools`, `test_graphrag_pack_drawings`.
- **Only in `packages/aec`**: DXF reading delegated to `sion_cad.reader`, title-block drawing-number extraction (platform #51),
  the `/v1/search` `model` hint, the docker-cp backup mirror (#76), switch-to-monorepo ops, and platform-side netguard tests.
  Tests: `test_backup_docker_mirror`, `test_netguard`, `test_search_model_hint`, `test_switch_to_monorepo_ops`, `test_title_block_text_number`.
- Both changed: `dxf.py`, `classifier.py`, `dashboard.py`, `mcp_http.py`, `operational/{api,auth,backup,catalog,cli,embeddings,netguard,parsers,worker}.py`, `operational/graphrag/{ask,commands}.py`.

Phase 1 shows the **output contract** still agrees. For `simple_house.dxf` and `simple_house.ifc`, the bundled subtree and Ontology @ `d81598f` produce
the same CAIR object ids
(`tests/test_aec_cair_ingest_contract.py::test_live_bundled_*`).

## 2. Target shape

- **Ontology-platform is the product and the single source of truth for code.** The AEC engine lives in
  `packages/aec` and keeps the import name `aec_intelligence`. It is edited in the monorepo, as AGENTS.md already requires.
- **Ontology becomes the ingestion/adapters package.** Its role narrows to "file → CAIR snapshot"
  (`aec-cair-snapshot/1`). Sion owns everything downstream: canonical graph, evidence, review, GraphRAG, the knowledge MCP server and the API.
- After phase 2 the **khs0927/Ontology GitHub repo** becomes a frozen, read-only mirror with a README pointer to
  `packages/aec`. It is not deleted.
- Option B keeps Ontology as an independently released package: the platform depends on a pinned git revision instead of a subtree. The phase-1 adapter
  supports this (`python=` / `ontology_root=`), but it is **not recommended**: two writable copies are what caused the drift.

## 3. Module-by-module mapping

Legend:

- **keep**: stays where it is.
- **move**: another module takes it over, or it moves into another module.
- **retire**: development stops. Delete it only in a later phase, after its replacement passes the same tests.

### Ontology (`aec_intelligence`, in `packages/aec`)

| Module | Decision | Target / note |
|---|---|---|
| `dxf.py`, `dwg.py`, `formats.py` (IFC/GIS), `operational/pdf_drawings.py`, `raster_adapters.py`, `asset3d_adapters.py`, `document_adapters.py`, `tables.py`, `spatial_relations.py`, `classifier.py`, `secondary_census.py` | **keep** | The ingestion/adapters package. In the subtree, DXF reading already goes through `sion_cad.reader`. |
| `cair.py`, `resources/cair-*.json`, `cair-shapes.ttl`, `global/schemas/cair` | **keep** | CAIR is the ingestion output format. `aec-cair-snapshot/1` freezes it at 0.x; CAIR 1.x needs `aec-cair-snapshot/2`. |
| `pipeline.py`, `format_pipeline.py`, `cli.py` (`ingest-dxf`, `ingest-file`) | **keep** | Producer side of the contract. `sion_cair.ingest` calls them through the CLI. |
| `application_adapters.py`, `freecad_adapter.py`, `blender_adapter.py`, `runtime_adapters.py`, `extensions/external_capabilities` (read-only bridges) | **keep** | Adapters. Their evidence should later flow through the same kind of contract. |
| `repository.py`, `registry.py`, `storage.py`, `graph.py`, `rebuild.py`, `iterations.py`, `global/*` JSONL indexes | **retire** (phase 6) | Replaced by the Sion DB and `sion_drive_store`. Kept while the operational stack and power-cad depend on them. |
| `ontology.py`, `ontology_model.py`, `ontology_alignment.py`, `shacl.py`, `global/ontology/*` | **move** (phase 4) | Vocabulary merges into `ontology/core` and the relation types (`migrations`). SHACL moves into `ontology/validation` (`/api/v1/validation/shacl`). |
| `validation.py`, `validation_engine.py`, `compliance.py` | **move** (phase 4) | Rule checks move to ArchOntos (`packages/regulation`) as evidence-only inputs. Parse validation stays with ingestion. |
| `retrieval.py`, `query.py`, `visual_reasoning.py`, `intelligence_bridge.py` | **retire** (phase 5) | Replaced by `sion_graphrag` and Sion vector search. |
| `operational/graphrag/*` (KG, communities, ask/refusal, knowledge packs, eval) | **move** (phase 5) | Port the refusal rules, knowledge packs and eval set into `sion_graphrag`. Keep it until power-cad `/v1/ask` moves to Sion. |
| `operational/api.py`, `worker.py`, `ingest_jobs.py`, `search.py`, `embeddings.py`, `db.py`, migrations | **keep → retire** (phase 6) | power-cad-mcp uses `/v1/*` (contract §1). Retire only after power-cad switches to Sion endpoints. Embeddings go to `embedhub` (open PR #106). |
| `operational/netguard.py`, `auth.py`, `backup.py`, `drive_checkpoint.py` | **move** (phase 5) | The netguard and auth patterns move into `sion_api` (`http_security`, scopes). Backups move into `sion_drive_store` ops. |
| `mcp_gateway.py`, `mcp_stdio.py`, `mcp_http.py` | **keep → move** (phase 4) | Becomes the `aec.*` namespace of a single Sion MCP server. Until then `sion_cair.adapter` stays the read-only client. |
| `dashboard.py`, `library/archioffice`, `library/gdrive_cad` | **keep** | Knowledge packs and data. `gdrive_cad` must first be ported (phase 2). |

### Ontology-platform (Sion)

| Module | Decision | Note |
|---|---|---|
| `sion_api`, `sion_core`, `sion_graphrag`, `sion_drive_store`, `archontos`, `sion_ingestion` (documents, map import, SketchUp, relations) | **keep** | Product core. |
| `sion_cad.reader` | **keep** | The one DXF reader for every lane. |
| `sion_cad.dxf` (shallow DXF → graph) | **retire** (phase 3) | Superseded by `sion_cair.ingest`, which imports CAIR objects with classification and provenance. Keep the `/api/v1/ingest/dxf` route; switch its implementation behind a flag first. |
| `sion_bim.ifc` (GlobalId+Name scanner) | **retire** (phase 3) | Same as `sion_cad.dxf`. Keep the STEP line scanner as the fallback when ifcopenshell is missing. |
| `sion_cair.adapter` (MCP read-only federation) | **keep** | |
| `sion_cair.ingest` (new, phase 1) | **keep** | The thin adapter for contract `aec-cair-snapshot/1`. |
| `god_cad` | **keep** | Analysis lane; already on `sion_cad.reader`. |

## 4. Phased order

| Phase | What | Exit criteria | Destructive? |
|---|---|---|---|
| 0 (done, 2026-10-08) | Subtree `packages/aec`, one DXF reader, `SION_AEC_ONTOLOGY_ROOT=bundled` | aec CI green in the monorepo | no |
| **1 (this change)** | `aec-cair-snapshot/1` schema with upstream fixtures. `sion_cair.ingest` behind a subprocess boundary (bundled, local path or pinned venv). `[aec]` extra. Producer contract test in Ontology. This plan in both repos | Both suites green; contract tests pass on both sides | no |
| 2 | Drift port: replay Ontology #95, #97, #98, #99 and #100 onto `packages/aec` as patches (`git format-patch` + `git am --directory=packages/aec`, not `subtree pull`). Include #101/#102 if they merge first. Then freeze Ontology `master`: README banner, branch protection, CI only | `diff -r` of `src/ tests/ library/ extensions/` shows only intentional platform-side differences, listed in `packages/aec/UPSTREAM.md` | no (Ontology frozen, not deleted) |
| 3 | Route `/api/v1/ingest/dxf` and `/api/v1/ingest/ifc` through `sion_cair.ingest` behind `SION_INGEST_ENGINE=aec`; it becomes the default after one release. Keep `sion_cad.dxf` and `sion_bim.ifc` as the fallback | The same API tests pass and produce a richer graph; the review queue handles CAIR classifications | no |
| 4 | Merge vocabulary and SHACL. One MCP server (`sion.*` + `aec.*`). Compliance moves to ArchOntos | Tool catalogue test; the SHACL endpoint covers the CAIR shapes | no |
| 5 | Consolidate GraphRAG, embeddings and retrieval into `sion_graphrag` + embedhub. Port the knowledge packs and eval set. Move netguard, auth and backups into Sion | GraphRAG eval parity (refusal and recall) against the `operational/graphrag` baseline | no |
| 6 | power-cad-mcp moves from aec `/v1/*` to Sion endpoints under a new pinned contract. Then retire the aec operational API, the worker and the JSONL repository | power-cad contract tests green against Sion | **yes**: retired modules are deleted only now, one PR each |

## 5. Risks

1. **Two writable copies** (Ontology `master` and `packages/aec`) keep diverging until phase 2, so every fix
   has to be made twice for now. Mitigation: do phase 2 first, then freeze.
2. **`git subtree pull` will not work**: the filter-repo import rewrote the hashes and left out the binaries. Use patch replay instead.
3. **Both distributions use the import name** `aec_intelligence` (`aec-intelligence` and
   `sion-ontology-platform`). Installing both into one venv silently overwrites files. Phase 1 avoids this
   by running Ontology in a subprocess. Never `pip install` Ontology into the platform venv.
4. **power-cad-mcp depends on aec `/v1/*`** (contract §1) and on the `aec-mcp` tools. Retiring the
   operational stack early breaks the CAD host, so that waits for phase 6.
5. **Evidence semantics differ.** CAIR `Classification.state` (`ACCEPT`, `AUTO_ACCEPT`, `REQUIRES_REVIEW`)
   records machine confidence, not human review. The adapter imports everything as `unverified` and keeps the
   CAIR state only as a property. Promoting it to `machine_verified` needs an explicit decision.
6. **Python floor**: Ontology needs `>=3.11` and the platform `>=3.12`. The packaged subtree inherits 3.12.
7. **The open PRs on Ontology** target a repo that is about to freeze (section 6).
8. **Windows-only paths** (ODA/LibreDWG, AutoCAD, Docker on the PC) cannot be verified in cloud CI.

## 6. Open PRs and the plan (2026-11-07; none closed)

| PR | Status under this plan |
|---|---|
| khs0927/Ontology#80, #83, #84, #86, #87, #88, #89, #90, #91 | **Already obsolete**: Ontology #100 (merged) integrated their commits. The owner can close them. |
| khs0927/Ontology#101 (ArchiOffice prune before relink) | **Obsolete as an Ontology PR after phase 2**: port it to `packages/aec/library/archioffice` instead. |
| khs0927/Ontology#102 (draft: interrupted work, Python 3.13/CI, E2E smoke) | **Obsolete as an Ontology PR after phase 2**: port the operational/library fixes and the E2E harness into `packages/aec`. Its CI changes stop mattering once Ontology is frozen. |
| khs0927/Ontology-platform#101 (regulation workflow W05–W13) | Unaffected. |
| khs0927/Ontology-platform#106 (embedhub) | Unaffected. It is the phase-5 target for aec `operational/embeddings.py`. |
| khs0927/Ontology-platform#107 (GraphRAG audit isolation) | Unaffected. |
| khs0927/Ontology-platform#108 (SketchUp rollback) | Unaffected. |
| khs0927/Ontology-platform#109 (NVIDIA auto-ontology, read-only) | Unaffected. |

## 7. Phase 1 usage

```python
from sion_cair.ingest import ingest_cair_snapshot, ingest_with_ontology, run_ontology_ingest

ingest_with_ontology(session, "plan.dxf", "proj-01")                                      # bundled packages/aec
ingest_with_ontology(session, "model.ifc", "proj-01", ontology_root=r"C:\CODE\Ontology")  # local checkout
snap = run_ontology_ingest("plan.dxf", "proj-01", python=r"C:\venvs\aec\Scripts\python.exe")  # pinned venv
ingest_cair_snapshot(session, snap)
```

Install with `pip install -e ".[aec]"` (= `cad` + `bim`). The contract is in `docs/INTEGRATION_CONTRACTS.md` §11.

Producer-side contract test (khs0927/Ontology): `tests/test_consumer_contract_aec_cair_snapshot.py`, with a verbatim copy of the schema in
`tests/contracts/aec-cair-snapshot-1.schema.json`. Consumer-side tests (Ontology-platform): `tests/test_aec_cair_ingest_contract.py`.

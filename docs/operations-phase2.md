# Ingest resilience, DWG cache, graph indexes (Phase 2, 2026-10)

## Schema migrations

`python -m aec_intelligence.operational.cli init-db` is the single migration entry point. It takes a
Postgres advisory lock (so two workers/containers never migrate at once), applies the numbered SQL
migrations recorded in `aec.schema_migrations`, and then creates any missing AGE label indexes on
every project graph. `docker compose up` runs it as the one-shot `migrate` service; `api` and
`worker` start only after it exits 0. `cli graph-indexes` runs just the index step.

### AGE graph indexes

AGE creates label tables without indexes. Each project graph now gets:

| Label kind | Index | Serves |
|---|---|---|
| vertex | `<graph>_<Label>_id` (btree `id`) | edge → vertex joins |
| vertex | `<graph>_entity_props` / `<graph>_<Label>_props` (GIN `properties`) | `MATCH (a:Entity {id: ...})` |
| edge (incl. legacy `contains`, `onStorey`, … labels under `Rel`) | `<graph>_<Label>_start_id`, `<graph>_<Label>_end_id` | expansion, `DETACH DELETE` on re-ingest |

Search relation expansion uses two directed patterns (`->` then `<-`) instead of the undirected
`(a)-[r]-(b)`, which AGE planned as a join over every edge label (≈50 s per hit on a 40k-edge
graph → power-cad-mcp `ontology_search` timeouts). With the indexes each direction takes a few ms.

## Timeouts, leases, attempts

| Variable | Default | Meaning |
|---|---|---|
| `AEC_DB_CONNECT_TIMEOUT_SECONDS` | 10 | libpq connect timeout |
| `AEC_DB_STATEMENT_TIMEOUT_SECONDS` | 30 | interactive (API/search) statement timeout |
| `AEC_INGEST_STATEMENT_TIMEOUT_SECONDS` | 300 | worker projection/graph writes |
| `AEC_LEASE_SECONDS` | 300 (min 15) | job lease; a heartbeat renews it |
| `AEC_MAX_ATTEMPTS` | 3 (min 1) | attempts before a job is FAILED |
| `AEC_ODA_TIMEOUT_SECONDS` | 900 | ODA File Converter per file |

Malformed or below-minimum values raise at startup instead of silently using defaults.
Failed jobs are retried with `POST /v1/jobs/{id}/retry` (token required).

## Embedding outages

A configured `AEC_EMBEDDING_URL` that fails no longer throws away a whole drawing's ingest: the
objects, relations and graph are committed, the embedding write is rolled back to a savepoint, and
the job succeeds with `result.embeddings_pending = true` and `result.embedding_error`. Vectors are
never relabelled as the hash model. `GET /v1/stats` reports `embeddings_by_model` and
`embeddings_pending` for the active `AEC_EMBEDDING_MODEL`. Fill them when the endpoint is back:

```powershell
python -m aec_intelligence.operational.cli reembed --dry-run     # count pending
python -m aec_intelligence.operational.cli reembed               # write vectors of the active model
```

`AEC_EMBEDDING_STRICT=1` restores the old behaviour (the job fails and is retried).

### Ollama (local, RTX 3060 Ti)

Ollama serves the OpenAI-compatible `/v1/embeddings` the client uses. With `bge-m3` pulled:

```dotenv
# host workers / CLI
AEC_EMBEDDING_URL=http://127.0.0.1:11434
AEC_EMBEDDING_MODEL=bge-m3
# containers (api/worker) reach the host via
# AEC_EMBEDDING_URL=http://host.docker.internal:11434
```

The model name is stored with each vector; keep one name per corpus (`bge-m3` for Ollama,
`BAAI/bge-m3` for the compose TEI profile) or re-embed when switching.

## DWG → DXF conversion and cache

* ODA File Converter converts *every* DWG in its input folder, so each job's file is now staged
  alone in a temp folder (`input/source.dwg`, filter `*.DWG`). Previously a job on a Drive folder
  could convert the whole folder and time out.
* Converted DXFs are cached by content hash: `<AEC_DXF_CACHE_DIR>/<sha[:2]>/<sha>.<converter>.dxf`
  (default `<AEC_DATA_ROOT>/dxf-cache`, i.e. `D:\AECData\dxf-cache`). Re-ingests and retries skip
  conversion; job metrics show `dxf_cache: hit|miss`.
* The containers have no ODA. Either run the host worker (`run-workers`) with `AEC_ODA_EXECUTABLE`
  set, or pre-fill the cache on the host — containers see the same folder through the `/data` mount:

```powershell
python -m aec_intelligence.operational.cli convert-dwg --census D:\AECData\census\census.jsonl
python -m aec_intelligence.operational.cli convert-dwg "G:\내 드라이브\some\folder"
```

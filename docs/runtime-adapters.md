# Runtime adapter boundary

The canonical repository is JSON/JSONL CAIR and geometry artifacts. Runtime
systems are rebuildable consumers and must not become the source of truth.

- `DuckDBRuntimeAdapter` materializes global JSONL registries into `runtime/` when DuckDB is installed.
- `RDFLibRuntimeAdapter` and `OxigraphRuntimeAdapter` parse project Turtle exports when their optional dependencies are installed; the `[semantic]` extra installs both runtimes.
- `Neo4jRuntimeAdapter` reports explicit configuration/dependency/connectivity states and can idempotently MERGE the canonical global registries into an operational runtime graph through `ingest_global`. `write_neo4j_cypher` creates a reviewable runtime seed script without making a network call.
- `write_memory_packages` also emits a rebuildable deterministic lexical vector index (`global/09_AGENT_MEMORY/vector-index.jsonl`, with optional Parquet acceleration). It is a supplementary retrieval index, not a geometry or semantic authority.

Missing dependencies return `REQUIRES_DEPENDENCY`; missing credentials return
`REQUIRES_CONFIGURATION`; adapter errors return `FAILED`. There is no silent
fallback that would make a partial runtime look canonical.

# External capability evidence — v0.1

Isolated, derived registry helpers. No CAD launcher, MCP write tools, database
migration, canonical source writes, or duplicate SourceRevision contract.

Imports accept caller-supplied pinned snapshots and provenance. They do not fetch
upstreams or verify that supplied hashes match bytes: the snapshot acquisition
layer must establish those facts before import. CAD-MCP scores, latency, rankings
and launch configuration are deliberately excluded. Imported capabilities remain
declarations with zero verification evidence.

Evidence records scope outcomes to capability/provider/commit/host/version/adapter/
fixture/test kind. Freshness and outcome remain separate; skipped and not-run are
not passes. License disagreements remain visible even with an observed LICENSE;
this is evidence bookkeeping, not a legal compliance determination. No record
confers execution permission.

Run:

```bash
PYTHONPATH=extensions/external_capabilities python -m pytest -q extensions/external_capabilities/tests
```

## Next acceptance boundary

Reuse `extensions/drawing_context/context_fabric/contracts.py`. Reconcile Power CAD
PR #13 and stacked #15 before source binding. A filename/path/handle/semantic match
is candidate discovery only. Separate original byte revision, parser-derived
revision_id, open document session and fresh native state. Hashing disk bytes does
not prove an unsaved in-memory document matches. Recheck inside executor transaction.

Native AutoCAD acceptance, registry snapshot acquisition, evidence persistence,
supersession resolution, HS asset exporter and runtime routing are NOT implemented
by this milestone. Do not advertise any provider as verified from these unit tests.

## Exact-byte snapshot import

`import_snapshot` accepts UTF-8 JSON bytes and calculates the provenance SHA-256
over those exact bytes. Duplicate keys, non-object roots and malformed provider
lists are rejected. Whitespace changes therefore produce a different digest.
This proves local byte integrity only: the caller still must authenticate the
upstream repository and pinned revision during acquisition. No imported
declaration becomes verification evidence or grants execution permission.

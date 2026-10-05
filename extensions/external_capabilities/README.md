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

Native AutoCAD acceptance, registry snapshot acquisition, HS asset exporter and runtime routing are NOT implemented
by this milestone. Do not advertise any provider as verified from these unit tests.

## Exact-byte snapshot import

`import_snapshot` accepts UTF-8 JSON bytes and calculates the provenance SHA-256
over those exact bytes. Duplicate keys, non-object roots and malformed provider
lists are rejected. Whitespace changes therefore produce a different digest.
This proves local byte integrity only: the caller still must authenticate the
upstream repository and pinned revision during acquisition. No imported
declaration becomes verification evidence or grants execution permission.

## Local append-only evidence history

`capability_registry.history.EvidenceHistory(path)` stores bounded JSON events in
SQLite (10,000 events, 64 KiB per event). `add(id, record, supersedes=id)` reuses
`evidence_record`; supersession requires active evidence with exactly the same
capability/provider/commit/host/version/adapter/fixture/kind. `revoke(id, reason=...)`
removes active applicability without deleting the historical observation.

`project(scope=..., now=aware_datetime)` reports current exact-scope observations:
TESTED for current PASS, CONFLICTED for simultaneous PASS and FAIL, STALE for
expired active observations, REVOKED when all applicable records were revoked,
and UNKNOWN otherwise. FAIL/ERROR/SKIPPED/NOT_RUN remain visible as outcomes;
they never become TESTED. Historical observations include their inactive flag.
A scope change cannot inherit an earlier run. No projection returns VERIFIED,
execution permission, or canonical permission.

Writes use SQLite transactions to serialize connections. UPDATE/DELETE are
blocked by triggers, and sequence/hash-chain validation rejects accidental
alteration before reads and writes. This is **not** authenticated or immutable
storage against a database administrator: an attacker can rewrite the entire
chain or truncate its tail without an externally trusted head digest. Producer,
fixture, run URL, timestamps and native-live claims remain caller-supplied.
Lifecycle changes are current administrative state; this API does not reconstruct
historical revocation state for a past `now`. Keep returned `head_digest` in an
independent trusted receipt if stronger continuity checks are required.

No CAD host/version is assumed. Tests use headless fixture declarations only.


## Read-only bridge lifecycle integration

As of 2026-10-05, `EvidenceHistory` had no non-test runtime consumer: references were limited to this README and `test_history.py`. That made the ledger vulnerable to becoming isolated/dead code even though its own contract tests were healthy.

`readonly_bridges.evidence.record_readonly_probe_evidence(...)` now provides one concrete module-level integration point. It records only the headless capability `readonly-bridge-contract`:

- a `DECLARED` bridge projection becomes `PASS` for that contract capability only;
- a `NOT_RUN` projection remains `NOT_RUN`;
- the ledger scope uses `host=headless-contract`, `host_version=1`, and `verification_kind=headless` so it cannot be mistaken for a native CAD-host observation;
- lifecycle projection still returns `execution_allowed: false` and `canonical_allowed: false`.

This removes the previous **test-only ledger isolation**, but one residual dead-code risk remains: there is still no production CLI/API scheduler that invokes this adapter automatically. The integration is callable code with contract tests, not an operational background service. A future runtime caller must preserve the same non-authorizing scope and must not rewrite it as `native-live` without separately collected native evidence.

# ArchOntos evidence projection

This optional headless module accepts a normalized ArchOntos envelope. It does not fetch law.go.kr, parse its wire format, write CAIR/PostgreSQL, or decide compliance. Existing Ontology models remain authoritative; the projection does not create a competing canonical claim model.

`project_law_evidence(metadata, response_bytes=...)` requires upstream repository/commit, source path/file hash, law ID/MST, effective date and an aware observation timestamp. Source-file hash is a supplied upstream provenance assertion; only the response hash is computed here. Acquisition/authentication and source file verification remain separate gates.

A response envelope contains `law_id`, `law_mst`, `effective_from`, and nonempty `articles` with nonempty `text`. Identity/date mismatch, duplicate keys, malformed input and missing article evidence are rejected. Future effective dates are valid evidence, but do not establish current applicability.

| Input | Projection status |
| --- | --- |
| No response bytes, any kind | NOT_RUN |
| mock-fixture bytes | FIXTURE_TESTED |
| official-api-response labeled bytes | RESPONSE_CAPTURED |

The official label is supplied by the caller and is not authenticated here. Even RESPONSE_CAPTURED leaves transport authentication false, applicability unknown, compliance UNDETERMINED and canonical/execution permission false. Original responses should be retained by the upstream evidence store, keyed by computed response SHA-256; this module only emits a read-only projection.

CI runs synthetic fixtures only. No official API E2E, workstation or CAD connection is claimed.

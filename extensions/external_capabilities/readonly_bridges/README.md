# Portable read-only bridge input v1

This is a derived ingestion boundary inside Ontology. It never imports executor tools, starts a CAD process, changes drawings, or writes CAIR canonical records. It reuses the capability registry digest validation.

`ingest_section_catalog(bytes, expected_identity=..., source_files=...)` accepts a normalized HS Steel exporter payload; it does not directly parse .NET validation reports. Identity fields are `provider_id`, `upstream_repo`, `upstream_commit` (40 hex), `source_path`, and `adapter_version`. The caller supplies the expected identity independently. This equality check and supplied byte hashes do not authenticate acquisition.

The payload requires `schema_version: 1`, `identity`, `units` (`dimensions: mm`, `unit_weight: kg/m`, `paint_area: m2/m`), empty `errors`, and nonempty `rows`. Each row carries `source_path`, positive `line_number`, `source_file_sha256`, `parse_success: true`, `designation`, `family`, `raw_value`, six nonnegative `dimensions` (at least one positive), positive `unit_weight`, nonnegative `paint_area`, and integer `aci_color` 0..256. Source file bytes must be supplied and hashed locally. Unit semantics must be confirmed by the upstream exporter; this module does not infer units or prove that a row is correctly parsed from legacy assets. Unknown row fields are discarded.

`ingest_readonly_probe(bytes, expected_identity=..., expected_capabilities=..., transport_state='ok')` supports provider IDs `freecad`, `rhino`, `sketcharch`. The caller supplies the expected capability set independently; the response must match it exactly and may not contain duplicates. A normal response requires `schema_version: 1`, matching `identity`, `read_only: true`, integer `mutation_count: 0`, `authenticated: false`, and `complete: true`. A payload cannot self-promote to authenticated state. Missing/blank `host` or `host_version` produces `NOT_RUN`; a present host must match the provider declaration and the response still remains only `DECLARED`. `transport_state='timeout'` with no bytes and `transport_state='empty'` with empty bytes return a non-authorizing `NOT_RUN` projection; an `ok` transport with empty bytes, a partial response, schema mismatch, capability mismatch, or contradictory authentication claim fails closed. The projection explicitly keeps `probe_authenticated`, `host_identity_verified`, and `native_mapping_verified` false.

Both functions compute a payload SHA-256, preserve only selected projection fields, force execution/canonical/native mapping authority false, and discard incoming VERIFIED claims. Fixtures validate this boundary only. Native host acceptance and actual HS legacy asset coverage remain outstanding. This is not a new SourceRevision or evidence lifecycle store.

Run: `PYTHONPATH=extensions/external_capabilities python -m pytest -q extensions/external_capabilities/tests`


This contract does not prove that a live process exists or that a live document was unchanged. Those claims require a separately authorized native-host validation lane.


## Violation fixtures

The contract tests include independent fixtures under
`extensions/external_capabilities/tests/fixtures/readonly_bridges/` for:

- `schema-mismatch.json`
- `capability-mismatch.json`
- `auth-state-error.json`
- `partial-response.json`
- `timeout.json`
- `empty-response.bin`

These fixtures do not simulate a native CAD host. They only verify that malformed,
contradictory, absent or incomplete contract evidence cannot be promoted.

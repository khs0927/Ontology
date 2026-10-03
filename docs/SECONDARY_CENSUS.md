# Secondary DWG census preparation

`aec_intelligence.secondary_census` compares reports from a primary ODA/ezdxf
path and a proposed ACadSharp direct-read path. It does not run either reader,
replace the parser, connect to a CAD host, or change canonical objects.

The existing operational `census.py` inventories files. This separate contract
compares entities, entity counts per layer and block definition counts. Both
readers must report the original source DWG SHA-256, units and the exact
`model_space_top_level_unexpanded` scope. Block expansion, paper space, xrefs
and unsupported objects must not silently become complete coverage claims.

A report has schema_version=1, source_sha256 (lowercase hex), reader,
reader_version, units, scope, verification_kind (`synthetic` or
`headless_fixture`), and four maps: entities, layers, block_definitions,
unsupported. Map values are nonnegative integer counts. An empty entity census
is rejected. A drawing that is intentionally empty needs a different explicit
fixture policy; it cannot prove parser coverage here.

`load_report(path)` limits input to 4 MiB, rejects duplicate JSON keys and hashes
actual report bytes. `compare_reports(primary, secondary)` returns NOT_RUN for
an absent secondary report, INVALID for malformed input, INCOMPARABLE for
source/unit/kind or reader conflicts, MISMATCH for count differences, INCOMPLETE
when unsupported objects remain, or PARITY for matching counts.

PARITY is count agreement only. Reports are caller supplied: their reader names,
source hashes and verification_kind are not authenticated run evidence.
`independent_verification_proven`, `execution_allowed` and `canonical_allowed`
are always false. Registry promotion needs independently acquired source bytes,
pinned tool/license evidence, original-to-converted provenance, trusted run
receipt and fixture coverage. It must retain reader warnings and measured
elapsed time/peak RAM from the actual invocation. Those invocation/measurement
adapters are intentionally not implemented in this preparation PR.

CI tests use synthetic JSON reports. ACadSharp and DWG fixture execution are
NOT_RUN in this PR; no runtime download or arbitrary subprocess is introduced.

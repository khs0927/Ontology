# Portable captured-source handoff

`source_handoff.build_source_handoff` matches captured bytes against the existing
`SourceRevision.sha256` and reuses `source_byte_revision_id`. Parser identity remains
separate. The receiving adapter must supply its expected capability and adapter IDs
to `validate_source_handoff`; a rehashed scope change is rejected.

This contract is independent of CAD products, operating systems and host versions.
It does not inspect or invoke CAD. `BYTES_MATCHED` describes byte integrity only:
acquisition authentication is false, native validation is `NOT_RUN`, and execution
and canonical promotion are false. The digest is not a signature. Persist captured
bytes and their acquisition evidence separately; no authenticated fetch is implied.

The legacy native source mapping remains unchanged. Linux/macOS CI tests synthetic
captured bytes, tampering, membership isolation and parser revision changes. Passing
CI does not establish native mapping, parser correctness or real drawing acceptance.

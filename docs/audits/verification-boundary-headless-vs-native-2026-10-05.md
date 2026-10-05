# Verification boundary — headless vs native host

Date: 2026-10-05

This note narrows the remaining unverified surface without promoting fixture or CI evidence into native CAD verification.

## Headless/CI verification that is meaningful now

These checks can be executed without starting a CAD host:

| Area | Headless acceptance condition |
| --- | --- |
| schema/version | unsupported schema, missing required fields, wrong types, duplicate JSON keys and non-finite values fail closed |
| identity | caller-owned provider/repository/commit/path/adapter identity must match the response exactly |
| provider/host declarations | contradictory declared identities are rejected; declarations never establish a live host |
| capabilities | returned capability set must match the independently expected contract; unsupported, duplicate, missing or unexpected capabilities fail closed |
| authentication state | payloads cannot promote themselves to authenticated/verified/native status; contradictory authentication claims are rejected |
| transport outcome | timeout, empty response and explicitly partial response produce a non-authorizing NOT_RUN/ERROR result or are rejected before evidence promotion |
| mutation boundary | read_only must be true and mutation_count must be the integer zero; any mutation declaration is rejected |
| authority | execution_allowed, canonical_allowed and native_mapping_verified remain false for contract-only evidence |
| evidence lifecycle | append-only ordering, scope matching, supersession, revocation, expiry, tamper detection and non-promotion can be tested with synthetic evidence |
| portability | path handling and fixture behavior can be tested on Linux/macOS/Windows runners |

Passing these checks means only that the **contract rejects known-invalid or contradictory evidence**.

## Verification that requires a real host

The following cannot be established by fixtures or CI declarations:

- whether FreeCAD, Rhino or SketchArch is actually installed, running and reachable;
- whether a response really came from that host rather than a fixture or forged payload;
- whether the reported host/version matches the live process;
- whether a real document remained unchanged while a read-only probe ran;
- whether host APIs expose the declared capabilities on the installed version;
- actual CAD GUI behavior;
- AutoCAD 2027 integration;
- native write, transaction, rollback and write receipt behavior;
- fidelity against original DWG/native assets, including host-specific rendering and object semantics.

These remain **UNVERIFIED_NATIVE** until a separately authorized native validation lane exists.

## Fail-closed rule

A headless contract may emit only non-authorizing states such as DECLARED, NOT_RUN, ERROR, STALE, REVOKED or UNKNOWN. It must not infer VERIFIED, execution permission, canonical authority, authenticated acquisition, native mapping, or live document integrity from caller-supplied fields.

## Current work order

1. Harden read-only bridge fixture contracts for schema/capability/authentication/transport failures.
2. Connect at least one real module boundary to the append-only evidence lifecycle so the ledger is not test-only dead code.
3. Leave live-host acceptance isolated and explicitly unverified.

## JEV advisory

JEV `jev-1.13.0` was used only to review whether this boundary is conservative. The result was `threshold_status=uncalibrated`, `advisory_only=true`. It is not a probability, completion metric, or native verification result.

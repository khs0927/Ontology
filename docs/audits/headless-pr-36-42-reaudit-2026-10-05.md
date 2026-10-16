# Headless PR #36–#42 re-audit — 2026-10-05

Scope: headless/CI only. Excluded: live CAD connections, AutoCAD 2027, native writes, GUI probes, and original assets. No merge is authorized or performed.

## Current baseline

- Current `master` tip at this audit: `23bdaa292d9de13be9ffe619734dffc2c82e8941` (#82).
- No later master commit was observed after #82 at the time of this audit.
- PR #50 (`eba880080ce58aec71dce204fdf6242ed96d7bce`) merged the substantive code from #36–#40 onto master.
- #50 head CI:
  - External capability evidence contracts run `37173354500`: success
  - Ontology intelligence safety run `37173354496`: success

## Disposition

| PR | Historical PR state | Current disposition | Evidence |
| --- | --- | --- | --- |
| #36 ArchOntos law evidence projection | closed, not individually merged | **absorbed via #50; keep closed** | #50 contains `archontos_bridge`; current master still contains the module and its tests. The test blob matches the old branch. The implementation is present on master with a small later cleanup difference, so rebasing the closed branch would risk reintroducing older code. |
| #37 portable captured-byte handoff | closed, not individually merged | **absorbed via #50; keep closed** | Current master `source_handoff.py` and `test_source_handoff.py` match the old branch blobs. |
| #38 portable steel catalog / read-only bridge declarations | closed, not individually merged | **absorbed via #50; keep closed** | Current master read-only bridge module and tests match the old branch blobs. |
| #39 bounded headless secondary DWG census contract | closed, not individually merged | **absorbed via #50; keep closed** | Current master `secondary_census.py` and tests match the old branch blobs. No secondary reader invocation is implied. |
| #40 capability evidence lifecycle | closed, not individually merged | **absorbed via #50; keep closed** | Current master evidence history module and tests match the old branch blobs. |
| #41 pinned dependency snapshot | closed, not merged | **obsolete snapshot; keep closed** | Its own close note states that #34–#40 had moved/closed, invalidating the pinned-head snapshot. Rebase is not useful because the document's purpose was point-in-time dependency capture. |
| #42 cross-repository CI duplication audit/pilot | closed, not merged | **obsolete audit/pilot; keep closed** | Its own close note states the workflow snapshot no longer matched current workflows and the pilot did not replace authoritative jobs. Any future reusable-workflow work should start from a fresh audit of current workflow files. |

## File checks against current master

Representative code/test comparisons:

- #36:
  - `extensions/external_capabilities/tests/test_archontos_bridge.py`: identical blob to old branch.
  - `extensions/external_capabilities/archontos_bridge/__init__.py`: present on master but not identical; master contains a later cleanup. Treat master as the newer implementation.
- #37:
  - `extensions/drawing_context/context_fabric/source_handoff.py`: identical.
  - `extensions/drawing_context/tests/test_source_handoff.py`: identical.
- #38:
  - `extensions/external_capabilities/readonly_bridges/__init__.py`: identical.
  - `extensions/external_capabilities/tests/test_readonly_bridges.py`: identical.
- #39:
  - `src/aec_intelligence/secondary_census.py`: identical.
  - `tests/test_secondary_census.py`: identical.
- #40:
  - `extensions/external_capabilities/capability_registry/history.py`: identical.
  - `extensions/external_capabilities/tests/test_history.py`: identical.

These content checks are supporting evidence only. The main integration evidence is PR #50 plus its successful CI.

## Rebase / conflict decision

No #36–#42 branch should be rebased as a merge candidate:

- #36–#40: functionality is already integrated through #50.
- #41: point-in-time snapshot is obsolete by design.
- #42: audit snapshot is obsolete; a new audit must use current workflows.

Thus no active merge conflict needs repair in these seven old branches. They are historical/stale branches, not current integration candidates.

## Verification boundary

Validated here:
- repository state,
- PR history,
- current file presence/content checks,
- headless CI history.

Not validated:
- live CAD behavior,
- AutoCAD 2027,
- Rhino/FreeCAD native execution,
- native write,
- GUI,
- original drawing assets,
- production host behavior.

## JEV advisory

JEV `jev-1.13.0` was used only as advisory input for the conservative disposition above. The result was `threshold_status=uncalibrated`, `advisory_only=true`. It is not a completion percentage, probability of success, merge approval, or native verification.

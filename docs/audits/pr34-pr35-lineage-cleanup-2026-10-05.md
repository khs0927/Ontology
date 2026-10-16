# PR #34 / #35 lineage cleanup — 2026-10-05

Scope: repository lineage and headless/CI evidence only. No live CAD, AutoCAD 2027, native write, GUI probe, or original asset validation.

## Historical relationship

Both closed draft PRs were based on the same legacy extraction branch:

- base: `claude/project-thread-pkpomf`
- base SHA: `da5e57d15602322368a628ffd038cb0ee1c5e84b`

They also both modified:

- `src/aec_intelligence/classifier.py`
- `src/aec_intelligence/operational/parsers.py`

That shared base predates the later master integration path and should not be revived as a current merge base.

## #34 disposition

PR #34 (`agent/pr20-extraction-recall`) is closed and not individually merged.

Its extraction work was moved onto then-current master through merged PR #49:

- #49 title: `feat(extraction): 중첩 블록 텍스트·다중 도곽 분리·worker timeout 통합 (#34 대체)`
- merge commit: `c44ac8029db3c6ca4bdffc384c48b29ebe1e42b9`
- #49 explicitly records that the old branch conflicted because it was based before the #32 squash.

Therefore #34 is a historical implementation branch, not a current integration candidate.

## #35 disposition

PR #35 (`codex/drive-ingest-extraction-v2-20261003`) is also closed and not individually merged.

Its overlapping extraction work was superseded by #49. The close note identified several settings as #35-only follow-up work at that time:

- `AEC_LEASE_SECONDS`
- `AEC_MAX_ATTEMPTS`
- DB connect timeout
- DB statement timeout

Those follow-ups are now present on current master:

- `src/aec_intelligence/operational/config.py` reads `AEC_LEASE_SECONDS` and `AEC_MAX_ATTEMPTS`.
- `src/aec_intelligence/operational/db.py` supports connect and statement timeout settings.
- `.env.example` documents `AEC_DB_CONNECT_TIMEOUT_SECONDS`, `AEC_DB_STATEMENT_TIMEOUT_SECONDS`, `AEC_LEASE_SECONDS`, and `AEC_MAX_ATTEMPTS`.
- `tests/test_phase2_ingest_resilience.py` covers the environment-backed lease/attempt and DB timeout behavior.

Therefore there is no identified #35-only headless setting that needs recovery from the stale branch in this audit.

## Base cleanup decision

Do **not** retarget or rebase #34 or #35.

Reason:
1. both PRs are already closed;
2. both are based on a pre-integration branch;
3. their shared extraction work was superseded by #49 on master;
4. the previously deferred #35 settings are now present on master;
5. retargeting would rewrite the meaning of historical reviews and can reintroduce pre-squash conflicts.

Use current `master` as the base for any new extraction change.

## Status

- #34: historical / superseded
- #35: historical / superseded
- legacy base `claude/project-thread-pkpomf`: not a current integration base
- current master remains the only base for follow-up headless work

## JEV advisory

JEV `jev-1.13.0` was consulted only as advisory input for the conservative lineage decision. The result was `threshold_status=uncalibrated` and `advisory_only=true`. It is not a success probability, readiness metric, or merge approval.

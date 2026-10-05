# Font-substitution observability evidence — 2026-10-05

Scope: headless/CI evidence only. No CAD host, AutoCAD 2027, native write, GUI probe, or original asset validation is claimed.

## Verified repository state

- PR #82 was opened from `fix/font-substitution-observability`.
- PR head at merge: `c943fd59b8955837460f5432cd3a67dcff3f45a6`.
- Merge commit on `master`: `23bdaa292d9de13be9ffe619734dffc2c82e8941`.
- GitHub compare `master...fix/font-substitution-observability` still reports the branch as ahead 3 / behind 1. This is a history relationship and is not by itself a statement that current tip contents differ.

## Failure isolation

Two pre-merge PR runs failed only in `intelligence-safety > Test core package`:

| run | observed failure | interpretation |
| --- | --- | --- |
| 37278821214 | `test_a_font_that_cannot_be_resolved_is_collected_from_a_real_render`: `watch.missing == set()` | Linux runner did not produce the assumed last-resort substitution for the synthetic drawing |
| 37280093321 | `test_the_hook_is_reached_by_the_real_renderer`: `seen == []` | Linux runner did not route this synthetic render through the wrapped `make_font` call |

In both runs, `operational-postgres` and `app-image` succeeded. The failures therefore did not establish a repository-wide intelligence-safety regression.

The branch then replaced the render-dependent assertion with deterministic contract tests and finally removed the synthetic real-render assertion in commit `c943fd59`.

## Reproduction / re-run evidence

The final PR-head workflow run was re-run after the investigation:

- run: `37280821496`
- re-run job: `111679341488`
- job: `intelligence-safety`
- result: **success**
- `Test core package`: **success**
- MCP intelligence bridge: **success**
- intelligence extension: **success**
- drawing-context safety contracts: **success**

This supports the narrow conclusion that the earlier red runs were caused by the host/render-dependent test condition rather than by the final font-observability implementation.

It does **not** prove every ezdxf font-resolution path, every platform, or every production drawing.

## Current tip content check

Current master and branch tips were read independently through the GitHub contents API.

| file | master blob | branch blob | current content result |
| --- | --- | --- | --- |
| `src/aec_intelligence/operational/parsers.py` | `ca7925194943cc2ec90a6b4a25d983753b8fcbc9` | `ca7925194943cc2ec90a6b4a25d983753b8fcbc9` | identical current blob |
| `tests/test_font_substitution_warning.py` | `92f8b385f339d75dd88cd87efe22b86f22b67ced` | `92f8b385f339d75dd88cd87efe22b86f22b67ced` | identical current blob |

Therefore the branch is **stale/diverged in history**, while the two files changed by PR #82 have the same current content at the compared tips. The GitHub compare endpoint still lists those files because it reports changes introduced on the head side since the merge base, not a simple "tip blobs differ" assertion.

## Commit-by-commit disposition

1. `8ddcf459` — implementation of resolver wrapping, warning collection, and the first render-dependent test.
   - implementation behavior is present in current master.
   - the original render-dependent test form is not retained.

2. `518c27d0` — replaced the first platform-sensitive assertion with a different render-path assertion plus deterministic resolver tests.
   - deterministic resolver tests are retained in final content.
   - the real-render hook assertion was later removed.

3. `c943fd59` — removed the remaining real-render-dependent case.
   - this is the final branch state and is represented by current master content.

No branch-only production behavior was found in the two PR #82 files at the current tips. Any broader equivalence outside those files is **unverified**.

## Operational conclusion

Status: **merged behavior + stale branch history**.

The two historical red runs should not be used as evidence that current `master` is broken. The final PR-head CI re-run is green. No further production-code patch is proposed here.

## JEV advisory

A JEV score was requested only as advisory input for whether a documentation-only evidence PR is conservative. The returned result was from `jev-1.13.0`, `threshold_status=uncalibrated`, `advisory_only=true`. It is not a probability, readiness score, merge approval, or verification result.


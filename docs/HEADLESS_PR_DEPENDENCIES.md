# Headless draft PR dependency snapshot

Baseline: `fd1bce4464d522c9e07f40678f3445292267c1bf`. Exact reviewed heads and paths are in [the manifest](HEADLESS_PR_DEPENDENCIES.json). This is a review snapshot, not authorization to merge or a guarantee about future heads. PRs 36–40 remain separate and open; no source files from them are included here.

| PR | Runtime code dependency already on baseline | Hard dependency on another reviewed PR |
|---|---|---|
| 36 | Python standard library | None |
| 37 | `SourceRevision`, `digest` in contracts; `source_byte_revision_id` in source_mapping | None |
| 38 | `capability_registry._hash` | None |
| 39 | Python standard library | None |
| 40 | `capability_registry.evidence_record`, `project_evidence` | None |

All five diffs have disjoint changed paths and apply sequentially to this baseline using `git apply --check` followed by `git apply` in a detached temporary worktree. GitHub also reports each independently mergeable at the recorded heads. This checks textual applicability, not semantic interoperability. PR38 imports a private `_hash` helper: it exists on this baseline but future internal changes may break that coupling.

The optional producer/consumer relationships in the manifest describe potential future integration, not imports or interchangeable schemas. A separate adapter must translate projection identities and outcomes into the ledger's exact evidence kind/scope. Captured-byte equality does not authenticate acquisition. Probe declarations and synthetic fixture passes must not be promoted to native VERIFIED evidence.

PR34/35 are excluded from this independent assessment. They target `claude/project-thread-pkpomf`, not master, and overlap each other in extraction code (`src/aec_intelligence/classifier.py` and `src/aec_intelligence/operational/parsers.py`, the GitHub PR changed-file intersection). PR34 head is an ancestor of PR35 head at this snapshot (`git merge-base --is-ancestor` succeeded). Shared paths alone do not prove a merge conflict; their extraction behavior requires separate review. None of PR36–40 imports their changed extraction APIs or changes those paths. No merge order is imposed by the five reviewed contracts; conceptual integration order is optional.

Combined local fixture checks on the temporary composed tree: source handoff/source mapping unittest suite 26 passed; external capability suite 68 passed; secondary census suite 19 passed. These are synthetic/headless boundary tests only. Original assets, official API acquisition, CAD processes, GUI and native writes were not run. The docs CI validates manifest consistency and the master prerequisites without pulling or running untrusted PR source.

JEV initial dependency proposition: 0.46; factual report review: 0.59, jev-1.13.0, uncalibrated/advisory. This is not independent merge approval.

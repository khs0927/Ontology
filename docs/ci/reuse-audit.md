# CI reuse audit — 2026-10-03

Scope: 9 repositories, all 18 requested workflow files. The adjacent JSON records exact commit and full source for every file. Findings concern commands and environments, not workflow names. No merge, native CAD, GUI, original assets or release execution is authorized here.

## First shared workflow

Central host: **khs0927/Ontology**, `.github/workflows/python-headless.yml`. The repository is confirmed public and accessible. A probe of khs0927/.github returned 404; it is not an available central host. This small infrastructure workflow imports no subsystem code into the knowledge kernel.

Contract: hosted Ubuntu, Python version input, caller-owned scripts/ci/*.py input; checkout and Python bootstrap centrally, contents:read, no inherited secrets, no credential persistence, 15-minute bound, failures propagated. The local script owns exact installation and validation policy. Initial scope excludes Windows/macOS, caching, artifact upload, deployment and native probes. Those require separate contracts.

Cross-repository job syntax after review:
```yaml
jobs:
  tests:
    permissions:
      contents: read
    uses: khs0927/Ontology/.github/workflows/python-headless.yml@FULL_REVIEWED_COMMIT_SHA
    with:
      python-version: '3.12'
      validation-script: scripts/ci/headless.py
```
FULL_REVIEWED_COMMIT_SHA is an illustrative placeholder, never an active caller. Checkout resolves caller source, not the central repository. Verify Actions allowlists and accessibility before migration. Keep the existing workflow enabled during pilot; the additional pilot intentionally duplicates one tiny suite temporarily. It is not a completed deduplication.

## Repeated patterns across at least three repositories

| Pattern | Actual repositories | Judgment |
|---|---|---|
| checkout → setup-python → dependency install → pytest | Ontology, Ontology-platform, power-cad-mcp, HS-CAD, All-In-Cad, GOD-CAD, ArchOntos | First reusable bootstrap; installation is not identical and remains local |
| Python compileall | Ontology, HS-CAD, CAD-MCP, ArchOntos | Shared candidate only; paths and ordering remain local |
| Ruff and pytest lanes | power-cad-mcp, All-In-Cad, GOD-CAD, ArchOntos | Later candidate; distinct lint paths, format checks, locks and extras prevent blind replacement |

.NET 10 restore/build/test is common to only power-cad-mcp and hs-steel-cad. All-In-Cad builds and probes .NET projects but does not repeat that test pipeline: it does not qualify as a three-repository equivalent. Artifact uploads share mechanics but purpose, retention and sensitivity differ.

## Direct workflow findings and responsibility split

| Repository / files read | Actual overlap and distinctions | Remains local |
|---|---|---|
| Ontology: external-capabilities, intelligence-safety | External contracts are not covered by safety extension checks. Safety runs tests then test_intelligence_bridge.py again; investigate removing the subset after equivalent failure visibility. PostgreSQL lane has a real DB and is not redundant with no-DB tests. | Extras, extension PYTHONPATH, Docker AGE/PostGIS/pgvector fixture and operational assertions |
| Ontology-platform: hindsight-advisory, tests | Advisory subset overlaps whole suite, but test vs test,graphrag extras and path filters differ. Not proven environment-equivalent. | Fast-lane trigger intent, advisory coverage, runtime dependencies |
| power-cad-mcp: ci, release | build_dotnet.sh repeated in package and release. Release has tag gating and publication; keep release semantics. Ubuntu/Windows and three Python versions are meaningful matrix coverage. | DXF smoke, .NET build/tests, Windows packaging, wheel smoke, tags and release token |
| HS-CAD: ci, build-windows-release, corpus-foundation, hscad_private_remote_worker, mobile-remote-control | ci and manual python-tests use same pytest flags; manual rerun is intentional. Windows release repeats pytest with build extras and different output. Corpus uses requirements; worker uses explicit packages and Drive secrets. | Windows installer, corpus fixture, worker/Drive effects, manual control and artifact retention |
| All-In-Cad: ci, upstream-audit | check_pins.py repeated offline. Online audit adds network verification, schedule, token and evidence artifact. Offline check is not a substitute. | Pin/license policy, scheduled online audit, native policy, ACadSharp protocol probe |
| CAD-MCP: ci | ezdxf install, compile, JSON, fixture generation, unittest and score isolation. Not a pytest/editable lane. | Registry declarations, synthetic score quarantine, fixture and unittest commands |
| GOD-CAD: ci | Python/Ruff/pytest pattern; lock install plus no-deps editable differs from extras install. Ubuntu/Windows matrix meaningful. | Lock policy, schema drift, synthetic demo and artifacts |
| hs-steel-cad: ci, validation | Ubuntu checkout/setup-dotnet10/restore/build Release no-restore/test no-build are identical in both. Strongest removable duplicate. | Solution/project choice, strict parser tests, Windows coverage; keep required status compatibility until protection migration |
| ArchOntos: ci | Editable dev install, Ruff, pytest, compileall: good second Ubuntu consumer. | Regulation fixtures, local lint/test paths and dependency policy |

Central only: bootstrap action versions, hosted runner, bounded execution, checkout permissions and validated invocation. No local triggers, matrices, secrets, fixture policies, releases or domain commands are centralized.

## Migration and rollback

1. Review central draft and run internal pilot while existing CI remains authoritative. Validate syntax, normal execution and nonzero exit propagation. No downstream reference to master or branch.
2. After a reviewed immutable SHA exists, pilot ArchOntos and Ontology-platform in separate drafts; reproduce their exact install commands and tests in caller scripts. Record resolved SHA and Actions access check.
3. Migrate All-In-Cad and GOD-CAD only after preserving action versions, lock/headless extras and lint/format/schema semantics. Migrating v7 actions to pinned v4/v5 bootstrap is a runtime change, not dedup-only.
4. Add a separately reviewed portable runner/matrix contract before power-cad-mcp or HS-CAD matrix migration. Never replace Windows jobs with Ubuntu.
5. Independently deduplicate hs-steel-cad Ubuntu lane after branch-protection required-check inventory. Keep a compatible status/aggregate job until required checks are deliberately updated.
6. Only remove old jobs once equivalent coverage, check names and failure behavior are observed. Revert caller to previous local workflow or previous central SHA on regression.

Risks: required-check renaming; central workflow access/allowlists; centralized compromise; action/runtime version drift; divergent extras/lock installation; accidental fixture skips; expression injection; artifact retention and secret scope. Caller script path is environment passed and resolved, never interpolated into shell code. Do not use secrets:inherit or pull_request_target. Savings are not measured; no duration or cost claim.

## JEV advisory

JEV Noul model jev-1.13.0 returned 0.72 on conservative bootstrap reuse with local policy and staged rollout. Response: threshold_status=uncalibrated, advisory_only=true. This is not CI evidence, completion probability or an approval threshold. Inputs explicitly included nine-repository evidence, strong two-repository .NET limit, required-check/access/action drift and no-merge scope. The code/command audit governs the decision.

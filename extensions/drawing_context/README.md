# Drawing Context Fabric — opt-in reference implementation

2026-09-29. Target: **khs0927/power-cad-mcp, AutoCAD 2027, C#/.NET 10 primary,
C++ ObjectARX hot paths when justified, COM fallback only**.

이 확장은 기존 Ontology 파서·CAIR·원본 연결 구조를 수정하지 않습니다.
기존 결과를 읽어서 재생성 가능한 별도 검색 카탈로그로 투영합니다.
전체 설계는 [FRAMEWORK.ko.md](FRAMEWORK.ko.md), Power CAD 업그레이드는
[POWER-CAD-UPGRADE.ko.md](POWER-CAD-UPGRADE.ko.md)를 보세요.

## Implemented and tested

- Immutable source/revision and embedding-space contracts.
- Read-only CAIR 0.1 and operational snapshot adapters; canonical evidence retained.
- Separate SQLite/FTS5 reference catalog, idempotent imports, immutable conflict checks.
- Explicit revision compare-and-swap, revocation, source-scope filtering before limit.
- Candidate rank fusion and deterministic ingestion planning; no per-entity MCP calls.
- RAGFlow **internal projection DTO** export, retaining external-to-canonical mapping.
- Power CAD candidate validation against caller-supplied native observations.
- Real ezdxf integration test using the **existing** operational parser, two layouts,
  Korean text, source checksum preservation and handle-bearing search results.

## Not deployed / not claimed

No Drive account scan, OAuth refresh, background service, RAGFlow server/API upload,
production PostgreSQL migration, embedding inference, graph inference, AutoCAD binary,
C++ plugin, or live CAD operation is implemented by this increment.
The planner emits jobs; it does not run a queue. The live guard verifies an adapter's
observations; it does not independently contact AutoCAD and never authorizes mutation.
The reference catalog is not intended as a second canonical database or a TB-scale engine.

## Run checks from the Ontology repository root

```bash
PYTHONPATH=src:extensions/drawing_context python -m unittest discover -s extensions/drawing_context/tests -v
```

21 tests at initial verification; the real-parser integration test requires the already
used `ezdxf` dependency. Without it that test is explicitly skipped. No cloud API required.

CLI import requires a captured source whose SHA-256 matches an explicit manifest:

```bash
PYTHONPATH=src:extensions/drawing_context python -m context_fabric import-snapshot \
  --snapshot /data/canonical/rev-1.json --source /data/manifests/source.json \
  --captured-file /data/captured/original.dwg --out /data/derived-context

PYTHONPATH=src:extensions/drawing_context python -m context_fabric search \
  --catalog /data/derived-context/catalog.sqlite3 --query '화장실 출입문' \
  --allowed-source SOURCE_ID_FROM_IMPORT --project PROJECT_ID
```

`--out` must be in a separate directory tree from input files. The CLI prints the
source/revision IDs and projection DTO; it makes no network calls. On revision replacement,
pass `--expected-current PREVIOUS_REVISION_ID`. This is deliberate CAS, not automatic
selection of the newest filename. The CLI's allowed-source option is for trusted local
testing; a deployed gateway derives memberships from authenticated authorization state.

Manifest example (replace hash with captured bytes, not this placeholder):

```json
{
  "account": "opaque-account-id", "corpus": "my-drive", "file_id": "drive-file-id",
  "project_id": "P1", "revision": "1",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "name": "1층평면도.dwg", "format": "dwg", "parser": "existing-oda-ezdxf",
  "parser_version": "pinned-build-and-options-id", "units": "mm"
}
```

## Integration boundaries

1. Preserve `src/aec_intelligence`, `projects`, `global` and existing databases.
2. Do not replace `stable_object_id`, CAIR schema, registry or source mappings.
3. Extension IDs scope existing IDs to source membership, revision and parser version.
4. Existing operational parser expects a parent directory equal to source SHA-256.
   Stage a **copy** at `captured/<sha256>/<name>` and verify bytes before invocation.
   Do not silently rewrite bad historical provenance. Quarantine it for reprocessing.
5. Multi-source CAIR snapshots must be split by verified source hash before import.
6. Missing layout/units/handle stays unknown. No image coordinate is treated as CAD WCS.
7. RAGFlow retrieval returns candidates; resolve canonical IDs through the catalog
   and current authorization before showing content or constructing Power CAD context.
8. Do not mount live DB volumes on Google Drive. Drive stores files and consistent exports.

Runtime cache paths should be outside the repository. No dependency or service was added
to the existing root package; this increment is opt-in through PYTHONPATH.

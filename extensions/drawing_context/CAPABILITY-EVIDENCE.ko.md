# External capability evidence + source mapping

기준일: 2026-10-03

이 확장은 외부 CAD 프로젝트를 Ontology에 합치는 registry가 아니다.
목표는 **각 capability의 원본·검증 근거·유효성·배포 상태를 분리해서 기록하고,
검색된 Ontology 객체가 실제 열린 CAD 객체와 같은 원본에서 왔는지 read-only로 증명**하는 것이다.

## 경계

- Ontology: SourceRevision, parser result, provenance, candidate, read-only binding evidence
- Registry: capability declaration과 검증 evidence의 재생성 가능한 projection
- Sion/router: provider 선택, 세션 조정, 요청 trace
- Power CAD/executor: live state, 승인, single writer, transaction-time revalidation, receipt
- Evidence ingestion: executor receipt/관찰을 원본에 연결하되 CAIR를 자동 덮어쓰지 않음

Registry는 실행 권한을 발급하지 않는다.

## SourceRevision 식별자 분리

기존 SourceRevision을 그대로 사용한다.

- source_id: Drive/file/project membership identity
- source byte revision id: source_id + source revision + SHA-256
- parser revision id: 기존 SourceRevision.revision_id. parser + parser_version 포함
- live document state: session_id + document_id + state_digest

같은 원본 bytes를 다른 parser version으로 다시 해석하면 source byte revision은 같고
parser revision id는 달라진다.

## Binding state

### CANDIDATE

검색/분류로 찾은 후보다. basename, handle, semantic score만으로 더 높은 상태가 되지 않는다.

### SOURCE_BOUND

다음 read-only 근거가 모두 일치할 때만 된다.

- trusted resolver의 source_id
- source byte revision id
- resolved file SHA-256
- immutable cache entry ID
- resolver receipt SHA-256
- trusted issuer / trust domain / signature key ID
- resolver 경계에서 detached signature 검증 완료
- 실제 열린 native path
- live file SHA-256
- layout
- native handle
- nested instance path
- native mapping observation
- live object fingerprint/state digest
- live modification generation

SOURCE_BOUND는 수정 허가가 아니다.

dirty document도 어떤 원본에서 시작했는지 SOURCE_BOUND가 될 수 있지만, 기존
verify_live_candidate guard는 REQUIRES_REVIEW를 반환한다.

### EXECUTION_AUTHORIZED

Ontology는 이 상태를 만들지 않는다.

Power CAD가 특정 plan에 대해 현재 document/object state를 transaction 안에서 다시
확인하고 사용자/정책 승인과 single-writer 조건을 만족했을 때만 executor 내부에서 판단한다.

## Capability evidence model

저장 모델은 하나의 status로 합치지 않는다.

- verification kind: static / unit / simulator / headless / native-live
- result: PASS / FAIL / ERROR / SKIPPED / NOT_RUN
- validity: CURRENT / STALE / REVOKED
- license: CONFIRMED / UNKNOWN / CONFLICTED
- deployment: UNINSTALLED / VERSION_MISMATCH / RESPONSIVE / FAILED

UI/manifest projection만 DECLARED / TESTED / VERIFIED / STALE / REVOKED / CONFLICTED로 요약한다.

VERIFIED projection 조건:

1. capability가 요구한 validation lane이 전부 CURRENT + PASS
2. upstream commit 존재 확인이 기록됨
3. license evidence가 CONFIRMED
4. active evidence conflict가 없음

검증 record는 덮어쓰지 않고 새 record가 supersedes/revokes로 이전 근거를 대체한다.
valid_until이 지난 PASS는 STALE로 projection된다.

## 외부 registry import 규칙

CAD-MCP의 기존 benchmark score/pass-rate/ranking은 import하지 않는다.
provider/test ID별 PASS/PARTIAL이 코드에 지정된 synthetic 결과이기 때문이다.

All-In-Cad lock도 바로 VERIFIED로 가져오지 않는다. 각 pin에 대해:

1. upstream repository에서 pinned commit 존재 확인
2. 실제 source file hash 기록
3. upstream LICENSE 파일과 hash 확인
4. metadata 충돌 보존
5. validation evidence 생성

후에 declaration/provenance snapshot을 만든다.

## 첫 acceptance

첫 완료 목표는 아래 E2E다.

SourceRevision
→ trusted cached/resolved source
→ 실제 열린 AutoCAD document
→ layout/handle/instance path
→ fresh native fingerprint/state digest
→ SOURCE_BOUND

명령:

    python scripts/verify_source_mapping.py \
      --source runtime/source.json \
      --record runtime/record.json \
      --resolution runtime/resolution.json \
      --live runtime/live.json \
      --require-review-ready \
      --out runtime/source-binding-report.json

exit 0은 SOURCE_BOUND이며, --require-review-ready 사용 시 기존 live guard도
VERIFIED_FOR_REVIEW여야 한다.

이 도구는 CAD를 수정하지 않는다.


## Resolver trust boundary

Ontology는 서명 알고리즘/키 관리를 새로 구현하지 않는다. 인증된 source resolver가
receipt의 detached signature를 검증하고 다음 attestation을 전달해야 한다.

- resolver_issuer
- trust_domain
- signature_key_id
- receipt_signature_verified=true
- resolver_receipt_sha256
- immutable cache_entry_id

이 중 하나라도 없거나 signature verification이 false이면 source resolution 자체를
신뢰하지 않고 SOURCE_BOUND에 사용하지 않는다.

receipt hash만 존재하는 것은 신뢰 근거가 아니다. hash는 검증된 receipt의 불변 식별자로
사용하고, 실제 issuer/key 신뢰 정책은 resolver 운영 경계에서 관리한다.

# LightRAG 비교 어댑터 — Ontology Drawing Context

작성/검증일: 2026-10-02 (KST)

## 목적

LightRAG를 기존 CAIR/원본/provenance를 대체하는 저장소로 사용하지 않는다.
RAGFlow와 같은 benchmark fixture와 promotion gate로 비교할 수 있는
**파생 검색 sidecar 후보**로만 다룬다.

기준 릴리스는 `v1.5.7`이다. 활성화할 때는 실제 컨테이너 image digest를
`sha256:...`로 고정해야 한다.

## 사용 API

비교 검색은 LightRAG의 `POST /query/data`를 사용한다.

이 경로는 답변 생성보다 retrieval 자체를 비교하기에 적합하며
entities / relationships / chunks / references를 구조화해 반환한다.

우리 어댑터는 현재 **chunks만 benchmark hit로 사용**한다.
remote `chunk_id`는 canonical ID가 아니다.

```text
LightRAG chunk_id
      ↓
runtime/lightrag/bindings.json
      ↓
drawing-context external_id
      ↓
canonical_id + source_id + revision_id + sha256
```

local binding이 없는 chunk는 provenance를 추정하지 않는다.

## 보안

LightRAG 서버는 인증 설정 없이 네트워크에 노출하면 안 된다.

이 어댑터는 enabled 상태에서 다음을 강제한다.

- API key 필수
- 첫 검색 전 `GET /auth/verify` 성공 필요
- loopback이 아닌 서버는 HTTPS 필수
- URL 안에 credential 삽입 금지
- release = `v1.5.7` 고정
- image digest = SHA-256 고정
- binding registry는 `runtime/lightrag/` 아래에만 저장

LightRAG 서버 측에서도 API key/account auth를 활성화하고,
공개 whitelist가 query/LLM 경로를 우회하지 않도록 별도로 검토해야 한다.

## production 검색과 benchmark 검색 분리

### production `search()`

다음 조건을 모두 만족하는 chunk만 content를 반환한다.

1. chunk_id가 local binding registry에 존재
2. source_id가 현재 사용자 allowed source 집합에 포함
3. revision_id가 현재 source revision과 동일

unmapped / unauthorized / stale chunk의 본문은 결과 객체에 포함하지 않는다.

### diagnostic `benchmark_search()`

provider leakage와 provenance coverage를 측정하기 위해 unmapped hit도 기록한다.
단 unmapped hit의 metadata는 빈 객체이며 canonical provenance를 만들어내지 않는다.

이 경로의 결과는 사용자 답변이나 Power CAD context로 직접 사용하지 않는다.

## RAGFlow와 비교 방법

두 provider 모두 기존 `context_fabric.benchmark`의 동일 gate를 사용한다.

Hard gates:

- provenance metadata coverage = 100%
- unauthorized source leakage = 0
- stale revision leakage = 0

Quality gates:

- Recall@5 >= 0.80
- MRR >= 0.60

latency / indexing time / storage size는 초기에는 관찰 지표다.

## 아직 하지 않은 것

- 실제 LightRAG v1.5.7 서버 기동
- 실제 API key 검증
- 실제 document upload/index completion
- remote chunk_id ↔ projection binding 자동 수집
- 사용자의 실제 Drive corpus에서 RAGFlow와 동시 benchmark
- production enable

이 문서와 어댑터는 **비교 준비**이며 LightRAG 채택 결론이 아니다.

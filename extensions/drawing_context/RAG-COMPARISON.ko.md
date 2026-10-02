# RAG provider comparison runner

RAGFlow와 LightRAG를 같은 질문·같은 projection·같은 ACL/revision 조건으로
비교하기 위한 provider-neutral 보고 계층이다.

## 원칙

1. provider마다 **동일한 provider-neutral benchmark fixture digest**를 사용해야 한다.
   기존 fixture의 `provider` 라벨만 digest에서 제외하며 projection/cases 등 다른 필드는 모두 동일해야 한다.
2. 각 provider run은 재현 가능한 실행 profile을 반드시 기록한다.
3. 기존 promotion gate를 PASS하지 못한 provider는 성능이 좋아도 후보가 아니다.
4. 가중치 점수를 만들지 않는다.
5. PASS provider만 다음 순서로 비교한다.
   - Recall@K
   - MRR
   - 모든 동률 provider에 측정값이 있을 때만 p95 latency
6. 위 지표가 같고 latency가 없으면 임의 winner를 만들지 않고 `TIE`로 남긴다.
7. 결과는 이 fixture와 기록된 provider profile에만 유효하며 provider를 canonical로 승격하지 않는다.

## provider 실행 profile

최소 다음 값을 기록해야 한다.

- `provider_version`
- `retrieval_mode`
- `embedding_model`
- `embedding_revision`
- `index_revision`

profile 전체는 별도의 SHA-256 digest로 묶인다. run 파일의 profile 내용을 나중에 바꾸면
digest 검증에서 거부된다.

RAGFlow와 LightRAG의 profile 값은 같을 필요가 없다. 서로 다른 검색 시스템을 비교하는 것이 목적이므로
각자의 실제 설정을 정확히 고정하고 기록하는 것이 중요하다.

## 실행 형식

provider benchmark 결과는 다음 wrapper로 저장한다.

```json
{
  "schema": "drawing-context-rag-provider-run/1",
  "provider": "ragflow",
  "fixture_digest": "<sha256>",
  "profile": {
    "provider_version": "v0.27.2",
    "retrieval_mode": "hybrid",
    "embedding_model": "BAAI/bge-m3",
    "embedding_revision": "<model-revision>",
    "index_revision": "<index-revision>"
  },
  "profile_digest": "<sha256>",
  "metrics": {"schema": "drawing-context-rag-benchmark-result/1"},
  "canonical_mutation": false
}
```

두 개 이상의 결과를 비교:

```bash
python scripts/compare_rag_results.py \
  runtime/bench/ragflow.json \
  runtime/bench/lightrag.json \
  --out runtime/bench/comparison.json
```

`NO_ELIGIBLE`이면 exit code 2이며 production provider 선택 근거로 사용하면 안 된다.

## 주의

이 도구는 live RAGFlow/LightRAG를 실행하지 않는다.
각 provider의 live runner가 생성한 metrics를 동일 fixture로 묶어 비교할 때 사용한다.
권한 누출, stale revision, provenance 누락은 quality/latency보다 먼저 차단된다.

fixture가 같아도 provider version, retrieval mode, embedding/index revision이 기록되지 않았다면
재현 가능한 비교로 간주하지 않는다.

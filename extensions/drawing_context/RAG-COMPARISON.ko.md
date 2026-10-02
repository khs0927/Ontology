# RAG provider comparison runner

RAGFlow와 LightRAG를 같은 질문·같은 projection·같은 ACL/revision 조건으로
비교하기 위한 provider-neutral 보고 계층이다.

## 원칙

1. provider마다 **정확히 같은 benchmark fixture digest**를 사용해야 한다.
2. 기존 promotion gate를 PASS하지 못한 provider는 성능이 좋아도 후보가 아니다.
3. 가중치 점수를 만들지 않는다.
4. PASS provider만 다음 순서로 비교한다.
   - Recall@K
   - MRR
   - 두 provider 모두 측정값이 있을 때만 p95 latency
5. 위 지표가 같고 latency가 없으면 임의 winner를 만들지 않고 `TIE`로 남긴다.
6. 결과는 이 fixture에만 유효하며 provider를 canonical로 승격하지 않는다.

## 실행 형식

provider benchmark 결과는 다음 wrapper로 저장한다.

```json
{
  "schema": "drawing-context-rag-provider-run/1",
  "provider": "ragflow",
  "fixture_digest": "<sha256>",
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

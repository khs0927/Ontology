# RAG remote inventory verification

검토일: 2026-10-03 (KST)

목적은 RAGFlow/LightRAG benchmark가 **실제로 동일한 원격 corpus를 색인했는지**
read-only로 검증하는 것이다. CAIR, 원본, provider index를 수정하지 않는다.

## RAGFlow

검토한 HTTP API는 다음 read-only 경로를 제공한다.

- `GET /api/v1/datasets/{dataset_id}/documents?page=...&page_size=...`
- `GET /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks?page=...&page_size=...`

`verify_ragflow_remote_inventory()`는 isolated benchmark dataset 전체를 페이지별로
읽고 local binding registry와 다음을 모두 비교한다.

- document ID 집합
- chunk ID 집합
- fixture projection external ID 집합
- record count

누락/추가/중복 document 또는 chunk가 하나라도 있으면 `BLOCKED`다.

완전히 같을 때만 index snapshot을 다음 상태로 승격한다.

```json
{
  "assurance": "remote-readback-complete",
  "remote_inventory_verified": true
}
```

모든 네트워크 요청은 GET이며 canonical mutation은 없다.

## LightRAG v1.5.7

공개 API의 `GET /documents`는 document ID/status와 `chunks_count`를 제공한다.
그러나 현재 공개 응답 계약에는 전체 chunk-ID 목록이 없다.

따라서 `inspect_lightrag_remote_inventory()`는 다음만 기록한다.

- remote document count / digest
- reported total chunk count
- local binding chunk count

결과는 항상:

```json
{
  "status": "PARTIAL",
  "remote_inventory_verified": false,
  "production_adoption_eligible": false
}
```

이다.

chunk ID를 추정하거나 query 결과에 나타난 일부 chunk만으로 전체 index를 검증했다고
간주하지 않는다.

## production 승격 규칙

RAG provider comparison에서 production evidence 후보가 되려면:

1. 모든 비교 provider의 snapshot이 `remote-readback-complete`
2. 모든 provider가 provenance / ACL / stale revision hard gate PASS
3. 동일 fixture / k / ordered cases
4. 동일 projection corpus identity
5. 비교 결과가 `SELECTED` (TIE 아님)

을 모두 만족해야 한다.

현재 LightRAG 공개 API 한계 때문에 RAGFlow vs LightRAG 비교는 여전히 production
승격 전 단계로 남는다. 이 제한은 의도적인 fail-closed 정책이다.

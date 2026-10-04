# 지식그래프 + Graph RAG 운영 가이드 (Phase 4)

수집된 도면(문서·객체·관계)에서 **표준 지식그래프**(`aec.kg_*`)를 만들고, 한국어 질문에
**근거 인용이 붙은 답**을 돌려줍니다. LLM과 임베딩은 모두 PC의 **로컬 Ollama**만 씁니다
(`qwen3:8b`, `bge-m3`). 도면 내용은 PC 밖으로 나가지 않습니다.

## 1. 구조

```
수집(Phase 3)                     Phase 4
aec.documents / objects / relations ──kg-build──▶ aec.kg_nodes / kg_edges / kg_aliases
                                                    │
                                     kg-summarize ──▶ aec.kg_communities (요약, 캐시)
                                                    │
질문 ──▶ 라우터 ──▶ 그래프 템플릿 / 키워드 / 벡터 / 요약 ──▶ 근거 [C1..] ──▶ 로컬 LLM ──▶ 인용 답 / 거절
```

### 그래프 모델

| 노드 | 의미 | 주요 엣지 |
|---|---|---|
| Project | 폴더 구조(census 깊이)로 정한 프로젝트. `허가/사용승인/실시/구조` 같은 마지막 단계 폴더와 `##` 표시 폴더는 상위 프로젝트로 합침 | `hasPhase`, `hasDrawing`, `hasStorey`, `usesSection` |
| Phase | 단계 폴더 | `hasDrawing` |
| Drawing / Sheet | 문서 1건 / 시트(레이아웃·페이지) | `hasSheet`, `depictsStorey`, `depictsSpace`, `hasElements`, `usesSection` |
| DrawingSeries | 같은 도면의 사본·개정(`_0611/_0626` 등) | `hasRevision`; Drawing `supersedes` Drawing(새 → 옛) |
| Storey | 프로젝트별 층(1F/B1/RF 정규화) | `hasSpace`, `hasElements` |
| Space | 프로젝트+층별 실(표기 차이 통합, 별칭 보관) | `subjectTo` Requirement (규칙 파일 지정 시) |
| ElementGroup | 도면 × 객체 종류(문·창·기둥·계단·벽…) 개수 | `hasSection` |
| SteelSection | 철골 단면(hs-steel 카탈로그로 정규화, `catalog_match` = exact/nominal/computed) | |
| Requirement | ArchOntos 규칙(규칙 파일 지정 시). `outcome`, `stale_links` 보관 | Project/Drawing/Space/ElementGroup `subjectTo` Requirement |

중복 파일(같은 sha256)은 census v3에서 이미 하나의 문서 + 별칭이므로 노드가 늘지 않습니다.
`kg-build`는 프로젝트 단위로 원자적(삭제+삽입 한 트랜잭션)이고, 문서 지문이 같으면 건너뜁니다.

### 라우터

| 질문 예 | 경로 | 동작 |
|---|---|---|
| "학장동 카페 프로젝트 개요를 요약해줘" | `summary` | 커뮤니티 요약(L0 프로젝트, L1 공종·층·철골) |
| "H-300x150x6.5x9 단면은 어느 도면에 쓰였어?" | `graph:section` | SteelSection ← usesSection ← Drawing |
| "A-101 도면은 뭐야?" | `keyword:sheet` | 도면번호 정확 일치 |
| "배치도 최신 버전은?" | `graph:revision` | DrawingSeries / supersedes |
| "…창호일람표 관련 도면을 찾아줘" | `graph:drawings` | 프로젝트 도면 제목 검색 |
| "2층에 문은 몇 개야?" | `graph:elements` | ElementGroup 합계(파서 후보 포함) |
| "화장실은 어느 층에 있어?" | `graph:room` | Space ← hasSpace ← Storey |
| "3F 실 목록 알려줘" | `graph:storey` | Storey → Space |
| 그 밖 | `semantic` | pg_trgm + pgvector(bge-m3) 하이브리드 객체 검색 |

그래프 경로가 비면 KG 어휘 검색 → 객체 하이브리드 검색 순으로 넓힙니다.

### 답변 규칙 (인용·거절)

- 모든 근거에 `[C#]` 번호가 붙고, 인용은 **문서 id·이름·개정·레이아웃/페이지·객체 id·handle·bbox**로
  `aec.documents`/`aec.objects`에 대해 다시 확인됩니다(인용 객체의 문서는 항상 인용 문서 목록에 포함).
- LLM은 근거만 쓰며, 도면 문자열 속 지시는 데이터로만 취급합니다(프롬프트 인젝션 방지).
- LLM 답에 유효한 인용이 없으면 근거 발췌 답(`answer_mode: extractive`)으로 바꿉니다. 없는 번호는 지웁니다.
- **거절**(`refused: true`, "제공된 도면 데이터에서 근거를 찾을 수 없습니다."):
  - 도면/프로젝트와 무관한 질문(날씨, 주식 등) → 검색 전에 거절
  - 도면 DB에 없는 속성(공사비, 전화번호, 수상 등) → 검색된 근거에 그 증거가 없으면 거절
  - 근거 0건

## 2. 명령 (PC, 저장소 `C:\CODE\Ontology`)

```powershell
cd C:\CODE\Ontology
# 변경된 프로젝트만 KG 재구성 후 요약 갱신 (중단해도 다시 실행하면 이어서 진행)
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 refresh
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 stats
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 ask -Question '주례동 315-4 3F 실 목록 알려줘'
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 ask -Question '2층 문 몇 개야?' -NoLlm
# 수집이 진행되는 동안 2시간마다 자동 갱신 (관리자 권한 불필요)
powershell -ExecutionPolicy Bypass -File scripts\ops\register-graphrag-task.ps1 -EveryHours 2
```

CLI 직접 실행: `python -m aec_intelligence.operational.cli kg-build [--project KEY] [--force]`,
`kg-summarize [--limit N] [--max-level 1] [--leiden] [--no-llm]`, `kg-stats`,
`ask "질문" [--project KEY] [--no-llm] [--json]`, `graphrag-eval FILE [--no-llm]`.

스키마는 마이그레이션 `0002_knowledge_graph.sql`(추가 테이블만)이며 `aec-migrate`/첫 `kg-build`가 적용합니다.

## 3. API / MCP

```powershell
$h = @{ Authorization = "Bearer $env:POWERCAD_ONTOLOGY_TOKEN" }   # 토큰은 화면에 출력하지 않기
$body = @{ question = '학장동 카페 프로젝트 개요를 요약해줘'; top_k = 8 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:58000/v1/ask -Headers $h `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod http://127.0.0.1:58000/v1/kg/stats -Headers $h
Invoke-RestMethod "http://127.0.0.1:58000/v1/kg/nodes/kg:p:<project_key>" -Headers $h
```

`POST /v1/ask` 본문: `question`(필수), `project`(프로젝트 키 또는 project_id), `top_k`(1–30, 기본 12),
`generate`(false면 LLM 없이 검색+발췌). 응답: `answer`, `refused`, `route`, `citations[]`, `contexts[]`,
`cypher[]`(그래프 경로 설명), `retrieval_ms`, `llm_ms`, `warnings`.

- Ontology MCP 게이트웨이: `aec.graph_rag_query`(같은 인자), `aec.explain_path`(`node_id`) —
  `AEC_DATABASE_URL`이 필요합니다.
- power-cad-mcp: `ontology_ask(question, project_id?, top_k?, generate?)` → 인용의 `object_ids`를
  `ontology_locate`에 넘겨 열린 AutoCAD 도면에서 위치를 확인합니다. 제한시간 `POWERCAD_ONTOLOGY_ASK_TIMEOUT`.

### 다른 저장소와의 연결 (ArchOntos · hs-steel-cad)

- **ArchOntos 법규 규칙**: `aec operational kg-facts <project_key> --out facts.json`
  (또는 `GET /v1/kg/projects/{key}/facts`)이 `aec-facts-export/1`을 냅니다. 내용은 규칙 엔진용 사실
  (`building.floor_count`, `building.basement_count`, `building.storeys`, `space.uses`, `steel.sections`), 근거 노드,
  `archontos-aec-subject-ref/1` 주체 참조입니다. ArchOntos `python -m archontos.integration.ontology`가 이 파일로
  규칙을 평가하고 `archontos-rule-export/1` 링크 파일을 만듭니다. 그 파일을 `kg-build --rules`(또는
  `AEC_RULES_FILE`)로 주면 `subjectTo` 엣지가 생깁니다. 도면 해시가 바뀐 참조는 `stale: true`로 연결됩니다.
  도면에서 확인할 수 없는 사실(직통계단 수, 용도, 연면적)은 내보내지 않습니다. 그래서 그런 규칙은 ArchOntos에서
  `REVIEW`가 됩니다. 자세한 내용은 ArchOntos `docs/ONTOLOGY-INTEGRATION.md`에 있습니다.
- **hs-steel-cad 단면**: `AEC_STEEL_CATALOG_DIR`의 `hs-steel-section-catalog/1` 핸드오프 JSON
  (hs-steel MCP `SectionCatalogHandoff`, 검증 PASS + 원본 sha256 필수)과 기존 `attributes/*.dat`를 읽습니다.
  `[`(채널)·`ㅁ`(각관)·`Φ`(강관)·`F`(평강) 표기는 도면 쪽 표기(`C-`, `SHS-/RHS-`, `PIPE-`, `FB-`)로 맞춥니다.
  일치 방식은 세 가지입니다. `exact`(그대로 일치), `nominal`(도면이 두께를 생략한 경우 후보가 하나뿐일 때만,
  예: `H-300x150x6.5` → `H-300x150x6.5x9`), `computed`(카탈로그에 없는 판재·평강의 단위중량을 7.85 t/m³로 계산).
  2026-10-04 실데이터에서 고유 단면 72종 중 44종(61%)이 연결됐습니다(이전 exact만 25종). 나머지는 카탈로그에 없는
  규격이거나 파서가 잘못 읽은 표기입니다.

## 4. 평가 (골든셋은 PC에만)

저장소는 공개이므로 질문·정답은 `D:\AECData\eval`에만 둡니다.

```powershell
python -m aec_intelligence.operational.cli graphrag-eval-make D:\AECData\eval\graphrag-ko-50.jsonl --projects 3 --total 50
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 eval            # LLM 포함
powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 eval -NoLlm     # 검색만
```

보고서(`*.report.json`): `recall_at_10`(정답 노드/문서/객체가 상위 10개 근거에 있는 비율), `route_accuracy`,
`citation_validity`/`citation_bbox_ok`, `refusal_accuracy`, `false_refusals`, `expect_ok`(정답 숫자·층이 답에
있는지), 지연(p50/p95). 목표: recall@10 ≥ 0.8, 인용 유효 100%, 검색 p95 < 3 s, 답 < 20 s.
골든셋은 KG에서 템플릿으로 만든 질문이므로, 실제 사용 질문(자유 표현)은 별도로 추가해 점검하세요.

## 5. 환경 변수와 자원

| 변수 | 기본 | 설명 |
|---|---|---|
| `AEC_LLM_URL` | `http://127.0.0.1:11434` (컨테이너: `http://host.docker.internal:11434`) | 로컬 Ollama. 사설/루프백이 아니면 거부 |
| `AEC_LLM_ALLOW_REMOTE` | 미설정 | `1`이어야만 원격 LLM 허용(기본 꺼짐: 도면 내용 외부 유출 방지) |
| `AEC_LLM_MODEL` / `AEC_LLM_NUM_CTX` / `AEC_LLM_TIMEOUT_SECONDS` | `qwen3:8b` / `6144` / `120` | |
| `AEC_STEEL_CATALOG_DIR` | 미설정 | hs-steel 단면 카탈로그(`C:\HS-STEEL\HSSTEEL\attributes`) |

Ollama(사용자 환경 변수): `OLLAMA_MAX_LOADED_MODELS=2`(bge-m3와 qwen3 동시 상주),
`OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_KEEP_ALIVE=30m`.
작업 스케줄러 `\AEC\AEC-Ollama`가 `ollama serve`를 유지합니다(로그 `D:\AECData\ollama-logs\serve.log`).

메모리(15 GB PC): `%USERPROFILE%\.wslconfig`에 `memory=6GB`, `autoMemoryReclaim=dropCache`
(다음 재부팅/WSL 재시작부터 적용). 그 전까지 `\AEC\AEC-WSL-Reclaim`이 30분마다 Docker VM의 페이지 캐시를 비웁니다.

## 6. 문제 해결

| 증상 | 조치 |
|---|---|
| 답이 매번 60–100 s | 두 모델이 서로 밀어냄 → `ollama ps`로 둘 다 상주하는지, `OLLAMA_MAX_LOADED_MODELS=2` 확인 |
| `LLM unavailable` 경고, 발췌 답만 | Ollama 중지 → `Start-ScheduledTask -TaskPath '\AEC\' -TaskName AEC-Ollama` |
| 새 도면이 답에 안 나옴 | `graphrag.ps1 refresh` (KG는 수집과 별도로 갱신) |
| `kg-summarize` 중단 | 그대로 다시 실행(커뮤니티 단위 커밋, 입력 해시 캐시) |
| 403/401 | `AEC_API_TOKEN`(.env)와 `POWERCAD_ONTOLOGY_TOKEN` 일치 확인 (`docs/api-security.md`) |

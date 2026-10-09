# ArchiOffice asset pack

로컬 ZWCAD/ArchiOffice(아키오피스) 환경에서 **읽기 전용**으로 추출한 설계 자산을
이 저장소의 지식그래프(`aec.kg_*`)와 벡터 저장소(`aec.text_vectors`, bge-m3 1024차원)로
적재하기 위한 자립형 팩입니다. 원본 트리는 수정하지 않습니다.

| | |
|---|---|
| 원본 | `%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\ArchiOfficeZW` |
| 원본 규모 | 1,164 파일 / 60,827,938 bytes / 44 디렉터리 |
| project_key | `ARCHIOFFICE` |
| 임베딩 | 로컬 Ollama `bge-m3`, 1024차원, `halfvec` |

## 구성

```
library/archioffice/
  manifest.json                  생성 시각, 원본 경로, 모든 카운트, 노드/엣지 어휘
  assets/                        원본에서 뽑아낸 도메인 레코드
    archioffice_files.jsonl             파일 1건당 1레코드 (1,164)
    archioffice_patterns.jsonl         해치 패턴 메타데이터 (333)
    archioffice_pattern_geometry.jsonl.gz  패턴 정의선 원문 (333, gzip)
    archioffice_linetypes.jsonl        선종류 정의 (177)
    archioffice_layers.jsonl           도면층 표준 + 도면층 역할 매핑 (53)
    archioffice_symbol_configs.jsonl   심볼 배치 설정 .CFG 원문 구조 (20)
    archioffice_block_specs.jsonl      블록명 → 사양 (30)
    archioffice_textstyles.jsonl       문자 스타일/높이 (10)
    archioffice_commands.jsonl         명령 단축키·외부명령·LISP 함수 (1,437)
    archioffice_settings.jsonl         기타 환경 설정/목록 (10)
    extract_diagnostics.json           파싱 실패, 중복 그룹
  graph/
    kg_nodes.jsonl                 aec.kg_nodes 로더용
    kg_edges.jsonl                 aec.kg_edges 로더용
    kg_aliases.jsonl               aec.kg_aliases 로더용
    graph_edges_flat.jsonl         subject/predicate/object 평면 표현
    graph.jsonld                   JSON-LD 상호교환
  vectors/
    vector_corpus.jsonl            {node_id, kind, text, content_hash, model, dim} — 임베딩 대상
  sql/
    archioffice_kg_load.sql        멱등 적재(자기 project_key만 삭제 후 재삽입)
  tools/
    build_archioffice_pack.py      원본 → assets/graph/vectors/sql 재생성
    verify_pack.py                 구조 검증 (FK 정합성, 해시, 인코딩, 카운트)
    query_pack.py                  DB 없이 키워드 + 1-hop 그래프 확장 질의
    load_archioffice.py            aec.* 적재 (기본 드라이런, --apply 필요)
  docs/
    ARCHIOFFICE_ASSET.ko.md        수집 범위·누락·활용법 (한글)
```

## 카운트 (생성 시점)

- 노드 2,901 / 엣지 4,139 / 별칭 45 / 벡터 2,860
- 노드 유형: `CommandAlias` 1397, `LibrarySymbol` 765, `HatchPattern` 333, `Linetype` 177,
  `LayerStandard` 56, `SymbolSetFile` 45, `LispFunction` 31, `BlockSpec` 30,
  `MaterialClass` 12, `LibraryCategory` 11, `ConfigSetting` 10, `TextStyle` 10,
  `ExternalCommand` 9, `LayerRole` 5, `PlantHabit` 5, `ViewType` 3, `Library` 1, `ConfigFile` 1
- 술어: `hasCommandAlias` 1397, `hasSymbol` 765, `inView` 741, `hasPattern` 333,
  `hasMaterialClass` 333, `hasLinetype` 177, `hasHabit` 174, `hasLayer` 48, `hasSetFile` 45,
  `hasLispFunction` 31, `hasBlockSpec` 30, `placedOnLayer` 18, `hasCategory` 11,
  `hasSetting` 10, `hasTextStyle` 10, `hasExternalCommand` 9, `hasLayerRole` 5,
  `hasFile` 1, `duplicateOf` 1

## 재생성 / 검증 / 조회

```powershell
cd C:\CODE\Ontology\library\archioffice
python tools\build_archioffice_pack.py      # 원본 재스캔, 팩 전체 재생성
python tools\verify_pack.py                 # 0 findings 이면 정상 (exit 0)
python tools\query_pack.py "엘리베이터 14인승" --top 3
python tools\query_pack.py "콘크리트 기둥 도면층" --expand 2
```

## 적재

```powershell
# 1) 드라이런: 무엇이 들어가는지만 확인 (DB 접속도 하지 않음)
python tools\load_archioffice.py

# 2) 실제 적재 (그래프만)
docker exec -i aec-db psql -U aec -d aec -v ON_ERROR_STOP=1 -f - < sql\archioffice_kg_load.sql

# 3) 실제 적재 (그래프 + 객체 + 벡터). Ollama bge-m3 가 떠 있어야 한다.
$env:AEC_DSN = "postgresql://<user>@127.0.0.1:55432/aec"
python tools\load_archioffice.py --apply
```

적재는 자기 `project_key` 행만 지우고 다시 넣으므로 반복 실행해도 안전합니다.
되돌리려면:

```sql
DELETE FROM aec.kg_edges  WHERE project_key = 'ARCHIOFFICE';
DELETE FROM aec.kg_aliases WHERE node_id IN (SELECT id FROM aec.kg_nodes WHERE project_key = 'ARCHIOFFICE');
DELETE FROM aec.kg_nodes  WHERE project_key = 'ARCHIOFFICE';
DELETE FROM aec.kg_build_state WHERE project_key = 'ARCHIOFFICE';
DELETE FROM aec.embeddings WHERE object_id IN (SELECT id FROM aec.objects WHERE project_id = 'ARCHIOFFICE');
DELETE FROM aec.objects    WHERE project_id = 'ARCHIOFFICE';
DELETE FROM aec.documents  WHERE project_id = 'ARCHIOFFICE';
```

## 경계 (설계상 명시)

- 원본 트리는 읽기만 했고 어떤 파일도 수정·삭제하지 않습니다.
- 이 팩은 커밋/푸시하지 않습니다. 저장소 작업 트리에 새 디렉터리로만 존재합니다.
- `.dwg` 765개는 **파일 메타데이터(이름·경로·크기·해시·공칭치수)** 만 담겼습니다.
  도형 내부(엔티티, 블록 정의, 레이어 배치)는 담기지 않았습니다. 이는 ODA File Converter
  (`C:\Program Files\ODA\ODAFileConverter 27.1.0`)로 DWG→DXF 변환 후 저장소의
  `aec_intelligence.dwg` / `dxf` 어댑터로 확장할 수 있는 별도 단계입니다.
- 텍스트 인코딩은 UTF-8을 먼저 시도하고 실패하면 cp949로 읽습니다. 한국 CAD 설정 파일이
  cp949인 경우가 있어 이 순서가 필요합니다.
- `.CFG` 의 `[XP:DESCRIPTION]` 에 있는 사람이 읽는 사양(예: `EH10N14X125-8X21=14인승 1400(W) x 1250(D)`)은
  `BlockSpec` 노드로, `_XPLayerSet.ini` 의 도면층 정의는 `LayerStandard` 노드로 승격했습니다.
- `material_class` 는 이름+설명에 대한 정규식 분류이며 333개 중 218개는 근거가 없어
  `unclassified` 로 남겼습니다. 추측으로 채우지 않았습니다.
- `query_pack.py` 는 어휘 기반 스모크 체크입니다. 실제 검색 품질은 적재 후
  bge-m3 + pgvector 경로에서 나옵니다.

## 적재 이력

| 시각 (KST) | 대상 | 결과 |
|---|---|---|
| 2026-10-09 | `aec.kg_nodes` / `kg_edges` / `kg_aliases` / `kg_build_state` | project_key `ARCHIOFFICE` 2,901 / 4,139 / 45 / 1 커밋 |
| 2026-10-09 | `aec.documents` / `aec.objects` / `aec.embeddings` / `aec.text_vectors` | 문서 12 / 객체 2,860 / 매핑 2,860 / 임베딩 2,860 (bge-m3, halfvec 1024) |

적재 후 실측:

- `pgvector` 코사인 검색 (실제 bge-m3 질의 임베딩)
  - "엘리베이터 14인승 치수" → `block_spec EH24H15X23-12X21` (cos 0.638), `EH10N14X125-8X21` (0.627)
  - "벽돌 조적 해치" → `hatch_pattern brick1` (0.580), `ibrickl` (0.578), `vbricks` (0.572)
  - "콘크리트 기둥 도면층" → `layer_standard AA-CLXM-CONC` (0.666) 1위
- 저장소 CLI GraphRAG 경로: `ask "엘리베이터 14인승 치수는?" --no-llm`
  → `route: semantic`, 인용 `ARCHIOFFICE-LIBRARY-SYMBOL-DOC` / object id 포함, `refused: false`
- 같은 실행에서 `semantic` 단계가 `budget_ms: 8000` 에 걸려 `timed_out: true` 인 질의가 있었습니다.
  이 팩 자체의 오류는 아니며(검색 예산 초과), 반복되면 `AEC_QUERY_*` 예산을 조정할 대상입니다.

적재 후 주의: `aec.objects` 에 `project_id='ARCHIOFFICE'` 객체 2,860건이 추가되었으므로,
프로젝트 필터 없이 도는 `semantic` 경로는 도면 질의에서도 라이브러리 심볼을 후보로 올릴 수 있습니다.
`kind`(`library_symbol`, `hatch_pattern`, …) 와 `project_id` 로 구분해 필터링하십시오.

되돌리기 SQL은 위 "적재" 절에 있습니다. 팩 파일 자체는 커밋하지 않았습니다.

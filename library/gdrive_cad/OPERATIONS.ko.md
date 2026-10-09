# gdrive_cad 운영 매뉴얼 (`OPERATIONS.ko.md`)

프로젝트 키 `GDRIVE_CAD` / 팩 이름 `gdrive_cad`. 구글 드라이브의 CAD 자산 전체를
`ask --project GDRIVE_CAD` 로 검색할 수 있게 만든 GraphRAG 자산 팩이다.
이 문서는 "무엇을 담고 있는가 → 어떻게 만들었는가 → 어떻게 다시 돌리는가 → 어디서 깨지는가"를
운영자 관점에서 정리한다.

---

## 1. 이 팩에 담긴 것 (개수만, `manifest.json` 기준)

| 항목 | 값 |
|---|---|
| source_files (드라이브 원본 파일 수) | 13,776 |
| records_parsed (심층 파싱된 DWG/DXF) | 788 |
| drawings (고유 도면 노드) | 11,440 |
| sub_projects (하위 프로젝트 노드) | 282 |
| duplicate_copies (중복 사본) | 2,336 |
| kg_nodes | 28,272 |
| kg_edges | 92,953 |
| kg_aliases | 4 |
| vector_rows | 28,271 |
| layer_standards (상한 4,000) | 4,000 |
| block_specs (상한 3,000) | 3,000 |
| layers_seen / blocks_seen | 26,249 / 8,776 |
| documents (PDF/xls/xlsx 메타데이터) | 9,549 |
| document_links (Document→Drawing) | 543 |
| document_duplicates | 3,354 |
| review_queue (공종 신뢰도 < 0.7) | 1,510 |

- 문서 형식: `pdf` 7,925 / `xlsx` 701 / `xls` 923.
- 파싱 상태: `metadata_only` 10,573 / `parsed` 788 / `unparsed_format` 79.
- 공종(discipline) 11종: ARCH 4,345 · STRUCT 2,178 · GENERAL 1,458 · MECH 760 · CIVIL 709 ·
  ELEC 653 · FIRE 539 · COMM 314 · LAND 274 · PLUMB 149 · INTERIOR 61.
- 노드 타입: `Project` · `SubProject` · `Drawing` · `Document` · `LayerStandard` · `BlockSpec`.
- 엣지 술어: `hasSubProject` · `hasDrawing` · `hasDocument` · `linkedTo` · `usedIn`.
- 임베딩: 모델 `bge-m3`, 차원 1024, 저장 위치 `aec.text_vectors`(halfvec).
- 팩 fingerprint: `gdrive-cad-pack-v1` (문서 없는 팩의 stale 삭제 방지용으로 `-pack-` 포함).

> 생성 데이터(`graph/ vectors/ sql/ assets/`, 약 60MB)에는 드라이브의 실제 폴더/파일명이 들어 있어
> **git에 올리지 않는다**(`.gitignore`). 커밋되는 것은 `tools/`, 테스트, `manifest.json`(개수만)뿐이다.

---

## 2. 전체 파이프라인

```
inventory  →  select  →  rclone/G: copy  →  ODA (DWG→DXF)  →  extract_one
           →  build  →  verify  →  load --apply  →  kg-summarize  →  ask
```

1. **inventory** — `inv_raw.json`(rclone `lsjson` 결과)을 정제해 `inventory.json` 생성.
   노이즈 경로 제외: `c_code`, `.sisyphus`, `.tmp.driveupload`, `eval_v2_out`, `node_modules`,
   `20261009-` 접두, `.CODE/` 하위. CAD 확장자와 관련 문서(pdf/xls/xlsx)만 남긴다.
2. **select** — CAD 행 전체를 `cad_all.json`으로, 그중 DWG를 프로젝트/공종 기준으로 층화 표본 추출해
   `select.json` + `files_from.txt` 로 저장. `AEC-INTELLIGENCE/`, `revit-mcp-guideline/`(저장소 사본)은 제외.
3. **copy** — rclone로 표본 파일을 `C:\CODE\_data\gdrive_cad\files\<드라이브 상대경로>` 로 내려받는다.
   (G: 미러/캐시 사용)
4. **ODA DWG→DXF** — ODA File Converter 27.1.0, `ACAD2018` / `DXF` 출력, 배치 폴더 단위 변환.
5. **extract_one** — 변환된 DXF를 `DXFParser` 로 파싱해 레코드(레이어·블록·텍스트·rooms·sheet·counts)를
   JSONL로 적재. `run_extract.py` 가 25개씩 배치 처리하고, DXF 변환은 ODA, 레코드 추출은
   `ThreadPoolExecutor(2)` 로 병렬.
6. **build** — `cad_all.json` + `records.jsonl` + `inventory.json` 을 입력으로 팩 전체
   (`graph/ vectors/ sql/ assets/ manifest.json`) 생성.
7. **verify** — 구조 검증(JSONL 파싱, id 정합성, 중복 노드, `content_hash==sha256(text)`, U+FFFD 없음,
   manifest 개수 일치, 이메일/전화번호 부재). exit 0 / findings NONE 이면 통과.
8. **load --apply** — aec 스키마 적재(기본은 dry-run). 객체 → KG 노드 연결 → 임베딩 → stale 정리.
9. **kg-summarize** — 커뮤니티 요약 생성.
10. **ask** — `ask --project GDRIVE_CAD` 로 검색 질의.

---

## 3. 정확한 실행 명령

### 0) 공통 환경 규칙 (필수)

- `PYTHONHOME` 을 **반드시 비운다**(안 비우면 인터프리터/패키지 오류).
- DB·Ollama 접속값은 **`C:\CODE\Ontology\.env`** 를 `KEY=VALUE` 로 파싱해 프로세스 환경변수로 넣는다.
  - ⚠️ **워크트리의 `.env` 는 잘못된 값이다. 쓰지 말 것.**
  - **값을 화면에 출력하지 않는다.**
- DSN 우선순위: `--dsn` → `AEC_DSN` → `AEC_DATABASE_URL` → `.env`.

### 1) 데이터 수집 (데이터 루트에서)

```powershell
$env:PYTHONHOME = $null
python C:\CODE\_data\gdrive_cad\tools\inv.py          # inv_raw.json → inventory.json
python C:\CODE\_data\gdrive_cad\tools\select.py       # inventory.json → cad_all.json / select.json / files_from.txt
# (rclone로 표본을 files\<상대경로> 로 복사)
python C:\CODE\_data\gdrive_cad\tools\run_extract.py  # select.json + cad_all.json(dxf) → records.jsonl
```

- `run_extract.py` 내부 상수: ODA 경로
  `C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe`,
  파이썬 `C:\CODE\Ontology\.venv\Scripts\python.exe`(`-I` 격리 실행).

### 2) 팩 재생성 (멱등)

```powershell
$env:PYTHONHOME = $null
python library\gdrive_cad\tools\build_gdrive_cad_pack.py --src C:\CODE\_data\gdrive_cad   # --force 로 강제 재생성
python library\gdrive_cad\tools\verify_pack.py                                            # exit 0, findings NONE
python library\gdrive_cad\tools\load_gdrive_cad.py                                        # 기본: dry-run(미적용)
```

### 3) DB 적재 (Docker / aec-db 기동 후)

```powershell
cd C:\CODE\Ontology
. .\scripts\ops\_common.ps1; Import-AecDotEnv
$env:AEC_EMBEDDING_URL='http://127.0.0.1:11434'; $env:AEC_LLM_URL='http://127.0.0.1:11434'

python C:\CODE\wt-gdrive-pack\library\gdrive_cad\tools\load_gdrive_cad.py --apply
Invoke-AecCli -Arguments @('kg-summarize','--project','GDRIVE_CAD')
Invoke-AecCli -Arguments @('ask','<질문>','--project','GDRIVE_CAD','--no-llm')
```

### 4) CLI 직접 호출 (검색만)

```powershell
$env:PYTHONHOME = $null
$env:PYTHONPATH = 'C:\CODE\wt-gdrive-pack\src'
python -m aec_intelligence.operational.cli ask "<질문>" --project GDRIVE_CAD --no-llm
```

---

## 4. 샘플(심층 파싱 대상) 추가 방법

`records.jsonl` 늘리는 것이 곧 커버리지 확대다. 빌더는 records가 늘면 그냥 다시 돌리면 된다.

**방법 A — 기존 인벤토리에서 추가 선택**
1. `select.json`(또는 `cad_all.json`)에 대상 행을 추가한다.
2. 새 파일이 아직 안 받아졌으면 rclone로 `files\<상대경로>` 에 복사한다.
3. `python run_extract.py` 재실행 → `records.jsonl` 에 증분 추가(이미 파싱된 `path`는 건너뜀).
4. `build → verify → load --apply → kg-summarize` 재실행.

**방법 B — 표본 로직으로 재선정**
- `select.py` 는 DWG를 프로젝트별로 공종 접두(도면번호 머리) 기준 층화 추출한다.
  cap 규칙: 프로젝트 파일 수 >150 → 10개, >40 → 6개, >8 → 3개, 그 외 2개(seed 고정 7).
- 표본 규모를 키우려면 이 cap 로직을 조정한 뒤 `select.py` 부터 다시 돌린다.

**주의**
- `run_extract.py` 는 `select.json` 의 DWG + `cad_all.json` 의 DXF(비중복)를 대상으로 한다.
- 300MB 초과 파일은 `select.py` 단계에서 제외(>500MB 스킵 집계 로직 참고).

---

## 5. 멱등 재실행 / 이어서 돌리기

- **build**: 입력(`cad_all.json`/`records.jsonl`/`inventory.json`) fingerprint가 같으면
  "UP TO DATE" 로 건너뛴다. 강제하려면 `--force`.
- **load**: `text_vectors` 는 이미 있으면 **재임베딩하지 않는다**. 임베딩 매핑은 upsert,
  stale 정리는 반드시 `project_id='GDRIVE_CAD'` 범위로만 수행(빈/무효 코퍼스면 거부).
- **중단 후 이어서**: `load_gdrive_cad.py --apply --skip-kg`
  (KG SQL 단계를 건너뛰고 임베딩만 재개, 배치 기본 32개마다 커밋).
- **extract**: `records.jsonl` 에 이미 있는 `path` 는 건너뛰므로 그냥 재실행하면 증분 진행된다.
- Ollama(`bge-m3`, `127.0.0.1:11434`)가 필요하다. `/api/embed` 배치 호출.

---

## 6. 알려진 한계

- **메타데이터 전용 도면**: `metadata_only` 10,573건 — 심층 파싱(DXF 변환)되지 않은 DWG는
  이름/크기/경로 등 메타데이터만 노드화된다.
- **미지원 형식**: `unparsed_format` 79건 — `rvt/skp/pln/dwt/dwf` 등은 파싱하지 않는다.
- **문서는 메타데이터만**: PDF/xls/xlsx(9,549건)는 파일명·형식·크기·소속만 보관하고
  **내용을 다운로드/파싱하지 않는다**. 그중 543건만 `Document -linkedTo-> Drawing` 으로 연결된다.
- **검토 대기열**: 공종 신뢰도 < 0.7 인 1,510건은 `assets/review_queue.jsonl` 로 빠져 수동 검토 대상이다.
- **live aec-api 경로 렌더링**: 서비스가 파일 경로를 렌더링하려면 **재배포가 필요**하다.
- **중복 사본**: 도면 2,336 / 문서 3,354건은 별도 노드가 아니라 `props.duplicate_copies` 로만 집계된다.
- 생성 데이터(약 60MB, 실제 폴더/파일명 포함)는 저장소에 없으므로 다른 PC에서는 반드시 로컬 재생성해야 한다.

---

## 7. 트러블슈팅

| 증상 | 원인 | 조치 |
|---|---|---|
| DB 접속 인증 실패 | 워크트리 `.env` 의 잘못된 값 사용 | **`C:\CODE\Ontology\.env`** 로 다시 로드(값 출력 금지) |
| `ask` 첫 호출이 타임아웃 | 콜드 스타트(모델/캐시 워밍) | 같은 질의를 **재실행**하면 정상 |
| 인터프리터/패키지 오류 | `PYTHONHOME` 이 설정됨 | `$env:PYTHONHOME = $null` 후 재실행 |
| 빌드가 계속 "UP TO DATE" | 입력 fingerprint 동일 | 정말 최신이면 정상. 강제하려면 `--force` |
| 메모리 부족(RAM) | 대용량 레코드/벡터 | `load` 는 배치(기본 32)마다 커밋 — 중단해도 `--skip-kg` 로 재개. ODA 변환은 파일 배치 단위·타임아웃 1500s |
| `verify_pack.py` exit ≠ 0 | count/manifest 불일치, id 미해결, U+FFFD, 이메일·전화번호 잔존 등 | 출력된 findings 를 확인하고 해당 단계 재생성 |
| ODA가 DXF를 못 만듦 | 손상/미지원 DWG | `run_extract.py` 가 `status=failed` 로 기록하고 계속 진행(해당 파일만 스킵) |

---

## 참고

- 세부 규칙(공종 판정·연결 로직 등): `C:\CODE\_herdr-bus\out\gdrive-cad-decisions.md`
- 팩 개요: `library\gdrive_cad\README.md`
- 관련 PR: https://github.com/khs0927/Ontology/pull/97

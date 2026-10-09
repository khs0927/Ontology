# gdrive_cad - Google Drive CAD GraphRAG pack (project key `GDRIVE_CAD`)

ArchiOffice pack (`library/archioffice`)와 같은 구조의 자산 팩. 구글 드라이브의 CAD 파일 전체를
`ask --project GDRIVE_CAD` 로 검색할 수 있게 한다.

생성 데이터(`graph/ vectors/ sql/ assets/`)는 약 60 MB이고 드라이브의 실제 폴더/파일명을 담고 있어
**git에 넣지 않는다**(`.gitignore`). 커밋되는 것은 도구, 테스트, `manifest.json`(개수만)이다.

## 재생성 (멱등, 입력이 같으면 "UP TO DATE"로 건너뜀)

```powershell
$env:PYTHONHOME = $null
python library\gdrive_cad\tools\build_gdrive_cad_pack.py --src C:\CODE\_data\gdrive_cad   # --force 로 강제 재생성
python library\gdrive_cad\tools\verify_pack.py                                           # exit 0, findings NONE
python library\gdrive_cad\tools\load_gdrive_cad.py                                       # dry-run (기본)
```

입력(`--src`): `cad_all.json`(전체 CAD 행), `records.jsonl`(DWG->DXF 심층 파싱, 계속 늘어남),
`inventory.json`(전체 인벤토리 — PDF/xls/xlsx 행이 `Document` 메타데이터 노드가 된다).
records가 늘면 그냥 다시 실행하면 된다.

## 규칙 요약 (`C:\CODE\_herdr-bus\out\gdrive-cad-decisions.md`)

- 고유 파일(size+md5)당 `Drawing` 노드 1개, 중복 사본은 `props.duplicate_copies`.
- 하위 프로젝트(`SubProject` 노드; GraphRAG는 `type='Project'`를 키당 1개로 가정하므로 타입을 분리) -hasDrawing-> Drawing.
- 공종: 도면번호 접두(0.85) > 폴더/제목 키워드(0.75) > 레이어 투표(<=0.8) > GENERAL(0.4). 0.7 미만은
  `assets/review_queue.jsonl`.
- 도면번호/제목: 타이틀블록(`source=title_block`)만 신뢰, 그 외는 드라이브 파일명(`filename_sheet_fields`).
- DWG/DXF 이외(rvt/skp/pln/dwt/dwf)는 `parse_status=unparsed_format`, 심층 파싱 안 된 DWG는 `metadata_only`.
- `LayerStandard`(<=4000) / `BlockSpec`(<=3000) 전역 노드, `usedIn` 엣지로 Drawing에 연결.
- `Document`(PDF/xls/xlsx): 고유 파일(size+md5)당 1개, 파일명 제목·형식·크기·`sub_project`·`drive_path`만 보관(다운로드/파싱 없음). 같은
  `sub_project` 폴더에서 파일명(stem)이 도면 stem/도면번호/제목과 일치하면 `Document -linkedTo-> Drawing`, Project/SubProject에는 `hasDocument` 엣지.
- 실/문자/레이어명은 `search_text`(<=1500자)에 포함, 이메일/전화번호는 제거.
- `kg_build_state.fingerprint = gdrive-cad-pack-v1` (`-pack-` 포함: 문서 없는 팩의 stale 삭제 방지).

## DB 적재 (Docker/aec-db 기동 후)

```powershell
python library\gdrive_cad\tools\load_gdrive_cad.py --apply          # DSN: --dsn / AEC_DSN / AEC_DATABASE_URL / .env
python library\gdrive_cad\tools\load_gdrive_cad.py --apply --skip-kg   # 중단 후 이어서 (임베딩은 배치마다 커밋)
cd C:\CODE\Ontology; . .\scripts\ops\_common.ps1; Import-AecDotEnv
$env:AEC_EMBEDDING_URL='http://127.0.0.1:11434'; $env:AEC_LLM_URL='http://127.0.0.1:11434'
Invoke-AecCli -Arguments @('kg-summarize','--project','GDRIVE_CAD')
Invoke-AecCli -Arguments @('ask','<질문>','--project','GDRIVE_CAD','--no-llm')
```

로더는 객체(`aec.objects`)를 넣은 뒤에 `kg_nodes.object_ids/document_ids`를 다시 연결하고(ArchiOffice PR #95),
임베딩 매핑은 upsert, stale 정리는 `project_id='GDRIVE_CAD'` 범위로만 수행한다. Ollama bge-m3
(`127.0.0.1:11434`)가 필요하며 `/api/embed` 배치 호출, 이미 있는 `text_vectors`는 다시 임베딩하지 않는다.

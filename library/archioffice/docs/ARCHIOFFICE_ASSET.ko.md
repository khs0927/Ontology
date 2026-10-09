# ArchiOffice 로컬 자산 정리 (수집 범위 · 승격된 지식 · 누락)

이 문서는 `library/archioffice/` 팩이 **무엇을 근거로, 무엇을 담았고 무엇을 담지 않았는지**를
기록합니다. 모든 수치는 원본 트리를 스캔해 측정한 값입니다.

---

## 1. 원본

```
%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\ArchiOfficeZW
```

| 항목 | 값 |
|---|---|
| 총 파일 | 1,164 |
| 총 용량 | 60,827,938 bytes |
| 디렉터리 | 44 |
| 확장자 | .dwg 765, .pat 332, .slb 24, .cfg 20, .txt 8, .ini 6, .pgp 3, .lsp 2, .lin 1, .bak 1, .dwt 1, .exe 1 |

이 폴더는 ArchiOffice(아키오피스)가 ZWCAD 2024 위에서 쓰는 사용자 데이터 폴더입니다.
심볼 라이브러리(`Library/`), 해치 패턴(`Hatch/`), ZWCAD 플랫폼 지원 파일(`2024/`),
그리고 최상위 환경 설정(`*.ini`, `*.txt`, `ArchiOffice.dwt`, `ArchiOffice.bak`)로 구성됩니다.

---

## 2. 라이브러리 구성 (원본 폴더 기준 측정)

| 카테고리 | 파일 | 하위 구성 |
|---|---|---|
| 창호 | 421 | 문(Door) 3D(D3) 45 / 입면(DE) 114 / 평면(DP) 59 · 창(Window) 3D(W3) 91 / 창입면(WE) 81 / 평면(WP) 31 |
| 조경 | 268 | 평면 89 · 평면(조경설계용) 낙엽교목 105 / 상록교목 43 / 낙엽관목 17 / 상록관목 10 / 초화류·지피 4 |
| 엘리베이터 | 45 | 현대 기계실 없는 11 · 병원용 4 · 중저속 기어리스 루젠 운구형 5 / 일반형 12 · 중저속 승객용 일반형 13 |
| 화장실 | 30 | 기타유닛 8 · 소변기 입면 3 / 평면 1 · 욕조 평면 18 |
| 기호 | 29 | 방향(DIRECTION-01 ~ 29) |
| 가구 | 15 | 의자 5 · 주방용가구 평면 10 |

부호 규칙은 폴더명에서 확인됩니다. 창호는 `(D3)=3D, (DE)=입면, (DP)=평면`,
`(W3)=3D, (WE)=창입면, (WP)=평면` 이고, 파일명 362건이 `900x2100` 같은 공칭 치수를 포함합니다.
파일명만으로 알 수 없는 것(삽입점, 블록 내부 구성, 실제 도형)은 추측하지 않고 비워 두었습니다.

---

## 3. 승격된 지식 (파일 → 그래프 노드)

### 3.1 해치 패턴 333

`Hatch/*.PAT` 를 AutoCAD PAT 문법(`*이름[,설명]` + `각도,원점X,원점Y,증분X,증분Y[,대시…]`)으로
파싱했습니다. **파싱 실패 0건** — 파일 끝의 `0x1A`(DOS EOF) 줄은 정의선이 아니라 종료 표식이라
건너뜁니다. 정의선 원문은 `assets/archioffice_pattern_geometry.jsonl.gz` 에 보존했습니다.

재료 분류(이름+설명 정규식): 벽돌·조적 33, 지붕·기와 19, 추상·기하 15, 석재·암석 13, 포장 12,
목재 7, 콘크리트 4, 단열재 3, 금속 3, 유리 3, 지반·토사 2, 물 1, **미분류 218**.
미분류 218건은 설명이 없거나(G1, L1, hb 등 코드형 이름) 근거가 부족한 경우로, 채우지 않았습니다.

### 3.2 도면층 표준 53

`_XPLayerSet.ini` 는 도면층 이름 → 설명/색상/선종류 표입니다. 예:

```
[AA-CLXM-CONC]  Description=기둥(콘크리트) 도면층
[AA-DWXM-DOOR]  Description=문짝(단면) 도면층
[AA-XXXX-CNTL]  Description=중심선 도면층
[AA-ELEV]       Color=3  Linetype=Continuous
```

`_XPLayerConfig.ini` 는 역할 → 도면층 매핑 5건입니다:
`BALC_ELEV=AA-ELEV`, `LEADER_LINE=AZ-LEAL`, `LEADER_TEXT=AZ-LEAT`,
`T_JABSUK=AS-PATT`, `DIMENSION=AZ-DIML`.

### 3.3 심볼 배치 설정 20 + 블록 사양 30

`.CFG` 는 ArchiOffice 심볼의 배치 규칙입니다(`[XP:SLIDETYPE]`, `[XP:LAYER]`, `[XP:SCALE]`,
`[XP:USERBLOCK]`). 그중 `[XP:DESCRIPTION]` 이 **블록명 → 사람이 읽는 사양**을 담고 있어
가장 값어치 있는 부분입니다. 예 (현대 중저속 기어리스 일반형):

```
EH10N14X125-8X21=14인승 1400(W) x 1250(D)
EH24N20X175-11X21=24인승 2000(W) x 1750(D)
```

30건 전부 `BlockSpec` 노드로 승격했고, `SymbolSetFile —placedOnLayer→ LayerStandard`,
`SymbolSetFile —hasBlockSpec→ BlockSpec` 엣지로 연결했습니다.

### 3.4 그 밖

| 대상 | 수 | 처리 |
|---|---|---|
| 선종류(`2024/ZWCADiso.lin`) | 177 | `Linetype` 노드 + 요약 |
| 명령 단축키(`*.pgp` 3개 사본) | 고유 1,397 | `CommandAlias` 노드 (`L → LINE`) |
| 외부 명령 | 9 | `ExternalCommand` 노드 |
| LISP 함수(`ZWCAD2024doc.lsp` 등) | 31 | `LispFunction` 노드 |
| ArchiOffice 명령 이름(`ArchiOfficeXP.txt`) | 28 | 설정 파일로 보존 |
| 문자 스타일/높이(`_XPTextConfig.ini`, `_SymbolSet.ini`) | 10 | `TextStyle` 노드 |
| 기타 환경 설정 | 10 | `ConfigSetting` 노드 (원문 발췌 포함) |
| 버전 | Hatch `06-19-2014`, Library `02-20-2025` | `ConfigSetting` |
| 기본 문폭(`_DoorWidth.txt`) | `900` mm | `ConfigSetting` |

### 3.5 중복 (해시로 입증)

동일 내용 그룹 3건: `ZWCAD_backup_original.pgp` = `ZWCAD_bak.pgp`,
`장애인 의자.dwg` = `장애인의자.dwg`, `_RecentColumnData.txt` = `_UserBlockFolder2.txt`(둘 다 0바이트).
`duplicateOf` 엣지로 표현했습니다.

---

## 4. 그래프 모델

- 노드 유형 18종, 술어 19종 (전체 목록은 `manifest.json`)
- 중심 노드 `ARCHIOFFICE:library` 에서 `hasCategory` / `hasPattern` / `hasLinetype` / `hasLayer` /
  `hasSetting` / `hasTextStyle` / `hasCommandAlias` … 로 확장되는 **별(star) 구조**
- 잘 알려진 별칭은 `kg_aliases` 로 분리: 도서관(`ArchiOffice`, `아키오피스`),
  카테고리(한글·영문), 재료 분류(한글·영문), 뷰(평면/입면/3D), 수목 습성

이 구조는 기존 `aec.kg_*` 스키마를 그대로 쓰며, `project_key = 'ARCHIOFFICE'` 로 다른
프로젝트와 격리됩니다.

---

## 5. 벡터 / Graph RAG

- `vectors/vector_corpus.jsonl` 2,860건: `node_id`, `kind`, `text`, `content_hash = sha256(text)`,
  `model = bge-m3`, `dim = 1024`
- `content_hash` 는 `aec.text_vectors` 의 기본키 절반이므로 파이썬에서 계산해 넣습니다(SQL에서 재계산하지 않음).
- 임베딩은 로컬 Ollama `bge-m3` 만 사용합니다(도면·설계 내용이 PC 밖으로 나가지 않음).
- 벡터 종류 분포: `command_alias` 1397, `library_symbol` 790, `hatch_pattern` 333,
  `linetype` 177, `layer_standard` 48, `lisp_function` 31, `block_spec` 30,
  `symbol_set_file` 20, `config_setting` 10, `text_style` 10, `external_command` 9, `layer_role` 5

적재 후에는 기존 GraphRAG 라우터의 KG 어휘 검색(`kg_nodes.search_text` trigram)과
`semantic` 경로(pgvector) 양쪽에서 검색됩니다. 질문 예:

- "기둥(콘크리트) 도면층 이름이 뭐야?" → `LayerStandard AA-CLXM-CONC`
- "14인승 엘리베이터 치수 알려줘" → `BlockSpec EH10N14X125-8X21` (1400x1250)
- "벽돌 조적 해치 뭐 있어?" → `HatchPattern` + `hasMaterialClass → 벽돌·조적`
- "조경 상록교목 심볼 목록" → 평면(조경설계용)/상록교목 43건

---

## 6. 담지 않은 것 (의도적 누락)

1. **DWG 내부 형상** — 765개 `.dwg` 는 파일 메타데이터만 담겼습니다. 엔티티·블록 정의·레이어
   배치는 ODA File Converter(`C:\Program Files\ODA\ODAFileConverter 27.1.0`)로 DWG→DXF 변환 후
   저장소의 `aec_intelligence.dwg`/`dxf` 어댑터로 확장하는 별도 단계입니다. 변환기 사용 여부는
   소유자 결정 사항이라 실행하지 않았습니다.
2. **`.SLB` 슬라이드/`PatchLibrary.exe` 내부** — 바이너리이며 내부 포맷이 문서화되어 있지 않습니다.
3. **미분류 해치 218건의 재료** — 근거가 없어 추측하지 않았습니다.
4. **`.dwt`/`.bak`(도면 템플릿/백업)** — 메타데이터만.
5. `interior.txt`, `RoomName.txt` 등 일부 한글 목록 파일은 항목만 보존했고 의미 해석은 하지 않았습니다.

---

## 7. 재현 / 검증

```powershell
cd C:\CODE\Ontology\library\archioffice
python tools\build_archioffice_pack.py    # 원본 재스캔 → 팩 전체 재생성 (멱등)
python tools\verify_pack.py               # 0 findings 이면 exit 0
```

`verify_pack.py` 가 실제로 검사하는 것: JSONL 파싱, 엣지 양끝 노드 존재, 별칭/벡터의 노드 참조,
노드 ID 중복, `content_hash == sha256(text)`, 모델/차원, 매니페스트 카운트 일치,
U+FFFD 대체문자 0, 한글 음절 수(90,573자).

---

## 8. 알려진 한계

- `material_class` 는 정규식 분류입니다. 정확도는 측정하지 않았고 미분류를 억지로 채우지 않았습니다.
- `query_pack.py` 는 어휘 기반 스모크 체크이며 실제 의미 검색 품질의 근거가 아닙니다.
- `ArchiOffice.dwt`(템플릿, 28,249 bytes)와 `ArchiOffice.bak`(36,219 bytes, 2015년)는
  메타데이터만 담았습니다. 템플릿의 도면층/블록 내용은 DXF 변환 후 확장 대상입니다.
- `.pgp` 는 ZWCAD/AutoCAD 플랫폼 파일이며 ArchiOffice 고유 지식이 아닙니다.
  `command_alias` kind 로 구분되어 있어 검색에서 제외할 수 있습니다.

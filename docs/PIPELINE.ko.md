# 도면 전수 적재 파이프라인 운영 가이드 (Windows PC)

Google Drive for Desktop(`G:\내 드라이브\...`)에 있는 수만 개의 CAD 도면을 한 번에 조사(census)하고,
중복을 제거한 뒤 PostgreSQL(+AGE/pgvector/PostGIS)에 적재하는 절차입니다.
모든 단계는 **다시 실행해도 안전**합니다(재개·중복 방지).

```
preflight (Docker·DB·DWG 변환기·AEC_IMPORT_ROOTS·디스크 확인)  [+ 선택: embeddings(bge-m3) 기동]
   ↓
census (전수조사: 경로·크기·sha256·DWG 버전)  →  census.jsonl / census.csv / summary.md
   ↓
enqueue-census (sha256 기준 1회 적재, 사본은 aliases 로 기록)  →  aec.jobs
   ↓
run-workers (N개 프로세스로 큐 소진, DWG는 ODA 또는 LibreDWG로 DXF 변환)  →  aec.documents / aec.objects / 그래프
   ↓
report (프로젝트별 객체 종류 수·실패 작업·census 대비 적재율)  →  report.md / report.json
   ↓
backup (pg_dump -Fc, 최근 14개 보관)  →  G:\내 드라이브\AEC-INTELLIGENCE\backups
```

---

## 0. 사전 준비 (최초 1회)

| 항목 | 내용 |
|---|---|
| Docker Desktop | 실행 중이어야 함 |
| Python 3.11+ | 저장소 루트에서 `python -m venv .venv` → `.venv\Scripts\pip install -e ".[operational,pdf,bim]"` |
| ODA File Converter | `C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe` (DWG 변환용, 권장) |
| LibreDWG (대안) | ODA 를 설치할 수 없을 때 `dwg2dxf.exe` (GNU LibreDWG Windows 빌드). PATH 에 두거나 `AEC_LIBREDWG_EXECUTABLE` 로 지정 |
| 로컬 작업 폴더 | `D:\AECData` 처럼 **로컬 디스크** (Drive 동기화 폴더에 DB·산출물을 두지 마세요) |

### 0-1. `.env` 작성 (저장소 루트, `.env.example` 복사)

```dotenv
# docker compose 용
AEC_HOST_DATA_ROOT=D:/AECData
AEC_DB_PASSWORD=원하는-비밀번호

# 파이프라인(파이썬) 용
AEC_DATABASE_URL=postgresql://aec:원하는-비밀번호@localhost:55432/aec
AEC_DATA_ROOT=D:\AECData
# 적재를 허용할 원본 폴더. 여러 개면 ; 로 구분. Drive 루트를 그대로 넣어도 됩니다.
AEC_IMPORT_ROOTS=G:\내 드라이브;D:\AECData\imports
# DWG 변환기: auto(ODA 우선, 없으면 LibreDWG) | oda | libredwg
AEC_DWG_CONVERTER=auto
AEC_ODA_EXECUTABLE=C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe
# LibreDWG dwg2dxf.exe 경로(선택). 비우면 PATH 의 dwg2dxf 사용
AEC_LIBREDWG_EXECUTABLE=
# 백업 폴더(선택, 기본값은 아래와 동일)
AEC_BACKUP_DIR=G:\내 드라이브\AEC-INTELLIGENCE\backups
# 임베딩 서버(선택). 비우면 결정적 해시 벡터 사용(의미 검색 아님).
# run-pipeline.ps1 -Embeddings 를 쓰면 bge-m3 컨테이너를 띄우고 http://localhost:58080 으로 자동 설정
AEC_EMBEDDING_URL=
```

- `scripts\ops\*.ps1` 은 이 `.env` 를 자동으로 읽고, 한글 경로/출력이 깨지지 않도록 UTF-8 모드(`PYTHONUTF8=1`)로 실행합니다.
- 파이썬 CLI 를 직접 실행할 때는 PowerShell 에서 먼저:
  ```powershell
  $env:PYTHONUTF8 = '1'
  Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$k" $v }
  ```
- `AEC_IMPORT_ROOTS` 밖의 파일은 `enqueue-census` 가 건너뛰고 경고합니다(보안 경계). 한글·공백 경로는 그대로 써도 됩니다.
  ODA 는 한글 경로를 조용히 무시하는 문제가 있어, 워커가 자동으로 ASCII 임시 폴더에 복사해 변환합니다.

### 0-2. DWG 변환기 선택 (ODA / LibreDWG)

| `AEC_DWG_CONVERTER` | 동작 |
|---|---|
| `auto` (기본) | `AEC_ODA_EXECUTABLE` 이 있거나 PATH 에 `ODAFileConverter` 가 있으면 ODA, 없으면 LibreDWG `dwg2dxf` |
| `oda` | ODA 만 사용 (없으면 작업 실패) |
| `libredwg` | LibreDWG 만 사용 (`AEC_LIBREDWG_EXECUTABLE` 또는 PATH 의 `dwg2dxf`) |

- LibreDWG 는 오픈소스(GPL) 대안입니다. 최신 DWG(AC1032, 2018+)·복잡한 도면에서 ODA 보다 변환 실패/누락이 많을 수 있으므로,
  가능하면 ODA 를 쓰고 LibreDWG 는 ODA 설치가 불가한 PC 의 대체 수단으로 쓰세요.
- 변환 결과는 `artifacts\<doc_id>\rev-0\conversion.json` 에 어떤 변환기로 처리했는지와 오류가 기록됩니다.

### 0-3. 실제 임베딩(bge-m3) 사용 (선택)

```powershell
docker compose --profile embeddings up -d embeddings   # 최초 기동 시 모델(약 2.3GB) 다운로드
Invoke-WebRequest http://localhost:58080/health -UseBasicParsing   # 200 이면 준비 완료
```

- PC 에서 도는 워커는 `AEC_EMBEDDING_URL=http://localhost:58080` 을 사용합니다(컨테이너 내부는 `http://embeddings:80`).
- `run-pipeline.ps1 -Embeddings` 는 위 과정을 자동으로 수행합니다(기동 → `/health` 대기(기본 900초, `-EmbeddingsTimeoutSec`) → `AEC_EMBEDDING_URL` 설정).
- 비워두면 해시 벡터(`hash-sha256-1024-v1`)로 저장되며 의미 검색이 되지 않습니다. 나중에 임베딩 서버를 켠 뒤
  `python -m aec_intelligence.operational.cli reembed` (= `operational.embeddings.reindex_embeddings()`) 로 기존 행을 다시 임베딩할 수 있습니다.
  `AEC_EMBEDDING_MODEL` 벡터가 없는 객체만 대상이며, 옵션: `--project P-...`, `--batch-size N`,
  `--dry-run`(대상/오래된 행 수만 출력), `--delete-stale`(활성 모델 벡터가 생긴 객체의 다른 모델 행 삭제).
- `AEC_IMPORT_ROOTS` 는 `;` 또는 `os.pathsep`(리눅스 `:`)로 여러 경로를 구분합니다. `C:\`, `G:/` 같은 드라이브 문자는 분리되지 않습니다.

---

## 1. DB 기동 및 스키마 초기화

```powershell
docker compose up -d db          # aec-db 컨테이너만 기동 (localhost:55432)
docker compose ps                # STATUS 가 healthy 인지 확인
python -m aec_intelligence.operational.cli init-db
```

> **주의**: `docker compose up` 으로 `worker` 컨테이너까지 띄우지 마세요. 컨테이너 안에서는 `G:` 드라이브가
> 보이지 않아, 같은 `cad` 큐의 작업을 가져가서 "파일 없음"으로 실패시킵니다. 이 파이프라인은 PC 에서
> `run-workers` 로 워커를 실행합니다.

---

## 2. 전수조사 (census) — 파일럿 폴더부터

```powershell
# (1) 파일럿: 최상위 폴더 하나만 조사. 프로젝트 구분은 루트 기준 최상위 폴더명으로 유지됩니다.
python -m aec_intelligence.operational.cli census "G:\내 드라이브\도면" --only-folder "현장A 오피스텔" --out D:\AECData\census --resume

# (2) 전체 조사 (중단돼도 같은 명령으로 이어서 진행)
python -m aec_intelligence.operational.cli census "G:\내 드라이브\도면" --out D:\AECData\census --resume

# 루트가 여러 개면
python -m aec_intelligence.operational.cli census "G:\내 드라이브\도면" --root "G:\공유 드라이브\설계" --out D:\AECData\census --resume
```

옵션

| 옵션 | 설명 |
|---|---|
| `--extensions .dwg,.dxf,.ifc,.pdf` | 조사 대상 확장자 (기본값) |
| `--resume` | 이전 `census.jsonl` 에서 **경로·크기·수정시각이 같은 파일은 해시를 다시 계산하지 않음**. 오류 행은 재시도 |
| `--only-folder <이름>` | 루트 아래 해당 최상위 폴더만 조사(반복 지정 가능). 다른 폴더의 기존 행은 보존 |
| `--skip-placeholders` | Drive "스트리밍" 모드의 클라우드 전용(미다운로드) 파일은 해시하지 않음(`status=placeholder`). 기본은 해시를 위해 내려받습니다 |
| `--flush-every 100` | N개마다 디스크에 기록 (중단 시 손실 최소화) |

산출물 (`--out` 폴더)

| 파일 | 내용 |
|---|---|
| `census.jsonl` | 파일당 1행: `path, rel_path, root, top_folder, name, ext, size, mtime, sha256, dwg_version(AC1032 등), dwg_release(2018+ 등), placeholder, status(ok/skipped_temp/error/placeholder), error` |
| `census.csv` | 위와 동일 (Excel 에서 바로 열림, UTF-8 BOM) |
| `summary.json`, `summary.md` | 확장자별·최상위 폴더별·DWG 버전별 개수/용량, 중복 그룹(sha256), 임시파일 수, 폴더 접근 오류 |

- 임시/백업 파일(`*.bak, *.sv$, ~$*, *.dwl, *.dwl2, *.tmp, *.ac$`)은 해시하지 않고 `skipped_temp` 로만 집계합니다.
- 260자 넘는 경로는 `\\?\` 접두어로 자동 처리, 접근 거부 폴더는 건너뛰고 `walk_errors` 에 기록합니다.
- DWG 버전 표: AC1015=2000, AC1018=2004, AC1021=2007, AC1024=2010, AC1027=2013, AC1032=2018+ (DXF 는 `$ACADVER` 로 판별).
- 속도: Drive 는 첫 해시 시 파일을 내려받으므로 느립니다. 밤새 돌리고 다음날 `--resume` 으로 이어가는 방식을 권장합니다.

---

## 3. 작업 등록 (enqueue-census)

```powershell
# 먼저 무엇이 등록될지 확인 (DB 변경 없음)
python -m aec_intelligence.operational.cli enqueue-census D:\AECData\census --only-folder "현장A 오피스텔" --limit 20 --dry-run

# 파일럿 등록
python -m aec_intelligence.operational.cli enqueue-census D:\AECData\census --only-folder "현장A 오피스텔" --limit 50

# 전체 등록
python -m aec_intelligence.operational.cli enqueue-census D:\AECData\census
```

규칙

| 항목 | 규칙 |
|---|---|
| 중복 | 같은 sha256 은 **1번만 적재**. 사전순 첫 경로가 원본, 나머지는 작업 payload 의 `aliases` 에 기록 |
| `dedup_key` | sha256 → 같은 명령을 여러 번 실행해도 작업이 늘지 않음(`already_present` 로 집계) |
| `document_id` | `doc_` + sha256 앞 24자 (내용이 같으면 항상 같은 ID) |
| `project_id` | `P-` + 최상위 폴더명(공백·특수문자는 `-`, 한글 유지). 예: `현장A 오피스텔` → `P-현장A-오피스텔`. 루트 바로 아래 파일은 루트 폴더명 |
| `discipline` | 경로의 가장 깊은 폴더/파일명부터 키워드 탐색: 구조/STR→`STR`, 기계·설비·위생·소방/MEP·HVAC→`MEP`, 전기·통신/ELEC→`ELEC`, 토목→`CIVIL`, 조경→`LAND`, 인테리어·실내→`INT`, 건축·평면·입면·단면·상세·창호/ARCH→`ARCH`, 없으면 `--default-discipline`(기본 ARCH) |

기타 옵션: `--requeue-failed`(실패 작업을 다시 대기열로), `--queue cad`, `--extensions .dwg`, `--ignore-import-roots`.

---

## 4. 워커 실행 (run-workers)

```powershell
python -m aec_intelligence.operational.cli run-workers -n 3 --out D:\AECData\census\runs
```

- N개의 프로세스(Windows spawn 방식)가 큐를 비울 때까지(QUEUED·RUNNING 0) 처리 후 종료하고,
  성공/실패 수와 실패 목록(오류 메시지 포함)을 출력·저장(`run-summary.json`)합니다.
- 시작 전에 대기 중인 프로젝트의 그래프(AGE)를 미리 만들어 동시 생성 충돌을 막습니다.
  워커가 도는 중에 **새 프로젝트**를 추가 등록했다면 `run-workers` 를 한 번 더 실행하세요.
- 프로세스 수는 CPU 코어-1, 메모리(대형 DWG 1개당 1–2GB)를 고려해 정합니다. `run-pipeline.ps1` 은 `-Workers` 를
  생략하면 **min(코어수-1, 물리 RAM/2GB)** (최소 1)로 자동 설정합니다.
- 결과물: `D:\AECData\artifacts\<doc_id>\rev-0\` (변환 DXF, SVG 미리보기, geometry jsonl), `D:\AECData\snapshots\<doc_id>\rev-0.json`.
- 중단(Ctrl+C) 후 재실행하면 실행 중이던 작업은 lease(5분) 만료 뒤 다시 처리됩니다(최대 3회).

---

## 5. 현황 리포트 (report)

```powershell
python -m aec_intelligence.operational.cli report --out D:\AECData\census\report --census D:\AECData\census
python -m aec_intelligence.operational.cli report --out D:\AECData\census\report --project "P-현장A-오피스텔"
```

- `report.md`: 프로젝트별 문서 수, census 고유 파일 수(적재율 비교), 객체 종류(`Wall, Door, Window, Annotation, Dimension, View ...`)별 개수, 실패 작업 목록
- `report.json`: 같은 내용의 기계 판독용

---

## 6. 백업 / 복원

```powershell
# 수동 백업 (docker 안의 pg_dump 사용, 최근 14개만 보관)
powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1
# 또는
python -m aec_intelligence.operational.cli backup --target "G:\내 드라이브\AEC-INTELLIGENCE\backups" --docker-container aec-db --keep 14

# 매일 03:00 자동 백업 등록 (원할 때 직접 1회 실행; 해제는 Unregister-ScheduledTask)
powershell -ExecutionPolicy Bypass -File scripts\ops\register-backup-task.ps1 -At 03:00
```

- 파일명: `aec-db-YYYYMMDDTHHMMSSZ.dump` (UTC). 회전 삭제는 **그 폴더 안의 이 이름 패턴 파일만** 대상입니다.
- 덤프는 `.partial` 로 쓴 뒤 완료 시 이름을 바꾸므로, 불완전한 파일이 Drive 에 동기화되어 남지 않습니다.
- 로그: `logs\backup-YYYYMMDD.log`

복원 (기존 객체를 지우고 다시 만듭니다 — `--yes` 필수)

```powershell
python -m aec_intelligence.operational.cli restore "G:\내 드라이브\AEC-INTELLIGENCE\backups\aec-db-20261002T030000Z.dump" --docker-container aec-db --yes
# 빈 새 DB 로 복원 검증만 할 때
docker exec aec-db createdb -U aec aec_restore_check
python -m aec_intelligence.operational.cli restore <덤프> --database-url postgresql://aec:비밀번호@localhost:55432/aec_restore_check --no-clean --yes
```

`--docker-container` 를 주면 DSN 의 사용자/DB 이름으로 컨테이너 안에서 `pg_restore` 를 실행합니다.

---

## 7. 한 번에 실행 (스크립트)

```powershell
# 사전 점검만
powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root "G:\내 드라이브\도면" -PreflightOnly
# 파일럿
powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root "G:\내 드라이브\도면" -OnlyFolder "현장A 오피스텔" -Limit 50 -Workers 2
# 전체 + 실제 임베딩(bge-m3), 워커 수 자동
powershell -ExecutionPolicy Bypass -File scripts\ops\run-pipeline.ps1 -Root "G:\내 드라이브\도면" -Embeddings
```

preflight → (선택) embeddings → census(--resume) → enqueue-census → run-workers → report 를 순서대로 실행합니다.
중간에 멈추면 같은 명령을 다시 실행하세요.

| 파라미터 | 설명 |
|---|---|
| `-Workers N` | 워커 프로세스 수. 생략/0 이면 min(코어-1, RAM/2GB) |
| `-Embeddings` | `embeddings` 컨테이너(bge-m3) 기동, `/health` 대기 후 `AEC_EMBEDDING_URL` 설정 |
| `-EmbeddingsTimeoutSec 900` | 임베딩 서버 준비 대기 시간(초) |
| `-PreflightOnly` / `-SkipPreflight` | 점검만 실행 / 점검 생략 |
| `-MinFreeGB 20` | `AEC_DATA_ROOT` 드라이브의 최소 여유 공간 |
| `-SkipCensus` | census 생략(이미 조사한 결과로 등록·처리만) |

사전 점검(preflight) 항목 — 하나라도 `[FAIL]` 이면 중단합니다.

| 항목 | 실패 시 조치 |
|---|---|
| Docker 엔진 실행 여부 | Docker Desktop 실행 |
| `aec-db` 컨테이너 `healthy` | `docker compose up -d db` 후 잠시 대기 |
| DWG 변환기 (`AEC_DWG_CONVERTER` 에 따라 ODA / `dwg2dxf`) | `AEC_ODA_EXECUTABLE` 또는 `AEC_LIBREDWG_EXECUTABLE` 경로 확인 (0-2 참고) |
| `AEC_IMPORT_ROOTS` 가 `-Root` 를 포함 | `.env` 에 Drive 루트 추가 (아니면 enqueue 가 모든 파일을 건너뜀) |
| `AEC_DATA_ROOT` 드라이브 여유 공간 ≥ `-MinFreeGB` | 공간 확보 또는 다른 로컬 디스크 지정 |

스크립트 파일은 UTF-8(BOM) 으로 저장되어 있어 Windows PowerShell 5.1 에서도 한글 경로(`G:\내 드라이브`)가 깨지지 않습니다.
편집할 때 인코딩을 바꾸지 마세요.

---

## 8. 재개·문제 해결

| 증상 | 조치 |
|---|---|
| census 가 중간에 멈춤/PC 재부팅 | 같은 명령에 `--resume`. 끊긴 마지막 줄은 자동으로 잘라냅니다 |
| 파일을 수정함 | `--resume` 시 크기/수정시각이 바뀐 파일만 다시 해시. `enqueue-census` 는 새 sha256 으로 새 작업 생성 |
| `outside_import_roots` 경고 | `.env` 의 `AEC_IMPORT_ROOTS` 에 Drive 루트(`G:\내 드라이브`) 추가 |
| `ODAFileConverter is not configured` | `AEC_ODA_EXECUTABLE` 경로 확인, 또는 LibreDWG 설치 후 `AEC_DWG_CONVERTER=libredwg` |
| LibreDWG 변환 실패/객체 누락 | 해당 파일만 ODA 가 있는 PC 에서 `AEC_DWG_CONVERTER=oda` 로 `--requeue-failed` 재처리 |
| `embeddings not healthy` | `docker logs aec-embeddings` 확인(모델 다운로드 중이면 `-EmbeddingsTimeoutSec` 늘리기) |
| `OCR_REQUIRED` 실패 (스캔 PDF) | `ocr` 큐/OCR 워커가 필요한 파일입니다. 일반 텍스트 PDF 는 cad 큐에서 처리됩니다 |
| 실패 작업 재시도 | 원인 해결 후 `enqueue-census <census> --requeue-failed` → `run-workers` |
| 한글이 `\uXXXX` 로 출력됨 | `$env:PYTHONUTF8='1'` 설정 (스크립트는 자동) |
| Drive 파일이 클라우드 전용 | 해시 시 자동 다운로드됨. 디스크가 부족하면 Drive 설정에서 폴더를 "오프라인 사용" 으로 나눠 진행하거나 `--skip-placeholders` |

## 9. 한계

- 같은 내용(sha256)의 파일이 여러 프로젝트에 있으면 사전순 첫 경로의 프로젝트에만 적재되고, 나머지 경로는 `aliases` 로만 남습니다.
- DWG 버전은 헤더 6바이트로만 판별합니다(파일 손상 여부는 변환 단계에서 확인).
- 레이어 기반 벽/문/창 분류는 후보(`AI_INFERRED`)이며, 확정 BIM 객체가 아닙니다.

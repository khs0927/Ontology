# AEC Ontology 운영 가이드 (Windows PC, 단일 진입점)

이 문서 하나로 PC(DESKTOP-KTQHS1I)의 AEC 스택을 켜고 끄고, 점검하고, 복구합니다.
세부 내용은 각 절 끝의 링크 문서에 있습니다. 비밀값(토큰·DB 비밀번호)은 **화면에 출력하지 않습니다**.

| 구성 요소 | 위치 | 비고 |
|---|---|---|
| 저장소(배포본) | `C:\CODE\Ontology` (master) | `.env`(gitignore)에 토큰·DB 비밀번호 |
| DB `aec-db` | Docker, `127.0.0.1:55432` | Postgres 16 + AGE + pgvector + PostGIS, 볼륨 `ontology_aec-pgdata` |
| API `aec-api` | Docker, `127.0.0.1:58000` | Bearer 토큰 필수(없으면 401) |
| Docker 디스크 | `D:\DockerDesktop\disk\docker_data.vhdx` | **D:는 USB 디스크**(JMicron, 30–65 MB/s) |
| Ollama | `C:\AECLocal\Ollama` (NVMe) | 모델 `C:\AECLocal\Ollama\models` (bge-m3, qwen3:8b), `127.0.0.1:11434` |
| 데이터 | `D:\AECData` | 백업, DXF 캐시, census, 로그, 평가셋(비공개) |
| 호스트 워커 | `C:\CODE\Ontology\.venv` | ODA 변환·OCR·적재는 호스트에서 실행 |

> 이 저장소는 **공개(public)** 입니다. 도면, census 결과, DB 덤프, `.env`, 평가셋은 절대 커밋하지 않습니다.

---

## 1. 켜기 / 끄기

### 켜기 (재부팅 후)
활성화된 예약 작업은 로그온 후 올라옵니다(§3). Docker Desktop이 실행 중이어야 합니다. 비활성 작업은 자동 실행되지 않습니다.
```powershell
docker ps --format "{{.Names}} {{.Status}}"          # aec-db (healthy), aec-api
cd C:\CODE\Ontology
docker compose up -d db api                            # 내려가 있을 때만
Get-ScheduledTask -TaskPath \AEC\ | ft TaskName,State  # Workers/Ollama = Running
Invoke-RestMethod http://127.0.0.1:11434/api/version   # Ollama
```

### 배포(코드 갱신) — 워커를 먼저 비우고
```powershell
cd C:\CODE\Ontology
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Drain -TimeoutMin 30   # 재임베딩도 정지
git pull --ff-only
docker compose build api
docker compose run --rm --no-deps migrate        # 스키마 마이그레이션(멱등)
docker compose up -d --no-deps api               # aec-db는 건드리지 않음
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Resume                # 워커 재개; 재임베딩은 활성 작업만
```
- `-Drain`은 `D:\AECData\bulk\STOP-WORKERS` 파일을 만듭니다. 이 파일이 있는 동안 워커·재임베딩은 새로 시작하지 않습니다.
  **작업이 끝나면 꼭 `-Resume`** (파일이 남아 있으면 적재가 멈춘 상태로 유지됩니다).
- 큰 DWG/PDF 작업은 10분 이상 걸릴 수 있습니다. 시간 안에 안 끝나면 `-Drain`을 다시 실행해 기다립니다.

### 끄기
```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Drain
docker compose stop api        # DB까지 끌 때만: docker compose stop db
```
Docker Desktop/WSL 재시작은 사용자 컨테이너 전체(workmachine 등)를 재시작하므로 필요할 때만 합니다.
재시작 후 `restart` 정책이 없는 컨테이너(w19rec, cokac-graph-pg, cokac-graph-api)는 직접 `docker start` 해야 합니다.

## 2. 토큰과 교체

- `scripts\ops\init-env.ps1` 이 `.env`를 만들고 `AEC_API_TOKEN`(32바이트)과 `AEC_DB_PASSWORD`를 생성합니다. 기존 값은 바꾸지 않습니다.
- `-SetUserEnv`: 같은 토큰을 Windows 사용자 환경변수 `POWERCAD_ONTOLOGY_TOKEN`에 넣습니다(power-cad-mcp·MCP 클라이언트용).
- **토큰 교체**
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\ops\init-env.ps1 -RotateToken -SetUserEnv
  docker compose up -d --no-deps api      # 새 토큰으로 API 재생성
  # MCP 클라이언트(Claude Desktop, Cursor 등)를 재시작해야 새 사용자 환경변수를 읽습니다
  ```
- DB 비밀번호는 교체하지 않습니다(초기화된 볼륨에서 잠깁니다). 꼭 바꿔야 하면 `ALTER ROLE aec PASSWORD ...` 후 `.env` 두 곳(`AEC_DB_PASSWORD`, `AEC_DATABASE_URL`)을 함께 고칩니다.
- `.env` 수정 전에는 백업: `Copy-Item .env C:\CODE\_ops\Ontology.env.bak-<날짜>`.
- 호스트 DSN은 `localhost`가 아니라 **`127.0.0.1`** (Windows libpq가 `::1`을 먼저 시도해 연결마다 10초 지연).

자세히: [api-security.md](api-security.md)

## 3. 예약 작업 (`\AEC\` 및 루트 `\`, 관리자 권한 불필요, 로그온 중에만 실행)

아래 트리거는 등록 설정입니다. 실제 실행 여부는 작업의 State를 함께 확인합니다. 2026-10-05 감사 시 AEC-Reembed와 AEC-GraphRAG-Refresh는 **Disabled**였습니다. RAM 여유를 확보하고 활성화하기 전에는 벡터 보완과 KG 갱신이 자동으로 실행되지 않습니다.

| 작업 | 내용 | 트리거 | 등록 스크립트 |
|---|---|---|---|
| AEC-Bulk-Workers | `bulk-run.ps1 -Role workers` (적재 워커 루프, 개수는 `sources.json`의 `workers`) | 로그온 + 10분마다 감시 | `register-bulk-tasks.ps1` |
| AEC-Bulk-Census | census 재개(끝난 소스는 건너뜀) | 로그온 | 〃 |
| AEC-Bulk-Census-Refresh | 새/변경 도면 탐색 | 매일 03:00 | 〃 |
| AEC-Ollama | `ollama-serve.ps1` → `C:\AECLocal\Ollama\ollama.exe serve` (이미 응답 중이면 즉시 종료 = 감시, 시작 시 로그 200 MB 넘으면 회전) | 로그온 + 5분마다 감시 | `register-host-tasks.ps1` |
| AEC-Reembed | `reembed.ps1` (대기 중 벡터 채우기, 청크 단위 커밋) | 로그온 + 30분마다 | 〃 |
| AEC-WSL-Reclaim | `wsl-reclaim.ps1` (Windows 가용 RAM < 750 MB일 때만, 최대 2시간에 1번, VM dirty 페이지 ≤ 64 MB일 때만 깨끗한 페이지 캐시 비움; `sync` 안 함) | 30분마다 | 〃 |
| AEC-GraphRAG-Refresh | `graphrag.ps1 refresh`: 바뀐 프로젝트만 kg-build → kg-summarize(FAILED 커뮤니티 재시도) → kg-stats. 대화형 질의에 양보, 뮤텍스로 중복 방지, 3시간 제한, 로그 `D:\AECData\bulk\logs\graphrag-yyyyMMdd.log`(14일 지나면 삭제) | 로그온 10분 후 + 2시간마다 | `register-host-tasks.ps1 -Only GraphRag` |
| `\AEC-DB-Backup` | `backup.ps1 -Keep 7 -Target D:\AECData\backups` | 매일 04:30 | `register-backup-task.ps1` |

- 위 등록 스크립트로 만든 작업은 `MultipleInstances=IgnoreNew`를 사용합니다. 워커·Ollama·GraphRAG 등에는 내부 뮤텍스도 있습니다. 레거시 작업까지 같은 설정이라고 가정하지 않습니다.
- 활성 감시 트리거는 프로세스 종료 후 다음 주기에 다시 시작합니다(5–30분). 긴 작업은 **절대 임시 셸에서 띄우지 말고** 작업 스케줄러로 실행합니다.
- 상태: `Get-ScheduledTask -TaskPath \AEC\ | Get-ScheduledTaskInfo | ft TaskName,LastRunTime,LastTaskResult`
- 백업은 루트 `\`에 등록되어 위 조회에 나오지 않습니다: `Get-ScheduledTask -TaskPath '\' -TaskName AEC-DB-Backup | Get-ScheduledTaskInfo | ft TaskName,LastRunTime,LastTaskResult`.
- 감사 시 `\AEC\`에는 표의 7개 외에 레거시 `AEC-Ops-DbTune`, `AEC-Ops-DeployApi`, `AEC-Ops-DeployFinal`(Ready, 트리거 없음), `AEC-Ops-FinalAB`, `AEC-Ops-FinalCheck`, `AEC-Ops-FinalWrap`(Disabled)이 있었습니다. `D:\AECData\dev\*.ps1`을 실행하는 수동 작업이며 자동 복구 작업으로 취급하지 않습니다.
- 로그오프하면 멈춥니다(로그오프 후 실행에는 '일괄 작업으로 로그온' 권한=관리자 필요, Docker Desktop도 세션 필요).

## 4. Ollama (C:\AECLocal, GPU)

- 실행 파일·모델은 NVMe(`C:\AECLocal\Ollama`)에 있습니다. USB D:에서는 모델 로드가 시간 초과되어
  (`GPU discovery watchdog timed out`) Vulkan으로 떨어지거나 적재 작업마다 임베딩 대기가 생겼습니다.
  `D:\Ollama`는 원래 설치본(트레이 앱)이며 모델 경로로는 쓰지 않습니다.
- 사용자 환경변수 `OLLAMA_MODELS=C:\AECLocal\Ollama\models`; 서버 설정은 `ollama-serve.ps1`이 고정
  (`MAX_LOADED_MODELS=2`, flash attention, KV `q8_0`, `KEEP_ALIVE=10m`, `127.0.0.1` 전용).
- 점검: `C:\AECLocal\Ollama\ollama.exe ps` → `PROCESSOR 100% GPU`. 로그 `D:\AECData\ollama-logs\serve.log`
  에서 `inference compute ... library=CUDA` 확인(Vulkan이면 문제). 로그는 200 MB 넘으면 시작 시 `.1`로 교체.
- 재시작: `Stop-ScheduledTask -TaskPath \AEC\ -TaskName AEC-Ollama; Get-Process ollama,llama-server | Stop-Process; Start-ScheduledTask -TaskPath \AEC\ -TaskName AEC-Ollama`
- 기준값(2026-10-04): qwen3:8b 콜드 로드 12 s, 웜 65 tok/s; bge-m3 로드 7.5 s.

## 5. Docker 디스크(D:)와 메모리

- Docker Desktop 디스크 위치 = `D:\DockerDesktop` (설정 → Resources → Advanced). C:의 옛 vhdx는 삭제됨.
- `%USERPROFILE%\.wslconfig`: `memory=6GB, swap=4GB, autoMemoryReclaim=dropCache` (WSL 재시작 시 적용; 현재 적용됨).
- D:는 USB라 무작위 읽기가 느립니다. Postgres의 HNSW 인덱스가 캐시에서 밀려나면 벡터 INSERT/검색이 느려집니다.
  그래서 AEC-WSL-Reclaim은 Windows 메모리가 정말 부족할 때만 캐시를 비웁니다.
- 공간 점검: `docker system df`, `Get-PSDrive C,D`.
- **백업도 D:에 있습니다**(DB와 같은 USB 디스크). 디스크 고장 시 둘 다 잃으므로, 중요한 시점에는 덤프를 다른 디스크/Drive로
  복사하세요([DRIVE-CHECKPOINT.ko.md](DRIVE-CHECKPOINT.ko.md)).

### aec-db 크래시와 복구 시간 (2026-10-04 22:01 사건)

2026-10-05 감사에서는 컨테이너 재생성으로 당시 로그가 소실되어 아래 WAL 양·복구 시간과 사건 인과관계를 재검증하지 못했습니다. 이 수치를 현재 성능 기준이나 확정된 원인으로 사용하지 않습니다. 완화 설정은 현재 소스에서 확인합니다.

- 증상: `server process (PID …) exited with exit code 2` → postmaster가 모든 연결을 끊고 크래시 복구,
  WAL redo 366 MB에 **489 s**(USB에서 캐시가 빈 상태의 무작위 읽기). reembed가 죽고 API가 8분간 응답 불가.
- 확인한 것: VM 커널 로그(`wsl -d docker-desktop -e dmesg`)에 OOM/segfault 없음, VM 가용 메모리 3.9 GB,
  Postgres 로그에 해당 PID의 ERROR/PANIC 없음. 종료 코드 2는 Postgres 백엔드의 비상 종료 핸들러
  (SIGQUIT → `_exit(2)`) 형태이며 보낸 쪽은 로그로 특정할 수 없었습니다(근본 원인 미확정).
- 같은 순간의 사건: AEC-WSL-Reclaim의 `docker run --privileged alpine sh -c "sync; echo 1 > drop_caches"`
  (22:00:46–22:01:10, 커널 로그 `drop_caches: 1`)가 6분짜리 체크포인트(write 254 s, sync 93 s) 도중에
  VM 전체 dirty 페이지를 USB로 강제 flush.
- 완화(코드에 반영):
  1. `wsl-reclaim.ps1`: `sync` 제거, dirty+writeback > 64 MB면 건너뜀, 최대 2시간에 1번.
  2. `docker-compose.yml` db: `shared_buffers=512MB`(공유 메모리라 drop_caches가 못 비움 → 재시작 후에도 핫 페이지 유지),
     `work_mem=16MB`, `maintenance_work_mem=256MB`, `effective_cache_size=2GB`, `max_wal_size=512MB`(redo 상한 ≈ 절반),
     `wal_compression=lz4`(full-page image 축소 → USB 쓰기 감소), `log_checkpoints=on`, `shm_size=256m`
     (병렬 쿼리의 `could not resize shared memory segment` 해결), `stop_grace_period=10m`(기본 10 s면 종료 체크포인트 중
     SIGKILL → 매 재시작이 크래시 복구). 기본값은 `docker-compose.yml`에 있으며, `.env`에 `AEC_PG_SHARED_BUFFERS` 등 해당 `AEC_PG_*` 변수를 추가하면 재생성 시 덮어쓸 수 있습니다.
  3. VM 메모리 예산(6 GB): Postgres 공유 512 MB + 백엔드 ~10×16 MB + 다른 컨테이너 ~1.5 GB → 여유 충분.
- db 설정 적용 = 컨테이너 재생성(재시작). 순서: `stop-workers.ps1` 로 워커·reembed 비우기 →
  `docker exec aec-db psql -U aec -d aec -c CHECKPOINT` (종료 체크포인트를 짧게) → `docker stop -t 600 aec-db` →
  `docker compose up -d --no-deps db` → healthy 확인 → `stop-workers.ps1 -Resume`.
- 확인: `docker exec aec-db psql -U aec -d aec -Atc "show shared_buffers"`; 크래시가 다시 나면
  `docker logs aec-db --since <UTC>` 와 `wsl -d docker-desktop -e dmesg` 를 함께 보고, `wsl-reclaim.log`의 시각과 대조합니다.

## 6. 백업 / 복원 / 복원 훈련

```powershell
# 수동 백업 (pg_dump custom, 컨테이너 안에서 실행; 같은 폴더의 aec-db-*.dump만 회전)
powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1 -Target D:\AECData\backups -Keep 7
# 복원 훈련: 스크래치 DB에 복원 → 모든 테이블 행 수·인덱스 비교 → 스크래치 삭제
powershell -ExecutionPolicy Bypass -File scripts\ops\restore-drill.ps1 -Dump D:\AECData\backups\aec-db-<시각>Z.dump
```
- 행 수가 정확히 같으려면 덤프 전에 `stop-workers.ps1 -Drain` (아니면 적재 중인 테이블만 차이).
- 복원 훈련은 DB 크기만큼 VM 공간이 필요하고 USB 디스크에서 40분 이상 걸립니다(1.1 GB 덤프 기준).
- 실제 복원(재해 복구)은 API·워커를 멈춘 뒤 `python -m aec_intelligence.operational.cli restore <dump경로> --yes` (대상 DB 객체를 삭제·재생성하므로 대상 DSN을 먼저 확인) — [database-backup-restore.md](database-backup-restore.md),
  Drive 체크포인트는 [DRIVE-CHECKPOINT.ko.md](DRIVE-CHECKPOINT.ko.md).

## 7. 대량 적재 모니터링과 디스크 가드

- 설정: `D:\AECData\bulk\sources.json` (비공개; 원본 폴더 목록). 수정 전 `.bak-<날짜>`로 백업.
  - `"workers"`: 워커 수(라운드마다 다시 읽음). RAM 15 GB PC에서는 1–2.
  - 워커는 각 라운드 시작 전에 호스트 가용 RAM을 확인합니다. 기본 하한은 2048 MB이며 양의 정수 환경 변수 `AEC_MIN_AVAILABLE_MB`로 설정합니다. 하한 미만이거나 메모리 조회가 실패하면 `paused` 로그를 남기고 `IdleSleepSec`(기본 300초) 후 다시 확인합니다. 이때 예약 작업은 **Running**이어도 Python 적재 워커가 없고 큐가 소비되지 않을 수 있습니다. 진행 여부는 작업 상태와 함께 워커 로그·완료 이벤트로 확인합니다.
  - `"min_free_gb": {"C:\\": 15, "D:\\": 60}` (2026-10-04 설정값): 드라이브 여유 공간이 이보다 적으면 census·워커가 **일시정지**(5분마다 재확인).
    C:는 Google Drive 스트리밍 캐시·임시 파일·Ollama, D:는 Docker 디스크(DB)·DXF 캐시·백업.
- 로그: `D:\AECData\bulk\logs\workers-YYYYMMDD.log`, `census-*.log`, `reembed-*.log`, `wsl-reclaim.log`.
  디스크 가드가 걸리면 워커 로그에 `waiting for disk ... < N GB` 가 찍힙니다.
- 현황:
  ```powershell
  docker exec aec-db psql -U aec -d aec -c "select state, count(*) from aec.jobs group by 1"
  # aec.objects에는 embedding_state 컬럼이 없습니다. 인증된 GET /v1/stats의 embeddings_pending 사용.
  . .\scripts\ops\_common.ps1 # .env와 배포본 Python 환경 로드(비밀값 출력 없음)
  Invoke-AecCli -Arguments @('report', '--out', 'D:\AECData\reports\report')
  Invoke-AecCli -Arguments @('storage-report')  # 테이블별 바이트, 문서당 바이트
  ```
- 실패 작업은 `aec.jobs.error`에 이유가 남습니다. 재시도: API `POST /v1/jobs/{id}/retry`.
- 저장 구조: 같은 텍스트는 벡터 하나(`aec.text_vectors`, halfvec)만 저장하고 객체는 매핑(`aec.embeddings`)만 가집니다.
  매핑이 사라진 벡터 정리: `python -m aec_intelligence.operational.cli vectors-gc`는 삭제 대상 미리보기입니다. 확인 후 `vectors-gc --yes`로 1시간 이상 된 미참조 벡터를 실제 삭제합니다.

자세히: [PIPELINE.ko.md](PIPELINE.ko.md), [operations-phase2.md](operations-phase2.md)

## 8. Graph RAG 사용 (API / MCP / CLI)

- **API**
  ```powershell
  $h = @{ Authorization = "Bearer $env:POWERCAD_ONTOLOGY_TOKEN" }
  $body = @{ question = '<프로젝트명> 2층 창호 개수는?'; top_k = 8 } | ConvertTo-Json
  Invoke-RestMethod -Method Post http://127.0.0.1:58000/v1/ask -Headers $h `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
  ```
  `generate=$false`면 LLM 없이 검색+발췌. 응답에는 항상 `citations[]`(문서·시트·bbox)가 있고, 근거가 없으면 `refused=true`.
- **MCP**: Ontology 게이트웨이 `aec.graph_rag_query`, `aec.explain_path`; power-cad-mcp `ontology_ask` → `ontology_locate`로 AutoCAD에서 위치 확인.
- **CLI**: `powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 ask -Question '2층 실 목록'` / `refresh` / `stats` / `eval`.
- LLM은 **로컬 Ollama만** 사용합니다(원격 URL은 거부, 프록시 무시). 클라우드 LLM은 사용자가 명시적으로 허용할 때만.

자세히: [GRAPHRAG.ko.md](GRAPHRAG.ko.md), [mcp-gateway.md](mcp-gateway.md)

### 지연 점검
```powershell
# 비공개 평가셋(도면 질문)은 git 밖에 둡니다. 결과: D:\AECData\eval\latency\<시각>\run.log
powershell -ExecutionPolicy Bypass -File scripts\ops\latency-check.ps1 -EvalSet D:\AECData\eval\graphrag-ko-50.jsonl
```
기준(2026-10-04, 512 문서, 유휴 시): 검색 p50 약 2 s, Graph RAG(LLM 없음) p50 0.1 s 미만·p95 2 s 미만, LLM 답변 5–15 s.
재임베딩·적재가 동시에 돌면 USB 디스크 WAL 경합으로 몇 배 느려집니다.

### 대화형 우선순위 · 빠른 거절 · 영어 질의 (환경 변수, 기본값)
- **배경 쓰기 양보**: API가 질의(`/v1/search`, `/v1/ask`, `/v1/kg…` 등; `/v1/stats`·`/v1/jobs` 제외)를 처리하면
  `aec.interactive_activity`(UNLOGGED)에 시각을 남기고, reembed와 워커의 임베딩 단계는 청크마다
  최근 `AEC_INTERACTIVE_YIELD_SECONDS`(15)초 안에 질의가 있었거나 지금 API 쿼리가 실행 중이면 멈춥니다.
  한 번에 최대 `AEC_INTERACTIVE_MAX_WAIT_SECONDS`(120)초(워커는 30초)까지만 기다립니다. 0이면 끔.
- **reembed 야간 창**: `AEC_REEMBED_HOURS=22-7`(또는 `reembed.ps1 -Hours 22-7`)이면 그 시간대에만 돌고, 창을 벗어나면
  현재 청크까지 커밋하고 정상 종료(코드 0)합니다. 활성화된 `AEC-Reembed`가 다음 창에서 이어 갑니다(감사 시 Disabled). 기본은 항상.
- **빠른 거절**: Graph RAG의 객체 의미 검색 단계는 `AEC_ASK_SEMANTIC_TIMEOUT_MS`(8000) 안에서만 돕니다. 공사비·연락처·수상처럼
  도면 그래프가 모델링하지 않는 속성을 묻고 지식그래프에도 근거 문자열이 없으면 `AEC_ASK_GATE_TIMEOUT_MS`(1200)로 줄여
  근거가 없으면 바로 거절합니다. 예산을 넘긴 검색은 결과 없음 + 경고로 끝나며 오류가 되지 않습니다.
- **영어 질의 확장**: `AEC_QUERY_EXPANSION=glossary`(기본) — 일반 AEC 용어 사전(공개 용어만)으로 영어 질의를 한국어 검색어로
  바꿉니다("gas piping on the second floor" → "가스 배관 2층", 층 필터도 적용). `llm`이면 사전에 없는 단어를 로컬 LLM이 번역
  (`AEC_QUERY_TRANSLATION_CACHE=D:\AECData\cache\query-translations.json` 캐시), `off`면 끔.

### 성능 측정 CLI (예전 D:\AECData\dev 임시 스크립트)
```powershell
# 검색 골든셋(비공개, git 밖): [{"q": "...", "expect": "<document_id 일부>", "set": "ko|en"}, ...]
. .\scripts\ops\_common.ps1
Invoke-AecCli -Arguments @('search-eval', '--cases', 'D:\AECData\eval\search-cases.json', '--out', 'D:\AECData\eval', '--tag', 'now')
Invoke-AecCli -Arguments @('search-eval', '--cases', '<같은 cases 경로>', '--set', 'en', '--expansion', 'off') # 확장 전후 비교
# 적재 상태와 로그 기반 소요 시간. 처리량의 측정 근거는 아래 주의사항 확인.
Invoke-AecCli -Arguments @('ingest-stats', '--hours', '6', '--log', 'D:\AECData\bulk\logs\workers-<날짜>.log')
Invoke-AecCli -Arguments @('storage-report')   # 테이블/인덱스 바이트, 문서당 바이트
```

호스트 CLI는 §7의 `_common.ps1`을 먼저 로드하고 `Get-AecPython`이 선택한 환경에서 실행합니다. 새 `ingest-stats`는 `aec.metrics`의 `kind='ingestion_completed'` 이벤트와 `created_at`으로 기간별 적재 완료 건수·처리율을 계산합니다(`throughput_source` 참조). 이 이벤트는 DB 적재 커밋을 뜻하며 작업 최종 성공·실패 이력이 아닙니다. 기간별 실패 원장이 없으므로 `window_failure_count`는 `null`이고, `failure_rate`는 현재 큐의 완료 상태에서 계산한 비율입니다. 새 census는 기존 별칭 갱신 때 작업 완료 시각을 덮어쓰지 않지만, 과거 `jobs.updated_at`은 이미 오염되어 있으므로 역사적 처리율·ETA를 복원하는 근거로 사용하지 않습니다. 완료 로그를 함께 확인하고 측정 구간과 분모를 기록합니다.

## 9. 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| API 401 | 토큰 불일치. `.env`의 `AEC_API_TOKEN`과 클라이언트 `POWERCAD_ONTOLOGY_TOKEN` 확인, 교체 후 MCP 클라이언트 재시작 |
| 적재 작업이 몇 분씩 걸림, 로그에 `embeddings pending ... timed out` | Ollama 로드 실패. `ollama ps`, serve.log의 `watchdog`/`Load failed`/`Vulkan` 확인 → §4 재시작. 대기 벡터는 AEC-Reembed가 활성화되어 있어야 자동으로 채움(§3) |
| 적재가 느리고 `pg_stat_activity`에 `DataFileRead` 대기 | USB D:에서 캐시 미스. 무거운 작업(복원 훈련, 대량 평가) 동시 실행 피하기, AEC-WSL-Reclaim 임계값 확인 |
| 워커가 아무것도 안 함 | `D:\AECData\bulk\STOP-WORKERS` 남아 있음 → `stop-workers.ps1 -Resume`; 또는 디스크·메모리 가드(§7). 작업이 Running이고 로그가 `paused`이면 가용 RAM 2048 MB 하한과 300초 재확인 주기 확인 |
| `could not resize shared memory segment` | 컨테이너 `/dev/shm` (이제 compose `shm_size: 256m`). 병렬 인덱스 빌드 끄기(`max_parallel_maintenance_workers=0`, `0003_text_vectors.sql` 마이그레이션 및 `operational/backup.py`의 pg_restore 옵션에 반영됨) |
| 호스트 연결마다 10초 지연 | DSN `localhost` → `127.0.0.1` |
| PC 메모리 부족(가용 < 0.5 GB) | qwen3는 10분 유휴 후 내려감. 사용자 앱(브라우저 등) 정리, `workers` 1로 |
| Docker 재시작 후 일부 컨테이너 없음 | restart 정책 없는 사용자 컨테이너를 `docker start` |

## 10. 방화벽 (관리자 필요, 사용자가 직접)

AEC 포트(55432, 58000, 11434)는 모두 `127.0.0.1`에만 바인드되어 방화벽 규칙이 필요 없습니다.
다른 사용자 컨테이너가 `0.0.0.0`에 공개한 18080/22217 포트를 막는 스크립트가 준비되어 있습니다(에이전트는 UAC를 우회하지 않음):
```powershell
# 관리자 PowerShell에서
powershell -ExecutionPolicy Bypass -File C:\CODE\_ops\grokbot-firewall.ps1
# 되돌리기: Remove-NetFirewallRule -Name GrokBot-block-18080, GrokBot-block-22217
```

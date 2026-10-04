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
로그온하면 예약 작업이 알아서 올라옵니다(§3). Docker Desktop만 켜져 있으면 됩니다.
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
powershell -ExecutionPolicy Bypass -File scripts\ops\stop-workers.ps1 -Resume                # 워커+재임베딩 재개
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

## 3. 예약 작업 (`\AEC\`, 관리자 권한 불필요, 로그온 중에만 실행)

| 작업 | 내용 | 트리거 | 등록 스크립트 |
|---|---|---|---|
| AEC-Bulk-Workers | `bulk-run.ps1 -Role workers` (적재 워커 루프, 개수는 `sources.json`의 `workers`) | 로그온 + 10분마다 감시 | `register-bulk-tasks.ps1` |
| AEC-Bulk-Census | census 재개(끝난 소스는 건너뜀) | 로그온 | 〃 |
| AEC-Bulk-Census-Refresh | 새/변경 도면 탐색 | 매일 03:00 | 〃 |
| AEC-Ollama | `C:\AECLocal\Ollama\ollama.exe serve` (현재 등록본은 같은 환경변수를 넣은 `cmd /c`; `register-host-tasks.ps1 -Only Ollama`로 다시 등록하면 `ollama-serve.ps1`+로그 회전) | 로그온 + 5분마다 감시 | `register-host-tasks.ps1` |
| AEC-Reembed | `reembed.ps1` (대기 중 벡터 채우기, 청크 단위 커밋) | 로그온 + 30분마다 | 〃 |
| AEC-WSL-Reclaim | `wsl-reclaim.ps1` (Windows 가용 RAM < 750 MB일 때만 VM 페이지 캐시 비움) | 30분마다 | 〃 |
| `\AEC-DB-Backup` | `backup.ps1 -Keep 7 -Target D:\AECData\backups` | 매일 04:30 | `register-backup-task.ps1` |
| (선택) AEC-GraphRAG-Refresh | KG 재구성 + 커뮤니티 요약 | N시간마다 | `register-graphrag-task.ps1` |

- 모든 작업은 `MultipleInstances=IgnoreNew` + 스크립트 내부 뮤텍스 → 반복 트리거가 와도 중복 실행되지 않습니다.
- 감시 트리거가 있으므로 프로세스가 조용히 죽어도 5–30분 안에 다시 뜹니다. 긴 작업은 **절대 임시 셸에서 띄우지 말고** 작업 스케줄러로 실행합니다.
- 상태: `Get-ScheduledTask -TaskPath \AEC\ | Get-ScheduledTaskInfo | ft TaskName,LastRunTime,LastTaskResult`
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

## 6. 백업 / 복원 / 복원 훈련

```powershell
# 수동 백업 (pg_dump custom, 컨테이너 안에서 실행; 같은 폴더의 aec-db-*.dump만 회전)
powershell -ExecutionPolicy Bypass -File scripts\ops\backup.ps1 -Target D:\AECData\backups -Keep 7
# 복원 훈련: 스크래치 DB에 복원 → 모든 테이블 행 수·인덱스 비교 → 스크래치 삭제
powershell -ExecutionPolicy Bypass -File scripts\ops\restore-drill.ps1 -Dump D:\AECData\backups\aec-db-<시각>Z.dump
```
- 행 수가 정확히 같으려면 덤프 전에 `stop-workers.ps1 -Drain` (아니면 적재 중인 테이블만 차이).
- 복원 훈련은 DB 크기만큼 VM 공간이 필요하고 USB 디스크에서 40분 이상 걸립니다(1.1 GB 덤프 기준).
- 실제 복원(재해 복구)은 API·워커를 멈춘 뒤 `cli restore` — [database-backup-restore.md](database-backup-restore.md),
  Drive 체크포인트는 [DRIVE-CHECKPOINT.ko.md](DRIVE-CHECKPOINT.ko.md).

## 7. 대량 적재 모니터링과 디스크 가드

- 설정: `D:\AECData\bulk\sources.json` (비공개; 원본 폴더 목록). 수정 전 `.bak-<날짜>`로 백업.
  - `"workers"`: 워커 수(라운드마다 다시 읽음). RAM 15 GB PC에서는 1–2.
  - `"min_free_gb": {"C:\\": 15, "D:\\": 60}` (2026-10-04 설정값): 드라이브 여유 공간이 이보다 적으면 census·워커가 **일시정지**(5분마다 재확인).
    C:는 Google Drive 스트리밍 캐시·임시 파일·Ollama, D:는 Docker 디스크(DB)·DXF 캐시·백업.
- 로그: `D:\AECData\bulk\logs\workers-YYYYMMDD.log`, `census-*.log`, `reembed-*.log`, `wsl-reclaim.log`.
  디스크 가드가 걸리면 워커 로그에 `waiting for disk ... < N GB` 가 찍힙니다.
- 현황:
  ```powershell
  docker exec aec-db psql -U aec -d aec -c "select state, count(*) from aec.jobs group by 1"
  docker exec aec-db psql -U aec -d aec -c "select embedding_state, count(*) from aec.objects group by 1"
  python -m aec_intelligence.operational.cli report        # 또는 GET /v1/stats (토큰 필요)
  python -m aec_intelligence.operational.cli storage-report  # 테이블별 바이트, 문서당 바이트
  ```
- 실패 작업은 `aec.jobs.error`에 이유가 남습니다. 재시도: API `POST /v1/jobs/{id}/retry`.
- 저장 구조: 같은 텍스트는 벡터 하나(`aec.text_vectors`, halfvec)만 저장하고 객체는 매핑(`aec.embeddings`)만 가집니다.
  매핑이 사라진 벡터 정리: `cli vectors-gc` (1시간 이상 된 미참조 벡터만 삭제).

자세히: [PIPELINE.ko.md](PIPELINE.ko.md), [operations-phase2.md](operations-phase2.md)

## 8. Graph RAG 사용 (API / MCP / CLI)

- **API**
  ```powershell
  $h = @{ Authorization = "Bearer $env:POWERCAD_ONTOLOGY_TOKEN" }
  $body = @{ question = '주례동 315-4 2층 창호 개수는?'; top_k = 8 } | ConvertTo-Json
  Invoke-RestMethod -Method Post http://127.0.0.1:58000/v1/ask -Headers $h `
    -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
  ```
  `generate=$false`면 LLM 없이 검색+발췌. 응답에는 항상 `citations[]`(문서·시트·bbox)가 있고, 근거가 없으면 `refused=true`.
- **MCP**: Ontology 게이트웨이 `aec.graph_rag_query`, `aec.explain_path`; power-cad-mcp `ontology_ask` → `ontology_locate`로 AutoCAD에서 위치 확인.
- **CLI**: `powershell -ExecutionPolicy Bypass -File scripts\ops\graphrag.ps1 ask -Question '2층 실 목록'` / `refresh` / `stats` / `eval`.
- LLM은 **로컬 Ollama만** 사용합니다(원격 URL은 거부, 프록시 무시). 클라우드 LLM은 사용자가 명시적으로 허용할 때만.

자세히: [GRAPHRAG.ko.md](GRAPHRAG.ko.md), [mcp-gateway.md](mcp-gateway.md)

## 9. 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| API 401 | 토큰 불일치. `.env`의 `AEC_API_TOKEN`과 클라이언트 `POWERCAD_ONTOLOGY_TOKEN` 확인, 교체 후 MCP 클라이언트 재시작 |
| 적재 작업이 몇 분씩 걸림, 로그에 `embeddings pending ... timed out` | Ollama 로드 실패. `ollama ps`, serve.log의 `watchdog`/`Load failed`/`Vulkan` 확인 → §4 재시작. 대기 벡터는 AEC-Reembed가 채움 |
| 적재가 느리고 `pg_stat_activity`에 `DataFileRead` 대기 | USB D:에서 캐시 미스. 무거운 작업(복원 훈련, 대량 평가) 동시 실행 피하기, AEC-WSL-Reclaim 임계값 확인 |
| 워커가 아무것도 안 함 | `D:\AECData\bulk\STOP-WORKERS` 남아 있음 → `stop-workers.ps1 -Resume`; 또는 디스크 가드(§7) |
| `could not resize shared memory segment` | 컨테이너 `/dev/shm` 64 MB. 병렬 인덱스 빌드 끄기(`max_parallel_maintenance_workers=0`, 스크립트에 반영됨) |
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

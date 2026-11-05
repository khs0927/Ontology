# 중단 작업 완료·업그레이드·실구동 검증 보고 (2026-11-05)

클라우드 세션에서 중단된 작업을 전수 조사하고, 남은 코드 작업을 통합·업그레이드한 뒤
실제 데이터베이스와 실제 프로세스로 구동 테스트를 완료한 기록이다.

## 1. 중단 작업 조사 결과

원격 브랜치 97개와 열린 PR 10건을 `git merge-tree`로 master(`d81598f`)에 병합해 보고 분류했다.

| 대상 | 상태 | 조치 |
|---|---|---|
| PR #101 `fix/archioffice-relink-after-prune` | 미병합, 충돌 없음 | **이 브랜치에 통합** (prune → relink 순서, `--sql-only`는 벡터 코퍼스를 읽지 않음) |
| 초안 PR #80, #83, #84, #86, #87, #88, #89, #90, #91 | 내용이 #100 통합 커밋에 이미 포함 | 코드 작업 없음. 초안 PR은 닫아도 됨 (사용자 결정) |
| `codex/headless-secondary-census-contract`, `codex/portable-source-handoff-20261004` | 남은 것은 단독 CI 워크플로 파일뿐 | 해당 테스트(`tests/test_secondary_census.py`, `extensions/drawing_context/tests/test_source_*.py`)는 이미 기존 워크플로에서 실행됨 → 별도 파일 불필요 |
| `codex/ci-reuse-audit-pilot`, `codex/headless-dependency-snapshot` | 2026-10-04 시점 스냅샷 문서·파일럿 | 당시 18개 워크플로 기준이라 현재(4개)와 맞지 않음 → 통합하지 않음 |
| 나머지 브랜치 (충돌 표시) | 스쿼시 병합으로 이미 master에 반영된 과거 브랜치 | 조치 없음 |
| `docs/status/knowledge-pack-remaining-20261006.ko.md` ③ PR 생성·병합 | #92로 병합 완료 | 완료 |

## 2. 업그레이드

- **Python 3.13 지원**: 3.13 가상환경에서 전체 테스트(DB 포함) 통과 → CI를 3.12/3.13 매트릭스로, 앱 이미지를 `python:3.13-slim`으로.
- **CI 현대화**: `actions/checkout@v5`, `actions/setup-python@v6`, pip 캐시. ruff·compileall 범위에 `scripts/`, `library/` 추가.
  외부 기능 계약 테스트를 메인 워크플로에 포함. 오래된 기능 브랜치 push 트리거 정리.
- **DB 테스트 전체 실행**: 기존 CI는 운영 테스트 일부만 Postgres로 실행했다. 이제 전체 `tests/`를 Postgres로 실행하고,
  `AEC_TEST_DATABASE_URL`이 있는데도 DB 테스트가 건너뛰어지면 실패한다.
- **pgvector 버전 가드**: 마이그레이션 0003은 `halfvec`(pgvector 0.7+)을 쓴다. Ubuntu 24.04 기본 패키지(0.6.0)에서는
  마이그레이션 도중 `type "halfvec" does not exist`로 깨졌다. 이제 `init-db`가 적용 전에 확인하고 해결 방법을 알려준다.
- **Starlette 1.x**: TestClient의 httpx 지원 폐기 예고 경고 → dev extra에 `httpx2` 추가 (경고 0건).
- **정적 검사**: `library/*/tools`의 미사용 import·변수 10건 정리.

## 3. 오픈소스 결합과 실구동 테스트

실제 PostgreSQL 16 + Apache AGE 1.6.0 + pgvector 0.8.1 + PostGIS 3.4 + pg_trgm 위에서 실행했다.

- `scripts/e2e/e2e_smoke.py` (신규): 제품 코드를 모의하지 않고 실제 프로세스로 전 구간을 구동한다.
  프로토타입 CLI 12단계 → MCP stdio → `init-db`(2회, 멱등) → 적재 큐 → 워커 → 검색 → `kg-build` →
  ArchiOffice 팩 전체 적재 2회(객체 2,860·텍스트 벡터 2,860, 재적재 멱등) → Graph RAG 인용 답변·근거 없는 질문 거절 →
  REST API(토큰 인증, 가져오기 루트 차단, 검색, 질의, 대시보드) → MCP HTTP(프로토콜 헤더, graph_rag_query).
- `scripts/e2e/ollama_stub.py` (신규): 모델 다운로드가 불가능한 환경(CI, 샌드박스)을 위한 결정적 Ollama 호환 스텁.
  **의미 있는 임베딩이 아니므로** 배선 검증용이다. 루프백에만 바인딩한다.
- CI의 `operational-postgres` 잡이 DB 테스트 다음에 이 스모크를 실행하고 `e2e-report.json`을 업로드한다.

### 결과

| 검증 | 결과 |
|---|---|
| 전체 테스트, Python 3.12, 실제 Postgres | 812 통과 / 0 실패 / 경고 0 (16 건너뜀: FreeCAD·Blender·QGIS·ODA·LibreDWG·PowerShell 미설치) |
| 전체 테스트, Python 3.13, 실제 Postgres | 809 통과 / 0 실패 (`-W error::DeprecationWarning`, 리뷰 수정 전 측정) |
| 확장 계약 테스트 (drawing_context / external_capabilities / intelligence_fabric) | 135 / 97 / 13 통과 |
| E2E 스모크 | 43 / 43 통과, 약 64초 (같은 DB 재실행은 거부됨) |
| ruff (src tests extensions scripts library) | 통과 |

## 4. 남은 일 — 이 클라우드 환경에서 할 수 없는 것

| 항목 | 이유 | 다음 행동 |
|---|---|---|
| 1층 평면도 재작성(AutoCAD) | Windows의 AutoCAD 세션 필요 | `knowledge-pack-remaining-20261006.ko.md` ① 절차 그대로 |
| Windows Docker Desktop의 `aec-db` 재기동 후 `kg-knowledge-load` | 사용자 PC의 Docker Desktop | 같은 문서 ②. 적재 경로 자체는 이번 E2E로 검증됨 |
| `drafting_kb.json` 적재 | 파일이 power-cad-mcp 저장소(이 세션 범위 밖)에 있음 | PC에서 ②의 명령 실행 |
| bge-m3·qwen3 실제 품질 평가 | 이 환경은 Hugging Face·Ollama 레지스트리 접근 불가 | PC에서 `search-eval`, `graphrag-eval` 실행 |
| `PROGRESS.md` 대량 수집 게이트(24시간 DB 안정성 등) | Windows 호스트·D:\AECData 대상 장기 관측 | PC에서 `scripts\ops\observe-bulk.ps1` |
| 이번 환경의 `docker build` 확인 | 컨테이너 빌드에서 deb.debian.org 접근 차단 | GitHub Actions `app-image`·`operational-postgres` 잡이 확인 |

## 5. 독립 리뷰 (서브에이전트 6개: 리뷰 4 + 반박 검증 2)

| 심각도 | 지적 | 조치 |
|---|---|---|
| 높음 | CI `pytest \| tee` 단계가 pipefail 없이 실행돼 테스트 실패가 숨겨짐 | `shell: bash`(= `-eo pipefail`) 지정 |
| 중간 | E2E의 워커 검사가 실패한 작업도 통과로 셈 | 작업 ID별 상태가 모두 `SUCCEEDED`인지 DB에서 확인, IFC 프로젝트 검색 추가 |
| 낮음 | 같은 DB 재실행 시 중복 제거로 결과가 바뀜 | 기존 작업이 있는 DB는 거부, API 적재는 별도 픽스처 사용 |
| 낮음 | key/value DSN에서 DB 이름 검사를 우회 가능 | `psycopg.conninfo`로 실제 dbname 확인 |
| 낮음 | 서버 stderr 파이프를 읽지 않음 | 작업 폴더의 로그 파일로 기록, 기동 실패 시 꼬리 출력 |

## 6. GitHub CI에서 드러난 문제와 수정

| 문제 | 원인 | 수정 |
|---|---|---|
| 백업·복원 테스트 실패 (`unrecognized configuration parameter "transaction_timeout"`) | 러너의 PostgreSQL 클라이언트가 17 이상으로 올라가 PG16 서버에 모르는 설정을 보냄. 클라이언트가 서버보다 새 PC에서도 같은 일이 생김 | `backup.py`가 서버 주 버전과 같은 `/usr/lib/postgresql/<버전>/bin` 도구를 먼저 사용 (단위 테스트 추가) |
| `postgresql-client-16` 설치 실패 | `ubuntu-latest`가 Ubuntu 26.04로 바뀌어 해당 패키지가 없음 | PostgreSQL 16 잡을 `ubuntu-24.04`에 고정, `upload-artifact@v5` |

최종 CI(`ca47d39`): 8개 검사 모두 통과. 여기에 DB 전체 테스트와 E2E 스모크가 포함된다.

## 7. Local Windows run (2026-11-05)

사용자 PC(Windows 10, 시스템 로캘 cp949, Python 3.13, PowerShell 5.1, ODA File Converter 27.1.0, FreeCAD 1.1)에서 실행했다. 클라우드가 건너뛴 도구 의존 테스트가 대상이다.

| 명령 | 결과 |
|---|---|
| `python -m pytest tests` (`PYTHONUTF8` 없음, DB 없음) | 792 통과 / 0 실패 / 38 건너뜀 |
| ODA 실변환: `test_dwg.py`, `test_cross_format_acceptance.py` | 통과. `simple_house.dwg`가 정답 값과 일치 |
| PowerShell: `test_restore_drill_ops.py` (5.1) | 통과 |
| FreeCAD: `test_native_ifc_probe_*` 2건 | 통과 |
| 실제 ODA로 한글·공백 경로 변환 (`select_dwg_converter("auto", ...)`) | SUCCESS, 엔티티 149개 |
| `scripts/` 아래 `.ps1` 19개를 PowerShell 5.1 파서로 검사 | 구문 오류 0 |
| 확장 계약 테스트 (external_capabilities / drawing_context / intelligence_fabric) | 97 / 135 / 13 통과 |
| ruff (src tests extensions scripts library) | 통과 |

이번 실행에서 찾아 고친 문제:

| 문제 | 원인 | 수정 |
|---|---|---|
| MCP stdio 서버가 한국어 Windows에서 깨짐 (실제 제품 버그) | 파이프 stdio가 로캘을 따라 cp949로 동작. 클라이언트에 cp949 바이트를 보내고, cp949에 없는 문자에서는 서버가 멈출 수 있음. GitHub Windows 러너는 영어 로캘이라 드러나지 않음 | `aec-mcp` 시작 시 stdin/stdout을 UTF-8로 고정, 회귀 테스트 추가 (수정 없이는 실패 확인) |
| `test_oda_converter_tolerates_undecodable_console_output` 실패 | 가짜 ODA를 `.sh`로 만들어 Windows에서 실행 불가 | Windows에서는 `.cmd` 래퍼 사용 |

남은 일:

| 항목 | 필요한 것 |
|---|---|
| 건너뛴 DB 테스트 33건, `pg_dump` 백업·복원 테스트 1건, E2E 스모크의 Windows 실행 | Docker Desktop의 `aec-db` 기동 또는 로컬 PostgreSQL (이번 실행 때 Docker 엔진 꺼짐). Linux에서는 GitHub CI가 검증 |
| FreeCAD+Blender 연계 테스트 1건 | `blender.exe`. `runtime\external\blender-5.2.0` 폴더에 실행 파일이 없음 |
| QGIS 1건, LibreDWG 1건 | 이 PC에 미설치 |
| 심볼릭 링크 테스트 1건 | Windows 심볼릭 링크 권한 |
| bge-m3·qwen3 실제 답변 품질 | Ollama 미설치·미실행 |
| 4절의 나머지 항목(AutoCAD 재작성, `drafting_kb.json` 적재, 대량 수집 게이트) | 이번 실행 범위 밖 |

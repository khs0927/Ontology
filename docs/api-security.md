# Operational API 보안 설정 / 레거시 document_id 정리

## 1. Bearer 토큰 인증 (선택, `AEC_API_TOKEN`)

| `AEC_API_TOKEN` | 동작 |
|---|---|
| 미설정·공백 | 인증 없음(기존 동작). API는 loopback(`127.0.0.1`)에만 노출하세요 — `aec serve`, docker-compose 기본값 |
| 설정됨 | `/healthz`, 대시보드 화면(`/`, `/dashboard`, `/static/*`)을 제외한 모든 요청에 `Authorization: Bearer <token>` 필요. 없거나 틀리면 `401` + `WWW-Authenticate: Bearer` |

- 비교는 `hmac.compare_digest`(상수 시간)로 합니다. 환경 변수 값의 앞뒤 공백은 제거됩니다.
- CORS preflight(`OPTIONS`)는 인증 없이 CORS 미들웨어가 처리합니다(`AEC_CORS_ORIGINS` 허용 목록 필요).
- 대시보드는 첫 `401` 때 토큰을 물어보고 현재 탭(`sessionStorage`)에만 저장합니다.
- 구현: `src/aec_intelligence/operational/auth.py`(순수 ASGI 미들웨어), `create_app`에서 연결. 테스트: `tests/test_operational_api_auth.py`.

### power-cad-mcp 연동

power-cad-mcp는 Python(`ontology_*` 도구)과 C#(`OntologyRestTools`) 모두 `POWERCAD_ONTOLOGY_TOKEN`을
`Authorization: Bearer <token>`으로 보냅니다. **`POWERCAD_ONTOLOGY_TOKEN` = `AEC_API_TOKEN`** 으로 같은 값을 넣으세요.

```powershell
$env:AEC_API_TOKEN = "<긴 임의 문자열>"; aec serve            # Ontology
$env:POWERCAD_ONTOLOGY_TOKEN = "<같은 문자열>"                  # power-cad-mcp
```

docker-compose는 `.env`의 `AEC_API_TOKEN`, `AEC_CORS_ORIGINS`를 api 컨테이너에 전달합니다.

### 토큰 정책 (2026-10 결정)

- **설치 시 생성, `.env`에 저장**: `scripts/ops/init-env.ps1`이 `.env`가 없으면 `.env.example`에서 만들고
  `AEC_API_TOKEN`(32바이트 URL-safe 난수)과 `AEC_DB_PASSWORD`를 생성합니다. 이미 있는 값은 바꾸지 않습니다
  (DB 비밀번호를 바꾸면 초기화된 Postgres 볼륨에 접속할 수 없음). `.env`는 gitignore 대상이며 값은 출력하지 않습니다.
- **power-cad-mcp에 같은 값 전달**: `-SetUserEnv`를 주면 `POWERCAD_ONTOLOGY_TOKEN`을 Windows 사용자 환경 변수로 저장합니다. power-cad-mcp는 `.env`를 읽지 않고 MCP 클라이언트가 넘겨주는 환경을 쓰므로, 클라이언트를 재시작하거나 클라이언트 MCP 설정의 `env`에 같은 값을 넣으세요.
- **교체**: `-RotateToken`으로 새 토큰을 만들고 Ontology API와 power-cad-mcp를 재시작합니다.
- 코드 기본값(미설정 = 인증 없음)은 기존 배포 호환을 위해 유지하지만, 새 설치는 위 스크립트로 항상 토큰을 켭니다.
- Linux/macOS: `python -c "import secrets; print(secrets.token_urlsafe(32))"` 값을 `.env`의 `AEC_API_TOKEN=`에 넣습니다.

## 2. 그 밖의 경계

- `POST /v1/ingestions`는 `AEC_IMPORT_ROOTS` 안의 경로만 받습니다(밖이면 403, 없으면 400, 루트 밖을 가리키는 심볼릭 링크는 건너뜀).
- `AEC_CORS_ORIGINS`: 쉼표로 구분한 허용 origin 목록. `*`는 무시됩니다. 기본값은 CORS 비활성(같은 origin 대시보드·서버 간 호출에는 필요 없음).

## 3. 레거시 `doc_<파일명>` 문서 정리 (마이그레이션 메모)

수정 전 REST/CLI 임시 수집은 `document_id`가 없어 worker가 `doc_<stem>`을 사용했습니다. 이름이 같은 다른 파일
(`A/평면도.dwg`, `B/평면도.dwg`)이 같은 문서로 투영되어 **나중 파일이 앞 파일의 객체·관계를 덮어썼고**,
`aec.documents.source_key`는 처음 파일 경로로 남았습니다. 새 수집은 내용 기반 id(`doc_` + sha256 24자리)를 씁니다.

이미 저장된 데이터는 다음 도구로 확인·재수집합니다(DB를 직접 수정·삭제하지 않음).

```bash
# 1) 보고만(기본, dry-run): 스냅샷 파일(<AEC_DATA_ROOT>/snapshots/doc_*/rev-N.json)을 읽어 충돌 여부 표시
python -m aec_intelligence.operational.legacy_ids --json legacy-report.json
# 2) 재수집 작업만 큐에 넣기(원래 project_id 유지, AEC_IMPORT_ROOTS 안의 존재하는 파일만)
python -m aec_intelligence.operational.legacy_ids --apply
# 3) 새 문서 색인 완료 후, 출력된 cleanup SQL(BEGIN … 확인 후 COMMIT/ROLLBACK)을 검토해 직접 실행
```

- `COLLIDED`: 서로 다른 `source_key`가 한 문서에 섞인 경우 → 반드시 재수집 필요.
- `legacy`: 파일 하나뿐 → 재수집하면 내용 기반 id로 옮겨짐(선택).
- 원본 파일이 없거나 import root 밖이면 `skip`으로 표시되며 재수집되지 않습니다.

## 보안 점검 결과 (Phase 5, 2026-10-04)

| 항목 | 결과 / 조치 |
|---|---|
| 컨테이너가 저장소를 마운트 | compose가 `.:/app`을 api/migrate/worker에 쓰기 가능하게 마운트하고 있었습니다. 그래서 컨테이너 안에서 호스트 `.env`(토큰·DB 비밀번호)를 읽을 수 있었고, 작업 스케줄러가 실행하는 호스트 스크립트를 고칠 수도 있었습니다. **마운트를 제거**했습니다. 코드는 이미지에 들어가므로 배포할 때 `docker compose build`가 필요합니다 |
| 이미지에 비밀 포함 | `.dockerignore`가 없어서 `COPY . .`가 `.env`·`.venv`·`.git`까지 복사했습니다. `.dockerignore`를 추가했고, CI가 미끼 `.env`로 이미지에 빠지는지 검사합니다 |
| 컨테이너 root 실행 | 비특권 사용자 `aec`(uid 10001)로 바꿨습니다. 코드는 root 소유(읽기 전용)이고, 이미지에서 pytest를 뺐습니다 |
| 임베딩 엔드포인트 외부 전송 | LLM과 같은 규칙을 적용했습니다. 루프백·사설·링크로컬이 아닌 `AEC_EMBEDDING_URL`은 거부합니다(`AEC_EMBEDDING_ALLOW_REMOTE=1`로만 허용). 로컬 호출은 환경 변수나 레지스트리의 HTTP 프록시를 거치지 않습니다(`operational/netguard.py`). URL 안의 사용자 정보(`127.0.0.1@외부`)나 `file:` 스킴도 거부합니다 |
| WebSocket | 현재 라우트는 없습니다. 나중에 생기더라도 같은 토큰이 필요합니다(없으면 1008로 닫음) |
| 입력 크기 | `/v1/search` `query`는 최대 2,000자입니다(`/v1/ask`와 같음) |
| SQL / Cypher 주입 | 확인했습니다. 사용자 값은 모두 psycopg 파라미터로 넘깁니다. Cypher 값은 JSON 문자열 리터럴로 넣고, 쿼리는 `$aec_cypher$` 고정 태그로 감쌉니다. 태그가 들어 있으면 거부합니다. f-string으로 조립하는 SQL 조각은 모두 내부 상수나 정수입니다 |
| 경로 탐색 | `POST /v1/ingestions`는 `resolve(strict=True)` 뒤 `AEC_IMPORT_ROOTS` 안에 있는지 검사합니다. 디렉터리 안의 심볼릭 링크도 하나씩 다시 검사합니다 |
| 도면 문자열 프롬프트 주입 | 답변 프롬프트가 자료를 데이터로만 취급하도록 지시합니다. 목록에 없는 인용 번호는 지우고, 인용이 없는 답은 발췌 답변으로 바꿉니다. LLM에는 도구 권한이 없습니다 |
| 의존성 | `pip-audit`: Ontology 호스트 venv·API 이미지·개발 venv, ArchOntos, power-cad-mcp 모두 알려진 취약점이 없습니다(ArchOntos 개발용 pytest 8.4.2 PYSEC-2026-1845는 하한을 9.0.3으로 올림). `dotnet list package --vulnerable --include-transitive`: hs-steel-cad 6개, power-cad-mcp 4개 프로젝트 모두 없음. npm 프로젝트는 없습니다 |
| 남은 위험 (수용) | MCP 게이트웨이(stdio / 로컬 HTTP)의 CAIR 도구(`aec.parse_cad`, `aec.ingest_file` 등)는 임의 경로를 받습니다. 로컬 MCP 클라이언트를 사용자 자신으로 신뢰하는 전제입니다. HTTP 전송은 localhost 바인드와 Origin 검사만 하고 토큰은 없습니다. 원격으로 노출하지 마세요 |

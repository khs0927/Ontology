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

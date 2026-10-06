# 남은 작업 브리핑 — 건축,구조1 재작성 · 지식 그래프 (2026-10-06)

## 1. 지금까지 완료된 것

| 항목 | 상태 | 위치 |
|---|---|---|
| 건축,구조1.dwg 전체 덤프·의미 분석 (76시트, 9개 도면 종류, 요소 50종) | 완료 | `C:\CODE\power-cad-mcp\docs\analysis\건축구조1\SEMANTIC_ANALYSIS.md` |
| A201 1층 평면도 재작성 + 검증 (유형별 573/573 일치, 코어 확대 비교 일치) | 완료했으나 **DWG 미저장으로 사라짐** | 절차: `docs\playbooks\SHEET_REPLICATION_PLAYBOOK.md` §8 |
| 재작성 데이터셋 보강 (해치 다중 루프, 폴리선 폭, SOLID 점) | 완료 | `docs\standards\datasets\A201_1F.json` |
| 작업 지식 → 지식 그래프 팩 (노드 749, 관계 1,269) + OWL/Turtle | 완료 | `docs\knowledge\drafting_kb.json`, `drafting_kb.ttl` |
| Ontology 적재 기능 `kg-knowledge-load` + Graph RAG `knowledge` 질의 경로 | 완료, 테스트 77건 통과 | Ontology 브랜치 `feat/knowledge-pack` |
| 레포 푸시 | 완료 | power-cad-mcp `feat/full-toolset`, Ontology `feat/knowledge-pack` |
| 재검증 (서브에이전트 3개: 병합·코드·데이터) | 모두 통과 | — |

## 2. 남은 작업 (우선순위 순)

### ① 1층 평면도 재작성본 다시 그리기 — 사용자: 도면 열기 필요
- 원인: `건축,구조1.dwg`가 저장 없이 닫혀 재작성본이 사라짐 (원본 마지막 저장 2026-02-06, 원본은 그대로).
- 할 일: AutoCAD에서 도면을 다시 연 뒤 아래 명령 한 번 → 개수 비교 → 스냅샷 비교.
  ```
  cd C:\CODE\power-cad-mcp
  python scripts\draw_from_dataset.py docs\standards\datasets\A201_1F.json --origin 347227.744 357230
  ```
- 끝나면 **저장 여부를 결정** (저장하지 않으면 다시 사라짐).

### ② 온톨로지 DB 적재 — Docker Desktop 필요
- 원인: Docker Desktop이 "unable to start" 오류로 엔진이 뜨지 않음 (aec-db 컨테이너 접속 불가).
- 할 일: Docker Desktop 정상 기동 확인 → aec-db healthy → 아래 실행.
  ```
  cd C:\CODE\Ontology-main
  powershell -Command ". .\scripts\ops\_common.ps1; Import-AecDotEnv; Invoke-AecCli -Arguments @('kg-knowledge-load','C:\CODE\power-cad-mcp\docs\knowledge\drafting_kb.json')"
  ```
- 확인 질문 예: `ask "평면도 해치는 어떻게 그려?"`, `ask "leader 버그 해결 방법"`

### ③ Ontology PR 생성·병합 — 사용자 결정
- `feat/knowledge-pack` 브랜치만 올라가 있음 (master는 PR 방식으로 관리됨).

### ④ C# 빌드·테스트 — ✅ 완료 (2026-10-06)
- `C:\CODE\All-In-Cad\.dotnet` (SDK 10.0.401)로 실행: 테스트 54/54 통과, AutoCAD 플러그인 빌드 경고 0·오류 0.
- 남은 것: 플러그인을 AutoCAD에 재배포(`bash scripts/build_dotnet.sh` 패키징 후 번들 교체, AutoCAD 재시작).

### (이전 메모) C# 빌드
- 이 PC에는 SDK가 없어 정적 검사만 함 (충돌 병합한 3개 파일: CommandDispatcher.cs, AcadTransaction.cs, ServerTests.cs).
- 실행: `bash scripts/build_dotnet.sh` (빌드 + 테스트 + 패키징). 성공하면 AutoCAD 플러그인 재배포.

### ⑤ 선택 개선
- Ontology 전체 테스트(`.venv` 파이썬) 실행 — 이번 세션에서는 권한 문제로 관련 77건만 실행.
- 다른 층(2층·3~5층…) 평면도도 같은 절차로 데이터셋화 → 지식 팩 재생성(`scripts\export_knowledge_pack.py`) → 재적재.
- 플러그인 도구 보완: leader의 style을 치수 스타일로 해석, cad_copy·set_properties에 그리기 순서 옵션.

## 3. 핵심 교훈 (지식 팩에도 기록됨)
- 재작성한 결과는 **저장하지 않으면 사라진다** — 매번 저장 여부 확인.
- 구 데이터셋은 해치 첫 루프만 있음 → `enrich_dataset.py`로 보강 후 그릴 것.
- 복사한 채움 해치는 맨 위에 놓여 벽선을 덮음 → `cad_create`의 `draw_order: back`으로 재생성.
- leader `style`이 치수 스타일 이름이면 실패 → style 없이 재시도.

<!-- README 스니펫: 아래 '## 생태계 내 역할' 절을 README.md에 그대로 붙여 넣을 수 있습니다. -->

## 생태계 내 역할 (Role in ecosystem)

**지식 — AEC 의미 엔진 원본(상류)**

- CAIR, DXF/IFC/GIS 수집, `aec-cair-gateway` MCP를 담은 원본입니다. 같은 코드가 `Ontology-platform/packages/aec`에 git subtree로 병합되어 있습니다(파일 목록 동일).
- 새 개발은 Ontology-platform에서 하는 것으로 보입니다 **(추정: 마지막 커밋 2026-10-17)**.
- 주는 것: `aec-executor-handoff/1` → power-cad-mcp, All-In-Cad. 받는 것: `sketcharch-drawing-export/1`, `aec-execution-evidence/1`(All-in-memory 기록).

전체 지도: [ECOSYSTEM.md](https://github.com/khs0927/power-cad-mcp/blob/main/docs/ecosystem/ECOSYSTEM.md) · CAD 정리 계획: [CAD_CONSOLIDATION_PLAN.md](https://github.com/khs0927/power-cad-mcp/blob/main/docs/ecosystem/CAD_CONSOLIDATION_PLAN.md) (2026-11-07 기준, 설계안)

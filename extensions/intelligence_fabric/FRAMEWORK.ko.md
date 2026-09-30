# Ontology 업그레이드 프레임워크 — Jev + Graph Intelligence Fabric

검토 기준일: **2026-09-30**

## 1. 결론

이번 업그레이드는 기존 Ontology의 CAIR/원본/Provenance 구조를 교체하지 않는다. 대신 아래 3계층을 추가한다.

1. **Canonical Plane** — 기존 CAIR, JSON/JSONL, 원본 Artifact가 진실의 원천이다.
2. **Decision / Context Plane** — Jev 계열은 대형 코드·스키마·어댑터 중 “무엇을 먼저 읽을지” 좁히는 데 사용한다.
3. **Graph Acceleration Plane** — Apache AGE/PyOxigraph를 로컬 기본으로 유지하고, 대규모 Object Storage/분산 요구가 생길 때 HydraDB를 외부 서비스로 사용한다.

즉, `Jev -> 후보/컨텍스트 선택`, `Graph -> 관계 질의/탐색 가속`, `CAIR -> 최종 근거/정규화`로 책임을 분리한다.

## 2. Jev 도입 방향

### dzhng/jevgrep — 즉시 도입

용도는 코드 생성이 아니라 **대형 저장소 컨텍스트 탐색**이다. 질문을 주면 관련 파일, 읽을 지점, 소스 발췌를 stdout으로 돌려준다. 현재 Power CAD / Ontology / 파서 코드가 커질수록 “어디를 읽어야 하는가” 비용을 줄이는 데 직접적이다.

중요한 경계가 있다. upstream 문서는 검색 가능한 소스가 설정된 provider로 전송될 수 있다고 명시한다. 따라서 본 프레임워크는 사설 저장소에서 `allow_source_egress=False`를 기본값으로 고정했다. 명시 승인 없이는 실행하지 않는다.

Windows에서는 upstream이 현재 macOS/Linux를 요구하므로 WSL 경로를 기본 운영안으로 본다. Windows native 지원이 확인되기 전에는 별도 포크를 만들지 않는다.

### browser-use/jev-ultrafast — 설계 패턴 채택

브라우저 전용 코드를 Ontology에 직접 넣지는 않는다. 대신 “동적으로 관찰된 후보 중 허용된 동작/대상만 선택하고, 자유 생성은 최소화한다”는 구조를 Ontology의 향후 `candidate -> choose -> verify -> execute` 경계에 적용한다.

특히 CAD 수정은 Jev가 직접 명령을 생성하게 하지 않고, 기존 drawing_context의 live verification 이후 허용된 native operation ID만 고르는 방향이 맞다.

## 3. Graph 도입 방향

### Apache AGE — 로컬 기본 유지

현재 저장소 docker-compose의 PostgreSQL/AGE 구조와 잘 맞는다. 소규모/중간 규모 프로젝트, 로컬 개발, SQL과 Graph를 한 DB에서 쓰는 구간은 AGE를 기본으로 유지한다.

### PyOxigraph — RDF/SPARQL 전용

BOT/GeoSPARQL/PROV-O/bSDD 같은 RDF alignment를 빠르게 검증하고 질의할 때 사용한다. Property Graph와 경쟁시키지 않고 semantic index 역할로 제한한다.

### HydraDB — 분산 Graph 확장 옵션

HydraDB는 Object Storage를 durable source of truth로 두고 compute/indexer를 분리한다. OpenCypher와 Neo4j-compatible Bolt, HTTP query API를 제공하므로 Google Drive 대규모 인벤토리에서 파생된 관계 데이터가 커질 때 확장 후보가 된다.

하지만 라이선스가 AGPL-3.0이므로 이번 단계에서는 **코드 vendoring/링킹 없이 외부 서비스 프로토콜 경계**로만 연결한다. `runtime/hydradb/seed.cypher`은 우리 canonical JSONL에서 재생성되는 파생물이다.

## 4. 최종 데이터 흐름

```text
Google Drive / DWG / DXF / IFC / PDF / HWP / SKP ...
        |
        v
Existing parsers + source revision/provenance guards
        |
        v
CAIR + canonical JSON/JSONL ------------------------------+
        |                                                  |
        |                                                  |
        +--> Local structured/vector/RDF                   |
        |      DuckDB / pgvector / PyOxigraph              |
        |                                                  |
        +--> Property Graph acceleration                   |
        |      Apache AGE (default)                        |
        |      HydraDB (large/distributed, external only)  |
        |                                                  |
        +--> Agent context selection                       |
               exact symbol/path -> direct read/rg         |
               unfamiliar cross-file question -> jevgrep   |
               result -> agent reads canonical evidence    |
                                                           |
Power CAD / MCP / Agent action <--- verify/provenance -----+
```

## 5. 적용 우선순위

### P0 — 이번 브랜치

- Jevgrep 안전 어댑터
- private source egress 기본 차단
- WSL wrapper 가능 구조
- HydraDB용 portable OpenCypher seed 생성
- HydraDB HTTP API 어댑터
- capability 기반 graph backend planner
- canonical/global 원본 비변경 테스트

### P1 — 이번 브랜치에서 구현

- MCP Gateway에 `aec.context_inspect_code`, `aec.graph_backend_plan`, `aec.graph_hydradb_preview`, `aec.graph_hydradb_export` 노출
- Jev source egress는 기본 차단하고, root를 저장소 내부로 제한
- HydraDB preview는 메모리에서만 생성하고 export는 `runtime/hydradb/`에만 허용
- Power CAD drawing_context 후보 선택에 Jev-style numbered typed choice contract 연결
- typed choice 결과는 실행 권한이 아니며 기존 live verification을 반드시 거치도록 고정

### P2 — 데이터 규모가 커질 때

- HydraDB S3-compatible object storage 배포 실험
- GraphBLAS path procedure를 공간/인접/연결성 질의에 제한적으로 벤치마크
- Hindsight 같은 장기 Agent Memory는 CAIR truth와 분리된 advisory memory로 추가 검토

## 6. 절대 지켜야 할 규칙

- Jev 결과는 **근거가 아니라 읽을 후보**다.
- Graph DB는 **원본이 아니라 가속기**다.
- Agent Memory는 **사실 저장소가 아니라 보조 기억**이다.
- CAD mutation은 기존 source/revision/live mapping 검증을 통과한 뒤에만 가능하다.
- 새 backend가 실패해도 canonical CAIR/JSONL만으로 전체를 재구축할 수 있어야 한다.

# RAGFlow v0.27.2 로컬 배포 준비 — Ontology Drawing Context

작성/검증일: 2026-10-01 (KST)

이 문서는 RAGFlow를 Ontology의 **파생 검색 sidecar**로 배포하기 위한 운영 절차입니다.
RAGFlow는 CAIR, 원본 도면, provenance, ACL 또는 AutoCAD 상태의 canonical authority가 아닙니다.

## 1. 고정 버전과 원칙

- Upstream: `https://github.com/infiniflow/ragflow.git`
- Release: `v0.27.2`
- Image tag: `infiniflow/ragflow:v0.27.2`
- 실제 활성화 시 Docker `RepoDigest`에서 `sha256:...` digest를 기록하고
  `RagflowHttpConfig.release_image_digest`에 사용합니다.
- `nightly`, floating `latest`, 임의 API 버전 혼용 금지.
- 공식 Docker Compose를 재사용하며 Ontology 저장소에 복제하지 않습니다.
- `docker compose down -v`는 데이터 볼륨을 삭제할 수 있으므로 운영 절차에 사용하지 않습니다.

## 2. 최소 호스트 조건

공식 v0.27.2 문서 기준 시작점:

- CPU: 4 cores 이상
- RAM: 16 GB 이상
- free disk: 50 GB 이상
- Docker: 24.0.0 이상
- Docker Compose: 2.26.1 이상
- 공식 prebuilt image: x86_64/amd64 기준
- Elasticsearch 사용 시 `vm.max_map_count >= 262144`

Windows Docker Desktop/WSL2에서는 `vm.max_map_count`가 Windows Python에서 직접
관찰되지 않을 수 있으므로 preflight가 manual check를 반환할 수 있습니다.

## 3. 읽기 전용 preflight

Ontology root에서:

```bash
PYTHONPATH=extensions/drawing_context \
python -m context_fabric.ragflow_deploy \
  --path . \
  --checkout runtime/ragflow-upstream
```

이 명령은 **clone/pull/start/stop을 실행하지 않습니다**.
Docker/Compose 버전, CPU/RAM/disk, architecture, 가능한 경우
`vm.max_map_count`와 현재 image RepoDigest를 읽고 JSON 보고서와 명령 계획만 출력합니다.

상태:

- `READY`: 관찰 가능한 필수 조건 통과
- `READY_WITH_MANUAL_CHECKS`: Windows/WSL 등 수동 확인 필요
- `BLOCKED`: CPU/RAM/disk/Docker/Compose/architecture 등 필수 조건 미충족

## 4. upstream 설치

preflight가 출력한 명령을 검토한 뒤 직접 실행합니다.

새 checkout 예:

```bash
git clone https://github.com/infiniflow/ragflow.git runtime/ragflow-upstream
git -C runtime/ragflow-upstream checkout -f v0.27.2
docker compose -f runtime/ragflow-upstream/docker/docker-compose.yml pull
docker compose -f runtime/ragflow-upstream/docker/docker-compose.yml up -d
```

health 확인:

```bash
curl -f http://127.0.0.1/api/v1/system/healthz
```

image digest 확인:

```bash
docker image inspect infiniflow/ragflow:v0.27.2 \
  --format "{{index .RepoDigests 0}}"
```

출력 예의 `@sha256:...` 중 `sha256:...` 부분만 release image digest로 사용합니다.

## 5. 네트워크와 비밀값

초기 검증은 loopback만 사용합니다.

- RAGFlow API key는 환경변수/secret store에만 둡니다.
- API key를 Git, Google Drive manifest, CAIR, provenance, benchmark fixture에 기록하지 않습니다.
- non-loopback RAGFlow URL은 HTTPS만 허용됩니다.
- RAGFlow 자체의 MySQL/Redis/MinIO/Elasticsearch 비밀번호도 기본값을 그대로 외부에 노출하지 않습니다.

## 6. dataset/document 준비

RAGFlow UI 또는 공식 API로 별도 benchmark dataset과 document를 만듭니다.
Ontology는 dataset ownership을 canonical로 관리하지 않습니다.

Drawing Context의 `ragflow_projection()` 결과는 기존 document에 chunk로 투영할 수 있으며,
remote chunk ID는 다음 로컬 파생 매핑으로 다시 연결됩니다.

```text
remote chunk_id
  -> drawing-context external_id
  -> canonical_id
  -> source_id
  -> revision_id
  -> project_id
  -> sha256
```

이 매핑은 `runtime/ragflow/bindings.json`에만 저장할 수 있습니다.
`global/`, `projects/`, 원본 source 경로에는 쓸 수 없습니다.

## 7. 재시작 후 binding 복구

Python:

```python
from context_fabric.ragflow_http import RagflowBindingRegistry

registry = RagflowBindingRegistry.load_runtime(".")
# ...
registry.save_runtime(".")
```

저장은 temp file 후 atomic replace로 이루어지며 schema/conflict 검증을 통과해야 합니다.

## 8. 검색 경로

운영 검색:

```python
adapter.search(
    "화장실 출입문",
    allowed_source_ids=authorized_source_ids,
    current_revision_by_source=current_revisions,
    top_k=5,
)
```

운영 `search()`는 다음 결과를 반환하지 않습니다.

- local binding이 없는 remote chunk
- 현재 사용자에게 허용되지 않은 source
- 현재 revision이 아닌 chunk

차단된 hit의 **본문도 반환하지 않습니다**.

`benchmark_search()`는 누출을 측정하기 위한 diagnostic-only API입니다.
사용자 응답이나 Power CAD context에 직접 사용하지 않습니다.

## 9. live benchmark

실서버가 준비된 후 `run_live_benchmark()`는 read-only 검색만 수행합니다.

승격 hard gate:

- provenance metadata coverage = 100%
- unauthorized source leakage = 0
- stale revision leakage = 0
- Recall@5 >= 0.80
- MRR >= 0.60

p50/p95 latency, indexing time, storage는 첫 단계에서는 비교 지표입니다.

hard gate를 통과하기 전:

- RAGFlow를 default search로 승격하지 않음
- Power CAD context source로 직접 신뢰하지 않음
- LightRAG 대비 우위라고 선언하지 않음

## 10. revision / ACL 변경

새 revision:

1. 새 projection chunk를 먼저 생성
2. 모든 새 binding 생성 성공 확인
3. 이전 revision chunk 삭제
4. runtime binding 저장
5. benchmark/search에서 stale leakage 0 확인

ACL 회수:

1. canonical authorization gate에서 즉시 source 접근 차단
2. RAGFlow `purge_source()` 수행
3. remote delete 성공 후 binding 제거
4. 삭제가 지연돼도 production `search()`는 unauthorized source를 노출하지 않음

## 11. 아직 완료로 간주하지 않는 것

현재 저장소 테스트는 fake HTTP transport와 계약 검증입니다.
다음은 실제 환경에서 별도 검증해야 합니다.

- 실제 RAGFlow v0.27.2 Docker stack 기동
- 실제 image RepoDigest
- 실제 API key
- 실제 dataset/document/chunk 생성
- Google Drive 대표 문서 20개 이상
- 고정 질의 30개 이상
- revision 교체/ACL 회수/삭제 replay
- p50/p95, indexing throughput, storage
- CPU/RAM 압박 시 안정성

위 항목을 통과해야 G4 live RAGFlow acceptance로 기록합니다.

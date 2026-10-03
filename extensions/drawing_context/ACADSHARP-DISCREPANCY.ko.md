# ACadSharp discrepancy detector

기준일: 2026-10-03

목적은 ACadSharp를 두 번째 정답 엔진으로 만드는 것이 아니다.
같은 원본 DWG를 독립된 두 parsing lane으로 읽었을 때 서로 다른 관찰을
review 대상으로 표면화하는 discrepancy detector다.

## 재사용 대상

ACadSharp 구현은 Ontology에 복사하지 않는다.

현재 외부 verifier 후보:

- repository: khs0927/All-In-Cad
- reviewed commit: 957c1f8e1d1e212f830325f08de2d7543efe1c2d
- project: native/headless/AllInCad.ACadSharpProbe.csproj
- ACadSharp package: 3.7.1
- output: all-in-cad-headless-census/v1
- source access: read-only DwgReader

All-In-Cad의 2026-09-28 upstream-audit 실패 때문에 upstream.lock 전체를
VERIFIED registry로 가져오지 않는다. 이 probe는 별도 외부 실행 capability로만
평가한다.

## 비교 원칙

두 census는 동일한 source SHA-256이어야 한다.
다르면 INCOMPARABLE이다.

비교 가능한 항목:

- 전체 entity count
- entity type별 count
- layer별 count
- 양쪽 모두 layout 정보를 완전하게 제공할 때 layout별 count
- 양쪽 모두 제공할 때 block definition / nested reference / XREF 정보
- 양쪽 모두 제공할 때 unsupported/proxy 목록

### handle과 geometry

ODA→DXF→ezdxf 결과의 handle이 원본 DWG handle과 같다고 가정하지 않는다.

객체별 geometry/type/layer 비교는 독립적으로 검증된 handle mapping evidence가
있을 때만 수행한다.

mapping evidence가 없으면 결과는:

    object_level_comparison = SKIPPED_NO_VERIFIED_HANDLE_MAPPING

으로 남는다.

같은 handle 문자열이 우연히 양쪽에 있어도 자동 매칭하지 않는다.

## 결과 의미

status:

- MATCH: 비교 가능한 범위에서 discrepancy가 발견되지 않음
- DISCREPANCIES: 하나 이상의 비교 가능한 차이가 발견됨
- INCOMPARABLE: 같은 원본 bytes라는 전제부터 만족하지 않음

MATCH는 어느 parser가 정확하다는 증명이 아니다.

항상:

    truth_oracle = false
    winner = null
    correctness_score = null
    canonical_mutation = false

이다.

elapsed_ms, peak_rss_mb 같은 비용 관찰은 report에 보존하지만 correctness 판정에
사용하지 않는다.

## 실행 예

All-In-Cad ACadSharp probe와 기존 ezdxf lane이 같은 source를 읽은 결과가 있을 때:

    python scripts/compare_cad_parsers.py \
      --source runtime/A-201.dwg \
      --left runtime/acadsharp-census.json \
      --right runtime/ezdxf-census.json \
      --left-provider acadsharp \
      --left-version 3.7.1 \
      --left-handle-space source-dwg \
      --left-adapter-commit 957c1f8e1d1e212f830325f08de2d7543efe1c2d \
      --right-provider ontology-ezdxf \
      --right-version <pipeline-version> \
      --right-handle-space converted-dxf \
      --out runtime/parser-discrepancy.json

실제 representative DWG에서 먼저 실행해 discrepancy 종류와 비용을 측정한다.
전체 Drive를 처음부터 두 parser로 모두 읽지 않는다.

## 다음 native acceptance

대표 fixture마다 다음을 기록한다.

- source SHA-256
- All-In-Cad probe commit/package version
- Ontology parser version
- ODA converter version 또는 lane
- entity/type/layer/layout/block discrepancy
- unsupported/proxy discrepancy
- elapsed time / peak memory
- verified handle mapping 존재 여부

차이가 나면 원본과 parser/version을 포함한 report를 evidence로 보존하고
사람 또는 native AutoCAD readback 검토로 넘긴다.

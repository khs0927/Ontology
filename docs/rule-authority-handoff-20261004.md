# 법규 결과 handoff의 검토 조건

`load_rules`는 JSON의 PASS/FAIL만으로 법규 판정을 채택하지 않는다. 원래 분기 결과는
`exported_outcome`에 남고, canonical evaluation metadata가 없거나 불완전하면 `outcome=REVIEW`다.
잘못된 subject reference, 현재 도면/파서 개정 불일치, 평가 프로젝트 불일치도 검토 대상이다.

`canonical_context` 계약(`archontos-canonical-context/1`): UUID 형식의 `rule_version_id`,
`evaluation_id`, `source_version_id`, 비어 있지 않은 `assertion_ids`; `project_key`;
`review_status=approved`, `status=active`, `binding=true`, `synthetic=false`;
`valid_from`, 선택적인 `valid_to`, 시간대가 명시된 `evaluated_at`이 필요하다.
평가일과 현재일 모두 시행 기간 안이어야 한다. 현재일은 평가 기록의 시간대에서 계산한다.

이는 로컬 파일의 증거 전달 계약이다. 메타데이터만으로 작성자·법령 진위를 인증하지 않는다.
실제 법규 수집 및 승인 절차를 거친 canonical exporter를 별도로 연결해야 한다.
테스트의 canonical metadata는 합성 fixture이며 실제 법규 데이터나 법률 적합성 결과가 아니다.

오프라인 회귀 테스트 50개 통과, Ruff 및 diff whitespace 검사 통과.
운영 DB/API/모델 변경은 수행하지 않았다. GitHub PostgreSQL CI는 PR에서 별도 확인한다.

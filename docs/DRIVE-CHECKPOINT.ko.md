# Google Drive DB 체크포인트

원본·추출 결과·온톨로지·검증 근거·복구 가능한 DB 백업은 Google Drive에서 관리한다.
실행 중인 PostgreSQL 데이터 디렉터리, WAL, Docker VHDX, 모델 캐시는 동기화하지 않는다.
각 PC의 로컬 DB는 검증된 백업에서 복구 가능한 실행 상태다.

## 업로드와 읽기 검증

`scripts/ops/publish-checkpoint.ps1`은 **이미 완성된 덤프**만 처리한다. 새로운 백업이나 복원을
시작하지 않으므로 별도 에이전트의 디스크 이전과 작업 소유권을 구분할 수 있다. 대용량 전송은
이전 작업 완료 후 실행하는 것을 권장한다. rclone의 `gdrive:` 원격이 설정되어 있어야 한다.

```powershell
powershell -NoProfile -File scripts\ops\publish-checkpoint.ps1 `
  -Dump 'D:\AECData\backups\aec-db-YYYYMMDDTHHMMSSZ.dump' `
  -Checkpoint 'D:\AECData\checkpoint.json' `
  -Receipt 'D:\AECData\reports\drive-publication.json'
```

체크포인트 JSON에는 재개 위치·작업 설정·소스 revision 등의 비밀정보가 아닌 값만 넣는다.
`.env`, API 토큰, DSN, 암호는 넣지 않는다. 일반 파일이나 전체 폴더 업로드는 지원하지 않는다.
덤프·선택적 체크포인트 JSON·선택적 복원 보고서만 별도 UUID 폴더에 복사한다.

모든 원격 파일을 다시 다운로드하여 크기와 SHA-256을 검증한 후 `READY.json`을 마지막으로
업로드하고 읽기 검증한다. 실패한 중간 폴더에는 사용 가능한 체크포인트로 인정되는 완료 표시가
없다. 기존 원격 자료를 삭제하거나 회전하지 않는다. D staging에는 덤프 한 벌과 일시적 읽기
검증 파일이 필요하다. 성공·실패 staging 정리는 운영자가 명시적으로 관리한다.

`BACKUP_VERIFIED`는 원격 바이트 검증 완료를 뜻하며, 실제 DB 복원 성공을 뜻하지 않는다.
`RECOVERY_VERIFIED`는 해당 덤프의 `dump_sha256`이 포함된 강화된 restore-drill 보고서의
MATCH, 종료 코드 0, 동일한 행 수, 정상 인덱스와 제약조건을 확인한 경우에만 사용한다.
PGDMP 헤더 검사만으로 덤프 전체의 내부 건전성을 증명할 수 없으므로 복구 훈련은 별도 필수다.

## 새 PC에서 가져오기

```powershell
$env:PYTHONPATH = 'C:\CODE\Ontology\src'
python -m aec_intelligence.operational.drive_checkpoint fetch `
  --remote 'gdrive:AEC-INTELLIGENCE/90_ARCHIVE/db-checkpoints/<정확한 UUID>' `
  --destination 'D:\AECData\restore-input\<새 UUID>'
```

다운로드는 완료 표시·manifest·개별 SHA-256·경로를 검증한다. 기존 목적지 디렉터리는 덮어쓰지
않는다. **실제 DB 복원이나 worker 시작은 수행하지 않는다.** 기존 PC가 쓰기를 멈추고 새 PC를
지정해 소유권을 넘겼다는 별도 검증 없이 새 PC는 읽기 전용으로 유지해야 한다.
Drive 동기화 파일이나 업로드 receipt를 분산 쓰기 잠금으로 간주하면 안 된다.

## 검증 범위

단위/통합 테스트는 원격 바이트 손상, 전송 중단, manifest 변조·경로 탈출, 불완전한 복원 근거,
비밀정보 포함 metadata, 기존 목적지 보호를 확인한다. 실제 Drive 전송 및 DB 복원 결과는
별도 운영 receipt에 기록한다. 이 문서는 PC 자동 전환의 구현 완료를 주장하지 않는다.

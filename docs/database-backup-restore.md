# AEC-INTELLIGENCE Database Backup & Restore Guide (`aec-db`)

본 문서는 **AEC-INTELLIGENCE** 시스템의 운영 데이터베이스인 `aec-db` (PostgreSQL 16 + Apache AGE)의 무중단·무디스크(Zero-Disk) 백업 및 재해 복구(DR) 절차를 규정합니다.

---

## 1. 아키텍처 개요 (Architecture Overview)

### 1.1. 시스템 내 역할 및 저장소 구조
AEC-INTELLIGENCE의 [docker-compose.yml](https://github.com/khs0927/Ontology/blob/master/docker-compose.yml) 스택에서 `aec-db`는 지식 그래프 런타임과 비동기 작업 파이프라인의 중심 저장소입니다.

```text
+-------------------------------------------------------------------------+
|                        AEC-INTELLIGENCE Stack                           |
|                                                                         |
|  [ aec-api ]          [ aec-worker-cad ]         [ aec-worker-ocr ]     |
|  (Port: 58000)        (Queue: cad)               (Queue: ocr)           |
|        │                     │                          │               |
|        └──────────────┬──────┴──────────────────────────┘               |
|                       ▼                                                 |
|            [ aec-db (PostgreSQL 16 + Apache AGE) ]                      |
|            - Port: 55432:5432                                           |
|            - Volume: D:/AECData/docker/pgdata                           |
+───────────────────────┬─────────────────────────────────────────────────+
                        │ Zero-Disk Streaming (Pipe)
                        ▼
       [ rclone (gdrive:AEC-INTELLIGENCE/08_BACKUPS/db) ]
```

* **엔진**: PostgreSQL 16 및 Apache AGE 1.6.0 (그래프 데이터베이스 확장)
* **주요 적재 데이터**:
  * CAD/BIM 형상 메타데이터 및 도면 엔티티
  * Apache AGE 그래프 노드 및 엣지 (`ag_catalog` 레이블 및 그래프 메타데이터)
  * OCR 분석 결과 및 비동기 작업 큐 상태
* **호스트 볼륨 경로**: `D:/AECData/docker/pgdata`

### 1.2. 스토리지 제약과 클라우드 백업 전략
도면 및 3D 모델 메타데이터의 누적으로 로컬 호스트 디스크의 가용 공간이 극히 제한적인 환경입니다. 이에 따라 [docs/drive-integration.md](https://github.com/khs0927/Ontology/blob/master/docs/drive-integration.md)에 명시된 원격 스토리지 정책을 준용하여, **로컬 디스크에 일체의 중간 덤프 파일(`.dump`, `.sql`)을 생성하지 않고 Google Drive로 직접 스트리밍하는 파이프라인**을 운영 표준으로 정의합니다.

---

## 2. Zero-Disk Streaming 설계 원리 (Zero-Disk Design)

### 2.1. 백업 스트리밍 메커니즘
덤프 생성 프로세스의 표준 출력(stdout)을 중간 파일 없이 Unix 파이프(`|`)로 `rclone rcat`의 표준 입력(stdin)에 연결하여 Google Drive로 실시간 멀티파트 업로드합니다.

```bash
docker exec -i aec-db pg_dump -U aec -d aec -F c -Z 6 | rclone rcat "${REMOTE_PATH}"
```

* **`-F c` (Custom Archive Format)**:
  * PostgreSQL 독점 바이너리 압축 아카이브 포맷입니다.
  * 테이블/인덱스 단위의 메타데이터(TOC: Table of Contents)가 포함되어 있어, 복원 시 선택적 복원, 스키마 재정렬, 병렬 복원(`-j`)이 가능합니다.
* **`-Z 6` (Compression Level)**:
  * 압축 효율과 CPU 부하 간 최적 균형을 제공합니다 (기본 권장값 6).
* **메모리 버퍼링**:
  * 스트림은 프로세스 메모리 버퍼(`rclone` chunk size) 내에서만 유지되며 디스크 I/O를 유발하지 않습니다.

### 2.2. 복원 스트리밍 메커니즘
복구 시에도 원격지 덤프 파일을 로컬로 내려받지 않고 `rclone cat`으로 스트리밍하여 `pg_restore`로 직접 주입합니다.

```bash
rclone cat "${REMOTE_FILE}" | docker exec -i aec-db pg_restore -U aec -d aec --clean --if-exists -v
```

* **`--clean --if-exists`**: 기존 데이터베이스 객체를 안전하게 드롭한 후 재생성합니다.

### 2.3. 파이프라인 무결성 제어 (`set -euo pipefail`)
기본 Bash 환경에서는 파이프 앞단의 `pg_dump`가 실패하더라도 뒷단의 `rclone rcat`이 정상 종료되면 전체 명령어가 성공으로 처리됩니다. 이를 차단하기 위해 반드시 다음 선언을 강제합니다:
* `set -e`: 0이 아닌 종료 코드가 반환되면 즉시 스크립트 중단
* `set -u`: 선언되지 않은 변수 참조 시 에러 발생
* `set -o pipefail`: 파이프라인 내 어느 한 명령어라도 실패할 경우 해당 에러 코드를 전체 파이프라인의 종료 코드로 반환

---

## 3. 환경 설정 (Environment Configuration)

### 3.1. rclone 설치 및 원격지(`gdrive`) 구성
호스트 머신(Linux)에서 다음 단계를 수행합니다.

```bash
# 1. rclone 바이너리 설치
curl https://rclone.org/install.sh | sudo bash

# 2. 원격지 설정 시작
rclone config
```

대화형 설정 입력값:
1. `n` (New remote)
2. `name`: `gdrive`
3. `Storage type`: `drive` (Google Drive)
4. `client_id` / `client_secret`: 프로덕션 안정성을 위해 GCP 콘솔에서 생성한 전용 OAuth Client ID 사용 권장
5. `scope`: `drive.file` (rclone이 직접 생성한 파일에만 접근하는 최소 권한 부여 권장)
6. 인증 완료 후 토큰 저장

연결 확인:
```bash
# 원격 디렉터리 접근 테스트
rclone lsd gdrive:AEC-INTELLIGENCE/
```

### 3.2. 저장소 경로 및 보존 주기 (Retention Policy)
* **원격지 기본 경로**: `gdrive:AEC-INTELLIGENCE/08_BACKUPS/db/`
* **파일 명명 규칙**: `aec_db_backup_{YYYYMMDD_HHMMSS}.dump`
* **보존 기간 (30일)**: 백업 성공 직후 30일이 경과한 백업 파일은 자동 삭제됩니다.
  ```bash
  rclone delete --min-age 30d "gdrive:AEC-INTELLIGENCE/08_BACKUPS/db"
  ```

---

## 4. 일상 운영 자동화 (Operational Automation)

### 4.1. 스크립트 위치 및 실행 권한
* 백업 스크립트: `scripts/ops/backup_db.sh`
* 복원 스크립트: `scripts/ops/restore_db.sh`

```bash
chmod +x scripts/ops/backup_db.sh scripts/ops/restore_db.sh
```

### 4.2. Linux Host Crontab 등록 및 로그 관리

#### Crontab 등록 (`crontab -e`)
매일 새벽 03:00에 백업 작업을 자동 수행하도록 등록합니다.

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 매일 새벽 3시 aec-db 무디스크 스트리밍 백업 수행
0 3 * * * /bin/bash /path/to/Ontology/scripts/ops/backup_db.sh >> /var/log/aec_db_backup.log 2>&1
```

#### 로그 로테이션 (`/etc/logrotate.d/aec-db-backup`)
```text
/var/log/aec_db_backup.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0640 root root
}
```

---

## 5. 재해 복구(DR) 및 데이터 무결성 검증 절차

### 5.1. 원격 덤프 무결성 및 TOC 사전 검증 (Zero-Disk TOC Verification)
복원을 수행하기 전, 원격지에 저장된 덤프 파일이 깨지지 않았는지 로컬 다운로드 없이 실시간 검증합니다.

```bash
rclone cat "gdrive:AEC-INTELLIGENCE/08_BACKUPS/db/<TARGET_BACKUP>.dump" \
  | pg_restore --list \
  | head -n 40
```

### 5.2. 재해 복구(DR) 단계별 실행 절차 (Runbook)

#### 1단계: 복구 대상 백업 파일 확인
```bash
./scripts/ops/restore_db.sh --list
```

#### 2단계: 애플리케이션 및 워커 컨테이너 중지
복원 중 데이터 불일치를 방지하기 위해 쓰기 작업을 수행하는 서비스를 일시 정지합니다.
```bash
docker stop aec-api aec-worker-cad aec-worker-ocr
```

#### 3단계: Zero-Disk 스트리밍 복원 수행
```bash
# 최신 백업으로 대화형 복원
./scripts/ops/restore_db.sh

# 또는 특정 파일 강제 복원
./scripts/ops/restore_db.sh --force aec_db_backup_YYYYMMDD_HHMMSS.dump
```

#### 4단계: 사후 무결성 및 확장 기능 점검 (SQL Query)
복원 완료 후 Apache AGE 확장과 핵심 테이블이 정상 상태인지 검증합니다.

```bash
docker exec -it aec-db psql -U aec -d aec -c "
-- 1. AGE 확장 설치 상태 확인
SELECT extname, extversion FROM pg_extension WHERE extname = 'age';

-- 2. 등록된 AGE 그래프 목록 확인
SELECT * FROM ag_catalog.ag_graph;

-- 3. 테이블 레코드 건수 점검
SELECT schemaname, relname, n_live_tup 
FROM pg_stat_user_tables 
ORDER BY n_live_tup DESC LIMIT 10;
"
```

#### 5단계: 애플리케이션 및 워커 재기동
```bash
docker start aec-api aec-worker-cad aec-worker-ocr
```

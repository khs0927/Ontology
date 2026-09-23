#!/usr/bin/env bash
# ==============================================================================
# Script Name : backup_db.sh
# Target Repo : khs0927/Ontology
# Description : PostgreSQL 16 + Apache AGE (aec-db) Zero-Disk Streaming Backup
# Pipeline    : docker exec -i aec-db pg_dump ... | rclone rcat ...
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# 1. 설정 및 환경 변수
# ------------------------------------------------------------------------------
DOCKER_CONTAINER="${DOCKER_CONTAINER:-aec-db}"
DB_NAME="${DB_NAME:-aec}"
DB_USER="${DB_USER:-aec}"
COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-6}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:AEC-INTELLIGENCE/08_BACKUPS/db}"
RETENTION_DAYS="${RETENTION_DAYS:-30d}"

TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
BACKUP_FILENAME="${DB_NAME}_backup_${TIMESTAMP}.dump"
TARGET_REMOTE_PATH="${RCLONE_REMOTE}/${BACKUP_FILENAME}"

# ------------------------------------------------------------------------------
# 2. 로깅 및 에러 핸들러
# ------------------------------------------------------------------------------
log() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] $*"
}

error_handler() {
    local exit_code="$?"
    local line_no="$1"
    local failed_command="$2"
    log "[ERROR] 백업 파이프라인 실패 (Line: ${line_no}, Command: '${failed_command}', Exit Code: ${exit_code})" >&2
    exit "${exit_code}"
}

trap 'error_handler ${LINENO} "${BASH_COMMAND}"' ERR

# ------------------------------------------------------------------------------
# 3. 사전 환경 및 의존성 검증
# ------------------------------------------------------------------------------
log "[INFO] 사전 점검 시작..."
command -v docker >/dev/null 2>&1 || { log "[FATAL] docker 명령어를 찾을 수 없습니다." >&2; exit 1; }
command -v rclone >/dev/null 2>&1 || { log "[FATAL] rclone 명령어를 찾을 수 없습니다." >&2; exit 1; }

if ! docker ps --format '{{.Names}}' | grep -Eq "^${DOCKER_CONTAINER}$"; then
    log "[FATAL] 대상 컨테이너 '${DOCKER_CONTAINER}'가 실행 중이지 않습니다." >&2
    exit 1
fi

if ! docker exec -i "${DOCKER_CONTAINER}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    log "[FATAL] '${DOCKER_CONTAINER}' 컨테이너 내 PostgreSQL(${DB_NAME}) 연결 준비가 되지 않았습니다." >&2
    exit 1
fi

# ------------------------------------------------------------------------------
# 4. Zero-Disk 스트리밍 백업 실행
# ------------------------------------------------------------------------------
log "[START] PostgreSQL 스트리밍 백업 시작"
log "  - 컨테이너: ${DOCKER_CONTAINER}"
log "  - 데이터베이스: ${DB_NAME} (사용자: ${DB_USER})"
log "  - 압축 레벨: -F c -Z ${COMPRESSION_LEVEL}"
log "  - 대상 원격지: ${TARGET_REMOTE_PATH}"

docker exec -i "${DOCKER_CONTAINER}" pg_dump \
    -U "${DB_USER}" \
    -d "${DB_NAME}" \
    -F c \
    -Z "${COMPRESSION_LEVEL}" \
    | rclone rcat "${TARGET_REMOTE_PATH}"

log "[SUCCESS] 원격지 스트리밍 업로드 완료: ${TARGET_REMOTE_PATH}"

# ------------------------------------------------------------------------------
# 5. 원격 저장소 보존 정책 적용 (Retention Policy: 30일)
# ------------------------------------------------------------------------------
log "[CLEANUP] 보존 주기(${RETENTION_DAYS}) 초과 백업 파일 정리 중..."
rclone delete --min-age "${RETENTION_DAYS}" "${RCLONE_REMOTE}"
log "[DONE] 백업 파이프라인 및 정리 작업이 성공적으로 완료되었습니다."

#!/usr/bin/env bash
# ==============================================================================
# Script Name : restore_db.sh
# Target Repo : khs0927/Ontology
# Description : PostgreSQL 16 + Apache AGE (aec-db) Zero-Disk Streaming Restore
# Pipeline    : rclone cat ... | docker exec -i aec-db pg_restore ...
# ==============================================================================
set -euo pipefail

# ------------------------------------------------------------------------------
# 환경 변수 및 기본 구성
# ------------------------------------------------------------------------------
DOCKER_CONTAINER="${DOCKER_CONTAINER:-aec-db}"
DB_USER="${DB_USER:-aec}"
DB_NAME="${DB_NAME:-aec}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive:AEC-INTELLIGENCE/08_BACKUPS/db}"

FORCE_RESTORE=false
TARGET_FILE=""

# ------------------------------------------------------------------------------
# 로깅 및 에러 핸들러
# ------------------------------------------------------------------------------
log() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] [INFO] $*"
}

warn() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] [WARN] $*" >&2
}

error() {
    echo "[$(date +"%Y-%m-%d %H:%M:%S")] [ERROR] $*" >&2
}

error_handler() {
    local exit_code=$?
    local line_no=$1
    error "데이터베이스 복원 파이프라인 실패 (Line: ${line_no}, Exit Code: ${exit_code})"
    exit "${exit_code}"
}

trap 'error_handler ${LINENO}' ERR

# ------------------------------------------------------------------------------
# 사용법 안내
# ------------------------------------------------------------------------------
usage() {
    cat <<EOF
사용법: $(basename "$0") [옵션] [백업_파일명_또는_경로]

설명:
  Google Drive 원격지에서 로컬 디스크 저장 없이 aec-db 컨테이너로 직접 스트리밍 복원합니다.
  백업 파일명을 지정하지 않으면 가장 최신 백업 파일을 자동으로 탐색하여 복원합니다.

옵션:
  -f, --force       확인 프롬프트를 건너뛰고 즉시 복원을 실행합니다.
  -l, --list        원격지의 사용 가능한 백업 파일 목록을 출력하고 종료합니다.
  -h, --help        본 도움말 메시지를 출력하고 종료합니다.

환경 변수:
  DOCKER_CONTAINER  대상 Docker 컨테이너명 (기본값: aec-db)
  DB_USER           PostgreSQL 접속 계정 (기본값: aec)
  DB_NAME           대상 데이터베이스명 (기본값: aec)
  RCLONE_REMOTE     rclone 원격지 디렉터리 경로 (기본값: gdrive:AEC-INTELLIGENCE/08_BACKUPS/db)

사용 예시:
  # 1. 최신 백업 파일 자동 탐색 및 대화형 복원
  ./scripts/ops/restore_db.sh

  # 2. 특정 백업 파일 지정 및 프롬프트 없이 강제 복원
  ./scripts/ops/restore_db.sh --force aec_db_backup_20260912_103356.dump

  # 3. 원격지 전체 백업 목록 조회
  ./scripts/ops/restore_db.sh --list
EOF
}

# ------------------------------------------------------------------------------
# 옵션 파싱
# ------------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--force)
            FORCE_RESTORE=true
            shift
            ;;
        -l|--list)
            log "원격 저장소 백업 파일 목록 조회 (${RCLONE_REMOTE}):"
            rclone lsf "${RCLONE_REMOTE}" --files-only --format "tp" --sort time --reverse
            exit 0
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            error "알 수 없는 옵션: $1"
            usage
            exit 1
            ;;
        *)
            TARGET_FILE="$1"
            shift
            ;;
    esac
done

# ------------------------------------------------------------------------------
# 사전 점검
# ------------------------------------------------------------------------------
command -v rclone >/dev/null 2>&1 || { error "rclone 바이너리가 설치되어 있지 않거나 PATH에 없습니다."; exit 1; }
command -v docker >/dev/null 2>&1 || { error "docker 바이너리가 설치되어 있지 않거나 PATH에 없습니다."; exit 1; }

if ! docker ps --format '{{.Names}}' | grep -q "^${DOCKER_CONTAINER}$"; then
    error "대상 Docker 컨테이너 '${DOCKER_CONTAINER}'가 실행 중이지 않습니다."
    exit 1
fi

# ------------------------------------------------------------------------------
# 대상 백업 파일 결정
# ------------------------------------------------------------------------------
if [[ -z "${TARGET_FILE}" ]]; then
    log "복원할 파일명이 지정되지 않아 최신 백업 파일을 탐색합니다..."
    
    LATEST_FILE=$(rclone lsf "${RCLONE_REMOTE}" --files-only --sort time --reverse 2>/dev/null | grep -E '\.dump)();
      return { result };
    } catch (err) {
      const message = (err && err.message) ? err.message : String(err);
      const stack = (err && err.stack) ? err.stack : undefined;
      return { err: message, stack };
    }
  }
}
 | head -n 1 || true)
    
    if [[ -z "${LATEST_FILE}" ]]; then
        error "원격 저장소(${RCLONE_REMOTE})에서 .dump 확장자를 가진 백업 파일을 찾을 수 없습니다."
        exit 1
    fi
    
    TARGET_FULL_PATH="${RCLONE_REMOTE}/${LATEST_FILE}"
    log "최신 백업 파일 발견: ${LATEST_FILE}"
else
    if [[ "${TARGET_FILE}" == *":"* ]]; then
        TARGET_FULL_PATH="${TARGET_FILE}"
    else
        TARGET_FULL_PATH="${RCLONE_REMOTE}/${TARGET_FILE}"
    fi
fi

if ! rclone lsf "${TARGET_FULL_PATH}" >/dev/null 2>&1; then
    error "지정한 백업 파일이 존재하지 않습니다: ${TARGET_FULL_PATH}"
    exit 1
fi

# ------------------------------------------------------------------------------
# 운영 안전장치: 사용자 승인 확인 프롬프트
# ------------------------------------------------------------------------------
if [[ "${FORCE_RESTORE}" != true ]]; then
    echo "=============================================================================="
    warn "경고: 데이터베이스 복원 작업 안내"
    echo "  - 대상 컨테이너: ${DOCKER_CONTAINER}"
    echo "  - 대상 DB명     : ${DB_NAME} (사용자: ${DB_USER})"
    echo "  - 원격 덤프 파일: ${TARGET_FULL_PATH}"
    echo "  - 적용 옵션     : --clean --if-exists -v (기존 오브젝트 삭제 후 재생성)"
    echo "=============================================================================="
    
    if [[ -t 0 ]]; then
        read -r -p "정말로 데이터베이스를 복원하시겠습니까? [y/N]: " CONFIRM
    elif [[ -c /dev/tty ]]; then
        read -r -p "정말로 데이터베이스를 복원하시겠습니까? [y/N]: " CONFIRM < /dev/tty
    else
        error "비대화형 환경에서는 --force 플래그를 사용해야 복원을 수행할 수 있습니다."
        exit 1
    fi

    if [[ ! "${CONFIRM}" =~ ^[yY]([eE][sS])?$ ]]; then
        log "사용자에 의해 복원 작업이 취소되었습니다."
        exit 0
    fi
fi

# ------------------------------------------------------------------------------
# 무디스크 스트리밍 복원 실행
# ------------------------------------------------------------------------------
log "[START] Google Drive 스트리밍 복원 시작: ${TARGET_FULL_PATH} -> ${DOCKER_CONTAINER}:${DB_NAME}"

rclone cat "${TARGET_FULL_PATH}" \
    | docker exec -i "${DOCKER_CONTAINER}" pg_restore \
        -U "${DB_USER}" \
        -d "${DB_NAME}" \
        --clean \
        --if-exists \
        -v

log "[SUCCESS] 데이터베이스 스트리밍 복원이 성공적으로 완료되었습니다."

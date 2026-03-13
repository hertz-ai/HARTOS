#!/bin/bash
# ============================================================
# HART OS — Docker Build & Run
# ============================================================
# Usage:
#   scripts/start_docker.sh                # Build + run (default)
#   scripts/start_docker.sh build          # Build only
#   scripts/start_docker.sh run            # Run only (image must exist)
#   scripts/start_docker.sh logs           # Tail container logs
#   scripts/start_docker.sh stop           # Stop + remove container
#   scripts/start_docker.sh restart        # Stop + run (no rebuild)
#   scripts/start_docker.sh status         # Show container status + health
# ============================================================

set -e

# ── Configuration ────────────────────────────────────────────
CONTAINER_NAME="langchain"
IMAGE_NAME="langchain_gpt"
IMAGE_TAG="gpt4.1"
IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
PORT=6777

# Resolve repo root (one level up from scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# All paths relative to repo root
ENV_FILE="${REPO_DIR}/.env"
LOG_DIR="${REPO_DIR}/logs"
IMAGE_DIR="${REPO_DIR}/output_images"
MANIFEST="${REPO_DIR}/release_manifest.json"
DOCKERFILE="${REPO_DIR}/Dockerfile"

# Master key location (server-specific, never in repo)
MASTER_KEY_FILE="/etc/hevolve/master_private_key.hex"

# ── Helpers ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Detect sudo requirement
DOCKER_CMD="docker"
if ! docker info > /dev/null 2>&1; then
    if sudo docker info > /dev/null 2>&1; then
        DOCKER_CMD="sudo docker"
    else
        error "Docker is not running or not accessible"
        exit 1
    fi
fi

# ── Build ────────────────────────────────────────────────────
do_build() {
    info "Building ${IMAGE} from ${REPO_DIR}..."
    ${DOCKER_CMD} build -t "${IMAGE}" -f "${DOCKERFILE}" "${REPO_DIR}"
    info "Build complete: ${IMAGE}"
}

# ── Stop ─────────────────────────────────────────────────────
do_stop() {
    if ${DOCKER_CMD} ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        info "Stopping ${CONTAINER_NAME}..."
        ${DOCKER_CMD} stop "${CONTAINER_NAME}" 2>/dev/null || true
        ${DOCKER_CMD} rm "${CONTAINER_NAME}" 2>/dev/null || true
        info "Container removed"
    else
        info "Container ${CONTAINER_NAME} not running"
    fi
}

# ── Run ──────────────────────────────────────────────────────
do_run() {
    # Verify image exists
    if ! ${DOCKER_CMD} image inspect "${IMAGE}" > /dev/null 2>&1; then
        error "Image ${IMAGE} not found. Run: scripts/start_docker.sh build"
        exit 1
    fi

    # Stop existing container
    do_stop

    # Create directories
    mkdir -p "${LOG_DIR}" "${IMAGE_DIR}"

    # Build run command
    RUN_ARGS="-d --name ${CONTAINER_NAME} --restart unless-stopped"
    RUN_ARGS="${RUN_ARGS} -p ${PORT}:${PORT}"

    # Env file
    if [ -f "${ENV_FILE}" ]; then
        RUN_ARGS="${RUN_ARGS} --env-file ${ENV_FILE}"
        info "Using env file: ${ENV_FILE}"
    else
        warn "No .env file found at ${ENV_FILE}"
    fi

    # Master key (only if file exists on server)
    if [ -f "${MASTER_KEY_FILE}" ]; then
        MASTER_KEY_VAL="$(sudo cat "${MASTER_KEY_FILE}" 2>/dev/null || cat "${MASTER_KEY_FILE}" 2>/dev/null)"
        if [ -n "${MASTER_KEY_VAL}" ]; then
            RUN_ARGS="${RUN_ARGS} -e HEVOLVE_MASTER_PRIVATE_KEY=${MASTER_KEY_VAL}"
            info "Master key loaded from ${MASTER_KEY_FILE}"
        fi
    else
        warn "Master key file not found at ${MASTER_KEY_FILE} (boot verification will use public key only)"
    fi

    # Release manifest
    if [ -f "${MANIFEST}" ]; then
        RUN_ARGS="${RUN_ARGS} -v ${MANIFEST}:/app/release_manifest.json:ro"
        info "Release manifest mounted"
    fi

    # Volume mounts
    RUN_ARGS="${RUN_ARGS} -v ${LOG_DIR}:/app/logs"
    RUN_ARGS="${RUN_ARGS} -v ${IMAGE_DIR}:/app/output_images"

    info "Starting ${CONTAINER_NAME} on port ${PORT}..."
    ${DOCKER_CMD} run ${RUN_ARGS} "${IMAGE}"

    # Wait for startup and show status
    sleep 2
    if ${DOCKER_CMD} ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        info "Container running"
        echo ""
        do_status
    else
        error "Container failed to start. Check logs:"
        ${DOCKER_CMD} logs "${CONTAINER_NAME}" --tail 30
        exit 1
    fi
}

# ── Logs ─────────────────────────────────────────────────────
do_logs() {
    ${DOCKER_CMD} logs "${CONTAINER_NAME}" -f --tail 100
}

# ── Status ───────────────────────────────────────────────────
do_status() {
    if ${DOCKER_CMD} ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "  Container:  ${CONTAINER_NAME}"
        echo "  Image:      ${IMAGE}"
        echo "  Port:       ${PORT}"
        echo "  Status:     $(${DOCKER_CMD} ps --format '{{.Status}}' -f name=${CONTAINER_NAME})"
        echo ""

        # Quick health check
        if command -v curl > /dev/null 2>&1; then
            HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:${PORT}/status" 2>/dev/null || echo "000")
            if [ "${HEALTH}" = "200" ]; then
                info "Health check: OK (HTTP 200)"
            else
                warn "Health check: HTTP ${HEALTH} (container may still be starting)"
            fi
        fi
    else
        warn "Container ${CONTAINER_NAME} is not running"
    fi
}

# ── Main ─────────────────────────────────────────────────────
case "${1:-}" in
    build)
        do_build
        ;;
    run)
        do_run
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        do_run
        ;;
    logs)
        do_logs
        ;;
    status)
        do_status
        ;;
    ""|start)
        do_build
        do_run
        ;;
    *)
        echo "Usage: scripts/start_docker.sh [build|run|stop|restart|logs|status]"
        exit 1
        ;;
esac

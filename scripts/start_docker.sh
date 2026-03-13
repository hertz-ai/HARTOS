#!/bin/bash
# ============================================================
# HART OS — Docker Build & Run (All Deployment Tiers)
# ============================================================
#
# Deployment tiers:
#   central   — Production server. Cloud LLM (GPT/Claude). Master key + signed manifest.
#               Requires: .env with OPENAI_API_KEY, HEVOLVE_DB_URL (cloud MySQL)
#               Optional: /etc/hevolve/master_private_key.hex, release_manifest.json
#
#   regional  — Regional host. Connects to a regional LLM server (llama.cpp/vLLM).
#               Requires: .env with HEVOLVE_LLM_ENDPOINT_URL
#               Optional: HART_NODE_KEY (federation HMAC), HEVOLVE_DB_URL
#
#   flat      — Local/desktop. Local llama.cpp on localhost.
#               Requires: .env with OPENAI_API_KEY or local llama.cpp running
#               Optional: HART_NODE_KEY (federation HMAC)
#
# Usage:
#   scripts/start_docker.sh                          # Build + run (tier from .env or default: flat)
#   scripts/start_docker.sh --tier central            # Central deployment
#   scripts/start_docker.sh --tier regional           # Regional deployment
#   scripts/start_docker.sh --tier flat               # Local/desktop
#   scripts/start_docker.sh build                     # Build only
#   scripts/start_docker.sh run                       # Run only (image must exist)
#   scripts/start_docker.sh run --tier regional       # Run as regional
#   scripts/start_docker.sh logs                      # Tail container logs
#   scripts/start_docker.sh stop                      # Stop + remove container
#   scripts/start_docker.sh restart                   # Stop + run (no rebuild)
#   scripts/start_docker.sh status                    # Show container status + health
#
# Minimal .env for community users:
#   OPENAI_API_KEY=sk-...
#   # Optional: join the hive
#   HART_NODE_KEY=your-secret-key
#   ENABLE_FEDERATION=true
#
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

# Master key location (central server only, never in repo)
MASTER_KEY_FILE="/etc/hevolve/master_private_key.hex"

# Default tier — overridden by --tier flag or HEVOLVE_NODE_TIER in .env
TIER=""

# ── Helpers ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
header() { echo -e "${CYAN}$*${NC}"; }

# Parse --tier from anywhere in args
ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --tier) TIER="$2"; shift 2 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
set -- "${ARGS[@]}"

# Resolve tier: --tier flag > HEVOLVE_NODE_TIER in .env > default flat
if [ -z "${TIER}" ] && [ -f "${ENV_FILE}" ]; then
    TIER=$(grep -E "^HEVOLVE_NODE_TIER=" "${ENV_FILE}" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
fi
TIER="${TIER:-flat}"

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

# ── CORS auto-config (only when this server resolves to hevolve.ai) ──
_configure_cors() {
    # Skip if CORS_ORIGINS already in .env
    if [ -f "${ENV_FILE}" ] && grep -q "^CORS_ORIGINS=" "${ENV_FILE}" 2>/dev/null; then
        info "CORS_ORIGINS already configured in .env"
        return
    fi

    # Resolve hevolve.ai to IP(s)
    HEVOLVE_IPS=""
    if command -v dig > /dev/null 2>&1; then
        HEVOLVE_IPS=$(dig +short hevolve.ai A 2>/dev/null | grep -E '^[0-9]+\.' || true)
    elif command -v nslookup > /dev/null 2>&1; then
        HEVOLVE_IPS=$(nslookup hevolve.ai 2>/dev/null | awk '/^Address:/{if(NR>2) print $2}' | grep -E '^[0-9]+\.' || true)
    elif command -v getent > /dev/null 2>&1; then
        HEVOLVE_IPS=$(getent ahosts hevolve.ai 2>/dev/null | awk '{print $1}' | sort -u | grep -E '^[0-9]+\.' || true)
    fi

    if [ -z "${HEVOLVE_IPS}" ]; then
        info "Could not resolve hevolve.ai — skipping CORS auto-config"
        return
    fi

    # Get this server's public IP
    MY_IP=""
    for svc in "https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com"; do
        MY_IP=$(curl -s --max-time 5 "${svc}" 2>/dev/null | grep -Eo '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' || true)
        [ -n "${MY_IP}" ] && break
    done

    if [ -z "${MY_IP}" ]; then
        info "Could not detect public IP — skipping CORS auto-config"
        return
    fi

    # Check if this server's IP matches any hevolve.ai A record
    if echo "${HEVOLVE_IPS}" | grep -qF "${MY_IP}"; then
        info "This server (${MY_IP}) resolves to hevolve.ai — configuring CORS"
        {
            echo ""
            echo "# Auto-configured: this server resolves to hevolve.ai"
            echo "CORS_ORIGINS=https://hevolve.ai,https://www.hevolve.ai,http://localhost:3000"
            echo "ALLOWED_HOSTS=localhost,127.0.0.1,azurekong.hertzai.com"
        } >> "${ENV_FILE}"
        info "Appended CORS_ORIGINS and ALLOWED_HOSTS to .env"
    else
        info "Server IP (${MY_IP}) does not match hevolve.ai (${HEVOLVE_IPS}) — no CORS auto-config"
    fi
}

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

    # Print deployment banner
    echo ""
    header "========================================"
    case "${TIER}" in
        central)
            header " HART OS — Central Deployment (Docker)"
            header " Cloud LLM + Master Key + Signed Manifest"
            ;;
        regional)
            header " HART OS — Regional Deployment (Docker)"
            header " Regional LLM Host + Federation"
            ;;
        flat|*)
            header " HART OS — Local/Desktop (Docker)"
            header " Local LLM or Cloud API"
            ;;
    esac
    header "========================================"
    echo ""

    # Build run command
    RUN_ARGS="-d --name ${CONTAINER_NAME} --restart unless-stopped"
    RUN_ARGS="${RUN_ARGS} -p ${PORT}:${PORT}"

    # Tier-specific env
    RUN_ARGS="${RUN_ARGS} -e HEVOLVE_NODE_TIER=${TIER}"

    # Env file (.env in repo root — has API keys, DB URL, federation keys)
    if [ -f "${ENV_FILE}" ]; then
        RUN_ARGS="${RUN_ARGS} --env-file ${ENV_FILE}"
        info "Env file: ${ENV_FILE}"
    else
        warn "No .env file found at ${ENV_FILE}"
        echo ""
        echo "  Create one with at minimum:"
        echo "    OPENAI_API_KEY=sk-..."
        echo ""
        if [ "${TIER}" = "central" ]; then
            echo "  For central deployment also add:"
            echo "    HEVOLVE_DB_URL=mysql+pymysql://user:pass@host/db"
            echo "    HEVOLVE_ENFORCEMENT_MODE=hard"
            echo ""
        elif [ "${TIER}" = "regional" ]; then
            echo "  For regional deployment also add:"
            echo "    HEVOLVE_LLM_ENDPOINT_URL=http://your-llm-server:8080/v1"
            echo "    HART_NODE_KEY=your-secret-key"
            echo ""
        fi
    fi

    # ── Auto-detect CORS for hevolve.ai production ──
    _configure_cors

    # ── Enable daemons on central/regional (flywheel requires them) ──
    if [ "${TIER}" = "central" ] || [ "${TIER}" = "regional" ]; then
        RUN_ARGS="${RUN_ARGS} -e HEVOLVE_AGENT_ENGINE_ENABLED=true"
        RUN_ARGS="${RUN_ARGS} -e HEVOLVE_CODING_AGENT_ENABLED=true"
    fi

    # ── Central-only: master key + signed manifest ──
    if [ "${TIER}" = "central" ]; then
        RUN_ARGS="${RUN_ARGS} -e HEVOLVE_ENFORCEMENT_MODE=hard"
        RUN_ARGS="${RUN_ARGS} -e HEVOLVE_DEV_MODE=false"

        if [ -f "${MASTER_KEY_FILE}" ]; then
            MASTER_KEY_VAL="$(sudo cat "${MASTER_KEY_FILE}" 2>/dev/null || cat "${MASTER_KEY_FILE}" 2>/dev/null)"
            if [ -n "${MASTER_KEY_VAL}" ]; then
                RUN_ARGS="${RUN_ARGS} -e HEVOLVE_MASTER_PRIVATE_KEY=${MASTER_KEY_VAL}"
                info "Master key loaded"
            fi
        else
            warn "Master key not found at ${MASTER_KEY_FILE}"
        fi

        if [ -f "${MANIFEST}" ]; then
            RUN_ARGS="${RUN_ARGS} -v ${MANIFEST}:/app/release_manifest.json:ro"
            info "Release manifest mounted"
        else
            warn "Release manifest not found at ${MANIFEST}"
        fi
    fi

    # Volume mounts (all tiers)
    RUN_ARGS="${RUN_ARGS} -v ${LOG_DIR}:/app/logs"
    RUN_ARGS="${RUN_ARGS} -v ${IMAGE_DIR}:/app/output_images"

    info "Tier: ${TIER}"
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
        echo "  Tier:       ${TIER}"
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
        echo "Usage: scripts/start_docker.sh [build|run|stop|restart|logs|status] [--tier central|regional|flat]"
        echo ""
        echo "Tiers:"
        echo "  central   — Production server with master key + cloud DB"
        echo "  regional  — Regional LLM host + federation"
        echo "  flat      — Local/desktop (default)"
        exit 1
        ;;
esac

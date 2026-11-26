#!/bin/bash
# Deploy or stop an isolated LLM candidate container on an openQA worker for benchmarking.
# Runs via SSH through the o3 bastion host. Does not touch production port 8080.

set -euo pipefail

BASTION="o3"
DEFAULT_WORKER="w26"
DEFAULT_IMAGE="ghcr.io/ggml-org/llama.cpp:server"
DEFAULT_PORT=8082
DEFAULT_THREADS=64
DEFAULT_CTX=65536

usage() {
    cat << 'USAGE'
Usage:
  deploy_candidate.sh start [--worker NAME] [--image IMAGE] [--draft <repo@file>]
      [--spec-type TYPE] [--spec-draft-n-max N] <hf_model[:quant]|repo@file> [port] [threads] [ctx]
  deploy_candidate.sh stop [--worker NAME] [port]
  deploy_candidate.sh status [--worker NAME] [port]

Options:
  --worker NAME           openQA worker behind the o3 bastion (default: w26)
  --image IMAGE           runner image (default: ghcr.io/ggml-org/llama.cpp:server)
  --draft repo@file       speculative draft model, repo optional (default: none)
  --spec-type TYPE        llama.cpp --spec-type value, e.g. draft-mtp (default: unset)
  --spec-draft-n-max N    maximum number of drafted tokens (default: unset, server default 3)

Examples:
  deploy_candidate.sh start unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M 8082 64 65536
  deploy_candidate.sh --worker w27 --image localhost/llama-fork:pinned start repo@file 8083
  deploy_candidate.sh --draft unsloth/gemma-4-26B-A4B-it-GGUF@mtp-gemma-4-26B-A4B-it.gguf \
      --spec-type draft-mtp --spec-draft-n-max 6 start unsloth/gemma-4-26B-A4B-it-GGUF@UD-Q4_K_M 8083
  deploy_candidate.sh status 8082
  deploy_candidate.sh stop 8082
USAGE
    exit 1
}

ACTION="${1:-}"
[ -z "$ACTION" ] && usage
shift

WORKER="$DEFAULT_WORKER"
IMAGE="$DEFAULT_IMAGE"
DRAFT=""
SPEC_TYPE=""
SPEC_DRAFT_N_MAX=""
POSITIONAL=()

while [ $# -gt 0 ]; do
    case "$1" in
        --worker)
            [ $# -ge 2 ] || usage
            WORKER="$2"
            shift 2
            ;;
        --image)
            [ $# -ge 2 ] || usage
            IMAGE="$2"
            shift 2
            ;;
        --draft)
            [ $# -ge 2 ] || usage
            DRAFT="$2"
            shift 2
            ;;
        --spec-type)
            [ $# -ge 2 ] || usage
            SPEC_TYPE="$2"
            shift 2
            ;;
        --spec-draft-n-max)
            [ $# -ge 2 ] || usage
            SPEC_DRAFT_N_MAX="$2"
            shift 2
            ;;
        -h | --help)
            usage
            ;;
        --*)
            echo "[ERROR] Unknown option: $1" >&2
            usage
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

SPEC_ARGS=""
if [ -n "$SPEC_TYPE" ]; then
    SPEC_ARGS=" --spec-type '${SPEC_TYPE}'"
fi
if [ -n "$SPEC_DRAFT_N_MAX" ]; then
    SPEC_ARGS="${SPEC_ARGS} --spec-draft-n-max '${SPEC_DRAFT_N_MAX}'"
fi
if [ -n "$DRAFT" ]; then
    if [[ "$DRAFT" == *"@"* ]]; then
        SPEC_ARGS="${SPEC_ARGS} --spec-draft-hf '${DRAFT%%@*}' --spec-draft-model '${DRAFT##*@}'"
    else
        SPEC_ARGS="${SPEC_ARGS} --spec-draft-model '${DRAFT}'"
    fi
fi

echo "[INFO] Target worker: ${WORKER}, image: ${IMAGE}"
if [ -n "$DRAFT" ]; then
    echo "[INFO] Speculative decoding: draft ${DRAFT}, spec-type ${SPEC_TYPE:-unset}, n-max ${SPEC_DRAFT_N_MAX:-unset}"
fi

case "$ACTION" in
    start)
        MODEL="${POSITIONAL[0]:-}"
        PORT="${POSITIONAL[1]:-$DEFAULT_PORT}"
        THREADS="${POSITIONAL[2]:-$DEFAULT_THREADS}"
        CTX="${POSITIONAL[3]:-$DEFAULT_CTX}"
        [ -z "$MODEL" ] && usage

        # Support repo@file syntax to pin exact GGUF file
        if [[ "$MODEL" == *"@"* ]]; then
            HF_ARGS="-hf '${MODEL%%@*}' --hf-file '${MODEL##*@}'"
        else
            HF_ARGS="-hf '${MODEL}'"
        fi

        CONTAINER_NAME="llama-bench-${PORT}"
        echo "[INFO] Deploying candidate $MODEL on $WORKER:$PORT (container: $CONTAINER_NAME)..."

        # the remote command needs the local values of ${PORT} and ${CONTAINER_NAME}
        # shellcheck disable=SC2087
        ssh -o BatchMode=yes "$BASTION" ssh -o BatchMode=yes "$WORKER" bash -s << EOF
set -e
# Check if port is already in use
if ss -tulpn | grep -q ":${PORT} "; then
    echo "[ERROR] Port ${PORT} is already in use on ${WORKER}!"
    exit 1
fi

# Clean up any leftover stopped container with the same name
su - llama -c "podman rm -f ${CONTAINER_NAME} 2>/dev/null || true"

# Launch candidate container
echo "[INFO] Starting container ${CONTAINER_NAME}..."
su - llama -c "podman run --name ${CONTAINER_NAME} -d \
  -v /opt/llm/models:/root/.cache/huggingface:z \
  -p ${PORT}:8080 \
  ${IMAGE} \
  ${HF_ARGS}${SPEC_ARGS} \
  --threads ${THREADS} \
  --ctx-size ${CTX} \
  --cache-ram 65536 \
  -ctk q8_0 \
  -ctv q8_0 \
  -fa on \
  --no-mmproj \
  --temp 1.0 \
  --top-p 0.95 \
  --top-k 64"

# Wait for container health check
echo "[INFO] Waiting for ${CONTAINER_NAME} to become healthy on port ${PORT}..."
for i in \$(seq 1 60); do
    if curl -s -f "http://127.0.0.1:${PORT}/health" | grep -q '"status":"ok"'; then
        echo "[INFO] Container ${CONTAINER_NAME} is healthy on port ${PORT}!"
        exit 0
    fi
    sleep 5
done
echo "[ERROR] Timed out waiting for ${CONTAINER_NAME} to become healthy."
su - llama -c "podman logs --tail 50 ${CONTAINER_NAME}"
exit 1
EOF
        ;;

    stop)
        PORT="${POSITIONAL[0]:-$DEFAULT_PORT}"
        CONTAINER_NAME="llama-bench-${PORT}"
        echo "[INFO] Stopping container $CONTAINER_NAME on $WORKER..."
        # the remote command needs the local values of ${PORT} and ${CONTAINER_NAME}
        # shellcheck disable=SC2087
        ssh -o BatchMode=yes "$BASTION" ssh -o BatchMode=yes "$WORKER" bash -s << EOF
su - llama -c "podman rm -f ${CONTAINER_NAME} 2>/dev/null || true"
echo "[INFO] Container ${CONTAINER_NAME} removed."
EOF
        ;;

    status)
        PORT="${POSITIONAL[0]:-$DEFAULT_PORT}"
        CONTAINER_NAME="llama-bench-${PORT}"
        echo "[INFO] Checking status of $CONTAINER_NAME on $WORKER:$PORT..."
        # the remote command needs the local values of ${PORT} and ${CONTAINER_NAME}
        # shellcheck disable=SC2087
        ssh -o BatchMode=yes "$BASTION" ssh -o BatchMode=yes "$WORKER" bash -s << EOF
if ss -tulpn | grep -q ":${PORT} "; then
    echo "[INFO] Port ${PORT} is listening."
    curl -s "http://127.0.0.1:${PORT}/health" || true
    echo
    curl -s "http://127.0.0.1:${PORT}/props" | head -c 200 || true
    echo
else
    echo "[INFO] Port ${PORT} is not listening."
fi
su - llama -c "podman ps -a --filter name=${CONTAINER_NAME}"
EOF
        ;;

    *)
        usage
        ;;
esac

#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HF_HOME" "$GRADIO_TEMP_DIR"

if [[ -z "${GRADIO_ROOT_PATH:-}" && -n "${RUNPOD_POD_ID:-}" ]]; then
    export GRADIO_ROOT_PATH="https://${RUNPOD_POD_ID}-8001.proxy.runpod.net"
fi

# Keep the inherited nginx and SSH setup without persisting HF_TOKEN in /etc/rp_environment.
env -u HF_TOKEN /start.sh &

exec omnivoice-demo --device cuda --ip 0.0.0.0 --port 8000 "$@"

#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HF_HOME" "$GRADIO_TEMP_DIR"

# Keep the inherited nginx and SSH setup without persisting HF_TOKEN in /etc/rp_environment.
env -u HF_TOKEN /start.sh &

exec omnivoice-demo --device cuda --ip 0.0.0.0 --port 8000 "$@"

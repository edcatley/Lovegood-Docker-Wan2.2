#!/usr/bin/env bash
set -e

# Use libtcmalloc for better memory management
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"

# Suppress verbose logging
export TORCH_LOGS="-all"
export TORCH_CPP_LOG_LEVEL="ERROR"
export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
export CUDA_LAUNCH_BLOCKING="0"
export PYTHONWARNINGS="ignore"

# Create symlink from the platform's network volume mount point to our stable internal path.
# Override NETWORK_VOLUME_PATH to match whatever the platform uses (e.g. /workspace, /vast, etc.)
# Defaults to /workspace (RunPod pods).
: "${NETWORK_VOLUME_PATH:=/workspace}"

if [ -d "${NETWORK_VOLUME_PATH}" ] && [ ! -L /network-volume ]; then
    echo "lovegood-comfyui - Linking ${NETWORK_VOLUME_PATH} -> /network-volume"
    ln -s "${NETWORK_VOLUME_PATH}" /network-volume
elif [ ! -d "${NETWORK_VOLUME_PATH}" ]; then
    echo "lovegood-comfyui - WARNING: ${NETWORK_VOLUME_PATH} not found, models may not load"
fi

# Set ComfyUI-Manager to offline mode
comfy-manager-set-mode offline || echo "lovegood-comfyui - Could not set ComfyUI-Manager network_mode" >&2

: "${COMFY_LOG_LEVEL:=INFO}"

echo "lovegood-comfyui - Starting ComfyUI on port 8188..."
python -u /comfyui/main.py \
    --disable-auto-launch \
    --disable-metadata \
    --listen 0.0.0.0 \
    --verbose "${COMFY_LOG_LEVEL}" \
    --log-stdout &

echo "lovegood-comfyui - Starting sidecar on port 8189..."
exec python -u /sidecar/handler.py

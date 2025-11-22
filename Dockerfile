# Step 1: Start FROM the official, versioned RunPod ComfyUI image.
FROM runpod/worker-comfyui:5.5.0-base

# Step 2: Install our single, required Python dependency for GCS.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Step 3: Use the built-in script to permanently install our custom nodes.
# This is the "official" way to add nodes. We just add our GCS uploader to the list.
RUN comfy-node-install comfyui-videohelpersuite comfymath seedvr2_videoupscaler comfyui-frame-interpolation tripleksampler randomseedgenerator comfyui-unload-model

COPY src/handler.py .

# Change working directory to ComfyUI
WORKDIR /comfyui

# Support for the network volume
ADD src/extra_model_paths.yaml ./

# Go back to the root
WORKDIR /
